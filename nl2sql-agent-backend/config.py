import tomllib
from pathlib import Path
from pkgutil import iter_modules

# 日志参数（由 logger.setup_logger 使用）
LOG_LEVEL = "INFO"
LOG_FILE = "logs/{time:YYYY-MM-DD}.log"
# Celery worker 进程的日志单独成文件（由 logger.setup_celery_logger 使用），不与应用日志混在一起
LOG_CELERY_FILE = "logs/{time:YYYY-MM-DD}.celery.log"
LOG_ROTATION = "00:00"
LOG_RETENTION = "30 days"
LOG_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)
LOG_FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}"


def _load_config(section: str, key: str, default=None):
    """读取 [tool.<section>] 下的配置项。"""
    return PYPROJECT.get(section, {}).get(key, default)


# MySQL 连接串：元数据库搬到 PostgreSQL 之后，它只用来访问搬走之前的老元数据库（迁移脚本用），
# 业务库的连接地址在 database 表的行里、与它无关
MYSQL_URL = "mysql://root:123456@localhost:3306/text2sql"
# 元数据库走 PostgreSQL：业务库登记、表、字段、指标与评测数据集都在这个库里（评测会话的 checkpoint
# 也存在这里，见 agnets.checkpointers）。scheme 用 postgres://：Tortoise 只认 asyncpg://
# 与它的别名 postgres://（不认 postgresql://），而 psycopg 认 postgres://，两边共用这一个值
POSTGRESQL_URL = "postgres://postgres:123456@localhost:5432/text2sql"

CORS_ALLOW_ORIGINS = ["*"]
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_METHODS = ["*"]
CORS_ALLOW_HEADERS = ["*"]
CORS_EXPOSE_HEADERS = ["*"]

import models

TORTOISE_ORM_CONFIG = {
    "connections": {
        "meta": POSTGRESQL_URL
    },
    "apps": {
        "models": {
            "models": [f"{models.__name__}.{x.name}" for x in iter_modules(models.__path__)] + ["aerich.models"],
            "default_connection": "meta"
        }
    },
    "use_tz": True,
    "timezone": "UTC"
}

SNOWFLAKE_DATACENTER_ID = 0
SNOWFLAKE_WORKER_ID = 0
SNOWFLAKE_SEQUENCE = 0

QDRANT_URL = "http://localhost:6333"

# Redis：检索索引、短期记忆缓存与 Celery 共用一套连接配置（连接串由 plugins/redis.py 的
# build_redis_url 拼出）。0 号库留给 RediSearch 索引与缓存（索引只允许建在 0 号库，见
# plugins.redis.migrate），故 Celery 的 broker 与结果后端另用两个库，互不打扰。
# 密码为空表示无鉴权。
REDIS_HOST = "localhost"
REDIS_PORT = 6379
REDIS_PASSWORD = ""
REDIS_DB = 0

# Celery 总开关：暂时停用（False 时不拉起 worker 子进程、也不向 broker 投递；后台任务改由 API 进程内的
# asyncio 任务直接跑，进度帧与任务记录照旧，调用方轮询无感，见 plugins.celery.submit_task 与
# plugins.celery.tasks.run_locally）。恢复时改成 True 并重启应用即可，任务定义本身不用改
CELERY_ENABLED = True

# Celery：broker 与结果后端复用上面的 Redis 地址与口令，这里只指定各自的库号。
# concurrency 为 0、pool 为空串时用默认值（进程池在 Windows 上默认 solo，其余平台 prefork）。
CELERY_BROKER_DB = 1
CELERY_RESULT_DB = 2
CELERY_LOGLEVEL = "INFO"
CELERY_CONCURRENCY = 0
CELERY_POOL = ""
# 拉起 worker 时是否内嵌 beat（-B）按周期投递定时任务：单实例部署够用，多实例时应关掉并单独跑 celery beat
CELERY_BEAT = True
# 表结构巡检开关：暂时关停（False 时调度表为空、任务本身也直接返回），
# 恢复时改成 True 并重启 worker / beat 即可（见 plugins.celery 的 beat_schedule 与 tasks.scan_tables）
CELERY_SCAN_ENABLED = False
# 表结构巡检周期（秒）：定时收集业务库里已变动 / 新增 / 删除的表（见 plugins.celery.tasks）
CELERY_SCAN_INTERVAL = 600
# beat 的调度状态文件（PersistentScheduler 的 shelve 库）：放 logs 下，不在项目根目录留 celerybeat-schedule
CELERY_BEAT_SCHEDULE_FILE = "logs/celerybeat-schedule"

READY = False

if not READY:
    READY = True

    # 从 pyproject.toml 的 [tool] 读取配置，未配置的项沿用上面的默认值
    with (Path(__file__).resolve().parent / "pyproject.toml").open("rb") as fp:
        PYPROJECT = tomllib.load(fp).get("tool", {})

    MYSQL_URL = _load_config("mysql", "url", MYSQL_URL)

    POSTGRESQL_URL = _load_config("postgresql", "url", POSTGRESQL_URL)
    TORTOISE_ORM_CONFIG["connections"]["meta"] = POSTGRESQL_URL

    CORS_ALLOW_ORIGINS = _load_config("cors", "allow_origins", CORS_ALLOW_ORIGINS)
    CORS_ALLOW_CREDENTIALS = _load_config("cors", "allow_credentials", CORS_ALLOW_CREDENTIALS)
    CORS_ALLOW_METHODS = _load_config("cors", "allow_methods", CORS_ALLOW_METHODS)
    CORS_ALLOW_HEADERS = _load_config("cors", "allow_headers", CORS_ALLOW_HEADERS)
    CORS_EXPOSE_HEADERS = _load_config("cors", "expose_headers", CORS_EXPOSE_HEADERS)

    SNOWFLAKE_DATACENTER_ID = _load_config("snowflake", "datacenter_id", SNOWFLAKE_DATACENTER_ID)
    SNOWFLAKE_WORKER_ID = _load_config("snowflake", "worker_id", SNOWFLAKE_WORKER_ID)
    SNOWFLAKE_SEQUENCE = _load_config("snowflake", "sequence", SNOWFLAKE_SEQUENCE)

    QDRANT_URL = _load_config("qdrant", "url", QDRANT_URL)

    REDIS_HOST = _load_config("redis", "host", REDIS_HOST)
    REDIS_PORT = _load_config("redis", "port", REDIS_PORT)
    REDIS_PASSWORD = _load_config("redis", "password", REDIS_PASSWORD)
    REDIS_DB = _load_config("redis", "db", REDIS_DB)

    CELERY_ENABLED = _load_config("celery", "enabled", CELERY_ENABLED)
    CELERY_BROKER_DB = _load_config("celery", "broker_db", CELERY_BROKER_DB)
    CELERY_RESULT_DB = _load_config("celery", "result_db", CELERY_RESULT_DB)
    CELERY_LOGLEVEL = _load_config("celery", "loglevel", CELERY_LOGLEVEL)
    CELERY_CONCURRENCY = _load_config("celery", "concurrency", CELERY_CONCURRENCY)
    CELERY_POOL = _load_config("celery", "pool", CELERY_POOL)
    CELERY_BEAT = _load_config("celery", "beat", CELERY_BEAT)
    CELERY_SCAN_ENABLED = _load_config("celery", "scan_enabled", CELERY_SCAN_ENABLED)
    CELERY_SCAN_INTERVAL = _load_config("celery", "scan_interval", CELERY_SCAN_INTERVAL)
    CELERY_BEAT_SCHEDULE_FILE = _load_config("celery", "beat_schedule_file", CELERY_BEAT_SCHEDULE_FILE)


