"""按目的生成 2–5 位参与者的人格与议题。

两人模式保持原来的一次人格调用；三人及以上先让模型设计一份「团队分工骨架」，
再把骨架一次性展开成完整人格。多出来的那一次调用换来的是角色互补性：
五个人一次性现想，很容易出现几个换汤不换药的「专家」。

    forge_personas(目的, 人数) → N 位立场/分工不同的专家 + 标题
    forge_topics(目的, N 位专家) → 一组议题

## 三个设计上的讲究

**一、输出是 JSON，而且带重试。** 格式错一次就整个流程作废太浪费，
所以解析失败时会把**具体的错**回给模型，让它重来一次（同一份 memory，
它看得到自己刚写的那份）。两次都错才抛 `AuthoringError`。

**二、解析要宽松，校验要严。** 模型很爱在 JSON 外面套一层 ``` 或者写一句
「好的，以下是」，所以 `_extract_json` 会自己去文本里找那个 JSON 对象；
但字段本身一个字都不放过——缺字段、类型不对、超长，全部当场报错并说清是哪一项。

**三、分歧轴是选题的第一标准。** 参与者必须**真的会吵**，议题必须是他们
**天然意见不一致**的具体问题。这是整个项目产出有没有信息量的唯一来源：
大家互相附和，成果就是一锅正确的废话。所以两处提示词里这条都写在最前面。

## 一个刻意的不对称

人格里的**发言纪律不归模型写**（见 `personas.py` 开头）：模型只写
【人格】【专业立场】【你的盲区】三块，纪律由代码追加。因为其中一条
（回应者「没有新的反对意见时只回【无异议】」）是 `society` 提前收手的硬契约，
模型漏写一次不会有任何报错，只会让每个议题都闷头跑满轮数。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Sequence

from camel.agents import ChatAgent

from .personas import (
    DISCIPLINE_MARKERS,
    MAX_NAME,
    MAX_PERSONA_TITLE,
    MAX_ROLE,
    MAX_STANCE,
    MAX_SYSTEM_MESSAGE,
    Persona,
    PersonaPlan,
    default_role,
)
from .purpose import MAX_MATERIAL, MAX_PURPOSE, MAX_TITLE, MIN_PURPOSE, Purpose
from .topics import (
    CLOSING_KEY,
    DEFAULT_TOPIC_COUNT,
    MAX_BRIEF,
    MAX_GOAL,
    MAX_OPENING,
    MAX_SECTION_TITLE,
    MAX_TOPICS,
    Topic,
)

if TYPE_CHECKING:  # 只为类型标注，运行时不导入（避免把 blueprint 拖进 camel 路径）
    from .blueprint import Blueprint

__all__ = [
    "AuthoringError",
    "DEFAULT_TOPIC_COUNT",
    "ForgedPersonas",
    "assemble_blueprint",
    "distill_material",
    "forge",
    "forge_personas",
    "forge_topics",
]

# 长度上限统一放在 personas.py（那里不依赖 camel，CLI / launcher 校验时也能用）。
# 这里 import 进来，现有的 `authoring.MAX_*` 引用仍然成立。

#: 生成失败时重试几次。两次足够——格式错是偶发的，两次都错说明是提示词的问题。
_MAX_ATTEMPTS = 2


class AuthoringError(RuntimeError):
    """生成失败，或者模型给的东西没法用。消息直接打到终端上。"""


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

PERSONA_SYSTEM = """你是「对谈智能体人格设计师」。用户会给你一个目的，你要设计出两位**立场真的对立**的专家。

设计要点：

1. 两位必须是同一个目的下**天然会吵起来**的两种立场。
   最常用的一根轴是「什么是对的」对「现实中做得到吗」，但你要按目的自己判断——
   选一根真正对得上的轴。两人的差别必须是**方向**上的对立，不能只是程度差别。
2. 两位都要是这个领域里说得上话的人，有具体身份（职业、年资、服务对象），
   不要写「专家 A」「专业人士」这种空壳。
3. 每人必须写清【你的盲区，不要掩饰】——这是对话有没有张力的关键。
   提方案的那位容易脱离现实；挑刺的那位容易为了「做得到」而放松标准。
   盲区写出来，对方戳到它时才接得住，而不是绕开。
