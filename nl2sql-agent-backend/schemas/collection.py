from typing import Any

from pydantic import Field

from models.meta import ColumnRoleOption, DatabaseTypeOption, TableRoleOption
from schemas import Schema


class DatabaseCreate(Schema):
    """新增业务库的请求体：名称、连接地址、类型、附加配置与采集名单。

    编号是雪花 ID、由服务端生成（见 models.meta.Database 的 default），不在请求体里；
    collected（是否已采集）也不在请求体里——它由采集流程写：整库采集成功才置位，登记时一律「未采集」。

    whitelist 是**只**采集的表名、blacklist 是不采集的表名（都是业务库里的表名，不是表编号）：
    白名单非空时采集与巡检只看名单里的表，黑名单再从里面剔除（同时出现在两个名单里的表不采）；
    两个字段留空（或 null）即各自不生效，此时白名单不生效就是「全库都采」。
    见 models.meta.Database 的 whitelist / blacklist。
    """
    name: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=255)
    type: DatabaseTypeOption = DatabaseTypeOption.MYSQL
    extra: dict[str, Any] | None = None
    whitelist: list[str] | None = None
    blacklist: list[str] | None = None


class DatabaseUpdate(Schema):
    """修改业务库的请求体：名称、连接地址、类型、附加配置与采集名单。

    已采集的库不能改（见 controllers.collection.update_database）：采集落下的表编号、字段、取值与向量
    都挂在这条登记（尤其是连接地址）上，改了登记等于换一个库；要改就先删掉这条登记重新采集。
    唯一例外是 whitelist / blacklist：它们只决定后续采集与巡检看哪些表，与已采下的登记无关，
    已采集的库也允许改（专门的整组替换接口见 set_database_whitelist / set_database_blacklist）。
    未提交的字段保持原值，显式提交 null 表示清空（extra、whitelist、blacklist 都整组替换）。
    """
    name: str | None = Field(default=None, min_length=1, max_length=128)
    url: str | None = Field(default=None, min_length=1, max_length=255)
    type: DatabaseTypeOption | None = None
    extra: dict[str, Any] | None = None
    whitelist: list[str] | None = None
    blacklist: list[str] | None = None


class DatabaseWhitelist(Schema):
    """设置采集白名单的请求体：整组替换「只采集的表名」。

    tables 是业务库里的**表名**（不是元数据库的表编号）：非空时采集（见 agnets.collection.discover_tables）
    与表结构巡检（见 plugins.celery.tasks.scan_database）只看这些表，其余的一律当不存在；空列表表示清空白名单
    （即恢复成「全库都采」）。整组替换、不做增量合并：要增删就取当前值改完整体提交
    （见 controllers.collection.set_database_whitelist）。黑名单优先：同时出现在两个名单里的表不采。
    """
    tables: list[str] = Field(default_factory=list)


class DatabaseBlacklist(Schema):
    """设置采集黑名单的请求体：整组替换「不参加采集的表名」。

    tables 是业务库里的**表名**（不是元数据库的表编号）：采集（见 agnets.collection.discover_tables）
    与表结构巡检（见 plugins.celery.tasks.scan_database）都按它跳过这些表；空列表表示清空黑名单。
    整组替换、不做增量合并：要增删就取当前值改完整体提交（见 controllers.collection.set_database_blacklist）。
    白名单优先圈范围、黑名单再从里面剔除：同时出现在两个名单里的表不采。
    """
    tables: list[str] = Field(default_factory=list)


class TableCreate(Schema):
    """新增表的请求体：表名、表类型、描述与所属数据库编号。

    表编号不在请求体里，由所属数据库编号与表名拼出（<database_id>.<表名>），故 database_id 必填，
    与采集表时的编号口径一致（见 plugins.agents.collection.discover_tables）；描述可为空，
    表类型取 fact / dim 两种。
    """
    name: str = Field(min_length=1, max_length=128)
    role: TableRoleOption
    description: str | None = None
    database_id: int


