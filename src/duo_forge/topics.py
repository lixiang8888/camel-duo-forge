"""议题：专家们逐条谈过去，每条产出成果的一节。

搬运自 camel-fitness-nutrition-duo 的 topics.py，但**议题表本身搬不走**——
那边写死的是健身饮食的五个议题，这里换成由 `authoring.forge_topics` 按目的生成。

留下来的是三样不随领域变的东西：
1. `Topic` 的形状（key / section_title / brief / goal / opening）；
2. 各字段的长度上限——它们每一轮都要进每位专家的 system message，是有真金白银的；
3. 收尾议题：它不由模型生成，因为它跟前面每个议题的形状都不一样（见下）。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 各字段的长度上限。定这些数不是为了防攻击（这是本机跑的东西），
#: 而是因为 `brief` 和 `goal` 每个议题、每一轮都要原样发给各位专家：
#: 一个议题写长了，成本是**乘上轮数再乘上议题数**地翻。
MAX_SECTION_TITLE = 60
MAX_BRIEF = 400
MAX_GOAL = 200
MAX_OPENING = 1000

#: 收尾议题的固定 key。society 的提前收手、pipeline 的分岔全靠它认人。
CLOSING_KEY = "closing"

#: 全部议题结束后的最终统一轮。它同样不是蓝图里的议题，只是一个占位，
#: 好让统一轮复用 Topic / 回调 / 对话记录的接口。
CONVERGENCE_KEY = "convergence"

#: 读者读完成果后追加提问的长度上下限。它比 `MAX_BRIEF` 宽松，是因为完整的
#: 问题会作为一次性上下文发给所有专家；system message 里只放截断后的 brief。
MIN_FOLLOWUP_QUESTION = 2
MAX_FOLLOWUP_QUESTION = 2000

#: 追加讨论的 key 前缀。每次追加生成 `followup-1` / `followup-2`，只用于页面分组，
#: 不参与任何业务判断。
FOLLOWUP_KEY_PREFIX = "followup"

#: 重生成议题 / 人格时用户写的那段「这次想要什么」。它只进这一次生成的提示词，
#: 不写进蓝图；上限和团队要求同一量级。
MAX_REFORGE_HINT = 500

MAX_TOPICS = 12

#: 默认生成几个正文议题（不含自动追加的收尾议题）。
#: 放在这里而不是 authoring.py，是因为 argparse 的默认值在 main() 里就要用到，
#: 而 authoring **会拖进 camel**——`duo-forge show` 那条路不该为它付这笔导入开销。
DEFAULT_TOPIC_COUNT = 5


@dataclass(frozen=True)
class Topic:
    key: str
    section_title: str
    brief: str  # 写进 system message 的议题描述
    goal: str  # 本轮要收敛出什么
    opening: str  # 方案提出方的开场发言


def closing_topic(count: int, title: str = "") -> Topic:
    """收尾议题。**不由模型生成，由代码造。**

    前 N 个议题是「围绕目的吵出内容」，收尾议题是「回顾前面吵过的所有内容」——
    形状本来就不一样（它的输入是前面全部对话，不是单一议题）。把它交给模型生成，
    只会得到一个和普通议题重复的东西。

    这不违反「议题由模型生成」：`count` 是模型给的议题数，这里只是按它算出一句
    「我们前面谈了 N 个议题」，好让开场白接得上。
    """
    subject = f"《{title}》" if title else "这份成果"
    return Topic(
        key=CLOSING_KEY,
        section_title="附录：分歧备忘 · 红线信号 · 行动自检清单",
        brief=(
            f"收尾讨论：回顾前面 {count} 个议题，{subject}里哪些地方你们真正达成了共识，"
            "哪些地方你们仍然保留不同意见。以及这个目的下最容易踩的红线有哪些。"
        ),
        goal=(
            "如实记录未解决的分歧（不要强行和稀泥），"
            "并产出一份读者可以照着做的自检清单。"
        ),
        opening=(
            f"【结论先行】我们前面谈了 {count} 个议题。我想先确认一件事："
            "哪些结论是我们都真的认同的，哪些只是我们各自让步的结果。\n\n"
            "让步出来的方案如果不写明代价，读者会以为它是免费的。"
        ),
    )


def is_closing(topic: Topic) -> bool:
    return topic.key == CLOSING_KEY


def convergence_topic() -> Topic:
    """全部议题结束后的最终统一轮。

    它不由模型生成，也不进蓝图；存在的意义是让 `pipeline` 的 `on_turn` 回调
    仍有一个 Topic 可传（启动器按 topic.key 给发言分组），而不是再发明一套
    只给这一轮用的回调。
    """
    return Topic(
        key=CONVERGENCE_KEY,
        section_title="最终统一意见",
        brief="全部议题已经谈完。这一轮只收敛仍未谈拢的地方。",
        goal="回应者明确接受，或只留最后一条修正；主提出方给出最终统一结论。",
        opening="",
    )
