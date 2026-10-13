"""把对话整理成成果文件。

搬运自 camel-fitness-nutrition-duo 的 digest.py。这个模块本来就是领域无关的
（它的活是「压缩」，不是「懂行」），所以改动几乎只有一处：所有写死「健身饮食手册」
的地方，换成调用方传进来的标题。

注意：这一步**不是第三位智能体**。它没有独立人格，也不参与讨论，只是一个格式
整理环节——把两位专家已经说过的话，压缩成能直接用的条文。观点全部来自对话本身，
整理环节被明确要求不得添加新观点。

这个约束在泛化之后**更要紧了**：目的可以是任何东西（睡眠、装修、面试准备），
编辑环节对那个领域一无所知，唯一能保证它不胡说八道的，就是「只整理对话里
出现过的内容」这一条。
"""

from __future__ import annotations

from camel.agents import ChatAgent

from .personas import Persona
from .topics import Topic

EDITOR_SYSTEM = """你是《{title}》的文字编辑。

你唯一的工作是把两位专家的对话整理成读者可直接照做的条文。

硬性要求：
1. 只整理对话里已经出现过的内容。严禁添加任何新观点、新数据。
2. 用「怎么做」的语气写，不要写成对话实录。
3. 凡是出现数字的地方，必须保留原数字和单位。
4. 两位专家有分歧的地方，必须如实并列写出双方立场，不要擅自调和。
5. 直接输出 Markdown 正文，不要写「好的」「以下是」这类开场白。
6. 如果对话里某一点说得不具体、无法执行，就不要写进这份文件。"""

SECTION_TEMPLATE = """议题：{title}

议题背景：
{brief}

本轮要收敛的结论：
{goal}

以下是两位专家围绕这个议题的完整对话：

{dialogue}

---

请把它整理成一节。

输出格式（严格遵守）：

## {title}

### 要点
（3–5 条，每条一句话，直接给可执行的做法，带具体标准）

### 具体怎么做
（分条展开，每条都要具体到读者今天就能照做）

### 两人分歧
（如实写出两位专家没有谈拢的地方，各自立场和理由。
 如果确实完全一致，写「本议题双方无实质分歧」。）

### 注意
（这一节里最容易做错、或者不适合哪些人的地方。没有就写「无」。）

不要使用一级标题，不要重复输出议题名称之外的标题。"""

CLOSING_TEMPLATE = """以下是两位专家在收尾讨论中的完整对话，以及他们此前各议题的结论摘要：

{dialogue}

---

请整理成本文件的附录，输出格式（严格遵守）：

## 附录 A · 分歧备忘
（把整个过程里两人始终没有谈拢的点列出来。每条写：分歧点 / 方案方立场 / 挑战者立场 / 读者该怎么选。
 这是这份文件最有价值的部分——如实呈现，不要和稀泥。）

## 附录 B · 红线信号
（出现哪些信号时必须停止当前的做法、转而去求助专业人士（按这个目的所属的领域来定该找谁），
 而不是继续自己调整。只写对话里提到的。）

## 附录 C · 行动自检清单
（一份可以定期勾选的清单，用 Markdown 复选框 `- [ ]` 格式，控制在 8 条以内。
 每条必须是能明确判断「做到 / 没做到」的，不要写「保持好心情」这种无法判定的条目。）"""


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
不要写「两人分歧」这类只有完整版才需要的内容。"""


def editor_system(title: str) -> str:
    return EDITOR_SYSTEM.format(title=title)


def header(title: str, proposer: Persona, challenger: Persona) -> str:
    """成果文件开头的门面。

    由代码生成、不走模型：它是**说明这份文件是怎么来的**，不该由被说明的对象
    自己来写（模型写这段时几乎一定会开始加内容）。
    """
    return f"""# {title}

> 本文件由两个立场相反的智能体对谈产出：
> **{proposer.name}**（{proposer.title}，负责「{proposer.stance}」）
> ×
> **{challenger.name}**（{challenger.title}，负责「{challenger.stance}」）。
>
> 所有结论都来自两人的分歧与妥协。凡是他们没谈拢的地方，
> 附录 A 里原样保留，没有替你抹平。

**怎么用**：先读第一节定基准，再挑对你有用的部分执行。
不要试图一次全部做到。"""


def quick_header(title: str, full_name: str) -> str:
    return f"""# {title} · 速查版

> 这是从完整版 `{full_name}` 压缩出来的一页速查：只留最常用的部分，没有新增任何说法。
> 分节展开的做法、两人始终没谈拢的地方（附录 A）、行动自检清单（附录 C）
> 都在完整版里，需要时再翻。"""


def footer() -> str:
    """免责声明。

    泛化之后这段话**比原来更要紧**：原来只知道是「健身饮食」，写一句「不构成
    医疗建议」就够了；现在什么目的都可能进来，所以只能写通用的一条——凡是
    需要专业判断的事，去找那个专业的人。
    """
    return """
---

*本文件由 camel-duo-forge 生成：两位立场对立的 AI 专家围绕一个给定目的对谈，
再由编辑环节整理成文。内容仅供参考，不构成专业意见——涉及医疗、法律、财务
这类专业问题时，请找相应的人，不要只看这里。*"""


def _generate(system: str, prompt: str, backend) -> str:
    agent = ChatAgent(system_message=system, model=backend)
    response = agent.step(prompt)
    msgs = getattr(response, "msgs", None) or []
    return str(msgs[0].content).strip() if msgs else ""


def digest_section(topic: Topic, dialogue: str, *, backend, title: str) -> str:
    prompt = SECTION_TEMPLATE.format(
        title=topic.section_title,
        brief=topic.brief,
        goal=topic.goal,
        dialogue=dialogue,
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


def assemble(sections: list[str], closing: str, *, header: str) -> str:
    body = "\n\n---\n\n".join(s.strip() for s in sections if s.strip())
    parts = [header.strip(), body]
    if closing.strip():
        parts.append(closing.strip())
    parts.append(footer().strip())
    return "\n\n".join(parts) + "\n"


def assemble_quick(body: str, *, header: str) -> str:
    """速查版的拼装：页眉 + 正文 + 免责声明。

    免责声明必须跟着走：速查版是会被人**单独打开、单独转发**的那一份
    ——「一页纸」正是它容易被转出去的原因，所以那句话不能只留在完整版里。
    """
    return "\n\n".join([header.strip(), body.strip(), footer().strip()]) + "\n"
