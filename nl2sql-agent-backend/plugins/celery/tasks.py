"""Celery 任务定义。

由 :data:`plugins.celery.celery_client` 的 ``include`` 引入，worker 启动时加载。任务跑在
worker 子进程里（不是 API 进程）：celery 的任务是同步的，要访问异步依赖（数据库、向量库、
模型）就用 :func:`plugins.celery.run` 把协程丢进 worker 常驻的事件循环——worker 启动时已经
注册好插件（见 ``worker_process_init`` 信号），连接可以直接复用。

后台任务（``celery.collect_database`` 整库采集、``celery.create_dataset`` 出题、
``celery.evaluate_dataset`` 评测）由 API 投递（见 ``controllers.collection`` 与 ``controllers.evaluation``）：
进度实时写进 Redis 的任务记录（见 :class:`plugins.redis.Task`），调用方轮询任务接口拿
（见 ``controllers.evaluation.list_tasks``）——不占 Celery 的结果后端，也不必把进度塞进任务状态。
定时任务（``celery.scan_tables``）由 beat 按 ``config.CELERY_SCAN_INTERVAL`` 周期投递，
调度表挂在应用上（见 :data:`plugins.celery.celery_client`）：它比对业务库与元数据库的登记，
已移除的表清掉向量与登记，新增的表直接采集，改动的表先删旧登记与旧向量再整表重采，
``Column.values`` 不足的字段单独补采取值（见 :func:`scan_tables`）。
巡检目前**关停**（``config.CELERY_SCAN_ENABLED`` 为 False）：调度表为空、任务本身也直接返回，
恢复时把开关打开并重启 worker / beat 即可。

Celery 目前也整体**停用**（``config.CELERY_ENABLED`` 为 False）：不拉起 worker、也不向 broker 投递，
上面三个后台任务改由 API 进程直接跑 :func:`run_locally`（worker 里那套 ``@celery_client.task`` 定义原样
留着）；恢复时把开关打开、重启应用即可。
"""

from dataclasses import asdict
from typing import Any, AsyncGenerator, Coroutine

from celery.app.task import Task
from loguru import logger
from tortoise.transactions import in_transaction

from config import CELERY_BROKER_DB, CELERY_SCAN_ENABLED
from models import VALUE_LIMIT, get_column_values, get_table_infos, get_table_names, is_value_type, \
    register_connection
from models.dataset import Result
from models.meta import Column, ColumnRoleOption, Database, Table
from agnets.collection import VALUE_BATCH_SIZE, discover_tables, embed_columns
from agnets.evaluation import evaluate_dataset as evaluate_dataset_stream, \
    generate_dataset as generate_dataset_stream
from agnets.schemas import Step, StepStatus
from plugins.celery import celery_client, run
from plugins.qdrant import Qdrant, Value
from plugins.redis import Task as TaskRecord, TaskStatus, TaskType, lock

# 向量库封装：与采集流程 / 检索图共用同一个实例（Qdrant 是插件单例，见 plugins.qdrant），
# 巡检里清已移除表的字段点与取值点要用它（见 purge_tables）
qdrant = Qdrant()

# 巡检互斥锁（见 plugins.redis.lock）：一轮同时只跑一个。锁落在 broker 的库上，
# TTL 是兜底——持有者被强杀时不会有人释放锁，靠它自己过期，免得后续巡检一直跳过
SCAN_LOCK_KEY = "celery:scan_tables:lock"
SCAN_LOCK_TTL = 3600
# 取值样本少于该条数的字段视为「取值没采全」：巡检会为这些字段补采一次取值（见 scan_database / collect_values）。
# 样本存在 Column.values 里，由采集流程（含补采本流程）写回，故这个数字是「库里已有几条」而不是「业务库里有几条」
MIN_VALUES = 3
# 元数据库连接名（见 config.TORTOISE_ORM_CONFIG）：清掉已移除表的登记时，字段与表放在一个事务里删
META_CONNECTION = "meta"

# 后台任务的重试次数与间隔（秒）：worker 出了意外（连不上库、进程被重启）时等一会儿重跑，
# 任务本身的业务失败（表已不存在、SQL 生成失败）不重试——同样的输入重跑还是同样的结果
TASK_RETRIES = 3
TASK_RETRY_DELAY = 60

