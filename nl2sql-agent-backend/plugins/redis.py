"""Redis 基础设施：常驻客户端、RedisOM 模型定义、索引迁移与跨进程互斥锁。

所有 Redis 操作都定义在本模块：客户端（见 :class:`RedisClient`）、模型（Schema）、迁移入口与互斥锁；
每个模型的读写方法挂在模型自己身上（见 :class:`ShortTermMemory` / :class:`Task`）。RedisOM 的连接挂在
模型类的 ``Meta.database`` 上，由 :class:`RedisClient` 在启动时统一注入（见 ``register``），全进程共用
同一份连接池；:class:`ShortTermMemory` 也可以由使用方另注入一份（见
:class:`agnets.checkpointers.CachedAsyncPostgresSaver`）。

用法::

    from plugins.redis import RedisClient, ShortTermMemory, Task, lock

    client = RedisClient().client  # 常驻客户端（插件启动时已建好，这里直接取）
    await Task.list()  # 按用户批量查任务
    async with lock("celery:scan_tables:lock", 3600) as acquired:
        ...  # 抢到锁才做事

Note:
    模型基于 RedisJSON（``JSON.SET`` / ``JSON.GET``），要求 Redis 已加载
    ReJSON 模块（Redis Stack / ``redis-stack-server`` 镜像）。裸 Redis 上
    缓存读写会失败，按事务方的 fail-open 策略降级为直连 PostgreSQL；
    :func:`migrate` 同样只告警、不阻断启动。任务记录的读写不吃 fail-open：
    没有 Redis 就没有进度可读，写不进去只能让调用方自己失败；按用户批量查
    任务还要 RediSearch 索引（见 :meth:`Task.setup`）。
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from enum import StrEnum
from time import time
from typing import Any, AsyncIterator, ClassVar, Dict

import aredis_om.model.model as aredis_om_model
from aredis_om import Field, JsonModel, Migrator, NotFoundError
from redis.asyncio import Redis
from redis.asyncio.client import Pipeline
from redis.exceptions import LockNotOwnedError

import config
from logger import logger
from plugins import Plugin

__all__ = ["DEFAULT_USER_ID", "RedisClient", "ShortTermMemory", "TASK_LIMIT", "TASK_TTL", "Task", "TaskStatus",
           "TaskType", "build_redis_url", "lock", "migrate"]

# 任务记录的保留时长（秒）：最多留 3 天，到期即从 Redis 里消失（见 Task）
TASK_TTL = 3 * 24 * 60 * 60
# 按用户批量查任务时一次取多少条（新的在前，见 Task.list）
TASK_LIMIT = 20
# 任务所属用户编号的默认值：用户体系还没有，当前一律当 1 号用户的任务（见 Task.user_id）
DEFAULT_USER_ID = 1


def assume_module_loaded(*args: Any, **kwargs: Any) -> bool:
    """顶替 aredis_om 里那两个「忘了 await」的模块检查，一律放行。

    aredis_om 1.2.0 的异步分支把 ``has_redis_json`` / ``has_redisearch``（都是 ``async def``）当同步函数调用：
    前者在 ``JsonModel.__init__``、后者在 ``FindQuery.__init__`` 里，于是每构造一个模型实例、每执行一次 ``find``
    都会造出一个没人 await 的协程对象，进程退出时 GC 一回收就刷
    ``RuntimeWarning: coroutine 'has_redis_json' was never awaited``。

    ``not <协程对象>`` 恒为 False，等于原本就没拦——所以这里换成直接返回 True 的同步检查，行为与现状一致，
    只是不再产生协程与告警。Redis 真的没装 RedisJSON / RediSearch 时，报错会在实际执行命令那一步暴露
    （模块说明里的 fail-open 口径就是按这个前提写的）。
    """
    return True


# 在模型读写之前换上（见 assume_module_loaded 的说明）：调用方按模块全局名取值，替换这两个名字即可
aredis_om_model.has_redis_json = assume_module_loaded
aredis_om_model.has_redisearch = assume_module_loaded


def build_redis_url(db: int | None = None) -> str:
    """按 :mod:`config` 中的 Redis 配置拼接连接串。

    同一个 Redis 实例按用途分库存放（0 号库放检索索引与缓存，Celery 的 broker 与结果后端
    各占一个库），地址、端口与口令统一来自 :mod:`config`，调用方只覆盖库号；``db`` 缺省
    用 :data:`config.REDIS_DB`。
    """
    auth = f":{config.REDIS_PASSWORD}@" if config.REDIS_PASSWORD else ""
    return f"redis://{auth}{config.REDIS_HOST}:{config.REDIS_PORT}/{config.REDIS_DB if db is None else db}"


class RedisClient(Plugin):
    """常驻的 Redis 客户端：一个进程一份，启动时注入模型、关闭时收掉。

    Redis 在本项目里有这几处用法：任务记录（:class:`Task`）、会话记忆缓存（:class:`ShortTermMemory`，
    见 agnets.checkpointers）、索引迁移（:func:`migrate`）与跨进程互斥锁（:func:`lock`）。
    以前它们各自按需建连、用完即关：脚本里无所谓，常驻进程（API 与 worker）里就是反复建销连接池。

    这里按项目既有的插件写法登记（见 :class:`plugins.Plugin` 与 ``plugins.register_plugins``）：
    :meth:`register` 把这份客户端注入两个 RedisOM 模型、并把 RediSearch 索引迁移一遍，:meth:`close`
    在应用关闭时收掉连接池。之后全进程共用同一份：「``RedisClient().client``」在任何地方拿到的都是
    同一个实例（单例由 :class:`metaclasses.SingletonMeta` 保证）。

    解码方式固定 ``decode_responses=True``：RedisOM 的模型与各处调用方都以字符串读写（见 Task /
    ShortTermMemory）。
    """

    def __init__(self):
        self.client = Redis.from_url(build_redis_url(), decode_responses=True)

    async def register(self) -> None:
        """把客户端注入 RedisOM 模型并迁移索引（应用启动时由 ``register_plugins`` 调用一次）。

        连接池在 :meth:`__init__` 里就建好了：RedisOM 的 ``Meta.database`` 是类属性，必须在任何模型读写
        之前填上，故这里只做注入；迁移会重建 schema 变了的索引（见 :func:`migrate`），失败只告警、
        不阻断启动。
        """
        Task.Meta.database = self.client
        ShortTermMemory.Meta.database = self.client
        await migrate(self.client)
        logger.info("redis: 常驻客户端就绪")

    async def close(self) -> None:
        """收掉连接池（应用关闭时由 ``close_plugins`` 调用）。"""
        await self.client.aclose()


@asynccontextmanager
async def lock(key: str, ttl: int, db: int | None = None) -> AsyncIterator[bool]:
    """跨进程互斥锁：进入时抢锁（yield 是否抢到），退出时释放（没抢到就不放）。

    :param key: 锁名。自带用途前缀（如 ``celery:scan_tables:lock``），别与检索索引 / 缓存的键撞名
    :param ttl: 兜底过期时间（秒）：持有者被强杀时没人会来释放锁，靠它自然失效，
        免得后来者永远抢不到
    :param db: 库号，缺省用 :data:`config.REDIS_DB`（走常驻客户端）；换成别的库（如 broker 的库号
        ``config.CELERY_BROKER_DB``）时另建一份连接、用完即关
    :yield: 是否抢到锁。抢不到时调用方应跳过这次任务，而不是等待

    抢锁是 ``SET NX PX``（redis-py 的 ``Lock``）且不阻塞：抢不到立刻返回 False，等不等由调用方
    决定。释放只在退出时进行、且只放自己持有的那把锁——锁已因 ttl 过期被别人接管时，
    redis-py 会以 :class:`redis.exceptions.LockNotOwnedError` 拒绝，这里忽略即可
    （那把锁不归我们，放掉反而会拆掉别人的锁）。

    连接默认复用常驻客户端（见 :class:`RedisClient`）：常驻进程里不必为每把锁另开连接池；换了库号
    才临时建一份、退出时关掉，因此 prefork 的子进程里同样安全。
    """
    redis = RedisClient().client if db is None else Redis.from_url(build_redis_url(db), decode_responses=True)
    try:
        mutex = redis.lock(key, timeout=ttl)
        acquired = await mutex.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    await mutex.release()
                except LockNotOwnedError:
                    pass
    finally:
        # 常驻客户端不在这里关（全进程共用）；换了库号临时建的那份用完即关
        if db is not None:
            await redis.aclose()


class ShortTermMemory(JsonModel, index=True):
    """短期记忆缓存记录：一条记录承载一个 checkpoint 的完整状态。

    记录以 JSON 文档存放，``payload`` 是 langgraph ``serde`` 序列化后的负载
    （类型名见 ``serde_type``），内容与 PostgreSQL 中持久化的 checkpoint
    一致，可无损还原 ``CheckpointTuple``；RedisOM 会把 ``bytes`` 字段按
    base64 存取，二进制负载不会撞上 JSON 的编码限制。``checkpoint_id``
    单独成字段，便于用一次 ``JSON.GET <key> $.checkpoint_id`` 判断“最新”
    记录是否仍指向它。

    键名由 ``Meta`` 前缀与主键拼成 ``stm:memory:{pk}``。``index=True`` 是
    RedisOM 对 ``save()`` / ``key()`` 的硬性要求；读写都走主键、不做
    ``find()`` 检索，索引只由 :func:`migrate` 按需建立。

    Note:
        RedisOM 的元数据挂在类上（``Meta.database``），因此同一进程内应
        共用同一个 Redis 连接；重新注入会同时影响所有使用方。
    """

    class Meta:
        global_key_prefix = "nl2sql"
        model_key_prefix = "memory"
        database = None  # 由使用方注入

    checkpoint_id: str
    serde_type: str
    payload: bytes


class TaskStatus(StrEnum):
    """任务状态：排队 → 执行 → 结束。

    ``PENDING`` 是「已投递、还没开始跑」：同一时刻只允许一个任务执行（不分类型），后来的任务在队列里
    等，按 :attr:`Task.created_at` 从早到晚出队（见 :meth:`Task.next`）。执行方拿到它就置成 ``RUNNING``，
    轮询方据此区分「排队中」与「执行中」。
    """
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


class TaskType(StrEnum):
    """后台任务的类型（``Task.type``）：取值与投递时用的 Celery 任务名一一对应（见 plugins.celery.tasks）。"""

    CREATE_DATASET = "create_dataset"
    EVALUATE_DATASET = "evaluate_dataset"
    COLLECT_DATABASE = "collect_database"


class Task(JsonModel, index=True):
    """后台任务的进度记录：类型、入参、进度帧与产出各占一列，落 Redis，由轮询接口按用户批量查。

    ``id`` 是任务编号（雪花 ID，见 :mod:`plugins.snowflake`，由 API 生成），并且就做主键——RedisOM
    支持把业务字段声明成主键（``Field(primary_key=True)``），于是不必再多一个 ``pk`` 字段：
    ``Task.get(编号)`` 取记录、``Task.id == ...`` 查记录，一个编号走到底，Redis 里也是
    ``stm:task:{编号}`` 一条记录。

    字段与索引：

    - ``id``：主键，且 ``sortable``——按用户批量查任务时要按编号倒序取最新的几条（见 :meth:`list`）；
    - ``user_id``：任务归属的用户编号，``index`` 进 RediSearch，批量查询就按它筛（见 :meth:`list`）；
      用户体系还没做，当前一律取 :data:`DEFAULT_USER_ID`（1）；
    - ``type``：任务类型（见 :class:`TaskType`，存字符串）：投递任务时就写死，之后不再变；
    - ``inputs``：投递任务时的入参（出题是所用的表、题数、业务库与会话编号，评测是被评数据集、业务库与
      会话编号，采集是业务库编号，见 ``controllers.evaluation`` / ``controllers.collection``），任务还在
      队列里时轮询就能看到它是什么任务；
    - ``outputs``：本轮**输出**，进度帧也在里面——``steps`` 是到目前为止的全部进度帧（``Step`` 的字典形态，
      见 ``agnets.schemas``，执行方每收到一帧就整体覆盖写一次，读的一方因此一次就能拿到完整过程，
      不必自己拼接）；其余键是本轮产出（出题是 ``dataset_id``，评测是 ``result_id`` 与 ``mean``，采集是
      ``database_id`` 与 ``collected``），跑出产出的那一刻起就在里面。
    - ``created_at``：创建时间（Unix 秒，``sortable``）——投递时写一次，之后不再变，排队就按它从早到晚
      （见 :meth:`next`）。记录是整体覆盖写的（见 :meth:`save`），故写进度时要把它带上（见
      ``plugins.celery.tasks.record``）；
    - ``status``：见 :class:`TaskStatus`——投递后先 ``pending`` 排队，轮到时置 ``running``，跑到最后落
      success 或 error（重试期间仍是 running，见 :mod:`plugins.celery.tasks`）；``error`` 是失败原因，
      仅 error 状态有值。

    四列内容都由写入方整体给出（见 :meth:`save`）：写入方只有任务的执行者（或投递任务的 API）一个。

    **排队也归本类管**（见 :meth:`enqueue` / :meth:`drain`）：任务不一定只在 Celery 里跑，停用 Celery 时
    就是本进程内的 asyncio 任务，故「同一时刻只跑一个、下一个跑谁」放在任务记录这里，与具体执行方式无关——
    调用方只交出「怎么执行」的协程工厂，出队、置状态、串行都由本类负责。

    记录留 :data:`TASK_TTL`（3 天，见 :meth:`save`），到期即消失，轮询方看到的只是「没有这个任务」；
    批量查询也要索引在（见 :meth:`setup`）。

    Note:
        RedisOM 的元数据挂在类上（``Meta.database``），连接由 :meth:`setup` 在启动时注入，
        同一进程内共用一份即可，不要各处重新注入。
    """

    class Meta:
        global_key_prefix = "nl2sql"
        model_key_prefix = "task"
        database = None  # 由 setup 注入

    # ── 排队执行：同一时刻只跑一个任务，按 created_at 从早到晚（见 enqueue / drain） ──────────────
    # 队列放在这里而不是 Celery 那边：任务不一定只在 Celery 里跑（停用 Celery 时就是进程内的 asyncio），
    # 「谁能同时跑、下一个跑谁」属于任务记录自己的事。

    #: 同一时刻只跑一个任务（不分类型）：队列消费者用它互斥
    QUEUE_LOCK: ClassVar[asyncio.Lock] = asyncio.Lock()
    #: 已投递待执行任务的执行体：任务编号 -> (创建时间, 执行协程的工厂)。
    #: 记录里存的是业务入参，怎么执行（哪个函数、哪些位置参数）留在进程内；进程重启后残留的排队记录
    #: 没有这份执行体，出队时直接落成失败并提示重新提交（见 :meth:`drain`）
    QUEUE: ClassVar[dict[int, tuple[float, Callable[[], Awaitable[Any]]]]] = {}
    #: 队列消费者的强引用：asyncio 对协程只持弱引用，不留一份的话排队跑到一半可能被回收
    QUEUE_RUNNERS: ClassVar[set[Any]] = set()
    #: 创建时间的进程内副本：记录是整体覆盖写的，写进度时从这里带上，别让排队顺序越写越新（见 :meth:`created_at_of`）
    CREATED: ClassVar[dict[int, float]] = {}

    id: int = Field(primary_key=True, sortable=True, description="任务编号（雪花 ID）")
    user_id: int = Field(default=DEFAULT_USER_ID, index=True, description="任务所属用户编号（暂固定为 1）")
    created_at: float | None = Field(default=None, sortable=True,
                                     description="创建时间（Unix 秒，排队按它从早到晚）")
    type: str = Field(default="", description="任务类型（见 TaskType，与投递时用的 Celery 任务名一一对应）")
    # 注解用 typing.Dict / List 而不是内置的 dict / list：RedisOM 的模型上有个 dict 方法，类体里的注解求值时
    # 会把 dict 解析成它（TypeError: 'classmethod' object is not subscriptable），故这里要避开这两个名字
    inputs: Dict[str, Any] = Field(default_factory=dict, description="投递任务时的入参")
    outputs: Dict[str, Any] = Field(default_factory=dict,
                                    description="本轮输出：steps（进度帧）与产出（dataset_id 等）")
    error: str | None = Field(default=None, description="失败原因，仅 status 为 error 时有值")
    status: TaskStatus = Field(default=TaskStatus.PENDING, index=True, description="任务状态（见 TaskStatus）")

    @classmethod
    async def setup(cls) -> None:
        """把任务记录挂到常驻的 Redis 客户端上，并建好 RediSearch 索引。

        连接现在由 :class:`RedisClient` 统一持有（见其文档）：应用启动与 worker 启动时的初始化都交给了
        插件注册（``register_plugins`` 会调 ``RedisClient.register``），这里留着是给脚本 / 单测单独把任务
        记录备好用的，重复调用无副作用。

        顺手调一次 :func:`migrate` 建好 RediSearch 索引：:meth:`list` 是按 ``user_id`` 索引查的
        （不是扫全部键），索引没建起来时只能查到空。建索引是幂等的，失败只告警（见 :func:`migrate`）。
        """
        redis_client = RedisClient()
        cls.Meta.database = redis_client.client
        await migrate(redis_client.client)

    async def save(self, pipeline: Pipeline | None = None, nx: bool = False, xx: bool = False) -> "Task | None":
        """写入任务记录（整体覆盖），顺手续期到 :data:`TASK_TTL`——重写 RedisOM 的 save，让「写一次留 3 天」成为唯一入口。

        整体覆盖是刻意的：写入方只有任务的执行者（或投递任务的 API）一个，每次带上「到目前为止的全部
        进度」，读的一方（轮询接口）因此总能拿到完整的一份，也不会读到写了一半的记录；续期放在这里，
        调用方不必记着还能活多久，跑得久、写得勤的任务自然就留得久。

        ``created_at`` 只在第一次写入时补齐（没给就取当前时间）：记录的整体覆盖写不会把创建时间越写越新，
        排队顺序因此稳定；写进度的一方要么带头一次读到的值，要么交给这里按「首次写入」兜底。

        :param pipeline: 可选的 Redis 管道（与 RedisOM 的 save 一致，便于和别的写入批量提交）
        :param nx: 只在键不存在时写入（同 RedisOM）
        :param xx: 只在键已存在时写入（同 RedisOM）
        :return: 写入成功返回自己；``nx`` / ``xx`` 条件不满足时返回 ``None``（同 RedisOM，也顺带不续期）
        """
        if self.created_at is None:
            self.created_at = time()
        saved = await super().save(pipeline=pipeline, nx=nx, xx=xx)
        if saved is None:
            return None
        await self.expire(TASK_TTL, pipeline=pipeline)
        return self

    @classmethod
    async def list(cls, user_id: int = DEFAULT_USER_ID, limit: int = TASK_LIMIT) -> "list[Task]":
        """批量查询某个用户的任务记录，新的在前（编号倒序），最多 :data:`TASK_LIMIT` 条。

        :param user_id: 用户编号，缺省取 :data:`DEFAULT_USER_ID`（用户体系还没有，当前都是 1 号用户）
        :param limit: 取多少条（新的优先）——任务记录小、轮询又要看得见，故按条数截断而不是全量返回

        走的是 ``user_id`` 上的 RediSearch 索引（见 :meth:`setup`），按 ``id`` 倒序排（要 sortable，
        见字段定义）；过期的记录不在结果里（键已经没了）。
        """
        return await cls.find(cls.user_id == user_id).sort_by("-id").page(0, limit)

    @classmethod
    async def next(cls) -> "Task | None":
        """取排队中最靠前的一条任务：``status`` 为 pending 里 ``created_at`` 最小的那条。

        :return: 该出队执行的任务记录；队列空（没有 pending 的记录）时返回 None

        走 ``status`` 上的 RediSearch 索引 + ``created_at`` 的排序（两者都 ``sortable`` / ``index``），
        不必扫全部键。同一时刻只允许一个任务执行，出队方拿到记录后先把它置成 running 再开跑
        （见 :meth:`drain`），因此并发取到的永远是同一条。
        """
        records = await cls.find(cls.status == TaskStatus.PENDING).sort_by("created_at").page(0, 1)
        return records[0] if records else None

    @classmethod
    def enqueue(cls, record: "Task", execute: Callable[[], Awaitable[Any]]) -> None:
        """把一条已落库的 pending 任务挂到排队队列上，并保证有消费者在跑。

        :param record: 刚落库的任务记录（状态应为 ``pending``，创建时间已写好）
        :param execute: 执行体：一个无参协程工厂（调用即干活，执行方自己负责写进度与收尾状态）

        每次投递都起一个消费者（见 :meth:`drain`），已有消费者在跑时新起的那个会立刻返回——正在跑的那个
        每跑完一个就重新 :meth:`next` 取队头，因此随后投递的任务不会被漏掉，也不会两个同时跑。
        """
        cls.QUEUE[record.id] = (record.created_at or time(), execute)
        cls.CREATED[record.id] = cls.QUEUE[record.id][0]
        runner = asyncio.get_running_loop().create_task(cls.drain())
        cls.QUEUE_RUNNERS.add(runner)
        runner.add_done_callback(cls.QUEUE_RUNNERS.discard)

    @classmethod
    async def drain(cls) -> None:
        """按创建时间从早到晚逐个执行排队中的任务：一次只跑一个，跑完放下一个。

        队头由 :meth:`next` 从 Redis 里取（``created_at`` 最小的 pending 记录，不分任务类型），出队即置
        ``running`` 再执行，跑完再取下一张；取不到就退出，等下一次投递重新拉起消费者。

        出队的记录若在进程内找不到执行体（进程重启过），没有可跑的东西：直接落成失败并提示重新提交——
        留在 pending 里只会让后面所有任务永远排在它后面。
        """
        if cls.QUEUE_LOCK.locked():
            # 已有消费者在跑：它每轮都会重新取队头，刚投递的那条会被它取到，这里不必再起一个
            return
        async with cls.QUEUE_LOCK:
            while True:
                record = await cls.next()
                if record is None:
                    return
                queued = cls.QUEUE.get(record.id)
                if queued is None:
                    logger.warning(f"task: 任务 {record.id} 在排队中但执行体已丢失（进程重启？），置为失败")
                    await cls.fail(record.id, "服务重启，排队中的任务未能执行，请重新提交")
                    continue
                record.status = TaskStatus.RUNNING
                await record.save()
                logger.info(f"task: 开始执行任务 {record.id}（{record.type}，排队 "
                            f"{max(0.0, time() - queued[0]):.0f} 秒）")
                await queued[1]()
                cls.QUEUE.pop(record.id, None)
                cls.CREATED.pop(record.id, None)

    @classmethod
    async def created_at_of(cls, task_id: int) -> float:
        """取任务的创建时间（进程内缓存，读不到就用当前时间兜底）。

        :param task_id: 任务编号（雪花 ID）
        :return: Unix 秒

        记录是整体覆盖写的，写进度时要用它带上原来的创建时间（见 ``plugins.celery.tasks.record``），
        不然排队顺序与「排了多久」都随每帧刷新而失真。
        """
        if task_id not in cls.CREATED:
            try:
                cls.CREATED[task_id] = (await cls.get(task_id)).created_at or time()
            except NotFoundError:
                cls.CREATED[task_id] = time()
        return cls.CREATED[task_id]

    @classmethod
    async def fail(cls, task_id: int, error: str | BaseException) -> None:
        """把任务记为失败（重试用尽时调用，见 :func:`plugins.celery.tasks.create_dataset`）。

        记录里已经写下的类型、入参、进度帧与产出原样保留——跑到哪、错在哪都要留给轮询方看，故这里是读出来
        改状态、再整体写回，而不是新写一份。记录已经过期时（超过 :data:`TASK_TTL`）没什么可保留的，
        按空的一份写回去（其余列取字段默认值）：该编号的任务确实失败了，轮询方查得到这个结论。
        """
        try:
            task = await cls.get(task_id)
        except NotFoundError:
            task = cls(id=task_id)
        task.status = TaskStatus.ERROR
        task.error = str(error)
        await task.save()

    def dump(self) -> dict[str, Any]:
        """任务快照：编号、所属用户、类型、状态、创建时间、失败原因、入参与本轮输出（含进度帧）。

        即轮询接口的一条响应；``created_at`` 是 Unix 秒（排队顺序就是按它排的，前端可据此展示「排在第几」）。
        """
        return {"id": self.id, "user_id": self.user_id, "type": self.type, "status": self.status,
                "created_at": self.created_at, "error": self.error, "inputs": self.inputs,
                "outputs": self.outputs}


async def migrate(client: Redis | None = None) -> None:
    """按模型定义建立/更新 RediSearch 索引（由 :class:`RedisClient` 在启动时调用）。

    :param client: 用哪份连接迁移，缺省取常驻客户端（见 :class:`RedisClient`）；不再临时建连、也就不必关闭

    迁移是幂等的：RedisOM 把每个模型的索引 schema 哈希记在 Redis 中，只有
    索引缺失或 schema 变更时才重建（``DROP`` + ``CREATE``），因此可以放心地
    在每次启动时执行。

    Redis 未加载 RediSearch 模块时迁移会失败，这里捕获后只记录告警，不阻断
    启动：:class:`ShortTermMemory` 的读写走主键、用不上索引，:class:`Task`
    的按主键读写也一样——但 :meth:`Task.list`（按用户批量查任务）走的就是索引，
    索引没建起来时它只能查到空。

    Note:
        Redis 只允许在 0 号库创建检索索引：``config.REDIS_DB`` 非 0 时迁移
        必然失败（同样只告警），此时模型仍可正常读写。
    """
    connection = client if client is not None else RedisClient().client
    try:
        await Migrator(conn=connection).run()
        logger.info("Redis 模型迁移完成：索引已就绪")
    except Exception as exc:  # noqa: BLE001 - 迁移失败不应阻断应用启动
        logger.warning("Redis 模型迁移失败（仅影响索引检索，主键读写不受影响）: {}", exc)
