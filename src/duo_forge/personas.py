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

# ---------------------------------------------------------------------------
# 长度上限
# ---------------------------------------------------------------------------
# 这些常量放在不依赖 camel 的 personas.py 里，`authoring.py` 再 import 过去，
# 这样 CLI / launcher 校验人格时不用为了几个数字把 camel 拖进来。

MAX_NAME = 20
MAX_PERSONA_TITLE = 60
MAX_STANCE = 40

#: 模型生成的人格上限。提示词里要求「400 字以内」，这里留足余量。
MAX_SYSTEM_MESSAGE = 4000

#: 用户**手写**的人格上限。允许附一段长期背景与案例（「人格记忆」）。
#: 注意：它会在每个议题、每一轮随 system message 发给模型，越长越贵。
MAX_MANUAL_SYSTEM_MESSAGE = 12000

#: 「给方向」模式下一两句话的上限。
MAX_PERSONA_HINT = 300

#: 这两段由 `full_system_message` 追加。用户手写或提示里出现会造成重复，
#: 所以解析时直接拒绝。只匹配精确标记，不按「发言纪律」四个字宽泛拦截。
DISCIPLINE_MARKERS = ("【发言纪律】", "【收手纪律】")

#: 自定义人格的三种模式。见 `PersonaSpec`。
MODES = ("auto", "hint", "manual")


class PersonaSpecError(ValueError):
    """用户提供的人格设定不合法。消息直接打到终端或网页上。"""


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


# ---------------------------------------------------------------------------
# 用户自定义人格
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonaSpec:
    """一位专家的「用户意图」。

    - `auto`：完全交给模型生成（默认）。
    - `hint`：用户只写一两句方向，模型展开成完整人格。
    - `manual`：四项都由用户写死，模型不再生成这一位。

    `manual` 的 `system_message` 是**人格三块**（可再加「长期背景与案例」），
    不是最终的 system message——发言纪律和收手纪律仍由 `full_system_message` 追加。
    """

    mode: str = "auto"
    hint: str = ""
    name: str = ""
    title: str = ""
    stance: str = ""
    system_message: str = ""

    @property
    def is_auto(self) -> bool:
        return self.mode == "auto"

    def to_persona(self) -> Persona:
        return Persona(
            name=self.name,
            title=self.title,
            stance=self.stance,
            system_message=self.system_message,
        )


@dataclass(frozen=True)
class PersonaPlan:
    """两位专家各自用什么模式。顺序有语义：proposer 先开口，challenger 挑刺。"""

    proposer: PersonaSpec = PersonaSpec()
    challenger: PersonaSpec = PersonaSpec()

    @property
    def all_auto(self) -> bool:
        return self.proposer.is_auto and self.challenger.is_auto

    @property
    def both_manual(self) -> bool:
        return self.proposer.mode == "manual" and self.challenger.mode == "manual"

    @property
    def has_manual(self) -> bool:
        return self.proposer.mode == "manual" or self.challenger.mode == "manual"


_SIDE_LABELS = {
    "proposer": "第一位专家（方案提出方）",
    "challenger": "第二位专家（挑战者）",
}


def parse_persona_plan(raw: object) -> PersonaPlan:
    """把 CLI 文件 / 网页请求里的 `personas` 解析成 `PersonaPlan`。

    `None` 和 `{}` 都表示「两位都交给模型」；某一侧缺失或 `{}` 也表示那一侧 auto。
    所有错误都是 `PersonaSpecError`，消息里带「第几位」的标签，能直接给人看。
    """
    if raw is None:
        return PersonaPlan()
    if not isinstance(raw, dict):
        raise PersonaSpecError(
            "personas 必须是一个 JSON 对象（可以包含 proposer / challenger 两个键）。"
        )
    unknown = set(raw) - {"proposer", "challenger"}
    if unknown:
        raise PersonaSpecError(
            f"personas 里有不认识的键：{', '.join(sorted(unknown))}。"
            "只允许 proposer / challenger。"
        )
    return PersonaPlan(
        proposer=parse_persona_spec(raw.get("proposer"), _SIDE_LABELS["proposer"]),
        challenger=parse_persona_spec(raw.get("challenger"), _SIDE_LABELS["challenger"]),
    )


