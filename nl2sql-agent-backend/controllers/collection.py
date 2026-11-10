from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from loguru import logger
from tortoise.expressions import Q
from tortoise.transactions import in_transaction

from models import Model, clear_column_relations, discard_connection, get_table_names, register_connection
from models.meta import Column, ColumnRelation, Database, Metric, Table
from agnets.models import fetch_embedding_model
from plugins.celery import submit_task
from plugins.qdrant import Qdrant
from plugins.redis import TaskType
from schemas import Schema
from schemas.collection import ColumnUpdate, DatabaseBlacklist, DatabaseCreate, DatabaseUpdate, DatabaseWhitelist, \
    MetricCreate, MetricUpdate, RelationCreate, RelationUpdate, TableBatchDelete, TableUpdate


router = APIRouter(prefix="/collection")
# 向量库封装：与采集流程 / 检索图共用同一个实例（Qdrant 是插件单例，见 plugins.qdrant），
# 改字段时把改动同步过去（见 update_columns）；嵌入模型取自 plugins.agents.models，与采集、检索同源
qdrant = Qdrant()
# 元数据库连接名（见 config.TORTOISE_ORM_CONFIG）：删库时表与字段的级联清理放在一个事务里完成
META_CONNECTION = "meta"


async def get_or_404(model: type[Model], object_id: int | str, message: str) -> Model:
    """按主键取一条元数据，取不到即 404（各资源的 get_* 都走它，报错口径统一）。"""
    instance = await model.get_or_none(id=object_id)
    if instance is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, message)
    return instance


async def get_database(database_id: int) -> Database:
    """按库编号取业务库，取不到即 404（改、删、采集都以它为入口）。"""
    return await get_or_404(Database, database_id, "指定的数据库不存在")


def database_dump(database: Database) -> dict[str, Any]:
    """业务库快照：连接地址（url）里含库账号口令，不进响应，其余字段照常返回。"""
    return {key: value for key, value in database.dump().items() if key != "url"}


async def get_table(table_id: str) -> Table:
    """按表编号取表，取不到即 404（改表与查字段列表都以它为入口）。"""
    return await get_or_404(Table, table_id, "指定的表不存在")


async def get_column(column_id: str) -> Column:
    """按字段编号取字段，取不到即 404。"""
    return await get_or_404(Column, column_id, "指定的字段不存在")


async def get_metric(metric_id: int) -> Metric:
    """按指标编号取指标，取不到即 404（改、删都以它为入口）。"""
    return await get_or_404(Metric, metric_id, "指定的指标不存在")


def apply_updates(instance: Model, payload: Schema) -> list[str]:
    """把请求体里显式提交的字段写进实例，返回需要落库的字段名；一个字段都没提交即 422。

    exclude_unset 区分「没提交」与「提交了 null」：前者不动、后者清空；请求体的字段名与模型字段
    一一对应（见 schemas.collection），交 update_from_dict 写入并做类型转换（枚举、JSON 数组、null）。
    """
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "没有可修改的字段")
    instance.update_from_dict(updates)
    return list(updates)


def relation_ref(column: Column) -> str:
    """字段引用：`<表名>.<字段名>`（与 ColumnInfo.related_columns 的格式一致，拼 SQL 时直接照着写 JOIN）。

    表名取表编号的后半段：表编号形如 <库编号>.<表名>（见 models.meta.Table）。
    """
    return f"{column.table_id.split('.', 1)[-1]}.{column.name}"


async def get_columns(*column_ids: str) -> dict[str, Column]:
    """按编号取一批字段（去重）：有编号取不到即 404——关联的两端都必须是已登记的字段。"""
    ids = list(dict.fromkeys(column_ids))
    columns = {column.id: column for column in await Column.filter(id__in=ids)}
    missing = [column_id for column_id in ids if column_id not in columns]
    if missing:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"指定的字段不存在：{'、'.join(missing)}")
    return columns


def check_relation_ends(source: Column, target: Column) -> None:
    """校验一条关联的两端：不能是同一个字段，且必须同属一个业务库（表编号的前缀就是库编号）。"""
    if source.id == target.id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "字段不能与自身建立关联")
    if source.table_id.split(".", 1)[0] != target.table_id.split(".", 1)[0]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "两个字段必须属于同一个业务库")


def relation_item(source: Column, target: Column) -> dict[str, Any]:
    """一条关联的响应：两端字段编号，外加 `<表名>.<字段名>` 的可读引用。"""
    return {"source_id": source.id, "target_id": target.id,
            "source": relation_ref(source), "target": relation_ref(target)}


