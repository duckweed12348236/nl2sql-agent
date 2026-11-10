from typing import Any, Sequence

from loguru import logger
from tortoise import Tortoise
from tortoise.expressions import Q
from tortoise.models import Model as TortoiseModel
from tortoise.fields import ReverseRelation
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.connection import connections


class Model(TortoiseModel):
    excluded_fields = {"_partial", "_saved_in_db", "_custom_generated_pk", "_await_when_save"}
    # 已登记业务库的连接配置：`database.<Database.id>` -> 库连接地址（见 renew）
    connections: dict[str, str] = {}

    def dump(self) -> dict[str, Any]:
        """把实例转成可序列化的字典，顺带展开已取出的关联对象。

        关联对象只在「已取到」时才出现在 ``__dict__`` 里（预取 / fetch_related / 赋值过的外键），
        因此这里按值判断形态：Model 递归 dump，反向关系（ReverseRelation，含多对多）与
        已加载的对象列表逐个 dump——不这么展开就会把 ReverseRelation 这个描述符本体写进响应，
        序列化出来的东西毫无意义。未取到的关联根本不在 ``__dict__`` 里，不会触发查询。
        """
        pairs = {}
        excluded_fields = self.excluded_fields
        for key, value in self.__dict__.items():
            if key in excluded_fields:
                continue
            if key.startswith("_"):
                key = key[1:]

            if isinstance(value, Model):
                pairs[key] = value.dump()
            elif ((isinstance(value, list) and len(value) > 0 and isinstance(value[0], Model)) or
                  isinstance(value, ReverseRelation)):
                pairs[key] = [x.dump() for x in value]
            else:
                pairs[key] = value
        return pairs

    @classmethod
    async def init(cls):
        from config import TORTOISE_ORM_CONFIG
        await cls.open(TORTOISE_ORM_CONFIG)
        await cls.renew()

    @classmethod
    async def open(cls, config: dict[str, Any]):
        """初始化 Tortoise：建连接池、注册模型。

        :param config: Tortoise 的配置字典（见 config.TORTOISE_ORM_CONFIG）

        Tortoise 1.x 把「当前上下文」存在 ContextVar 里，而 FastAPI / Starlette 的 lifespan 跑在 anyio 的
        子任务中（上下文是另一份拷贝）：在那里 init 出来的上下文，处理请求的任务看不到，一查库就报
        ``No TortoiseContext is currently active``。故开 ``_enable_global_fallback``——它额外挂一份进程级
        全局上下文（见 tortoise.context.set_global_context），ContextVar 里取不到时回退到它，正好覆盖
        「lifespan 里初始化、请求里使用」这种跨任务场景；单任务脚本里 ContextVar 命中，走不到回退分支，
        行为不变。这份全局上下文在 :meth:`close` 里随连接一起清掉，重复 init 不会撞「已存在」。
        """
        await Tortoise.init(config, _enable_global_fallback=True)

    @classmethod
    async def close(cls):
        await Tortoise.close_connections()

    @classmethod
    async def renew(cls) -> None:
        """连接元数据库，把元数据库里已登记的业务库注册成连接。

        从元数据库读出全部业务库（models.meta.Database），键取 `database.<Database.id>`、
        值取库的连接地址，登记进 connections 字典；登记结果写回
        config.TORTOISE_ORM_CONFIG["connections"]（Tortoise 的 connections.db_config 就是这份字典，
        见 tortoise.connection），重启 Tortoise 后即可按名取该库的连接（见 get_connection）。

        不登记也不保存默认业务库：用哪个库由调用方按编号指定，database_id 一路传进各查询函数
        （见 get_connection），元数据库里哪个库被标成默认（Database.default）是界面上的默认选中，
        与连接无关，故这里不看它、也不记默认库编号。
        """
        from config import TORTOISE_ORM_CONFIG
        from models.meta import Database
        databases = await Database.all()
        cls.connections = {
            **{f"database.{database.id}": database.url for database in databases},
            **TORTOISE_ORM_CONFIG["connections"]
        }
        await cls.close()
        await cls.open({**TORTOISE_ORM_CONFIG, "connections": cls.connections})


