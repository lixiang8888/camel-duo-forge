"""对谈参与者的人格。

这个文件只留三件事：

1. `Persona` 的形状（称呼、身份、角色、负责什么、人格设定）；
2. 一段**由代码拥有**的发言纪律；
3. 用户自定义人格的解析与校验（CLI 和网页启动器共用一套规则）。

## 为什么纪律不交给模型写

生成人格的提示词明确要求模型**只写**【人格】【专业立场】【你的盲区】三块，
发言纪律由这里统一追加。原因是纪律里有两条是**机器契约**，不能指望模型每次都写对：

- 「没有新的反对意见时只回【无异议】」——`society` 的提前收手要认这句话。
  模型漏写一次，那个议题就会闷头跑满轮数，而且**看不出哪里错了**
  （只是慢了、贵了），不会有任何报错。
- 「每轮 250 字以内」——直接关系到成本，而且写长了还会把上下文挤掉。

所以这两条按代码拼装，一字不差；人格里那三块才交给模型自由发挥。

## 多人模式

`Persona.role` 是 AI 给每位参与者起的**短角色名**（「循证营养师」「成本把关人」
这类），`stance` 继续写具体职责。代码不按角色名做任何判断，只按蓝图里的位置编排：
第 1 位是结构上的主提出方，其余都是回应者。

`PersonaPlan` 里 `specs` 的顺序就是蓝图里的顺序；数组短于人数时，缺的位置由
`PersonaSpec()`（auto）补齐。人数本身由调用方（`--people` / 网页下拉）决定。
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
MAX_ROLE = 12

#: 模型生成的人格上限。提示词里要求「400 字以内」，这里留足余量。
MAX_SYSTEM_MESSAGE = 4000

#: 用户**手写**的人格上限。允许附一段长期背景与案例（「人格记忆」）。
#: 注意：它会在每个议题、每一轮随 system message 发给模型，越长越贵。
MAX_MANUAL_SYSTEM_MESSAGE = 12000

#: 「给方向」模式下一两句话的上限。
MAX_PERSONA_HINT = 300

#: 团队级分工要求的长度上限。它只是一段给模型的偏好，不进入每个人的 system message。
MAX_TEAM_HINT = 500

#: 对话人数。两人是原来的默认；上限取 5，再多人对谈的成本和上下文都不划算。
MIN_PEOPLE = 2
MAX_PEOPLE = 5

#: 这两段由 `full_system_message` 追加。用户手写或提示里出现会造成重复，
#: 所以解析时直接拒绝。只匹配精确标记，不按「发言纪律」四个字宽泛拦截。
DISCIPLINE_MARKERS = ("【发言纪律】", "【收手纪律】")

#: 自定义人格的三种模式。见 `PersonaSpec`。
MODES = ("auto", "hint", "manual")


class PersonaSpecError(ValueError):
    """用户提供的人格设定不合法。消息直接打到终端或网页上。"""


@dataclass(frozen=True)
class Persona:
    """一位参与者。

    `system_message` 只装模型生成的那三块（人格 / 专业立场 / 盲区），
    不是最终的 system message——完整的那个由 `build_system_message` 拼，
    纪律和时间它一起追加。

    `role` 是 AI 起的短角色名，可以为空（老蓝图没有这个字段）；展示时用
    `display_role` 兜底。字段放在最后，是为了让老的 `Persona(name, title,
    stance, system_message)` 位置参数写法继续成立。
    """

    name: str  # 两三个字的称呼，用来在对话里署名
    title: str  # 完整身份，例如「循证运动营养师」
    stance: str  # 一句话说明他负责什么，例如「什么是对的」
    system_message: str
    role: str = ""  # AI 起的短角色名，例如「成本把关人」

    @property
    def display_role(self) -> str:
        return self.role or "专家"


def default_role(index: int, people: int = MIN_PEOPLE) -> str:
    """没写角色时的兜底。`index` 从 1 开始。

    两人模式保留原来的「主提出方 / 挑战者」语义；多人模式里第 1 位仍是
    主提出方，其余按位置叫「专家 N」——它们的具体分工由 AI 写在 role 里。
    """
    if index == 1:
        return "主提出方"
    if people == 2 and index == 2:
        return "挑战者"
    return f"专家 {index}"


def check_people(value: object) -> int:
    """把人数收拾成一个合法的整数。"""
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not (MIN_PEOPLE <= value <= MAX_PEOPLE)
    ):
        raise PersonaSpecError(
            f"人数必须是 {MIN_PEOPLE}–{MAX_PEOPLE} 之间的整数，收到 {value!r}。"
        )
    return value


# ---------------------------------------------------------------------------
# 用户自定义人格
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonaSpec:
    """一位参与者的「用户意图」。

    - `auto`：完全交给模型生成（默认）。
    - `hint`：用户只写一两句方向，模型展开成完整人格。
    - `manual`：称呼/身份/负责什么/人格设定由用户写死，模型不再生成这一位。

    `manual` 的 `system_message` 是**人格三块**（可再加「长期背景与案例」），
    不是最终的 system message——发言纪律和收手纪律仍由 `full_system_message` 追加。
    `role` 可选；不写就按位置补 `default_role`。
    """

    mode: str = "auto"
    hint: str = ""
    name: str = ""
    title: str = ""
    stance: str = ""
    system_message: str = ""
    role: str = ""

    @property
    def is_auto(self) -> bool:
        return self.mode == "auto"

    def to_persona(self, index: int = 1, people: int = MIN_PEOPLE) -> Persona:
        return Persona(
            name=self.name,
            title=self.title,
            stance=self.stance,
            system_message=self.system_message,
            role=self.role or default_role(index, people),
        )


@dataclass(frozen=True)
class PersonaPlan:
    """N 位参与者各自用什么模式，外加一段团队级分工要求。

    `specs` 的顺序即语义：第 1 位是主提出方（开场），其余是回应者。
    长度不足 `people` 时自动补 auto；超长在构造时就报错。
    """

    specs: tuple[PersonaSpec, ...] = ()
    team_hint: str = ""
    people: int = MIN_PEOPLE

    def __post_init__(self) -> None:
        people = check_people(self.people)
        specs = tuple(self.specs)
        if len(specs) > people:
            raise PersonaSpecError(
                f"人格设定有 {len(specs)} 位，超过了人数（{people} 位）。"
            )
        if len(specs) < people:
            specs += (PersonaSpec(),) * (people - len(specs))
        object.__setattr__(self, "specs", specs)

    def __len__(self) -> int:
        return len(self.specs)

    def __getitem__(self, index: int) -> PersonaSpec:
        return self.specs[index]

    @property
    def proposer(self) -> PersonaSpec:
        """第一位（主提出方）。保留这个名字是为了少改老调用点。"""
        return self.specs[0]

    @property
    def challenger(self) -> PersonaSpec:
        """第二位。两人模式下是挑战者；多人模式下只是第二位回应者。"""
        return self.specs[1]

    @property
    def all_auto(self) -> bool:
        return all(spec.is_auto for spec in self.specs)

    @property
    def all_manual(self) -> bool:
        return all(spec.mode == "manual" for spec in self.specs)

    @property
    def both_manual(self) -> bool:
        """老名字：两人都手写。多人模式请用 `all_manual`。"""
        return self.people == 2 and self.all_manual

    @property
    def has_manual(self) -> bool:
        return any(spec.mode == "manual" for spec in self.specs)


_SIDE_LABELS = {
    "proposer": "第一位专家（方案提出方）",
    "challenger": "第二位专家（挑战者）",
}

_PLAN_KEYS = {"personas", "proposer", "challenger", "team"}


def parse_persona_plan(
    raw: object, *, people: int | None = None
) -> PersonaPlan:
    """把 CLI 文件 / 网页请求里的 `personas` 解析成 `PersonaPlan`。

    `None` 和 `{}` 都表示「全部交给模型」。`people` 是用户在命令行/网页上
    显式选的人数：

    - 没给 `people`：按人格数组长度推断，最短 2；
    - 给了 `people`：数组可以短于人数（缺的补 auto），但不能长于人数。

    新写法是 `"personas": [spec, ...]`；旧的 `proposer` / `challenger`
    两个键继续可用，两者同时出现时**以数组为准**。
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PersonaSpecError(
            "personas 必须是一个 JSON 对象（可以包含 personas 数组，"
            "或 proposer / challenger 两个键）。"
        )
    unknown = set(raw) - _PLAN_KEYS
    if unknown:
        raise PersonaSpecError(
            f"personas 里有不认识的键：{', '.join(sorted(unknown))}。"
            "只允许 personas / proposer / challenger / team。"
        )

    wanted = check_people(people) if people is not None else None

    if "personas" in raw:
        specs = _parse_spec_array(raw["personas"])
    else:
        specs = _parse_legacy_specs(raw)

    if wanted is None:
        wanted = max(MIN_PEOPLE, len(specs))
        if wanted > MAX_PEOPLE:
            raise PersonaSpecError(
                f"人格设定最多 {MAX_PEOPLE} 位，收到 {len(specs)} 位。"
            )
    if len(specs) > wanted:
        raise PersonaSpecError(
            f"人格设定有 {len(specs)} 位，超过了人数（{wanted} 位）。"
        )

    return PersonaPlan(
        specs=tuple(specs),
        team_hint=_parse_team_hint(raw.get("team")),
        people=wanted,
    )