4. 第一位（proposer）是**提出方案的人**：负责给出可执行的做法，会被对方挑刺。
   第二位（challenger）是**挑刺的人**：负责指出方案落不了地的地方，他的任务是质疑不是附和。
5. `role` 是这位专家的短角色名（2–8 字），例如「循证营养师」「成本把关人」。
   两人必须是不同的角色，不要都写「专家」。
6. 全程中文。

【不要写发言纪律】——「每轮多少字」「不许自称 AI」「什么时候可以收手」这些
由系统统一追加，你写了会重复。你**只**写这三块：

【人格】
【专业立场】
【你的盲区，不要掩饰】

输出：只输出一个 JSON 对象，不要写任何解释，不要用 ``` 包裹。

{
  "title": "成果物的标题，6–14 字，像个书名，例如「健身饮食实战手册」",
  "slug": "标题的英文短标识，小写字母加连字符，例如 fitness-nutrition",
  "proposer": {
    "role": "短角色名，例如「循证营养师」",
    "name": "两三个字的称呼，例如「营养师」",
    "title": "完整身份，例如「循证运动营养师」",
    "stance": "一句话说明他负责什么，例如「什么是对的」",
    "system_message": "他的完整人格设定，含【人格】【专业立场】【你的盲区，不要掩饰】三块，400 字以内"
  },
  "challenger": { 同上，五个字段一模一样 }
}"""

PERSONA_PROMPT = """目的：

{purpose}
{title_hint}
请按上面的要求，为这个目的设计两位对谈专家。只输出那个 JSON 对象。"""

PERSONA_TITLE_HINT = """
成果物的标题已经定好了，就叫《{title}》，照用，不要另起。
"""


# ---------------------------------------------------------------------------
# 网页拖入的材料：先提炼成一段目的
# ---------------------------------------------------------------------------

DISTILL_SYSTEM = """你是「目的提炼师」。用户会给你一份或多份 Markdown / 文本材料，
内容可能是需求、笔记、文章、提纲、会议记录、代码说明，**不一定已经写成一段目的**。

你的任务是从材料里提炼出一件可以拿去做多方对谈的事。

要求：

1. 只依据材料，不要添加材料里没有的事实、数字、人物或背景。
2. 目的要写清：这份材料想解决什么问题、给谁用、希望得到什么形态的成果。
   200–800 字，最多不超过 2000 字。不要写成「关于……的一切」这种空泛的话。
3. 如果材料本身已经是一段目的，保留它的核心、约束和具体数字，不要改写成空话。
4. 如果材料来自多个文件，找出它们共同指向的那个问题；不要把每个文件各写一句。
5. 标题 6–14 字，像个书名。材料里已经有明确标题（例如第一行 `# ...`）时优先用它。
6. 全程中文。
7. 只输出一个 JSON 对象，不要写任何解释，不要用 ``` 包裹：

{
  "title": "6–14 字的成果标题",
  "distilled_purpose": "200–800 字的目的，最多 2000 字"
}"""

DISTILL_PROMPT = """材料来自：{files}

以下是材料全文：

{material}

---

请提炼出成果标题和目的。只输出那个 JSON 对象。"""


# ---------------------------------------------------------------------------
# 三人及以上：先团队骨架，再一次性展开
# ---------------------------------------------------------------------------

TEAM_SKELETON_SYSTEM = """你是「对谈团队设计师」。用户会给你一个目的和人数，
你要设计一个**分工互补、且至少有一对会真的争起来**的专家团队。

设计要点：

1. 第 1 位是**主提出方**：负责给出可执行的具体方案，后面所有人都会挑他的刺。
   他的专业身份要撑得起这个方案。
2. 其余每一位都要有**不同的关注点**，不能只是换一个称呼。常见的分工方向：
   领域专家（什么是对的）、落地执行者（现实中做不做得到）、成本/风险把关人、
   受益方代表（这件事对谁有什么影响）。你也可以按目的自己选更合适的轴。
3. 团队里至少要有一位**主要挑战者**，他的任务是质疑而不是附和。
   如果所有人都站在同一侧，这个团队就没有存在的意义。
4. 每位都要有具体身份（职业、年资、服务对象），不要写「专家 A」「专业人士」。
5. `role` 是每人 2–8 字的短角色名；全队不能重名。
6. 全程中文。

输出：只输出一个 JSON 对象，不要写任何解释，不要用 ``` 包裹。
不要写完整人格，这里只定分工骨架。

