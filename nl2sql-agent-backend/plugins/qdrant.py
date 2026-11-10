from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from langchain_core.embeddings import Embeddings
from loguru import logger
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import VectorParams, Distance, QueryRequest, Filter, FilterSelector, FieldCondition, \
    MatchAny, PointStruct, PayloadSchemaType

from models import VALUE_LIMIT, get_column_values, is_value_type
from models.meta import Column, Database, Metric, Table
from plugins import Plugin
from config import QDRANT_URL

# 单次写入的字段数：1024 维向量约占 4KB/条，过大（如上万条）会让单次 upsert 请求体积失控
INSERT_BATCH_SIZE = 200


@dataclass()
class Value:
    """一个字段的取值集合：payload 装该字段已登记的全部取值范围（采集上限见 models.VALUE_LIMIT），
    column_id 指出属于哪个字段。

    Qdrant 的 payload 原生支持数组，取值的原类型（整数 / 字符串）直接存，出库即原样，不必再记一份类型。
    """

    payload: list[Any]
    column_id: str

# 参与向量化的字段：与表名、表描述拼成一段文本后共同决定那个向量（见 build_column_text），它们一变就得重算。
# 表名与表描述属于 Table、不在此列：改表名等于换一张表，改表描述也不会经过 update_columns——
# 后者要重算字段向量得重新跑一遍字段向量化（见采集的 embed_columns）
VECTORIZED_FIELDS = {"name", "description", "alias"}

# 检索侧给关键词加的指令：当前嵌入模型是 bge-large-zh-v1.5（见 plugins.agents.models），
# 这类 BGE 模型检索时要按「为查询加前缀」的口径调用——指令只加在查询一侧，入库的文本不加
# （见 build_column_text / build_metric_text），检索向量才落在文档向量该被检索到的位置。
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def build_column_payload(column: Column) -> dict[str, Any]:
    """字段点的载荷：只记字段编号，字段内容不落向量库（见 find_columns）。

    字段名 / 描述 / 别名等本来就在元数据库里，再存一份就得在每次改动时同步两边；这里只留编号，
    向量库只回答「哪些字段相关」，字段内容一律回元数据库取。键名与 register 建的 id 索引一致，
    按编号删点（delete_columns）走的就是这个索引。
    """
    return {"id": column.id}


def build_column_text(column: Column, table_name: str, table_description: str | None = None) -> str:
    """字段的向量化文本：表名（连表描述）、字段名、别名与描述拼成一段自然语言（见 add_columns）。

    这几样讲的都是同一个字段，拼成一句再向量化，语义才是完整的：表说明它装什么、字段名与别名说的是
    同一个东西、描述再补一层业务口径，落在同一个向量里互相补足，一次检索就能覆盖各种说法；分成多个点
    则要为同一个字段堆出若干个向量，检索时还得再按字段编号并回来。空的内容不占位置，直接省掉。

    表描述由调用方从元数据库取（见 add_columns）：表名只说"叫什么"，加上描述才说得清"装什么"，
    "订单主表里跟钱有关的字段" 这类问法因此也能落到 amount 上。
    """
    # 表名后面接表描述；没有描述时补一个空格，保持「表 orders 的字段 amount」原有的读法
    label = f"表 {table_name}"
    label += f"（{table_description}）" if table_description else " "
    text = f"{label}的字段 {column.name}"
    if column.alias:
        text += f"（别名：{'、'.join(column.alias)}）"
    if column.description:
        text += f"：{column.description}"
    return text


def build_columns_collection(database_id: int) -> str:
    """字段集合的集合名：字段按业务库分开存，集合名以数据库编号为后缀（集合见 register）。"""
    return f"columns.{database_id}"


def build_values_collection(database_id: int) -> str:
    """取值集合的集合名：取值按业务库分开存，集合名以数据库编号为后缀（集合见 register）。"""
    return f"values.{database_id}"


def build_metrics_collection(database_id: int) -> str:
    """指标集合的集合名：指标与字段 / 取值一样按业务库分开存，集合名以数据库编号为后缀（集合见 register）。"""
    return f"metrics.{database_id}"


