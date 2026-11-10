"""模型管理接口：`model` 表的增删查改，以及「设为默认」。

登记口径见 :class:`models.models.ModelConfig`：一张表管 chat / embedding / rerank 三类模型，
同用途只有一个默认（``is_default``，由保存时清掉其他默认保证）。这里只管登记本身——
运行期按默认那条构造实例（见 plugins.agents.models），故**任何写操作之后都清一次实例缓存**，
下一次取用即按新登记重建，不必重启进程。

``api_key`` 按原样返回明文（管理面要能复制粘贴到别处），与业务库连接地址 ``url`` 的处理不同——
那边含库账号口令、对外不返回；模型密钥是本系统自己要用的配置，管理接口认作可信调用方。
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from loguru import logger

from agnets import RetrievalAgent
from models.models import ModelConfig, ModelTypeOption
from agnets.models import reset_models
from schemas.models import ModelCreate, ModelUpdate

router = APIRouter(prefix="/model")


async def refresh_runtime() -> None:
    """登记写完之后刷新运行期：清实例缓存，并让检索图按新的默认对话模型重编一次。

    嵌入与重排是每轮从缓存里取的，清缓存即够；对话模型在编译图时固定进图里（见
    RetrievalAgent.build 的 model 参数），故还要重编——图未装配时 rebuild 自己会跳过。
    """
    reset_models()
    await RetrievalAgent().rebuild()


async def get_model(model_id: int) -> ModelConfig:
    """按编号取一条模型登记，取不到即 404（改、删、设默认都以它为入口）。"""
    config = await ModelConfig.get_or_none(id=model_id)
    if config is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "指定的模型不存在")
    return config


def model_dump(config: ModelConfig) -> dict[str, Any]:
    """模型登记的响应：字段原样给出，``api_key`` 也是明文（管理面要能复制）。

    ``type`` 统一按字符串给（StrEnum 的值，chat / embedding / rerank），前端据此选表单；
    ``is_default`` / ``enabled`` 是布尔，列表里按用途分组展示用得上。
    """
    return {
        "id": config.id,
        "name": config.name,
        "type": ModelTypeOption(config.type).value,
        "base_url": config.base_url,
        "model_name": config.model_name,
        "api_key": config.api_key,
        "dimension": config.dimension,
        "instruct_field": config.instruct_field,
        "extra": config.extra,
        "enabled": config.enabled,
        "is_default": config.is_default,
        "description": config.description
    }


def apply_updates(config: ModelConfig, payload: ModelUpdate) -> list[str]:
    """把请求体里显式提交的字段写进实例，返回需要落库的字段名；一个字段都没提交即 422。

    exclude_unset 区分「没提交」与「提交了 null」：前者不动、后者清空；字段名与模型列一一对应，
    交 update_from_dict 做类型转换（枚举、JSON、null）。
    """
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "没有可修改的字段")
    config.update_from_dict(updates)
    return list(updates)


@router.get("")
async def list_models(model_type: ModelTypeOption | None = Query(default=None, alias="type"),
                      enabled: bool | None = Query(default=None)):
    """列出已登记的模型，可按用途与启用状态过滤。

    :param model_type: 只看某一类（chat / embedding / rerank）；不传则全部
    :param enabled: 只看启用的 / 只看停用的；不传则都看

    按用途排序（chat → embedding → rerank）、同用途内新登记的在前，便于前端直接分组展示。
    同用途的默认那一条 ``is_default`` 为真（最多一条，见 ModelConfig.save）。
    """
    query = ModelConfig.all()
    if model_type is not None:
        query = query.filter(type=model_type)
    if enabled is not None:
        query = query.filter(enabled=enabled)
    order = [ModelTypeOption.CHAT.value, ModelTypeOption.EMBEDDING.value, ModelTypeOption.RERANK.value]
    configs = sorted(await query, key=lambda x: (order.index(ModelTypeOption(x.type).value), -x.id))
    return [model_dump(x) for x in configs]


@router.get("/{model_id}")
async def retrieve_model(model_id: int):
    """查看一条模型登记（编号为雪花 ID）。"""
    return model_dump(await get_model(model_id))


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_model(payload: ModelCreate):
    """新增一条模型登记。

    名称允许重复（同名不同参数并存是常见用法）；``is_default`` 为真时同用途原有的默认自动清掉。
    新增对话 / 嵌入 / 重排任一类后，运行期下一次取用即按新登记构造实例（缓存已清）。
    """
    config = ModelConfig(**payload.model_dump())
    await config.save()
    await refresh_runtime()
    logger.info(f"create_model: {config.type} / {config.name}（{config.id}）")
    return model_dump(config)


@router.put("/{model_id}")
async def update_model(model_id: int, payload: ModelUpdate):
    """修改一条模型登记：未提交的字段保持原值，显式提交 null 表示清空。

    改完清一次实例缓存，下一次取用按新登记重建（换地址 / 换密钥 / 换模型名都即时生效）。
    ``is_default`` 提交为真时，同用途其他默认会被清掉（见 ModelConfig.save）。
    """
    config = await get_model(model_id)
    updates = apply_updates(config, payload)
    await config.save(update_fields=updates)
    await refresh_runtime()
    logger.info(f"update_model: {config.id} 改了 {updates}")
    return model_dump(config)


@router.put("/{model_id}/default")
async def set_default_model(model_id: int):
    """把这一条设为它所属用途的默认（同用途其他默认一并清掉）。

    运行期取模型看的就是默认那条（见 plugins.agents.models.default_config）：没有默认时退到最新登记的
    一条并告警，故切换默认后建议确认该用途确实有默认。设完清一次实例缓存，下一次取用即换用它。
    """
    config = await ModelConfig.set_default(model_id)
    if config is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "指定的模型不存在")
    await refresh_runtime()
    logger.info(f"set_default_model: {config.type} 的默认改为 {config.name}（{config.id}）")
    return model_dump(config)


@router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(model_id: int):
    """删除一条模型登记。

    只删登记、不动别的：若删的正是该用途的默认，该用途可能就没有默认了——运行期会退到最新登记的
    一条（并告警），要固定下来就再调一次「设为默认」。已经构造过的实例还在缓存里，删完清一次缓存，
    下一次取用会重新按库里剩下的登记挑。
    """
    config = await get_model(model_id)
    model_type = ModelTypeOption(config.type).value
    await config.delete()
    await refresh_runtime()
    logger.info(f"delete_model: {model_type} / {config.name}（{model_id}）")
