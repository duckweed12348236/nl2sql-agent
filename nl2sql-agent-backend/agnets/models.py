"""模型实例的装配与调用：按 `model` 表里的登记取默认模型，构造并缓存对话 / 嵌入 / 重排三类实例。

模型配置不再写在 config 与 pyproject.toml 里（原先的 ``[tool.qwen]`` 等段已由
``scripts/migrate_model_config.py`` 搬进 ``model`` 表）：每条登记见 :class:`models.models.ModelConfig`，
按 ``type`` 分 chat / embedding / rerank，每种用途由 ``is_default`` 指一条为默认。

取用一律异步（要查库）：:func:`chat_model` / :func:`embeddings` / :func:`reranker`。
构造好的实例按「模型编号」缓存在进程内：同一条登记重复取用不会重复建客户端；库里换了默认
（编号变了）下一次取用自动重建；改的是**同一条**的地址 / 密钥时，调 :func:`reset_models`
（或重启进程）才生效。

本模块不读 config，也不在 import 期建实例——库连不上、表还没登记时应用仍能启动，
第一次取模型时才报错（信息里带「先跑迁移脚本 / 在库里登记」的提示）。
"""

from abc import ABC, abstractclassmethod
from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx
from langchain.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from loguru import logger

from models.models import ModelConfig, ModelTypeOption

#: 对话模型的请求超时（秒）：本地推理在长上下文（整库表结构）下首个 token 可能等很久，
#: 连接超时另算——连不上时要尽快失败，而不是陪着等半小时
REQUEST_TIMEOUT = 1800.0
CONNECT_TIMEOUT = 10.0
#: 嵌入模型在第三方兼容端点上的默认口径：不能先按 tiktoken 编码成 token 数组再发
EMBEDDING_CTX_LENGTH = False

#: 已构造的实例：用途 -> (模型编号, 实例)。编号一变即重建，见模块说明
CACHE: dict[str, tuple[int, Any]] = {}
#: 默认登记的短缓存：用途 -> (取到的时刻, 登记)。中间件每次都取对话模型，不能每次都查库；
#: TTL 内的切换默认会晚几秒生效，改登记后想立刻生效则调 reset_models 清掉
DEFAULT_TTL = 5.0
DEFAULT_CACHE: dict[str, tuple[float, ModelConfig]] = {}


@dataclass()
class RerankModel:
    """重排模型的连接信息，随图上下文传入重排客户端。

    url：完整请求地址。OpenAI 官方协议没有重排端点，「OpenAI 兼容」的各家路径并不统一
        （百炼是 {base}/compatible-api/v1/reranks，硅基流动是 {base}/v1/rerank），故直接给出完整地址；
    name：模型名，如 Qwen/Qwen3-Reranker-8B、BAAI/bge-reranker-v2-m3；
    key：该服务商的 API Key；
    instruct_field：任务说明（instruction）在请求体里的字段名，各家叫法不统一
        （硅基流动为 instruction，百炼为 instruct）；None 表示不发该字段。
    """
    url: str
    name: str
    key: str
    instruct_field: str | None = "instruct"


async def build_default_config(model_type: ModelTypeOption) -> ModelConfig:
    """取某种用途下当前生效的模型登记。

    :param model_type: 用途（chat / embedding / rerank，见 :class:`models.models.ModelTypeOption`）
    :return: 该用途的默认登记
    :raises RuntimeError: 该用途一条启用中的登记都没有

    优先取 ``is_default`` 且启用的那条；没有默认（或默认被停用）时退到最新登记的一条，并记一条告警——
    这样即便库里没人点过「设为默认」，系统也能跑起来；两者都没有就报错，提示先登记。

    结果按 :data:`DEFAULT_TTL` 秒缓存：中间件每次模型调用都会取一次对话模型，逐次查库太贵；
    改登记后要立即生效，调 :func:`reset_models` 把它一起清掉（管理接口写操作后就是这么做的）。
    """
    now = monotonic()
    cached = DEFAULT_CACHE.get(model_type.value)
    if cached is not None and now - cached[0] < DEFAULT_TTL:
        return cached[1]
    config = await ModelConfig.filter(type=model_type, is_default=True, enabled=True).first()
    if config is None:
        config = await ModelConfig.filter(type=model_type, enabled=True).order_by("-id").first()
        if config is not None:
            logger.warning(f"models: {model_type} 没有启用的默认模型，暂用最新登记的一条（{config.name}）")
    if config is None:
        raise RuntimeError(f"没有可用的 {model_type} 模型：先在 model 表里登记一条"
                           f"（或跑 scripts/migrate_model_config.py 从旧配置导入）")
    DEFAULT_CACHE[model_type.value] = (now, config)
    return config