def get_connection(database_id: int) -> BaseDBAsyncClient:
    """按业务库编号取连接：编号即 Database.id，连接名是 `database.<Database.id>`（见 Model.renew）。

    :param database_id: 业务库编号（元数据库 Database.id）
    :return: 该业务库的连接；库没登记过时由 Tortoise 抛出连接名不存在的错误

    元数据库自己的连接不走这里：它挂在 Tortoise 的默认连接上（见 config.TORTOISE_ORM_CONFIG 的
    default_connection），各模型按它读写。
    """
    return Tortoise.get_connection(f"database.{database_id}")


async def register_connection(database_id: int, url: str) -> None:
    """登记（或更新）一个业务库的连接：地址写进连接配置，已建好的旧连接对象关掉丢掉。

    :param database_id: 业务库编号（Database.id），连接别名即 `database.<编号>`
    :param url: 业务库连接地址

    连接对象由 Tortoise 按别名惰性创建（见 tortoise.connection.ConnectionHandler.get）：配置里有了
    这个别名、旧对象又被丢掉，下一次取连接就会按新地址重建——所以新增业务库、改了连接地址，都用它
    把连接同步到位，不必像 renew 那样把全部连接关掉重开。API 进程里新增 / 修改业务库时调用；
    worker 是另一个进程，跑采集前也要补登记一次（见 plugins.celery.tasks.collect_database）。
    """
    from config import TORTOISE_ORM_CONFIG
    alias = f"database.{database_id}"
    TORTOISE_ORM_CONFIG["connections"][alias] = url
    await _drop_connection(alias)


async def discard_connection(database_id: int) -> None:
    """摘掉某个业务库的连接：别名从连接配置里删掉，已建好的连接对象关掉丢掉（删库时用）。"""
    from config import TORTOISE_ORM_CONFIG
    alias = f"database.{database_id}"
    TORTOISE_ORM_CONFIG["connections"].pop(alias, None)
    await _drop_connection(alias)


async def _drop_connection(alias: str) -> None:
    """关掉并丢掉某个别名下已建好的连接对象（配置里的条目由调用方增删）。"""
    connection = connections.discard(alias)
    if connection is not None:
        await connection.close()


# 每个字段同步的取值条数上限：只取去重后的前 VALUE_LIMIT 个，该集合不代表字段的全部取值
VALUE_LIMIT = 50
# 需要采集取值的字段类型：整数类型与字符串类型。Column.type 取自 information_schema.column_type
# （形如 varchar(64)、bigint(20) unsigned），故按括号与空格截出基础类型名后再匹配
VALUE_TYPES = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint",
               "char", "varchar", "tinytext", "text", "mediumtext", "longtext"}


def is_value_type(column_type: str) -> bool:
    """该类型的字段是否需要采集取值：只处理整数类型与字符串类型。

    Column.type 形如 varchar(64)、bigint(20) unsigned，先截出括号前、空格前的基础类型名再匹配，
    因此长度、精度与 unsigned / zerofill 等修饰都不影响判断。取值采集与向量库都按这个口径挑字段
    （见 plugins.agents.collection.embed_columns 与 plugins.qdrant.Qdrant.update_columns）。
    """
    head = (column_type or "").partition("(")[0].split()
    return bool(head) and head[0].lower() in VALUE_TYPES


