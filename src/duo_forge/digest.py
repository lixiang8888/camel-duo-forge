"""把对话整理成成果文件。

搬运自 camel-fitness-nutrition-duo 的 digest.py。这个模块本来就是领域无关的
（它的活是「压缩」，不是「懂行」），所以改动几乎只有一处：所有写死「健身饮食手册」
的地方，换成调用方传进来的标题。

注意：这一步**不是第三位智能体**。它没有独立人格，也不参与讨论，只是一个格式
整理环节——把各位专家已经说过的话，压缩成能直接用的条文。观点全部来自对话本身，
整理环节被明确要求不得添加新观点。

这个约束在泛化之后**更要紧了**：目的可以是任何东西（睡眠、装修、面试准备），
编辑环节对那个领域一无所知，唯一能保证它不胡说八道的，就是「只整理对话里
出现过的内容」这一条。
"""

from __future__ import annotations

import re
from typing import Sequence

from camel.agents import ChatAgent

from .personas import Persona
from .topics import Topic

EDITOR_SYSTEM = """你是《{title}》的文字编辑。

你唯一的工作是把各位专家的对话整理成读者可直接照做的条文。

硬性要求：
1. 只整理对话里已经出现过的内容。严禁添加任何新观点、新数据。
2. 用「怎么做」的语气写，不要写成对话实录。
3. 凡是出现数字的地方，必须保留原数字和单位。
4. 专家们有分歧的地方，必须如实并列写出各方立场，不要擅自调和、也不要只挑两个人的说法。
5. 直接输出 Markdown 正文，不要写「好的」「以下是」这类开场白。
6. 如果对话里某一点说得不具体、无法执行，就不要写进这份文件。
7. 模板要求在最后单独一行输出分歧状态标记；必须原样输出，不要省略、不要解释。"""

SECTION_TEMPLATE = """议题：{title}

议题背景：
{brief}

本轮要收敛的结论：
{goal}

以下是各位专家围绕这个议题的完整对话：

{dialogue}

---

请把它整理成一节。

输出格式（严格遵守）：

## {title}

### 要点
（3–5 条，每条一句话，直接给可执行的做法，带具体标准）

### 具体怎么做
（分条展开，每条都要具体到读者今天就能照做）

### {disagreement_heading}
（如实写出各位专家没有谈拢的地方，各自立场和理由。
 如果确实完全一致，写「本议题各方无实质分歧」。）

### 注意
（这一节里最容易做错、或者不适合哪些人的地方。没有就写「无」。）

最后，在整节输出的**最后单独一行**原样写一个分歧状态标记（只写一个，不要解释，
也不要把两个都写出来）：
- 这一节里还有任何各方没有谈拢的点，写：【本节分歧：有】
- 各方已经完全一致，写：【本节分歧：无】

不要使用一级标题，不要重复输出议题名称之外的标题。"""

CLOSING_TEMPLATE = """以下是各位专家在收尾讨论中的完整对话，以及他们此前各议题的结论摘要：

{dialogue}

---

请整理成本文件的附录，输出格式（严格遵守）：

## 附录 A · 分歧备忘
（把整个过程里各方始终没有谈拢的点列出来。每条写：分歧点 / 各方立场（逐位列明
 是哪位专家、什么理由）/ 读者该怎么选。
 这是这份文件最有价值的部分——如实呈现，不要和稀泥。）

## 附录 B · 红线信号
（出现哪些信号时必须停止当前的做法、转而去求助专业人士（按这个目的所属的领域来定该找谁），
 而不是继续自己调整。只写对话里提到的。）

## 附录 C · 行动自检清单
（一份可以定期勾选的清单，用 Markdown 复选框 `- [ ]` 格式，控制在 8 条以内。
 每条必须是能明确判断「做到 / 没做到」的，不要写「保持好心情」这种无法判定的条目。）

最后，在整份附录输出的**最后单独一行**原样写一个未解决分歧标记（只写一个，
不要解释，也不要把两个都写出来）：
- 整个过程里还有任何始终没有谈拢的点，写：【未解决分歧：有】
- 所有点都已谈拢，写：【未解决分歧：无】"""