@celery_client.task(bind=True, name="celery.collect_database")
def collect_database(self: Task, task_id: int, database_id: int) -> dict[str, Any]:
    """后台任务：把一个业务库整库采集进元数据库与向量库（登记表结构、分析字段、向量化、采集取值）。

    :param self: 本任务实例（``bind=True``）：已重试次数（``self.request.retries``）与重试入口
        （``self.retry``）都从它取
    :param task_id: 任务编号（雪花 ID）：任务记录（:class:`plugins.redis.Task`）按它归属，进度就写在
        这条记录上，调用方拿它轮询（见 controllers.evaluation.list_tasks）；记录本身由 API 在投递之前
        写好（类型、入参写在 meta 里），worker 沿用同一份入参、只往上追加进度帧与产出
    :param database_id: 要采集的业务库编号（Database.id）：整库采集，不挑表（见
        :func:`collect_tables`）
    :return: ``{"steps": [...], "dataset_id": ..., "outputs": {...}, "error": ...}``：本轮的全部进度帧、
        产出（库编号与采集标记，见 :func:`run_collection`）与失败原因

    采集成功才把 Database.collected 置位（登记就此锁死，见 :func:`run_collection`）；采集失败时库还能改，
    改完再采一次即可。意外失败（连不上库之类的）重试 :data:`TASK_RETRIES` 次，仍不成才把记录落成 error
    （见 :func:`run_with_retry`）。
    """
    return run_with_retry(self, task_id, run_collection(task_id, database_id))


async def collect_tables(database_id: int, table_ids: list[str] | None = None) -> AsyncGenerator[Any, Any]:
    """依次跑两个采集工具，把两条进度流拼成一条。

    :param database_id: 业务库编号（Database.id）
    :param table_ids: 本轮要采的表编号（Table.id，留空表示本库全部表），两步用同一份范围
    :return: 先「分析表结构」（discover_tables），再「向量化字段与取值」（embed_columns）的进度帧

    两步各自是独立的工具函数（见 plugins.agents.collection）：前者写元数据库，后者重写向量库与取值，
    可以分开调用（脚本里只补向量时就只跑后者）。模型撞上意外异常时 discover_tables 自己收尾（已落库的表
    照样往下向量化，重写是幂等的），故这里一前一后串起来即可。
    """
    async for step in discover_tables(database_id, table_ids):
        yield step
    async for step in embed_columns(database_id, table_ids):
        yield step


async def run_collection(task_id: int, database_id: int) -> dict[str, Any]:
    """在 worker 的常驻事件循环里整库采集一轮，逐帧把进度写进任务记录，返回本轮汇总。

    采集前先补登记该库的连接：worker 是独立进程、只在启动时登记过一批业务库（见
    :func:`plugins.celery.bootstrap_worker`），启动之后新登记的库它还不知道；连接是按别名惰性创建的，
    补登记一次即可（见 :func:`models.register_connection`）。

    产出是采集标记：整轮跑完（没有失败帧）就把 ``Database.collected`` 置位——登记就此锁死、不允许再改，
    这是「采集成功才算数」的口径：采集失败时库还能改，改完再采一次。库在任务排队期间被删掉时直接把
    任务记成失败（不必重试，重跑也是一样），不跑采集。
    """
    inputs = {"database_id": database_id}
    database = await Database.get_or_none(id=database_id)
    if database is None:
        error = f"业务库 {database_id} 已不存在，无需采集"
        await record(task_id, TaskType.COLLECT_DATABASE, inputs, [], {},
                     status=TaskStatus.ERROR, error=error)
        return {"steps": [], "dataset_id": None, "outputs": {}, "error": error}
    await register_connection(database.id, database.url)
    summary = await run_graph(task_id, TaskType.COLLECT_DATABASE, inputs, collect_tables(database_id))
    if summary["error"] is None:
        await Database.filter(id=database_id).update(collected=True)
        summary["outputs"].update({"database_id": database_id, "collected": True})
    await finish(task_id, TaskType.COLLECT_DATABASE, inputs, summary)
    return summary