async def describe_relations(rows: list[ColumnRelation]) -> list[dict[str, Any]]:
    """把关系行整理成对称的关联项：同一对字段只回一条，两端带上 `<表名>.<字段名>` 引用。

    中间表里一条关系存正反两行（见 create_relation），列表按无序对去重，
    免得同一层关系回两遍；字段已被删掉（关系行本该随外键级联消失）时跳过那一条。
    """
    pairs: dict[frozenset[str], tuple[str, str]] = {}
    for row in rows:
        if row.source_id != row.target_id:
            pairs.setdefault(frozenset((row.source_id, row.target_id)), (row.source_id, row.target_id))
    if not pairs:
        return []
    ids = {column_id for pair in pairs.values() for column_id in pair}
    columns = {column.id: column for column in await Column.filter(id__in=list(ids))}
    items = []
    for source_id, target_id in pairs.values():
        source, target = columns.get(source_id), columns.get(target_id)
        if source is None or target is None:
            continue
        items.append(relation_item(source, target))
    return items


@router.get("/database")
async def list_databases():
    """列出元数据库中已登记的业务库，按编号倒序（新登记的在前）。

    供前端选库用（表 / 字段 / 指标都按业务库划分），也用于展示采集状态：collected 为真的库已整库
    采集过、登记已锁死不能再改（见 update_database）。连接地址不进响应（里面含库账号口令），
    故这里用 only 只取要展示的几列，查询与返回都不带 url。
    """
    databases = await Database.all().only(
        "id", "name", "type", "extra", "collected", "whitelist", "blacklist").order_by("-id")
    return [database_dump(x) for x in databases]


@router.post("/database")
async def create_database(database: DatabaseCreate):
    """新增业务库：登记名称、连接地址、类型与附加配置，登记完即可发起采集。

    编号是雪花 ID、由服务端生成；collected 一律从「未采集」开始——整库采集成功后才由采集任务置位
    （见 plugins.celery.tasks.collect_database），故请求体里没有这个字段。

    登记完顺手把连接注册进 Tortoise（见 models.register_connection）：连接按别名惰性创建，
    注册之后本进程里立刻能按编号取到该库的连接（检索、采集都用它），不必等重启。
    """
    instance = Database(**database.model_dump())
    await instance.save()
    await register_connection(instance.id, instance.url)
    logger.info(f"create_database: database {instance.id}（{instance.name}）")
    return database_dump(instance)


@router.put("/database/{database_id}")
async def update_database(database_id: int, database: DatabaseUpdate):
    """修改业务库：名称、连接地址、类型、附加配置与采集名单；已采集的库只能改名单。

    采集落下的表编号、字段、取值与向量都挂在这条登记（尤其是连接地址）上，采集之后再改登记等于
    换一个库，故一律拒绝（409）——包括改名：名字虽不进编号与连接，但登记已经锁死，口径统一；
    要改就先删掉这条登记、重新登记再采集。
    唯一例外是 whitelist / blacklist（只采集 / 不采集的表名）：它们只决定后续采集与巡检看哪些表，
    与已采下的登记无关，故已采集的库也允许改；被排除的表在下一轮巡检时按「业务库里已删除」清掉登记与
    向量（见 plugins.celery.tasks 的 scan_database）。
    未提交的字段保持原值，显式提交 null 则清空该字段（extra、whitelist、blacklist 都整组替换）。

    连接地址变了要同步连接（见 models.register_connection）：连接对象按地址惰性创建，旧的连接对象
    不丢，下一次取连接还会连到旧地址；类型不影响连接，改它不必重连。
    """
    instance = await get_database(database_id)
    if instance.collected and set(database.model_dump(exclude_unset=True)) - {"whitelist", "blacklist"}:
        raise HTTPException(status.HTTP_409_CONFLICT, "该数据库已采集，除采集名单（白名单 / 黑名单）外不能再修改")
    updates = apply_updates(instance, database)
    await instance.save(update_fields=updates)
    if "url" in updates:
        await register_connection(instance.id, instance.url)
    logger.info(f"update_database: database {instance.id}（{instance.name}）改了 {list(updates)}")
    return database_dump(instance)


