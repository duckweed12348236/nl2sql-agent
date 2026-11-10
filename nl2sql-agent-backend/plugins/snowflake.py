# Twitter's Snowflake algorithm implementation which is used to generate distributed IDs.
# https://github.com/twitter-archive/snowflake/blob/snowflake-2010/src/main/scala/com/twitter/service/snowflake/IdWorker.scala

import time

from config import SNOWFLAKE_WORKER_ID, SNOWFLAKE_DATACENTER_ID, SNOWFLAKE_SEQUENCE
from plugins import Singleton

# 64位ID的划分
WORKER_ID_BITS = 5
DATACENTER_ID_BITS = 5
SEQUENCE_BITS = 12

# 最大取值计算
MAX_WORKER_ID = -1 ^ (-1 << WORKER_ID_BITS)  # 2**5-1 0b11111
MAX_DATACENTER_ID = -1 ^ (-1 << DATACENTER_ID_BITS)

# 移位偏移计算
WOKER_ID_SHIFT = SEQUENCE_BITS
DATACENTER_ID_SHIFT = SEQUENCE_BITS + WORKER_ID_BITS
TIMESTAMP_LEFT_SHIFT = SEQUENCE_BITS + WORKER_ID_BITS + DATACENTER_ID_BITS

# 序号循环掩码
SEQUENCE_MASK = -1 ^ (-1 << SEQUENCE_BITS)

# Twitter元年时间戳
TWEPOCH = 1288834974657


class InvalidSystemClock(Exception):
    """
    时钟回拨异常
    """
    pass


class Snowflake(Singleton):
    """
    用于生成IDs
    """

    worker_id = SNOWFLAKE_WORKER_ID
    datacenter_id = SNOWFLAKE_DATACENTER_ID
    sequence = SNOWFLAKE_SEQUENCE
    last_timestamp = -1  # 上次计算的时间戳

    @classmethod
    def _gen_timestamp(cls) -> int:
        """
        生成整数时间戳
        :return:int timestamp
        """
        return int(time.time() * 1000)

    @classmethod
    def _til_next_millis(cls, last_timestamp: int) -> int:
        """
        等到下一毫秒
        """
        timestamp = cls._gen_timestamp()
        while timestamp <= last_timestamp:
            timestamp = cls._gen_timestamp()
        return timestamp

    @classmethod
    def get_id(cls) -> int:
        """
        获取新ID
        :return:int ID
        """
        timestamp = cls._gen_timestamp()

        # 时钟回拨
        if timestamp < cls.last_timestamp:
            raise InvalidSystemClock

        if timestamp == cls.last_timestamp:
            cls.sequence = (cls.sequence + 1) & SEQUENCE_MASK
            if cls.sequence == 0:
                timestamp = cls._til_next_millis(cls.last_timestamp)
        else:
            cls.sequence = 0

        cls.last_timestamp = timestamp

        return ((timestamp - TWEPOCH) << TIMESTAMP_LEFT_SHIFT) | (cls.datacenter_id << DATACENTER_ID_SHIFT) | \
               (cls.worker_id << WOKER_ID_SHIFT) | cls.sequence