@celery_client.task(bind=True, name="celery.create_dataset")
def create_dataset(self: Task, task_id: int, thread_id: int, table_ids: list[str], size: int,
                   database_id: int) -> dict[str, Any]:
    """后台任务：按指定的表跑一轮出题（生成问题与 SQL → 校验纠正 → 落库数据集），逐帧上报进度。

    :param self: 本任务实例（``bind=True``）：已重试次数（``self.request.retries``）与重试入口
        （``self.retry``）都从它取
    :param task_id: 任务编号（雪花 ID）：任务记录（:class:`plugins.redis.Task`）按它归属，进度就写在
        这条记录上，调用方拿它轮询（见 controllers.evaluation.list_tasks）；记录本身由 API 在投递
        之前写好（类型、入参写在 meta 里），worker 沿用同一份入参、只往上追加进度帧与产出
    :param thread_id: 会话编号（雪花 ID，由 API 生成，见 controllers.evaluation.create_dataset）：只作为
        来源标记记在数据集上（Dataset.thread_id），评测按数据集编号取内容，不再依赖它恢复状态
    :param table_ids: 出题所用的表编号（Table.id，形如 <库编号>.<表名>）：API 侧已校验存在并按编号排好序
    :param size: 生成的问题数量
    :param database_id: 本次出题所用业务库编号（Database.id）：表信息与 SQL 校验都连它
    :return: ``{"steps": [...], "dataset_id": ..., "outputs": {...}, "error": ...}``：本轮的全部进度帧
        （Step 的字典形态）、落库的数据集编号、产出与失败原因

    出题失败不抛异常：原因在返回值的 error 里（进度帧本身也带），任务记录随即落 error 状态——
    调用方只看记录，不必去看 Celery 的任务状态。进度每帧一次、整体覆盖写进记录（见
    :func:`generate_dataset`），因此轮询随时能看到跑到哪了。意外失败（连不上库之类的）重试
    :data:`TASK_RETRIES` 次，仍不成才把记录落成 error（见 :func:`run_with_retry`）。
    """
    return run_with_retry(self, task_id, generate_dataset(task_id, thread_id, table_ids, size, database_id))


@celery_client.task(bind=True, name="celery.evaluate_dataset")
def evaluate_dataset(self: Task, task_id: int, dataset_id: int, thread_id: int, database_id: int) -> dict[str, Any]:
    """后台任务：评测一份已落库的数据集并落一条结果，逐帧上报进度。

    :param self: 本任务实例（``bind=True``）：与出题任务同理，重试从它取
    :param task_id: 任务编号（雪花 ID）：与出题任务同一条约定，进度写在 ``Task`` 记录里（类型为
        :attr:`plugins.redis.TaskType.EVALUATE_DATASET`）
    :param dataset_id: 被评测的数据集编号（Dataset.id）：问题、参考 SQL 与库上下文都从它取
        （见 plugins.agents.evaluation.nodes.evaluate_dataset），结果挂在它下面（Dataset.results），
        评完取该数据集最新的一条结果作为本轮的产出（见 :func:`run_evaluation`）
    :param thread_id: 数据集的来源标记（Dataset.thread_id）：只写进任务入参便于追溯，评测本身用不到
    :param database_id: 本次评测的业务库编号（Database.id）：检索智能体在该库上答题
    :return: ``{"steps": [...], "dataset_id": ..., "outputs": {...}, "error": ...}``：本轮的全部进度帧、
        被评测的数据集编号、产出（结果编号与平均分）与失败原因

    同一个数据集可以反复评测，每投递一次就是一个独立的任务（各跑一轮、各落一条结果）。
    失败与重试的处置与出题任务完全一致，都交给 :func:`run_with_retry`。
    """
    return run_with_retry(self, task_id, run_evaluation(task_id, dataset_id, thread_id, database_id))


def run_with_retry(task: Task, task_id: int, coroutine: Coroutine[Any, Any, dict[str, Any]]) -> dict[str, Any]:
    """在 worker 的常驻事件循环里跑任务的协程，意外失败就交给 Celery 重试（两个后台任务共用）。

    :param task: 当前的 Celery 任务实例（``bind=True``）：重试次数取自 ``task.request.retries``，
        重试入口是 ``task.retry``
    :param task_id: 任务编号：重试用尽时把任务记录落成 error（见 :meth:`plugins.redis.Task.fail`）
    :param coroutine: 真正干活的协程（列出进度帧、写任务记录、返回本轮汇总）
    :return: 协程的返回值：``{"steps": [...], "dataset_id": ..., "error": ...}``

    重试期间任务记录保持 running（协程只在跑完时才写收尾那一笔，见 :func:`run_graph`）：重跑会从第一帧
    起重新覆盖进度，调用方看到的是一次「又动起来了」，而不是一次假的失败。重试用尽仍失败时，原因落进
    任务记录再抛出，让 Celery 也记一笔 FAILURE 便于排查。
    """
    try:
        return run(coroutine)
    except Exception as exception:
        if task.request.retries >= TASK_RETRIES:
            logger.exception(f"celery: 任务 {task_id} 重试 {TASK_RETRIES} 次仍失败")
            run(TaskRecord.fail(task_id, exception))
            raise
        logger.warning(f"celery: 任务 {task_id} 第 {task.request.retries + 1} 次重试，"
                       f"{TASK_RETRY_DELAY} 秒后重跑：{exception}")
        raise task.retry(exc=exception, countdown=TASK_RETRY_DELAY)