@router.put("/database/{database_id}/whitelist")
async def set_database_whitelist(database_id: int, whitelist: DatabaseWhitelist):
    """设置采集白名单：整组替换这个库「只采集的表名」，返回库的当前快照。

    表名是业务库里的名字（不是元数据库的表编号），还没登记的表也能先写进来；提交空列表即清空白名单
    （恢复成「全库都采」）。落库前统一去首尾空白、去重（保持提交顺序），空列表存成 null
    （见 models.meta.Database.whitelist）。

    影响范围只有后续的采集与巡检，不动已采下的登记与向量：
    - 采集（见 agnets.collection.discover_tables）：白名单非空时只看名单里的表，名单外的表既不分析也不
      向量化，即使调用方在 table_ids 里点名也不采（只会推一条跳过帧）；
    - 巡检（见 plugins.celery.tasks.scan_database）：名单外的表不算「新增」；已登记的会落到「已删除」，
      由巡检连同登记与向量一起清掉；巡检也会把名单新增的表当「新增」，可按提示重新采集。
    黑名单优先：同时出现在两个名单里的表不采。已采集的库也能改（与 update_database 对名单的例外口径一致）。
    """
    instance = await get_database(database_id)
    tables = list(dict.fromkeys(name.strip() for name in whitelist.tables if name.strip()))
    instance.whitelist = tables or None
    await instance.save(update_fields=["whitelist"])
    logger.info(f"set_database_whitelist: database {database_id} whitelist -> {tables}")
    return database_dump(instance)


@router.put("/database/{database_id}/blacklist")
async def set_database_blacklist(database_id: int, blacklist: DatabaseBlacklist):
    """设置采集黑名单：整组替换这个库「不参加采集的表名」，返回库的当前快照。

    表名是业务库里的名字（不是元数据库的表编号），还没登记的表也能先加进来；提交空列表即清空黑名单。
    落库前统一去首尾空白、去重（保持提交顺序），空列表存成 null（见 models.meta.Database.blacklist）。

    影响范围只有后续的采集与巡检，不动已采下的登记与向量：
    - 采集（见 agnets.collection.discover_tables）：黑名单里的表不分析、不向量化，即使调用方在
      table_ids 里点名也不采（只会推一条跳过帧）；
    - 巡检（见 plugins.celery.tasks.scan_database）：黑名单里的表不算「新增」；已登记的会落到「已删除」，
      由巡检连同登记与向量一起清掉——即「已采过的表后来不该出现在库上下文里」的处置方式。
    黑名单优先于白名单：同时出现在两个名单里的表不采。
    已采集的库也能改：黑名单与已锁死的登记无关（与 update_database 对名单的例外口径一致）。
    """
    instance = await get_database(database_id)
    tables = list(dict.fromkeys(name.strip() for name in blacklist.tables if name.strip()))
    instance.blacklist = tables or None
    await instance.save(update_fields=["blacklist"])
    logger.info(f"set_database_blacklist: database {database_id} blacklist -> {tables}")
    return database_dump(instance)


@router.delete("/database/{database_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_database(database_id: int):
    """删除业务库：连同它名下已登记的表、字段与指标一起删，向量库里该库的三个集合也一并清掉。

    表 / 字段 / 指标都挂在库上（外键为 RESTRICT），得先删字段、表与指标才能删库，四步放在一个事务里完成；
    采集标记（collected）不影响删除——已采集的库删掉的是这条登记与采集产物，登记没了自然不能再采集，
    也没法再被检索（字段点与指标点随集合一起删）。

    向量库的清理放在事务之后（见 Qdrant.delete_database）：元数据库是登记的事实来源，先删登记；
    向量库清不动（Qdrant 不可用）只记日志、不影响删除结果——编号是雪花 ID、不会复用，留下的孤儿集合
    只是占空间。连接一并摘掉（见 models.discard_connection）。
    """
    await get_database(database_id)
    async with in_transaction(META_CONNECTION):
        await Column.filter(table_id__startswith=f"{database_id}.").delete()
        await Table.filter(database_id=database_id).delete()
        await Metric.filter(database_id=database_id).delete()
        await Database.filter(id=database_id).delete()
    await discard_connection(database_id)
    try:
        await qdrant.delete_database(database_id)
    except Exception as error:  # noqa: BLE001 - 登记已删干净，向量库清不动只记日志
        logger.warning(f"delete_database: 清理业务库 {database_id} 的向量集合失败：{error}")
    logger.info(f"delete_database: database {database_id} deleted")


@router.post("/database/{database_id}/collect", status_code=status.HTTP_202_ACCEPTED)
async def collect_database(database_id: int):
    """发起整库采集：把采集任务交给后台的 Celery worker，立刻返回任务编号，进度靠轮询查。

    采集整库进行（登记表结构 → 分析字段 → 向量化 → 采集取值），一轮以分钟计，故不在请求里跑完：
    这里只校验库、写好任务记录（类型为 collect_database，入参在 inputs），再把任务投进队列
    （任务见 plugins.celery.tasks.collect_database），由 worker 执行——请求随即返回，进度由 worker
    实时更新在任务记录上，轮询「评测数据集」章节的任务接口（GET /evaluation/task）即可看到；
    采集成功后库的登记就此锁死（collected 置位，见 update_database），worker 是采集成功才置位的。

    采集会跳过黑名单里的表（Database.blacklist，见 set_database_blacklist）：这些表不分析、不向量化，
    也不会出现在库上下文里。
    已采集的库可以再采：业务库结构变了、或上次采集有失败帧，重跑一轮覆盖即可。
    """
    database = await get_database(database_id)
    task_id = await submit_task(TaskType.COLLECT_DATABASE, {"database_id": database.id},
                                "celery.collect_database", [database.id])
    logger.info(f"collect_database: task {task_id}, database {database.id}（{database.name}）")
    return {"task_id": task_id}


@router.get("/database/{database_id}/tables")
async def list_database_tables(database_id: int, name: str | None = Query(default=None, max_length=128)):
    """列出业务库里**实际存在**的表名（读的是业务库，不是元数据库的登记）。

    database_id 指定业务库编号（Database.id），按它取连接去查该库自己的 information_schema（见
    models.get_table_names）：反映的是业务库当前的样子——还没采集过的表也在这里；元数据库里删掉了、业务库里
    却还在的也会出现（重新采集就会把它们再登记回来）。要与「已登记的表」对照，用 GET /collection/table。

    name 不传或只传空白视作不过滤；传了就按不区分大小写的子串匹配，在取回的表名里本地过滤——业务库那一次
    查询只取一遍表名，不必为每次搜索再连一次库。结果按表名排序。

    库没登记时报 404（无从知道连哪个库）；连接不通 / 没权限时报 502——这是业务库那边的问题，不是参数问题，
    细节只记日志（连接串里含账号口令，不回给调用方）。采集会跳过采集黑名单里的表（见
    set_database_blacklist），本接口照常把它们列出来，方便挑选要拉黑的表名。
    """
    await get_database(database_id)
    try:
        tables = await get_table_names(database_id)
    except Exception as error:  # noqa: BLE001 - 外部库连不上按 502 报，原因只记日志
        logger.warning(f"list_database_tables: 读取业务库 {database_id} 的表名失败：{error}")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "读取业务库表名失败，请检查连接与权限")
    keyword = (name or "").strip()
    if keyword:
        tables = [table for table in tables if keyword.lower() in table.lower()]
    return tables


