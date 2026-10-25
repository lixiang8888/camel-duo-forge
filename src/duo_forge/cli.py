"""命令行入口。

三个子命令，对应流程的两段：

    forge   目的 → 2–5 位专家的人格 + 一组议题 → 蓝图（落盘，可读可改）
    run     蓝图 → 对谈 → 整理 → 成果文件
    show    把一份蓝图摊开给人看（不调用模型）

`run` 也接受 `--purpose`：那就先 forge 一遍再跑。但**跑第二次时建议用
`--blueprint`** ——forge 每次的结果都不一样，同一份目的连着跑两次会得到
两份不同的人格和议题，没法比较。

`authoring` 是**函数内 import** 的，不在模块顶层：它要拖进 camel，而
`duo-forge show` 只读一个 JSON 文件，不该为 camel 的导入付钱。
同理，`DEFAULT_TOPIC_COUNT` 放在不拖 camel 的 `topics.py` 里。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from . import blueprint as blueprint_mod
from .blueprint import Blueprint, BlueprintError
from .config import BLUEPRINT_DIR, MissingApiKey, build_author_backend, build_backend
from .personas import (
    MAX_PEOPLE,
    MIN_PEOPLE,
    PersonaPlan,
    PersonaSpecError,
    parse_persona_plan,
    parse_team_hint,
)
from .purpose import InvalidPurpose, Purpose, make_purpose, read_purpose_file
from .topics import DEFAULT_TOPIC_COUNT


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def _add_purpose_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("目的（--purpose 和 --purpose-file 二选一）")
    group.add_argument(
        "--purpose", default="",
        help='一段话说明你想要什么，例如 "给刚上班的程序员写一份颈椎保养手册"',
    )
    group.add_argument(
        "--purpose-file", type=Path, default=None,
        help="从文本文件读目的；文件第一行写成 `# 标题` 的话，那一行当标题",
    )
    group.add_argument("--title", default="", help="成果物标题（留空由模型起）")


def _purpose_from_args(args: argparse.Namespace) -> Purpose:
    if args.purpose and args.purpose_file:
        raise InvalidPurpose("--purpose 和 --purpose-file 只能给一个。")
    if args.purpose_file:
        from_file = read_purpose_file(args.purpose_file)
        # 命令行上显式给的 --title 覆盖文件里的标题
        return make_purpose(from_file.text, args.title or from_file.title)
    return make_purpose(args.purpose, args.title)


def _add_persona_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("团队与自定义人格（可选）")
    group.add_argument(
        "--people", type=int, default=None,
        help=f"对话人数 {MIN_PEOPLE}–{MAX_PEOPLE}（默认 2）；不给时按人格数组长度推断",
    )
    group.add_argument(
        "--team-hint", default="",
        help='团队级分工要求，例如 "要有一个一线医生、一个患者、一个管理者"',
    )
    group.add_argument(
        "--personas-file", type=Path, default=None,
        help="JSON 文件：逐位自定义 2–5 位专家（mode=auto/hint/manual），也可写 team",
    )


def _load_persona_plan(
    path: Path | None, *, people: int | None = None, team_hint: str = ""
) -> PersonaPlan:
    """读人格文件并校验。所有失败都包成 PersonaSpecError，消息能直接给人看。

    `team_hint` 是命令行显式给的团队要求：它覆盖文件里的 `team`。
    """
    raw: object = None
    if path is not None:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise PersonaSpecError(f"读不出人格文件 {path}：{exc}") from None
        except json.JSONDecodeError as exc:
            raise PersonaSpecError(
                f"人格文件不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）"
            ) from None
    plan = parse_persona_plan(raw, people=people)
    if team_hint.strip():
        plan = PersonaPlan(
            specs=plan.specs,
            team_hint=parse_team_hint(team_hint),
            people=plan.people,
        )
    return plan


def _fail(label: str, detail: object) -> int:
    """统一把错误打到 stderr，并返回命令行的失败退出码。"""
    print(f"\n[{label}] {detail}\n", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# forge
# ---------------------------------------------------------------------------


def _cmd_forge(args: argparse.Namespace) -> int:
    from . import authoring  # 惰性 import：会拖进 camel（见模块开头）

    try:
        purpose = _purpose_from_args(args)
    except InvalidPurpose as exc:
        return _fail("目的有问题", exc)

    # 人格文件在任何付费调用之前解析完：写错一个字不该先花掉一次模型调用。
    try:
        plan = _load_persona_plan(
            args.personas_file, people=args.people, team_hint=args.team_hint
        )
    except PersonaSpecError as exc:
        return _fail("人格设定有问题", exc)

    print(f"目的：{purpose.summary()}")
    print(f"对话人数：{plan.people} 位")
    if plan.all_manual:
        print(f"要生成：约 {args.topics} 个议题（{plan.people} 位专家都由你提供，1 次模型调用）\n")
    elif plan.people == 2:
        print(f"要生成：专家的人格 + 约 {args.topics} 个议题（2 次模型调用）\n")
    else:
        print(
            f"要生成：团队分工骨架 + {plan.people} 位专家的人格 + 约 {args.topics} 个议题"
            "（3 次模型调用）\n"
        )

    if args.mock:
        if plan.all_manual:
            print(
                "!! 模拟模式：议题是占位内容，没有参考价值；"
                "专家是你手写的，不受影响。\n"
            )
        else:
            print(
                "!! 模拟模式：模型生成的人格和议题都是占位内容，没有参考价值。\n"
            )

    try:
        author = build_author_backend(mock=args.mock)
        print(
            "· 专家由你提供，跳过人格生成……"
            if plan.all_manual
            else "· 正在设计专家团队……"
        )
        made = authoring.forge(
            purpose, backend=author, topic_count=args.topics, plan=plan
        )
    except MissingApiKey as exc:
        return _fail("配置缺失", exc)
    except authoring.AuthoringError as exc:
        return _fail("生成失败", exc)

    made = dataclasses.replace(made, origin="mock" if args.mock else "model")
    return _save_and_report(made, args.out)


def _save_and_report(made: Blueprint, out_dir: Path) -> int:
    json_path, md_path = made.save_bundle(out_dir)

    print(f"\n标题：{made.title}（slug: {made.slug}）")
    heading = "两位专家" if made.people == 2 else f"{made.people} 位专家"
    print(f"{heading}：")
    for index, persona in enumerate(made.personas, 1):
        position = "主提出方" if index == 1 else "回应者"
        role = persona.display_role
        extra = "" if role == position else f" · {role}"
        print(
            f"  第 {index} 位 · {position}{extra}："
            f"{persona.name}（{persona.title}）"
        )
    print(f"\n议题（{len(made.topics)} 个正文 + 1 个自动追加的收尾）：")
    for index, topic in enumerate(made.topics, 1):
        print(f"  {index}. [{topic.key}] {topic.section_title}")

    print(f"\n蓝图（给机器，也可以手改）：{json_path}")
    print(f"蓝图（给人看，含全部专家完整的提示词）：{md_path}")
    print(f"\n下一步：uv run duo-forge run --blueprint {json_path}")
    return 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _topic_done_line(transcript) -> str:
    """某个议题谈完时那两行（第二行只在提前收手时出现）。"""
    line = f"    对话完成，共 {len(transcript.turns)} 条发言"
    if transcript.stopped_early:
        who = "、".join(transcript.conceding) or "所有其他参与者"
        line += (
            f"\n    ↑ {who} 已无新的反对意见，"
            f"第 {transcript.rounds_run} 轮后提前收手"
        )
    return line


def _cmd_run(args: argparse.Namespace) -> int:
    from . import authoring  # 惰性 import：会拖进 camel（见模块开头）

    if args.blueprint and (args.purpose or args.purpose_file):
        return _fail(
            "参数冲突",
            "--blueprint 和 --purpose 只能给一个：蓝图里已经存着目的了。",
        )

    if args.blueprint and args.personas_file:
        return _fail(
            "参数冲突",
            "用 --blueprint 时人格已经存在，不能再给 --personas-file。"
            "想改人格就直接改蓝图 JSON。",
        )
    if args.blueprint and args.people is not None:
        return _fail(
            "参数冲突",
            "用 --blueprint 时人数已经存在，不能再给 --people。"
            "想改人数就改蓝图里的 personas 数组，或者重新 forge。",
        )
    if args.blueprint and args.team_hint.strip():
        return _fail(
            "参数冲突",
            "用 --blueprint 时人格已经生成，--team-hint 没有意义。",
        )

    try:
        if args.blueprint:
            bp = blueprint_mod.load(args.blueprint)
            print(f"用蓝图：{args.blueprint}")
            print(f"目的：{bp.purpose.summary()}")
        else:
            purpose = _purpose_from_args(args)
            try:
                plan = _load_persona_plan(
                    args.personas_file,
                    people=args.people,
                    team_hint=args.team_hint,
                )
            except PersonaSpecError as exc:
                return _fail("人格设定有问题", exc)
            print(f"目的：{purpose.summary()}")
            print(
                f"· {plan.people} 位专家由你提供，正在设计议题……"
                if plan.all_manual
                else f"· 正在设计 {plan.people} 位专家和议题……"
            )
            bp = authoring.forge(
                purpose,
                backend=build_author_backend(mock=args.mock),
                topic_count=args.topics,
                plan=plan,
            )
            bp = dataclasses.replace(bp, origin="mock" if args.mock else "model")
            # 顺手把蓝图存下来：不然这一轮的人格和议题用完就没了，
            # 想复现或想改一个议题重跑，都得再花两次调用。
            saved = bp.save(args.out or BLUEPRINT_DIR)
            print(f"· 蓝图已存：{saved}")
    except MissingApiKey as exc:
        return _fail("配置缺失", exc)
    except (InvalidPurpose, BlueprintError, authoring.AuthoringError) as exc:
        return _fail(type(exc).__name__, exc)

    try:
        topics = bp.select(args.only)
    except BlueprintError as exc:
        return _fail("议题选择有问题", exc)

    try:
        backend = build_backend(mock=args.mock)
    except MissingApiKey as exc:
        return _fail("配置缺失", exc)

    if args.mock:
        print("\n!! 模拟模式：不会调用真实模型，产出的是占位内容，没有参考价值。\n")
    else:
        # 真实模式很慢，先说清楚要等多久，免得跑到一半以为卡死了。
        # 每个议题 = 每轮 N 次发言 + 1 次整理，开场白不花调用；末尾再花 1 次压速查。
        # 这是**上限**：所有回应者都没有新的反对意见时该议题会提前收手。
        calls = len(topics) * (args.rounds * bp.people + 1) + 1
        unify_note = ""
        if args.converge:
            # 全部议题结束后如果成果里仍标着分歧，会再补一轮统一（人数次调用）。
            calls += bp.people
            unify_note = (
                f"\n全部议题结束后，如果成果里仍有未统一的分歧，"
                f"会再花最多 {bp.people} 次调用补一轮强制统一。"
            )
        print(
            f"真实模式：本次最多约 {calls} 次模型调用，每次通常十几秒，"
            f"最多约 {max(1, round(calls * 16 / 60))} 分钟。跑的过程中会逐条打印进展。"
            f"{unify_note}\n"
        )

    def run_start(total: int, rounds: int) -> None:
        heading = "两位专家" if bp.people == 2 else f"{bp.people} 位专家"
        print(f"{heading}：")
        for index, persona in enumerate(bp.personas, 1):
            position = "主提出方" if index == 1 else "回应者"
            role = persona.display_role
            extra = "" if role == position else f" · {role}"
            print(
                f"  第 {index} 位 · {position}{extra}："
                f"{persona.name}（{persona.title}）"
            )
        if args.mock:
            print(f"议题数：{total}，每个议题 {rounds} 轮\n")
        else:
            tail = (
                "（上限；所有回应者都没有新的反对意见会提前收手；"
                "全部议题结束后若仍有分歧，会再补一轮强制统一）"
                if args.converge
                else "（不做收敛处理，每个议题跑满）"
            )
            print(f"议题数：{total}，每个议题最多 {rounds} 轮{tail}\n")

    def run_done(result) -> None:
        print(f"\n对话实录：{result.transcript_path}")
        print(f"成果文件：{result.handbook_path}")
        # 模型没吐出速查版时不落盘，这里也就不提——不能指着一个不存在的文件
        if result.quick_path:
            print(f"一页速查：{result.quick_path}")

    hooks = _hooks(run_start, run_done)
    from . import pipeline

    try:
        pipeline.run_pipeline(
            blueprint=bp,
            backend=backend,
            rounds=args.rounds,
            topics=topics,
            out_dir=args.out,
            mock=args.mock,
            hooks=hooks,
            converge=args.converge,
        )
    except pipeline.RunStopped:
        print("\n已停止：这一轮作废，一个文件都没写。", file=sys.stderr)
        return 1
    return 0


def _hooks(run_start, run_done):
    from . import pipeline

    def unify_start() -> None:
        print("\n全部议题已结束：成果里仍有未统一的分歧，追加一轮强制统一……")

    def unify_done(result) -> None:
        if result.all_conceded:
            print("    统一轮完成：所有回应者都接受统一结论")
        else:
            who = "、".join(name for name, _ in result.reservations)
            print(f"    统一轮结束：{who} 仍保留意见，已写进附录 D")

    return pipeline.PipelineHooks(
        on_run_start=run_start,
        on_topic_start=lambda index, total, topic: print(
            f"[{index}/{total}] {topic.section_title}"
        ),
        on_turn=lambda topic, turn: print(f"    · {turn.speaker} 发言 {len(turn.content)} 字"),
        on_topic_done=lambda topic, transcript: print(_topic_done_line(transcript)),
        on_section_done=lambda topic, is_final, markdown: print(
            "    已整理出附录" if is_final else "    已整理成正文一节"
        ),
        on_unify_start=unify_start,
        on_unify_done=unify_done,
        on_run_done=run_done,
    )


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _cmd_show(args: argparse.Namespace) -> int:
    try:
        bp = blueprint_mod.load(args.blueprint)
    except BlueprintError as exc:
        return _fail("蓝图有问题", exc)
    print(bp.to_json() if args.json else bp.to_markdown())
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="duo-forge",
        description="给一个目的，生成 2–5 位对谈智能体的人格与议题，再让他们对谈出成果",
    )
    sub = parser.add_subparsers(dest="command")

    forge = sub.add_parser(
        "forge", help="功能一 + 功能二：按目的生成 2–5 位专家的人格和一组议题"
    )
    _add_purpose_args(forge)
    _add_persona_args(forge)
    forge.add_argument(
        "--topics", type=int, default=DEFAULT_TOPIC_COUNT,
        help=f"生成几个正文议题（默认 {DEFAULT_TOPIC_COUNT}，上限 12）",
    )
    forge.add_argument(
        "--out", type=Path, default=BLUEPRINT_DIR, help="蓝图输出目录（默认 blueprints/）"
    )
    forge.add_argument("--mock", action="store_true", help="离线模拟，不调用真实模型")
    forge.set_defaults(func=_cmd_forge)

    run = sub.add_parser("run", help="用蓝图（或现生成一份）跑完整流程")
    run.add_argument(
        "--blueprint", type=Path, default=None,
        help="用之前 forge 出来的蓝图跑；不给就用 --purpose 现生成一份",
    )
    _add_purpose_args(run)
    _add_persona_args(run)
    run.add_argument(
        "--topics", type=int, default=DEFAULT_TOPIC_COUNT,
        help=f"现生成蓝图时用几个议题（默认 {DEFAULT_TOPIC_COUNT}）",
    )
    run.add_argument(
        "--rounds", type=int, default=3, help="每个议题的对谈轮数上限（默认 3）"
    )
    run.add_argument(
        "--no-converge", dest="converge", action="store_false",
        help=(
            "关闭所有收敛处理：每个议题跑满轮数，全部议题结束后也不做最终统一"
        ),
    )
    run.add_argument(
        "--only", default="",
        help="只跑指定议题，逗号分隔（key 见 forge 的输出或 show --blueprint）",
    )
    run.add_argument("--out", type=Path, default=None, help="产物输出目录（默认 outputs/）")
    run.add_argument("--mock", action="store_true", help="离线模拟，不调用真实模型")
    run.set_defaults(func=_cmd_run)

    show = sub.add_parser("show", help="把一份蓝图摊开看（不调用模型）")
    show.add_argument("--blueprint", type=Path, required=True, help="蓝图 JSON 文件")
    show.add_argument("--json", action="store_true", help="打印原始 JSON 而不是可读版")
    show.set_defaults(func=_cmd_show)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