def parse_persona_spec(raw: object, label: str) -> PersonaSpec:
    """解析一位专家。`raw` 是 `None` / `{}` 时返回 auto。"""
    if raw is None or raw == {}:
        return PersonaSpec()
    if not isinstance(raw, dict):
        raise PersonaSpecError(f"{label}的设定必须是一个 JSON 对象。")

    allowed = {"mode", "hint", "name", "title", "stance", "system_message"}
    unknown = set(raw) - allowed
    if unknown:
        raise PersonaSpecError(
            f"{label}有不认识的字段：{', '.join(sorted(unknown))}。"
            f"可用字段：{', '.join(sorted(allowed))}。"
        )

    mode = raw.get("mode")
    if mode is None:
        if set(raw) - {"mode"}:
            raise PersonaSpecError(
                f"{label}写了字段但没写 mode。mode 只能是 auto / hint / manual。"
            )
        return PersonaSpec()
    if not isinstance(mode, str) or mode not in MODES:
        raise PersonaSpecError(
            f"{label}的 mode 不合法：{mode!r}。只能是 auto / hint / manual。"
        )

    if mode == "auto":
        extra = set(raw) - {"mode"}
        if extra:
            raise PersonaSpecError(
                f"{label}是 auto，但还写了：{', '.join(sorted(extra))}。"
                "auto 不需要任何字段。"
            )
        return PersonaSpec()

    if mode == "hint":
        extra = set(raw) - {"mode", "hint"}
        if extra:
            raise PersonaSpecError(
                f"{label}是 hint，不能再写：{', '.join(sorted(extra))}。"
            )
        hint = _text(raw.get("hint"), f"{label}的方向描述")
        if len(hint) > MAX_PERSONA_HINT:
            raise PersonaSpecError(
                f"{label}的方向描述太长：上限 {MAX_PERSONA_HINT} 字，"
                f"给了 {len(hint)} 字。"
            )
        _reject_discipline(hint, f"{label}的方向描述")
        return PersonaSpec(mode="hint", hint=hint)

    # manual
    if "hint" in raw:
        raise PersonaSpecError(f"{label}是 manual，不能再写 hint。")
    name = _line(raw.get("name"), f"{label}的称呼", MAX_NAME)
    title = _line(raw.get("title"), f"{label}的身份", MAX_PERSONA_TITLE)
    stance = _line(raw.get("stance"), f"{label}负责什么", MAX_STANCE)
    message = _text(raw.get("system_message"), f"{label}的人格设定")
    if len(message) > MAX_MANUAL_SYSTEM_MESSAGE:
        raise PersonaSpecError(
            f"{label}的人格设定太长：上限 {MAX_MANUAL_SYSTEM_MESSAGE} 字，"
            f"给了 {len(message)} 字。"
            "它会随每个议题、每一轮发给模型，越长越贵。"
        )
    _reject_discipline(message, f"{label}的人格设定")
    return PersonaSpec(
        mode="manual",
        name=name,
        title=title,
        stance=stance,
        system_message=message,
    )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersonaSpecError(f"{label}缺失，或者不是非空文本。")
    return value.strip()


def _line(value: object, label: str, limit: int) -> str:
    text = _text(value, label)
    if "\n" in text or "\r" in text:
        raise PersonaSpecError(f"{label}必须是单行。")
    if len(text) > limit:
        raise PersonaSpecError(
            f"{label}太长：上限 {limit} 字，给了 {len(text)} 字。"
        )
    return text


def _reject_discipline(text: str, label: str) -> None:
    for marker in DISCIPLINE_MARKERS:
        if marker in text:
            raise PersonaSpecError(
                f"{label}里不要写「{marker}」——发言纪律和收手纪律由系统自动追加，"
                "写了会重复。"
            )


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
