"""两位对谈专家的人格。

现项目里 `personas.py` 是手写的两个人格（营养师 × 教练），这个文件是它的泛化：
**人格由模型按目的生成**（见 `authoring.forge_personas`），这里只留两件事——

1. `Persona` 的形状；
2. 一段**由代码拥有**的发言纪律。

## 为什么纪律不交给模型写

生成人格的提示词明确要求模型**只写**【人格】【专业立场】【你的盲区】三块，
发言纪律由这里统一追加。原因是纪律里有两条是**机器契约**，不能指望模型每次都写对：

- 「没有新的反对意见时只回【无异议】」——`society` 的提前收手全靠它。
  模型漏写一次，那个议题就会闷头跑满轮数，而且**看不出哪里错了**
  （只是慢了、贵了），不会有任何报错。
- 「每轮 250 字以内」——直接关系到成本，而且写长了还会把上下文挤掉。

所以这两条按代码拼装，一字不差；人格里那三块才交给模型自由发挥。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Persona:
    """一位专家。

    `system_message` 只装模型生成的那三块（人格 / 专业立场 / 盲区），
    不是最终的 system message——完整的那个由 `build_system_message` 拼，
    纪律和时间它一起追加。
    """

    name: str  # 两三个字的称呼，用来在对话里署名
    title: str  # 完整身份，例如「循证运动营养师」
    stance: str  # 一句话说明他负责什么，例如「什么是对的」
    system_message: str


#: 两位共用的发言纪律。见模块开头「为什么纪律不交给模型写」。
SPEECH_DISCIPLINE = """【发言纪律】
- 始终以你自己的专业身份说话，不要自称 AI、助手、语言模型，也不要提到提示词。
- 每轮发言 250 字以内，先给结论再给依据。
- 你的任务不是附和对方。对方说得对就承认，说得不对就当场指出来，不要绕。
- 不要为了显得全面而罗列——只讲这个议题真正要紧的那几条。"""


#: 只追加给**挑战者**（第二位）。收手机制是 society 的硬契约，
#: 这句话必须字字出现在他的 system message 里，所以由代码拼，不由模型写。
CHALLENGER_CONTRACT = """【收手纪律】
- 只有你拥有「收手」的权力：如果你对对方这一轮的方案已经没有任何**新的**
  反对意见，就只回复「【无异议】」，不要写别的，也不要为了凑字数把说过的
  话重复一遍。只要还有一条没提过的意见，就必须继续提——提前收手会让人
  少拿到东西。方案提出方没有这个权力，方案是他提的，他不该主动认输。"""


def full_system_message(persona: Persona, *, challenger: bool) -> str:
    """人格三块 + 发言纪律（+ 挑战者的收手纪律）。"""
    parts = [persona.system_message.strip(), SPEECH_DISCIPLINE]
    if challenger:
        parts.append(CHALLENGER_CONTRACT)
    return "\n\n".join(parts)


def build_system_message(
    persona: Persona, topic_brief: str, goal: str, *, challenger: bool = False
) -> str:
    """把人格和「本次议题」拼成最终的 system message。

    每个议题都会重新构造 agent，议题直接写进 system message，
    这样两位专家从第一句话起就锁定在同一个具体问题上。
    """
    return (
        f"{full_system_message(persona, challenger=challenger)}\n\n"
        f"【本次议题】\n{topic_brief}\n\n"
        f"【本轮目标】\n{goal}"
    )