async def fetch_chat_model() -> BaseChatModel:
    """取对话模型实例（缓存按登记编号，换了默认自动重建）。

    :return: 可直接 ``ainvoke`` 的对话模型

    温度取登记 ``extra`` 里的 ``temperature``（默认 1）；TLS 与超时挂在自建的 httpx 客户端上
    （见登记 ``extra`` 的 ``verify_ssl`` / ``ca_bundle``）——内网自签名证书不处理的话，
    httpx 会抛 CERTIFICATE_VERIFY_FAILED，被 openai 客户端包成一句看不出真因的「Connection error.」。
    输出上限不在实例上设：调用处按需传 ``max_tokens``（结构化输出体积大，服务端默认值过小会被静默截断）。
    """
    config = await build_default_config(ModelTypeOption.CHAT)
    cached = CACHE.get(ModelTypeOption.CHAT.value)
    if cached is not None and cached[0] == config.id:
        return cached[1]
    extra = config.extra or {}
    http_client = httpx.AsyncClient(
        verify=extra.get("ca_bundle") or extra.get("verify_ssl", True),
        timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT)
    )
    instance = ChatOpenAI(
        api_key=config.api_key or "EMPTY",  # 本地端点不校验 key，但客户端要求非空
        model=config.model_name,
        base_url=config.base_url,
        temperature=extra.get("temperature", 1),
        http_async_client=http_client
    )
    CACHE[ModelTypeOption.CHAT.value] = (config.id, instance)
    logger.info(f"models: 对话模型已就绪（{config.name}：{config.base_url} / {config.model_name}）")
    return instance


async def fetch_embedding_model() -> Embeddings:
    """取嵌入模型实例（缓存按登记编号，换了默认自动重建）。

    :return: 可直接 ``aembed_documents`` 的嵌入模型

    ``check_embedding_ctx_length`` 默认关掉：开着会先用 tiktoken 把文本编码成 token 数组再发请求，
    只有 OpenAI 官方端点接受这种入参，第三方兼容端点必须直接传文本（见登记 ``extra`` 可覆盖）。
    向量维度（登记 ``dimension``）要与向量库集合一致，这里不校验——建集合时用的是它（见 plugins.qdrant）。
    """
    config = await build_default_config(ModelTypeOption.EMBEDDING)
    cached = CACHE.get(ModelTypeOption.EMBEDDING.value)
    if cached is not None and cached[0] == config.id:
        return cached[1]
    extra = config.extra or {}
    instance = OpenAIEmbeddings(
        api_key=config.api_key or "EMPTY",  # 构造阶段就校验 key，留空会让整个导入链失败
        model=config.model_name,
        base_url=config.base_url,
        check_embedding_ctx_length=extra.get("check_embedding_ctx_length", EMBEDDING_CTX_LENGTH)
    )
    CACHE[ModelTypeOption.EMBEDDING.value] = (config.id, instance)
    logger.info(f"models: 嵌入模型已就绪（{config.name}：{config.base_url} / {config.model_name}，"
                f"{config.dimension or '维度未登记'} 维）")
    return instance


async def fetch_rerank_model() -> RerankModel:
    """取重排模型的连接信息（缓存按登记编号，换了默认自动重建）。

    :return: 重排客户端要用的 :class:`RerankModel`（地址、模型名、密钥、任务说明字段）

    重排不走 langchain 的 Model 接口（各家只是普通 HTTP 端点），故只给出连接信息，
    由 :class:`plugins.agents.rerank.RerankClient` 在调用时用。
    """
    config = await build_default_config(ModelTypeOption.RERANK)
    cached = CACHE.get(ModelTypeOption.RERANK.value)
    if cached is not None and cached[0] == config.id:
        return cached[1]
    instance = RerankModel(url=config.base_url or "", name=config.model_name,
                           key=config.api_key or "", instruct_field=config.instruct_field)
    CACHE[ModelTypeOption.RERANK.value] = (config.id, instance)
    logger.info(f"models: 重排模型已就绪（{config.name}：{config.base_url} / {config.model_name}）")
    return instance