class TableUpdate(Schema):
    """修改表的请求体：只接受表类型与描述。

    name 与 database_id 都不在可改字段里：两者一起决定表编号，表编号又被字段编号引用
    （<表编号>.<字段名>，见 models.meta.Column），改名等于换一张表，会牵动字段主键与向量库里的点；
    未提交的字段保持原值，role / description 显式提交 null 表示清空。
    """
    role: TableRoleOption | None = None
    description: str | None = None


class TableBatchDelete(Schema):
    """批量删除表的请求体：一批表编号（Table.id，形如 <库编号>.<表名>）。

    table_ids 可以是跨库的（一次删多个业务库里的表）：删登记与清向量都按每张表自己的库走。
    重复编号自动去重；查不到的编号不报错，会在响应的 missing 里列出来——批量删除时「有一张已经
    被删过」不该把整批回滚，调用方按返回值区分即可。一次最多 200 张。
    """
    table_ids: list[str] = Field(min_length=1, max_length=200)


class ColumnUpdate(Schema):
    """修改字段的请求体：只接受字段角色、描述与别名。

    name 与 table_id 一起决定字段编号（<表编号>.<字段名>，见 models.meta.Column），编号被向量库里的
    字段点引用，改名等于换一个字段，故不接受修改。

    type 与 synchronized 同样不在可改字段里，两者都是采集口径的登记而非人工填写的属性：type 取业务库的
    真实类型（采集时按业务库结构登记，见 plugins.agents.collection.discover_tables），该不该采集取值
    也由类型决定（见 models.is_value_type）；synchronized 是「这个字段的取值要不要采集」的标记，由采集时
    的模型分析给出。改这两样等于手动偏离业务库现状，要改就重跑一轮采集。

    可改的三个：role 取 primary_key / foreign_key / measure / dimension、description 为字段描述、
    alias 为别名数组（整组替换，不再合并）。未提交的字段保持原值，显式提交 null 表示清空。
    """
    role: ColumnRoleOption | None = None
    description: str | None = None
    alias: list[str] | None = None


class RelationCreate(Schema):
    """新增关联的请求体：两个字段编号（Column.id，形如 <表编号>.<字段名>）。

    关联是双向的：登记一次即两个方向都能 JOIN（落库时正反两行，见 plugins.agents.nodes.recognise_relations
    与 controllers.collection.create_relation），故只需提交一次；两端必须已登记、同属一个业务库，且不能是
    同一个字段。
    """
    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)


class RelationUpdate(Schema):
    """更新关联的请求体：按 source_id / target_id 定位已有关系，把某一端换成另一个字段。

    new_source_id / new_target_id 不提交就保持原值；两个都不提交即没有要改的东西（接口按 422 拒绝）。
    换完仍是双向关系（正反两行一起改），换上去的字段同样要已登记、与另一端同属一个业务库。
    """
    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    new_source_id: str | None = Field(default=None, min_length=1)
    new_target_id: str | None = Field(default=None, min_length=1)


class MetricCreate(Schema):
    """新增指标的请求体：所属业务库、指标名、描述与别名。

    指标编号是雪花 ID、由服务端生成（见 models.meta.Metric 的 default），不在请求体里。
    database_id 指明指标归属哪个业务库：指标与表 / 字段一样按库划分，只在所属库里召回（见
    Qdrant.find_metrics），登记后不可再改——换库等于换一份指标口径。
    """
    database_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    alias: list[str] | None = None


class MetricUpdate(Schema):
    """修改指标的请求体：指标名、描述与别名都在可改字段里。

    与表 / 字段不同，指标编号是雪花 ID、与名字无关，改名不影响任何引用，故允许改；
    所属业务库不在可改字段里（决定指标在哪个库里召回，换库等于换一个指标）；
    未提交的字段保持原值，显式提交 null 表示清空。
    """
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    alias: list[str] | None = None
