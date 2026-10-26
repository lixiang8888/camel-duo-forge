"""读完成果之后的追加讨论。

`run` 跑完以后，读者往往还有一两个具体问题：某一节的做法在某个场景里到底怎么
执行、两条建议冲突时听谁的、能不能给一个更省时间的版本。这个模块让同一批专家
围绕这个追问再谈一轮，把整理出来的一节**追加进同一份成果手册**，同时更新
一页速查和对话实录；可以反复追加。

    handbook.md ──追问──> 同一批专家对谈 ──整理──> 新的一节
                                                      │
                              handbook.md（新一节插在免责声明之前）
                              transcript.md（追加对话）
                              quick.md（按更新后的全文重压）

## 三个设计上的取舍

**一、追加，不是另起一份。** 用户说的是「在结果上再增加一个议题」，所以手册、
速查、实录都在原文件上更新；原内容一字不删。写入用临时文件 + `os.replace`，
中途失败不会留下半份手册。

**二、追问原文进 brief，模型只负责标题/目标/开场。** 追问要谈的正是读者问的
那一点，让模型再概括一次只会把它写跑偏。完整问题还会作为一次性上下文发给
所有专家（见 `society.debate_topic(context=...)`），因为 brief 有 400 字上限。

**三、上下文要克制。** 它进入每位专家的记忆后会随之后每一轮重发；五个人、
三轮下来，放一整份几万字的手册会把成本和上下文都撑爆。所以对谈用的是
`digest.convergence_excerpt`（各节要点 + 分歧 + 附录 A + 已有统一结论）；
只有负责写开场发言的那一次作者调用能看到更长的全文。

## 和 pipeline 的分工

这里不碰「一整轮 run」的落盘规则，也不认识 CLI / 启动器；`run` 负责从零生成
三份文件，这里只负责在已有成果上追加一节。两边共用 society / digest / authoring，
没有第二套对谈或整理逻辑。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from .blueprint import Blueprint
from .pipeline import MAX_ROUNDS, MIN_ROUNDS, RunStopped
from .purpose import MAX_TITLE
from .topics import (
    MAX_FOLLOWUP_QUESTION,
    MIN_FOLLOWUP_QUESTION,
    Topic,
)

if TYPE_CHECKING:  # 只为类型标注，运行时不导入（society / digest 会拖进 camel）
    from .society import ConvergenceResult, TopicTranscript, Turn

__all__ = [
    "FollowupError",
    "FollowupHooks",
    "FollowupResult",
    "append_to_handbook",
    "check_question",
    "check_title",
    "next_index",
    "run_followup",
    "sibling_path",
]

#: 作者调用能看到的成果长度上限。这一次调用只发生一次，可以比每次发言都重发的
#: 上下文宽松得多；超过就截断，免得把模型的上下文撑爆。
MAX_AUTHOR_CONTEXT = 12000

#: 对谈上下文（每位专家都会收到、并随之后每一轮重发）的长度上限。
MAX_DEBATE_CONTEXT = 6000

#: 免责声明在成果里的开头。追加的一节必须插在它前面，否则会落在声明之后，
#: 看起来像「声明只管正文、不管追记」。
_FOOTER_MARK = "---\n\n*本文件由 camel-duo-forge 生成"

#: 每次追加的小节标题。`next_index` 靠它数已经追加过几次。
_SECTION_RE = re.compile(r"(?m)^## 追加讨论 (\d+)：")


class FollowupError(ValueError):
    """追问本身不合法，或者没有可追加的成果。消息直接给人看。"""


# ---------------------------------------------------------------------------
# 输入校验与文件命名
# ---------------------------------------------------------------------------


def check_question(raw: object) -> str:
    """校验读者的追问，返回去掉首尾空白的文本。"""
    if not isinstance(raw, str):
        raise FollowupError("追问必须是文本。")
    text = raw.strip()
    if not text:
        raise FollowupError("没有写追问。先写清楚你想对成果问什么。")
    if len(text) < MIN_FOLLOWUP_QUESTION:
        raise FollowupError(
            f"追问太短了（{len(text)} 字）：至少 {MIN_FOLLOWUP_QUESTION} 字，"
            "要说清你想追问成果里的哪一点。"
        )
    if len(text) > MAX_FOLLOWUP_QUESTION:
        raise FollowupError(
            f"追问太长了：上限 {MAX_FOLLOWUP_QUESTION} 字，收到 {len(text)} 字。"
            "请先精简成你最想问的那一个点。"
        )
    return text


def check_title(raw: object) -> str:
    """校验用户自己给的小节标题（可以不给）。"""
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str):
        raise FollowupError("小节标题必须是文本。")
    text = raw.strip()
    if "\n" in raw or "\r" in raw:
        raise FollowupError("小节标题必须是单行。")
    if len(text) > MAX_TITLE:
        raise FollowupError(
            f"小节标题太长了：上限 {MAX_TITLE} 字，收到 {len(text)} 字。"
        )
    return text


def next_index(handbook: str) -> int:
    """下一次追加该编几号。

    从手册里已有的「## 追加讨论 N：」读最大值，而不是数个数：中间某一节
    被手动删掉时，编号也不会撞车。
    """
    found = [int(match.group(1)) for match in _SECTION_RE.finditer(handbook or "")]
    return max(found, default=0) + 1


def sibling_path(handbook_path: Path | str, prefix: str) -> Path:
    """按 `handbook-<时间戳>-<slug>.md` 的命名，推出同批产物的路径。

    找不到这个命名规则时退化成「前缀 + 原文件名」，至少不会写到别的目录去。
    """
    path = Path(handbook_path)
    if path.name.startswith("handbook-"):
        return path.with_name(prefix + path.name[len("handbook"):])
    return path.with_name(f"{prefix}-{path.name}")


def _read_text(path: Optional[Path]) -> str:
    if path is None or not Path(path).exists():
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 拼装：把新的一节插进现有手册
# ---------------------------------------------------------------------------


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _ensure_heading(section: str, title: str) -> str:
    """保证这一节以期望的 `## 追加讨论 N：…` 开头。

    整理提示词要求模型输出这个标题，但模型偶尔会自己换一个；mock 模式下更是
    只回一段占位文本。标题是手册结构的一部分，也靠它数下一次的编号，所以
    这里以代码为准：有标题就换成期望的，没有就补上。
    """
    expected = f"## {title}"
    text = (section or "").strip()
    if not text:
        return expected
    lines = text.splitlines()
    if lines[0].lstrip().startswith("#"):
        if lines[0].strip() == expected:
            return text
        rest = "\n".join(lines[1:]).strip()
        return expected + ("\n\n" + rest if rest else "")
    return expected + "\n\n" + text


def _insert_question(section: str, *, index: int, question: str, mock: bool) -> str:
    """把读者提问插在小节标题下面，而不是整段堆在标题前面。"""
    marker = f"## 追加讨论 {index}："
    start = section.find(marker)
    note = [f"\n\n> 读者提问（第 {index} 次追加）：", ">", _quote(question)]
    if mock:
        note += ["", "> ⚠️ 本节由模拟模式生成，是占位内容，没有真实结论。"]
    block = "\n".join(note)
    if start < 0:
        # 整理环节没按模板输出标题：退化成「提问在前、正文在后」，至少内容不丢。
        return block.strip() + "\n\n" + section.strip()
    line_end = section.find("\n", start)
    if line_end < 0:
        return section + block
    return section[:line_end] + block + section[line_end:]


def append_to_handbook(handbook: str, block: str) -> str:
    """把一段新内容插到免责声明之前；没有声明就接在末尾。

    用 `rfind` 而不是 `find`：手册可能已经被追加过几次，真正的免责声明永远
    在最后；正文里（比如读者提问）即便碰巧出现同样的标记，也不会被误认。
    """
    text = (handbook or "").rstrip()
    block = block.strip()
    if not block:
        return text + "\n" if text else ""
    at = text.rfind(_FOOTER_MARK)
    if at < 0:
        return f"{text}\n\n---\n\n{block}\n" if text else block + "\n"
    head = text[:at].rstrip()
    tail = text[at:].strip()
    return f"{head}\n\n---\n\n{block}\n\n{tail}\n"


def _write_atomic(path: Path, text: str) -> None:
    """先写临时文件、再 `os.replace`：写一半失败时，原文件一个字节都不会变。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 一次性上下文
