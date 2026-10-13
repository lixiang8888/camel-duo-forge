"""离线模拟后端。

搬运自 camel-fitness-nutrition-duo，只加了一个类（`ScriptedAuthorBackend`）。

本模块还提供一个 `OfflineTokenCounter`，**真实模式也在用**（见 config._real_backend）：
camel 默认的 token 计数器要联网下载 tiktoken 词表，在国内会无限期挂住。

目的：让你在还没有 API key 的情况下，也能把整条链路跑通
（生成人格 → 生成议题 → 建 agent → 多轮 step → 落盘 → 整理成文），
确认是「模型没配」而不是「代码有 bug」。

它不是假的模型调用：CAMEL 的 ModelPlatformType.STUB 只会回一句 "Lorem Ipsum"，
没法验证多轮对话。这里通过继承 BaseModelBackend 实现一个真正的后端，
回复里会带上上一轮内容的摘录，方便你肉眼确认对话确实在按顺序流转。

注意：模拟模式产出的一切都是占位内容，没有任何参考价值。
"""

from __future__ import annotations

import json
from typing import Any, List, Optional, Type

from camel.models import BaseModelBackend
from camel.utils import BaseTokenCounter
from camel.types import ModelType
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice

MOCK_BANNER = "[离线模拟回复 · 非真实内容]"


class OfflineTokenCounter(BaseTokenCounter):
    """粗糙的字符数估算。只为满足接口，不追求准确。

    **这个类同时在两处用**：离线模拟后端，以及 config 造真实后端时。
    真实模式也要给，是因为 camel 默认会去建 `OpenAITokenCounter`，而它要用 tiktoken
    加载 o200k_base 词表——本地没有就去 openaipublic.blob.core.windows.net 下载。
    那个域名在国内连不上，于是**无限期挂住**：不抛异常、不超时、日志里什么都没有。
    tiktoken 默认把词表缓存在 `/tmp/data-gym-cache`，重启一次就没了，所以这毛病
    会「昨天还好好的，今天就不行」。

    估算值偏小（字符数除以 2），对本项目的对话长度绰绰有余——
    DeepSeek 的上下文是 64k 量级，而一个议题的对话也就几千 token。
    """

    def count_tokens_from_messages(self, messages: List[Any]) -> int:
        total = 0
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else message
            total += len(str(content))
        return total // 2 + 1

    def encode(self, text: str) -> List[int]:
        return list(text.encode("utf-8"))

    def decode(self, token_ids: List[int]) -> str:
        return bytes(token_ids).decode("utf-8", errors="ignore")


def _last_excerpt(messages: List[Any]) -> str:
    """找出最后一条非 system 消息，摘一句出来，证明轮次确实在推进。"""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") in ("user", "assistant"):
            return str(message.get("content", "")).replace("\n", " ")[:60]
    return ""


class ScriptedBackend(BaseModelBackend):
    """按固定剧本回话的假后端，用于对谈环节的离线自测。"""

    def __init__(self) -> None:
        super().__init__(model_type=ModelType.STUB, model_config_dict={})
        self._counter = OfflineTokenCounter()
        self._calls = 0

    @property
    def token_counter(self) -> BaseTokenCounter:
        return self._counter

    def _reply(self, messages: List[Any]) -> str:
        self._calls += 1
        excerpt = _last_excerpt(messages)

        return (
            f"{MOCK_BANNER} 第 {self._calls} 次调用。\n\n"
            f"【接住上一轮】你刚才说到「{excerpt}…」。\n\n"
            f"【模拟观点】这里本应是结合人格与议题展开的具体论证，"
            f"模拟模式下不产生任何真实结论。\n\n"
            f"【下一步】把 DEEPSEEK_API_KEY 填进 .env 后重新运行，"
            f"这一段会变成真实的专业发言。"
        )

    def _build(self, messages: List[Any]) -> ChatCompletion:
        return ChatCompletion(
            id=f"mock-{self._calls}",
            created=0,
            model="offline-mock",
            object="chat.completion",
            choices=[
                Choice(
                    finish_reason="stop",
                    index=0,
                    message=ChatCompletionMessage(
                        role="assistant", content=self._reply(messages)
                    ),
                )
            ],
        )

    def _run(
        self,
        messages: List[Any],
        response_format: Optional[Type[Any]] = None,
        tools: Optional[List[Any]] = None,
    ) -> ChatCompletion:
        return self._build(messages)

    async def _arun(
        self,
        messages: List[Any],
        response_format: Optional[Type[Any]] = None,
        tools: Optional[List[Any]] = None,
    ) -> ChatCompletion:
        return self._build(messages)

    @property
    def token_limit(self) -> int:
        return 1_000_000


