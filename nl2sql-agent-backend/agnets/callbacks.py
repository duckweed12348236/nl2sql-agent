"""图运行的两个回调：Token / 耗时统计，以及工具调用日志。

都挂在图运行的 ``config.callbacks`` 上（见 :meth:`agnets.RetrievalAgent.stream`）：
``RunStats`` 的 Token 走 ``on_llm_end``，只统计对话模型——嵌入走 embed 通道、重排是直接 HTTP，都不在这里；
耗时按构造时刻算墙上时间，中途暂停、子智能体往返都算在里面；另按 ``on_llm_start`` / ``on_llm_end``
配对累计「模型调用耗时」，与总耗时相减就是工具、检索、重排这些环节的开销。``ToolLogger`` 记录每次工具调用的
工具名、入参、耗时与结果规模，回调会穿透子图，所以子智能体里的工具也一并覆盖。
"""

from time import perf_counter
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from loguru import logger


def extract_usage(response: Any) -> dict[str, int] | None:
    """从一次模型调用的返回（LLMResult）里抠出 token 用量。

    :param response: ``on_llm_end`` 收到的 LLMResult
    :return: ``{"prompt": n, "completion": m, "total": t}``；没带用量时返回 None

    优先取消息上的 ``usage_metadata``（新版 langchain 的统一口径，input / output / total），
    退化到 ``llm_output["token_usage"]``（OpenAI 兼容服务的原始字段）。两条都没有（报错重试前的空返回）
    就当作没统计到，不臆造数字。
    """
    generations = getattr(response, "generations", None) or []
    if generations and generations[0]:
        message = getattr(generations[0][0], "message", None)
        usage = getattr(message, "usage_metadata", None) or {}
        if usage:
            prompt = int(usage.get("input_tokens") or 0)
            completion = int(usage.get("output_tokens") or 0)
            return {"prompt": prompt, "completion": completion,
                    "total": int(usage.get("total_tokens") or prompt + completion)}
    output = getattr(response, "llm_output", None) or {}
    usage = output.get("token_usage") or output.get("usage") or {}
    if usage:
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        return {"prompt": prompt, "completion": completion,
                "total": int(usage.get("total_tokens") or prompt + completion)}
    return None


class RunStats(BaseCallbackHandler):
    """一轮运行累计的 Token 用量与耗时。

    tokens：``prompt`` / ``completion`` / ``total`` 三项累加值，随每次模型调用更新；
    elapsed：从构造到此刻的墙上时间（秒）——每帧都带上，前端直接显示「跑了多久」；
    llm_elapsed：这一轮花在模型调用上的累计时间（秒）——与 elapsed 相减就是工具、检索、重排等其余开销。
    一轮跑完用 :meth:`summary` 打一条总账（总耗时 / 模型耗时 / token）。
    """

    # 只记数字，无需异步等待：让回调在发起事件的线程里直接跑
    run_inline = True

    def __init__(self):
        super().__init__()
        self.tokens: dict[str, int] = {"prompt": 0, "completion": 0, "total": 0}
        self._started_at = perf_counter()
        # 每次模型调用的起始时刻，按 run_id 配对；用掉的累加进 _llm_total
        self._llm_started: dict[UUID, float] = {}
        self._llm_total = 0.0

    @property
    def elapsed(self) -> float:
        """从构造到此刻的墙上时间（秒，保留两位）。"""
        return round(perf_counter() - self._started_at, 2)

    @property
    def llm_elapsed(self) -> float:
        """这一轮花在模型调用上的累计时间（秒，保留两位）。"""
        return round(self._llm_total, 2)

    def summary(self) -> str:
        """一轮的总账，一行：总耗时、其中模型调用耗时、token 用量。"""
        return (f"总耗时 {self.elapsed}s（其中模型调用 {self.llm_elapsed}s），"
                f"token 总 {self.tokens.get('total', 0)}"
                f"（prompt {self.tokens.get('prompt', 0)} / completion {self.tokens.get('completion', 0)}）")

    def on_llm_start(self, serialized: dict[str, Any], prompts: list[str], *,
                     run_id: UUID, parent_run_id: UUID | None = None, tags: list[str] | None = None,
                     metadata: dict[str, Any] | None = None, **kwargs: Any) -> None:
        """一次模型调用开始：记下起始时刻，等结束时配对算耗时。"""
        self._llm_started[run_id] = perf_counter()

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        """一次模型调用结束：把耗时记进总数，用量累加进来（取不到的用量不计，见 :func:`extract_usage`）。"""
        started = self._llm_started.pop(run_id, None)
        if started is not None:
            self._llm_total += perf_counter() - started
        usage = extract_usage(response)
        if usage is None:
            return
        for key, value in usage.items():
            self.tokens[key] = self.tokens.get(key, 0) + value

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """模型调用失败：这段等待同样算进耗时，不然总账会少算。"""
        started = self._llm_started.pop(run_id, None)
        if started is not None:
            self._llm_total += perf_counter() - started


class ToolLogger(BaseCallbackHandler):
    """工具调用的日志：调了哪个工具、参数是什么、跑了多久、结果多大。

    :param limit: 参数与结果各自最多打印多少字符（超出截断）——工具参数里可能带着整张表结构、
        结果里可能带着结果集，原样刷进日志会把文件撑爆

    一条调用打两行：开始一行带参数，结束一行带耗时与结果规模；出错时打一行 ``(error)`` 带原因。
    子智能体里的工具也会走到这里（回调穿透子图），所以整条链路的工具调用都看得到。
    """

    # 记时刻而已，无需异步等待：在发起事件的线程里直接跑，避免线程池调度干扰计时
    run_inline = True

    def __init__(self, limit: int = 200):
        super().__init__()
        self.limit = limit
        self._started: dict[UUID, tuple[str, float]] = {}

    def on_tool_start(self, serialized: dict[str, Any], input_str: str, *,
                      run_id: UUID, parent_run_id: UUID | None = None, tags: list[str] | None = None,
                      metadata: dict[str, Any] | None = None, inputs: dict[str, Any] | None = None,
                      **kwargs: Any) -> None:
        """工具开始：记下名字与起始时刻，打出参数（截断）。"""
        name = (serialized or {}).get("name") or "tool"
        self._started[run_id] = (name, perf_counter())
        logger.info(f"tool: {name}(start) {self._short(input_str or inputs)}")

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        """工具结束：算耗时，打出结果规模与开头一小段。"""
        name, started = self._started.pop(run_id, ("tool", perf_counter()))
        logger.info(f"tool: {name}(done) {(perf_counter() - started) * 1000:.0f} ms, "
                    f"{self._summary(output)}")

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """工具抛错：耗时照记，原因原样打出来。"""
        name, started = self._started.pop(run_id, ("tool", perf_counter()))
        logger.error(f"tool: {name}(error) {(perf_counter() - started) * 1000:.0f} ms, {error}")

    def _short(self, value: Any) -> str:
        """压成一行并截断：日志里一行一条，太长没意义。"""
        text = " ".join(str(value).split())
        return text if len(text) <= self.limit else f"{text[:self.limit]}…"

    def _summary(self, output: Any) -> str:
        """结果摘要：Command 只报它写回状态的键（内容太长，日志里看键名就够），其余报长度与开头。"""
        if isinstance(output, Command):
            keys = sorted(key for key in (output.update or {}) if key != "messages")
            return f"update keys={keys}"
        content = output.content if isinstance(output, ToolMessage) else output
        return f"{len(str(content))} chars: {self._short(content)}"