@router.get("/table")
async def list_tables(database_id: int = Query(ge=1), name: str | None = Query(default=None, max_length=128)):
    """列出某个业务库里已登记的表，name 可按表名模糊搜索（不区分大小写的子串匹配）。

    database_id 指定业务库编号（元数据库里的 Database.id）：表都归属某个库（Table.database_id），
    按库查才是可枚举的范围。库没登记时报 404，与「库下没有表」的空列表区分开。

    name 不传或只传空白视作不过滤；传了就按 LIKE '%name%' 匹配表名（Tortoise 的 icontains），
    故 % 与 _ 会被当作匹配符参与 LIKE，本接口只用于选表，不能拿它做唯一性判断。
    结果按表名排序，便于挑选。
    """
    if not await Database.filter(id=database_id).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "指定的数据库不存在")
    query = Table.filter(database_id=database_id)
    keyword = (name or "").strip()
    if keyword:
        query = query.filter(name__icontains=keyword)
    tables = await query.order_by("name")
    return [x.dump() for x in tables]


@router.put("/table/{table_id}")
async def update_table(table_id: str, table: TableUpdate):
    """修改表：只接受表类型与描述。

    表名与所属数据库编号一起决定表编号、且被字段编号引用（<表编号>.<字段名>），故不接受修改；
    未提交的字段保持原值，显式提交 null 则清空该字段。

    不需要同步向量库：向量库里只有字段（columns.<库编号>）与取值（values.<库编号>）两类点，
    表本身不参与向量化；检索用到的表信息是按字段载荷里的 table_id 回元数据库取的
    （见 plugins.agents.retrieval.nodes.recall_columns），故这里改完，检索下一次就能读到新类型与描述。

    但**改描述会让该表字段的向量文本过期**：字段的向量化文本里带着表描述（见
    plugins.qdrant.build_column_text），描述变了、字段向量没重算，召回仍按旧描述。要立即生效需重跑一次
    该库的字段向量化（python test.py <数据库编号>）。
    """
    instance = await get_table(table_id)
    await instance.save(update_fields=apply_updates(instance, table))
    return instance.dump()


