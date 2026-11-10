"""Celery：任务队列的 app 实例、worker 进程管理，以及 worker 进程内的日志与插件装配。

本包提供这些能力：

- :data:`celery_client`：Celery 应用实例。broker 与结果后端复用 :mod:`plugins.redis` 的 Redis
  连接配置（地址与口令来自 ``config.REDIS_*``，库号取 ``config.CELERY_BROKER_DB`` /
  ``CELERY_RESULT_DB``），与检索索引、短期记忆缓存共用一个 Redis 实例。任务定义放在
  :mod:`plugins.celery.tasks`，由 ``include`` 引入、worker 启动时加载；应用内投递任务用
  :func:`submit_task`（它顺手把任务记录写进 Redis），定时任务的调度表（``beat_schedule``）挂在
  :data:`celery_client` 上，周期取自 ``config.CELERY_SCAN_INTERVAL``。
- :class:`CeleryWorker`：跟随应用生命周期的插件（见 :class:`plugins.Plugin`），启动时用当前
  解释器拉起一个 worker 子进程（``python -m celery -A plugins.celery worker``，默认带 ``-B`` 内嵌
  beat；Windows 上 celery 不接受 ``-B``，那里要单独跑 ``celery beat``），关闭时先发终止信号、
  超时再强杀。
- worker 进程内的装配（走 celery 的信号，见文件末尾）：``setup_logging`` 信号里由 loguru
  接管 celery 自己的日志，单独写进 ``<日期>.celery`` 日志文件；``worker_process_init`` 信号里
  注册应用插件（数据库、向量库、模型等），并开一个常驻的事件循环（任务的异步依赖都跑在它上面，
  出题任务就是其中之一，见 :mod:`plugins.celery.tasks`）。

Note:
    Celery 目前**停用**（``config.CELERY_ENABLED`` 为 False）：:class:`CeleryWorker` 不拉起 worker，
    :func:`submit_task` 也不往 broker 投递，后台任务改由 API 进程内的 asyncio 任务直接跑
    （见 ``plugins.celery.tasks.run_locally``）——进度帧与任务记录照旧，调用方轮询无感。恢复时把开关
    打开、重启应用即可，任务定义不用改。

Note:
    worker 是**独立进程**，跑的不是这个 FastAPI 应用，因此上面第二组的装配只发生在 worker 进程里。
    同步任务要访问异步依赖时用 :func:`run`（它复用 worker 里注册好的那套连接），别在任务里另起
    ``asyncio.run``。另外每个应用进程各拉起一个 worker，多副本部署时更该把 worker 单独部署
    （用同一份 broker 配置即可），并把 ``CELERY_BEAT`` 关掉、只让其中一个实例单独跑 celery beat。
"""

import subprocess
import sys
from asyncio import AbstractEventLoop, set_event_loop, sleep, to_thread
from functools import partial
from os import getpid
from pathlib import Path
from typing import Any, Coroutine

from celery import Celery
from celery.signals import setup_logging, worker_process_init
from loguru import logger

from config import CELERY_BEAT, CELERY_BEAT_SCHEDULE_FILE, CELERY_BROKER_DB, CELERY_CONCURRENCY, CELERY_ENABLED, \
    CELERY_LOGLEVEL, CELERY_POOL, CELERY_RESULT_DB, CELERY_SCAN_ENABLED, CELERY_SCAN_INTERVAL
from logger import setup_celery_logger
from models import Model
from plugins import Plugin, register_plugins
from plugins.redis import Task, TaskType, build_redis_url
from plugins.snowflake import Snowflake

# 项目根目录：worker 在该目录下启动，`python -m celery` 才能 import 到 plugins 包
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# `celery -A` 的模块路径：即本包。celery 不认「实例叫什么名字」，它导入模块后自己找 Celery
# 实例（见 celery.app.utils.find_app），本模块的实例叫 celery_client
APP_MODULE = "plugins.celery"
# worker 的进程池：Windows 上默认的 prefork 跑不稳，改用 solo（单进程串行执行任务）
DEFAULT_POOL = "solo" if sys.platform == "win32" else "prefork"
# 启动后等一小会儿确认 worker 没有立刻退出（导入失败、参数写错时会立刻退出）
STARTUP_PROBE_SECONDS = 1.0
# 关闭时等待 worker 退出的上限：超过则强杀
STOP_TIMEOUT = 10.0

