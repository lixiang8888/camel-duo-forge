"""CAMEL 多智能体角色扮演编排（2–5 人）。

原实现直接套 CAMEL 的 `RolePlaying`，它天然只有 assistant / user 两方。多人化之后
这里改成手动编排：每位参与者一个 `ChatAgent`，每轮按蓝图顺序轮流发言，第 1 位
（主提出方）最后回应。这样 2 人时正好还原原来的「挑战者先说、提出方后答」。

## 发言顺序

```
开场（第 1 位，代码里预写好的 topic.opening，不花模型调用）
第 1 轮：第 2 位 → 第 3 位 → … → 第 N 位 → 第 1 位
第 2 轮：第 2 位 → … → 第 N 位 → 第 1 位
```

第 1 位最后说，是为了让他听完所有人的意见再修订方案；其余人先各自表态。

## 记忆怎么共享

每轮的发言要进入**所有**参与者的记忆，而且角色要标对：自己说过的是 assistant，
别人说的是 user。做法是「上一位的发言交给下一位作为 step 的输入，其余人广播」：

- 发言者在 `step()` 里自动把自己的回复记成 assistant；
- 下一位通过 `step(上一条)` 收到它，记成 user；
- 其他所有人用 `update_memory(..., USER)` 收到同一句。

这样每条发言在每个人的记忆里恰好出现一次，上下文按线性增长，不会因为「每轮
重发全部历史」而按轮数平方膨胀。

## 关于「跑几轮」

`rounds` 是**上限**，不是定额。开着 `early_stop` 时，如果**所有回应者**在同一轮
都表示没有新的意见（只回 `NO_MORE_OBJECTION`），这场讨论就到此为止。两人模式
下回应者只有挑战者，判据和改动前完全一致。

`ChatAgent.step()` 返回的 `terminated` 在默认配置下很少为真（上下文超限、
外部 stop_event 等）。它仍然是一条**保留的安全网**，不是收手机制本身。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.types import OpenAIBackendRole

from .personas import MAX_PEOPLE, MIN_PEOPLE, Persona, build_system_message
from .topics import Topic


#: 回应者宣布「没有新的反对意见」时说的那句话。看到它这场讨论就可能收手。
#: 这条纪律写在 personas.CONCESSION_CONTRACT 里，由代码追加给所有回应者。
NO_MORE_OBJECTION = "【无异议】"

#: 至少跑满这么多轮才允许收手。第 1 轮是「回应者表态 + 提出方回应」，
#: 是结构上最小的有效交锋，砍了那一节就没内容了。
MIN_ROUNDS = 2

#: 判定前从表态两端剥掉的东西：空白、以及模型很爱在句末补的那个句号。
_CONCESSION_PADDING = " \t\n。.！!～~"

#: 某一位没有吐出任何内容时，下一位收到的占位消息。它不会进对话记录，
#: 只用来保证轮转不断档。
_EMPTY_TURN = "（这一位本轮没有发言。）"


@dataclass
class Turn:
    speaker: str
    content: str
    role: str = ""


@dataclass
class TopicTranscript:
    topic: Topic
    turns: list[Turn] = field(default_factory=list)
    #: 回应者们表示没有新的反对意见、没跑满就收手了。
    stopped_early: bool = False
    #: 实际跑完的轮数 / 计划的上限轮数。收手那轮的轮号就是 rounds_run。
    rounds_run: int = 0
    planned_rounds: int = 0
    #: 收手时已经表态「无异议」的回应者称呼。用于记录里说清是谁没意见了。
    conceding: tuple[str, ...] = ()

    def to_markdown(self) -> str:
        lines = [f"## 议题：{self.topic.section_title}", ""]
        for turn in self.turns:
            label = (
                f"{turn.speaker}（{turn.role}）" if turn.role else turn.speaker
            )
            lines.append(f"**{label}**：{turn.content}")
            lines.append("")
        if self.stopped_early:
            who = "、".join(self.conceding) or "所有其他参与者"
            lines.append(
                f"> {who} 已无新的反对意见，第 {self.rounds_run} 轮后收手"
                f"（上限 {self.planned_rounds} 轮）。"
            )
            lines.append("")
        return "\n".join(lines)

    def as_dialogue(self) -> str:
        """给整理阶段用的纯对话文本。"""
        lines = []
        for turn in self.turns:
            label = (
                f"{turn.speaker}（{turn.role}）" if turn.role else turn.speaker
            )
            lines.append(f"{label}：{turn.content}")
        return "\n\n".join(lines)


def _first_text(response) -> str:
    """从 ChatAgentResponse 里安全取文本。"""
    msgs = getattr(response, "msgs", None) or []
    if not msgs:
        return ""
    return str(msgs[0].content).strip()


def _is_concession(text: str) -> bool:
    """这一轮发言是不是「没有新的反对意见」的表态。

    **用完全相等，不是包含。** 模型要是写了「【无异议】不过……」这种一长段，
    那说明它其实还有话说——这时候必须当没看见、让讨论继续。两个方向的错代价
    差得很远：漏判只是多花一轮的钱，误判会把一整段发言从成果里抹掉。
    所以这里宁可漏判。

    首尾空白和句末标点忽略掉，因为模型很爱在句末补一个句号。
    """
    return text.strip(_CONCESSION_PADDING) == NO_MORE_OBJECTION


def debate_topic(
    topic: Topic,
    *,
    personas: Sequence[Persona],
    backend,
    rounds: int = 3,
    on_turn: Optional[Callable[[Turn], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    early_stop: bool = False,
    min_rounds: int = MIN_ROUNDS,
    tools: Optional[Sequence] = None,
) -> TopicTranscript:
    """让 2–5 位参与者围绕一个议题对谈若干轮，返回完整记录。

    `personas` 的顺序即语义：`[0]` 是主提出方（先开场、每轮最后回应），
    `[1:]` 是回应者（按顺序先发言）。

    `should_stop` 只在**轮次边界**被问到：一轮里每一位都要调用一次模型，
    没法从中间掐断，所以点了停止最坏要等当前这一轮跑完才收手。

    **`rounds` 是上限，不是定额。** 开着 `early_stop` 时，如果所有回应者在
    同一轮都明确表示对当前方案已经没有新的反对意见（只回一句
    `NO_MORE_OBJECTION`），这场讨论就到此为止——没人有新意见了，再跑只是复读。

    `early_stop` **默认关**：直调本函数的调用方（包括测试）行为因此是可预期的，
    pipeline 才是决定开不开的地方（它还要把 mock 模式排除掉）。
    """
    if not (MIN_PEOPLE <= len(personas) <= MAX_PEOPLE):
        raise ValueError(
            f"personas 必须有 {MIN_PEOPLE}–{MAX_PEOPLE} 位，收到 {len(personas)} 位。"
        )

    people = len(personas)
    extra = list(tools or ())

    # 每位参与者一个 agent。system message 里带自己的角色、人格、发言纪律；
    # 第 1 位不带收手纪律（他没有收手权），其余人都带。
    agents = [
        ChatAgent(
            system_message=build_system_message(
                persona,
                topic.brief,
                topic.goal,
                opener=(index == 0),
            ),
            model=backend,
            tools=list(extra),
        )
        for index, persona in enumerate(personas)
    ]

    transcript = TopicTranscript(topic=topic, planned_rounds=rounds)

    def record(speaker: str, role: str, content: str) -> None:
        if not content:
            return
        turn = Turn(speaker=speaker, content=content, role=role)
        transcript.turns.append(turn)
        if on_turn:
            on_turn(turn)

    # 开场白是 forge 时写好的，不花模型调用；原样记在第 1 位名下。
    opening = BaseMessage.make_assistant_message(
        role_name=personas[0].name, content=topic.opening
    )
    agents[0].update_memory(opening, OpenAIBackendRole.ASSISTANT)
    # 第 2 位会通过 step(opening) 收到它；其余人直接广播。
    for agent in agents[2:]:
        agent.update_memory(opening, OpenAIBackendRole.USER)
    record(personas[0].name, personas[0].role, topic.opening)

    # 一轮里的发言顺序：所有回应者，最后才是主提出方。
    order = list(range(1, people)) + [0]
    pending = opening
    terminated = False

    for index in range(1, rounds + 1):
        if should_stop is not None and should_stop():
            break

        round_texts: dict[int, str] = {}
        completed = True

        for position, who in enumerate(order):
            response = agents[who].step(pending)
            text = _first_text(response)
            record(personas[who].name, personas[who].role, text)
            round_texts[who] = text

            outbound = BaseMessage.make_assistant_message(
                role_name=personas[who].name,
                content=text or _EMPTY_TURN,
            )
            # 下一位会通过 step(outbound) 收到它；其他人都走广播。
            # 连续轮转：一轮的最后一位说完，下一位就是下一轮的第 2 位。
            next_who = order[position + 1] if position + 1 < len(order) else order[0]
            for other in range(people):
                if other == who or other == next_who:
                    continue
                agents[other].update_memory(outbound, OpenAIBackendRole.USER)
            pending = outbound

            # 这条是 camel 自己的终止信号（上下文超限等）。留着当安全网。
            # 注意只看 `terminated`，不看「这一轮恰好没返回文本」——后者用占位消息
            # 顶过去，不该让整场讨论提前结束。
            if getattr(response, "terminated", False):
                terminated = True
                completed = False
                break

        if completed:
            transcript.rounds_run = index

        if terminated:
            break

        if (
            early_stop
            and index >= min_rounds
            and index < rounds
            and all(
                _is_concession(round_texts.get(who, ""))
                for who in range(1, people)
            )
        ):
            transcript.stopped_early = True
            transcript.conceding = tuple(
                personas[who].name
                for who in range(1, people)
                if _is_concession(round_texts.get(who, ""))
            )
            break

    return transcript

