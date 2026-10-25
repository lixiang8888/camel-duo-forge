"""完整流程的编排：对谈 → 整理 → 出成果 → 再压出一页速查。

搬运自 camel-fitness-nutrition-duo 的 pipeline.py。它本来就把「跑什么」和
「怎么展示」分开了，所以泛化得很轻：标题、专家、议题全部改从蓝图里取，
不再 import 那两个写死的议题表和人格。

这个模块**不认识** `Purpose` 以外的领域概念，也不认识 CLI 和任何前端：
它只按顺序调用 society / digest，并在关键节点上回调 `PipelineHooks` 里挂的函数。

`backend` 由调用方构建、而不是这里自己 build_backend：`MissingApiKey` 的处理
每个前端都不一样（CLI 要退 2 并在 stderr 上印一段提示），放在外面，各自说了算。
测试也方便：直接注入 ScriptedBackend，完全不需要 .env。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from .blueprint import Blueprint
from .config import OUTPUT_DIR
from .topics import Topic, is_closing

if TYPE_CHECKING:  # 只为类型标注，运行时不导入（避免拖进 camel）
    from .society import TopicTranscript, Turn


class RunStopped(RuntimeError):
    """用户中途喊停。

    走到这里就说明这一轮作废——**一个文件都不写**。半成品比没有更危险：
    它看起来是完整成果，但缺了后面几个议题，容易被当成成品拿走。
    """


@dataclass(frozen=True)
class RunResult:
    title: str
    transcript_path: Path
    handbook_path: Path
    handbook: str
    transcripts: tuple["TopicTranscript", ...]
    sections: tuple[str, ...]
    closing: str
    #: 速查版（完整版压成的一页）。模型没吐出东西时 quick 是空串、路径是 None，
    #: 这一轮就只交付完整版——比落一个空文件干净。
    quick_path: Optional[Path]
    quick: str
    #: 对话实录的完整文本（和落盘的那份逐字相同）。launcher 直接取它，
    #: 不必再按 `"\n\n".join(...)` 重新拼一遍。
    transcript: str = ""


@dataclass
class PipelineHooks:
    """流程各节点的可选回调。全是 None 时就是一个安静的 run。

    每个回调都是一个具名事件，而不是 `on_event(kind, payload)` 那种大分发——
    因为 CLI 要按节点逐句 print，写成大分发的话终端那边会退化成一长串
    `if kind == ...`，还丢掉类型。
    """

    # (议题总数, 每个议题轮数)
    on_run_start: Optional[Callable[[int, int], None]] = None
    # (第几个·从 1 开始, 总数, 议题)
    on_topic_start: Optional[Callable[[int, int, Topic], None]] = None
    # (议题, 本轮发言)
    on_turn: Optional[Callable[[Topic, "Turn"], None]] = None
    # (议题, 该议题的完整记录)
    on_topic_done: Optional[Callable[[Topic, "TopicTranscript"], None]] = None
    # (议题, 是不是收尾议题)
    on_section_start: Optional[Callable[[Topic, bool], None]] = None
    # (议题, 是不是收尾议题, 整理出来的 markdown)
    on_section_done: Optional[Callable[[Topic, bool, str], None]] = None
    # () —— 开始把整份文件压成一页速查（真实模式下这又是一次十几秒的等待）
    on_quick_start: Optional[Callable[[], None]] = None
    # (速查版的 markdown；没产出就是空串)
    on_quick_done: Optional[Callable[[str], None]] = None
    # (最终结果)
    on_run_done: Optional[Callable[[RunResult], None]] = None


def run_pipeline(
    *,
    blueprint: Blueprint,
    backend,
    rounds: int = 3,
    topics: Optional[Sequence[Topic]] = None,
    out_dir: Optional[Path] = None,
    mock: bool = False,
    hooks: Optional[PipelineHooks] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    converge: bool = True,
) -> RunResult:
    """跑完整流程。产物全部跑完才落盘，中途抛异常或喊停都不留半成品。

    `should_stop` 的检查点只有两处：议题开始前、某个议题对谈结束后。
    所以喊停最坏要等当前这一轮对谈跑完（society.step() 内部是两次模型调用，
    没法从中间掐断）。

    `converge` 决定「所有回应者都没有新的反对意见就提前收手」要不要开（`rounds` 因此
    是上限而非定额）。**mock 模式下恒关**，两个原因：ScriptedBackend 只吐固定
    模板、根本不会说那句话，开着也只是白费判断；更重要的是 mock 的职责就是
    证明「循环确实跑满了 N 轮」，让它在轮数上变来变去会让那条保证失效。

    所以本模块是**唯一**把「mock」翻译成「关掉收手」的地方：society 只认
    `early_stop` 这个语义参数，它不认识 mock，也不该认识。
    """
    # 懒 import：这个模块会拖进 camel，而 `show --blueprint` 那条路不该为它付钱。
    from . import digest, society

    topics = list(topics) if topics else blueprint.all_topics()
    total = len(topics)
    h = hooks or PipelineHooks()

    if h.on_run_start:
        h.on_run_start(total, rounds)

    transcripts: list["TopicTranscript"] = []
    sections: list[str] = []
    #: 每个议题的对话文本。整理正文和收尾议题都要用，算一次就够。
    dialogues: list[str] = []
    # 只有收尾议题会赋真值；先给个初值，免得议题表不以收尾结尾时
    # 末尾 assemble(sections, closing) 直接 NameError。
    closing = ""

    for index, topic in enumerate(topics, 1):
        if should_stop and should_stop():
            raise RunStopped()

        is_final = is_closing(topic)
        if h.on_topic_start:
            h.on_topic_start(index, total, topic)

        transcript = society.debate_topic(
            topic,
            personas=blueprint.personas,
            backend=backend,
            rounds=rounds,
            # 默认参数绑住当前 topic，不然闭包会捕获循环变量
            on_turn=(lambda t, _topic=topic: h.on_turn(_topic, t)) if h.on_turn else None,
            should_stop=should_stop,
            early_stop=converge and not mock,
        )
        transcripts.append(transcript)
        if h.on_topic_done:
            h.on_topic_done(topic, transcript)

        if should_stop and should_stop():
            raise RunStopped()

        if h.on_section_start:
            h.on_section_start(topic, is_final)

        dialogue = transcript.as_dialogue()
        dialogues.append(dialogue)
        if is_final:
            # 收尾议题要把前面所有议题的结论一起喂进去，否则写不出「分歧备忘」
            context = "\n\n".join(dialogues)
            closing = digest.digest_closing(context, backend=backend, title=blueprint.title)
            produced = closing
        else:
            produced = digest.digest_section(
                topic,
                dialogue,
                backend=backend,
                title=blueprint.title,
                people=len(blueprint.personas),
            )
            sections.append(produced)

        if h.on_section_done:
            h.on_section_done(topic, is_final, produced)

    # ---- 落盘 ----
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(out_dir) if out_dir else OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)

    suffix = ".mock" if mock else ""
    # slug 是 ASCII 的：同一个目录下会躺着好几份不同目的的成果，
    # 只靠时间戳分不出哪份是哪个目的的。
    slug = f"-{blueprint.slug}" if blueprint.slug else ""
    transcript_path = out / f"transcript-{stamp}{slug}{suffix}.md"
    handbook_path = out / f"handbook-{stamp}{slug}{suffix}.md"

    head = digest.header(blueprint.title, blueprint.personas)
    if mock:
        head = (
            "> ⚠️ **这是模拟模式生成的占位文件，不含任何真实内容。**\n\n"
            + head
        )

    handbook = digest.assemble(sections, closing, header=head)

    # 速查版在完整版之后生成，喂进去的就是上面这份拼好的全文——
    # 它因此不可能写出完整版里没有的东西，两份文件也不会互相矛盾。
    if h.on_quick_start:
        h.on_quick_start()
    quick_body = digest.digest_quick(handbook, backend=backend, title=blueprint.title)
    quick = ""
    quick_path: Optional[Path] = None
    if quick_body.strip():
        quick_path = out / f"quick-{stamp}{slug}{suffix}.md"
        quick_head = digest.quick_header(blueprint.title, handbook_path.name)
        if mock:
            quick_head = (
                "> ⚠️ **这是模拟模式生成的占位速查，不含任何真实内容。**\n\n"
                + quick_head
            )
        quick = digest.assemble_quick(quick_body, header=quick_head)
    if h.on_quick_done:
        h.on_quick_done(quick)

    transcript_text = "\n\n".join(t.to_markdown() for t in transcripts)
    transcript_path.write_text(transcript_text, encoding="utf-8")
    handbook_path.write_text(handbook, encoding="utf-8")
    if quick_path:
        quick_path.write_text(quick, encoding="utf-8")

    result = RunResult(
        title=blueprint.title,
        transcript_path=transcript_path,
        handbook_path=handbook_path,
        handbook=handbook,
        transcripts=tuple(transcripts),
        sections=tuple(sections),
        closing=closing,
        quick_path=quick_path,
        quick=quick,
        transcript=transcript_text,
    )
    if h.on_run_done:
        h.on_run_done(result)
    return result