def build_metric_payload(metric: Metric) -> dict[str, Any]:
    """指标点的载荷：只记指标编号，指标内容不落向量库（见 find_metrics）。

    与字段点（build_column_payload）同一套结构：指标名 / 描述 / 别名只存元数据库一份，
    向量库只回答「哪些指标相关」。键名与 register 在各库的 metrics.<编号> 集合上建的 id 索引一致，
    按编号删点（delete_metrics）走的就是这个索引。
    """
    return {"id": metric.id}


def build_metric_text(metric: Metric) -> str:
    """指标的向量化文本：指标名、别名与描述拼成一段自然语言（见 add_metrics）。

    与 build_column_text 同一口径：一段文本一个向量，讲讲同一个指标的几个说法落在同一个向量里
    互相补足；空的别名、空的描述不占位置，文本里直接省掉。
    """
    text = f"指标 {metric.name}"
    if metric.alias:
        text += f"（别名：{'、'.join(metric.alias)}）"
    if metric.description:
        text += f"：{metric.description}"
    return text


def build_query_text(keyword: str) -> str:
    """检索侧的嵌入文本：关键词前面加上嵌入模型要求的查询指令（见 QUERY_INSTRUCTION）。

    写入侧（文档侧）的文本不加指令，检索侧加——这种不对称正是 BGE 这类模型的训练口径，
    两边都加或都不加都会让查询向量偏离文档向量该有的位置。
    """
    return f"{QUERY_INSTRUCTION}{keyword}"


