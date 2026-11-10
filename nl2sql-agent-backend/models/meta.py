from enum import StrEnum
from typing import Any

from tortoise.fields import BigIntField, CharField, TextField, JSONField, BooleanField, CharEnumField, \
    ForeignKeyField, ManyToManyField, ReverseRelation, RESTRICT, CASCADE
from models import Model
from plugins.snowflake import Snowflake


class DatabaseTypeOption(StrEnum):
    """业务库类型：连接与方言都按它走。当前只实现了 MySQL——取值查询、表结构读取与 EXPLAIN
    校验用的都是 MySQL 的 information_schema / 语法（见 models.get_table_infos / models.explain_sql），
    别的库型要先补齐这些再放开取值。
    """
    MYSQL = "mysql"


class Database(Model):
    """业务库注册表：一个可供采集与查询的数据库。

    id 用雪花 ID：全局唯一、趋势递增的有符号 64 位整数（见 plugins.snowflake），
    因此字段为 BigIntField 且 generated=False——不入库自增，取值在 Python 侧由 default 生成
    （Tortoise 构造实例时对缺省字段调用可调用的 default，save 与 bulk_create 都走这条路）。

    collected 表示「这个库已经整库采集过」：采集成功后由采集任务置位（见
    plugins.celery.tasks.collect_database），置位之后库的登记就不再允许修改——采集落下的表编号、
    字段与向量都挂在这条登记（尤其是连接地址）上，改了登记等于换了库。

    whitelist 是**只**采集的表名（业务库里的表名，不是表编号）：非空时采集（见
    agnets.collection.discover_tables / embed_columns）与表结构巡检（见 plugins.celery.tasks.scan_database）
    只看这些表，其余的表一律当不存在；空或 null 表示不限（库里所有表都采）。白名单是「先圈范围」，
    采集前还要按它把业务库的表名过滤一遍，模型看不到范围外的表。

    blacklist 是不参加采集的表名（同样是业务库表名）：优先级高于白名单——两个名单里都有的表不采；
    它空或 null 表示不过滤。已采集的表被加进黑名单后，下一轮巡检会把它当「业务库里已删除」处理，
    登记与向量一并清掉。删表接口会自动把删掉的表名记进来（见 controllers.collection.purge_tables），
    免得下一轮巡检 / 采集又把它当新增表采回来。

    两个名单都只影响后续的采集与巡检，不动已采下的登记与向量（白名单之外的表若已被采过，靠黑名单或删表
    接口清理）。
    """
    id = BigIntField(pk=True, generated=False, default=Snowflake.get_id, description="数据库编号（雪花 ID）")
    name = CharField(max_length=128, description="数据库名称")
    url = CharField(max_length=255, description="数据库连接地址")
    type = CharEnumField(DatabaseTypeOption, default=DatabaseTypeOption.MYSQL, description="数据库类型")
    extra = JSONField[dict[str, Any] | None](null=True, description="附加配置（JSON）")
    collected = BooleanField(default=False, description="是否已采集")
    whitelist = JSONField[list[str] | None](null=True, description="只采集的表名（白名单，空则不限）")
    blacklist = JSONField[list[str] | None](null=True, description="不参与采集的表名（黑名单）")
    tables: ReverseRelation[Table]

    class Meta:
        table = "database"


class TableRoleOption(StrEnum):
    FACT = "fact"
    DIM = "dim"


class Table(Model):
    """业务库里的表：归属于 Database 中的某个库。

    database 必填：表编号形如 <Database.id>.<表名>，库归属由采集工具入参里的 database_id 提供
    （见 plugins.agents.collection.discover_tables）；已登记的表沿用元数据库里的原编号与原关联。
    """

    id = CharField(max_length=255, pk=True, description="表编号")
    name = CharField(max_length=128, null=True, description="表名称")
    role = CharField(max_length=100, null=True, description="表类型(fact/dim)")
    description = TextField(null=True, description="表描述")
    database = ForeignKeyField("models.Database", "tables", RESTRICT, description="所属数据库编号")
    database_id: int
    columns: ReverseRelation[Column]

    class Meta:
        table = "table"
        # 库内表名唯一（业务库本来就是这么保证的）：跨库同名表各有各的编号，
        # 元数据库层面也兜底，避免改名 / 并发采集 / 模型幻觉插出重复的 (数据库, 表名)
        unique_together = ("database_id", "name")


