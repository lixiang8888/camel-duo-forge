"""CAMEL 双智能体角色扮演编排。

搬运自 camel-fitness-nutrition-duo 的 society.py。改动只有两处：

1. 人格不再 import 写死的 NUTRITIONIST / COACH，改由调用方以 `personas` 传进来
   （顺序即语义：第一位是方案提出方 = assistant agent，第二位是挑战者 = user agent）。
2. 外挂了两个查表工具的那段删了——那是健身饮食专用的（见 fooddata.py）。
   这里留了一个 `tools` 参数当扩展点，默认不装任何工具。

原文件里那段关于 camel 版本坑的说明照抄在下面，因为坑还在：

1. camel-ai 0.2.90 的 ChatAgent 已经没有 role_name 参数了（老教程里还有）。
2. RolePlaying 默认会在内部另建 agent 并覆盖 system message。
   只有把 assistant_agent / user_agent 显式传进去，人格设定才会被保留。

关于「跑几轮」：camel 的 RolePlaying.step() 本身就是**一轮一调**，它内部不含
循环、也没有 max_turns，跑几轮完全由这里决定。所以 `rounds` 在本模块是**上限**，
不是定额——挑战者宣布没有新的反对意见就提前收手（见 `_is_concession`）。

顺带一提：`RolePlaying.step()` 返回的 `terminated` 在我们这套配置下**恒为 False**
（camel 的 ChatAgent 默认不装任何 response_terminator）。下面那句
`if ... .terminated: break` 是一条**保留的安全网**，不是收手机制本身。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from camel.agents import ChatAgent
from camel.societies import RolePlaying

from .personas import Persona, build_system_message
from .topics import Topic


#: 挑战者宣布「没有新的反对意见」时说的那句话。看到它这场辩论就收手。
#: 这条纪律写在 personas.CHALLENGER_CONTRACT 里，由代码追加给第二位专家。
NO_MORE_OBJECTION = "【无异议】"

#: 至少跑满这么多轮才允许收手。第 1 轮是「挑战者挑刺 + 提出方回应」，
#: 是结构上最小的有效交锋，砍了那一节就没内容了。
MIN_ROUNDS = 2

#: 判定前从表态两端剥掉的东西：空白、以及模型很爱在句末补的那个句号。
_CONCESSION_PADDING = " \t\n。.！!～~"


@dataclass
class Turn:
    speaker: str
    content: str


@dataclass
class TopicTranscript:
    topic: Topic
    turns: list[Turn] = field(default_factory=list)
    #: 挑战者表示没有新的反对意见、没跑满就收手了。
    stopped_early: bool = False
    #: 实际跑完的轮数 / 计划的上限轮数。收手那轮的轮号就是 rounds_run。
    rounds_run: int = 0
    planned_rounds: int = 0

    def to_markdown(self) -> str:
        lines = [f"## 议题：{self.topic.section_title}", ""]
        for turn in self.turns:
            lines.append(f"**{turn.speaker}**：{turn.content}")
            lines.append("")
        if self.stopped_early:
            lines.append(
                f"> 挑战者对当前方案已无新的反对意见，第 {self.rounds_run} 轮后收手"
                f"（上限 {self.planned_rounds} 轮）。"
            )
            lines.append("")
        return "\n".join(lines)

    def as_dialogue(self) -> str:
        """给整理阶段用的纯对话文本。"""
        return "\n\n".join(f"{t.speaker}：{t.content}" for t in self.turns)


def _first_text(response) -> str:
    """从 ChatAgentResponse 里安全取文本。"""
    msgs = getattr(response, "msgs", None) or []
    if not msgs:
        return ""
    return str(msgs[0].content).strip()


def _is_concession(text: str) -> bool:
    """这一轮发言是不是「没有新的反对意见」的表态。

    **用完全相等，不是包含。** 模型要是写了「【无异议】不过……」这种一长段，
    那说明它其实还有话说——这时候必须当没看见、让辩论继续。两个方向的错代价
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
    """让两位专家围绕一个议题对谈若干轮，返回完整记录。

    `personas` 必须恰好两位，顺序即语义：
    `[0]` 是方案提出方（assistant agent，先开口），`[1]` 是挑战者（user agent）。

    RolePlaying.step() 的语义是：把消息交给 user agent，再把它的回复交给
    assistant agent。所以一轮里时间顺序是「挑战者先说，提出方后答」。

    `should_stop` 只在**轮次边界**被问到：一次 step() 内部是两次模型调用，
    没法从中间掐断，所以点了停止最坏要等当前这轮 step 返回才收手。

    **`rounds` 是上限，不是定额。** 开着 `early_stop` 时，如果挑战者明确表示
    对当前方案已经没有新的反对意见（只回一句 `NO_MORE_OBJECTION`），
    这场辩论就到此为止——挑刺的人没刺可挑了，再跑下去只是复读。

    `early_stop` **默认关**：直调本函数的调用方（包括测试）行为因此是可预期的，
    pipeline 才是决定开不开的地方（它还要把 mock 模式排除掉）。
    """
    if len(personas) != 2:
        raise ValueError(f"personas 必须恰好两位，收到 {len(personas)} 位。")

    proposer, challenger = personas[0], personas[1]
    extra = list(tools or ())

    # 两位各自拿一份工具，不共用实例：CAMEL 的 tool 对象在 agent 内部会被引用，
    # 分开建省得将来出怪事。默认没有工具，这一层是为后续扩展留的。
    assistant_agent = ChatAgent(
        system_message=build_system_message(
            proposer, topic.brief, topic.goal, challenger=False
        ),
        model=backend,
        tools=list(extra),
    )
    user_agent = ChatAgent(
        system_message=build_system_message(
            challenger, topic.brief, topic.goal, challenger=True
        ),
        model=backend,
        tools=list(extra),
    )

    society = RolePlaying(
        assistant_role_name=proposer.name,
        user_role_name=challenger.name,
        task_prompt=topic.brief,
        # 关掉这两个：它们会在内部再建 agent，超出「只有两位智能体」的设定
        with_task_specify=False,
        with_task_planner=False,
        with_critic_in_the_loop=False,
        model=backend,
        assistant_agent=assistant_agent,
        user_agent=user_agent,
    )

    transcript = TopicTranscript(topic=topic, planned_rounds=rounds)

    def record(speaker: str, content: str) -> None:
        if not content:
            return
        turn = Turn(speaker=speaker, content=content)
        transcript.turns.append(turn)
        if on_turn:
            on_turn(turn)

    # init_chat 返回的是方案提出方（assistant）的开场白
    opening = society.init_chat(init_msg_content=topic.opening)
    record(proposer.name, _first_text(opening) or topic.opening)

    message = opening
    for index in range(1, rounds + 1):
        if should_stop is not None and should_stop():
            break

        assistant_response, user_response = society.step(message)

        # 先记挑战者（user agent），再记提出方（assistant agent），保持时间顺序。
        # **先记录再判断收手**：宣布收手的那一句必须留在 transcript 里，
        # 不能因为「反正要停了」就不记。
        challenger_text = _first_text(user_response)
        record(challenger.name, challenger_text)
        record(proposer.name, _first_text(assistant_response))
        transcript.rounds_run = index

        # 这条是 camel 自己的终止信号（上下文超限等）。留着当安全网。
        if assistant_response.terminated or user_response.terminated:
            break

        # 挑战者说没有新的反对意见 → 收手。
        # 注意检查在 step() **之后**：camel 的一轮往返是「挑战者先说、提出方后答」，
        # 中途掐不断，所以触发收手的那一轮提出方也已经答完了。这样记录里每一轮
        # 都是完整对称的（不会留下半轮），代价只是多一次调用。
        if (
            early_stop
            and index >= min_rounds
            and index < rounds
            and _is_concession(challenger_text)
        ):
            transcript.stopped_early = True
            break

    return transcript
