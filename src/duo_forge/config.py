"""配置与模型工厂。

搬运自 camel-fitness-nutrition-duo，基本未改：这里没有什么领域绑定的东西。
唯一的改动是**多了一个入口** `build_author_backend`——生成人格与议题要用低温，
和对谈用的温度不是一回事（见下面两个常量的说明）。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "outputs"
#: 生成出来的蓝图（人格 + 议题）落在这里。跑一次 `forge` 就多一份。
BLUEPRINT_DIR = ROOT / "blueprints"

# 读 .env（不存在也不报错，模拟模式不需要 key）
load_dotenv(ROOT / ".env")

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

#: 单次请求的超时（秒）。默认没有超时——网络一出问题就是**无限期挂住**，
#: 表现是计时器在走、一句发言都没有，比直接失败难查得多。
CALL_TIMEOUT = 180.0

#: 对谈用的温度。专家们要吵出有信息量的东西，需要一点发散。
DEBATE_TEMPERATURE = 0.75

#: 生成人格与议题用的温度。这一步的成品是**要被 json.loads 解析的**，
#: 发散在这里只会帮倒忙——温度一高，模型就爱在 JSON 前后加一句「好的，以下是」。
AUTHOR_TEMPERATURE = 0.3


class MissingApiKey(RuntimeError):
    """没配 key 时给出人话提示，而不是让 openai 抛一长串栈。"""


def _real_backend(*, temperature: float):
    """走 OpenAI 兼容协议的真实后端。换 Kimi / 通义 / Ollama 只改 .env 两行。"""
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise MissingApiKey(
            "没有找到 DEEPSEEK_API_KEY。\n"
            "  1) cp .env.example .env\n"
            "  2) 把 key 填进 .env\n"
            "  想先不花钱跑通流程的话，加上 --mock：\n"
            "  uv run duo-forge forge --purpose \"...\" --mock"
        )

    from camel.models import ModelFactory
    from camel.types import ModelPlatformType

    from .backends import OfflineTokenCounter

    return ModelFactory.create(
        model_platform=ModelPlatformType.OPENAI_COMPATIBLE_MODEL,
        model_type=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL),
        api_key=api_key,
        url=os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
        model_config_dict={"temperature": temperature},
        # 必须自己给 token_counter，不能让 camel 去建默认的 OpenAITokenCounter：
        # 它要用 tiktoken 加载 o200k_base 词表，本地没有就联网下载，而那个域名
        # （openaipublic.blob.core.windows.net）在国内连不上——于是建 ChatAgent
        # 那一步就**无限期挂住**，模型调用根本没发出去。症状是「计时器在走，
        # 一句发言都没有，也不报错」，而且 tiktoken 把词表缓存在 /tmp/data-gym-cache，
        # 重启一次就复发，看着像「昨天还好好的今天就不行」。
        token_counter=OfflineTokenCounter(),
        timeout=CALL_TIMEOUT,
    )


def build_backend(*, mock: bool = False, temperature: float = DEBATE_TEMPERATURE):
    """对谈用的后端。"""
    if mock:
        from .backends import ScriptedBackend

        return ScriptedBackend()
    return _real_backend(temperature=temperature)


def build_author_backend(*, mock: bool = False, temperature: float = AUTHOR_TEMPERATURE):
    """生成人格与议题用的后端。

    mock 时给的是 `ScriptedAuthorBackend`——它吐的是**合法的 JSON**，
    好让「解析 → 校验 → 建 dataclass」这条链路在离线状态下也真的被走一遍。
    如果 mock 直接把人格和议题硬编码出来，这条链路上有 bug 就永远发现不了。
    """
    if mock:
        from .backends import ScriptedAuthorBackend

        return ScriptedAuthorBackend()
    return _real_backend(temperature=temperature)
