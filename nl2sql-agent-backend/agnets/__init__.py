"""DeepAgents 智能体：装配技能库与短期记忆（checkpoint），供上层按会话跑多轮对话。

只做装配，不含业务链路（检索、采集那些图另见 plugins.agents）：

- 模型不在这里定义：每轮开跑时按 ``model`` 表里的默认登记取对话 / 嵌入 / 重排实例
  （见 plugins.agents.models，换模型只改库里的登记）。系统提示词只讲调度
  （见 agnets.prompts.ORCHESTRATOR_PROMPT）：按「指标口径 → 数据表 → SQL」把活派给子智能体；
  各子智能体的判断细节在技能里（见 skills/），deepagents 自带中间件的文件与技能说明另由中间件拼接；
- 短期记忆用 :class:`agnets.checkpointers.CachedAsyncPostgresSaver`：checkpoint 落
  PostgreSQL，读写先命中 Redis 缓存；带同一个 ``thread_id`` 再进来即从上一轮的状态接着跑。
  会话状态里有 DeltaChannel 的 messages，恢复时按 langgraph 默认的 ``JsonPlusSerializer``
  就能反序列化，不必额外登记允许的类型；
- 技能从项目根的 ``skills`` 目录加载：每个技能一个子目录、内含 ``SKILL.md``（frontmatter
  里写 ``name`` 与 ``description``，目录名要与 ``name`` 一致），按需再用 ``read_file``
  读全文（渐进式披露，见 deepagents 的 SkillsMiddleware）；
- 文件工具的默认后端是 ``StateBackend``（文件随会话走、不落磁盘），只有 ``/skills/``
  前缀路由到真实的 skills 目录（见 ``CompositeBackend``）：模型读得到技能，写不进项目目录。

用法::

    from agnets import RetrievalAgent

    agent = RetrievalAgent()  # 插件单例，应用启动时由 register_plugins 调 register()
    async for state in agent.stream(question, database_id, thread_id):
        print(state["sql"], state["data"])   # 检索接口直接把这些状态帧打成 SSE（见 controllers.retrieval）
    await agent.close()                     # 应用关闭时由 close_plugins 调
"""

from pathlib import Path
from typing import Any, AsyncGenerator

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph.state import CompiledStateGraph
from loguru import logger
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agnets.callbacks import RunStats, ToolLogger
from agnets.checkpointers import CachedAsyncPostgresSaver
from agnets.middlewares import DefaultModelMiddleware
from agnets.prompts import ORCHESTRATOR_PROMPT
from agnets.schemas import Context, State, TableInfo, dump_metric
from agnets.subagents import METRIC_CALIBER_REVIEWER, SKILLS_ROUTE, SQL_WRITER, TABLE_RECALLER
from config import POSTGRESQL_URL
from models.meta import ColumnRoleOption, TableRoleOption
from plugins import Plugin
from agnets.models import fetch_chat_model, fetch_embedding_model, fetch_rerank_model
from plugins.snowflake import Snowflake

#: 会话状态里会被序列化进 checkpoint 的枚举：表 / 字段的角色（见 agnets.schemas 的 TableInfo、ColumnInfo）。
#: msgpack 按「模块 + 类名」复原对象，不登记就只是警告、再往后会直接拒绝反序列化
CHECKPOINT_ALLOWED_MODULES = [
    ("models.meta", TableRoleOption.__name__),
    ("models.meta", ColumnRoleOption.__name__)
]

#: 技能目录：与 agnets 同级（项目根下），每个子目录是一个技能。挂载路径见 agnets.subagents.SKILLS_ROUTE
#: （子智能体也要按同一路径读技能，故常量放在那边、这里引用）
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