# 本进程就是 Celery worker 时置位（见 register_worker_process）：worker 里注册插件时不能再拉起
# worker，否则会层层套娃
IN_WORKER = False
# worker 进程里常驻的事件循环：数据库、模型、向量库的连接都绑在它上面，任务之间复用同一套连接
worker_loop: AbstractEventLoop | None = None

# 任务与结果都用 JSON：消息体可读、跨语言，也不必在 broker 里传 pickle。
# broker 与结果后端复用 [tool.redis] 的连接配置（见 plugins.redis.build_redis_url），只另指定库号
celery_client = Celery(
    "celery",
    broker=build_redis_url(CELERY_BROKER_DB),
    backend=build_redis_url(CELERY_RESULT_DB),
    include=["plugins.celery.tasks"]
)
celery_client.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    # 定时任务表：beat 按 CELERY_SCAN_INTERVAL 秒周期投递一次表结构巡检（任务见 tasks.scan_tables）。
    # 挂在这里而不是写进命令行，`--beat` 内嵌与单独跑 `celery beat` 都用同一份调度表。
    # 巡检当前**关停**（config.CELERY_SCAN_ENABLED 为 False）：调度表为空，beat 不投递任何周期任务；
    # 恢复时把开关打开、重启 worker / beat 即可，任务定义本身不用改（见 tasks.scan_tables 的守卫）
    beat_schedule={
        "scan-tables": {
            "task": "celery.scan_tables",
            "schedule": CELERY_SCAN_INTERVAL
        }
    } if CELERY_SCAN_ENABLED else {},
    # beat 的调度状态文件（PersistentScheduler 的 shelve 库）：worker 与 beat 的 cwd 都是项目根目录，
    # 故按 config.CELERY_BEAT_SCHEDULE_FILE 落到 logs 下，不在根目录留 celerybeat-schedule
    beat_schedule_filename=CELERY_BEAT_SCHEDULE_FILE
)


async def submit_task(task_type: TaskType, inputs: dict[str, Any], name: str, args: list) -> int:
    """登记一条任务记录并把任务投进队列，返回任务编号——后台任务的投递都走它（见各 controller）。

    :param task_type: 任务类型（见 :class:`plugins.redis.TaskType`）：写进任务记录的 ``type``
    :param inputs: 投递时的入参：写进任务记录的 ``inputs``——任务还没开跑时轮询就能看到它是
        什么任务、带了什么参数
    :param name: celery 的任务名（如 ``celery.collect_database``）：worker 按名分发到对应任务
    :param args: 任务自己的位置参数（任务编号由这里生成、自动放在最前，见 :mod:`plugins.celery.tasks`）
    :return: 任务编号（雪花 ID）：调用方随响应返回给前端，前端拿它轮询任务进度
        （见 ``controllers.evaluation.list_tasks``）

    任务记录先落库、再投递：任务还在排队时，轮询就能看到它是哪个任务（类型、入参、状态 ``pending``、
    创建时间齐）。celery 侧的任务编号（task_id）也用同一个编号，日志、队列与任务记录对得上。

    **同一时刻只跑一个任务**（不分类型）：投递时记录落成 ``pending`` 并带上创建时间，执行方按创建时间
    从早到晚出队。排队这件事由任务记录自己管（见 ``plugins.redis.Task`` 的 ``enqueue`` / ``drain``），
    与执行方式无关：停用 Celery 时（``config.CELERY_ENABLED`` 为 False）执行体是本进程的
    :func:`plugins.celery.tasks.run_locally`；启动 Celery 时交给 broker，串行由 worker 的并发度保证
    （``--concurrency 1`` / solo 池）。两种情况下进度帧与任务记录都一致，调用方照旧轮询任务接口。
    """
    task_id = Snowflake.get_id()
    # 创建时间在这里定一次（见 plugins.redis.Task.created_at）：排队顺序就按它，后面的写进度不会改它
    record = await Task(id=task_id, type=task_type, inputs=inputs, outputs={"steps": []}).save()
    if not CELERY_ENABLED:
        # 本地导入：tasks 反过来要 import 本模块的 celery_client，模块级导入会成环
        from plugins.celery.tasks import run_locally
        # 排队与「一次只跑一个」由任务记录自己管（见 plugins.redis.Task.enqueue / drain）：
        # 这里只交出「怎么执行」——任务名 + 位置参数
        Task.enqueue(record, partial(run_locally, name, [task_id, *args]))
        return task_id
    celery_client.send_task(name, args=[task_id, *args], task_id=str(task_id))
    return task_id