# --------------------------------------------------------------------------
# 速查版：把整份文件再压成一页
# --------------------------------------------------------------------------
# 完整版很详细，实际用起来大多数时候用不到那么多。于是每轮多花一次模型调用，
# 额外交付一份只留最常用部分的一页速查。
#
# 关键取舍：**它的输入是已经拼好的完整文件，不是对话**。这样速查版里的每一句
# 都能在完整版里找到出处，两份不可能互相矛盾；也正因为读的是成品，
# 压缩可以做得比「整理」更狠——它不需要再判断哪些观点站得住。
QUICK_SYSTEM = """你是《{title}》的编辑，现在要把一份已经写完的文件压成一页速查版。

硬性要求：
1. 只做删减与归类。严禁添加完整版里没有的建议、数字或说法。
2. 数字必须原样搬过来，不许改写、四舍五入或换算单位。
3. 宁可少写几条，也不要为了填满版面而复述。整篇控制在 400 字以内。
4. 直接输出 Markdown 正文，不要写「好的」「以下是」这类开场白。"""

QUICK_TEMPLATE = """这是完整版的全文：

{handbook}

---

请压缩成一页速查版，输出格式（严格遵守）：

## 最要紧的几条
（3–4 条。只留最该先盯住的那几条，写完整版里给过的具体标准。）

## 每天就这么做
（5–8 条，每条一句话、以动词开头。挑完整版里最该先做到的动作，按重要性排。）

## 做不到的时候
（3 条以内。完整版里给的、没法按计划来时能用的兜底动作。）

## 红线
（完整版里写明的「必须停下来去求助」的信号，最多 4 条。没有就写「无」。）

不要使用一级标题。不要复述完整版的章节结构，
不要写「各方分歧」这类只有完整版才需要的内容。"""


# --------------------------------------------------------------------------
# 分歧标记：整理环节给 pipeline 的机器契约
# --------------------------------------------------------------------------
# 整理出来的成果是自然语言，pipeline 没法可靠地「读懂」它有没有分歧。
# 所以模板要求编辑在正文最后单独写一个标记，和 society 的【无异议】一样是机器契约；
# 只是这里允许半角冒号，标记结构不变。拼装成品时由 strip_disagreement_markers 摘掉。

_DISAGREEMENT_MARKER = re.compile(
    r"【(?:本节分歧|未解决分歧)[ \t]*[:：][ \t]*(?P<state>有|无)】"
)

#: 整行匹配，连同行尾换行一起删；标记被写在正文中间时再由下面的 sub 兜底。
_MARKER_LINE = re.compile(
    r"(?m)^[ \t]*【(?:本节分歧|未解决分歧)[ \t]*[:：][ \t]*(?:有|无)】[ \t]*\n?"
)


def has_disagreement(text: str) -> bool:
    """这段整理结果是否标着「还有未谈拢的分歧」。

    **没有标记按「无分歧」处理。** 标记是提示词里的硬性格式要求；模型连它都
    没写，说明这一节的格式已经坏了，把它当成分歧去多跑一轮全员调用并不划算。
    这一点和 society 的 `_is_concession` 相反，那里的误判会删掉发言，这里
    最多是多花一轮或少花一轮，所以取更省钱的默认值。
    """
    return any(
        match.group("state") == "有"
        for match in _DISAGREEMENT_MARKER.finditer(text)
    )


def strip_disagreement_markers(text: str) -> str:
    """把给代码看的分歧标记从正文里摘掉，不留给读者。"""
    cleaned = _MARKER_LINE.sub("", text)
    cleaned = _DISAGREEMENT_MARKER.sub("", cleaned)
    return cleaned.rstrip() + "\n" if cleaned.strip() else ""