async def purge_tables(tables: list[Table]) -> None:
    """删掉这些表的登记与向量点：字段、关联关系与表本身在一个事务里删，表名同时记进采集黑名单，再按库清向量。

    :param tables: 要删的表（已取出的元数据库实例，可以跨库）

    字段挂在表上（Column.table 外键为 RESTRICT），得先删字段才能删表；关联关系（ColumnRelation）的两端都
    指向字段、外键为 CASCADE，字段一删关系行就随外键消失，这里仍显式清一遍——按「任一端命中」删整条关系，
    与字段改描述时的清理同一套口径（见 models.clear_column_relations），免得依赖外键的具体行为。

    删掉的表名一并写进所属库的**采集黑名单**（Database.blacklist，存的是业务库表名）：表还在业务库里，
    巡检（见 plugins.celery.tasks.scan_database）会把它当成「已删除」，采集（见
    plugins.agents.collection.discover_tables）也会跳过——不记这一笔，下一轮巡检就会把它当新增表重新采回来，
    这次删除等于白删。黑名单只增不减，要恢复采集就把表名从黑名单里去掉（见 set_database_blacklist）。

    向量库的清理放在事务之后（见 Qdrant.delete_columns / delete_values）：元数据库是登记的事实来源，先删
    登记；向量库按库分集合，字段编号先按表所属的库分组，再逐库删。清不动只记日志、不影响删除结果——字段
    编号形如 <表编号>.<字段名>、不会复用，留下的点只是占空间，检索拿到的只是编号、按编号回元数据库取整行，
    取不到自然略过。
    """
    if not tables:
        return
    table_ids = [table.id for table in tables]
    database_of = {table.id: table.database_id for table in tables}
    # 字段编号按库分组：向量库按库分集合，删点要落到各自的集合上（一次请求删该库的全部字段点）
    columns_of: dict[int, list[str]] = {}
    for column_id, table_id in await Column.filter(table_id__in=table_ids).values_list("id", "table_id"):
        columns_of.setdefault(database_of[table_id], []).append(column_id)
    # 要拉黑的表名按库分组（同一批可以跨库），组内去重、保持原顺序
    names_of: dict[int, list[str]] = {}
    for table in tables:
        names_of.setdefault(table.database_id, []).append(table.name)
    async with in_transaction(META_CONNECTION):
        await clear_column_relations([column_id for ids in columns_of.values() for column_id in ids])
        await Column.filter(table_id__in=table_ids).delete()
        await Table.filter(id__in=table_ids).delete()
        # 表名进黑名单：与删除同一个事务——要么登记删了、名字也拉黑了，要么两边都不动，
        # 不会出现「登记已经没了、下次巡检又采回来」的中间态
        for database_id, names in names_of.items():
            database = await Database.filter(id=database_id).first()
            if database is None:
                # 库本身没登记（正常走不到，删表前一定先有库）：没有黑名单可写，删除本身照常算数
                continue
            blacklist = list(database.blacklist or [])
            added = [name for name in dict.fromkeys(names) if name not in blacklist]
            if not added:
                continue
            database.blacklist = blacklist + added
            await database.save(update_fields=["blacklist"])
            logger.info(f"purge_tables: 业务库 {database_id} 黑名单新增 {added}")
    for database_id, column_ids in columns_of.items():
        try:
            await qdrant.delete_columns(database_id, column_ids)
            await qdrant.delete_values(database_id, column_ids)
        except Exception as error:  # noqa: BLE001 - 登记已删干净，向量库清不动只记日志
            logger.warning(f"purge_tables: 清理业务库 {database_id} 的向量点失败：{error}")
    logger.info(f"purge_tables: {len(table_ids)} table(s) deleted, "
                f"{sum(len(ids) for ids in columns_of.values())} column(s)")


@router.delete("/table")
async def delete_tables(tables: TableBatchDelete):
    """批量删除表：一次删掉这批表的登记、字段与关联关系，向量库里对应的点也一并清掉。

    :param tables: 一批表编号（Table.id，形如 <库编号>.<表名>，可跨库、重复的自动去重）

    与单张删除同一套处置（见 purge_tables）：登记在一个事务里删完，表名同时记进所属库的采集黑名单
    （Database.blacklist），向量点随后按库清。删的是登记与向量，业务库里的表本身不动：虽然它还在业务库里，
    但已经在黑名单里，巡检（见 plugins.celery.tasks.scan_database）不会再把它当新增表采回来；要恢复采集，
    把表名从黑名单里去掉即可（见 set_database_blacklist）。

    查不到的编号不报错、也不影响其余表：返回 ``{"deleted": [...], "missing": [...]}``，两张清单都按请求里的
    顺序给出（deleted 已去重），调用方据此知道哪几张已经不在库里。整批都在库里找不到时 deleted 为空——
    仍是 200，批量接口把「没得删」当作正常结果。
    """
    ids = list(dict.fromkeys(tables.table_ids))
    existing = await Table.filter(id__in=ids)
    found = {table.id for table in existing}
    await purge_tables(list(existing))
    deleted = [table_id for table_id in ids if table_id in found]
    missing = [table_id for table_id in ids if table_id not in found]
    logger.info(f"delete_tables: deleted {len(deleted)} table(s), missing {len(missing)}")
    return {"deleted": deleted, "missing": missing}