async def get_column_values(columns: list[tuple[str, str, int, int]],
                            database_id: int) -> dict[str, list[Any]]:
    """批量取回若干字段的取值（去重），按 `表名.列名` 归组返回。

    :param columns: 四元组 (表名, 列名, limit, offset)：每个字段取去重后的 [offset, offset + limit) 段
    :param database_id: 业务库编号（Database.id）：取值在该库上取（见 get_connection）

    每个字段对应一条 SELECT：内层 DISTINCT 去重后按 limit / offset 取一段，用常量列标记取值归属的字段；
    多条 SELECT 以 UNION ALL 合并成一次查询（同一条 UNION 里的字段排序规则需一致，见下），
    Python 侧再按标记列归组。

    去重由 SQL 的 DISTINCT 完成；NULL 也算一种取值，由 DISTINCT 保留一条。
    查询不做排序，故同一字段多次调用的窗口不保证稳定（LIMIT / OFFSET 的固有行为）。
    返回的字典覆盖传入的每个字段，未取到取值的字段为空列表。

    UNION ALL 要求各分支同一列位置的排序规则兼容：业务库里不同表的字段排序规则可能不同
    （实测同一库中并存 utf8mb4_general_ci、utf8_general_ci、utf8_bin、utf8mb4_unicode_ci），
    混在一条 UNION 里会被 MySQL 拒绝（1267 / 1271 Illegal mix of collations），故按排序规则分组、
    每组一条查询（见 _group_columns），而不是把整批字段塞进一条 SQL。
    """
    if not columns:
        return {}

    rows = []
    for group in await _group_columns(columns, database_id):
        rows.extend(await get_connection(database_id).execute_query_dict(_union_sql(group)))

    values = {f"{table_name}.{column_name}": [] for table_name, column_name, _, _ in columns}
    for row in rows:
        values[row["key"]].append(row["value"])
    return values


async def _column_collations(columns: list[tuple[str, str, int, int]],
                             database_id: int) -> dict[str, str | None]:
    """取这些字段的排序规则。

    :param columns: 四元组 (表名, 列名, limit, offset)
    :param database_id: 业务库编号（Database.id）
    :return: `表名.列名` → 排序规则名（字符串列有排序规则、数值列没有，取不到时为 None）

    排序规则取自业务库自己的 information_schema（与取值同库）。
    """
    pairs = list(dict.fromkeys((table_name, column_name) for table_name, column_name, _, _ in columns))
    table_names = list(dict.fromkeys(table_name for table_name, _ in pairs))
    column_names = list(dict.fromkeys(column_name for _, column_name in pairs))
    sql = ("SELECT TABLE_NAME, COLUMN_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
           f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({', '.join(['%s'] * len(table_names))}) "
           f"AND COLUMN_NAME IN ({', '.join(['%s'] * len(column_names))})")
    rows = await get_connection(database_id).execute_query_dict(sql, table_names + column_names)
    collations = {(row["TABLE_NAME"], row["COLUMN_NAME"]): row["COLLATION_NAME"] for row in rows}
    return {f"{table_name}.{column_name}": collations.get((table_name, column_name))
            for table_name, column_name in pairs}


async def _group_columns(columns: list[tuple[str, str, int, int]],
                         database_id: int) -> list[list[tuple[str, str, int, int]]]:
    """按排序规则把字段分组，返回可分多条 UNION ALL 执行的组。

    :param columns: 四元组 (表名, 列名, limit, offset)
    :param database_id: 业务库编号（Database.id，取排序规则用）
    :return: 分组后的字段（组内排序规则一致）

    字符串列按排序规则分组；数值列没有排序规则（见 _column_collations），并入第一组字符串列
    （整批都是数值列时自成一组，整数原样返回）：它们与字符串分支同组会被 UNION 聚合为字符串。
    取值原样写进向量库的 payload 数组（见 plugins.qdrant.add_values），故字段之间的类型差异不会串味。
    """
    collations = await _column_collations(columns, database_id)
    groups = {}
    numbers = []
    for table_name, column_name, limit, offset in columns:
        collation = collations.get(f"{table_name}.{column_name}")
        if collation is None:
            numbers.append((table_name, column_name, limit, offset))
        else:
            groups.setdefault(collation, []).append((table_name, column_name, limit, offset))
    if numbers:
        groups.setdefault(next(iter(groups), None), []).extend(numbers)
    return list(groups.values())


