"""评测数据集模型：出题产生的数据集、数据集条目，以及在其上跑评测得到的结果。

与 models.meta 分开：meta 放的是元数据（业务库、表、字段、指标），这里放的是评测这条业务线
（评估工具的产出，见 plugins.agents.evaluation）。本模块的类由 config 按模块名登记进 Tortoise
（见 config.TORTOISE_ORM_CONFIG 的 models 列表），因此本文件内定义即完成注册。
"""

from tortoise.fields import BigIntField, CASCADE, CharField, FloatField, ForeignKeyField, JSONField, \
    ReverseRelation, TextField

from models import Model
from plugins.snowflake import Snowflake


class Dataset(Model):
    """评测数据集：一批「问题 + 参考 SQL」，由出题工具生成（见
    plugins.agents.evaluation.nodes.generate_dataset）。

    id 用雪花 ID：全局唯一、趋势递增的有符号 64 位整数（见 plugins.snowflake），因此字段为
    BigIntField 且 generated=False——不入库自增，取值在 Python 侧由 default 生成
    （Tortoise 构造实例时对缺省字段调用可调用的 default，save 与 bulk_create 都走这条路）。

    thread_id 是出题这一轮的会话编号（同样用雪花 ID），只作为来源标记：把同一轮出题及其历次评测
    （Dataset.results）串在一起，便于追溯；评测本身按数据集编号取内容，不依赖它。
    会话编号由服务端生成，重新出题若沿用同一编号，这里不限制重复。

    reference_contexts 记下出题与判定所用的库上下文（出题所用的表及其字段与取值，即调用方指定表编号、由
    plugins.agents.evaluation.nodes.build_table_infos 按编号组装的 TableInfo 列表）：
    判定两条 SQL 是否等效要看字段的业务含义，存下来既让数据集可复现，
    也让后续评测不必依赖当时的元数据状态。
    """

    id = BigIntField(pk=True, generated=False, default=Snowflake.get_id, description="数据集编号（雪花 ID）")
    thread_id = BigIntField(description="会话编号（雪花 ID），用于定位会话")
    name = CharField(max_length=128, description="数据集名称")
    reference_contexts = JSONField[list[dict] | None](null=True, description="出题与判定所用的库上下文（表、字段与取值）")
    error = TextField(null=True, description="数据集生成失败的原因")
    items: ReverseRelation["DatasetItem"]
    results: ReverseRelation["Result"]

    class Meta:
        table = "dataset"


class DatasetItem(Model):
    """数据集条目：一个自然语言问题与它对应的参考 SQL。

    问题与参考 SQL 都用 TextField：问题可能很长，参考 SQL 更长，长度上不设上限。
    参考 SQL 是出题生成、再经 EXPLAIN 校验与纠正后的最终版本（见 plugins.agents.evaluation.nodes.generate_dataset）。
    """

    id = BigIntField(pk=True, generated=False, default=Snowflake.get_id, description="条目编号（雪花 ID）")
    question = TextField(description="自然语言问题")
    reference = TextField(description="参考 SQL")
    dataset = ForeignKeyField("models.Dataset", "items", CASCADE, description="所属数据集编号")
    dataset_id: int

    class Meta:
        table = "dataset_item"


class Result(Model):
    """一次评测结果：拿某个数据集跑一遍检索智能体，逐条与参考 SQL 判定等效性。

    mean 是这一轮的平均得分（SQL 等效率）：逐条得分（ResultItem.score）取平均，见
    plugins.agents.evaluation.nodes.evaluate_dataset。report 是对这一轮结果的解读报告：平均分之外，
    模型给出的「哪里答得不好、大致因为什么、可以怎么改」的成段说明，留空表示这一轮没有生成
    （如还没跑解读、或那一轮失败）。error 是整轮失败的原因：失败时没有条目、mean 记 0，原因留在这里。

    retrieved_contexts 是这一轮检索智能体检索到的表信息（表、字段与取值，即检索图重排后最终用于生成 SQL 的
    table_infos，逐条转成字典）：逐条问题检索到的表按问题顺序依次排开，不去重——同一张表被多条问题检索到就
    存多份，各条问题检索到什么就是什么；只有产出了校验无误的 SQL 的条目才有表信息（见检索图的 eval），检索
    失败的那条为空。这里因此没有逐条归属的标记，逐条的判定见 ResultItem。它与数据集上的 reference_contexts
    相对：那份是出题与判定所用的库上下文，这份是答题时真正检索到的片段，存下来便于回看「答错是不是因为该有的
    表没检索到」。
    """

    id = BigIntField(pk=True, generated=False, default=Snowflake.get_id, description="评测结果编号（雪花 ID）")
    mean = FloatField(default=0.0, description="平均得分（逐条等效性得分的均值）")
    report = TextField(null=True, description="评估结果的解读报告")
    retrieved_contexts = JSONField[list[dict] | None](null=True,
                                                      description="检索智能体检索到的表信息（表、字段与取值）")
    error = TextField(null=True, description="本轮评测失败的原因")
    dataset = ForeignKeyField("models.Dataset", "results", CASCADE, description="被评测的数据集编号")
    dataset_id: int
    items: ReverseRelation["ResultItem"]

    class Meta:
        table = "result"


class ResultItem(Model):
    """评测结果里的单条：某个问题的参考答案、检索生成的 SQL 与判定结果。

    reference 是该问题的参考答案（SQL），落库时直接从数据集条目抄过来：判定与展示都是「这两条
    SQL 是不是一回事」，存下参考 SQL 就不必再回到条目上取，也就不需要指向 DatasetItem 的外键了
    （同一数据集里参考 SQL 不重复，对着它就能找回是哪条问题）。

    answer 是检索智能体为该问题生成的 SQL，没生成或没通过校验（见检索图的 eval）时为 None；score 为等效性得分
    （1.0 等效、0.0 不等效或未生成 SQL）；reason 是得分理由，未生成 SQL 或指标调用失败时写失败
    原因——口径与评估工具逐条评测的返回值一致（见 plugins.agents.evaluation.nodes.evaluate_item）。
    """

    id = BigIntField(pk=True, generated=False, default=Snowflake.get_id, description="结果条目编号（雪花 ID）")
    reference = TextField(description="参考答案（SQL）")
    answer = TextField(null=True, description="检索智能体生成的 SQL")
    score = FloatField(default=0.0, description="等效性得分：等效 1.0、不等效或未生成 SQL 为 0.0")
    reason = TextField(null=True, description="得分理由；未生成 SQL 或失败时是失败原因")
    result = ForeignKeyField("models.Result", "items", CASCADE, description="所属评测结果编号")
    result_id: int

    class Meta:
        table = "result_item"
