"""给 ragas 打依赖补丁：必须在导入 ragas 之前导入本模块。

ragas 0.4.3 的 ragas/llms/base.py 里硬编码了 ``from langchain_community.chat_models.vertexai import
ChatVertexAI``，只用于 isinstance 判断（见 patch_vertexai）；而 langchain-community 0.4 起该模块路径已不存在
（VertexAI 拆到了独立的 langchain-google-vertexai 包），直接 import ragas 就会 ModuleNotFoundError。
这里在导入 ragas 之前把缺失的模块补进 sys.modules，其余行为不变。

用法：任何要导入 ragas 的模块，第一件事就是
``import agnets.compat  # noqa: F401``——导入本模块即完成补丁。
"""

import sys
import types
from importlib import import_module

# langchain-community 0.4 起把 VertexAI 拆到了独立的 langchain-google-vertexai 包，该模块路径不复存在
VERTEXAI_MODULE = "langchain_community.chat_models.vertexai"


def patch_vertexai() -> None:
    """补上 ragas 需要、但当前 langchain-community 已移除的 vertexai 模块。

    ragas 0.4.3 的 ragas/llms/base.py 里硬编码了两行导入：
        from langchain_community.chat_models.vertexai import ChatVertexAI
        from langchain_community.llms import VertexAI
    它们只用于 MULTIPLE_COMPLETION_SUPPORTED 的 isinstance 判断（判断模型是否支持 n 参数多轮补全），
    从不实例化、也不会真的走 Vertex AI。但自 langchain-community 0.4 起 VertexAI 已被拆到独立的
    langchain-google-vertexai 包，chat_models.vertexai 模块路径不存在，导致 import ragas 直接
    ImportError（ragas 只声明了 langchain-community 而不限版本，装上的 0.4 与它并不兼容）。

    这里在导入 ragas 之前把缺失的模块塞进 sys.modules：只放一个空占位类，除了让 isinstance 恒为
    False（等价于「该模型不支持多轮补全」，而实际用的 ChatOpenAI 本来就在白名单里）之外不改变任何行为。
    真实模块存在时不做任何事，模块被补回后本补丁自动失效。
    """
    if VERTEXAI_MODULE in sys.modules:
        return
    try:
        import_module(VERTEXAI_MODULE)
    except ImportError:
        pass
    else:
        return

    module = types.ModuleType(VERTEXAI_MODULE)
    module.ChatVertexAI = type("ChatVertexAI", (), {})
    sys.modules[VERTEXAI_MODULE] = module
    # 同时挂到父包上，保证按属性访问 langchain_community.chat_models.vertexai 也能命中
    import langchain_community.chat_models as chat_models
    chat_models.vertexai = module


# 导入本模块即完成补丁：ragas 相关模块必须在补丁之后导入（见模块文档）
patch_vertexai()