@router.delete("/table/{table_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_table(table_id: str):
    """删除表：连同它名下的字段与字段间的关联关系一起删，向量库里这些字段的向量点与取值点也一并清掉。

    取不到即 404；处置细节见 :func:`purge_tables`（与批量删除共用同一套：先删登记、把表名记进采集黑名单，
    再按库清向量）。删的是**登记与向量**，业务库里的表本身不动：它虽然还在业务库里，但已经在黑名单里，
    巡检（见 plugins.celery.tasks.scan_database）不会再把它当新增表采回来；要恢复采集就把表名从黑名单里
    去掉（见 set_database_blacklist）。
    """
    instance = await get_table(table_id)
    await purge_tables([instance])
    logger.info(f"delete_table: table {table_id} deleted")


@router.get("/column")
async def list_columns(table_id: str = Query(min_length=1)):
    """列出某张表下已登记的字段。

    table_id 指定表编号（元数据库里的 Table.id，形如 <database_id>.<表名>）：字段都挂在表上
    （Column.table_id），按表查才是可枚举的范围。表没登记时报 404，与「表下没有字段」的空列表区分开。
    """
    await get_table(table_id)
    columns = await Column.filter(table_id=table_id).all()
    return [x.dump() for x in columns]


@router.put("/column/{column_id}")
async def update_column(column_id: str, column: ColumnUpdate):
    """修改字段：只接受字段角色、描述与别名（见 ColumnUpdate），改动一并同步到向量库。

    字段名与所属表一起决定字段编号（<表编号>.<字段名>）、编号又被向量库里的字段点引用，故不接受修改；
    类型与同步标记是采集口径的登记（业务库的真实类型、取值要不要采集），也不在这里改，要改就重跑采集。
    别名按整组替换（提交什么就是什么，不与原值合并）；未提交的字段保持原值，显式提交 null 则清空该字段。
    描述与别名同时是识别表间关系的依据：这两样一改，挂在该字段上的关联关系会被清掉
    （见 models.clear_column_relations），下次检索按新语义重新识别——否则生成 SQL 时会照旧语义拼 JOIN。

    两步走：先落元数据库（本接口的登记），再按改动同步向量库（见 Qdrant.update_columns）——
    描述与别名参与向量化（与表名、表描述、字段名拼成一段文本，见 plugins.qdrant.build_column_text），改了就删掉旧点、
    重新向量化；只有角色变时不必再调嵌入模型，向量库的字段点只记字段编号（见
    plugins.qdrant.build_column_payload），字段内容的改动天然由元数据库生效。
    向量库是派生数据（改字段走的就是采集那套口径，见 plugins.agents.collection.embed_columns）：
    同步失败时元数据库的改动已经落库、仍然生效，接口照常报错，重跑一轮采集即可补齐。
    """
    instance = await get_column(column_id)
    # 字段改动要按库写进对应的集合，库编号取自所属表（表编号形如 <库编号>.<表名>，见 models.meta.Table）
    table = await get_table(instance.table_id)
    updates = apply_updates(instance, column)
    await instance.save(update_fields=updates)
    # 描述 / 别名是识别表间关系的依据（见 nodes.recognise_relations）：改了就把挂在这个字段上的关系清掉，
    # 免得生成 SQL 时还按旧语义拼 JOIN；下一次检索会重新识别并补回来
    if {"description", "alias"} & set(updates):
        await clear_column_relations([instance.id])
    # 单条也走批量接口：一次请求删旧点、一次请求重写，与成批改动同一套写法（改动集合逐条给）
    await qdrant.update_columns(table.database_id, [(instance, set(updates))], await fetch_embedding_model())
    return instance.dump()


@router.get("/relation")
async def list_relations(column_id: str | None = Query(default=None, min_length=1),
                         table_id: str | None = Query(default=None, min_length=1)):
    """列出字段间的关联关系，可按字段或表过滤。

    :param column_id: 只看与这个字段有关的关联（Column.id，形如 <表编号>.<字段名>）
    :param table_id: 只看与这张表有关的关联（Table.id）：取该表名下的字段，再按它们过滤；按表查时请先
        用 GET /collection/table 拿到表编号

    两个参数都不传则列出全部关联；都传时以 column_id 为准（它更具体）。column_id / table_id 不存在时
    报 404，与「没有关联」的空列表区分开。

    关联是双向的（中间表里一条关系存正反两行，见 create_relation），列表按无序
    对去重：同一层关系只回一条，两端用 source / target 给出 `<表名>.<字段名>` 的引用，与检索侧写进
    ColumnInfo.related_columns 的格式一致（拼 SQL 时直接照着写 JOIN）。
    """
    query = ColumnRelation.all()
    if column_id:
        await get_column(column_id)
        query = query.filter(source_id=column_id)
    elif table_id:
        await get_table(table_id)
        column_ids = list(await Column.filter(table_id=table_id).values_list("id", flat=True))
        if not column_ids:
            return []
        query = query.filter(source_id__in=column_ids)
    return await describe_relations(list(await query.order_by("source_id", "target_id")))


@router.post("/relation", status_code=status.HTTP_201_CREATED)
async def create_relation(relation: RelationCreate):
    """新增关联：登记两个字段可以 JOIN，正反两个方向一次写好。

    :param relation: 两端字段编号（Column.id，形如 <表编号>.<字段名>）

    校验三条：两端都已登记（否则 404）、不是同一个字段、同属一个业务库（跨库无法 JOIN，按 422 拒绝）。
    中间表里一条关系存正反两行（与识别关系同一套写法，见 create_relation），
    故提交一次即可两个方向都查得到；已存在同一对字段时报 409（无论当初是哪个方向提交的）。

    写库在一个事务里完成：要么两行都写好，要么都不写。手工补的关系与识别出来的关系等价——检索侧的
    load_related_refs 直接读这张中间表，下一次检索就会把它们交给模型拼 JOIN。
    """
    columns = await get_columns(relation.source_id, relation.target_id)
    source, target = columns[relation.source_id], columns[relation.target_id]
    check_relation_ends(source, target)
    exists = await ColumnRelation.filter(
        Q(source_id=source.id, target_id=target.id) | Q(source_id=target.id, target_id=source.id)
    ).exists()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "这两个字段的关联已存在")
    async with in_transaction(META_CONNECTION):
        await ColumnRelation.bulk_create([ColumnRelation(source_id=source.id, target_id=target.id),
                                          ColumnRelation(source_id=target.id, target_id=source.id)],
                                         ignore_conflicts=True)
    logger.info(f"create_relation: {source.id} <-> {target.id}")
    return relation_item(source, target)