# ---------------------------------------------------------------------------


def _author_excerpt(handbook: str) -> str:
    text = (handbook or "").strip()
    if len(text) <= MAX_AUTHOR_CONTEXT:
        return text
    return text[:MAX_AUTHOR_CONTEXT].rstrip() + "\n\n（成果全文过长，已截断。）"


def _debate_context(handbook: str, question: str) -> str:
    from . import digest

    excerpt = digest.convergence_excerpt(handbook or "").strip()
    if not excerpt:
        excerpt = (handbook or "").strip()
    if len(excerpt) > MAX_DEBATE_CONTEXT:
        excerpt = excerpt[:MAX_DEBATE_CONTEXT].rstrip() + "\n\n（摘要已截断。）"
    return (
        "【读者读完成果后的追加提问】\n"
        f"{question.strip()}\n\n"
        "【现有成果的要点与分歧摘要】\n"
        f"{excerpt}"
    )


# ---------------------------------------------------------------------------
# 结果与回调
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FollowupResult:
    index: int
    question: str
    topic: Topic
    #: 整理出来的一节（含插进去的读者提问和可选的统一结论）。
    section: str
    transcript: "TopicTranscript"
    #: 统一结论那一块；没跑统一轮时是空串。
    convergence: str
    #: 更新后的三份正文。路径和调用方给的路径相同（原地更新）。
    handbook: str
    handbook_path: Path
    quick: str
    quick_path: Optional[Path]
    transcript_text: str
    transcript_path: Path