def reset_models() -> None:
    """清空缓存（实例 + 默认登记的短缓存）：库里改了某条登记的地址 / 密钥 / 默认后调一下。

    只清缓存、不关旧客户端：旧实例可能还有并发调用在跑，等它自己回收即可（进程内的 HTTP 连接池
    会随对象释放关闭）。换默认（编号变化）时严格说不用调——:func:`default_config` 取到的编号对不上
    就会重建——只是它会等 :data:`DEFAULT_TTL` 到期；要立刻生效（管理接口的写操作）就调这里。
    """
    CACHE.clear()
    DEFAULT_CACHE.clear()
    logger.info("models: 模型缓存已清空，下次取用按库里的登记重建")


class JSONOutputValidator(ABC):
    """大模型 JSON 输出的校验接口：各模型自己把模型返回的文本解析成结构化结果。

    校验失败一律抛异常（解析错误、字段缺失、类型不符），调用方据此重试（见 invoke）。
    """

    @abstractclassmethod
    def validate_output(cls, text: str) -> Any:
        pass


# 校验失败后的重试指令：连同模型的上一版输出一起作为新一轮对话回传，让它据此自我纠正
RETRY_INSTRUCTION = "上一次输出未通过校验，请按下面的错误信息修正后重新输出" \
                    "（只输出符合要求的 JSON，不要任何解释）：\n{error}"


async def invoke(model: BaseChatModel,
                 messages: list[AnyMessage],
                 validator: type[JSONOutputValidator],
                 max_retry: int = 3,
                 **kwargs) -> tuple[Any, str | None]:
    """调用大模型并校验其输出，校验不过就带上错误信息重试，超过上限返回错误文本而不抛异常。

    :param model: 大模型实例
    :param messages: 本轮对话消息
    :param validator: 输出校验器（见 JSONOutputValidator），负责把模型返回的文本解析成结构化结果
    :param max_retry: 最多尝试几次（含首次），至少为 1
    :param kwargs: 透传给 model.ainvoke 的参数，如 max_tokens、extra_body
    :return: (校验后的输出, 错误文本)：成功时错误文本为空串；达到重试上限时输出为 None，
        错误文本为最后一次的失败原因（调用异常或校验异常）

    重试覆盖两类失败：调用本身抛异常（限流、超时等），以及返回文本没过校验。
    后者会把模型的上一版输出与失败原因（见 RETRY_INSTRUCTION）追加进对话，下一轮模型据此纠正；
    追加只发生在本地副本上，不改动调用方传入的消息列表。
    调用方拿到的永远是一对返回值，不必再自己 try / except；取消等 BaseException 不在此吞掉，直接向上抛。
    """
    error = ""
    # 本地的对话副本：校验失败时往里追加「上一版输出 + 错误信息」，调用方的消息列表保持不变
    attempt_messages = list(messages)
    for attempt in range(1, max_retry + 1):
        try:
            response = await model.ainvoke(attempt_messages, **kwargs)
        except Exception as e:  # noqa: BLE001 - 失败原因统一以错误文本返回给调用方
            error = str(e)
            logger.warning(f"invoke: attempt {attempt}/{max_retry} failed: {error}")
            continue
        try:
            return validator.validate_output(response.content), None
        except Exception as e:  # noqa: BLE001 - 校验失败要回传错误信息，故与调用异常分开处理
            error = str(e)
            logger.warning(f"invoke: attempt {attempt}/{max_retry} failed: {error}")
            attempt_messages += [AIMessage(content=response.content),
                                 HumanMessage(content=RETRY_INSTRUCTION.format(error=error))]
    logger.error(f"invoke: retry limit exceeded after {max_retry} attempt(s): {error}")
    return None, error
