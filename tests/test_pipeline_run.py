"""端到端跑一遍（离线），以及落盘、钩子、提前收手在编排层的开关。

全程用 ScriptedBackend，不联网、不需要 API key。
"""

from __future__ import annotations

import pytest

from duo_forge import digest, pipeline, society
from duo_forge.backends import ScriptedBackend
from duo_forge.pipeline import PipelineHooks, RunStopped, run_pipeline
from duo_forge.society import ConvergenceResult, Turn
from duo_forge.topics import CLOSING_KEY

from test_society import ConcedingBackend


def _run(blueprint, tmp_path, **kwargs):
    kwargs.setdefault("mock", True)
    return run_pipeline(
        blueprint=blueprint,
        backend=ScriptedBackend(),
        rounds=2,
        out_dir=tmp_path,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 跑通与落盘
# ---------------------------------------------------------------------------


def test_a_full_run_writes_three_files(blueprint, tmp_path):
    result = _run(blueprint, tmp_path)

    assert result.handbook_path.exists()
    assert result.transcript_path.exists()
    assert result.quick_path is not None and result.quick_path.exists()
    assert result.handbook_path.parent == tmp_path


def test_the_transcript_covers_every_topic(blueprint, tmp_path):
    result = _run(blueprint, tmp_path)
    text = result.transcript_path.read_text(encoding="utf-8")

    assert len(result.transcripts) == len(blueprint.all_topics())
    for topic in blueprint.all_topics():
        assert topic.section_title in text


def test_the_handbook_has_one_section_per_body_topic_plus_the_closing(
    blueprint, tmp_path
):
    result = _run(blueprint, tmp_path)

    assert len(result.sections) == len(blueprint.topics)
    assert result.closing.strip()
    assert blueprint.title in result.handbook
    for persona in blueprint.personas:
        assert persona.name in result.handbook


def test_mock_output_is_labelled(blueprint, tmp_path):
    """模拟模式的产出必须一眼能认出来，否则会被当成真成果转出去。"""
    result = _run(blueprint, tmp_path)
    assert "模拟模式生成的占位文件" in result.handbook
    assert "模拟模式生成的占位速查" in result.quick


def test_the_quick_version_points_back_at_the_full_one(blueprint, tmp_path):
    result = _run(blueprint, tmp_path)
    assert result.handbook_path.name in result.quick


def test_the_filename_carries_the_slug(blueprint, tmp_path):
    """同一个目录下躺着好几份不同目的的成果，只靠时间戳分不出哪份是哪个。"""
    result = _run(blueprint, tmp_path)
    assert blueprint.slug in result.handbook_path.name


def test_the_quick_version_is_compressed_from_the_finished_handbook(
    blueprint, tmp_path, monkeypatch
):
    """速查版的核心不变量：它的输入必须是**拼好的完整版**，不是对话。

    这样「压缩」这一步没有任何新信息可加，速查版里不可能出现完整版没有的说法，
    两份文件也不会互相矛盾。这是这个功能唯一真正重要的性质。
    """
    captured: dict[str, str] = {}
    real = digest.digest_quick

    def spy(handbook, *, backend, title):
        captured["handbook"] = handbook
        return real(handbook, backend=backend, title=title)

    monkeypatch.setattr(digest, "digest_quick", spy)
    result = _run(blueprint, tmp_path)

    assert captured["handbook"] == result.handbook
    # 它拿到的是成品：页眉、正文、附录、免责声明都在里面
    # （不用 startswith：模拟模式的页眉顶上还压着一条警告）
    assert f"# {blueprint.title}" in captured["handbook"]
    assert "本文件由两个立场相反的智能体对谈产出" in captured["handbook"]
    assert result.sections[0].strip() in captured["handbook"]
    assert result.closing.strip() in captured["handbook"]
    assert "不构成专业意见" in captured["handbook"]


def test_only_runs_the_selected_topics(blueprint, tmp_path):
    topics = blueprint.select(blueprint.topics[1].key)
    result = _run(blueprint, tmp_path, topics=topics)

    assert [t.topic.key for t in result.transcripts] == [
        blueprint.topics[1].key,
        CLOSING_KEY,
    ]
    assert len(result.sections) == 1


def test_no_file_is_written_when_stopped(blueprint, tmp_path):
    """半成品比没有更危险：它看起来是完整成果，但缺了后面几个议题。"""
    checks = {"count": 0}

    def should_stop() -> bool:
        checks["count"] += 1
        return checks["count"] > 1  # 第一次放行（跑第一个议题），第二次拦下

    with pytest.raises(RunStopped):
        _run(blueprint, tmp_path, should_stop=should_stop)

    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# 钩子
# ---------------------------------------------------------------------------


def test_hooks_fire_in_order(blueprint, tmp_path):
    events: list[str] = []
    box: dict = {}

    def run_done(result) -> None:
        box["result"] = result
        events.append("run-done")

    hooks = PipelineHooks(
        on_run_start=lambda total, rounds: events.append(f"start:{total}:{rounds}"),
        on_topic_start=lambda i, total, topic: events.append(f"topic:{i}/{total}"),
        on_turn=lambda topic, turn: events.append("turn"),
        on_topic_done=lambda topic, t: events.append("topic-done"),
        on_section_start=lambda topic, is_final: events.append("section"),
        on_section_done=lambda topic, is_final, md: events.append("section-done"),
        on_quick_start=lambda: events.append("quick"),
        on_quick_done=lambda quick: events.append("quick-done"),
        on_run_done=run_done,
    )

    _run(blueprint, tmp_path, hooks=hooks)

    total = len(blueprint.all_topics())
    assert events[0] == f"start:{total}:2"
    assert events[-1] == "run-done"
    assert events.count("topic-done") == total
    assert events.count("section-done") == total
    # 每次发言一条 on_turn：1 条开场 + N 轮 × 2 人
    assert events.count("turn") == sum(
        len(t.turns) for t in box["result"].transcripts
    )
    # 速查在最后，在所有章节之后
    assert events.index("quick") > events.index("section-done")


def test_the_closing_section_is_flagged(blueprint, tmp_path):
    flags: list[bool] = []
    hooks = PipelineHooks(on_section_start=lambda topic, is_final: flags.append(is_final))

    _run(blueprint, tmp_path, hooks=hooks)

    assert flags == [False] * len(blueprint.topics) + [True]


# ---------------------------------------------------------------------------
# 提前收手：pipeline 是唯一把「mock」翻译成「关掉收手」的地方
# ---------------------------------------------------------------------------


def test_converge_stops_a_topic_early(blueprint, tmp_path):
    result = run_pipeline(
        blueprint=blueprint,
        backend=ConcedingBackend(),
        rounds=4,
        out_dir=tmp_path,
        mock=False,
        converge=True,
    )
    assert result.transcripts[0].stopped_early is True
    assert result.transcripts[0].rounds_run == 2


def test_converge_off_runs_the_full_rounds(blueprint, tmp_path):
    result = run_pipeline(
        blueprint=blueprint,
        backend=ConcedingBackend(),
        rounds=4,
        out_dir=tmp_path,
        mock=False,
        converge=False,
    )
    assert result.transcripts[0].stopped_early is False
    assert result.transcripts[0].rounds_run == 4


def test_mock_always_runs_the_full_rounds(blueprint, tmp_path):
    """mock 的职责就是证明「循环确实跑满了 N 轮」，所以收手在 mock 下恒关。

    这里特意用一个**会**说「无异议」的后端：如果 mock 没把收手关掉，
    这条就会红。
    """
    result = run_pipeline(
        blueprint=blueprint,
        backend=ConcedingBackend(),
        rounds=2,
        out_dir=tmp_path,
        mock=True,
    )
    assert all(t.stopped_early is False for t in result.transcripts)


def test_a_three_person_blueprint_runs_end_to_end(blueprint3, tmp_path):
    result = _run(blueprint3, tmp_path)

    assert result.handbook_path.exists()
    assert result.transcript_path.exists()
    for persona in blueprint3.personas:
        assert persona.name in result.handbook
    assert "3 个立场与分工不同的智能体" in result.handbook
    assert result.transcripts[0].turns[0].role == "主提出方"


def test_the_editor_is_told_the_team_size(blueprint3, tmp_path, monkeypatch):
    captured: list[int] = []
    real = digest.digest_section

    def spy(topic, dialogue, *, backend, title, people=2):
        captured.append(people)
        return real(topic, dialogue, backend=backend, title=title, people=people)

    monkeypatch.setattr(digest, "digest_section", spy)
    _run(blueprint3, tmp_path)

    assert captured
    assert all(people == 3 for people in captured)


# ---------------------------------------------------------------------------
# 全部议题结束后的强制统一
# ---------------------------------------------------------------------------


def _stub_digest(monkeypatch, *, section_disagrees: bool, closing_disagrees: bool):
    """让整理环节直接吐带标记的固定文本，把测点收窄到 pipeline 的判定。"""

    def fake_section(topic, dialogue, *, backend, title, people=2):
        marker = "【本节分歧：有】" if section_disagrees else "【本节分歧：无】"
        return f"## {topic.section_title}\n正文\n{marker}"

    def fake_closing(dialogue, *, backend, title):
        marker = "【未解决分歧：有】" if closing_disagrees else "【未解决分歧：无】"
        return f"## 附录 A · 分歧备忘\n内容\n{marker}"

    monkeypatch.setattr(digest, "digest_section", fake_section)
    monkeypatch.setattr(digest, "digest_closing", fake_closing)


def _stub_convergence(monkeypatch, *, reservations=()):
    calls: list[str] = []

    def fake(*, personas, backend, context, on_turn=None):
        calls.append(context)
        result = ConvergenceResult(
            turns=[
                Turn(speaker="老张", content="统一轮表态", role="挑战者"),
                Turn(speaker="康复师", content="统一结论：定稿。", role="主提出方"),
            ],
            unified="统一结论：按原方案定稿，成本条款另算。",
            reservations=tuple(reservations),
        )
        if on_turn:
            for turn in result.turns:
                on_turn(turn)
        return result

    monkeypatch.setattr(society, "converge_history", fake)
    return calls


def test_unification_runs_when_the_final_output_still_has_disagreements(
    blueprint, tmp_path, monkeypatch
):
    _stub_digest(monkeypatch, section_disagrees=False, closing_disagrees=True)
    calls = _stub_convergence(monkeypatch, reservations=(("老张", "成本没人算。"),))

    result = run_pipeline(
        blueprint=blueprint,
        backend=ScriptedBackend(),
        rounds=1,
        out_dir=tmp_path,
        mock=False,
        converge=True,
    )

    assert calls, "收尾附录标着「有」，必须触发统一轮"
    assert "## 附录 D · 最终统一意见" in result.handbook
    assert "统一结论：按原方案定稿" in result.handbook
    assert "成本没人算。" in result.handbook
    assert "最终统一意见（全部议题结束后）" in result.transcript
    # 标记是给代码看的，成品里不能留。
    assert "【未解决分歧：有】" not in result.handbook


def test_unification_is_skipped_when_every_section_is_marked_consistent(
    blueprint, tmp_path, monkeypatch
):
    _stub_digest(monkeypatch, section_disagrees=False, closing_disagrees=False)

    def boom(**kwargs):
        raise AssertionError("没有分歧时不该跑统一轮")

    monkeypatch.setattr(society, "converge_history", boom)

    result = run_pipeline(
        blueprint=blueprint,
        backend=ScriptedBackend(),
        rounds=1,
        out_dir=tmp_path,
        mock=False,
        converge=True,
    )

    assert result.convergence == ""
    assert "附录 D" not in result.handbook


def test_unification_respects_the_converge_switch(blueprint, tmp_path, monkeypatch):
    _stub_digest(monkeypatch, section_disagrees=True, closing_disagrees=True)

    def boom(**kwargs):
        raise AssertionError("--no-converge 时不该跑统一轮")

    monkeypatch.setattr(society, "converge_history", boom)

    result = run_pipeline(
        blueprint=blueprint,
        backend=ScriptedBackend(),
        rounds=1,
        out_dir=tmp_path,
        mock=False,
        converge=False,
    )

    assert result.convergence == ""


def test_mock_never_runs_unification(blueprint, tmp_path, monkeypatch):
    """mock 的职责是证明链路跑通，不做真实收敛判断，也不多花调用。"""
    _stub_digest(monkeypatch, section_disagrees=True, closing_disagrees=True)

    def boom(**kwargs):
        raise AssertionError("mock 下不该跑统一轮")

    monkeypatch.setattr(society, "converge_history", boom)

    result = _run(blueprint, tmp_path)

    assert result.convergence == ""
    assert "附录 D" not in result.handbook


def test_the_unification_turns_go_through_the_turn_hook(
    blueprint, tmp_path, monkeypatch
):
    _stub_digest(monkeypatch, section_disagrees=True, closing_disagrees=True)
    _stub_convergence(monkeypatch)
    topics_seen: list[str] = []
    hooks = PipelineHooks(on_turn=lambda topic, turn: topics_seen.append(topic.key))

    run_pipeline(
        blueprint=blueprint,
        backend=ScriptedBackend(),
        rounds=1,
        out_dir=tmp_path,
        mock=False,
        converge=True,
        hooks=hooks,
    )

    assert "convergence" in topics_seen


def test_unification_is_dropped_when_stopped(blueprint, tmp_path, monkeypatch):
    """统一轮跑完才点停止也一样：这一轮作废，一个文件都不写。"""
    _stub_digest(monkeypatch, section_disagrees=True, closing_disagrees=True)
    _stub_convergence(monkeypatch)

    checks = {"count": 0}

    def should_stop() -> bool:
        # 每个议题有 3 个检查点（开始前、对谈每轮、结束后）；放行它们，
        # 拦住统一轮开始前的那一次。
        checks["count"] += 1
        return checks["count"] > len(blueprint.all_topics()) * 3

    with pytest.raises(RunStopped):
        run_pipeline(
            blueprint=blueprint,
            backend=ScriptedBackend(),
            rounds=1,
            out_dir=tmp_path,
            mock=False,
            converge=True,
            should_stop=should_stop,
        )

    assert list(tmp_path.iterdir()) == []