@dataclass
class FollowupHooks:
    """追加讨论各节点的可选回调。和 `PipelineHooks` 同一个思路：具名事件，不做大分发。"""

    # (第几次追加, 轮数, 追问原文)
    on_followup_start: Optional[Callable[[int, int, str], None]] = None
    # (这次追加的议题)
    on_topic_start: Optional[Callable[[Topic], None]] = None
    # (本轮发言)
    on_turn: Optional[Callable[["Turn"], None]] = None
    # (这次追加的完整记录)
    on_topic_done: Optional[Callable[["TopicTranscript"], None]] = None
    # (这次追加的议题)
    on_section_start: Optional[Callable[[Topic], None]] = None
    # (整理出来的一节)
    on_section_done: Optional[Callable[[str], None]] = None
    # () —— 开始统一这一节里仍未谈拢的地方
    on_unify_start: Optional[Callable[[], None]] = None
    # (统一轮结果)
    on_unify_done: Optional[Callable[["ConvergenceResult"], None]] = None
    # () —— 按更新后的全文重压一页速查
    on_quick_start: Optional[Callable[[], None]] = None
    # (新速查；没产出时是旧速查)
    on_quick_done: Optional[Callable[[str], None]] = None
    # (最终结果)
    on_followup_done: Optional[Callable[["FollowupResult"], None]] = None


