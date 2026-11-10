"""智能体可调用的工具。

两条件，各两个工具，都是一前一后：

指标（判断流程见 skills/metric-caliber-review）：

- :func:`recall_metrics`：把用户问题交给 Qdrant 的指标集合召回候选，再用重排模型按口径相关度排序，
  断崖截断后把最相关的几条写回会话状态的 ``metric_infos``，同时交回给模型——每条带编号、名称、口径描述
  与别名，据它判断用户要的口径有没有着落；
- :func:`commit_metrics`：定稿。按编号锁定口径对上的指标，顺便把用户补充的新口径登记进指标库并向量化，
  结果整份覆盖 ``metric_infos``——候选在这一刻收口，下游生成 SQL 只看这一份。

表（判断流程见 skills/table-recall）：

- :func:`recall_tables`：一批自然语言语句并行召回数据表，按用户问题重排、断崖截断取 TopK 后**追加**进
  ``table_infos``，并把表结构交回给模型筛表；
- :func:`commit_tables`：定稿。关联关系落库并回填到表结构，表**追加**进 ``final_table_infos``。

SQL（判断流程见 skills/sql-generation）：

- :func:`read_sql_context`：把生成 SQL 要用的输入交回给模型——用户问题、候选表（含取值样本与可关联字段）、
  指标口径与库信息；
- :func:`check_sql`：执行前的闸门。先挡数据变更与多语句，再用 EXPLAIN 校验语法与库结构，通过才写进 ``sql``；
- :func:`execute_sql`：执行 ``sql``，结果集整份写进 ``data``，只回行数与列名给模型。

反问或纠正次数到顶后，:func:`commit_metrics` / :func:`check_sql` / :func:`execute_sql` 不再干活，
直接交回一条「不可继续执行」的消息（见 :func:`blocked_by_limit`）——到顶就该收口，而不是继续试。

依赖的模型（嵌入、重排）随图上下文（:class:`agnets.schemas.Context`）传入，召回哪个业务库取自会话状态
（:class:`agnets.schemas.State` 的 ``database_id``），二者都由工具运行时（``ToolRuntime``）拿：模型不必、
也无法自己猜库编号。批量写入一律走 bulk_create、批量查询一次取回，不在循环里逐条 await。
"""

import asyncio
import re

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from loguru import logger

from agnets.schemas import ColumnInfo, Context, MetricDefinition, MetricInfo, State, TableInfo, TableRelation
from models import execute_raw_sql, explain_sql
from models.meta import Column, ColumnRelation, Metric, Table, TableRoleOption
from plugins.agents.prompts import METRIC_RERANK_INSTRUCT, TABLE_RERANK_INSTRUCT
from agnets.rerank import RerankClient, RerankResult
from plugins.qdrant import Qdrant

# 向量库与重排客户端都是无状态的常驻组件，随进程复用
qdrant = Qdrant()
rerank_client = RerankClient()

# 断崖式截断的三个阈值：
# 相邻分数落差的绝对值下限、落差占前一名分数的比值下限，以及 topK 的固定上限
ABSOLUTE_GAP = 0.5
RATIO_GAP = 0.25
MAX_TOP_K = 5

#: 指标口径最多反问几次：用户答得不全、或者敷衍了事时换个问法再问，到顶后 commit_metrics 不再执行，
#: 交回「不可继续执行」，由模型按现有口径收口。计数存在 State.retry_count，由 commit_metrics 维护
MAX_ASK_RETRY = 3

#: SQL 语法 / 执行错误最多纠正几次：到顶后 check_sql / execute_sql 不再执行，交回「不可继续执行」，
#: 由模型在回话里说明卡在哪。计数与反问共用 State.retry_count，由 check_sql / execute_sql 维护
MAX_SQL_RETRY = 5

#: 只读守卫要排除的关键字（数据变更 / 结构变更 / 文件外写 / 加锁读）：命中即拒绝，连 EXPLAIN 都不发
SQL_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|replace|merge|truncate|drop|alter|create|grant|revoke|"
    r"call|exec|execute|load|outfile|dumpfile|rename|handler)\b",
    re.I
)