def build_worker_command() -> list[str]:
    """拉起 worker 的命令。

    用当前解释器跑 ``python -m celery``（而不是 PATH 上的 celery 脚本），保证用的是本项目
    的虚拟环境；日志级别、并发数与进程池取自 :mod:`config`。worker 的输出继承父进程的
    stdout / stderr，文件出口则由 worker 进程内的 loguru 装配负责（见 :func:`setup_worker_logging`）。

    ``-B`` 按 ``config.CELERY_BEAT`` 决定：单实例部署时内嵌 beat 一起跑，多实例时关掉它、
    只留一个实例单独跑 ``celery beat``，否则每个实例都会按周期投递一遍定时任务。

    Windows 上不带 ``-B``：celery 会在参数校验阶段直接报
    ``-B option does not work on Windows. Please run celery beat as a separate service``，
    worker 根本起不来，故那条路只能在别的平台走（Windows 上要跑定时任务就单独起 ``celery beat``，
    见 register 里的告警）。
    """
    command = [sys.executable, "-m", "celery", "-A", APP_MODULE, "worker",
               "--loglevel", CELERY_LOGLEVEL, "--pool", CELERY_POOL or DEFAULT_POOL]
    if CELERY_BEAT and sys.platform != "win32":
        command.append("-B")
    if CELERY_CONCURRENCY:
        command += ["--concurrency", str(CELERY_CONCURRENCY)]
    return command


class CeleryWorker(Plugin):
    """随应用生命周期管理的 Celery worker 子进程。

    被 :class:`plugins.PluginMeta` 在类定义时自动登记，由 ``register_plugins`` /
    ``close_plugins`` 在应用启动与关闭时调用（见 app.py 的 lifespan 与那里的
    ``import plugins.celery``——不导入这个包，插件不会被登记）。
    """

    def __init__(self):
        self.process: subprocess.Popen | None = None

    async def register(self) -> None:
        """拉起 worker 子进程，并确认它没有立刻退出。

        停用 Celery 时（``config.CELERY_ENABLED`` 为 False）什么都不做：没有 worker，后台任务改由
        API 进程内的 asyncio 任务直接跑（见 :func:`submit_task`）。
        """
        if not CELERY_ENABLED:
            logger.warning("celery: 已停用（config.CELERY_ENABLED 为 False），不拉起 worker；"
                           "后台任务改由本进程内的 asyncio 任务执行")
            return

        if IN_WORKER:
            # 本进程就是 worker（见 register_worker_process）：注册插件时跳过拉起 worker 这一步
            logger.info("celery: in worker process, skip starting another worker")
            return

        if self.process is not None and self.process.poll() is None:
            logger.info("celery: worker already running")
            return

        command = build_worker_command()
        logger.info(f"celery: starting worker -> {' '.join(command)}")
        process = subprocess.Popen(command, cwd=PROJECT_ROOT)
        self.process = process
        await sleep(STARTUP_PROBE_SECONDS)

        # worker 起不来时不阻断应用启动：API 仍可服务，投递的任务只是排在队列里等 worker
        if process.poll() is not None:
            logger.error(f"celery: worker exited immediately with code {process.returncode}")
        else:
            logger.info(f"celery: worker started (pid={process.pid})")
            if CELERY_BEAT and sys.platform == "win32":
                # 这条路上 worker 没带 -B（celery 在 Windows 上拒绝内嵌 beat），定时任务得单独跑；
                # 明着提示一句，免得定时任务静悄悄地永远不跑
                logger.warning("celery: Windows 不支持内嵌 beat，定时任务需单独运行："
                               f"{sys.executable} -m celery -A {APP_MODULE} beat "
                               f"--loglevel {CELERY_LOGLEVEL}")

    async def close(self) -> None:
        """停掉 worker：先发终止信号让它收尾，超时未退出再强杀。

        Windows 上 terminate 是直接结束进程（收不到 celery 的优雅退出），只在应用关闭时
        触发，队列里未消费的任务会留到下次启动再执行。
        """
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return

        logger.info(f"celery: stopping worker (pid={process.pid})")
        process.terminate()
        try:
            await to_thread(process.wait, STOP_TIMEOUT)
            logger.info("celery: worker stopped")
        except subprocess.TimeoutExpired:
            logger.warning("celery: worker did not exit in time, killing it")
            process.kill()
            await to_thread(process.wait)


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    """在 worker 常驻的事件循环里执行协程：同步任务访问异步依赖（数据库、向量库、模型）的入口。

    例：``@celery_app.task def collect(...): return run(vectorize(...))``。这些依赖的连接都绑在
    :data:`worker_loop` 上，任务之间复用同一套连接池；因此不要在任务里另起 ``asyncio.run``——
    那会新建连接池，与注册时建立的实例脱钩。
    """
    if worker_loop is None:
        raise RuntimeError("worker 事件循环尚未初始化：run 只能在 worker 进程的任务里使用")
    return worker_loop.run_until_complete(coro)


