from contextlib import asynccontextmanager
from typing import AsyncGenerator

from models import Model
from plugins import register_plugins, close_plugins
# 导入 plugins.celery 即注册 Celery 插件（PluginMeta 在类定义时登记）：应用启动时由
# register_plugins 拉起 worker 子进程，关闭时由 close_plugins 停掉它——Celery 停用时
# （config.CELERY_ENABLED 为 False）不拉起 worker，后台任务改由本进程内的 asyncio 任务执行
import plugins.celery
# Redis 客户端也作为插件登记（见 plugins.redis.RedisClient）：register_plugins 建连、注入任务记录模型并迁移索引
from plugins.redis import RedisClient  # noqa: F401
from controllers import evaluation, retrieval, collection, models
from config import CORS_ALLOW_ORIGINS, CORS_ALLOW_CREDENTIALS, CORS_ALLOW_METHODS, CORS_ALLOW_HEADERS, \
    CORS_EXPOSE_HEADERS
from logger import setup_logger, teardown_logger

from fastapi import FastAPI
from loguru import logger
import uvicorn
from starlette.middleware.cors import CORSMiddleware


@asynccontextmanager
async def register(app: FastAPI) -> AsyncGenerator[None, None]:
    setup_logger()
    logger.info("application startup")
    await Model.init()
    # 插件注册：Redis 常驻客户端（任务记录与索引，见 plugins.redis.RedisClient）、Celery worker、
    # 检索图与向量库等都在这一步装配好
    await register_plugins()
    yield
    try:
        logger.info("application shutdown")
        await close_plugins()
        await Model.close()
    finally:
        # 刷新 enqueue 队列，确保关闭阶段的日志全部落盘
        teardown_logger()


app = FastAPI(lifespan=register)
app.include_router(retrieval.router)
app.include_router(collection.router)
app.include_router(evaluation.router)
app.include_router(models.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=CORS_ALLOW_CREDENTIALS,
    allow_methods=CORS_ALLOW_METHODS,
    allow_headers=CORS_ALLOW_HEADERS,
    expose_headers=CORS_EXPOSE_HEADERS
)

if __name__ == "__main__":
    # Windows 上必须显式给 uvicorn 指定 Selector 事件循环：短期记忆组件的 psycopg 异步模式不支持
    # 默认的 ProactorEventLoop（uvicorn 只在 --reload / --workers 子进程模式下才自己挑 Selector）。
    # 直接跑 uvicorn/fastapi 命令时同理：--loop agnets.checkpointers:loop_factory
    uvicorn.run(app, loop="agnets.checkpointers:loop_factory")