{
  "team": [
    {
      "role": "短角色名，例如「主提出方」",
      "name": "两三个字的称呼，例如「营养师」",
      "title": "完整身份，例如「循证运动营养师」",
      "stance": "一句话说明他负责什么，例如「什么是对的」"
    }
  ]
}"""

TEAM_SKELETON_PROMPT = """目的：

{purpose}
{title_hint}
人数：{people} 位。{plan_block}

请设计这 {people} 位专家的团队分工骨架。只输出那个 JSON 对象。"""

TEAM_PERSONAS_SYSTEM = """你是「对谈智能体人格设计师」。已经有一份团队分工骨架，
你要把它展开成每位专家的完整人格。

硬性要求：

1. 严格按骨架里的人数、顺序、角色、称呼、身份、负责什么来写，不要自行增删或调换。
2. 第 1 位是主提出方：人格里要体现他负责提出可执行方案，会成为其他人的靶子。
3. 第 2 位及以后是回应者：他们的任务是质疑、补充、指出落地问题，不是附和。
4. 每人必须写清【你的盲区，不要掩饰】——这是对话有没有张力的关键。
5. 每人的 `system_message` 只写【人格】【专业立场】【你的盲区，不要掩饰】三块，
   400 字以内。**不要写发言纪律**——「每轮多少字」「不许自称 AI」「什么时候
   可以收手」由系统统一追加，你写了会重复。
6. 全程中文。

输出：只输出一个 JSON 对象，不要写任何解释，不要用 ``` 包裹。

{
  "title": "成果物的标题，6–14 字，像个书名",
  "slug": "标题的英文短标识，小写字母加连字符",
  "personas": [
    {
      "role": "照抄骨架里的角色名",
      "name": "照抄骨架里的称呼",
      "title": "照抄骨架里的身份",
      "stance": "照抄骨架里的负责什么",
      "system_message": "完整人格，含【人格】【专业立场】【你的盲区，不要掩饰】三块，400 字以内"
    }
  ]
}"""

TEAM_PERSONAS_PROMPT = """目的：

{purpose}

人数：{people} 位。{plan_block}

团队分工骨架：

{skeleton}

请把它展开成 {people} 位专家的完整人格。只输出那个 JSON 对象。"""


TOPICS_SYSTEM = """你是「对谈议题设计师」。已经有一组立场和分工不同的专家，以及他们要谈的目的。
你要设计出他们逐个谈过去的议题。

一个议题 = 成果物的一节。选题标准只有一条，也是最要紧的一条：

**如果所有专家对这个议题天然意见一致，就不该收进来。**

每个议题都要是一个他们会被迫表态的具体分歧点，而不是一个宽泛的话题：
「关于睡眠的一切」不是议题，「周末补觉能不能抵消工作日的睡眠债」才是。
至少要有一对专家会在这个议题上真的争起来。

具体要求：

1. 议题要覆盖这个目的真正要紧的方面，**从最基础、最该先定下来的排起**。
   彼此不要重叠，也不要出现「一个议题整个装进另一个议题里面」的情况。
2. 具体到能被追问：带上限定条件、人群、场景或数量级。
3. `opening` 是**第 1 位专家（主提出方）的开场发言**：
   结论先行，摆出他的具体方案，并留出让其他人挑刺的空间。
   250 字以内，要像人在说话，不要写成大纲。
4. `brief` 是写进所有人 system message 的议题描述，要说清「争的是哪一点」。
5. `goal` 是这个议题谈完要收敛出什么。
6. `key` 是英文短标识（小写字母、数字、连字符），用于在命令行里指代这个议题，
   例如 "sleep-debt"。每个 key 不能重复。
7. `section_title` 带中文序号，例如「一、先定基准：热量与蛋白质」。
8. 全程中文。**不要设计收尾议题**（回顾分歧、罗列红线那类），系统会自动补一个。

输出：只输出一个 JSON 对象，不要写任何解释，不要用 ``` 包裹。