def convergence_excerpt(handbook: str) -> str:
    """从最终成果里抽出统一轮真正需要看的部分。

    只留每节的「要点」、各节的分歧小节、以及附录 A 的分歧备忘。完整版动辄
    上万字，五个人各读一遍既慢又贵；而统一轮要处理的恰恰只有这几块。
    抽不出来（格式被模型写坏了）就退回全文，宁可贵一点，也别让统一轮闭着眼统一。
    """
    kept: list[str] = []
    section_title: str | None = None
    title_emitted = False
    keep = False
    for line in handbook.splitlines():
        if line.startswith("## ") and not line.startswith("### "):
            section_title = line
            title_emitted = False
            keep = False
            if line.startswith("## 附录 A"):
                kept.append(line)
                title_emitted = True
                keep = True
            continue
        if line.startswith("#"):
            relevant = line.startswith(
                ("### 要点", "### 两人分歧", "### 各方分歧")
            )
            # 正文小节只有在抽到有用的小节时才带上标题，免得留下一串空标题。
            if relevant and not title_emitted and section_title:
                kept.append(section_title)
                title_emitted = True
            keep = relevant
            if keep:
                kept.append(line)
            continue
        if keep:
            kept.append(line)

    excerpt = "\n".join(kept).strip()
    if not excerpt:
        return handbook.strip()
    return "（以下摘自最终成果：各节要点、各节分歧与附录 A。）\n\n" + excerpt


def assemble_convergence(
    unified: str, reservations: Sequence[tuple[str, str]]
) -> str:
    """统一轮的成果：统一结论 + 仍保留的意见。

    这一节由代码拼装，不再多花一次模型调用——统一结论就是主提出方在听完
    所有回应后的那句发言，保留意见就是回应者没有回「【无异议】」的原话。
    """
    conclusion = unified.strip() or (
        "（主提出方在最终轮没有给出明确的统一结论，以下保留意见即为最终状态。）"
    )
    lines = [
        "## 附录 D · 最终统一意见",
        "",
        "> 全部议题结束后，专家们又用一轮专门收敛仍未谈拢的地方。",
        "> 附录 A 记录的是统一前的分歧；统一后的状态以本节为准。",
        "",
        "### 统一结论",
        "",
        conclusion,
        "",
        "### 保留意见",
        "",
    ]
    if reservations:
        for name, text in reservations:
            body = (text.strip() or "（没有明确表态）").replace("\n", "\n  ")
            lines.append(f"- **{name}**：{body}")
    else:
        lines.append("- 无。所有回应者都明确表示接受统一结论。")
    return "\n".join(lines).strip() + "\n"


def editor_system(title: str) -> str:
    return EDITOR_SYSTEM.format(title=title)


def header(title: str, personas: Sequence[Persona], *, unified: bool = False) -> str:
    """成果文件开头的门面。

    由代码生成、不走模型：它是**说明这份文件是怎么来的**，不该由被说明的对象
    自己来写（模型写这段时几乎一定会开始加内容）。

    `unified` 表示附上了附录 D。只在真的跑过统一轮时才提它——没跑却写一句
    「见附录 D」，读者会翻到一个不存在的小节。
    """
    people = len(personas)
    if people == 2:
        first, second = personas
        roster = f"""> 本文件由两个立场相反的智能体对谈产出：
> **{first.name}**（{first.title}，角色「{first.display_role}」，负责「{first.stance}」）
> ×
> **{second.name}**（{second.title}，角色「{second.display_role}」，负责「{second.stance}」）。"""
        closing = "所有结论都来自两人的分歧与妥协。"
    else:
        lines = [
            f"> 本文件由 {people} 个立场与分工不同的智能体对谈产出："
        ]
        for index, persona in enumerate(personas, 1):
            position = "主提出方" if index == 1 else "回应者"
            lines.append(
                f"> **{persona.name}**（{persona.title}，角色「"
                f"{persona.display_role}」，负责「{persona.stance}」，{position}）"
            )
        roster = "\n".join(lines)
        closing = f"所有结论都来自这 {people} 位专家的分歧与妥协。"

    note = "凡是他们没谈拢的地方，附录 A 里原样保留，没有替你抹平。"
    if unified:
        note += (
            "\n> 全部议题结束后，他们又用一轮把仍有分歧的地方收敛成附录 D；"
            "谁还保留意见，那里逐条写明。"
        )

    return f"""# {title}

{roster}
>
> {closing}
> {note}

**怎么用**：先读第一节定基准，再挑对你有用的部分执行。
不要试图一次全部做到。"""