async def generate_dataset(task_id: int, thread_id: int, table_ids: list[str], size: int,
                           database_id: int) -> dict[str, Any]:
    """在 worker 的常驻事件循环里跑一轮出题，逐帧把进度写进任务记录，返回本轮汇总。

    出题就是一个工具函数（见 plugins.agents.evaluation.nodes.generate_dataset），没有图与会话状态：
    问题、参考 SQL 与库上下文都由它自己落库。

    产出就是落库的数据集编号：进度帧一带上它（「数据集已保存」那帧），就写进 ``outputs``，
    调用方不必等到任务收尾（见 :func:`run_graph`）。
    """
    inputs = {"thread_id": thread_id, "table_ids": table_ids, "size": size, "database_id": database_id}
    summary = await run_graph(task_id, TaskType.CREATE_DATASET, inputs,
                              generate_dataset_stream(database_id, table_ids, size, thread_id))
    await finish(task_id, TaskType.CREATE_DATASET, inputs, summary)
    return summary


async def run_evaluation(task_id: int, dataset_id: int, thread_id: int, database_id: int) -> dict[str, Any]:
    """在 worker 的常驻事件循环里跑一轮评测，逐帧把进度写进任务记录，返回本轮汇总。

    评测同样是一个工具函数（见 plugins.agents.evaluation.nodes.evaluate_dataset）：问题、参考 SQL 与
    出题所用的表都从数据集（Dataset / DatasetItem）里取，不依赖任何会话状态。

    产出是这一轮评测的结果：逐条判定与平均分由评测工具落库，这里取该数据集最新的一条结果作为本轮产出
    （同一个数据集反复评测各落一条、按编号递增，跑完的这条就是最新的）。评测没跑到落库（失败）时
    没有产出，只留失败原因。
    """
    inputs = {"dataset_id": dataset_id, "thread_id": thread_id, "database_id": database_id}
    summary = await run_graph(task_id, TaskType.EVALUATE_DATASET, inputs,
                              evaluate_dataset_stream(dataset_id, database_id))
    if summary["error"] is None and summary["dataset_id"]:
        # 产出要在收尾那一笔之前补齐：结果编号与平均分就是这一轮的产出（见 finish）
        result = await Result.filter(dataset_id=summary["dataset_id"]).order_by("-id").first()
        if result is not None:
            summary["outputs"].update({"result_id": result.id, "mean": result.mean})
    await finish(task_id, TaskType.EVALUATE_DATASET, inputs, summary)
    return summary


async def run_graph(task_id: int, task_type: TaskType, inputs: dict[str, Any],
                    stream: AsyncGenerator[Step, Any]) -> dict[str, Any]:
    """消费图的进度流：逐帧把进度写进任务记录，返回本轮汇总（三个后台任务共用）。

    :param task_id: 任务编号（见 :class:`plugins.redis.Task`）
    :param task_type: 任务类型（见 :class:`plugins.redis.TaskType`）：写进 ``type``
    :param inputs: 投递时的入参，写进 ``inputs``
    :param stream: 进度流（采集工具或评估工具的产出，帧见 agnets.schemas.Step）
    :return: ``{"steps": [...], "dataset_id": ..., "outputs": {...}, "error": ...}``

    每收到一帧就整体覆盖写一次记录（类型 + 入参 + 到目前为止的全部帧 + 已经拿到的产出，帧落在
    ``outputs.steps`` 里，见 :func:`record`）：帧只有十几条、体积很小，整体写换来的是调用方一次轮询就能
    拿到完整过程，不必自己拼接。收尾那一笔（状态与补齐的产出）
    由调用方来写（见 :func:`finish`）：评测的产出要等结果落库后才能取到，只有调用方知道还要补什么。

    帧里的 dataset_id 只有出题 / 评测会带（采集的帧没有这一栏，见 agnets.schemas.Step），
    故按「有没有这一栏」取：一旦出现（数据集落库那帧）就写进产出，调用方随时能对上数据集。
    """
    steps = []
    dataset_id, error = None, None
    outputs = {}
    async for step in stream:
        frame = asdict(step)
        steps.append(frame)
        if frame.get("dataset_id"):
            # 数据集编号一出（出题落库那帧）就写进产出，调用方随时能对上数据集
            dataset_id = frame["dataset_id"]
            outputs["dataset_id"] = dataset_id
        if frame["status"] == StepStatus.FAILED:
            error = frame["error"]
        await record(task_id, task_type, inputs, steps, outputs)
    logger.info(f"celery: 任务 {task_id}（{task_type}）跑完，产出 {outputs or None}，失败原因 {error}")
    return {"steps": steps, "dataset_id": dataset_id, "outputs": outputs, "error": error}