@router.put("/relation")
async def update_relation(relation: RelationUpdate):
    """更新关联：按两端定位已有关系，把某一端换成另一个字段（另一端不动）。

    :param relation: source_id / target_id 定位原关系；new_source_id / new_target_id 指定换上去的字段

    没给 new_source_id / new_target_id 时按原值处理；两个都不给即没有要改的东西，按 422 拒绝。换上去的
    字段同样要已登记（否则 404）、与另一端同属一个业务库、且不能与自身关联（422）；换出来的那一对若已
    存在别的关联，按 409 拒绝（关系不能重复登记）。

    关系是双向的：旧的一对正反两行一起删、新的一对正反两行一起写，在一个事务里完成，不会出现「只删了一半」
    或「一个方向可用、另一个方向查不到」。换端前后其实是同一对字段时原样返回，不做多余写库。
    """
    if not relation.new_source_id and not relation.new_target_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "没有可修改的字段：new_source_id 与 new_target_id 至少给一个")
    columns = await get_columns(relation.source_id, relation.target_id)
    source, target = columns[relation.source_id], columns[relation.target_id]
    exists = await ColumnRelation.filter(
        Q(source_id=source.id, target_id=target.id) | Q(source_id=target.id, target_id=source.id)
    ).exists()
    if not exists:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "这两个字段之间没有关联")
    new_source_id = relation.new_source_id or source.id
    new_target_id = relation.new_target_id or target.id
    if {new_source_id, new_target_id} == {source.id, target.id}:
        return relation_item(source, target)
    new_columns = await get_columns(new_source_id, new_target_id)
    new_source, new_target = new_columns[new_source_id], new_columns[new_target_id]
    check_relation_ends(new_source, new_target)
    duplicated = await ColumnRelation.filter(
        Q(source_id=new_source.id, target_id=new_target.id) | Q(source_id=new_target.id, target_id=new_source.id)
    ).exists()
    if duplicated:
        raise HTTPException(status.HTTP_409_CONFLICT, "这两个字段的关联已存在")
    async with in_transaction(META_CONNECTION):
        await ColumnRelation.filter(
            Q(source_id=source.id, target_id=target.id) | Q(source_id=target.id, target_id=source.id)
        ).delete()
        await ColumnRelation.bulk_create([ColumnRelation(source_id=new_source.id, target_id=new_target.id),
                                          ColumnRelation(source_id=new_target.id, target_id=new_source.id)],
                                         ignore_conflicts=True)
    logger.info(f"update_relation: {source.id} <-> {target.id} -> {new_source.id} <-> {new_target.id}")
    return relation_item(new_source, new_target)


