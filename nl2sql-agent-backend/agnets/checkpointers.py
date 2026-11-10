"""短期记忆（checkpoint）存储组件。

在 langgraph 的 :class:`AsyncPostgresSaver` 之上叠加一层 RedisOM 缓存，
读写都遵循 **先 Redis、后 PostgreSQL** 的顺序：

- 写：先把新 checkpoint / pending writes 写入 Redis，再落 PostgreSQL。
  紧随其后的读取可直接命中缓存；PostgreSQL 写入失败时补偿性失效刚写入
  的记录并抛出异常，避免缓存长期领先于事实来源。
- 读：先按主键取缓存记录（指定了 ``checkpoint_id`` 时取精确记录，否则
  取“最新”记录），未命中再回源 PostgreSQL 并按需回填。
- PostgreSQL 是唯一的事实来源，负责持久化、建表与迁移；Redis 只是可重建
  的缓存——缓存读写的任何异常都只告警（fail-open），自动降级为直连
  PostgreSQL，不影响业务主流程。

缓存记录由 :class:`plugins.redis.ShortTermMemory` 描述（RedisOM
``JsonModel``）：一条记录即一个 checkpoint 的完整 :class:`CheckpointTuple`，
用 langgraph 自身的 ``serde`` 序列化后存入 ``payload``，与落库格式完全一致；
``checkpoint_id`` 另存为一个字段，用于低成本判断“最新”记录指向哪个
checkpoint。模型只有 Schema，连接由本组件在实例化时注入；由于基于
RedisJSON，要求 Redis 加载了 ReJSON 模块（Redis Stack）。

主键规则：精确记录为 ``{thread_id}:{checkpoint_ns}:{checkpoint_id}``，
“最新”记录为 ``{thread_id}:{checkpoint_ns}:latest``。两者内容相同，由
``aput`` 整体覆盖（无需先删后写的空窗）；``aput_writes`` 只在确认该
checkpoint 仍是“最新”时才连带刷新“最新”记录，以免把更旧的 checkpoint
覆盖回“最新”位置。

用法::

    saver = CachedAsyncPostgresSaver(pool)
    await saver.setup()
    ...
    await saver.aclose()
"""

import asyncio
import sys
from collections.abc import Sequence
from typing import Any

from aredis_om import NotFoundError
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    RunnableConfig,
    get_checkpoint_id,
    get_serializable_checkpoint_metadata,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from redis.asyncio import Redis

from logger import logger
from plugins.redis import ShortTermMemory, build_redis_url

#: “最新 checkpoint”记录在主键中的哨兵值（checkpoint_id 为 uuid，不会撞车）
LATEST = "latest"

#: 缓存记录的默认过期时间（秒）。不放进配置文件：它只决定缓存这份**可重建的副本**留多久，
#: 事实来源始终是 PostgreSQL——过期只是让下一条读到它的请求回一次源，与其他业务配置无关，
#: 因此留在组件里，需要按实例调整时走 ``CachedAsyncPostgresSaver(ttl=...)``
DEFAULT_TTL = 3600