async def finish(task_id: int, task_type: TaskType, inputs: dict[str, Any],
                 summary: dict[str, Any]) -> None:
    """收尾那一笔：按本轮成败把任务记录定成 success / error，并把补齐后的产出写进去（见 :func:`record`）。

    业务失败（出题失败、评测失败）也是「跑完了」，故这里落 error 状态而不是留着 running：调用方看
    ``status`` 就知道要不要接着轮询。真正的意外失败不写在这里——重试期间记录停在 running，
    重试用尽才由 :func:`run_with_retry` 落 error。
    """
    error = summary["error"]
    await record(task_id, task_type, inputs, summary["steps"], summary["outputs"],
                 status=TaskStatus.ERROR if error else TaskStatus.SUCCESS, error=error)


async def record(task_id: int, task_type: TaskType, inputs: dict[str, Any], steps: list[dict[str, Any]],
                 outputs: dict[str, Any], status: TaskStatus = TaskStatus.RUNNING,
                 error: str | None = None) -> None:
    """写一条任务记录（整体覆盖）：类型、入参、进度帧与产出（见 :meth:`plugins.redis.Task.save`）。

    进度帧也属于本轮输出，统一放进 ``outputs.steps``（不单独占一列）：调用方一次轮询就同时拿到「跑到哪」
    与「拿到了什么」，两者读的是同一个对象。其余产出（``dataset_id``、``result_id`` 与 ``mean`` 等）与它同层。

    ``created_at`` 原样带上（见 :meth:`plugins.redis.Task.created_at_of`）：记录是整体覆盖写的，不带上就
    等于每帧刷新一次创建时间，排队顺序与「排了多久」都失真。
    """
    await TaskRecord(id=task_id, type=task_type, inputs=inputs, outputs={**outputs, "steps": steps},
                     status=status, error=error,
                     created_at=await TaskRecord.created_at_of(task_id)).save()


# 停用 Celery 时在本进程跑的后台任务：celery 任务名 → 真正干活的协程（见 :func:`run_locally`）。
# 入参口径与投递一致：第一位是任务编号，其余是任务自己的参数（见 :func:`plugins.celery.submit_task`）
LOCAL_TASKS = {
    "celery.collect_database": run_collection,
    "celery.create_dataset": generate_dataset,
    "celery.evaluate_dataset": run_evaluation
}


async def run_locally(name: str, args: list) -> dict[str, Any]:
    """在本进程（API 进程的事件循环）里跑一个后台任务，进度照旧写进任务记录。

    :param name: celery 的任务名（``celery.collect_database`` 等，即 :data:`LOCAL_TASKS` 的键）
    :param args: 任务的位置参数：第一位是任务编号（雪花 ID），其余是任务自己的参数
    :return: 任务的汇总（``{"steps": [...], "dataset_id": ..., "outputs": {...}, "error": ...}``）

    由 :func:`plugins.celery.submit_task` 在 ``config.CELERY_ENABLED`` 为 False 时挂到事件循环上：任务记录、
    进度帧与产出和 worker 那条路完全一致，调用方照旧轮询任务接口，前端无感。

    没有 worker 也就没有 Celery 的重试：意外失败只记一条带栈的日志、把记录落成 error 就收尾，调用方看到的
    是失败而不是一直 running（业务失败本来就走 error 帧，见 :func:`finish`，不在这里）。
    """
    try:
        return await LOCAL_TASKS[name](*args)
    except Exception as exception:  # noqa: BLE001 - 后台任务没人接它的异常，落进日志与记录才看得见
        logger.exception(f"celery: 任务 {args[0]}（{name}）在本进程执行失败")
        await TaskRecord.fail(args[0], exception)
        return {}


@celery_client.task(name="celery.ping")
def ping() -> str:
    """占位任务：回一个 pong，用来确认 worker 已就绪、任务能被正常投递与执行。

    换成真正的异步任务时按同样的写法注册即可（``@celery_client.task(name=...)``）。
    """
    return "pong"


