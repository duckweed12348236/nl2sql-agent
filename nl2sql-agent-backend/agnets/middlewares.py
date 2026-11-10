"""智能体中间件：在**每次模型调用**时按库里的默认登记选对话模型。

为什么需要它：``create_deep_agent(model=...)`` 把模型固定在编译好的图里，换默认模型就得重编图
（原先靠 ``RetrievalAgent.rebuild``）。有了这个中间件，图编译时给的那份只是个兜底——
真正用哪条由 ``model`` 表决定（``type=chat`` 且 ``is_default``，见 plugins.agents.models.default_config），
在管理接口里切换默认后最迟 :data:`plugins.agents.models.DEFAULT_TTL` 秒生效，不必重编、不必重启。

取用走的是带缓存的那套（``chat_model`` 按编号缓存实例、默认登记另有短 TTL 缓存），故这里逐次调用
只在缓存过期时查一次库，不会给每个 token 都加一次查询。
"""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from loguru import logger

from agnets.models import CACHE, ModelTypeOption, fetch_chat_model


class DefaultModelMiddleware(AgentMiddleware):
    """把每次模型调用改写成「库里的默认对话模型」。

    异步链路（图的正常跑法，``ainvoke`` / ``astream``）走 :meth:`awrap_model_call`：取一次默认模型，
    取到的与请求里那个不同就 ``request.override(model=...)`` 换掉，再交给下一个处理器。

    同步链路（``invoke``）拿不到库（默认模型要查表，同步函数里没法 await），只能退而求其次用
    **已经构造过的实例**（见 :data:`plugins.agents.models.CACHE`）；缓存里没有就原样放行、用图里那份兜底。
    本项目的检索链路全程异步，这条路只是给 langgraph 的同步入口留个不炸的兜底。
    """

    name = "default_model"

    async def awrap_model_call(self, request: ModelRequest[Any],
                              handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]]
                              ) -> ModelResponse[Any]:
        """每次模型调用前换成本轮该用的对话模型。

        :param request: 这次的模型请求（含消息、工具、绑定参数与当前模型）
        :param handler: 继续往下执行的处理器
        :return: 模型响应

        取不到默认模型（库里一条 chat 登记都没有、或库暂时不可用）时不拦路：记一条告警、原样交给
        handler，让它用编译时那份兜底模型跑——总比整个问答挂掉好。
        """
        try:
            model = await fetch_chat_model()
        except Exception as error:  # noqa: BLE001 - 取不到就退回图里那份，别把整轮打断
            logger.warning(f"middleware: 取默认对话模型失败，沿用图里那份：{error}")
            return await handler(request)
        if model is not request.model:
            request = request.override(model=model)
        return await handler(request)

    def wrap_model_call(self, request: ModelRequest[Any],
                        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]]) -> ModelResponse[Any]:
        """同步链路的兜底：用缓存里已构造的对话模型（不查库），没有就原样放行。

        :param request: 这次的模型请求
        :param handler: 继续往下执行的处理器
        :return: 模型响应
        """
        cached = CACHE.get(ModelTypeOption.CHAT.value)
        if cached is not None:
            return handler(request.override(model=cached[1]))
        return handler(request)