def _union_sql(columns: list[tuple[str, str, int, int]]) -> str:
    """把一组字段拼成一条 UNION ALL 查询：每个字段一条 SELECT，用常量列标记取值归属的字段。"""
    selects = [
        f"SELECT '{table_name}.{column_name}' AS `key`, `value`\n"
        f"FROM (SELECT DISTINCT `{column_name}` AS `value` FROM `{table_name}` "
        f"LIMIT {int(limit)} OFFSET {int(offset)}) AS `v{index}`"
        for index, (table_name, column_name, limit, offset) in enumerate(columns)
    ]
    return "\nUNION ALL\n".join(selects) + ";"


async def clear_column_relations(column_ids: Sequence[str]) -> int:
    """清掉这些字段的关联关系：字段的描述 / 别名改过，之前识别出的 JOIN 关系不再可信。

    :param column_ids: 字段编号（Column.id）
    :return: 删掉的关系行数

    关系存在 ColumnRelation 中间表里，一行是 (source, target)、识别时正反两行一起写，故按「任一端落在
    这批编号里」删即可把整条关系清干净，不会留下半边。关系是识别模型照着字段描述与别名判断出来的
    （见 plugins.agents.retrieval.nodes.recognise_relations），描述 / 别名一改，判断依据就没了，留着
    只会让生成 SQL 时按旧语义拼 JOIN；清掉后下一次检索会重新识别。
    """
    if not column_ids:
        return 0
    # 延迟导入：models.meta 反过来依赖本模块的 Model（见其导入），放模块顶部会循环导入
    from models.meta import ColumnRelation
    ids = list(column_ids)
    deleted = await ColumnRelation.filter(Q(source_id__in=ids) | Q(target_id__in=ids)).delete()
    logger.info(f"clear_column_relations: removed {deleted} row(s) of {len(ids)} column(s)")
    return deleted


async def get_db_info(database_id: int) -> dict[str, Any]:
    """取业务库的方言 / 版本 / 字符集（编号为 Database.id，见 get_connection）。"""
    connection = get_connection(database_id)
    dialect = connection.capabilities.dialect
    rows = await connection.execute_query_dict(
        "SELECT VERSION() AS `version`, @@character_set_database AS `charset`;"
    )
    return {"dialect": dialect, "version": rows[0]["version"], "charset": rows[0]["charset"]}


async def explain_sql(sql: str, database_id: int) -> tuple[bool, str | None]:
    """用 EXPLAIN 校验 SQL 能否在该业务库上执行（语法与库结构），不真正执行。"""
    try:
        await get_connection(database_id).execute_query_dict(f"EXPLAIN {sql};")
        return True, None
    except Exception as e:
        logger.error(e)
        return False, str(e)


async def execute_raw_sql(sql: str, database_id: int) -> list[dict[str, Any]]:
    """在业务库上执行 SQL 并取回结果行。"""
    return await get_connection(database_id).execute_query_dict(sql)


async def get_table_names(database_id: int, database_name: str | None = None) -> list[str]:
    """列出业务库里的基础表。

    :param database_id: 业务库编号（Database.id）：连哪个库（见 get_connection）
    :param database_name: 库名（MySQL 的 schema 名）：不传就按所连的库（DATABASE()）过滤
    :return: 表名列表，按表名排序
    """
    schema = f"'{database_name}'" if database_name else "DATABASE()"
    sql = f"""
    SELECT table_name AS `table_name`
    FROM information_schema.tables
    WHERE table_type = 'BASE TABLE'
      AND table_schema = {schema}
    ORDER BY table_name;
    """
    rows = await get_connection(database_id).execute_query_dict(sql)
    return [row["table_name"] for row in rows]