@celery_client.task(name="celery.scan_tables")
def scan_tables() -> dict[str, Any]:
    """定时巡检：比对业务库里的真实结构与元数据库里的登记，把差异按表处置掉。

    比对维度是表、字段名、字段类型与键角色（普通字段 ↔ 主键 / 外键，见 :func:`scan_database`），
    外加「取值够不够」：``Column.values`` 少于 :data:`MIN_VALUES` 条的取值类字段也算一处差异。
    四类差异的处置（见 :func:`reconcile_database`）：

    - 已移除的表：清掉其名下字段的嵌入向量、取值点与元数据库里的登记，不惊动模型；
    - 改动的表：**先删掉旧登记与旧向量，再整表重采**（删除重建，不按字段打补丁）——字段改名 / 改类型后
      旧字段编号下的向量点再也对不上，留着只会污染检索；
    - 新增的表：直接采集并保存（模型分析出角色 / 描述 / 别名 / 同步标记，随后向量化）；
    - 取值不足的字段：单独补采一次取值（不重跑分析、不重算字段向量）。

    一轮同时只跑一个（见 :func:`scan_exclusively`），由 beat 投递（周期取自
    ``config.CELERY_SCAN_INTERVAL``，调度表见 :data:`plugins.celery.celery_client`）。

    :return: ``{业务库编号: {"added": [...], "removed": [...], "changed": [...], "values": [...]}}``：
        本轮查出差异的库（差异已处置；处置失败的库与尚未采集成功的库只记日志，见 :func:`scan_databases`），
        ``values`` 里是字段编号

    巡检当前**关停**（``config.CELERY_SCAN_ENABLED`` 为 False）：调度表里已经没有这条周期任务，拦住
    手动投递与残留的 beat 调度；开关打开并重启 worker 才真正跑（见 plugins.celery 的 ``beat_schedule``）。
    """
    if not CELERY_SCAN_ENABLED:
        logger.info("scan_tables: 巡检已关停（config.CELERY_SCAN_ENABLED 为 False），本轮跳过")
        return {}
    return run(scan_exclusively())


async def scan_exclusively() -> dict[str, Any]:
    """抢到互斥锁才巡检，抢不到（上一轮还没结束）就跳过本轮，返回空报告。

    锁是 Redis 上的（见 :func:`plugins.redis.lock`），跨进程有效：prefork 的多个 worker 子进程
    抢的是同一把锁。上一轮巡检还没结束时抢不到锁，本轮直接跳过——巡检会重写元数据库与向量库
    （先删后建，见 :func:`reconcile_database`），少看一次只是晚一轮发现差异，下一轮照常；
    TTL 见 :data:`SCAN_LOCK_TTL`，兜底持有者被强杀时锁不会一直挂着。
    """
    async with lock(SCAN_LOCK_KEY, SCAN_LOCK_TTL, db=CELERY_BROKER_DB) as acquired:
        if not acquired:
            logger.warning("scan_tables: 上一轮巡检还没结束，跳过本轮")
            return {}
        return await scan_databases()


async def scan_databases() -> dict[str, Any]:
    """逐个业务库巡检并处置，返回 ``{业务库编号: {...}}``（键见 :func:`scan_tables`）。

    只收录真正有差异的库，键即元数据库里的 Database.id（表名的口径见 :func:`scan_database`）；
    查出来的差异当场交给 :func:`reconcile_database` 处置，故返回的是「本轮发现过的差异」，
    不是「还留着的差异」。

    某个库读不动（连接没登记、库被移走）只记一条日志跳过，不影响其它库；处置失败（向量库不可用、
    模型报错）同样只记日志，下一轮巡检会重新发现这批差异并再处置一次。

    还没采集成功的库（``Database.collected`` 为假）只报差异、不处置：这类库的登记本身还可能被改
    （见 controllers.collection.update_database），它名下的表也会被整批当成新增——采不采由用户按需发起
    （``POST /collection/database/{id}/collect``），巡检不该替它把整库的模型调用跑掉。
    """
    report = {}
    for database in await Database.all():
        try:
            differences = await scan_database(database)
        except Exception as error:  # noqa: BLE001 - 单个库读不动不该拖垮整轮巡检
            logger.warning(f"scan_tables: 巡检业务库 {database.name}（{database.id}）失败：{error}")
            continue
        if not any(differences.values()):
            continue
        report[database.id] = differences
        logger.warning(f"scan_tables: 业务库 {database.name}（{database.id}）与元数据库登记不一致："
                       f"新增 {differences['added']}、已删除 {differences['removed']}、"
                       f"变动 {differences['changed']}、取值不足 {len(differences['values'])} 个字段")
        if not database.collected:
            # 还没整库采集过：登记还能改、也没有可维护的采集产物，交给用户按需发起的整库采集
            logger.warning(f"scan_tables: 业务库 {database.name}（{database.id}）尚未采集成功，跳过处置")
            continue
        try:
            await reconcile_database(database, differences)
        except Exception as error:  # noqa: BLE001 - 单个库处置失败不该拖垮整轮巡检，下一轮会再发现
            logger.exception(f"scan_tables: 处置业务库 {database.name}（{database.id}）的差异失败：{error}")
    logger.info(f"scan_tables: 巡检完成，{len(report)} 个库与元数据库登记不一致")
    return report