async def bootstrap_worker() -> None:
    """worker 进程启动时要做的初始化：注册应用插件，与 app.py 的 lifespan 同一套。

    worker 进程没有 app.py 的导入链，带插件的模块要先导入才会被 :class:`plugins.PluginMeta`
    登记，故这里显式导入（CeleryWorker 由本模块登记）；登记齐了再交给 ``register_plugins``
    逐个 register，任务里就能直接用这些依赖。

    Redis 常驻客户端（任务记录与 RediSearch 索引，见 :class:`plugins.redis.RedisClient`）也在这里
    随插件注册备好（见 ``worker_process_init`` 信号，每个执行进程一次）：连接与索引都不便宜，任务里再
    临时初始化一遍只会白白拖慢每一轮出题，故留到这里、之后由单例常驻复用。
    """
    # 显式导入带插件的模块：不导入就不会登记
    from agnets.rerank import RerankClient  # noqa: F401
    from plugins.qdrant import Qdrant  # noqa: F401
    from plugins.redis import RedisClient  # noqa: F401

    await Model.init()
    await register_plugins()


def setup_worker_process() -> None:
    """装配 worker 进程的日志与插件（同步回调，内部起常驻事件循环跑异步初始化）。"""
    global IN_WORKER, worker_loop
    IN_WORKER = True
    setup_celery_logger()

    if worker_loop is None:
        # 事件循环按 checkpoint 组件的口径挑（见 agnets.checkpointers.loop_factory）：
        # 会话记忆要开 checkpoint 连接池，而 psycopg 的异步模式在 Windows 上不支持默认的 ProactorEventLoop
        from agnets.checkpointers import loop_factory
        worker_loop = loop_factory()
        set_event_loop(worker_loop)
    try:
        worker_loop.run_until_complete(bootstrap_worker())
    except Exception:  # noqa: BLE001 - 注册失败不阻断 worker 启动，任务用到时再报错
        logger.exception("celery: 插件注册失败，任务里用到数据库 / 向量库时会报错")
    else:
        logger.info(f"celery: plugins registered in worker process {getpid()}")


@setup_logging.connect
def setup_worker_logging(**kwargs: Any) -> None:
    """接管 celery 自己的日志装配，让 worker 的日志走 loguru。

    celery 在装配日志前会发 ``setup_logging`` 信号，只要它有接收者就跳过自己的 handler 配置
    （见 ``celery.app.log.Logging.setup_logging_subsystem``）；这里接了它，worker 的启动信息、
    任务日志（含 celery 内部 logger）就都经 loguru 落到 ``<日期>.celery`` 日志文件里。
    """
    setup_celery_logger()


@worker_process_init.connect
def register_worker_process(**kwargs: Any) -> None:
    """worker 进程启动时（每个执行任务的进程各一次）装配日志并注册插件。

    solo 与 prefork 都会发这个信号（见 ``celery.concurrency.solo`` 与 ``celery.concurrency.prefork``），
    且在 prefork 下由每个子进程各发一次——连接因此不会被 fork 继承，每个执行进程各有一套。
    """
    setup_worker_process()