def quick_header(title: str, full_name: str, *, unified: bool = False) -> str:
    extra = "、最终统一意见（附录 D）" if unified else ""
    return f"""# {title} · 速查版

> 这是从完整版 `{full_name}` 压缩出来的一页速查：只留最常用的部分，没有新增任何说法。
> 分节展开的做法、各方始终没谈拢的地方（附录 A）{extra}、行动自检清单（附录 C）
> 都在完整版里，需要时再翻。"""


def footer() -> str:
    """免责声明。

    泛化之后这段话**比原来更要紧**：原来只知道是「健身饮食」，写一句「不构成
    医疗建议」就够了；现在什么目的都可能进来，所以只能写通用的一条——凡是
    需要专业判断的事，去找那个专业的人。
    """
    return """
---

*本文件由 camel-duo-forge 生成：多位立场与分工不同的 AI 专家围绕一个给定目的对谈，
再由编辑环节整理成文。内容仅供参考，不构成专业意见——涉及医疗、法律、财务
这类专业问题时，请找相应的人，不要只看这里。*"""


def _generate(system: str, prompt: str, backend) -> str:
    agent = ChatAgent(system_message=system, model=backend)
    response = agent.step(prompt)
    msgs = getattr(response, "msgs", None) or []
    return str(msgs[0].content).strip() if msgs else ""


def digest_section(
    topic: Topic, dialogue: str, *, backend, title: str, people: int = 2
) -> str:
    prompt = SECTION_TEMPLATE.format(
        title=topic.section_title,
        brief=topic.brief,
        goal=topic.goal,
        dialogue=dialogue,
        disagreement_heading="两人分歧" if people == 2 else "各方分歧",
    )
    return _generate(editor_system(title), prompt, backend)


def digest_closing(dialogue: str, *, backend, title: str) -> str:
    prompt = CLOSING_TEMPLATE.format(dialogue=dialogue)
    return _generate(editor_system(title), prompt, backend)


def digest_quick(handbook: str, *, backend, title: str) -> str:
    """完整版 → 一页速查版。

    收的是**拼好的全文**而不是对话或各节正文：这样「压缩」这一步没有任何
    新信息可加，速查版里也就不可能出现完整版没有的说法。
    """
    return _generate(
        QUICK_SYSTEM.format(title=title), QUICK_TEMPLATE.format(handbook=handbook), backend
    )


def assemble(
    sections: list[str], closing: str, *, header: str, convergence: str = ""
) -> str:
    body = "\n\n---\n\n".join(s.strip() for s in sections if s.strip())
    parts = [header.strip(), body]
    if closing.strip():
        parts.append(closing.strip())
    if convergence.strip():
        parts.append(convergence.strip())
    parts.append(footer().strip())
    return "\n\n".join(parts) + "\n"


def assemble_quick(body: str, *, header: str) -> str:
    """速查版的拼装：页眉 + 正文 + 免责声明。

    免责声明必须跟着走：速查版是会被人**单独打开、单独转发**的那一份
    ——「一页纸」正是它容易被转出去的原因，所以那句话不能只留在完整版里。
    """
    return "\n\n".join([header.strip(), body.strip(), footer().strip()]) + "\n"
