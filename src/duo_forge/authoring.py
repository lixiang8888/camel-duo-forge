"""按目的生成两位专家的人格与议题。

这是新项目相对 camel-fitness-nutrition-duo 的**全部增量**。那边的人格和议题是
手写死的（`personas.py` 里的营养师与教练、`topics.py` 里的五个健身议题），
这里换成两次模型调用：

    forge_personas(目的) → 两位立场对立的专家       （功能 1）
    forge_topics(目的, 两位专家) → 一组议题         （功能 2）

## 三个设计上的讲究

**一、输出是 JSON，而且带重试。** 格式错一次就整个流程作废太浪费，
所以解析失败时会把**具体的错**回给模型，让它重来一次（同一份 memory，
它看得到自己刚写的那份）。两次都错才抛 `AuthoringError`。

**二、解析要宽松，校验要严。** 模型很爱在 JSON 外面套一层 ``` 或者写一句
「好的，以下是」，所以 `_extract_json` 会自己去文本里找那个 JSON 对象；
但字段本身一个字都不放过——缺字段、类型不对、超长，全部当场报错并说清是哪一项。

**三、分歧轴是选题的第一标准。** 两个人格必须**真的会吵**，议题必须是他们
**天然意见不一致**的具体问题。这是整个项目产出有没有信息量的唯一来源：
两位互相附和，成果就是一锅正确的废话。所以两处提示词里这条都写在最前面。

## 一个刻意的不对称

人格里的**发言纪律不归模型写**（见 `personas.py` 开头）：模型只写
【人格】【专业立场】【你的盲区】三块，纪律由代码追加。因为其中一条
（挑战者「没有新的反对意见时只回【无异议】」）是 `society` 提前收手的硬契约，
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
    MAX_STANCE,
    MAX_SYSTEM_MESSAGE,
    Persona,
    PersonaPlan,
)
from .purpose import Purpose
from .topics import (
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
5. 全程中文。

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
    "name": "两三个字的称呼，例如「营养师」",
    "title": "完整身份，例如「循证运动营养师」",
    "stance": "一句话说明他负责什么，例如「什么是对的」",
    "system_message": "他的完整人格设定，含【人格】【专业立场】【你的盲区，不要掩饰】三块，400 字以内"
  },
  "challenger": { 同上，四个字段一模一样 }
}"""

PERSONA_PROMPT = """目的：

{purpose}
{title_hint}
请按上面的要求，为这个目的设计两位对谈专家。只输出那个 JSON 对象。"""

PERSONA_TITLE_HINT = """
成果物的标题已经定好了，就叫《{title}》，照用，不要另起。
"""


TOPICS_SYSTEM = """你是「对谈议题设计师」。已经有一对立场对立的专家，以及他们要谈的目的。
你要设计出他们逐个谈过去的议题。

一个议题 = 成果物的一节。选题标准只有一条，也是最要紧的一条：

**如果两位专家对这个议题天然意见一致，就不该收进来。**

每个议题都要是一个他们会被迫表态的具体分歧点，而不是一个宽泛的话题：
「关于睡眠的一切」不是议题，「周末补觉能不能抵消工作日的睡眠债」才是。

具体要求：

1. 议题要覆盖这个目的真正要紧的方面，**从最基础、最该先定下来的排起**。
   彼此不要重叠，也不要出现「一个议题整个装进另一个议题里面」的情况。
2. 具体到能被追问：带上限定条件、人群、场景或数量级。
3. `opening` 是**第一位专家（proposer，提方案的那位）的开场发言**：
   结论先行，摆出他的具体方案，并留出让对方挑刺的空间。
   250 字以内，要像人在说话，不要写成大纲。
4. `brief` 是写进两人 system message 的议题描述，要说清「争的是哪一点」。
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
      "opening": "方案提出方的开场发言"
    }
  ]
}"""

