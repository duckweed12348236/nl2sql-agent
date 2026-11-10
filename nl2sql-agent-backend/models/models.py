"""模型管理：把系统用到的三类模型（Chat / Embedding / Rerank）登记成一份可管理的清单。

一张表管三类模型，靠 ``type`` 区分：三者的共有信息基本一致（显示名、服务地址、模型标识、密钥、
是否启用、是否默认），差异只在少数几项（Embedding 有向量维度、Rerank 有任务说明字段名），
分三张表会让「列个清单 / 切默认 / 停用某个」这类操作各写一遍。专属字段用可空列承载，
取用时按 ``type`` 判断该看哪几个（见 :meth:`ModelConfig.options`）。

与运行期的关系：模型实例由 :mod:`plugins.agents.models` **按本表构造**——每轮开跑时取该用途的
默认登记（``is_default``）建实例并缓存在进程内，故改完这里的地址 / 密钥 / 模型名后调
``reset_models()``（或换掉默认那条）即生效，不再碰 config 与 pyproject.toml。

名称可以重复：同一个模型登记多条、每条一套参数（不同超时、不同温度、指向不同部署）是常见做法，
故不做唯一约束。唯一受限的是**默认**——``is_default`` 每种用途只允许一条为真，由 :meth:`ModelConfig.save`
在写之前把同用途的其他默认清掉来保证（Postgres 的部分唯一索引 Tortoise 建不出来，故放在保存时做）。

本模块的类由 config 按模块名登记进 Tortoise（见 config.TORTOISE_ORM_CONFIG 的 models 列表），
因此本文件内定义即完成注册。
"""

from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from tortoise.fields import BigIntField, BooleanField, CharEnumField, CharField, IntField, JSONField, TextField

from models import Model
from plugins.snowflake import Snowflake


class ModelTypeOption(StrEnum):
    """模型的用途分类：对话、向量化、重排。

    取值与运行期取用的三种实例一一对应（见 plugins.agents.models 的 chat_model / embeddings / reranker，
    每轮开跑时按各自的默认登记取一条）。
    """

    CHAT = "chat"
    EMBEDDING = "embedding"
    RERANK = "rerank"


