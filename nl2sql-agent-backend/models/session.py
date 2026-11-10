"""会话模型：一个会话绑定一个 thread_id，下面挂多轮问答，每轮记下用户输入与该轮的产出。

会话编号（id）与 thread_id 都是雪花 ID：前者是本系统里的会话标识，后者是这一轮检索在 langgraph 里的
会话编号（见 plugins.agents.graph.Retriever.invoke，传进去时转成十进制字符串），两者一一对应。
本模型存的是业务侧的记录（用户输入、产出），会话状态本身仍在 checkpointer 里（见
plugins.agents.checkpointers），两者按同一个 thread_id 对齐。
本模块的类由 config 按模块名登记进 Tortoise（见 config.TORTOISE_ORM_CONFIG 的 models 列表），
因此本文件内定义即完成注册。
"""

from typing import Any

from tortoise.fields import BigIntField, CASCADE, DatetimeField, ForeignKeyField, IntField, JSONField, \
    ReverseRelation, TextField

from models import Model
from plugins.snowflake import Snowflake


class Session(Model):
    """一个会话：绑定一个 thread_id。

    id 与 thread_id 都用雪花 ID（见 plugins.snowflake；字段为 BigIntField 且 generated=False，取值在
    Python 侧由 default 生成，同 models.dataset.Dataset）。两者分工不同：id 是本系统的会话编号，只在内部与
    轮次的外键上用；thread_id 是检索那边的会话编号，checkpointer 按它存状态、客户端带着它把下一轮接回
    同一个会话，故它唯一（一个会话绑定一个 thread_id）。

    created_at 是会话创建时间（写库时自动填，见 DatetimeField 的 auto_now_add；config 开了 use_tz，
    取到的是带时区的时间）。

    tokens 是这条会话累计的 Token 用量：每轮跑完把该轮的用量（见 Turn.tokens）加上去，用来回答
    「这个会话一共花了多少」。

    rounds 是这条会话下的全部轮次（见 Turn），按 round 升序取用。
    """
    id = BigIntField(pk=True, generated=False, default=Snowflake.get_id, description="会话编号（雪花 ID）")
    thread_id = BigIntField(unique=True, description="会话的 thread_id（雪花 ID，与 langgraph 的 thread_id 相同）")
    created_at = DatetimeField(auto_now_add=True, description="创建时间")
    tokens = BigIntField(default=0, description="会话累计 Token 用量（各轮之和）")
    rounds: ReverseRelation["Turn"]

    class Meta:
        table = "session"


class Turn(Model):
    """会话里的一轮：用户输入 + 该轮的产出（JSON）。

    id 用雪花 ID（同 Dataset，见其说明）：趋势递增，按 id 排序即「先问的在前」；
    round 是这一轮在会话里的序号（从 1 开始），(session_id, round) 唯一——同一会话不会出现两个第 3 轮；
    created_at 是这一轮的开始时间（写库时自动填，见 DatetimeField 的 auto_now_add）；
    input 是用户这一轮的原始输入，原样保存、不做改写；
    outputs 是这一轮的产出，按 JSON 存成一个字典：模型输出文本（messages，按产生顺序）、耗时（elapsed，
    秒）、通过校验的 SQL（sql）、结果集（data）与最终候选表（tables）——取自最后一帧的对应字段，
    整轮跑完由接口写回（见 controllers.retrieval.save_outputs）；
    tokens 是这一轮的 Token 用量（prompt + completion，取自帧里的累计值，见帧上的 tokens）——跑完由接口写回，
    同时累加到 Session.tokens 上。
    会话删掉时轮次随外键级联删除（CASCADE），不留孤儿。
    """
    id = BigIntField(pk=True, generated=False, default=Snowflake.get_id, description="轮次编号（雪花 ID）")
    session = ForeignKeyField("models.Session", "rounds", CASCADE, description="所属会话编号")
    session_id: int
    round = IntField(description="轮次序号（同一会话内从 1 开始）")
    created_at = DatetimeField(auto_now_add=True, description="创建时间")
    input = TextField(description="用户输入")
    outputs = JSONField[dict[str, Any]](description="该轮产出（JSON：messages / elapsed / sql / data / tables）")
    tokens = IntField(default=0, description="该轮 Token 用量（prompt + completion）")

    class Meta:
        table = "turn"
        unique_together = ("session_id", "round")