@router.delete("/relation", status_code=status.HTTP_204_NO_CONTENT)
async def delete_relation(source_id: str = Query(min_length=1), target_id: str = Query(min_length=1)):
    """删除关联：正反两个方向一起删。

    :param source_id: 一端字段编号（Column.id，形如 <表编号>.<字段名>）
    :param target_id: 另一端字段编号

    两端都给齐而不是按「一行」删：中间表里一条关系存正反两行（见 create_relation），只删一行会留下
    「换个方向还能查到」的半条关系。字段本身不存在时同样按 404 报（与「字段在、但没有这层关联」区分开
    的是后者的提示语）；这一对之间没有任何关联时也报 404。
    """
    columns = await get_columns(source_id, target_id)
    source, target = columns[source_id], columns[target_id]
    deleted = await ColumnRelation.filter(
        Q(source_id=source.id, target_id=target.id) | Q(source_id=target.id, target_id=source.id)
    ).delete()
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "这两个字段之间没有关联")
    logger.info(f"delete_relation: {source.id} <-> {target.id}, {deleted} row(s) removed")


@router.get("/metric")
async def list_metrics(database_id: int | None = Query(default=None, ge=1)):
    """列出已登记的指标，可按业务库过滤。

    database_id 不传则列出全部库的指标；传了就只看这个库的（指标与表 / 字段一样按库划分，见
    models.meta.Metric.database）。按库划分之前登记的指标没有所属库（database_id 为空），只有不过滤时
    才看得到。
    """
    query = Metric.filter(database_id=database_id) if database_id else Metric.all()
    metrics = await query
    return [x.dump() for x in metrics]


@router.post("/metric")
async def create_metric(metric: MetricCreate):
    """新增指标：提交所属业务库、指标名、描述与别名，登记后立即向量化写进该库的指标集合。

    指标归属某个业务库（见 models.meta.Metric.database），编号为雪花 ID、由服务端生成。
    两步走：先落元数据库（本接口的登记），再把指标名 / 描述 / 别名拼成的文本写进该库的向量集合
    （见 Qdrant.add_metrics）：一个指标一个点，点的载荷只挂这个编号（见 plugins.qdrant.build_metric_payload），
    指标内容一律回元数据库取。向量库是派生数据：写入失败时登记已经生效、接口照常报错，再 PUT 一次即可补齐。
    """
    if not await Database.filter(id=metric.database_id).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "指定的数据库不存在")
    instance = Metric(**metric.model_dump())
    await instance.save()
    await qdrant.add_metrics(instance.database_id, [instance], await fetch_embedding_model())
    return instance.dump()


@router.put("/metric/{metric_id}")
async def update_metric(metric_id: int, metric: MetricUpdate):
    """修改指标：指标名、描述与别名都可改，改完按新内容重写该指标的向量。

    与表 / 字段不同，指标编号是雪花 ID、与名字无关，改名不牵动任何引用；别名按整组替换。
    未提交的字段保持原值，显式提交 null 则清空该字段。三样都参与向量化（见 plugins.qdrant.build_metric_text），
    故落库后先按编号删旧点、再整条重写（见 Qdrant.update_metrics）；同步失败时元数据库的改动仍然生效，
    接口照常报错，重试本接口即可补齐。
    所属库不在可改字段里；按库划分之前登记、没有所属库的老指标没法定位向量集合，只改登记、跳过向量同步。
    """
    instance = await get_metric(metric_id)
    await instance.save(update_fields=apply_updates(instance, metric))
    if instance.database_id is None:
        logger.warning(f"update_metric: 指标 {metric_id} 没有所属业务库，跳过向量同步（重新登记指标即可带上）")
        return instance.dump()
    # 单条也走批量接口，与成批改动同一套写法
    await qdrant.update_metrics(instance.database_id, [instance], await fetch_embedding_model())
    return instance.dump()


@router.delete("/metric/{metric_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_metric(metric_id: int):
    """删除指标：登记与向量点一起清。

    先删元数据库（登记是事实来源），再按编号删该库指标集合里的点（见 Qdrant.delete_metrics）。
    向量库清不动只记日志、不影响删除结果：检索拿到的只是编号、按编号回元数据库取整行（见
    Qdrant.find_metrics），登记删掉后残留的点取不回数据、会被略过，只是占空间。
    没有所属库的老指标只删登记（不知该清哪个集合）。
    """
    instance = await get_metric(metric_id)
    database_id = instance.database_id
    await instance.delete()
    if database_id is None:
        logger.warning(f"delete_metric: 指标 {metric_id} 没有所属业务库，跳过向量点清理")
        return
    try:
        await qdrant.delete_metrics(database_id, [metric_id])
    except Exception as error:  # noqa: BLE001 - 登记已删干净，向量库清不动只记日志
        logger.warning(f"delete_metric: 清理指标 {metric_id} 的向量点失败：{error}")