def _check_stop(should_stop: Optional[Callable[[], bool]]) -> None:
    if should_stop is not None and should_stop():
        raise RunStopped()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_followup(
    *,
    blueprint: Blueprint,
    handbook: str,
    handbook_path: Path | str,
    question: str,
    title: str = "",
    index: Optional[int] = None,
    rounds: int = 3,
    mock: bool = False,
    converge: bool = True,
    backend,
    author_backend=None,
    transcript: str = "",
    transcript_path: Path | str | None = None,
    quick_path: Path | str | None = None,
    hooks: Optional[FollowupHooks] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> FollowupResult:
    """跑一次追加讨论，并把结果原地写回成果手册 / 实录 / 速查。

    `handbook`、`transcript` 是内存里的正文，`*_path` 是它们对应的文件；
    调用方（启动器）直接传上一轮 `RunResult` 里的字段即可。所有写入都发生在
    最后一步：中途抛异常或被喊停，三份文件都不会被改动。

    `rounds` 是上限；`converge` 和 `run` 的含义一致——这一节整理完仍标着分歧时，
    再补一轮固定一轮的强制统一。mock 下收敛恒关（和 pipeline 一样，mock 只
    证明链路通）。
    """
    # 懒 import：这三个模块会拖进 camel，而 `followup.next_index` 这类小工具
    # （启动器校验、命令行预检查）不该为它付钱。
    from . import authoring, digest, society

    question = check_question(question)
    title = check_title(title)
    if not (handbook or "").strip():
        raise FollowupError("还没有可追加的成果：手册是空的。")
    # 追问可以比 brief 的 400 字上限长：完整问题会作为一次性上下文发出去，
    # 进 system message 的只是截断版（见 authoring._parse_followup_topic）。
    rounds = max(MIN_ROUNDS, min(MAX_ROUNDS, int(rounds)))
    h = hooks or FollowupHooks()
    handbook_path = Path(handbook_path)
    if index is None:
        index = next_index(handbook)

    transcript_path = (
        Path(transcript_path)
        if transcript_path is not None
        else sibling_path(handbook_path, "transcript")
    )
    quick_path = Path(quick_path) if quick_path is not None else None

    if h.on_followup_start:
        h.on_followup_start(index, rounds, question)

    _check_stop(should_stop)

    # ---- 1. 追问 → 一个议题（额外一次作者调用） ----
    author = author_backend or backend
    topic = authoring.forge_followup_topic(
        question,
        backend=author,
        title=blueprint.title,
        purpose=blueprint.purpose.text,
        excerpt=_author_excerpt(handbook),
        index=index,
        section_title_hint=title,
    )
    if h.on_topic_start:
        h.on_topic_start(topic)

    _check_stop(should_stop)

    # ---- 2. 对谈 ----
    followup_transcript = society.debate_topic(
        topic,
        personas=blueprint.personas,
        backend=backend,
        rounds=rounds,
        on_turn=h.on_turn,
        should_stop=should_stop,
        early_stop=converge and not mock,
        context=_debate_context(handbook, question),
    )
    if h.on_topic_done:
        h.on_topic_done(followup_transcript)

    _check_stop(should_stop)

    # ---- 3. 整理成一节 ----
    if h.on_section_start:
        h.on_section_start(topic)
    produced = digest.digest_section(
        topic,
        followup_transcript.as_dialogue(),
        backend=backend,
        title=blueprint.title,
        people=blueprint.people,
    )
    has_unresolved = digest.has_disagreement(produced)
    produced = digest.strip_disagreement_markers(produced)
    if not produced.strip():
        raise FollowupError(
            "整理环节没有产出任何内容，本次追加已放弃；成果文件没有被改动。"
        )
    produced = _ensure_heading(produced, topic.section_title)
    if h.on_section_done:
        h.on_section_done(produced)

    _check_stop(should_stop)

    # ---- 4. 仍标着分歧就补一轮统一（固定一轮，和 run 的附录 D 同义） ----
    convergence_block = ""
    if converge and not mock and has_unresolved:
        if h.on_unify_start:
            h.on_unify_start()
        conv = society.converge_history(
            personas=blueprint.personas,
            backend=backend,
            context=produced,
            on_turn=h.on_turn,
        )
        if h.on_unify_done:
            h.on_unify_done(conv)
        convergence_block = digest.assemble_followup_convergence(
            conv.unified, conv.reservations
        )
        _check_stop(should_stop)

    section = produced.strip()
    if convergence_block.strip():
        section += "\n\n" + convergence_block.strip()
    section = _insert_question(section, index=index, question=question, mock=mock)

    # ---- 5. 插进手册（免责声明之前） ----
    updated = append_to_handbook(handbook, section)

    # ---- 6. 按更新后的全文重压速查 ----
    if h.on_quick_start:
        h.on_quick_start()
    old_quick = _read_text(quick_path)
    quick_body = digest.digest_quick(
        updated, backend=backend, title=blueprint.title
    )
    if quick_body.strip():
        if quick_path is None:
            quick_path = sibling_path(handbook_path, "quick")
        quick_head = digest.quick_header(
            blueprint.title,
            handbook_path.name,
            unified="## 附录 D · 最终统一意见" in updated,
        )
        if mock:
            quick_head = (
                "> ⚠️ **这是模拟模式生成的占位速查，不含任何真实内容。**\n\n"
                + quick_head
            )
        quick = digest.assemble_quick(quick_body, header=quick_head)
    else:
        # 模型没吐出速查时保留上一版：它至少和原内容一致，比覆盖成空文件干净。
        quick = old_quick
        if not old_quick.strip():
            quick_path = None
    if h.on_quick_done:
        h.on_quick_done(quick)

    # ---- 7. 追加对话实录 ----
    piece = followup_transcript.to_markdown().strip()
    transcript_text = transcript or ""
    if transcript_text.strip() and piece:
        transcript_text = transcript_text.rstrip() + "\n\n---\n\n" + piece + "\n"
    elif piece:
        transcript_text = piece + "\n"

    _check_stop(should_stop)

    # ---- 8. 落盘（最后一步；失败或喊停就什么都不写） ----
    _write_atomic(handbook_path, updated)
    _write_atomic(transcript_path, transcript_text)
    if quick_path is not None and quick.strip():
        _write_atomic(quick_path, quick)

    result = FollowupResult(
        index=index,
        question=question,
        topic=topic,
        section=section,
        transcript=followup_transcript,
        convergence=convergence_block,
        handbook=updated,
        handbook_path=handbook_path,
        quick=quick,
        quick_path=quick_path,
        transcript_text=transcript_text,
        transcript_path=transcript_path,
    )
    if h.on_followup_done:
        h.on_followup_done(result)
    return result
