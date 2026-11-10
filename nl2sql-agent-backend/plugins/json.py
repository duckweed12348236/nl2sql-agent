import orjson

from typing import Any, Callable


def load(text: str | bytearray | bytes) -> Any:
    return orjson.loads(text)


def dump(obj: Any, default: Callable[[Any], Any] | None = None) -> str:
    """序列化为 JSON 文本。

    default 用于兜底 orjson 原生不支持的类型（如 Decimal），签名与 json.dumps 一致；
    datetime/date/uuid 等 orjson 原生已支持，无需走 default。
    """
    return orjson.dumps(obj, default=default).decode()