{
  "topics": [
    {
      "key": "英文短标识",
      "section_title": "一、带序号的标题",
      "brief": "这个议题争的是哪一点",
      "goal": "谈完要收敛出什么",
      "opening": "第 1 位专家（主提出方）的开场发言"
    }
  ]
}"""

TOPICS_PROMPT = """目的：

{purpose}

参与对谈的专家（共 {people} 位，顺序即发言顺序）：

{personas}

请设计 {count} 个议题，按他们对谈的先后顺序排列。只输出那个 JSON 对象。"""


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def _extract_json(text: str) -> dict:
    """从模型的回复里掏出那个 JSON 对象。

    宽松处：允许外面套 ``` 代码块，也允许前后各有一句人话（模型很爱写
    「好的，以下是设计」）。做法是找到第一个 `{`，再按括号配对找它对应的 `}`，
    中间的部分才是要解析的——字符串里的花括号会被正确跳过。
    """
    if not isinstance(text, str) or not text.strip():
        raise AuthoringError("模型没有返回任何内容。")

    body = text.strip()
    fenced = _FENCE.search(body)
    if fenced:
        body = fenced.group(1).strip()

    start = body.find("{")
    if start < 0:
        raise AuthoringError("模型的回复里找不到 JSON 对象。")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(body)):
        char = body[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                snippet = body[start : index + 1]
                try:
                    data = json.loads(snippet)
                except json.JSONDecodeError as exc:
                    raise AuthoringError(
                        f"模型的输出不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）"
                    ) from None
                if not isinstance(data, dict):
                    raise AuthoringError("模型的输出顶层不是 JSON 对象。")
                return data

    raise AuthoringError("模型的 JSON 没有闭合（少了一个右花括号）。")


def _field(raw: dict, name: str, label: str, limit: int) -> str:
    """取一个必填的文本字段。

    报错里**同时**给中文标签和 JSON 字段名：这句话会原样回到重试提示里，
    模型要照着它改，只给中文标签（「第一位专家的人格设定」）它不知道该动哪个键。
    """
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AuthoringError(f"{label}缺失，或者不是非空文本。（JSON 字段：{name}）")
    value = value.strip()
    if len(value) > limit:
        raise AuthoringError(
            f"{label}太长：上限 {limit} 字，模型给了 {len(value)} 字。"
            f"（JSON 字段：{name}）"
        )
    return value


def _optional_field(raw: dict, name: str, label: str, limit: int) -> str:
    value = raw.get(name)
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise AuthoringError(f"{label}必须是一段文本。（JSON 字段：{name}）")
    value = value.strip()
    if "\n" in value or "\r" in value:
        raise AuthoringError(f"{label}必须是单行。（JSON 字段：{name}）")
    if len(value) > limit:
        raise AuthoringError(
            f"{label}太长：上限 {limit} 字，模型给了 {len(value)} 字。"
            f"（JSON 字段：{name}）"
        )
    return value


def _slug(raw: Any, fallback: str) -> str:
    slug = _NON_SLUG.sub("-", str(raw or "").strip().lower()).strip("-")
    return slug[:40] or fallback


def _parse_persona(raw: Any, label: str, *, index: int, people: int) -> Persona:
    if not isinstance(raw, dict):
        raise AuthoringError(f"{label}必须是一个 JSON 对象。")
    message = _field(
        raw, "system_message", f"{label}的人格设定", MAX_SYSTEM_MESSAGE
    )
    # 提示词已经要求模型只写三块，但 hint 有可能诱导它把纪律也写进来。
    # 纪律是机器契约，必须只由代码追加一次。
    for marker in DISCIPLINE_MARKERS:
        if marker in message:
            raise AuthoringError(
                f"{label}的人格设定里出现了「{marker}」。"
                "发言纪律和收手纪律由系统追加，不要写进人格设定。"
            )
    role = _optional_field(raw, "role", f"{label}的角色名", MAX_ROLE)
    return Persona(
        name=_field(raw, "name", f"{label}的称呼", MAX_NAME),
        title=_field(raw, "title", f"{label}的身份", MAX_PERSONA_TITLE),
        stance=_field(raw, "stance", f"{label}负责什么", MAX_STANCE),
        system_message=message,
        role=role or default_role(index, people),
    )


def _ensure_unique(personas: Sequence[Persona], *, field: str) -> None:
    values = [getattr(p, field).strip() for p in personas]
    duplicates = sorted({v for v in values if values.count(v) > 1 and v})
    if duplicates:
        label = "称呼" if field == "name" else "角色名"
        raise AuthoringError(
            f"有重复的{label}：{'、'.join(duplicates)}。"
            f"每位参与者的{label}必须唯一，否则对话记录分不清谁是谁。"
        )


@dataclass(frozen=True)
class ForgedPersonas:
    """人格生成的产物：标题 + N 位参与者。"""

    title: str
    slug: str
    personas: tuple[Persona, ...]

    @property
    def pair(self) -> tuple[Persona, ...]:
        """老名字。现在可能是 2–5 位，不再是固定的一对。"""
        return self.personas

    @property
    def proposer(self) -> Persona:
        return self.personas[0]

    @property
    def challenger(self) -> Persona:
        return self.personas[1]

    @property
    def people(self) -> int:
        return len(self.personas)


def _parse_personas(
    raw: dict, *, plan: PersonaPlan | None = None
) -> ForgedPersonas:
    """两人模式：解析模型生成的 proposer / challenger。

    只解析模型需要生成的那几位；manual 侧直接取用户给的，不理会模型写了什么。
    这样即使模型漏写/写坏了手写侧，也不会触发一次无意义的重试。
    """
    plan = plan or PersonaPlan(people=2)
    if plan.people != 2:
        raise AuthoringError("两人模式的解析器收到了不是两位的团队。")

    proposer = (
        plan.specs[0].to_persona(1, 2)
        if plan.specs[0].mode == "manual"
        else _parse_persona(
            raw.get("proposer"), "第一位专家（proposer）", index=1, people=2
        )
    )
    challenger = (
        plan.specs[1].to_persona(2, 2)
        if plan.specs[1].mode == "manual"
        else _parse_persona(
            raw.get("challenger"), "第二位专家（challenger）", index=2, people=2
        )
    )
    personas = (proposer, challenger)
    _ensure_unique(personas, field="name")
    _ensure_unique(personas, field="role")
    return ForgedPersonas(
        title=_field(raw, "title", "成果物标题", MAX_TITLE),
        slug=_slug(raw.get("slug"), "duo"),
        personas=personas,
    )


def _parse_skeleton(raw: dict, *, plan: PersonaPlan) -> list[dict]:
    """解析团队分工骨架。manual 位置用用户给的摘要覆盖模型输出。"""
    items = raw.get("team")
    if not isinstance(items, list):
        raise AuthoringError("`team` 缺失，或者不是一个数组。")
    if len(items) != plan.people:
        raise AuthoringError(
            f"团队分工骨架应该有 {plan.people} 位，模型给了 {len(items)} 位。"
        )

    skeleton: list[dict] = []
    for index, (spec, item) in enumerate(zip(plan.specs, items), 1):
        label = f"第 {index} 位专家"
        if spec.mode == "manual":
            persona = spec.to_persona(index, plan.people)
            skeleton.append(
                {
                    "role": persona.role,
                    "name": persona.name,
                    "title": persona.title,
                    "stance": persona.stance,
                }
            )
            continue
        if not isinstance(item, dict):
            raise AuthoringError(f"团队分工骨架的{label}不是 JSON 对象。")
        skeleton.append(
            {
                "role": _field(item, "role", f"{label}的角色名", MAX_ROLE),
                "name": _field(item, "name", f"{label}的称呼", MAX_NAME),
                "title": _field(item, "title", f"{label}的身份", MAX_PERSONA_TITLE),
                "stance": _field(item, "stance", f"{label}负责什么", MAX_STANCE),
            }
        )

    _ensure_unique(
        [
            Persona(
                name=item["name"],
                title=item["title"],
                stance=item["stance"],
                system_message="",
                role=item["role"],
            )
            for item in skeleton
        ],
        field="name",
    )
    _ensure_unique(
        [
            Persona(
                name=item["name"],
                title=item["title"],
                stance=item["stance"],
                system_message="",
                role=item["role"],
            )
            for item in skeleton
        ],
        field="role",
    )
    return skeleton


def _parse_team_personas(raw: dict, *, plan: PersonaPlan) -> ForgedPersonas:
    """三人及以上：解析展开后的 personas 数组。"""
    items = raw.get("personas")
    if not isinstance(items, list):
        raise AuthoringError("`personas` 缺失，或者不是一个数组。")
    if len(items) != plan.people:
        raise AuthoringError(
            f"`personas` 应该有 {plan.people} 位，模型给了 {len(items)} 位。"
        )

    personas: list[Persona] = []
    for index, (spec, item) in enumerate(zip(plan.specs, items), 1):
        label = f"第 {index} 位专家"
        if spec.mode == "manual":
            personas.append(spec.to_persona(index, plan.people))
            continue
        personas.append(_parse_persona(item, label, index=index, people=plan.people))

    _ensure_unique(personas, field="name")
    _ensure_unique(personas, field="role")
    return ForgedPersonas(
        title=_field(raw, "title", "成果物标题", MAX_TITLE),
        slug=_slug(raw.get("slug"), "team" if plan.people > 2 else "duo"),
        personas=tuple(personas),
    )


def _plan_prompt_block(plan: PersonaPlan) -> str:
    """把用户的人格意图和团队要求拼进提示词。

    手写侧只给 `role/name/title/stance` 摘要——**不**注入完整 `system_message`。
    那段可能有上万字，塞进这里既贵，也扩大提示词注入面；对方只要能看懂
    他「是谁、负责什么」就够了。
    """
    lines: list[str] = []
    for index, spec in enumerate(plan.specs, 1):
        if plan.people == 2:
            label = (
                "第一位专家（proposer，主提出方）"
                if index == 1
                else "第二位专家（challenger，主要挑战者）"
            )
        else:
            label = f"第 {index} 位专家"
        if spec.mode == "manual":
            role = spec.role or default_role(index, plan.people)
            lines.append(
                f"- {label}由用户指定，必须原样采用：角色「{role}」，"
                f"称呼「{spec.name}」，身份「{spec.title}」，负责「{spec.stance}」。"
            )
        elif spec.mode == "hint":
            lines.append(f"- {label}请按这个方向设计：{spec.hint}")

    parts: list[str] = []
    if plan.team_hint:
        parts.append(
            "【团队分工要求】\n"
            f"{plan.team_hint}\n"
            f"这是强偏好：人数固定为 {plan.people} 位。如果要求里的角色数与人数"
            "对不上，请自行合并或补充，但要点名出现的角色尽量保留。"
        )
    if lines:
        parts.append("【用户对人格的额外要求】\n" + "\n".join(lines))
    if not parts:
        return ""
    return "\n\n" + "\n\n".join(parts)


def _parse_topics(raw: dict) -> tuple[Topic, ...]:
    items = raw.get("topics")
    if not isinstance(items, list) or not items:
        raise AuthoringError("`topics` 缺失，或者不是一个非空的数组。")
    if len(items) > MAX_TOPICS:
        # 超出的直接砍掉，不为此重试一次——模型给多了不算错，砍掉即可。
        items = items[:MAX_TOPICS]

    topics: list[Topic] = []
    # 收尾议题的 key 是保留的，模型要是生成了一个同名的，这里给它让开
    seen = {CLOSING_KEY}
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise AuthoringError(f"第 {index} 个议题不是 JSON 对象。")
        label = f"第 {index} 个议题"
        topics.append(
            Topic(
                key=_unique_key(item.get("key"), index, seen),
                section_title=_field(item, "section_title", f"{label}的标题", MAX_SECTION_TITLE),
                brief=_field(item, "brief", f"{label}的 brief", MAX_BRIEF),
                goal=_field(item, "goal", f"{label}的 goal", MAX_GOAL),
                opening=_field(item, "opening", f"{label}的开场发言", MAX_OPENING),
            )
        )
    return tuple(topics)


def _unique_key(raw: Any, index: int, seen: set[str]) -> str:
    """把模型给的 key 收拾成一个能用的标识，并保证不重名。"""
    base = _slug(raw, f"topic-{index}")
    key = base
    suffix = 2
    while key in seen:
        key = f"{base}-{suffix}"
        suffix += 1
    seen.add(key)
    return key


# ---------------------------------------------------------------------------
# 调用
# ---------------------------------------------------------------------------


def _step(agent: ChatAgent, prompt: str) -> str:
    response = agent.step(prompt)
    msgs = getattr(response, "msgs", None) or []
    return str(msgs[0].content).strip() if msgs else ""


def _ask_json(
    agent: ChatAgent,
    prompt: str,
    parse: Callable[[dict], Any],
    *,
    what: str,
) -> Any:
    """让 agent 吐一份能用的 JSON，不行就带着错误信息重来一次。

    重试用的是**同一个 agent**，所以模型看得到自己刚写的那份，改起来有依据；
    直接把提示词重发一遍的话，它多半会原样再错一次。
    """
    attempt_prompt = prompt
    last_error: Exception | None = None
    for _ in range(_MAX_ATTEMPTS):
        text = _step(agent, attempt_prompt)
        try:
            return parse(_extract_json(text))
        except AuthoringError as exc:
            last_error = exc
            attempt_prompt = (
                f"{prompt}\n\n"
                f"【上一次的输出不能用了】{exc}\n"
                "请重新输出一次。**只输出一个 JSON 对象**，"
                "不要写任何解释，不要用 ``` 包裹。"
            )
    raise AuthoringError(f"{what}生成失败：{last_error}")