class ColumnRoleOption(StrEnum):
    PRIMARY_KEY = "primary_key"
    FOREIGN_KEY = "foreign_key"
    MEASURE = "measure"
    DIMENSION = "dimension"


class Column(Model):
    id = CharField(max_length=255, pk=True, description="列编号")
    name = CharField(max_length=128, null=True, description="列名称")
    type = CharField(max_length=64, null=True, description="数据类型")
    role = CharEnumField(ColumnRoleOption, null=True, description="列类型(primary_key,foreign_key,measure,dimension)")
    description = TextField(null=True, description="列描述")
    alias = JSONField[list[str] | None](null=True, description="列别名")
    table = ForeignKeyField("models.Table", "columns", RESTRICT, description="所属表编号")
    table_id: str
    synchronized = BooleanField(default=False, description="是否为枚举列")
    # 该字段在业务库里的部分取值（样本，不是全部）：检索时拼进库上下文、也用来给模型举例。
    # 取值全量仍存在向量库的 values.<库编号> 集合里（一个字段一个点，见 Qdrant.add_values），
    # 这里只留一小撮，省得为看几个样本再连一次向量库；默认空列表，没采过取值的就是空的
    values = JSONField[list[Any]](default=list, null=True, description="该字段在数据库中的部分取值")
    # 可用于关联查询的字段（多对多指向 Column 自己，关系行落在 ColumnRelation 中间表）：一条关系双向登记两行。
    # 读写都直接走中间表（见 recognise_relations / load_related_refs）：批量、幂等，也不必为自引用多对多的
    # 预取行为操心；这里声明多对多是为了把「字段之间可关联」这层关系表达清楚
    related_columns = ManyToManyField("models.Column", through="ColumnRelation",
                                      related_name="related_by",
                                      forward_key="source_id", backward_key="target_id")

    class Meta:
        table = "column"


class ColumnRelation(Model):
    """字段关联关系：Column.related_columns 的中间表，一行表示「源字段与目标字段可以关联查询」。

    两端都指向 Column，外键都是 CASCADE：字段随表、随库删掉时，挂着它的关系行一并消失，不留孤儿。
    (source, target) 唯一：关系本身是双向的，识别时正反两行一起写，重复识别由 ignore_conflicts 跳过。
    """
    source = ForeignKeyField("models.Column", related_name=False, on_delete=CASCADE, description="源字段编号")
    source_id: str
    target = ForeignKeyField("models.Column", related_name=False, on_delete=CASCADE, description="目标字段编号")
    target_id: str

    class Meta:
        table = "column_relation"
        unique_together = ("source_id", "target_id")


class Metric(Model):
    """业务指标：id 用雪花 ID 生成（同 Database，见其说明）。

    database：所属业务库——指标与表 / 字段一样按库划分，只在所属库的检索里召回，不跨库（见
    Qdrant.find_metrics）。这一列是可以为空的：按库划分之前登记的指标没有它，任何库都召回不到，
    重新登记一次即可。
    """

    id = BigIntField(pk=True, generated=False, default=Snowflake.get_id, description="指标编号（雪花 ID）")
    name = CharField(max_length=128, null=True, description="指标名称")
    description = TextField(null=True, description="指标描述")
    alias = JSONField[list[str] | None](null=True, description="指标别名")
    database = ForeignKeyField("models.Database", "metrics", RESTRICT, null=True, description="所属数据库编号")
    database_id: int | None

    class Meta:
        table = "metric"
