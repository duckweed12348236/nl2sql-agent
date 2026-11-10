/**
 * 元数据管理接口的数据结构（见 api_docs.md「元数据管理」）。
 *
 * 雪花 ID 是服务端生成的有符号 64 位整数，超出 JS 安全整数范围，
 * 请求层已把它们转成字符串，因此这里统一按 string 声明。
 */

/** 表类型 */
export type TableRole = 'fact' | 'dim'

/** 字段角色 */
export type ColumnRole = 'primary_key' | 'foreign_key' | 'measure' | 'dimension'

/** 数据库类型（当前只有 mysql） */
export type DatabaseType = 'mysql'

/** 业务库快照；连接地址含账号口令，只写不读、不在响应里 */
export interface Database {
  /** 雪花 ID */
  id: string
  name: string
  type: DatabaseType | null
  /** 附加配置（JSON） */
  extra: Record<string, unknown> | null
  /** 整库采集成功后会置位，登记就此锁死、不能再改（黑名单除外） */
  collected: boolean
  /** 不参加采集的表名（业务库里的表名，非表编号），未设置时为 null */
  blacklist: string[] | null
}

/** 新增业务库 */
export interface DatabaseCreate {
  name: string
  /** 连接地址（含账号口令），只写不读 */
  url: string
  type?: DatabaseType
  extra?: Record<string, unknown> | null
  blacklist?: string[] | null
}

/** 修改业务库：只提交字段表示保留原值，提交 null 表示清空 */
export interface DatabaseUpdate {
  name?: string | null
  url?: string | null
  type?: DatabaseType | null
  extra?: Record<string, unknown> | null
  blacklist?: string[] | null
}

export interface Table {
  /** 形如 `<database_id>.<表名>` */
  id: string
  name: string
  role: TableRole | null
  description: string | null
  /** 所属业务库的雪花 ID */
  database_id: string
}

export interface Column {
  /** 形如 `<table_id>.<字段名>` */
  id: string
  name: string
  /** 业务库的真实类型，来自采集登记，不提供修改 */
  type: string | null
  role: ColumnRole | null
  description: string | null
  alias: string[] | null
  /** 所属表编号 */
  table_id: string
  /** 是否采集取值，来自采集登记，不提供修改 */
  synchronized: boolean
  /** 部分取值样本（采样写入，全量在向量库的 values 集合里），没采过时为空数组 */
  values: string[] | null
}

export interface Metric {
  /** 雪花 ID */
  id: string
  name: string
  description: string | null
  alias: string[] | null
  /** 所属业务库编号；按库划分之前登记的老指标为 null */
  database_id: string | null
}

/**
 * 一条字段关联：两端各带 `<表名>.<字段名>` 的可读引用。
 * 关联是双向的（中间表存正反两行），接口按无序对去重后返回。
 */
export interface Relation {
  /** 一端字段编号（Column.id） */
  source_id: string
  /** 另一端字段编号 */
  target_id: string
  source: string
  target: string
}

/**
 * 批量删除表的出参：真正删掉的与元数据库里查不到的编号（按请求里的顺序）。
 * 查不到的编号不报错、也不影响其余表。
 */
export interface TableBatchDeleteResult {
  deleted: string[]
  missing: string[]
}

/** 修改表：只提交字段表示保留原值，提交 null 表示清空 */
export interface TableUpdate {
  role?: TableRole | null
  description?: string | null
}

/**
 * 修改字段：可改的只有角色、描述与别名。
 * `type` 与 `synchronized` 是采集口径的登记，接口不再接受（提交也会被当成额外字段忽略）。
 */
export interface ColumnUpdate {
  role?: ColumnRole | null
  description?: string | null
  alias?: string[] | null
}

/** 新增指标：归属业务库必填，登记后不可再改 */
export interface MetricCreate {
  /** 所属业务库编号（Database.id），请求层按整数原样输出 */
  database_id: string
  name: string
  description?: string | null
  alias?: string[] | null
}

/** 修改指标：所属库不在可改字段里 */
export interface MetricUpdate {
  name?: string | null
  description?: string | null
  alias?: string[] | null
}

/** 新增关联：两个方向一次写好 */
export interface RelationCreate {
  source_id: string
  target_id: string
}

/** 更新关联：按两端定位，把某一端换成另一个字段 */
export interface RelationUpdate {
  source_id: string
  target_id: string
  new_source_id?: string | null
  new_target_id?: string | null
}