TOPICS_PROMPT = """目的：

{purpose}

两位专家：

- {a_name}（{a_title}）—— 负责「{a_stance}」，他的方案会被对方挑刺
- {b_name}（{b_title}）—— 负责「{b_stance}」

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


def _slug(raw: Any, fallback: str) -> str:
    slug = _NON_SLUG.sub("-", str(raw or "").strip().lower()).strip("-")
    return slug[:40] or fallback


def _parse_persona(raw: Any, label: str) -> Persona:
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
    return Persona(
        name=_field(raw, "name", f"{label}的称呼", MAX_NAME),
        title=_field(raw, "title", f"{label}的身份", MAX_PERSONA_TITLE),
        stance=_field(raw, "stance", f"{label}负责什么", MAX_STANCE),
        system_message=message,
    )


@dataclass(frozen=True)
class ForgedPersonas:
    """第一次调用的产物：标题 + 两位专家。"""

    title: str
    slug: str
    proposer: Persona
    challenger: Persona

    @property
    def pair(self) -> tuple[Persona, Persona]:
        return (self.proposer, self.challenger)


def _parse_personas(
    raw: dict, *, plan: PersonaPlan | None = None
) -> ForgedPersonas:
    """只解析模型需要生成的那几位；manual 侧直接取用户给的，不理会模型写了什么。

    这样即使模型漏写/写坏了手写侧，也不会触发一次无意义的重试。
    """
    plan = plan or PersonaPlan()
    proposer = (
        plan.proposer.to_persona()
        if plan.proposer.mode == "manual"
        else _parse_persona(raw.get("proposer"), "第一位专家（proposer）")
    )
    challenger = (
        plan.challenger.to_persona()
        if plan.challenger.mode == "manual"
        else _parse_persona(raw.get("challenger"), "第二位专家（challenger）")
    )
    return ForgedPersonas(
        title=_field(raw, "title", "成果物标题", 60),
        slug=_slug(raw.get("slug"), "duo"),
        proposer=proposer,
        challenger=challenger,
    )


def _plan_prompt_block(plan: PersonaPlan) -> str:
    """把用户的人格意图拼进人格提示词。

    手写侧只给 `name/title/stance` 摘要——**不**注入完整 `system_message`。
    那段可能有上万字，塞进这里既贵，也扩大提示词注入面；对方只要能看懂
    他「是谁、负责什么」就够了。
    """
    lines: list[str] = []
    for side, spec, label in (
        ("proposer", plan.proposer, "第一位专家（proposer）"),
        ("challenger", plan.challenger, "第二位专家（challenger）"),
    ):
        if spec.mode == "manual":
            lines.append(
                f"- {label}由用户指定，必须原样采用：称呼「{spec.name}」，"
                f"身份「{spec.title}」，负责「{spec.stance}」。"
                "你要设计的另一位必须与它形成真正的对立。"
            )
        elif spec.mode == "hint":
            lines.append(f"- {label}请按这个方向设计：{spec.hint}")
    if not lines:
        return ""
    return "\n\n【用户对人格的额外要求】\n" + "\n".join(lines)


def _parse_topics(raw: dict) -> tuple[Topic, ...]:
    items = raw.get("topics")
    if not isinstance(items, list) or not items:
        raise AuthoringError("`topics` 缺失，或者不是一个非空的数组。")
    if len(items) > MAX_TOPICS:
        # 超出的直接砍掉，不为此重试一次——模型给多了不算错，砍掉即可。
        items = items[:MAX_TOPICS]

    topics: list[Topic] = []
    # 收尾议题的 key 是保留的，模型要是生成了一个同名的，这里给它让开
    seen = {"closing"}
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


def forge_personas(
    purpose: Purpose, *, backend, plan: PersonaPlan | None = None
) -> ForgedPersonas:
    """功能一：按目的生成两位立场对立的专家（外加大标题）。

    `plan` 里标记为 `manual` 的侧不交给模型。两位都 manual 时**一次人格模型调用都
    不发**：只返回 `purpose.title`（或「对谈成果」）当标题，议题仍由 `forge_topics`
    生成。只改一位时仍要调用一次人格模型——它至少还得生成另一位和标题。
    """
    plan = plan or PersonaPlan()
    if plan.both_manual:
        title = purpose.title or "对谈成果"
        return ForgedPersonas(
            title=title,
            slug=_slug(title, "duo"),
            proposer=plan.proposer.to_persona(),
            challenger=plan.challenger.to_persona(),
        )

    hint = PERSONA_TITLE_HINT.format(title=purpose.title) if purpose.title else ""
    prompt = PERSONA_PROMPT.format(purpose=purpose.text, title_hint=hint)
    # 全 auto 时这里返回空串，提示词与改动前逐字一致。
    prompt += _plan_prompt_block(plan)
    agent = ChatAgent(system_message=PERSONA_SYSTEM, model=backend)
    return _ask_json(
        agent,
        prompt,
        lambda raw: _parse_personas(raw, plan=plan),
        what="人格",
    )


def forge_topics(
    purpose: Purpose,
    personas: Sequence[Persona],
    *,
    backend,
    count: int = DEFAULT_TOPIC_COUNT,
) -> tuple[Topic, ...]:
    """功能二：按目的和这两位专家，生成一组他们会真的吵起来的议题。

    注意这里**只生成正文议题**，收尾议题由 `topics.closing_topic` 按代码造
    （它的输入是前面全部对话，形状和普通议题不一样，见那个函数的说明）。
    """
    proposer, challenger = personas[0], personas[1]
    prompt = TOPICS_PROMPT.format(
        purpose=purpose.text,
        a_name=proposer.name,
        a_title=proposer.title,
        a_stance=proposer.stance,
        b_name=challenger.name,
        b_title=challenger.title,
        b_stance=challenger.stance,
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
        personas=forged.pair,
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
    """目的 → 蓝图（标题 + 两位专家 + 一组议题）。

    正常是两次模型调用；只改一位仍是两次（人格 + 议题）；两位都手写时只有一次
    （议题）。返回值是 `blueprint.Blueprint`。
    """
    forged = forge_personas(purpose, backend=backend, plan=plan)
    topics = forge_topics(
        purpose, forged.pair, backend=backend, count=topic_count
    )
    return assemble_blueprint(purpose, forged, topics)