# ---------------------------------------------------------------------------
# 生成人格 / 议题的模拟后端
# ---------------------------------------------------------------------------

_MOCK_PERSONAS = {
    "title": "模拟目的对谈手册",
    "slug": "mock-purpose",
    "proposer": {
        "name": "方案方",
        "title": "模拟方案提出者",
        "stance": "什么是对的",
        "system_message": (
            "【人格】\n这是离线模拟模式下生成的人格，不含任何真实内容，"
            "只用来验证解析链路是不是通的。\n\n"
            "【专业立场】\n模拟立场：先摆方案，再等对方挑刺。\n\n"
            "【你的盲区，不要掩饰】\n模拟盲区：方案偏理论，不太管执行成本。"
        ),
    },
    "challenger": {
        "name": "挑刺方",
        "title": "模拟方案质疑者",
        "stance": "现实中做不做得到",
        "system_message": (
            "【人格】\n这是离线模拟模式下生成的人格，不含任何真实内容，"
            "只用来验证解析链路是不是通的。\n\n"
            "【专业立场】\n模拟立场：专门指出对方方案落不了地的地方。\n\n"
            "【你的盲区，不要掩饰】\n模拟盲区：容易为了「做得到」而放松标准。"
        ),
    },
}

_MOCK_TOPICS = {
    "topics": [
        {
            "key": "mock-baseline",
            "section_title": "一、模拟议题：先把基准定下来",
            "brief": "模拟议题一：这个目的下最该先定下来的那个基准是什么，由谁来定。",
            "goal": "收敛出一个可以直接照做的基准，并说明什么情况下必须重新算。",
            "opening": (
                "【结论先行】模拟模式下我先摆一个占位方案。\n\n"
                "这里本应是针对你的目的给出的具体做法，模拟模式下不产生任何真实结论。"
            ),
        },
        {
            "key": "mock-reality",
            "section_title": "二、模拟议题：放到真实场景里还能不能做",
            "brief": "模拟议题二：上一节定的基准，在真实场景里要怎么打折，打折的代价是什么。",
            "goal": "给出至少三个真实场景下的具体应对动作，每个都要当场能做。",
            "opening": (
                "【结论先行】模拟模式下这一段同样是占位内容。\n\n"
                "填上 DEEPSEEK_API_KEY 后重新运行，这里会变成真实的专业发言。"
            ),
        },
    ]
}


class ScriptedAuthorBackend(ScriptedBackend):
    """给「生成人格 / 生成议题」用的假后端：吐的是**合法 JSON**。

    它靠提示词里出现的 schema 键名分辨这次要的是哪一份（`"proposer"` 还是
    `"topics"`）。这不是什么优雅的协议，但胜在**不用往真实提示词里塞暗号**
    ——那些键名本来就在提示词的 schema 里写着，真实模型看到的提示词一个字没变。

    存在的意义：让 `authoring` 的「解析 → 校验 → 建 dataclass」这条链路
    在离线状态下也真的被走一遍。mock 若直接把内容硬编码成 dataclass，
    这条链路上有 bug 就永远发现不了。
    """

    def _reply(self, messages: List[Any]) -> str:
        self._calls += 1
        text = " ".join(
            str(m.get("content", "")) for m in messages if isinstance(m, dict)
        )
        if '"proposer"' in text:
            payload: dict = _MOCK_PERSONAS
        elif '"topics"' in text:
            payload = _MOCK_TOPICS
        else:
            # 走到这里说明 authoring 的提示词模板被改得认不出来了。
            # 直接回一段非 JSON，让 authoring 抛出「解析失败」而不是悄悄产出一份假蓝图。
            return f"{MOCK_BANNER} 模拟后端不认识这个提示词，无法生成 JSON。"

        return json.dumps(payload, ensure_ascii=False, indent=2)