async def scan_database(database: Database) -> dict[str, list[str]]:
    """比对单个业务库与元数据库里登记的表 / 字段，报出四类差异。

    :param database: 元数据库里登记的业务库（models.meta.Database）
    :return: ``{"added": [表名], "removed": [表名], "changed": [表名], "values": [字段编号]}``

    真实结构取自业务库自己的 information_schema（见 models.get_table_names / get_table_infos），
    连的是该库编号对应的连接（见 models.get_connection）：

    - **新增**：业务库有、元数据库没有的表；
    - **移除**：元数据库有、业务库里没有的表（表改名会表现为一张新增 + 一张已移除）；
    - **改动**：两侧都有、但「(字段名, 类型, 是否主键 / 外键) 的集合」对不上——覆盖字段改名、类型变更、
      主键 / 外键角色变更（业务库一侧取自 information_schema 的 keys，元数据库一侧取自 Column.role，
      两侧类型同源，可直接比）；
    - **取值不足**：``Column.values`` 少于 :data:`MIN_VALUES` 条的取值类字段（``synchronized`` 且类型属于
      ``VALUE_TYPES``）。只挑没改动的表：新增 / 改动的表本来就要整表重采一遍，取值会顺带补上，不必重复列出。

    采集名单之外的表明不参与比对（见 models.meta.Database 的 whitelist / blacklist）：白名单非空时只看名单里的表，
    黑名单里的表一律不看。两者都不算「新增」，已登记的会落到「已移除」里、由调用方连登记与向量一起清掉——
    名单表示这些表不该出现在库上下文里。

    只读（读业务库结构、读元数据库登记），报出差异而不处置：怎么处置交给调用方（见
    :func:`reconcile_database`）。
    """
    whitelist = set(database.whitelist or [])
    blacklist = set(database.blacklist or [])
    available = [name for name in await get_table_names(database.id)
                 if name not in blacklist and (not whitelist or name in whitelist)]
    registered = {table.name: table for table in
                  await Table.filter(database_id=database.id).only("id", "name")}
    names = set(available)
    added = [name for name in available if name not in registered]
    removed = [name for name in registered if name not in names]

    # 两侧都有的表逐字段比对：字段名、类型、键角色任一不符即「改动」。
    # 业务库一侧的键从 information_schema 的键标记取，元数据库一侧从 Column.role 取（两侧都归成布尔）
    shared = [name for name in available if name in registered]
    changed = []
    if shared:
        actual = {info["name"]: {(column["name"], column["type"],
                                  any(key.get("type") in ("PK", "FK") for key in column["keys"]))
                                 for column in info["columns"]}
                  for info in await get_table_infos(shared, database.id)}
        ids = {registered[name].id: name for name in shared}
        expected = {name: set() for name in shared}
        for column in await Column.filter(table_id__in=list(ids)).only("table_id", "name", "type", "role"):
            name = ids.get(column.table_id)
            if name is not None:
                expected[name].add((column.name, column.type,
                                    column.role in (ColumnRoleOption.PRIMARY_KEY, ColumnRoleOption.FOREIGN_KEY)))
        changed = [name for name in shared if actual.get(name) != expected[name]]

    # 取值不足的字段：只看「没改动」的表，改了的那批整表重采时会顺带补上取值
    settled_ids = [registered[name].id for name in shared if name not in changed]
    values = [column.id for column in
              await Column.filter(table_id__in=settled_ids, synchronized=True).only("id", "type", "values")
              if is_value_type(column.type) and len(column.values or []) < MIN_VALUES] if settled_ids else []
    return {"added": added, "removed": removed, "changed": changed, "values": values}


async def reconcile_database(database: Database, differences: dict[str, list[str]]) -> None:
    """按巡检结果处置一个业务库的差异（差异见 :func:`scan_database`）。

    :param database: 元数据库里登记的业务库（models.meta.Database）
    :param differences: ``{"added": [...], "removed": [...], "changed": [...], "values": [...]}``

    - 已移除的表：清掉向量与登记（见 :func:`purge_tables`）。表都没了，留下的登记会让前端仍能选中它、
      检索也仍以为这些字段可用；
    - 改动的表：**先清掉旧登记与旧向量，再整表重采**（删除重建）。字段改名 / 改类型后，旧字段编号下的
      向量点再也对不上，留着只会污染检索；重采按业务库的最新结构重建整张表；
    - 新增的表：直接采集并保存（表编号按 ``<业务库编号>.<表名>`` 拼，与首次采集的口径一致）；
    - 取值不足的字段：补采一次取值（见 :func:`collect_values`），不重跑模型分析。

    表编号按命名取：已登记的表沿用元数据库里的编号，未登记的按 ``<业务库编号>.<表名>`` 拼。
    表在业务库里改了名会表现为一张新增 + 一张已移除，两侧各按自己的口径处置，互不干扰。
    """
    registered = {table.name: table.id for table in
                  await Table.filter(database_id=database.id).only("id", "name")}
    added_ids = [f"{database.id}.{name}" for name in differences["added"]]
    changed_ids = [registered[name] for name in differences["changed"]]
    if differences["removed"]:
        await purge_tables(database.id, [registered[name] for name in differences["removed"]])
    if changed_ids:
        # 删除重建：旧登记与旧向量都不再可信（字段可能已经改名 / 改类型），先清干净再整表重采
        await purge_tables(database.id, changed_ids)
    table_ids = [*added_ids, *changed_ids]
    if table_ids:
        async for step in collect_tables(database.id, table_ids):
            if step.status == StepStatus.FAILED:
                logger.warning(f"scan_tables: 采集表失败：{step.message}（{step.error}）")
        logger.info(f"scan_tables: collected {len(table_ids)} table(s) of database {database.id}")
    if differences["values"]:
        await collect_values(database.id, differences["values"])


