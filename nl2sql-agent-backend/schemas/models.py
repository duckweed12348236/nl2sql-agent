from typing import Any

from pydantic import Field, model_validator

from models.models import ModelTypeOption
from schemas import Schema


class ModelCreate(Schema):
    """新增模型登记的请求体：用途、名称与连接信息。

    编号是雪花 ID、由服务端生成（见 models.models.ModelConfig 的 default），不在请求体里。
    按用途填不同的项：

    - ``chat``：``base_url`` 是 OpenAI 兼容端点的 base，``model_name`` 是服务端的模型标识；
      温度、超时、TLS 这类请求参数放 ``extra``（``temperature`` / ``verify_ssl`` / ``ca_bundle``）；
    - ``embedding``：``base_url`` / ``model_name`` 同上，**``dimension`` 必填**——向量维度要与向量库
      集合一致（见 plugins.qdrant 的 VectorParams），没登记维度就没法建集合；
    - ``rerank``：``base_url`` 填**完整请求地址**（各家重排路径不统一），``instruct_field`` 是任务说明
      字段名（硅基流动 ``instruction``、百炼 ``instruct``），留空表示不发。

    ``is_default`` 为真即把这一条设为该用途的默认，同用途原有的默认会被自动清掉（见
    ModelConfig.save）；同用途一条默认都没有时，运行期退到最新登记的一条（见 plugins.agents.models）。
    名称可以重复——同一模型登记多条、各带一套参数是允许的。
    """
    name: str = Field(min_length=1, max_length=128)
    type: ModelTypeOption
    base_url: str | None = Field(default=None, max_length=255)
    model_name: str = Field(min_length=1, max_length=255)
    api_key: str | None = Field(default=None, max_length=255)
    dimension: int | None = Field(default=None, ge=1)
    instruct_field: str | None = Field(default=None, max_length=64)
    extra: dict[str, Any] | None = None
    enabled: bool = True
    is_default: bool = False
    description: str | None = None

    @model_validator(mode="after")
    def _check_dimension(self) -> "ModelCreate":
        """嵌入模型必须给维度：向量库集合按它建，缺了没法用（对话 / 重排不需要）。"""
        if self.type == ModelTypeOption.EMBEDDING and not self.dimension:
            raise ValueError("嵌入模型需要填写 dimension（向量维度，与向量库集合一致）")
        return self


class ModelUpdate(Schema):
    """修改模型登记的请求体：除编号外的字段都可改，未提交的保持原值，显式提交 null 表示清空。

    改完立即生效于下一次取用：接口会清掉进程内的实例缓存（见 plugins.agents.models.reset_models），
    按新登记重建。``type`` 也可以改（把一条登记挪到另一个用途），但用途特有的项不会自动补——
    比如改成 ``embedding`` 时要自己把 ``dimension`` 填上。

    ``api_key`` 不提交表示不改（响应里按原样返回明文，见 controllers.models.model_dump）；要清空就显式
    提交空串或 null。
    """
    name: str | None = Field(default=None, min_length=1, max_length=128)
    type: ModelTypeOption | None = None
    base_url: str | None = Field(default=None, max_length=255)
    model_name: str | None = Field(default=None, min_length=1, max_length=255)
    api_key: str | None = Field(default=None, max_length=255)
    dimension: int | None = Field(default=None, ge=1)
    instruct_field: str | None = Field(default=None, max_length=64)
    extra: dict[str, Any] | None = None
    enabled: bool | None = None
    is_default: bool | None = None
    description: str | None = None