def _parse_distilled(raw: dict) -> Purpose:
    title = _field(raw, "title", "提炼出的标题", MAX_TITLE)
    if "\n" in title or "\r" in title:
        raise AuthoringError("提炼出的标题必须是单行。")
    text = _field(raw, "distilled_purpose", "提炼出的目的", MAX_PURPOSE)
    if len(text) < MIN_PURPOSE:
        raise AuthoringError(
            f"提炼出的目的太短了（{len(text)} 字）：至少 {MIN_PURPOSE} 字，"
            "要说清「给谁、解决什么、想要什么成果」。"
        )
    return Purpose(text=text, title=title)


def distill_material(
    material: str, *, backend, files: Sequence[str] = ()
) -> Purpose:
    """把网页拖入的材料提炼成一段目的。

    这是多人/单人模式之外**额外的一次模型调用**：材料可能是一份长文档，
    不适合直接塞进每个人的 system message，所以先压成一段 ≤2000 字的目的，
    后面的 forge 仍然只认 Purpose。
    """
    if not isinstance(material, str):
        raise AuthoringError("材料必须是文本。")
    text = material.strip()
    if not text:
        raise AuthoringError("材料是空的。")
    if len(text) > MAX_MATERIAL:
        raise AuthoringError(
            f"材料太长：上限 {MAX_MATERIAL} 字，收到 {len(text)} 字。"
            "请先精简材料，或者拆成几次。"
        )

    names = "、".join(name for name in files if name) or "用户拖入的材料"
    prompt = DISTILL_PROMPT.format(files=names, material=text)
    agent = ChatAgent(system_message=DISTILL_SYSTEM, model=backend)
    return _ask_json(agent, prompt, _parse_distilled, what="目的提炼")