async def purge_tables(database_id: int, table_ids: list[str]) -> None:
    """清掉一批表：先删其名下字段的嵌入向量与取值点，再删元数据库里的登记。

    :param database_id: 这些表所属的业务库编号（Database.id）：向量按库分开存，字段点与取值点都从该库
        的集合里删（见 Qdrant.delete_columns / delete_values）
    :param table_ids: 要清的表编号（Table.id，形如 <库编号>.<表名>）

    向量先删、登记后删：向量库清不动（Qdrant 不可用）时登记还在，下一轮巡检照样能查出这批表并再清一次，
    删除本身是幂等的；反过来先删登记就会留下再也清不掉、却仍会被 find_columns 召回的字段点。
    登记里字段与表分两步删（外键为 RESTRICT，得先删字段再删表，同删库的口径，见
    controllers.collection.delete_database），两步放在一个事务里。巡检里两处用到它：已移除的表、
    以及要「删除重建」的改动表（先清后采）。
    """
    column_ids = [column.id for column in await Column.filter(table_id__in=table_ids).only("id")]
    await qdrant.delete_columns(database_id, column_ids)
    await qdrant.delete_values(database_id, column_ids)
    async with in_transaction(META_CONNECTION):
        await Column.filter(table_id__in=table_ids).delete()
        await Table.filter(id__in=table_ids).delete()
    logger.info(f"scan_tables: purged {len(table_ids)} table(s) "
                f"({len(column_ids)} column(s)), database {database_id}")


async def collect_values(database_id: int, column_ids: list[str]) -> None:
    """补采一批字段的取值：重查业务库、重写向量库的 values 集合，并把样本写回 Column.values。

    :param database_id: 这些字段所属的业务库编号（Database.id）
    :param column_ids: 待补采的字段编号（Column.id）：巡检挑出的「取值不足」字段，见 :func:`scan_database`

    只补取值，不重跑模型分析、也不重算字段向量——分析结果没变，变的只是这批字段在业务库里的取值。
    按 :data:`agnets.collection.VALUE_BATCH_SIZE` 分批，每批拼一条 UNION ALL 查询（见
    models.get_column_values）；写回 Column.values 是为了让下一轮巡检能据此判断「取值够不够」，
    否则这批字段每轮都会被当成取值不足、反复补采。
    """
    columns = await Column.filter(id__in=column_ids).only("id", "name", "table_id", "values")
    names = {table.id: table.name for table in
             await Table.filter(id__in={column.table_id for column in columns}).only("id", "name")}
    targets = [(names[column.table_id], column.name, column) for column in columns if column.table_id in names]
    collected = 0
    for start in range(0, len(targets), VALUE_BATCH_SIZE):
        batch = targets[start:start + VALUE_BATCH_SIZE]
        try:
            values = await get_column_values(
                [(table_name, name, VALUE_LIMIT, 0) for table_name, name, _ in batch], database_id)
            points, samples = [], []
            for table_name, name, column in batch:
                # NULL 不是可用取值，直接丢弃（与采集时的口径一致，见 agnets.collection.embed_columns）
                payload = [value for value in values.get(f"{table_name}.{name}", []) if value is not None]
                if payload:
                    points.append(Value(payload=payload, column_id=column.id))
                column.values = payload[:VALUE_LIMIT]
                samples.append(column)
            if points:
                await qdrant.add_values(database_id, points)
            await Column.bulk_update(samples, fields=["values"])
            collected += len(batch)
        except Exception as error:  # noqa: BLE001 - 一批失败不影响其余批次，下一轮巡检会再发现
            logger.warning(f"scan_tables: 补采取值失败（{len(batch)} 个字段）：{error}")
    logger.info(f"scan_tables: collected values for {collected}/{len(targets)} column(s), "
                f"database {database_id}")