class Qdrant(Plugin):
    """Qdrant 向量库的读写封装。

    文本向量化不由本类负责：写入与检索都要求调用方把嵌入模型传进来
    （add_columns / add_metrics / find_columns / find_metrics 的 embedding_model 参数），
    保证写库与检索用的是同一个模型。本类只负责拼要向量化的文本：入库是文档侧（build_column_text /
    build_metric_text），检索是查询侧、前面要加模型要求的指令（见 build_query_text）。
    """

    def __init__(self):
        self.client = AsyncQdrantClient(url=QDRANT_URL)

    async def find_columns(self, database_id: int, keywords: list[str], embedding_model: Embeddings) -> list[Column]:
        """按关键词检索字段：只在 database_id 指定的业务库的字段集合里检索（见 register），不跨库。

        字段点的载荷只有字段编号（见 build_column_payload），命中后按编号回元数据库取整行：
        元数据库是字段内容的事实来源，向量库只负责找出相关的字段编号。
        关键词按检索口径加上指令后再向量化（见 build_query_text）：入库的文本不带指令，两边不对称。
        """
        embeddings = await embedding_model.aembed_documents([build_query_text(keyword) for keyword in keywords])
        # 每个关键词在同一个（匿名）向量空间里各查一次（见 add_columns），命中结果按字段编号去重：
        # 一个字段只有一个点，去重是为了兜住重复采集残留的旧点，同一个字段不会在结果里出现两次
        requests = [QueryRequest(query=embedding, with_payload=True) for embedding in embeddings]
        responses = await self.client.query_batch_points(build_columns_collection(database_id), requests)
        column_ids = set(point.payload["id"] for res in responses for point in res.points if point.payload)
        if not column_ids:
            return []
        # 元数据库里已经不存在的编号（如清表时先删了点、登记又没删干净）取不回来，直接略过：
        # 这类字段本来也拼不出可执行的 SQL
        columns = await Column.filter(id__in=column_ids).all()
        return columns

    async def find_metrics(self, database_id: int, keywords: list[str],
                           embedding_model: Embeddings) -> list[Metric]:
        """按关键词检索指标：只在 database_id 指定的业务库的指标集合里检索（见 register），不跨库。

        指标与字段 / 取值一样按库分开存（指标归属某个业务库，见 models.meta.Metric.database）；
        与字段集合同一套结构：点的载荷只有指标编号，命中后按编号回元数据库取整行，
        指标名 / 描述 / 别名只存元数据库一份。关键词同样按检索口径加上指令后再向量化（见 build_query_text）。
        """
        embeddings = await embedding_model.aembed_documents([build_query_text(keyword) for keyword in keywords])
        requests = [QueryRequest(query=embedding, with_payload=True) for embedding in embeddings]
        responses = await self.client.query_batch_points(build_metrics_collection(database_id), requests)
        # 同一个指标可能有若干个点（重复写入或旧的命名文本），按编号去重后再回表，结果里不会重复出现
        metric_ids = set(point.payload["id"] for res in responses for point in res.points if point.payload)
        if not metric_ids:
            return []
        # 元数据库里已经删掉的指标（见 controller 的 delete_metric）取不回来，直接略过：
        # 残留的点只是占空间，不会再被召回
        metrics = await Metric.filter(id__in=metric_ids).all()
        return metrics

    async def find_values(self, database_id: int, column_ids: list[str]) -> list[Value]:
        """按字段编号批量召回取值：只在 database_id 指定的业务库的取值集合里查（见 register），不跨库。

        取值点一个字段一个（见 add_values），payload 就是这个字段的全部取值范围，故按字段编号过滤即可
        一次取回整批字段的取值——过滤条件取 payload 的 column_id，走 register 建好的关键字索引。
        没有取值登记的字段不会出现在结果里（调用方按空列表处理）。
        """
        if not column_ids:
            return []
        records, _ = await self.client.scroll(
            collection_name=build_values_collection(database_id),
            scroll_filter=Filter(must=[FieldCondition(key="column_id", match=MatchAny(any=column_ids))]),
            with_payload=True
        )
        # 一个字段一个点，重复采集残留的旧点按字段编号去重；老格式的点（payload 是单个取值）读出来对不上
        # 新结构，直接略过——重新采集一遍即可恢复
        values: dict[str, list[Any]] = {}
        for record in records:
            payload = record.payload or {}
            if isinstance(payload.get("payload"), list) and payload.get("column_id"):
                values.setdefault(payload["column_id"], payload["payload"])
        return [Value(payload=payload, column_id=column_id) for column_id, payload in values.items()]

    async def add_columns(self, database_id: int, columns: list[Column], embedding_model: Embeddings) -> None:
        """向向量数据库批量插入字段信息。

        - database_id 指定这批字段所属的业务库，决定写进哪一个字段集合（columns.<数据库编号>，
          见 register）：字段与取值都按库分开存，检索时不会串库；
        - 表名、表描述、字段名、字段描述、字段别名拼成一段文本交给嵌入模型（见 build_column_text），
          一个字段一个点：几样讲的都是同一个字段，合起来的语义最完整，检索一次就能覆盖字段的各种说法；
          嵌入模型由调用方传入（图上下文的 Context.embedding_model），保证写库与检索同源；
        - 表名与表描述从元数据库一次取回（表编号形如 <库编号>.<表名>，描述只在 Table 上）：
          表名也能从字段编号里拆出，但描述不行，故按 table_id 查一次；查不到所属表时退回编号里的表名、
          描述留空，不至于让个别脏数据挡住整批写入；
        - payload 只记字段编号（见 build_column_payload）：字段内容留在元数据库，不在向量库里存第二份；
        - 点编号取 uuid4（Qdrant 要求点必须自带编号，服务端不会代生成）：编号不承载「字段 + 文本」的
          语义；代价是 upsert 不再是「同编号覆盖」，重复采集会不断堆积重复点，故重新向量化前先按 payload
          的字段编号删掉旧点（见 delete_columns，update_columns 就是先删后写，整批一个来回）。
        """
        collection = build_columns_collection(database_id)
        # 表名与表描述：一次把这批字段涉及的表查回来（表编号形如 <库编号>.<表名>，描述只在 Table 上）；
        # 查不到的表退回编号里的表名、描述留空，与拼 SQL 用的是同一个表名
        table_ids = {column.table_id for column in columns}
        tables = await Table.filter(id__in=list(table_ids)).only("id", "name", "description") if table_ids else []
        table_text_of = {table.id: (table.name, table.description) for table in tables}
        for start in range(0, len(columns), INSERT_BATCH_SIZE):
            chunk = columns[start:start + INSERT_BATCH_SIZE]
            texts = [build_column_text(column, *table_text_of.get(
                column.table_id, (column.table_id.removeprefix(f"{database_id}."), None)))
                     for column in chunk]
            embeddings = await embedding_model.aembed_documents(texts)
            points = [PointStruct(id=str(uuid4()), vector=embedding, payload=build_column_payload(column))
                      for column, embedding in zip(chunk, embeddings)]
            await self.client.upsert(collection, points)
            logger.info(f"add_columns: {min(start + INSERT_BATCH_SIZE, len(columns))}/{len(columns)}")

    async def add_metrics(self, database_id: int, metrics: list[Metric],
                          embedding_model: Embeddings) -> None:
        """向向量数据库批量插入指标信息。

        - database_id 指定这批指标所属的业务库（见 models.meta.Metric.database），决定写进哪一个指标集合
          （metrics.<数据库编号>，见 register）：指标与字段 / 取值一样按库分开存，检索时不会串库；
        - 指标名、指标描述、指标别名拼成一段文本交给嵌入模型（见 build_metric_text），一个指标一个点；
          嵌入模型由调用方传入（controller 取 plugins.agents.models 里的那个，与检索侧同源，见类说明）；
        - payload 只记指标编号（见 build_metric_payload）：指标内容留在元数据库，不在向量库里存第二份；
        - 点编号取 uuid4（与 add_columns 一致，Qdrant 要求点必须自带编号、服务端不会代生成）：编号不
          承载语义，重复写入会堆积重复点，故重写前先按 payload 的指标编号删掉旧点（见 delete_metrics，
          update_metrics 就是先删后写，整批一个来回）。
        """
        collection = build_metrics_collection(database_id)
        for start in range(0, len(metrics), INSERT_BATCH_SIZE):
            chunk = metrics[start:start + INSERT_BATCH_SIZE]
            embeddings = await embedding_model.aembed_documents([build_metric_text(metric) for metric in chunk])
            points = [PointStruct(id=str(uuid4()), vector=embedding, payload=build_metric_payload(metric))
                      for metric, embedding in zip(chunk, embeddings)]
            await self.client.upsert(collection, points)
            logger.info(f"add_metrics: {min(start + INSERT_BATCH_SIZE, len(metrics))}/{len(metrics)}")

    async def add_values(self, database_id: int, values: list[Value]) -> None:
        """向向量数据库批量写入字段取值：一个字段一个点，payload 是它的全部取值范围。

        - database_id 指定这批取值所属的业务库，决定写进哪一个取值集合（values.<数据库编号>，
          见 register）：取值与字段一样按库分开存；
        - 取值集合只做 payload 检索（find_values 按字段编号整批取回），因此不写向量；
        - payload 存该字段的全部取值范围（数组，原类型直存：Qdrant 的 payload 原生支持数组，出库即原样，
          不必再像从前那样一个取值一个点、还额外记一份类型）；
        - 点编号取 uuid4（与 add_columns 一致，Qdrant 要求点必须自带编号、服务端不会代生成）：编号不承载
          语义；代价是 upsert 不再是「同编号覆盖」，重复写入会堆积重复点，故重写前先按字段编号删掉旧点
          （见 delete_values，update_columns 就是先删后写）。
        """
        if not values:
            return
        collection = build_values_collection(database_id)
        for start in range(0, len(values), INSERT_BATCH_SIZE):
            chunk = values[start:start + INSERT_BATCH_SIZE]
            points = [PointStruct(id=str(uuid4()), vector={},
                                  payload={"payload": list(x.payload), "column_id": x.column_id})
                      for x in chunk]
            await self.client.upsert(collection, points)
            logger.info(f"add_values: {min(start + INSERT_BATCH_SIZE, len(values))}/{len(values)}")

    async def delete_database(self, database_id: int) -> None:
        """删掉一个业务库在向量库里的全部数据：字段 / 取值 / 指标三个集合
        （columns.<数据库编号>、values.<数据库编号>、metrics.<数据库编号>）各是一个集合，整集合删除。

        删除业务库时调用（见 controller 的 delete_database）：库都删了，挂在它名下的字段点、取值点与指标点
        也不会再被检索，留着的只是占空间的孤儿集合。集合不存在（本就没采集过，或已删过）时静默跳过，
        故重复调用是安全的。
        """
        collections = (build_columns_collection(database_id), build_values_collection(database_id),
                       build_metrics_collection(database_id))
        for collection in collections:
            if not await self.client.collection_exists(collection):
                continue
            await self.client.delete_collection(collection)
            logger.info(f"delete_database: collection {collection} dropped")

    async def delete_values(self, database_id: int, column_ids: list[str]) -> None:
        """按字段编号删除取值点：删掉这些字段在向量库里登记的全部取值（不再是可用取值时调用）。

        - database_id 指定这些字段所属的业务库，决定删哪个取值集合（values.<数据库编号>，见 register）：
          取值按库分开存，不跨库删；
        - 过滤条件取 payload 的 column_id，与 add_values 写入时挂的字段一致，走 register 建好的关键字索引；
          编号用 MatchAny 一次带上，多个字段的取值一次请求删完，不必逐字段往返（字段编号可只传一个）；
        - 只删向量库里的点，不动元数据库：字段自身的登记与同步标记仍归调用方维护；
        - 空列表直接返回：Qdrant 不接受空的 MatchAny，且没有编号本来也不该命中任何点。
        """
        if not column_ids:
            return
        collection = build_values_collection(database_id)
        await self.client.delete(
            collection_name=collection,
            points_selector=FilterSelector(filter=Filter(
                must=[FieldCondition(key="column_id", match=MatchAny(any=column_ids))])
            )
        )
        logger.info(f"delete_values: removed all value(s) of {len(column_ids)} column(s) in {collection}")

    async def delete_columns(self, database_id: int, column_ids: list[str]) -> None:
        """按字段编号删除字段点：把字段那段文本（见 build_column_text）算出的向量整批清掉。

        - database_id 指定这些字段所属的业务库，决定删哪个字段集合（columns.<数据库编号>，见 register）：
          字段按库分开存，不跨库删；
        - 过滤条件取 payload 的 id，与 add_columns 写入时挂的字段一致，走 register 建好的关键字索引；
          编号用 MatchAny 一次带上，多个字段的点一次请求删完，不必逐字段往返；
        - 两个调用方：重新向量化前先清旧点（见 update_columns）；表已从业务库里移除时清掉其字段的点
          （见 plugins.celery.tasks 的巡检——字段不会再被采集，留下的点只会被 find_columns 召回，
          拼出的 SQL 必然执行失败）。取值点不在删除范围内（见 delete_values），清表时两者一起删；
        - 只删向量库里的点，不动元数据库：字段自身的登记仍归调用方维护；
        - 空列表直接返回：Qdrant 不接受空的 MatchAny。
        """
        if not column_ids:
            return
        await self.client.delete(
            collection_name=build_columns_collection(database_id),
            points_selector=FilterSelector(filter=Filter(
                must=[FieldCondition(key="id", match=MatchAny(any=column_ids))])
            )
        )
        logger.info(f"delete_columns: removed all point(s) of {len(column_ids)} column(s)")

    async def delete_metrics(self, database_id: int, metric_ids: list[int]) -> None:
        """按指标编号删除指标点：把指标那段文本（见 build_metric_text）算出的向量整批清掉。

        - database_id 指定这些指标所属的业务库，决定删哪个指标集合（metrics.<数据库编号>，见 register）：
          指标按库分开存，不跨库删；
        - 过滤条件取 payload 的 id，与 add_metrics 写入时挂的编号一致，走 register 建好的关键字索引；
          编号用 MatchAny 一次带上，多个指标的点一次请求删完，不必逐个往返（编号可只传一个）；
        - 两个调用方：改写向量前先清旧点（见 update_metrics）；指标登记已删除时清掉它的点
          （见 controller 的 delete_metric——登记没了，残留的点也取不回数据，只是占空间）；
        - 只删向量库里的点，不动元数据库：指标自身的登记仍归调用方维护；
        - 空列表直接返回：Qdrant 不接受空的 MatchAny。
        """
        if not metric_ids:
            return
        collection = build_metrics_collection(database_id)
        await self.client.delete(
            collection_name=collection,
            points_selector=FilterSelector(filter=Filter(
                must=[FieldCondition(key="id", match=MatchAny(any=metric_ids))])
            )
        )
        logger.info(f"delete_metrics: removed all point(s) of {len(metric_ids)} metric(s) in {collection}")

    async def update_columns(self,
                             database_id: int,
                             changes: list[tuple[Column, set[str]]],
                             embedding_model: Embeddings) -> None:
        """把一批字段的改动同步到向量库：必要时重新向量化，并维护取值点。

        :param database_id: 这些字段所属的业务库编号，决定写哪一套集合（columns.<数据库编号> /
            values.<数据库编号>，见 register）与取哪个业务库连接（database.<数据库编号>）
        :param changes: 每项是「改动后的字段（元数据库里的最新状态）+ 本条显式改动的字段名集合」
            （集合即调用方的 updates.keys()）。逐条给出：一批里有的改了描述与别名、有的只改了角色，
            不能按同一种改动处理
        :param embedding_model: 嵌入模型，与检索侧同源（见类说明），仅在需要重新向量化时用到

        - 字段名 / 描述 / 别名参与向量化（与表名、表描述拼成一段文本，见 build_column_text）：哪条改到了
          这些字段，它拼出的文本就变了、旧向量随之作废，先把这些编号一次性删点，再整批重写（见
          delete_columns / add_columns，两者都按批往返）；别名的数量、顺序都按 column 上的现值为准；
        - 其余字段（类型、角色、同步标记等）不落向量库（payload 只记字段编号，见 build_column_payload）：
          只改这些字段时向量库无需改动，find_columns 取值时自然读到元数据库里的新值，也就不必再调
          嵌入模型；
        - 改了 synchronized 的字段才动取值集合：由 False 变 True 按取值采集口径重新取回它的取值写进
          取值集合，由 True 变 False 删掉它的全部取值点（不再是可用取值）。字段类型不属于 VALUE_TYPES
          时不碰取值集合，与采集的取值范围一致（见 models.is_value_type）；取值同样成批取、成批删、
          成批写（见 get_column_values / delete_values / add_values）；
        - 只动向量库，不动元数据库：字段自身的登记仍归调用方维护（见 controller 的 update_column）。
        """
        if not changes:
            return
        # 逐条判断改动落到哪一类：一批里各条的改动可以完全不同（有的改描述、有的只改角色）
        vectorized = [column for column, updated in changes if VECTORIZED_FIELDS & updated]
        if vectorized:
            # 先删后写：点编号随机，不先清旧点会与本次写入的点并存（旧别名的向量会一直留在库里）
            await self.delete_columns(database_id, [column.id for column in vectorized])
            await self.add_columns(database_id, vectorized, embedding_model)

        touched = [column for column, updated in changes if "synchronized" in updated]
        if not touched:
            return
        # 关掉同步的一律清掉取值点；仍同步的只处理值类型（与采集的取值范围一致）
        disabled = [column for column in touched if not column.synchronized]
        await self.delete_values(database_id, [column.id for column in disabled])
        targets = [column for column in touched if column.synchronized and is_value_type(column.type)]
        skipped = [column for column in touched if column.synchronized and not is_value_type(column.type)]
        if skipped:
            logger.info(f"update_columns: {len(skipped)} column(s) are not value type, skip value collection")
        if not targets:
            return
        # 表编号形如 <库编号>.<表名>，取值拼 SQL 用的是业务库表名（与 discover_tables 取出表名的口径一致）
        table_of = {column.id: column.table_id.removeprefix(f"{database_id}.") for column in targets}
        # 先取回取值再清旧点：取数失败时不至于把已有的取值点删空（一次查询取回整批字段的取值）
        values = await get_column_values(
            [(table_of[column.id], column.name, VALUE_LIMIT, 0) for column in targets], database_id)
        # 一个字段一个点，payload 装它的全部取值范围；NULL 不是可用取值，直接丢弃（与采集一致）
        points = [Value(payload=[value for value in values.get(f"{table_of[column.id]}.{column.name}", [])
                                 if value is not None],
                        column_id=column.id)
                  for column in targets]
        await self.delete_values(database_id, [column.id for column in targets])
        # 没有取值的字段不必写点：旧点已经删掉，find_values 取不到就是该字段没有取值
        await self.add_values(database_id, [x for x in points if x.payload])
        logger.info(f"update_columns: collected value(s) for {len([x for x in points if x.payload])} "
                    f"of {len(targets)} column(s)")

    async def update_metrics(self, database_id: int, metrics: list[Metric],
                             embedding_model: Embeddings) -> None:
        """把一批指标的改动同步到向量库：按新内容成批重写这些指标的向量。

        :param database_id: 这些指标所属的业务库编号，决定写哪个指标集合（metrics.<数据库编号>，见 register）
        :param metrics: 改动后的指标（元数据库里的最新状态），同一批要一起改的一律放进来
        :param embedding_model: 嵌入模型，与检索侧同源（见类说明）

        - 指标名 / 描述 / 别名都参与向量化，且拼成同一段文本（见 build_metric_text）：改了哪一样，
          那段文本就变了、整条向量随之作废，故先按编号一次性删旧点、再整批重写（见 delete_metrics /
          add_metrics，两者都按批往返）；别名的数量、顺序都按 metric 上的现值为准；
        - 与 update_columns 不同，这里没有「只改元数据、不动向量」的分支：指标可改的字段全在文本里；
        - 只动向量库，不动元数据库：指标自身的登记仍归调用方维护（见 controller 的 update_metric）。
        """
        if not metrics:
            return
        await self.delete_metrics(database_id, [metric.id for metric in metrics])
        await self.add_metrics(database_id, metrics, embedding_model)

    async def register(self) -> None:
        # 字段、取值与指标三类集合都按业务库分开建：遍历元数据库里已登记的库，每个库各建一套（集合名见
        # add_columns / add_values / add_metrics 用的 build_*_collection）。集合与索引都在这里建好，写入与
        # 检索就不必每次先判断集合是否存在；元数据库里删掉的库不回收集合，残留集合不影响其它库
        for database_id in await Database.all().values_list("id", flat=True):
            await self._register_database(database_id)

    async def _register_database(self, database_id: int) -> None:
        """为单个业务库建字段 / 取值 / 指标集合，并建好写入与检索所依赖的 payload 索引。"""
        # columns 按整体文本一个向量（匿名）：一个字段一段文本一个点（见 build_column_text），与 add_columns 口径一致
        columns = build_columns_collection(database_id)
        if not await self.client.collection_exists(columns):
            await self.client.create_collection(columns, VectorParams(size=1024, distance=Distance.COSINE))
        # 字段集合的 payload 只有字段编号（见 build_column_payload），它既是字段点的唯一内容，也是范围删除的
        # 过滤条件（delete_columns 按编号用 MatchAny 一次性删掉某个字段的全部点）。建关键字索引，删点才不会
        # 每次全表扫；create_payload_index 可重复执行，已存在时按新配置覆盖。
        await self.client.create_payload_index(columns, field_name="id", field_schema=PayloadSchemaType.KEYWORD)
        # values 只做 payload 检索（find_values 按字段编号过滤，一次取回该字段的全部取值），不需要向量，
        # 故以空向量配置建集合（add_values 以空向量 {} 写入）。缺这个集合时 upsert 会报集合不存在、
        # find_values 也无从检索
        values = build_values_collection(database_id)
        if not await self.client.collection_exists(values):
            await self.client.create_collection(values, vectors_config={})
        # 取值点一律按字段编号过滤（find_values 召回、delete_values 删除、add_values 重写前先清旧点），
        # 建关键字索引才不会每次全表扫；create_payload_index 可重复执行，已存在时按新配置覆盖。
        # payload（取值数组）不参与过滤，故不建索引：老集合上遗留的全文索引不影响读写
        await self.client.create_payload_index(values, field_name="column_id",
                                               field_schema=PayloadSchemaType.KEYWORD)
        # metrics 与字段集合同一套结构：一个指标一段文本一个点（见 build_metric_text），按整体文本一个向量，
        # 用默认的匿名向量；payload 只记指标编号（见 build_metric_payload），建关键字索引供按编号过滤 / 删点
        metrics = build_metrics_collection(database_id)
        if not await self.client.collection_exists(metrics):
            await self.client.create_collection(metrics, VectorParams(size=1024, distance=Distance.COSINE))
        await self.client.create_payload_index(metrics, field_name="id", field_schema=PayloadSchemaType.KEYWORD)

    async def close(self) -> None:
        await self.client.close()