#: 判断关键字之前先剔掉注释与字符串字面量，免得「字符串里写了 update」「列名叫 delete」被误判
SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
SQL_QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`", re.S)


def decide_top_k(results: list[RerankResult]) -> list[RerankResult]:
    """按相关度降序断崖式取 topK（结果已由重排模型按分数降序返回）。

    从第一名起逐条比较相邻分数，出现断崖即在其前方截断；记 gap 为与前一名的落差、
    previous 为前一名的分数，满足任一条件即为断崖：

    - 落差绝对值：gap >= ABSOLUTE_GAP；
    - 落差比值：gap / previous >= RATIO_GAP。

    故 K 至少为 1：第一名没有前一名可比，始终保留；K 另有固定上限 MAX_TOP_K，
    分数密集而截不出断崖时也不会超过这一上限。
    """
    kept: list[RerankResult] = []
    for result in results:
        # 先判上限：已达上限就不再往下看，断崖与否都不再保留
        if len(kept) >= MAX_TOP_K:
            break
        if kept:
            previous = kept[-1].score
            gap = previous - result.score
            if gap >= ABSOLUTE_GAP or (previous and gap / previous >= RATIO_GAP):
                break
        kept.append(result)
    if len(kept) < len(results):
        logger.info(f"top_k: kept {len(kept)} of {len(results)} candidate(s), "
                    f"scores={[round(x.score, 6) for x in results[:len(kept) + 1]]}")
    return kept


def blocked_by_limit(times: int, limit: int, what: str, tool_call_id: str) -> Command | None:
    """到顶就拦下：反问 / 纠正次数已满时工具不再干活，只交回一条「不可继续执行」的消息。

    返回 None 表示还有额度，调用方照常往下做；返回 Command 时调用方应直接把它交回给模型——
    这条路上不改状态、不碰数据库，免得「到顶了还在写」把已经定下来的结果搅乱。

    Args:
        times: 这一类已经用掉几次（调用方从状态里的 `retry_count` 取）。
        limit: 上限，与 `times` 同一口径。
        what: 被拦的是哪一类活（反问 / SQL 纠正），写进日志与回话里。
        tool_call_id: 当前这次工具调用的编号（取自 `runtime.tool_call_id`），用来把消息配给这次调用。
    """
    if times < limit:
        return None
    logger.warning(f"blocked_by_limit: {what}已达上限（{times}/{limit}），拒绝继续执行")
    content = (f"{what}已达上限（{limit} 次）：本次不执行，也不用再调本工具；"
               f"直接回话把卡在哪一步、结论是什么告诉主智能体。")
    return Command(update={"messages": [ToolMessage(content=content, tool_call_id=tool_call_id)]})


@tool("recall_metrics", parse_docstring=True)
async def recall_metrics(runtime: ToolRuntime[Context, State]) -> Command:
    """按用户问题召回业务指标，按相关度重排后把候选写回会话状态，并以一段自然语言交回给模型。

    检索文本直接取自会话状态的 `question`（用户原话，调用方已经写进去），不用你传参：
    问题整句交给向量库，候选指标再交给重排模型按「口径是否与问题一致」排序，断崖截断后取最相关的几条。
    候选**整份覆盖**写进 state.metric_infos（这就是本轮召回阶段的候选，定稿见 commit_metrics），
    同时交回给模型。你没拿到用户问题？它在派给你的任务说明里——原话照读，不必改写。

    回话是一段自然语言，每条候选一行、按相关度从高到低（已断崖截断，至多 5 条）：
    「1. GMV（编号 1735600000000000001）：已支付订单金额之和…；别名：成交总额、销售额」。
    其中**编号**定稿时用它指代这条指标（commit_metrics 的 metric_ids），不要自己编号、不要用下标——
    名称可能重名，编号才是库里那一条的凭据；描述是口径（统计范围 + 计算方式 + 过滤条件 + 时间口径），
    判断口径对不对得上就看它。一条都没召回到时回话会明说，此时按「问题不需要指标」处理即可。

    Args:
        runtime: 运行时自动注入（会话状态与运行上下文），调用时不用提供。
    """
    database_id = runtime.state["database_id"]
    question = runtime.state["question"]
    context = runtime.context
    metrics = await qdrant.find_metrics(database_id, [question], context.embedding_model)
    logger.info(f"recall_metrics: question={question!r}, "
                f"{len(metrics)} metric(s) recalled in database {database_id}")

    # 指标名 / 描述 / 别名只存元数据库一份，向量库只回答「哪些指标相关」；这里按行显式取值，
    # 不用 Model.dump()——那会把所属库等与本工具无关的列也带出来
    candidates = [
        MetricInfo(id=metric.id, name=metric.name or "", description=metric.description or "",
                   alias=metric.alias or [])
        for metric in metrics
    ]
    kept = []
    if candidates:
        # 重排文本按 prompts/metric_rerank.md 约定的格式拼：指标名 + 描述 + 别名
        documents = [f"指标：{x['name']}；描述：{x['description']}；别名：{'、'.join(x['alias'] or [])}"
                     for x in candidates]
        results = await rerank_client.rerank(question, documents, context.rerank_model,
                                             instruct=METRIC_RERANK_INSTRUCT)
        # 重排结果已按相关度降序，断崖截断后按原顺序取回（列表顺序即重排后的顺序，供模型按编号引用）
        kept = [candidates[result.index] for result in decide_top_k(results)]
        logger.info(f"recall_metrics: kept {len(kept)} of {len(candidates)} metric(s)")

    # 回话拼成自然语言：一行一条候选，带编号（定稿凭据）、名称、口径描述与别名
    if kept:
        lines = [f"召回到 {len(kept)} 条候选指标（按相关度从高到低）："]
        for index, metric in enumerate(kept, start=1):
            alias = "；别名：" + "、".join(metric["alias"]) if metric["alias"] else ""
            lines.append(f"{index}. {metric['name']}（编号 {metric['id']}）：{metric['description']}{alias}")
        content = "\n".join(lines)
    else:
        content = "没有召回到任何指标：库里可能还没登记指标，或没有一条口径与问题相关；按「问题不需要指标」处理。"
    # 候选先落进状态：这一轮口径对不上、或用户还没确认时，下游至少知道召回看过什么；
    # 定稿由 commit_metrics 覆盖成真正要用的那几条
    return Command(update={
        "messages": [ToolMessage(content=content, tool_call_id=runtime.tool_call_id)],
        "metric_infos": kept
    })


@tool("commit_metrics", parse_docstring=True)
async def commit_metrics(metric_ids: list[int],
                         metrics: list[MetricDefinition],
                         runtime: ToolRuntime[Context, State],
                         question: str | None = None,
                         missing: list[str] | None = None) -> Command:
    """确定这一轮用哪些业务指标：锁定口径对上的指标、登记用户补充的新口径，并把改写后的问题写回状态。

    结果整份覆盖写回会话状态的 metric_infos（召回阶段的候选到此作废），下游据此生成 SQL。
    已存在的指标用 recall_metrics 交回的编号指代；用户补充的、库里还没有的口径走 metrics 参数——
    它们没有编号，这里按当前业务库归属登记进指标库并向量化，之后同一说法就能直接召回。
    只认本库的编号：别的库的、以及编造出来的编号会被丢掉，并在回话里列出来。

    同时把 `question`（你改写过的用户问题：指标名称换成了口径描述、读起来通顺）覆盖写回
    state.question，下游拿到的就是带口径的问题；不传则状态里的问题保持原样。

    反问次数也在这里记：传了 `missing`（还有口径没对上、这一轮要反问用户）就累加一次，
    没有缺口（`missing` 空）视为这一轮口径确认完毕，计数清零。
    问到上限（3 次）还缺口径时**本工具不再执行**：不改状态、不登记指标，回话会明说已达上限——
    这时候不要再问、也不用再调它，按现有口径收口，并在回话里告诉主智能体已达上限。

    回话是一段自然语言：定稿了哪些指标、新登记了哪些、忽略了哪些无效编号、问题是否已改写，
    以及（还在反问时）这是第几次反问、缺口是什么。

    `metrics` 里每条定义都由 MetricDefinition 校验：名称必填（不能是空白、不要写成问句），描述与别名可选。
    校验不过时这个工具不会被调用，你会直接收到一条参数错误——按报错把那一条改掉再调一次即可。

    Args:
        metric_ids: 口径与用户说法一致、这一轮要用的指标编号（取自 recall_metrics 的回话，按相关度从高到低给）；
            问题不需要指标、或一条都没对上时给空列表（那就是把候选清空）。
        metrics: 用户补充的、指标库里还没有的口径，每条含 name（必填）、description、alias；没有补充时给空列表。
        question: 改写后的用户问题——把问题里的指标名称替换成它的口径描述，其余说法（时间范围、筛选条件、
            分组维度）原样保留，整理成一句通顺的中文。没定下任何指标时给原问题即可。
        missing: 还没对上口径的指标说法（这一轮打算反问用户的那几条）；没有缺口时给空列表。
        runtime: 运行时自动注入（会话状态与运行上下文），调用时不用提供。
    """
    database_id = runtime.state["database_id"]
    context = runtime.context

    # 反问计数：还有口径没对上就累加一次；已到上限就直接拒绝执行（不落库、不改状态），交回不可继续；
    # 没有缺口说明这一轮口径确认完毕，计数清零，下一轮重新从零开始
    pending = [x for x in (missing or []) if x]
    asked_times = 0
    if pending:
        blocked = blocked_by_limit(runtime.state.get("retry_count") or 0, MAX_ASK_RETRY, "反问",
                                   runtime.tool_call_id)
        if blocked:
            return blocked
        asked_times = (runtime.state.get("retry_count") or 0) + 1
        logger.info(f"commit_metrics: 第 {asked_times}/{MAX_ASK_RETRY} 次反问，缺口 {pending}")

    # 已存在的指标按编号回表：编号是模型给的，编造的、以及别的库的编号在这里被挡掉
    selected = []
    unknown_ids = []
    if metric_ids:
        wanted = list(dict.fromkeys(metric_ids))
        rows = await Metric.filter(id__in=wanted, database_id=database_id).all()
        by_id = {row.id: row for row in rows}
        selected = [by_id[metric_id] for metric_id in wanted if metric_id in by_id]
        unknown_ids = [metric_id for metric_id in wanted if metric_id not in by_id]
        if unknown_ids:
            logger.warning(f"commit_metrics: ignore unknown metric id(s) {unknown_ids} in database {database_id}")
    logger.info(f"commit_metrics: {len(selected)} existing metric(s) selected in database {database_id}")

    # 新口径一次批量登记（每条都新登记、不查重：同名不同口径很常见，合并反而把口径搅在一起）。
    # 每条定义都已由 MetricDefinition 在调用前校验并规整（名称必填、去首尾空白，空描述变 None、
    # 空别名丢掉）；编号由 Metric.id 的 default 在构造时生成，故批量插入之后不必再回表取编号
    created = [
        Metric(name=definition.name, description=definition.description,
               alias=definition.alias, database_id=database_id)
        for definition in metrics
    ]
    if created:
        await Metric.bulk_create(created)
    # 向量库是派生数据：写入失败只影响下次召回，指标本身已经登记好了，重跑一次向量化即可补齐
    if created:
        try:
            await qdrant.add_metrics(database_id, created, context.embedding_model)
        except Exception as e:  # noqa: BLE001 - 派生数据失败不该挡住这一轮
            logger.warning(f"commit_metrics: 写入向量库失败（{len(created)} 条）：{e}")
        logger.info(f"commit_metrics: created {len(created)} metric(s) in database {database_id}")

    metric_infos = [
        MetricInfo(id=metric.id, name=metric.name or "", description=metric.description or "",
                   alias=metric.alias or [])
        for metric in [*selected, *created]
    ]
    logger.info(f"commit_metrics: metric_infos overwritten with {len(metric_infos)} metric(s)")

    # 回话拼成自然语言：定稿了什么、新登记了什么、忽略了哪些编号、问题是否已改写、反问进度如何
    parts = []
    if metric_infos:
        parts.append(f"已定稿 {len(metric_infos)} 条指标：" + "、".join(metric["name"] for metric in metric_infos)
                     + "，已写回状态。")
    else:
        parts.append("本轮没有采用任何指标（候选已清空）。")
    if created:
        parts.append(f"新登记 {len(created)} 条口径：" + "、".join(metric.name or "" for metric in created) + "。")
    if unknown_ids:
        parts.append(f"已忽略 {len(unknown_ids)} 个无效指标编号（不在本库或不存在）："
                     + "、".join(str(x) for x in unknown_ids) + "。")
    # 改写后的问题一并落进状态：下游生成 SQL 看到的就是「带口径」的那一版（没给就保留原问题）
    rewritten = (question or "").strip()
    if rewritten:
        parts.append(f"问题已按口径改写并写回状态：「{rewritten}」。")
    else:
        parts.append("问题保持原样。")
    if pending:
        parts.append(f"还有 {len(pending)} 个口径没对上：" + "、".join(pending)
                     + f"——这是第 {asked_times}/{MAX_ASK_RETRY} 次反问，把缺口整理成问句交给主智能体。")
    else:
        parts.append("口径确认完毕，反问计数已清零。")
    content = "".join(parts)
    if rewritten:
        logger.info(f"commit_metrics: question rewritten -> {rewritten!r}")
    return Command(update={
        "messages": [ToolMessage(content=content, tool_call_id=runtime.tool_call_id)],
        "metric_infos": metric_infos,
        "retry_count": asked_times,
        **({"question": rewritten} if rewritten else {})
    })


# ── 表召回：分解语句并行召回 → 重排 → 追加候选表；定稿写关联关系与最终候选表 ──────────────────────


def merge_table_infos(previous: list[TableInfo] | None, current: list[TableInfo] | None) -> list[TableInfo]:
    """按表编号把两批表合并：current 在前，previous 里没被覆盖的接在后面。

    工具都是「追加」语义（见 recall_tables / commit_tables），而状态里的这两个字段命中 keep_latest
    （后写覆盖先写，见 agnets.schemas.keep_latest），所以追加要在工具里先读出来合并再整体写回。
    """
    merged: dict[str, TableInfo] = {}
    for table in [*(current or []), *(previous or [])]:
        merged.setdefault(table["id"], table)
    return list(merged.values())


def build_table_document(table: TableInfo) -> str:
    """把一张表拼成重排文本（按 prompts/table_rerank.md 约定的格式）：
    表名 + 描述 + 字段（字段语义补足表承载的业务含义）。"""
    return (f"表名：{table['name']}；描述：{table['description']}；字段："
            + "；".join(f"{column['name']}（别名：{'、'.join(column['alias'] or []) or '无'}）："
                       f"{column['description'] or '无描述'}"
                       for column in table["columns"] or []))


async def load_table_infos(table_ids: list[str]) -> list[TableInfo]:
    """按表编号取回整表：表本身 + 它名下的全部字段（含取值样本与已登记的可关联字段）。

    字段按编号排序；取值只取元数据库里登记的样本，向量库的同步取值在定稿时再合并（见 commit_tables）。
    可关联字段从 ColumnRelation 中间表一次取回关系行与对端字段（两条查询，不在循环里逐字段回表），
    拼成 ``<表名>.<字段名>``——与 ColumnInfo.related_columns 的格式一致，拼 SQL 时照着写 JOIN。
    """
    if not table_ids:
        return []
    tables = await Table.filter(id__in=list(table_ids)).all()
    columns = await Column.filter(table_id__in=list(table_ids)).order_by("id")

    related_of: dict[str, list[str]] = {}
    relations = await ColumnRelation.filter(source_id__in=[column.id for column in columns]) \
        .values_list("source_id", "target_id")
    if relations:
        name_of = {
            column.id: (column.table_id.split(".", 1)[-1], column.name)
            for column in await Column.filter(id__in={target_id for _, target_id in relations}) \
                .only("id", "table_id", "name")
        }
        for source_id, target_id in relations:
            target = name_of.get(target_id)
            # 对端字段已被删掉（中间表随外键级联清，正常走不到这里）：跳过，不往上下文里塞野数据
            if target is not None:
                related_of.setdefault(source_id, []).append(f"{target[0]}.{target[1]}")

    grouped: dict[str, list[ColumnInfo]] = {}
    for column in columns:
        grouped.setdefault(column.table_id, []).append(ColumnInfo(
            id=column.id,
            name=column.name or "",
            type=column.type or "",
            description=column.description or "",
            alias=column.alias or [],
            table_id=column.table_id,
            values=column.values or [],
            role=column.role,
            related_columns=sorted(related_of.get(column.id, []))
        ))
    return [
        # Table.role 是普通 CharField（取回的是 str），TableInfo.role 声明的是 TableRoleOption：
        # 顺手转成枚举——状态里不该出现「该是枚举却是字符串」的值（pydantic 序列化与 msgpack 都省心）
        TableInfo(id=table.id, name=table.name or "",
                  role=TableRoleOption(table.role) if table.role else None,
                  description=table.description or "", columns=grouped.get(table.id, []))
        for table in tables
    ]


@tool("recall_tables", parse_docstring=True)
async def recall_tables(queries: list[str], runtime: ToolRuntime[Context, State]) -> Command:
    """用一组自然语言语句并行召回数据表，按语句各自重排取 TopK 后把**新**表追加到候选表，并以自然语言交回。

    每条语句各查一次字段的向量集合，命中字段所属的表就是候选（口径与检索图的字段召回一致）；
    候选表的字段一次取全（含取值样本与已登记的可关联字段）。随后**对每条语句各自重排它命中的表**、
    再断崖截断取 TopK（与指标召回同一套阈值，每条语句至多 5 张）——语句是「有哪些部门」这类实体问句，
    它召回的表之间比相关度才有意义；拿用户问题比会偏向主实体，把维度表、明细表挤掉。

    多次召回是**追加**语义：这一轮召回到、但**已经在 state.table_infos 里的表不再重复交回**（它们本来就在
    候选里，再报一遍只会让模型重复筛）；同一批语句里重复的说法也只查一次。
    跨轮则只发上一轮没发过的说法——重发的语句召回的表早就在候选里，白花一次检索。

    回话是一段自然语言：新召回的表按语句顺序、语句内按相关度一行一张（**表编号**、表名、角色、描述，
    以及每个字段的名称、类型、别名、描述、取值样本与**已登记的可关联字段**），并说明有几张因已在候选里
    或被截断而未返回、候选现共几张。表名可能重名，定稿（commit_tables 的 table_ids 与 relations）用
    **表编号**指代表，别自己编。没有新表时回话会明说——换一种拆法（换个实体说法），别原样重发。

    Args:
        queries: 分解出来的检索语句，一句一个实体（如「有哪些固定资产申请？」「有哪些部门？」）；
            工具内部并行执行，别把多个实体塞进一句；补召回时只发缺的那几个实体。
        runtime: 运行时自动注入（会话状态与运行上下文），调用时不用提供。
    """
    database_id = runtime.state["database_id"]
    context = runtime.context
    known = runtime.state.get("table_infos") or []
    known_ids = {table["id"] for table in known}
    statements = list(dict.fromkeys(query.strip() for query in queries if query and query.strip()))

    recalled: list[TableInfo] = []
    table_ids: list[str] = []
    if statements:
        hits = await asyncio.gather(*(
            qdrant.find_columns(database_id, [statement], context.embedding_model)
            for statement in statements
        ))
        table_ids = list(dict.fromkeys(column.table_id for columns in hits for column in columns))
        tables_of_id = {table["id"]: table for table in await load_table_infos(table_ids)}

        # 逐条语句重排它命中的表：语句就是实体问句，它召回的表之间比相关度才有意义；
        # 每条语句内先按表编号去重（一条语句命中同一张表的多个字段很常见），再断崖截断取 TopK
        statement_tables = [
            [tables_of_id[table_id] for table_id in dict.fromkeys(
                column.table_id for column in columns) if table_id in tables_of_id]
            for columns in hits
        ]
        rankings = await asyncio.gather(*(
            rerank_client.rerank(statement, [build_table_document(table) for table in tables],
                                 context.rerank_model, instruct=TABLE_RERANK_INSTRUCT)
            for statement, tables in zip(statements, statement_tables)
        ))
        seen: set[str] = set()
        for tables, results in zip(statement_tables, rankings):
            for result in decide_top_k(results):
                table = tables[result.index]
                if table["id"] not in seen:
                    seen.add(table["id"])
                    recalled.append(table)
        # 只要新表：已在候选里的表上一轮就交回给模型筛过了，重复返回不会带来新信息
        recalled = [table for table in recalled if table["id"] not in known_ids]
        logger.info(f"recall_tables: {len(statements)} statement(s) -> {len(table_ids)} hit, "
                    f"{len(recalled)} new table(s)")

    candidates = merge_table_infos(known, recalled)
    logger.info(f"recall_tables: candidates -> {len(candidates)} table(s): "
                + "、".join(f"{table['name']}（{table['id']}）" for table in candidates))

    # 回话拼成自然语言：一行一张新表（表编号、表名、角色、描述与字段清单），字段带取值样本
    if recalled:
        lines = [f"本轮新召回 {len(recalled)} 张表（按语句分组、语句内按相关度从高到低）："]
        for index, table in enumerate(recalled, start=1):
            role = f"，角色 {table['role']}" if table["role"] else ""
            lines.append(f"{index}. {table['name']}（编号 {table['id']}，{table['description'] or '无描述'}{role}）：")
            for column in table["columns"] or []:
                alias = "，别名 " + "、".join(column["alias"]) if column["alias"] else ""
                values = "，取值如 " + "、".join(str(value) for value in (column["values"] or [])[:5]) \
                    if column["values"] else ""
                related = "，可关联 " + "、".join(column["related_columns"] or []) \
                    if column["related_columns"] else ""
                lines.append(f"   - {column['name']}（{column['type'] or '类型未知'}{alias}）："
                             f"{column['description'] or '无描述'}{values}{related}")
        dropped = len(table_ids) - len(recalled)
        if dropped:
            lines.append(f"另有 {dropped} 张命中的表未返回（已在候选里，或重排时被截断）。")
        lines.append(f"候选表现共 {len(candidates)} 张，从这些表里筛出要用的，"
                     f"再按表编号用 commit_tables 定稿。")
        content = "\n".join(lines)
    else:
        content = (f"这批语句没有带来新表（命中的表都已在候选里，候选现共 {len(candidates)} 张）；"
                   f"换一种拆法（换个实体说法）再试，别原样重发。")
    return Command(update={
        "messages": [ToolMessage(content=content, tool_call_id=runtime.tool_call_id)],
        "table_infos": candidates
    })


@tool("commit_tables", parse_docstring=True)
async def commit_tables(table_ids: list[str],
                        relations: list[TableRelation],
                        runtime: ToolRuntime[Context, State]) -> Command:
    """把这一轮定下的数据表与它们之间的关联关系落库，并**追加**进最终候选表。

    做三件事：把关联关系写进 ColumnRelation 中间表（一条写正反两行，重复识别由 ignore_conflicts 跳过）；
    把这些关系写回表结构里的 `related_columns`（`<表名>.<字段名>`，下游照着拼 JOIN）；
    把表追加进 state.final_table_infos（按表编号去重，本轮定下的在前，此前定下的接在后面）。
    定稿前顺带把同步字段在向量库里的取值合并进字段（候选阶段只带元数据库的样本）。

    只认 `state.table_infos` 里出现过的表编号、且关系两端必须是这些表里真实存在的字段——
    不在候选里的表编号会被丢掉，并在回话里列出来（先 recall_tables 把它们召回来）。
    回话是一段自然语言：定稿了哪几张表、识别出几条关联（哪两个字段连起来）、最终候选共几张，
    以及忽略了哪些无效编号。

    Args:
        table_ids: 这一轮定下的表编号（取自 recall_tables 返回的 id），一张表一个。
        relations: 这些表之间的关联，每条含 source_table（表编号）/ source_column / target_table / target_column；
            没有关联给空列表。一条关系只写一次，工具会写正反两行。
        runtime: 运行时自动注入（会话状态与运行上下文），调用时不用提供。
    """
    database_id = runtime.state["database_id"]
    candidates = {table["id"]: table for table in (runtime.state.get("table_infos") or [])}
    wanted = list(dict.fromkeys(table_ids))
    selected = [candidates[table_id] for table_id in wanted if table_id in candidates]
    unknown_ids = [table_id for table_id in wanted if table_id not in candidates]
    if unknown_ids:
        logger.warning(f"commit_tables: ignore unknown table id(s) {unknown_ids}")
    if not selected:
        return Command(update={"messages": [ToolMessage(
            content="没有可用的表编号：这些表得先用 recall_tables 召回、出现在候选表里；请核对表编号后重试。",
            tool_call_id=runtime.tool_call_id)]})
    logger.info(f"commit_tables: {len(selected)} table(s) committed, unknown {unknown_ids}")

    # 关联关系：两端只认这批表里真实存在的字段，模型编出来的表或字段直接丢掉
    column_of: dict[tuple[str, str], str] = {}
    name_of: dict[str, tuple[str, str]] = {}
    for table in selected:
        for column in table["columns"] or []:
            if column["name"]:
                column_of[(table["id"], column["name"])] = column["id"]
                name_of[column["id"]] = (table["name"], column["name"])
    pairs: set[tuple[str, str]] = set()
    for relation in relations:
        source_id = column_of.get((relation.source_table, relation.source_column))
        target_id = column_of.get((relation.target_table, relation.target_column))
        if not source_id or not target_id or source_id == target_id:
            logger.warning(f"commit_tables: skip relation {relation}")
            continue
        # 关系是无向的：按字段编号排好序去重（同一条正反各给一次只算一条）
        pairs.add((source_id, target_id) if source_id < target_id else (target_id, source_id))
    if pairs:
        rows = [ColumnRelation(source_id=source_id, target_id=target_id) for source_id, target_id in pairs]
        rows += [ColumnRelation(source_id=target_id, target_id=source_id) for source_id, target_id in pairs]
        await ColumnRelation.bulk_create(rows, ignore_conflicts=True)
        logger.info(f"commit_tables: {len(pairs)} relation(s) saved, {len(rows)} row(s) written")

    # 关系写进表结构本身：related_columns 是 `<表名>.<字段名>`，与读库时拼的格式一致
    related_of: dict[str, list[str]] = {}
    for source_id, target_id in pairs:
        source_name, source_column = name_of[source_id]
        target_name, target_column = name_of[target_id]
        related_of.setdefault(source_id, []).append(f"{target_name}.{target_column}")
        related_of.setdefault(target_id, []).append(f"{source_name}.{source_column}")

    # 取值补全：候选表只带了元数据库的样本，这里把同步字段在向量库里的取值并进来（两次查询，不在循环里）
    sync_columns = await Column.filter(table_id__in=[table["id"] for table in selected],
                                       synchronized=True).only("id")
    sync_values = await qdrant.find_values(database_id, [column.id for column in sync_columns])
    extra = {value.column_id: list(value.payload) for value in sync_values}
    enriched = [
        TableInfo(
            id=table["id"], name=table["name"], role=table["role"], description=table["description"],
            columns=[
                ColumnInfo(
                    id=column["id"], name=column["name"], type=column["type"],
                    description=column["description"], alias=column["alias"], table_id=column["table_id"],
                    role=column.get("role"),
                    # 取值都是标量（枚举 / 数字 / 日期），dict.fromkeys 去重且保序：样本在前、同步取值在后
                    values=list(dict.fromkeys([*(column.get("values") or []), *extra.get(column["id"], [])])),
                    related_columns=sorted({*(column.get("related_columns") or []),
                                            *related_of.get(column["id"], [])})
                )
                for column in table["columns"] or []
            ]
        )
        for table in selected
    ]
    final_table_infos = merge_table_infos(runtime.state.get("final_table_infos") or [], enriched)
    logger.info(f"commit_tables: final_table_infos -> {len(final_table_infos)} table(s)")

    # 回话拼成自然语言：定稿的表、识别出的关联、无效编号与候选总数
    parts = [f"已定稿 {len(enriched)} 张表："
             + "、".join(f"{table['name']}（编号 {table['id']}，{table['description'] or '无描述'}）"
                        for table in enriched) + "。"]
    if pairs:
        lines = []
        for source_id, target_id in pairs:
            source_name, source_column = name_of[source_id]
            target_name, target_column = name_of[target_id]
            lines.append(f"{source_name}.{source_column} ↔ {target_name}.{target_column}")
        parts.append(f"识别出 {len(pairs)} 条关联关系：" + "；".join(lines)
                     + "，已写进两端字段的「可关联」信息，拼 JOIN 时照着连。")
    else:
        parts.append("没有识别出关联关系（这些表之间没有给出一对真实存在的字段连接）。")
    if unknown_ids:
        parts.append(f"已忽略 {len(unknown_ids)} 个无效表编号（不在候选里）："
                     + "、".join(unknown_ids) + "。")
    parts.append(f"最终候选共 {len(final_table_infos)} 张表。")
    content = "".join(parts)
    return Command(update={
        "messages": [ToolMessage(content=content, tool_call_id=runtime.tool_call_id)],
        "final_table_infos": final_table_infos
    })


# ── SQL：读上下文 → 校验（只读守卫 + EXPLAIN） → 执行（结果写回 state.data） ─────────────────────


def is_read_only_query(sql: str) -> bool:
    """判断是不是一条只读查询：须以 SELECT / WITH 开头、只有一条语句、不含数据或结构变更关键字。

    这是执行前的硬闸，与模型的自查互补——模型的判断会漏，这里至少把 DML / DDL 与多语句挡在 EXPLAIN 之前
    （EXPLAIN 对 DELETE / UPDATE 同样能通过，只靠它挡不住篡改）。判断前先剔掉注释与引号里的内容，免得
    字符串字面量里的关键字被误判；``delete_flag`` 这类标识符也不会命中——关键字按词边界匹配，下划线不算边界。
    """
    # 引号里的内容替换成空串、注释替换成空格，剩下的才是真正会被执行的语句骨架
    statement = SQL_QUOTED.sub("''", SQL_COMMENT.sub(" ", sql)).strip()
    if not re.match(r"(?is)^(select|with)\b", statement):
        return False
    # 只允许一条语句：去掉结尾的分号后，中间不该再有分号
    if ";" in statement.rstrip().rstrip(";"):
        return False
    return SQL_FORBIDDEN.search(statement) is None


@tool("read_sql_context", parse_docstring=True)
async def read_sql_context(runtime: ToolRuntime[Context, State]) -> str:
    """读取生成 SQL 要用的上下文：用户问题、候选表与指标口径，拼成一段自然语言交回。

    表取自 state.final_table_infos（表召回那条线定下的），带各自的字段——字段的类型、描述、别名、
    取值样本与可关联字段都在里面：取值样本决定 WHERE 里的字面量怎么写，可关联字段决定 JOIN 怎么拼，
    都别自己编。回话里每张表一段：表编号、表名、角色与描述打头，字段一行一个（名称、类型、别名、描述、
    取值样本、能与哪个表的哪个字段关联）。

    还没有候选表时回话会明说——先让表召回那条线把表定下来，没有表就写不出 SQL。

    Args:
        runtime: 运行时自动注入（会话状态与运行上下文），调用时不用提供。
    """
    state = runtime.state
    tables = state.get("final_table_infos") or []
    metrics = state.get("metric_infos") or []

    # 回话拼成自然语言：问题打头，然后是口径与表结构（字段带类型、别名、取值样本与可关联信息）
    parts = [f"用户问题：{state['question']}。"]
    if metrics:
        lines = [f"这一轮的指标口径共 {len(metrics)} 条："]
        for metric in metrics:
            alias = "；别名：" + "、".join(metric["alias"]) if metric["alias"] else ""
            lines.append(f"- {metric['name']}：{metric['description']}{alias}")
        parts.append("\n".join(lines))
    else:
        parts.append("这一轮没有选定指标口径。")
    if tables:
        lines = [f"可用的候选表共 {len(tables)} 张："]
        for table in tables:
            role = f"，角色 {table['role']}" if table["role"] else ""
            lines.append(f"- {table['name']}（编号 {table['id']}，{table['description'] or '无描述'}{role}）：")
            for column in table["columns"] or []:
                alias = "，别名 " + "、".join(column["alias"]) if column["alias"] else ""
                values = "，取值如 " + "、".join(str(value) for value in (column["values"] or [])[:5]) \
                    if column["values"] else ""
                related = "，可关联 " + "、".join(column["related_columns"] or []) \
                    if column["related_columns"] else ""
                lines.append(f"    {column['name']}（{column['type'] or '类型未知'}{alias}）："
                             f"{column['description'] or '无描述'}{values}{related}")
        parts.append("\n".join(lines))
    else:
        parts.append("还没有候选表：先让表召回那条线把表定下来，没有表就写不出 SQL。")
    logger.info(f"read_sql_context: {len(tables)} table(s)")
    return "\n".join(parts)


@tool("check_sql", parse_docstring=True)
async def check_sql(sql: str, runtime: ToolRuntime[Context, State]) -> Command:
    """校验一条只读 SQL：先挡数据变更与多语句，再用 EXPLAIN 校验语法与库结构（不真正执行）。

    通过时把这条 SQL 写进状态（state.sql），之后 execute_sql 执行的就是它；不通过则清空 state.sql，
    并把纠正次数 +1、报错原样交回。次数到顶（5 次）后**本工具不再执行**，回话会明说已达上限——
    这时不要再改，直接在回话里说明卡在哪。

    回话是一句自然语言：通过就说可以执行；不通过则带上这是第几次纠正、报错原文，
    并提示按报错改完再校验一次。

    Args:
        sql: 要校验的 SQL，整条语句，不要带 Markdown 代码块或解释文字。
        runtime: 运行时自动注入（会话状态与运行上下文），调用时不用提供。
    """
    blocked = blocked_by_limit(runtime.state.get("retry_count") or 0, MAX_SQL_RETRY, "SQL 纠正",
                               runtime.tool_call_id)
    if blocked:
        return blocked
    database_id = runtime.state["database_id"]
    statement = sql.strip()
    if is_read_only_query(statement):
        valid, detail = await explain_sql(statement, database_id)
        error = None if valid else (detail or "EXPLAIN 校验未通过")
    else:
        error = "只允许单条只读查询（SELECT / WITH ... SELECT）；禁止写入、更新、删除、改结构，也禁止多条语句。"

    # 到顶就停在 MAX_SQL_RETRY，不再往上涨：次数只用来提示「已经改了几轮」，不记流水账
    retried = 0 if error is None else min((runtime.state.get("retry_count") or 0) + 1, MAX_SQL_RETRY)
    retry_limit_reached = error is not None and retried >= MAX_SQL_RETRY
    # 回话拼成自然语言：通过就放行；不通过带次数与报错原文，到顶时明说别再改
    if error is None:
        content = "校验通过：这条 SQL 已写入状态，调 execute_sql 即可执行。"
    else:
        content = f"校验未通过（第 {retried}/{MAX_SQL_RETRY} 次纠正）：{error}——按报错修改后再校验一次。"
        if retry_limit_reached:
            content += f" 已到 {MAX_SQL_RETRY} 次纠正上限：不要再改，直接在回话里说明卡在哪一步、报错原文是什么。"
    logger.info(f"check_sql: valid={error is None}, retried={retried}, error={error}")
    return Command(update={
        "messages": [ToolMessage(content=content, tool_call_id=runtime.tool_call_id)],
        # 通过才落 state.sql：没通过校验的语句不许被执行（execute_sql 只认这一条）
        "sql": statement if error is None else None,
        "error": error,
        "retry_count": retried
    })


@tool("execute_sql", parse_docstring=True)
async def execute_sql(runtime: ToolRuntime[Context, State]) -> Command:
    """执行已通过 check_sql 的那条 SQL，把结果集整份写回 state.data，只回行数与列名给模型。

    只执行 state.sql——校验通过时 check_sql 才写它，所以没校验过就没有 SQL 可执行（回话会直接提示先校验）。
    结果集整份写进 state.data（下游取数就看它），**不回给模型**：回话只有行数与列名。
    执行报错按与 check_sql 相同的口径计数（上限 5 次）：改完再走一次 check_sql——
    EXPLAIN 只能查出语法与库结构问题，除零、类型转换、行锁这些要到执行时才暴露。
    次数到顶（5 次）后**本工具不再执行**，回话会明说已达上限。

    回话是一句自然语言：成功就说返回多少行、有哪些列；失败则带上这是第几次纠正与报错原文。

    Args:
        runtime: 运行时自动注入（会话状态与运行上下文），调用时不用提供。
    """
    blocked = blocked_by_limit(runtime.state.get("retry_count") or 0, MAX_SQL_RETRY, "SQL 纠正",
                               runtime.tool_call_id)
    if blocked:
        return blocked
    database_id = runtime.state["database_id"]
    sql = runtime.state.get("sql")
    if not sql:
        return Command(update={"messages": [ToolMessage(
            content="状态里没有通过校验的 SQL：先调 check_sql 把 SQL 校验通过，再来执行。",
            tool_call_id=runtime.tool_call_id)]})

    try:
        rows = await execute_raw_sql(sql, database_id)
    except Exception as e:  # noqa: BLE001 - 数据库报错原样交回给模型改
        retried = min((runtime.state.get("retry_count") or 0) + 1, MAX_SQL_RETRY)
        retry_limit_reached = retried >= MAX_SQL_RETRY
        logger.error(f"execute_sql: {e}")
        content = (f"执行失败（第 {retried}/{MAX_SQL_RETRY} 次纠正）：{e}"
                   f"——按报错修改 SQL 后再走一次 check_sql。")
        if retry_limit_reached:
            content += f" 已到 {MAX_SQL_RETRY} 次纠正上限：不要再改，直接在回话里说明报错原文。"
        return Command(update={
            "messages": [ToolMessage(content=content, tool_call_id=runtime.tool_call_id)],
            "sql": None,
            "error": str(e),
            "retry_count": retried
        })

    columns = list(rows[0].keys()) if rows else []
    logger.info(f"execute_sql: {len(rows)} row(s) -> state.data")
    content = f"执行完成：返回 {len(rows)} 行，列：{'、'.join(columns)}。"
    return Command(update={
        "messages": [ToolMessage(content=content, tool_call_id=runtime.tool_call_id)],
        "data": rows,
        "error": None,
        "retry_count": 0
    })