class ModelConfig(Model):
    """一条模型登记：某个用途下的某个模型，以及连它要用的信息。

    按 ``type`` 取用不同的字段：

    - ``chat``：``base_url`` 填 OpenAI 兼容端点的 base（如 vLLM 的 ``http://localhost:8000/v1``），
      ``model_name`` 是服务端的模型标识；温度、``max_tokens``、超时这类请求参数放 ``extra``；
    - ``embedding``：``base_url`` / ``model_name`` 同上，另需 ``dimension``——向量维度，建 Qdrant 集合
      时要与之一致（当前各集合都是 1024，对应 bge-large-zh-v1.5，见 plugins.qdrant 的 VectorParams）；
      ``extra`` 里可放 ``check_embedding_ctx_length`` 这类开关（第三方兼容端点必须关掉）；
    - ``rerank``：OpenAI 官方协议没有重排端点，各家路径不统一（百炼 ``{base}/compatible-api/v1/reranks``、
      硅基流动 ``{base}/v1/rerank``），故 ``base_url`` 直接填**完整请求地址**；``instruct_field`` 是任务说明
      在请求体里的字段名（硅基流动 ``instruction``、百炼 ``instruct``），留空表示不发该字段。

    ``api_key`` 明文存在元数据库里（内网部署的本地配置，管理接口按原样返回，便于复制到前端 / 别处）；
    ``enabled`` 为假表示暂不使用（保留登记，便于切回来）。

    名称不唯一：同一模型可以登记多条（换个名字或同名都行），各自一套参数，便于并存与快速切换；
    唯一受限的是 ``is_default``——每种用途只允许一条为真，见模块说明与 :meth:`save`。
    """

    id = BigIntField(pk=True, generated=False, default=Snowflake.get_id, description="模型编号（雪花 ID）")
    name = CharField(max_length=128, description="显示名称（可重复，如「本地千问」）")
    type = CharEnumField(ModelTypeOption, description="用途分类：chat / embedding / rerank")
    base_url = CharField(max_length=255, null=True, description="服务地址：Chat / Embedding 填 base，Rerank 填完整地址")
    model_name = CharField(max_length=255, description="服务方的模型标识（如 Qwen3-32B、BAAI/bge-large-zh-v1.5）")
    api_key = CharField(max_length=255, null=True, description="该服务的 API Key（本地端点可为空）")
    dimension = IntField(null=True, description="向量维度（仅 embedding 有意义，与向量库集合的维度对齐）")
    instruct_field = CharField(max_length=64, null=True,
                               description="任务说明在请求体里的字段名（仅 rerank 有意义，空表示不发）")
    extra = JSONField[dict[str, Any] | None](null=True,
                                             description="其余请求参数（温度、max_tokens、超时、TLS 等，按用途自定）")
    enabled = BooleanField(default=True, description="是否启用：为假时保留登记但暂不使用")
    is_default = BooleanField(default=False, description="是否为该用途的默认模型（每个用途只有一条为真）")
    description = TextField(null=True, description="备注：这条模型用在哪些环节、有什么限制")

    class Meta:
        table = "model"

    async def save(self, using_db: Any = None, update_fields: Iterable[str] | None = None,
                   force_create: bool = False, force_update: bool = False) -> None:
        """保存：默认是独占的，写之前把同用途的其他默认清掉。

        :param using_db: 走哪个连接（Tortoise 原样透传），不传用默认连接
        :param update_fields: 只写这些列（Tortoise 原样透传）
        :param force_create: 强制按新建写（Tortoise 原样透传）
        :param force_update: 强制按更新写（Tortoise 原样透传）

        「每种用途只有一个默认」在库里没有唯一约束（Postgres 的部分唯一索引 Tortoise 建不出来），
        故在保存时保证：这一条要当默认，先把同 ``type`` 其他条目的 ``is_default`` 置假，再写自己——
        切换默认因此只需给新的一条保存一次，不必调用方自己成对更新。
        绕过本方法写库的路径（``bulk_create``、``QuerySet.update``、原生 SQL）不受这条保证约束，
        批量导入多条默认会留下脏数据；需要时用 :meth:`set_default` 收口。
        """
        if self.is_default:
            await ModelConfig.filter(type=self.type, is_default=True).exclude(id=self.id) \
                .update(is_default=False)
        await super().save(using_db=using_db, update_fields=update_fields,
                           force_create=force_create, force_update=force_update)

    @classmethod
    async def set_default(cls, model_id: int) -> "ModelConfig | None":
        """把某条设为它那一类的默认（同用途的其他默认一并清掉）。

        :param model_id: 模型编号（ModelConfig.id）
        :return: 设好默认的那条；编号不存在时返回 None

        与「直接改 ``is_default`` 再 :meth:`save`」等价，只是省得调用方自己查一条再存一次；
        启用状态不在这里校验——停用的模型也能被设为默认，取用方按 ``enabled`` 自行决定是否回落到别的。
        """
        config = await cls.get_or_none(id=model_id)
        if config is None:
            return None
        config.is_default = True
        await config.save()
        return config

    def build_options(self) -> dict[str, Any]:
        """把链接参数整理成一份字典：按用途取该用的那几项，``extra`` 里的键平铺进来（不覆盖同名项）。

        :return: ``{base_url, model_name, api_key, ...extra}``，另带 ``dimension`` / ``instruct_field``
            中与该用途相关的那一项；空值不下发（端点、密钥可能是空串）

        运行期按它构造实例即可（对话模型见 ChatOpenAI、嵌入见 OpenAIEmbeddings、重排见
        plugins.agents.models.RerankModel），不必各处再判 ``type``。
        """
        options: dict[str, Any] = {}
        if self.base_url:
            options["base_url"] = self.base_url
        options["model_name"] = self.model_name
        if self.api_key:
            options["api_key"] = self.api_key
        if self.type == ModelTypeOption.EMBEDDING and self.dimension:
            options["dimension"] = self.dimension
        if self.type == ModelTypeOption.RERANK and self.instruct_field:
            options["instruct_field"] = self.instruct_field
        for key, value in (self.extra or {}).items():
            options.setdefault(key, value)
        return options