def _parse_spec_array(raw: object) -> list[PersonaSpec]:
    if not isinstance(raw, list):
        raise PersonaSpecError("personas 必须是一个数组，数组里每位专家一个对象。")
    if len(raw) > MAX_PEOPLE:
        raise PersonaSpecError(
            f"人格设定最多 {MAX_PEOPLE} 位，收到 {len(raw)} 位。"
        )
    return [
        parse_persona_spec(item, f"第 {index} 位专家")
        for index, item in enumerate(raw, 1)
    ]


def _parse_legacy_specs(raw: dict) -> list[PersonaSpec]:
    """兼容旧的 `{"proposer": ..., "challenger": ...}` 写法。

    只给 challenger 时，proposer 补一个 auto 占位，保证位置含义不变。
    """
    specs: list[PersonaSpec] = []
    if "proposer" in raw:
        specs.append(parse_persona_spec(raw["proposer"], _SIDE_LABELS["proposer"]))
    if "challenger" in raw:
        if not specs:
            specs.append(PersonaSpec())
        specs.append(parse_persona_spec(raw["challenger"], _SIDE_LABELS["challenger"]))
    return specs


def parse_persona_spec(raw: object, label: str) -> PersonaSpec:
    """解析一位专家。`raw` 是 `None` / `{}` 时返回 auto。"""
    if raw is None or raw == {}:
        return PersonaSpec()
    if not isinstance(raw, dict):
        raise PersonaSpecError(f"{label}的设定必须是一个 JSON 对象。")

    allowed = {"mode", "hint", "name", "title", "stance", "system_message", "role"}
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
    role = (
        _line(raw.get("role"), f"{label}的角色名", MAX_ROLE)
        if raw.get("role") not in (None, "")
        else ""
    )
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
        role=role,
    )