async def get_table_infos(table_names: list[str], database_id: int) -> list[dict[str, Any]]:
    """批量取回表结构。

    :param table_names: 表名列表
    :param database_id: 业务库编号（Database.id）：读这些表所在的库（见 get_connection）

    每条记录为 {name, comment, columns}，其中 columns 已反序列化为列数组，
    每个元素形如 {name, type, nullable, default, extra, keys, comment}。
    表/列的 comment 与列的 extra 在缺失（NULL 或空串）时统一为 None；
    列的 default 仅在无默认值（NULL）时为 None，保留合法的空串默认值 ''。

    把列 / 键拆成两条互不关联的集合查询，各自按 table_schema + table_name 过滤，
    再在内存里按 (表名, 列名) 归组。避免原先「每列各跑一次关联子查询」导致的开销
    （information_schema 在 MySQL 8 是数据字典视图，关联子查询会逐列重复扫描）。
    """
    if not table_names:
        return []
    expr = ", ".join([f"'{x}'" for x in table_names])
    connection = get_connection(database_id)

    columns = await connection.execute_query_dict(f"""
    SELECT c.table_name,
           t.table_comment,
           c.column_name,
           c.column_type,
           c.is_nullable,
           c.column_default,
           c.extra,
           c.column_comment
    FROM information_schema.columns AS c
             JOIN information_schema.tables AS t
                  ON t.table_schema = c.table_schema
                      AND t.table_name = c.table_name
    WHERE c.table_schema = DATABASE()
      AND c.table_name IN ({expr})
    ORDER BY c.table_name, c.ordinal_position;
    """)

    key_rows = await connection.execute_query_dict(f"""
    SELECT kcu.table_name,
           kcu.column_name,
           tc.constraint_type,
           kcu.referenced_table_name,
           kcu.referenced_column_name
    FROM information_schema.key_column_usage AS kcu
             JOIN information_schema.table_constraints AS tc
                  ON tc.constraint_schema = kcu.constraint_schema
                      AND tc.constraint_name = kcu.constraint_name
                      AND tc.table_name = kcu.table_name
    WHERE kcu.table_schema = DATABASE()
      AND kcu.table_name IN ({expr})
    ORDER BY kcu.table_name, kcu.column_name, tc.constraint_type;
    """)

    keys: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in key_rows:
        constraint_type = row["constraint_type"]
        if constraint_type == "PRIMARY KEY":
            key = {"type": "PK"}
        elif constraint_type == "UNIQUE":
            key = {"type": "UQ"}
        elif constraint_type == "FOREIGN KEY":
            key = {"type": "FK",
                   "ref_table": row["referenced_table_name"],
                   "ref_column": row["referenced_column_name"]}
        else:
            key = {"type": constraint_type}
        keys.setdefault((row["table_name"], row["column_name"]), []).append(key)

    tables: dict[str, dict[str, Any]] = {}
    for row in columns:
        table = tables.get(row["table_name"])
        if table is None:
            table = {"name": row["table_name"],
                     "comment": row["table_comment"] or None,
                     "columns": []}
            tables[row["table_name"]] = table
        position = (row["table_name"], row["column_name"])
        table["columns"].append({
            "name": row["column_name"],
            "type": row["column_type"],
            "nullable": row["is_nullable"] == "YES",
            # 仅在真正没有默认值（NULL）时为 None，保留 DEFAULT '' 这类合法空串
            "default": row["column_default"],
            "extra": row["extra"] or None,
            "keys": keys.get(position, []),
            "comment": row["column_comment"] or None,
        })

    return list(tables.values())


async def get_non_empty_table_names(table_names: list[str], database_id: int) -> list[str]:
    """返回其中至少含一行数据的表名（保持传入顺序）。

    :param table_names: 待探测的表名
    :param database_id: 业务库编号（Database.id）：在这些表所在的库上探测

    所有表合并为 **一次** 查询：每个表用一条 `SELECT ... FROM (SELECT 1 FROM t LIMIT 1)` 探测，
    命中一行即视为有数据（LIMIT 放进派生表，避免 UNION 语句中 LIMIT 语义歧义），
    再以 UNION ALL 合并；返回值按传入顺序过滤。
    """
    if not table_names:
        return []
    selects = [
        f"SELECT '{name}' AS `table_name` FROM (SELECT 1 FROM `{name}` LIMIT 1) AS `x{index}`"
        for index, name in enumerate(table_names)
    ]
    sql = "\nUNION ALL\n".join(selects) + ";"
    rows = await get_connection(database_id).execute_query_dict(sql)
    returned = {row["table_name"] for row in rows}
    return [name for name in table_names if name in returned]