def forge_personas(
    purpose: Purpose, *, backend, plan: PersonaPlan | None = None
) -> ForgedPersonas:
    """按目的生成 N 位参与者（外加大标题）。

    - 全部 manual：一次人格调用都不发；
    - 两人：一次调用，提示词仍要求一位主提出方 + 一位主要挑战者；
    - 三人及以上：先一次团队骨架，再一次展开全部人格。

    `plan` 里标记为 `manual` 的位置不交给模型。
    """
    plan = plan or PersonaPlan()
    if plan.all_manual:
        title = purpose.title or "对谈成果"
        return ForgedPersonas(
            title=title,
            slug=_slug(title, "team" if plan.people > 2 else "duo"),
            personas=tuple(
                spec.to_persona(index, plan.people)
                for index, spec in enumerate(plan.specs, 1)
            ),
        )

    hint = PERSONA_TITLE_HINT.format(title=purpose.title) if purpose.title else ""
    plan_block = _plan_prompt_block(plan)

    if plan.people == 2:
        prompt = PERSONA_PROMPT.format(purpose=purpose.text, title_hint=hint)
        # 全 auto 且没有团队要求时这里是空串，提示词与改动前逐字一致。
        prompt += plan_block
        agent = ChatAgent(system_message=PERSONA_SYSTEM, model=backend)
        return _ask_json(
            agent,
            prompt,
            lambda raw: _parse_personas(raw, plan=plan),
            what="人格",
        )

    skeleton_prompt = TEAM_SKELETON_PROMPT.format(
        purpose=purpose.text,
        title_hint=hint,
        people=plan.people,
        plan_block=plan_block,
    )
    skeleton_agent = ChatAgent(system_message=TEAM_SKELETON_SYSTEM, model=backend)
    skeleton = _ask_json(
        skeleton_agent,
        skeleton_prompt,
        lambda raw: _parse_skeleton(raw, plan=plan),
        what="团队分工",
    )

    personas_prompt = TEAM_PERSONAS_PROMPT.format(
        purpose=purpose.text,
        people=plan.people,
        plan_block=plan_block,
        skeleton=json.dumps({"team": skeleton}, ensure_ascii=False, indent=2),
    )
    personas_agent = ChatAgent(system_message=TEAM_PERSONAS_SYSTEM, model=backend)
    return _ask_json(
        personas_agent,
        personas_prompt,
        lambda raw: _parse_team_personas(raw, plan=plan),
        what="人格",
    )