class CachedAsyncPostgresSaver(AsyncPostgresSaver):
    """RedisOM 缓存的 PostgreSQL checkpoint 存储。

    Args:
        conn: 单个 ``AsyncConnection`` 或 ``AsyncConnectionPool`` 连接池。用连接池时必须按
            langgraph 的要求建：``kwargs={"autocommit": True, "row_factory": dict_row}``
            ——``setup()`` 里有 ``CREATE INDEX CONCURRENTLY``（不能跑在事务块里，非 autocommit
            会直接报 ``ActiveSqlTransaction``），其余查询按列名取值（默认的元组行装配不出
            checkpoint）。Windows 上还要注意事件循环：psycopg 异步模式不支持默认的
            ``ProactorEventLoop``，得用 ``SelectorEventLoop``。
        ttl: 缓存过期时间（秒）。默认取模块常量 :data:`DEFAULT_TTL`（1 小时），
            不从配置文件读——缓存只是可重建的副本。每条记录独立续期：
            只写不读的会话不会一直占着内存。
        redis_client: 复用的 Redis 客户端，须开启 ``decode_responses``。
            默认按 :mod:`config` 自行创建，此时由本组件负责关闭。
        serde: 自定义序列化器，默认沿用 langgraph 的 ``JsonPlusSerializer``。
    """

    def __init__(
            self,
            conn: Any,
            *,
            ttl: int | None = None,
            redis_client: Redis | None = None,
            serde: Any = None,
    ) -> None:
        super().__init__(conn, serde=serde)
        self.ttl: int = DEFAULT_TTL if ttl is None else ttl
        self._owns_redis: bool = redis_client is None
        self._redis: Redis = (
            redis_client
            if redis_client is not None
            else Redis.from_url(build_redis_url(), decode_responses=True)
        )
        # RedisOM 的连接挂在模型类上（见 plugins/redis.py），注入实例持有的连接
        ShortTermMemory.Meta.database = self._redis

    async def aclose(self) -> None:
        """关闭 Redis 客户端（仅当客户端由本组件创建时）。"""
        if self._owns_redis:
            await self._redis.aclose()

    # ------------------------------------------------------------------ #
    # 主键与编解码
    # ------------------------------------------------------------------ #
    @staticmethod
    def _scope_of(cfg: RunnableConfig) -> tuple[str, str]:
        """取出记录作用域：``(thread_id, checkpoint_ns)``。"""
        configurable = cfg["configurable"]
        return configurable["thread_id"], configurable.get("checkpoint_ns", "")

    @classmethod
    def _pk_of(cls, cfg: RunnableConfig, checkpoint_id: str | None = None) -> str:
        """推导记录主键；未指定 ``checkpoint_id`` 时指向“最新”记录。"""
        thread_id, checkpoint_ns = cls._scope_of(cfg)
        return f"{thread_id}:{checkpoint_ns}:{checkpoint_id or LATEST}"

    def _record(
            self, pk: str, checkpoint_id: str, encoded: tuple[str, bytes]
    ) -> ShortTermMemory:
        """用序列化结果构造一条缓存记录。"""
        serde_type, payload = encoded
        return ShortTermMemory(
            pk=pk,
            checkpoint_id=checkpoint_id,
            serde_type=serde_type,
            payload=payload,
        )

    def _encode(self, value: CheckpointTuple) -> tuple[str, bytes]:
        """用 checkpointer 自身的 serde 序列化，保证与落库格式一致。"""
        return self.serde.dumps_typed(
            {
                "config": value.config,
                "checkpoint": value.checkpoint,
                "metadata": value.metadata,
                "parent_config": value.parent_config,
                "pending_writes": value.pending_writes,
            }
        )

    def _decode(self, record: ShortTermMemory) -> CheckpointTuple:
        """把缓存记录还原成 :class:`CheckpointTuple`。"""
        return CheckpointTuple(
            **self.serde.loads_typed((record.serde_type, record.payload))
        )

    @staticmethod
    def _next_config(cfg: RunnableConfig, checkpoint: Checkpoint) -> RunnableConfig:
        """推导本次写入后的运行配置。

        与 ``AsyncPostgresSaver.aput`` 的推导保持一致（同样直接取
        ``checkpoint["id"]`` 且丢弃其它 configurable 项），这样在落库之前
        就能拿到新 ``checkpoint_id`` 用于写缓存；仅在 langgraph 改变该推导
        方式时才需要同步调整。
        """
        configurable = cfg["configurable"]
        return {
            "configurable": {
                "thread_id": configurable["thread_id"],
                "checkpoint_ns": configurable["checkpoint_ns"],
                "checkpoint_id": checkpoint["id"],
            }
        }

    # ------------------------------------------------------------------ #
    # 缓存读写（fail-open：任何异常都降级为直连 PostgreSQL）
    # ------------------------------------------------------------------ #
    async def _cache_get(self, pk: str) -> CheckpointTuple | None:
        """按主键读取缓存；未命中、记录损坏或缓存故障都返回 ``None``。"""
        try:
            return self._decode(await ShortTermMemory.get(pk))
        except NotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001 - 缓存故障不应中断业务
            logger.warning("短期记忆缓存读取失败，降级直读 PostgreSQL: {}", exc)
            return None

    async def _cache_put(self, *records: ShortTermMemory) -> None:
        """写入缓存记录并续期 TTL，整批共用一条流水线以减少往返。

        入队（save / expire）本身也是异步调用，但只是往同一条流水线里塞命令；用 gather 一次并发入队，
        不在循环里逐条 await——顺序仍是先写入再续期，同一条记录的 SET 一定在 EXPIRE 之前。
        """
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                await asyncio.gather(*(record.save(pipeline=pipe) for record in records))
                await asyncio.gather(*(record.expire(self.ttl, pipeline=pipe) for record in records))
                await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("短期记忆缓存写入失败: {}", exc)

    async def _cache_del(self, *pks: str) -> None:
        """删除缓存记录；空主键直接跳过。入队同样一次并发，不在循环里逐条 await。"""
        keys = tuple(pk for pk in pks if pk)
        if not keys:
            return
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                await asyncio.gather(*(ShortTermMemory.delete(pk, pipeline=pipe) for pk in keys))
                await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("短期记忆缓存失效失败: {}", exc)

    async def _is_latest(self, latest_pk: str, checkpoint_id: str) -> bool:
        """判断“最新”记录当前是否指向该 checkpoint。

        只探测单个字段（``JSON.GET <key> $.checkpoint_id``）而不是整条记录，
        避免为了判断而搬运完整 payload；JSONPath 命中的值以数组返回，这里
        统一按数组比较。缓存故障按“不是最新”处理——宁可少刷新一次，也不
        误覆盖更新的数据。
        """
        try:
            stored = await self._redis.json().get(
                ShortTermMemory.make_primary_key(latest_pk), "$.checkpoint_id"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("短期记忆缓存读取失败: {}", exc)
            return False
        return (stored if isinstance(stored, list) else [stored]) == [checkpoint_id]

    # ------------------------------------------------------------------ #
    # checkpoint 读取：先 Redis，未命中回源 PostgreSQL 并回填
    # ------------------------------------------------------------------ #
    async def aget_tuple(self, cfg: RunnableConfig) -> CheckpointTuple | None:
        """读取 checkpoint，优先命中 Redis 缓存，未命中则回源并回填。"""
        explicit_id = get_checkpoint_id(cfg)
        cached = await self._cache_get(self._pk_of(cfg, explicit_id))
        if cached is not None:
            return cached

        result = await super().aget_tuple(cfg)
        if result is None:
            return None

        await self._backfill(cfg, result, explicit_id)
        return result

    async def _backfill(
            self,
            cfg: RunnableConfig,
            result: CheckpointTuple,
            explicit_id: str | None,
    ) -> None:
        """回源后回填缓存：精确记录必填，未指定 id 时再填一条“最新”记录。"""
        checkpoint_id = result.config["configurable"]["checkpoint_id"]
        encoded = self._encode(result)
        records = [self._record(self._pk_of(cfg, checkpoint_id), checkpoint_id, encoded)]
        if not explicit_id:
            records.append(self._record(self._pk_of(cfg), checkpoint_id, encoded))
        await self._cache_put(*records)

    # ------------------------------------------------------------------ #
    # checkpoint 写入：先写 Redis，再落 PostgreSQL
    # ------------------------------------------------------------------ #
    async def aput(
            self,
            cfg: RunnableConfig,
            checkpoint: Checkpoint,
            metadata: CheckpointMetadata,
            new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """写入 checkpoint：先把完整 tuple 写进 Redis，再落 PostgreSQL。

        新 checkpoint 尚无 pending writes，先以空列表入缓存，待
        ``aput_writes`` 增量补全；同时整体覆盖该会话的“最新”记录。若
        PostgreSQL 写入失败，则失效刚写入的两条记录后抛出异常——缓存可以
        暂时缺失（下次读取回源），但绝不允许领先于事实来源。
        """
        next_cfg = self._next_config(cfg, checkpoint)
        thread_id, checkpoint_ns = self._scope_of(next_cfg)
        parent_id = get_checkpoint_id(cfg)
        staged = CheckpointTuple(
            config=next_cfg,
            checkpoint=checkpoint,
            metadata=get_serializable_checkpoint_metadata(cfg, metadata),
            # 与 AsyncPostgresSaver 读取端的 parent_config 构造保持一致
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_id,
                    }
                }
                if parent_id is not None
                else None
            ),
            pending_writes=[],
        )

        checkpoint_id = next_cfg["configurable"]["checkpoint_id"]
        encoded = self._encode(staged)
        exact_pk = self._pk_of(next_cfg, checkpoint_id)
        latest_pk = self._pk_of(next_cfg)
        await self._cache_put(
            self._record(exact_pk, checkpoint_id, encoded),
            self._record(latest_pk, checkpoint_id, encoded),
        )

        try:
            return await super().aput(cfg, checkpoint, metadata, new_versions)
        except Exception:
            await self._cache_del(exact_pk, latest_pk)
            raise

    async def aput_writes(
            self,
            cfg: RunnableConfig,
            writes: Sequence[tuple[str, Any]],
            task_id: str,
            task_path: str = "",
    ) -> None:
        """写入 pending writes：先并入 Redis 缓存，再落 PostgreSQL。

        缓存中存在该 checkpoint 时，把新 writes 按读取端一致的
        ``(task_id, idx)`` 顺序并入并整体覆盖；不存在（TTL 已过期、或该
        checkpoint 并非经由本缓存写入）时无法无损合并，退化为失效该记录，
        且仅当它确实还是“最新”记录时才连带失效“最新”记录，以免白白丢掉
        更热的数据。PostgreSQL 写入失败时同样失效本次改动的记录后抛出。
        """
        checkpoint_id = get_checkpoint_id(cfg)
        if checkpoint_id is None:
            # 没有锚定 checkpoint_id 的写入无从定位记录，只能失效“最新”记录
            await self._cache_del(self._pk_of(cfg))
            touched: tuple[str, ...] = ()
        else:
            touched = await self._merge_writes(cfg, checkpoint_id, writes, task_id)

        try:
            await super().aput_writes(cfg, writes, task_id, task_path)
        except Exception:
            await self._cache_del(*touched)
            raise

    async def _merge_writes(
            self,
            cfg: RunnableConfig,
            checkpoint_id: str,
            writes: Sequence[tuple[str, Any]],
            task_id: str,
    ) -> tuple[str, ...]:
        """把新 writes 并入缓存记录，返回本次改动的记录主键（供补偿失效）。"""
        exact_pk = self._pk_of(cfg, checkpoint_id)
        latest_pk = self._pk_of(cfg)
        cached = await self._cache_get(exact_pk)
        if cached is None:
            keys = [exact_pk]
            if await self._is_latest(latest_pk, checkpoint_id):
                keys.append(latest_pk)
            await self._cache_del(*keys)
            return ()

        merged = cached._replace(
            pending_writes=self._merge_pending(cached.pending_writes, writes, task_id)
        )
        encoded = self._encode(merged)
        records = [self._record(exact_pk, checkpoint_id, encoded)]
        if await self._is_latest(latest_pk, checkpoint_id):
            records.append(self._record(latest_pk, checkpoint_id, encoded))
        await self._cache_put(*records)
        return tuple(record.pk for record in records)

    @staticmethod
    def _merge_pending(
            pending: list[tuple[str, str, Any]],
            writes: Sequence[tuple[str, Any]],
            task_id: str,
    ) -> list[tuple[str, str, Any]]:
        """把新 writes 按读取端顺序并入已有 pending writes。

        PostgreSQL 中 pending writes 按 ``(task_id, idx)`` 排序、读取时也按该
        顺序返回；``idx`` 是 ``_dump_writes`` 里“特殊通道取负索引、普通通道取
        本次写入序号”的结果。缓存只保留排序后的三元组，这里按同 ``task_id``
        分组内的先后位置复原 ``idx``——相对顺序与 PostgreSQL 完全一致，足以
        还原出相同的 ``pending_writes``。
        """
        counters: dict[str, int] = {}
        entries: list[tuple[str, int, str, Any]] = []
        for tid, channel, value in pending:
            idx = counters.get(tid, 0)
            counters[tid] = idx + 1
            entries.append((tid, idx, channel, value))
        entries.extend(
            (task_id, WRITES_IDX_MAP.get(channel, idx), channel, value)
            for idx, (channel, value) in enumerate(writes)
        )
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        return [(tid, channel, value) for tid, _, channel, value in entries]

    async def adelete_thread(self, thread_id: str) -> None:
        """删除线程的 checkpoint，并清理其全部缓存记录。"""
        await super().adelete_thread(thread_id)
        try:
            keys = [
                key
                async for key in self._redis.scan_iter(
                    match=ShortTermMemory.make_key(f"{thread_id}:*")
                )
            ]
            if keys:
                await self._redis.delete(*keys)
        except Exception as exc:  # noqa: BLE001
            logger.warning("短期记忆缓存清理失败: {}", exc)


def loop_factory() -> asyncio.AbstractEventLoop:
    """给 uvicorn / 独立脚本用的事件循环工厂：Windows 上给出 Selector 循环。

    psycopg 的异步模式不能在 Windows 默认的 ``ProactorEventLoop`` 上跑（会直接报
    ``Psycopg cannot use the 'ProactorEventLoop' to run in async mode``），而 uvicorn 只在
    子进程模式（``--reload`` / ``--workers``）下才在 Windows 上挑 Selector，单进程默认给 Proactor，
    因此要跑本组件的进程得显式指定::

        uvicorn app:app --loop agnets.checkpointers:loop_factory

    其它平台返回默认的新循环，不必特殊处理。
    """
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()
