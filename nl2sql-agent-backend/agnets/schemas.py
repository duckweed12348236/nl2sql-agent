"""智能体链路上共用的数据结构：检索侧的状态与上下文、采集与评估用的进度帧与模型输出。

按用途分节：

- 通用：``keep_latest``（同名通道后写覆盖先写的 reducer）；
- 检索（agnets.tools / agnets.subagents）：``State`` 会话状态、``Context`` 运行上下文、
  ``TableInfo`` / ``ColumnInfo`` / ``MetricInfo`` 在状态与模型之间传递的表 / 字段 / 指标结构，
  以及工具的复杂入参 ``MetricDefinition`` / ``TableRelation``；
- 进度帧：``Step`` / ``StepStatus``（采集与评估共用，见 agnets.collection / agnets.evaluation）；
- 采集：``DiscoveredColumn`` / ``DiscoveredTable`` / ``DiscoveredTableList``（大模型对表与列的分析结果）；
- 评估：``EvalTableInfo`` / ``EvalColumnInfo``（出题与判定用的库上下文快照）、``DatasetItemInfo``，
  以及 ``StringList`` / ``SqlList`` / ``EvaluationReview`` 这几个模型输出校验器。

命名上有一处要注意：**``TableInfo`` / ``ColumnInfo`` 是检索侧 TypedDict**（走状态、按 ``info["name"]``
取值）；评估侧同形的快照叫 ``EvalTableInfo`` / ``EvalColumnInfo``（dataclass，``asdict`` 进提示词、
落库时再转字典）。两者字段相近但语义不同——前者是「检索到的」，后者是「出题 / 判定所用的」。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Annotated, Any, NotRequired, TypedDict

from deepagents import DeepAgentState
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field, RootModel, field_validator, model_validator

from agnets.models import JSONOutputValidator, RerankModel
from models.meta import ColumnRoleOption, TableRoleOption
from schemas import Schema


# ── 通用：同名通道的合并规则 ──────────────────────────────────────────────────────────────────

def keep_latest(left: Any, right: Any) -> Any:
    """同名通道被写多次时取最后一次写入（用作 Annotated 的 reducer）。

    LangGraph 的默认通道只允许「一步写一次」：并行分支、回退重跑、中断恢复都可能让同一个字段在同一步
    被写两次，撞上就是 InvalidUpdateError（INVALID_CONCURRENT_GRAPH_UPDATE）。这里声明「后写覆盖先写」，
    只用在「各分支算出来的是同一件事的最新结果」的字段上（见 State.table_infos / final_table_infos /
    metric_infos）。
    """
    return right


# ── 检索：运行上下文与会话状态 ────────────────────────────────────────────────────────────────

@dataclass()
class Context:
    """图运行上下文：由 StateGraph(context_schema=Context) / create_deep_agent(context_schema=Context)
    注入为 runtime.context。

    上下文是 dataclass 而不是 TypedDict：它只承载运行期的依赖，不参与状态合并，也不需要序列化进
    checkpoint；用对象可以按属性取用——节点与工具写 ``runtime.context.model`` 取 LLM、
    ``runtime.context.embedding_model`` 取嵌入模型、``runtime.context.rerank_model`` 取重排模型
    （重排客户端本身不绑定模型，每次调用传入它）。LangGraph 认 dataclass 与 TypedDict 两种
    ``context_schema``，这里取了更好读的那种。

    检索哪个库、哪种模式属于这一轮的输入、随状态走（见 State.database_id），不放上下文里——
    上下文只装那些与状态无关、每轮都一样的依赖。
    """
    model: BaseChatModel
    embedding_model: Embeddings
    rerank_model: RerankModel


class State(DeepAgentState):
    """智能体的状态：deepagents 的 DeepAgentState 加上本项目的业务字段。

    继承 DeepAgentState 而不是自己另起一份 TypedDict：它把 messages 通道换成了 DeltaChannel，
    消息按增量归并（见 deepagents 的 _messages_delta_reducer），否则每一轮都把整段对话再存一遍，
    checkpoint 的体积随轮次平方增长；技能、文件工具、子智能体这些中间件也按它的字段存取。
    因此本类必须是 TypedDict（DeepAgentState 本身就是 TypedDict，dataclass 继承不了它），
    字段也不能带默认值：messages / question / database_id 是必填项，其余一律标 NotRequired。

    字段就是这条链路上真正会写、会读的那几个：

    - question / database_id：这一轮的必填输入。工具直接按下标取用、不做非空检查（见 agnets.tools）；
      question 会被 commit_metrics 改写——指标名称换成口径描述后覆盖写回，后续生成 SQL 用的就是它；
    - metric_infos：定稿后的指标口径（commit_metrics 整份覆盖）；
    - table_infos：本轮召回出来的候选表，只给重排用；final_table_infos：跨轮累积、后续都读的那份；
    - sql：通过 check_sql 校验的那条语句（execute_sql 只认它，没通过校验时为空）；
    - data：execute_sql 取回的结果集，下游取数就看它；
    - error：最后一次失败的原因（校验 / 执行 / 子智能体报上来的），成功时清空；
    - retry_count：纠错与反问共用的重试计数——指标那条线记已反问过用户几次（上限
      MAX_ASK_RETRY），SQL 那条线记语法 / 执行错误已纠正几次（上限 MAX_SQL_RETRY），两边都在自己成功时清零。

    table_infos / final_table_infos / metric_infos 带 keep_latest reducer：这几份都是「算到现在的最新结果」，
    同一步可能被写两次（子智能体与父图各写一次），默认通道遇到同一步两次写入会直接抛 InvalidUpdateError。
    """
    question: str
    database_id: int
    error: NotRequired[str | None]
    metric_infos: NotRequired[Annotated[list[MetricInfo] | None, keep_latest]]
    table_infos: NotRequired[Annotated[list[TableInfo] | None, keep_latest]]
    final_table_infos: NotRequired[Annotated[list[TableInfo] | None, keep_latest]]
    sql: NotRequired[str | None]
    data: NotRequired[list[dict[str, Any]] | None]
    retry_count: NotRequired[int]


# ── 检索：表 / 字段 / 指标结构 ────────────────────────────────────────────────────────────────

class ColumnInfo(TypedDict):
    """检索阶段的字段信息（状态里流转的那种）。

    values：该列在数据库中的取值（元数据库里登记的样本 + 同步字段从向量库 values 集合召回的取值，
    合并去重）；该列没采过取值时为空列表或 None。

    related_columns：该字段可用于关联查询的对端字段，命名格式 `<表名>.<字段名>`；没有识别过时为空列表或 None。
    """
    id: str
    name: str
    type: str
    description: str
    alias: list[str]
    table_id: str
    values: NotRequired[list[Any] | None]
    role: NotRequired[ColumnRoleOption | None]
    related_columns: NotRequired[list[str] | None]


class TableInfo(TypedDict):
    """检索到的表：表编号、表名、角色、描述与它名下的字段（columns 未召回字段时不带该键或为 None）。

    role 允许为空：元数据库里 Table.role 是可空列，没分析出角色的表就留空；有值时一律是
    TableRoleOption 枚举（见 agnets.tools.load_table_infos 的转换），不要塞普通字符串。
    """
    id: str
    name: str
    role: TableRoleOption | None
    description: str
    columns: NotRequired[list[ColumnInfo] | None]


def dump_table(table: TableInfo) -> dict[str, Any]:
    """转成提示词里的表格结构（各提示词的「输入格式」以它为准）。

    字段上不带 `table_id`：所属表已由外层的表给出，字段里再写一遍只是噪声，提示词的 Schema 里也不列它。
    TypedDict 挂不了方法，故这里是模块级函数而不是 ``TableInfo.dump()``。返回的是深拷贝，
    调用方改它不会动到状态里的原表。
    """
    payload = deepcopy(dict(table))
    for column in payload.get("columns") or []:
        column.pop("table_id", None)
    return payload


class MetricInfo(TypedDict):
    """指标信息：id 取自元数据库的 Metric.id，即雪花 ID（整数）。

    id 是库里那一条的凭据：名称可能重名，定稿时按 id 指定「这一轮用哪条」（见 agnets.tools.commit_metrics）。
    只把口径交给模型判断时用 :func:`dump_metric` 去掉它。
    """
    id: int
    name: str
    description: str
    alias: list[str]


def dump_metric(metric: MetricInfo) -> dict[str, Any]:
    """转成提示词里的指标结构：去掉 id，模型只按名称 / 描述 / 别名判断口径。

    TypedDict 挂不了方法，故这里是模块级函数而不是 ``MetricInfo.dump()``。
    """
    payload = dict(metric)
    payload.pop("id", None)
    return payload


class MetricDefinition(BaseModel):
    """待登记的指标口径：一条定义对应指标库里即将新增的一行。

    用户补充口径的回答经整理后就是这个形态（见 skills/metric-caliber-review），作为
    :func:`agnets.tools.commit_metrics` 的 ``metrics`` 数组传进来。

    是 Pydantic 模型而不是 TypedDict：它是工具的复杂入参，由 Pydantic 在调用前把整份参数校验完——
    名称缺失或全是空白、字段类型不对，都会在工具执行之前被驳回并告诉模型错在哪，可以重填，
    不会写出半条脏数据。校验时顺手规整：名称 / 描述 / 别名去首尾空白，空描述变 None，空别名叫丢掉。
    编号不在入参里：它由入库时生成。
    """
    name: str = Field(min_length=1, description="指标名称（用户对这个指标的称呼，不要写成问句）")
    description: str | None = Field(
        default=None,
        description="规范口径一句话：统计对象 + 计算方式 + 过滤条件 + 时间口径；用户没说的不要补")
    alias: list[str] = Field(default_factory=list, description="指标的其他说法（用户可能怎么叫它），可空")

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("指标名称不能为空（全是空白也不行）")
        return name

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: str | None) -> str | None:
        return (value or "").strip() or None

    @field_validator("alias")
    @classmethod
    def _clean_alias(cls, value: list[str]) -> list[str]:
        return [x.strip() for x in value if x and x.strip()]


class TableRelation(BaseModel):
    """候选表之间的一条关联：源表的某字段连到目标表的某字段，两者可做等值 JOIN。

    表用表编号（Table.id，recall_tables 返回的那个 id）、字段用字段名——与模型手上的表结构一致。
    是 Pydantic 模型：作为 :func:`agnets.tools.commit_tables` 的 ``relations`` 入参，调用前校验空白与
    缺字段；落库时再按「这批表里真实存在的字段」对一遍，编出来的表 / 字段直接丢掉。
    一条关系只写一次即可（工具会写正反两行），字段名大小写照抄表结构。
    """
    source_table: str = Field(min_length=1, description="源表的表编号（recall_tables 返回的 id）")
    source_column: str = Field(min_length=1, description="源表上的字段名")
    target_table: str = Field(min_length=1, description="目标表的表编号")
    target_column: str = Field(min_length=1, description="目标表上的字段名")

    @field_validator("source_table", "source_column", "target_table", "target_column")
    @classmethod
    def _clean(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("表编号与字段名都不能为空")
        return text


# ── 进度帧：采集与评估共用 ────────────────────────────────────────────────────────────────────

class StepStatus(StrEnum):
    """进度状态。

    RUNNING / SUCCESS 表示某一步开始与完成；FAILED 表示某一项失败但流程照常往下走
    （调用方据此继续记日志 / 继续下一批）。
    """
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    FAILED = "failed"


@dataclass()
class Step:
    """一条进度：逐条推给调用方的流式消息（采集与评估共用）。

    message：该节点正在做的工作，如「开始采集表结构」；
    status：见 StepStatus，缺省为 RUNNING；
    error：失败原因（status 为 failed 时携带），如某张表采集失败的理由；
    dataset_id：评估侧落库的数据集编号（Dataset.id）：出题从数据集落库那一帧起带上，
    评测每帧都带，调用方据此把进度与数据集对上。
    """
    message: str
    status: StepStatus = StepStatus.RUNNING
    error: str | None = None
    dataset_id: int | None = None


# ── 模型输出清洗：位置数组兼容 ────────────────────────────────────────────────────────────────

def strip_fence(text: str) -> str:
    """去掉模型偶尔附加的 Markdown 代码块围栏（```json ... ```）。

    只负责「文本 → 文本」的清洗，解析与校验交给各模型自己的
    validate_output：LLM 输出统一按 JSON 文本校验。
    """
    content = text.strip()
    if content.startswith("```"):
        content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return content


def _pad(values: Any, size: int) -> list[Any]:
    """把位置数组补齐到固定长度（缺少的下标补 None），采集侧解析模型输出的位置数组用。"""
    values = list(values) if isinstance(values, (list, tuple)) else [values]
    return (values + [None] * size)[:size]


def _pick(source: dict[str, Any], *names: str) -> Any:
    """按候选字段名取值，用于兼容模型偶发的「对象」输出（采集侧）。"""
    for name in names:
        if name in source:
            return source[name]
    return None


# ── 采集：大模型对表 / 列的分析结果 ───────────────────────────────────────────────────────────

class DiscoveredColumn(Schema):
    """大模型对单列的分析结果。

    正常输出是位置数组 [列名, 角色, 描述, 别名, 同步标记]；模型偶尔会改用字段名表示
    （如 {"name": ..., "role": ...}），这里一并兼容。
    """
    name: str
    role: ColumnRoleOption
    description: str = ""
    alias: list[str] = []
    synchronized: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            name, role, description, alias, synchronized = _pad(value, 5)
            return {"name": name, "role": role, "description": description or "",
                    "alias": alias or [], "synchronized": bool(synchronized)}
        if isinstance(value, dict):
            return {"name": _pick(value, "name", "column_name", "column", "field"),
                    "role": _pick(value, "role", "column_role"),
                    "description": _pick(value, "description", "comment", "column_comment") or "",
                    "alias": _pick(value, "alias", "aliases") or [],
                    "synchronized": bool(_pick(value, "synchronized", "is_enum", "enum"))}
        return value


class DiscoveredTable(Schema):
    """大模型对单表的分析结果。

    正常输出是位置数组 [表名, 角色, 表描述, 列数组]；同样兼容字段名（对象）表示。
    """
    name: str
    role: TableRoleOption
    description: str = ""
    columns: list[DiscoveredColumn] = []

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            name, role, description, columns = _pad(value, 4)
            return {"name": name, "role": role,
                    "description": description or "", "columns": columns or []}
        if isinstance(value, dict):
            columns = _pick(value, "columns", "column_infos", "fields")
            if isinstance(columns, dict):
                columns = list(columns.values())
            return {"name": _pick(value, "name", "table_name", "table"),
                    "role": _pick(value, "role", "table_role"),
                    "description": _pick(value, "description", "comment", "table_comment") or "",
                    "columns": columns or []}
        return value


class DiscoveredTableList(RootModel[list[DiscoveredTable]]):
    """大模型输出的表分析结果：根类型即 list[DiscoveredTable]，校验的是「裸列表」本身。

    - model_validate([element])：收原始列表，用于整体解析失败时逐个元素抢救（见
      agnets.collection._salvage_tables）；
    - model_validate_json(content)：收模型返回的 JSON 文本，用于整体解析（见
      agnets.collection.analyze），解析失败抛 ValidationError，调用方据此区分截断与格式错误。
    """


# ── 评估：出题 / 判定用的库上下文快照 ────────────────────────────────────────────────────────

@dataclass()
class EvalColumnInfo:
    """评估侧的字段快照（出题上下文、判定上下文与落库片段都用它）。

    values：该列在数据库中的取值（元数据库里登记的样本 + 同步字段从向量库 values 集合召回的取值，
    合并去重）；该列没采过取值时为空列表。

    related_columns：该字段可用于关联查询的对端字段，命名格式 `<表名>.<字段名>`；没有识别过时为空列表。

    与检索侧的 ``ColumnInfo``（TypedDict）字段相近但用途不同：这里会 ``asdict`` 进提示词、随数据集落库。
    """
    id: str
    name: str
    type: str
    description: str
    alias: list[str]
    table_id: str
    values: list[Any] | None = None
    role: ColumnRoleOption | None = None
    related_columns: list[str] | None = None


@dataclass()
class EvalTableInfo:
    """评估侧的表快照：出题所用的库上下文（见 agnets.evaluation.build_table_infos）。"""
    id: str
    name: str
    role: TableRoleOption
    description: str
    columns: list[EvalColumnInfo] | None = None

    def dump(self) -> dict[str, Any]:
        """转成提示词里的表格结构。

        字段上不带 `table_id`：所属表已由外层的表给出，字段里再写一遍只是噪声。
        """
        payload = asdict(self)
        for column in payload["columns"] or []:
            column.pop("table_id", None)
        return payload


# ── 评估：模型输出校验器 ──────────────────────────────────────────────────────────────────────

class StringList(JSONOutputValidator, RootModel[list[str]]):
    """LLM 输出的字符串数组：根类型即 list[str]，校验的是「裸列表」本身（出题用）。

    校验模型输出时用 validate_output，例如：
    StringList.validate_output('["t_order", "t_user"]')。
    """

    @classmethod
    def validate_output(cls, text: str) -> list[str]:
        """清洗代码块围栏后校验模型输出，并直接返回根列表。"""
        return cls.model_validate_json(strip_fence(text)).root


class SqlRecord(BaseModel):
    """单条 SQL 结果：问题标识 key（整数）与对应的 SQL。"""

    key: int
    sql: str


class SqlList(JSONOutputValidator, RootModel[list[SqlRecord]]):
    """LLM 输出的 SQL 列表：每项携带问题标识 key（整数）与对应 SQL。

    校验模型输出时用 validate_output，例如：
    SqlList.validate_output('[{"key": 1, "sql": "SELECT 1"}]')。
    """

    @classmethod
    def validate_output(cls, text: str) -> list[SqlRecord]:
        """清洗代码块围栏后校验模型输出，并直接返回根列表。"""
        return cls.model_validate_json(strip_fence(text)).root


class EvaluationReview(JSONOutputValidator):
    """LLM 输出的一轮评测解读报告（落进 Result.report）：整体结论、逐条归因、共性问题与改进建议。

    报告是 **纯文本**（Markdown 正文），模型直接输出正文、不做 JSON 包装，因此这里只把文本清洗一下，
    没有可校验失败的结构——配合 invoke 用时，重试只会发生在调用本身出错（限流、超时）的场合。
    """

    @classmethod
    def validate_output(cls, text: str) -> str:
        """返回报告正文：去掉首尾空白，以及模型偶尔自作主张加的代码块围栏。"""
        return strip_fence(text)


@dataclass()
class DatasetItemInfo:
    """评估数据集的一条明细：问题与对应的参考 SQL（落库前后都用它传递）。"""
    question: str
    sql: str


# 评测的逐条结果用元组传递（生成的 SQL、等效性得分、得分理由、检索到的表信息，见
# agnets.evaluation.evaluate_item）：它只服务于落库那一刻，不再单独建模，也不进会话状态。