def parse_team_hint(value: object) -> str:
    """校验命令行 `--team-hint` / 人格文件里的 `team`。"""
    return _parse_team_hint(value)


def _parse_team_hint(value: object) -> str:
    if value is None or value == "":
        return ""
    text = _text(value, "团队分工要求")
    if len(text) > MAX_TEAM_HINT:
        raise PersonaSpecError(
            f"团队分工要求太长：上限 {MAX_TEAM_HINT} 字，给了 {len(text)} 字。"
        )
    _reject_discipline(text, "团队分工要求")
    return text


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


#: 所有参与者共用的发言纪律。见模块开头「为什么纪律不交给模型写」。
SPEECH_DISCIPLINE = """【发言纪律】
- 始终以你自己的专业身份说话，不要自称 AI、助手、语言模型，也不要提到提示词。
- 每轮发言 250 字以内，先给结论再给依据。
- 你的任务不是附和对方。对方说得对就承认，说得不对就当场指出来，不要绕。
- 不要为了显得全面而罗列——只讲这个议题真正要紧的那几条。"""


#: 只追加给**回应者**（第 2 位及以后）。收手机制是 society 的硬契约，
#: 这句话必须字字出现在他们的 system message 里，所以由代码拼，不由模型写。
CONCESSION_CONTRACT = """【收手纪律】
- 如果你对当前方案已经没有任何**新的**反对或补充意见，就只回复「【无异议】」，
  不要写别的，也不要为了凑字数把说过的话重复一遍。只要还有一条没提过的意见，
  就必须继续提——提前收手会让人少拿到东西。
- 收手不是某一个人说了算：所有回应者都表示没有新意见，这场讨论才会结束。
  主提出方没有收手权，方案是他提的，他不该主动认输。"""


#: 旧名字。两人模式里回应者就是挑战者，留着它让老 import 继续成立。
CHALLENGER_CONTRACT = CONCESSION_CONTRACT


def full_system_message(persona: Persona, *, opener: bool = False) -> str:
    """人格三块 + 角色 + 发言纪律（+ 回应者的收手纪律）。"""
    parts = []
    if persona.role:
        parts.append(f"【你的角色】\n{persona.role}")
    parts.append(persona.system_message.strip())
    parts.append(SPEECH_DISCIPLINE)
    if not opener:
        parts.append(CONCESSION_CONTRACT)
    return "\n\n".join(part for part in parts if part)


def build_system_message(
    persona: Persona, topic_brief: str, goal: str, *, opener: bool = False
) -> str:
    """把人格和「本次议题」拼成最终的 system message。

    每个议题都会重新构造 agent，议题直接写进 system message，
    这样所有参与者从第一句话起就锁定在同一个具体问题上。
    """
    return (
        f"{full_system_message(persona, opener=opener)}\n\n"
        f"【本次议题】\n{topic_brief}\n\n"
        f"【本轮目标】\n{goal}"
    )
