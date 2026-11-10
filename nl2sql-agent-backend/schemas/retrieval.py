from pydantic import Field

from schemas import Schema


class Question(Schema):
    """提问的请求体：这一轮的输入、检索的业务库编号，以及（可选的）会话编号。

    session_id 是「接着上一轮跑」的定位方式（见 models.session.Session.id）：检索链路在指标口径对不上时会
    停下来反问（帧里带 clarification），客户端把用户的回答作为 content、带上同一个 session_id 再提交一次，
    图就从停住的地方继续——会话到 thread_id 的对应由服务端按 session_id 查，客户端不必关心 thread_id。
    不传 session_id 表示开一轮新的对话：服务端新建一条会话（thread_id 现生成一个雪花 ID），并在响应头
    X-Session-Id 与每帧的 session_id 里带回来。
    """
    content: str = Field(min_length=1)
    database_id: int
    session_id: int | None = Field(default=None, ge=1)