def forge_topics(
    purpose: Purpose,
    personas: Sequence[Persona],
    *,
    backend,
    count: int = DEFAULT_TOPIC_COUNT,
) -> tuple[Topic, ...]:
    """按目的和这组专家，生成一组他们会真的吵起来的议题。

    注意这里**只生成正文议题**，收尾议题由 `topics.closing_topic` 按代码造
    （它的输入是前面全部对话，形状和普通议题不一样，见那个函数的说明）。
    """
    people = len(personas)
    lines = []
    for index, persona in enumerate(personas, 1):
        position = "主提出方，负责开场" if index == 1 else "回应者"
        lines.append(
            f"第 {index} 位（{position}）：{persona.name}（{persona.title}），"
            f"角色「{persona.display_role}」，负责「{persona.stance}」"
        )
    prompt = TOPICS_PROMPT.format(
        purpose=purpose.text,
        people=people,
        personas="\n".join(lines),
        count=count,
    )
    agent = ChatAgent(system_message=TOPICS_SYSTEM, model=backend)
    return _ask_json(agent, prompt, _parse_topics, what="议题")


def assemble_blueprint(
    purpose: Purpose,
    forged: ForgedPersonas,
    topics: Sequence[Topic],
    *,
    origin: str = "",
) -> "Blueprint":
    """把两段生成结果拼成蓝图。

    `forge` 和网页启动器共用这一份，避免「标题取谁」这类规则两边各写一遍。
    函数内 import 是为了让 `show` 子命令只 import blueprint 而不拖进 camel。
    """
    from .blueprint import Blueprint

    return Blueprint(
        purpose=purpose,
        title=purpose.title or forged.title,
        slug=forged.slug,
        personas=forged.personas,
        topics=tuple(topics),
        origin=origin,
    )


def forge(
    purpose: Purpose,
    *,
    backend,
    topic_count: int = DEFAULT_TOPIC_COUNT,
    plan: PersonaPlan | None = None,
) -> "Blueprint":
    """目的 → 蓝图（标题 + N 位参与者 + 一组议题）。

    两人正常两次调用（人格 + 议题）；三人及以上三次（骨架 + 人格 + 议题）；
    全部手写时只有一次（议题）。返回值是 `blueprint.Blueprint`。
    """
    plan = plan or PersonaPlan()
    forged = forge_personas(purpose, backend=backend, plan=plan)
    topics = forge_topics(
        purpose, forged.personas, backend=backend, count=topic_count
    )
    return assemble_blueprint(purpose, forged, topics)