class RetrievalAgent(Plugin):
    """DeepAgents 智能体的装配入口。

    池、saver 与编译好的图都在 :meth:`register` 里装配：带 checkpointer 的图要求连接池在运行中的
    事件循环里建，而本类在导入时就可能被实例化——那一刻还没有循环。
    """

    def __init__(self):
        self.graph: CompiledStateGraph | None = None
        #: 评测用的图（不带 checkpointer，见 eval）：整批复用，不落会话
        self.eval_graph: CompiledStateGraph | None = None
        self.pool: AsyncConnectionPool | None = None
        self.saver: CachedAsyncPostgresSaver | None = None

    async def register(self) -> None:
        """装配短期记忆存储并编译智能体图（应用启动时调用一次）。

        连接池按 langgraph 的硬性要求建：autocommit + dict_row（``setup()`` 里有
        ``CREATE INDEX CONCURRENTLY``，不能跑在事务块里；查询要按列名取值）。
        缓存用的 Redis 客户端由 saver 自己按 :mod:`config` 建、自己关，这里不必操心。

        对话模型取自 ``model`` 表里的默认登记：库里还没登记时只告警、把图的装配延后到第一次提问
        （见 :meth:`stream`），免得因为一次配置没搬完就让整个应用起不来。
        """
        pool = AsyncConnectionPool(
            POSTGRESQL_URL,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row}
        )
        await pool.open()
        # 状态里的表 / 字段角色是 StrEnum，序列化后要按「模块 + 类名」复原，故显式登记（见 CHECKPOINT_ALLOWED_MODULES）
        saver = CachedAsyncPostgresSaver(pool, serde=JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_ALLOWED_MODULES))
        await saver.setup()
        self.pool = pool
        self.saver = saver
        try:
            self.graph = self.build(await fetch_chat_model())
        except RuntimeError as error:  # 库里还没有对话模型：图延后到首次提问时再建
            logger.warning(f"agent: 暂无可用的对话模型，图延后装配（首次提问时再试）：{error}")
        logger.info("agent: 短期记忆与技能库就绪（checkpoint 落 PostgreSQL，缓存走 Redis）")

    async def rebuild(self) -> None:
        """按库里当前的默认对话模型重编一次图（一般不需要：中间件每轮自己换）。

        图里的模型只是兜底——真正用哪条由 ``DefaultModelMiddleware`` 在每次模型调用时按库决定
        （见 agnets.middleware），所以换默认后最迟几秒就生效，不必重编。这里留着给「兜底也要跟着换」
        或中间件被摘掉的场景；图未装配（应用还没 register）时直接返回。
        """
        if self.graph is None:
            return
        self.graph = self.build(await fetch_chat_model())
        logger.info("agent: 对话模型已按库里的默认登记重编")

    async def close(self) -> None:
        """收掉短期记忆占用的连接池（应用关闭时调用）。"""
        if self.pool is not None:
            await self.pool.close()

    def build(self, model: BaseChatModel, checkpointer: bool = True) -> CompiledStateGraph:
        """编译智能体图：技能目录做只读挂载，其余文件操作留在会话状态里。

        :param model: 兜底的对话模型（由调用方按 ``model`` 表里的默认登记取，见
            plugins.agents.models.chat_model）。实际每次调用用哪条由 ``DefaultModelMiddleware`` 按库决定，
            见 agnets.middleware——换默认不必重编图
        :param checkpointer: 是否挂短期记忆：会话链路要挂（见 register）；评测链路不挂，
            一条问题跑完就走，不该往会话存储里写状态（见 eval）

        技能的来源是后端路径而不是宿主机路径：``/skills/`` 由 ``CompositeBackend`` 转给以
        ``skills`` 目录为根的 ``FilesystemBackend``，未命中的路径仍归默认的 ``StateBackend``。
        技能目录为空时也能正常编译：技能清单退化为一句「暂无技能」。

        状态与上下文按本包的 schema 传给图：``state_schema=State`` 才能在会话状态里带上业务库编号等
        字段（工具要读它，见 agnets.tools.recall_metrics），同时保住 DeepAgentState 的增量 messages 通道；
        ``context_schema=Context`` 让嵌入与重排模型随每轮调用注入，工具从 runtime.context 取用。
        调用的写法::

            await agent.graph.ainvoke(
                {"messages": [...], "database_id": 1},
                context=Context(model=..., embedding_model=..., rerank_model=...)
            )

        子智能体见 agnets.subagents：派活时它继承了父级状态（``database_id`` 因此也在子智能体里）与同一份
        运行上下文，故它手上的工具能照常查库、读技能。

        系统提示词（``system_prompt``）只讲调度：按「指标口径 → 数据表 → SQL」的顺序派活，
        再按子智能体回话开头的标签决定下一步派给谁（见 agnets.prompts）。主智能体自己不挂工具，
        具体的库、SQL、字段定义都在子智能体手上。
        """
        backend = CompositeBackend(
            default=StateBackend(),
            routes={SKILLS_ROUTE: FilesystemBackend(root_dir=SKILLS_DIR)}
        )
        return create_deep_agent(
            model=model,
            system_prompt=ORCHESTRATOR_PROMPT,
            backend=backend,
            skills=[SKILLS_ROUTE],
            tools=[],
            # 中间件每次模型调用都按库里当前的默认登记换模型（见 agnets.middleware）：
            # 上面这个 model 只是兜底，切换默认后不必重编图
            middleware=[DefaultModelMiddleware()],
            subagents=[METRIC_CALIBER_REVIEWER, TABLE_RECALLER, SQL_WRITER],
            state_schema=State,
            context_schema=Context,
            checkpointer=self.saver if checkpointer else None
        )

    async def stream(self, question: str, database_id: int,
                     thread_id: str | None = None) -> AsyncGenerator[dict[str, Any], Any]:
        """把一轮提问跑成一串进度帧（供 controllers.retrieval 打成 SSE），只带调用方要看的几样东西。

        :param question: 用户这一轮的输入；带同一个 ``thread_id`` 再进来就是同一会话的下一轮
            （多轮上下文由 checkpoint 带着走，不需要恢复机制——反问是交给用户的一句话，不是图上的 interrupt）
        :param database_id: 业务库编号：子智能体的工具都从会话状态里读它
        :param thread_id: 会话编号（雪花 ID 的十进制文本）；不传则新生成一个
        :return: 逐帧的进度，每帧的键固定为：

            - ``messages``：本轮新增的模型消息文本（子智能体的回话 + 主智能体的答复，按产生顺序）；
            - ``current_message``：其中最后一条（前端直接显示「当前进度」用）；
            - ``error``：最后一次失败的原因，没有则为 null；
            - ``metrics``：定稿的指标口径（id 之外的 name / description / alias）；
            - ``tables``：最终候选表（id / name / role / description，不含字段与取值，免得每帧重复一大坨）；
            - ``sql``：通过校验的语句，没有则为 null；
            - ``data``：结果集，没有则为 null；
            - ``tokens``：本轮到此刻的累计用量（prompt / completion / total）；
            - ``elapsed``：本轮到此刻的耗时（秒，两位小数）。

        帧由图的 ``values`` 流产出：每个节点跑完一帧。耗时与 Token 由
        :class:`agnets.callbacks.RunStats` 随回调累计（会穿透到子图，子智能体的调用也算在内）；
        整轮跑完再打一条总账（总耗时 / 其中模型调用耗时 / token，见 ``RunStats.summary``）。

        每次调用前把 ``data`` 清空（随输入写回 None）：状态是跨轮累积的，不清的话上一轮的结果集会被当成
        这一轮的帧推给前端，直到新 SQL 跑完才被覆盖——中途看到的「数据」根本不是这次查询的结果。
        ``retry_count`` 同样按轮清零（反问次数与 SQL 纠正次数共用它）：额度是「这一轮」的账，
        不带上一轮的账；上一轮到顶了，新问题也要能重新问、重新改。
        """
        if self.saver is None:
            raise RuntimeError("智能体图尚未装配：应用启动时应先调用 register()")
        if self.graph is None:
            # 启动时库里还没有对话模型（见 register）：现在有了就补建
            self.graph = self.build(await fetch_chat_model())
        thread = thread_id or str(Snowflake.get_id())
        stats = RunStats()
        # 本轮用的三类模型都按 model 表里的默认登记取（见 plugins.agents.models，实例有进程内缓存）
        context = Context(model=await fetch_chat_model(), embedding_model=await fetch_embedding_model(),
                          rerank_model=await fetch_rerank_model())
        # 回调：RunStats 记 Token 与耗时，ToolLogger 记每次工具调用（两者都会穿透到子图）
        config: RunnableConfig = {"configurable": {"thread_id": thread}, "callbacks": [stats, ToolLogger()]}
        payload = {"messages": [HumanMessage(question)], "question": question, "database_id": database_id,
                   "data": None, "retry_count": 0}
        # 历史消息长度：只把本轮新增的模型文本交给调用方，不把上一轮的对话再推一遍
        history = 0
        if self.saver is not None:
            history = len((await self.graph.aget_state(config)).values.get("messages") or [])
        try:
            async for values in self.graph.astream(payload, context=context, config=config,
                                                   stream_mode="values"):
                texts = [
                    message.content for message in (values.get("messages") or [])[history:]
                    if isinstance(message.content, str) and message.content.strip()
                    and (isinstance(message, AIMessage) or getattr(message, "name", None) == "task")
                ]
                yield {
                    "messages": texts,
                    "current_message": texts[-1] if texts else "",
                    "error": values.get("error"),
                    "metrics": [dump_metric(metric) for metric in values.get("metric_infos") or []],
                    "tables": [
                        {"id": table["id"], "name": table["name"], "description": table["description"],
                         "role": str(table["role"]) if table["role"] else None}
                        for table in values.get("final_table_infos") or []
                    ],
                    "sql": values.get("sql"),
                    "data": values.get("data"),
                    "tokens": dict(stats.tokens),
                    "elapsed": stats.elapsed
                }
        except Exception as error:  # noqa: BLE001 - 检索接口按错误帧收尾，不把异常漏给 SSE
            logger.error(f"stream: {error}, {stats.summary()}")
            yield {"messages": [], "current_message": "", "error": str(error), "metrics": [], "tables": [],
                   "sql": None, "data": None, "tokens": dict(stats.tokens), "elapsed": stats.elapsed}
        else:
            logger.info(f"stream: 本轮结束，{stats.summary()}")

    async def eval(self, question: str, database_id: int) -> tuple[str | None, list[TableInfo]]:
        """评测用：把一条问题跑一遍，返回生成的 SQL 与最终选中的表（不推帧、不落会话）。

        :param question: 待评测的问题（数据集条目里的那条）
        :param database_id: 该问题所属的业务库编号
        :return: (SQL, 表信息)：没生成或没通过校验的 SQL 为 None；表信息是 ``final_table_infos``，
            评估侧据此把「检索到的片段」落库（见 models.dataset.Result.retrieved_contexts）

        走的是不带 checkpointer 的图（见 build 的 checkpointer 参数）：评测是批量的、没有人来回答反问，
        也不该把状态写进会话存储。图编译一次整批复用，缓存见 eval_graph。
        """
        if self.eval_graph is None:
            self.eval_graph = self.build(await fetch_chat_model(), checkpointer=False)
        context = Context(model=await fetch_chat_model(), embedding_model=await fetch_embedding_model(),
                          rerank_model=await fetch_rerank_model())
        stats = RunStats()
        payload = {"messages": [HumanMessage(question)], "question": question, "database_id": database_id,
                   "data": None, "retry_count": 0}
        values: dict[str, Any] = {}
        # 评测也要看得到工具调用与总耗时：并发跑的时候按日志追每条问题卡在哪一步、花了多久
        config: RunnableConfig = {"callbacks": [stats, ToolLogger()]}
        async for values in self.eval_graph.astream(payload, context=context, stream_mode="values",
                                                    config=config):
            pass
        logger.info(f"eval: sql={'yes' if values.get('sql') else 'no'}, "
                    f"{len(values.get('final_table_infos') or [])} table(s), {stats.summary()}")
        return values.get("sql"), list(values.get("final_table_infos") or [])

    async def forget(self, thread_id: str) -> None:
        """清除某个会话的记忆：把这个 thread_id 下的全部 checkpoint 删掉（删会话登记前调用）。

        :raises RuntimeError: 短期记忆尚未装配（应用启动时没走到 register）

        删完该编号即从记忆里消失：之后带同一个编号再提问，就是全新的一轮，而不是接着上一轮的历史。
        """
        if self.saver is None:
            raise RuntimeError("短期记忆尚未装配：应用启动时应先调用 register()")
        await self.saver.adelete_thread(str(thread_id))
        logger.info(f"forget: thread {thread_id} 的记忆已清除")
