from dataclasses import dataclass
from http import HTTPStatus
from typing import Sequence

import httpx
from loguru import logger

from plugins import Plugin
from agnets.models import RerankModel

# 单次请求超时（秒）
TIMEOUT = 30
# 单次请求的文档数上限：取各服务商中较严的取值（qwen3-rerank 为 500），超限时服务端会返回 400
MAX_DOCUMENTS = 500


@dataclass()
class RerankResult:
    """单条文档的重排结果。

    index：文档在传入列表中的原始下标；score：与 query 的相关度，取值 0~1。
    """
    index: int
    score: float

    def __repr__(self) -> str:
        return f"{self.index}:{self.score:.6f}"


class RerankClient(Plugin):
    """重排客户端：不绑定具体模型，请求地址、模型名与密钥都由调用方随 RerankModel 传入。

    请求体为扁平结构（与 gte-rerank 的 input/parameters 嵌套结构不同）：

        POST {model.url}
        {"model": "Qwen/Qwen3-Reranker-8B", "query": "...", "documents": ["..."]}

    响应中的 results 直接位于顶层，按 relevance_score 降序排列。

    注意：relevance_score 是本次请求内的相对分数，只能用于本次请求内排序，
    不可跨请求比较；故文档数超过 MAX_DOCUMENTS 时不做自动分批，由调用方先裁剪。
    """

    def __init__(self):
        # 地址与鉴权随调用传入，客户端本身不持有任何服务商信息
        self.client = httpx.AsyncClient(timeout=TIMEOUT)

    async def rerank(self,
                     query: str,
                     documents: Sequence[str],
                     model: RerankModel,
                     instruct: str | None = None) -> list[RerankResult]:
        """按与 query 的相关度重排 documents，返回结果即已按 score 降序排好序。

        model：本次调用使用的重排模型（完整请求地址 + 模型名 + 密钥）；
        instruct：任务类型说明，用于引导排序策略，按 model.instruct_field 指定的字段名发出
            （硅基流动认 instruction、百炼认 instruct，字段名不对会被服务端直接忽略）。
        """
        if not query or not documents:
            return []
        if len(documents) > MAX_DOCUMENTS:
            logger.warning(f"rerank: {len(documents)} document(s) exceeds the limit of {MAX_DOCUMENTS}")

        payload = {"model": model.name, "query": query, "documents": list(documents)}
        # 任务说明按各家的字段名发出，字段名对不上时服务端不会报错、只会忽略
        if instruct is not None and model.instruct_field:
            payload[model.instruct_field] = instruct

        response = await self.client.post(model.url, json=payload,
                                          headers={"Authorization": f"Bearer {model.key}"})
        data = response.json()
        if response.status_code != HTTPStatus.OK:
            logger.error(f"rerank: failed, status={response.status_code}, "
                         f"code={data.get('code')}, message={data.get('message')}")
            raise RuntimeError(f"rerank failed: {data.get('code')} - {data.get('message')}")

        results = [RerankResult(index=x["index"], score=x["relevance_score"])
                   for x in data.get("results") or []]
        # 服务端已按 relevance_score 降序返回，本地再排一次保证结果有序可取
        results.sort(key=lambda x: x.score, reverse=True)
        logger.info(f"rerank: model={model.name}, {len(documents)} document(s) -> {len(results)} result(s)")
        return results

    async def register(self) -> None:
        pass

    async def close(self) -> None:
        await self.client.aclose()
