"""读完成果后的追加讨论：追加、反复、上下文、统一轮和「失败不改文件」。

全程离线：对谈用 ScriptedBackend，追问设计用 ScriptedAuthorBackend。
"""

from __future__ import annotations

import pytest

from duo_forge import digest, followup, society
from duo_forge.authoring import forge_followup_topic
from duo_forge.backends import ScriptedAuthorBackend, ScriptedBackend
from duo_forge.pipeline import RunStopped, run_pipeline
from duo_forge.society import ConvergenceResult, Turn
from duo_forge.topics import MAX_BRIEF

QUESTION = "连续开会、赶版本的时候，这套动作到底怎么打折？"


def _seed(blueprint, tmp_path):
    """先跑一轮 mock run，拿到三份真实落盘的产物。"""
    return run_pipeline(
        blueprint=blueprint,
        backend=ScriptedBackend(),
        rounds=1,
        out_dir=tmp_path,
        mock=True,
    )


def _follow(blueprint, run_result, **kwargs):
    kwargs.setdefault("mock", True)
    kwargs.setdefault("rounds", 1)
    kwargs.setdefault("question", QUESTION)
    return followup.run_followup(
        blueprint=blueprint,
        handbook=kwargs.pop("handbook", run_result.handbook),
        handbook_path=run_result.handbook_path,
        backend=ScriptedBackend(),
        author_backend=ScriptedAuthorBackend(),
        transcript=kwargs.pop("transcript", run_result.transcript),
        transcript_path=run_result.transcript_path,
        quick_path=kwargs.pop("quick_path", run_result.quick_path),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 追加与反复
# ---------------------------------------------------------------------------


def test_a_followup_appends_a_section_and_updates_all_three_files(
    blueprint, tmp_path
):
    run_result = _seed(blueprint, tmp_path)
    old_handbook = run_result.handbook

    result = _follow(blueprint, run_result)

    assert result.index == 1
    assert result.topic.key == "followup-1"
    assert result.topic.section_title.startswith("追加讨论 1：")
    assert "## 追加讨论 1：" in result.section
    assert "读者提问（第 1 次追加）" in result.section
    assert QUESTION in result.section
    # 原有内容一字不删：免责声明之前的部分原样保留，声明本身也还在
    old_body = old_handbook.split("---\n\n*本文件由 camel-duo-forge 生成")[0].rstrip()
    assert old_body in result.handbook
    assert "本文件由 camel-duo-forge 生成" in result.handbook
    # 追加的一节在免责声明之前
    assert result.handbook.index("## 追加讨论 1：") < result.handbook.rindex(
        "本文件由 camel-duo-forge 生成"
    )
    assert result.handbook.rstrip().endswith("不要只看这里。*")

    # 三份文件都原地更新了
    assert result.handbook_path.read_text(encoding="utf-8") == result.handbook
    assert result.transcript_path.read_text(encoding="utf-8") == result.transcript_text
    assert result.quick_path is not None
    assert result.quick_path.read_text(encoding="utf-8") == result.quick
    assert "## 议题：追加讨论 1：" in result.transcript_text
    assert "模拟模式生成的占位速查" in result.quick


def test_a_second_followup_gets_the_next_number(blueprint, tmp_path):
    run_result = _seed(blueprint, tmp_path)
    first = _follow(blueprint, run_result)

    second = _follow(
        blueprint,
        run_result,
        handbook=first.handbook,
        transcript=first.transcript_text,
        quick_path=first.quick_path,
        question="那如果只能在工位上做两分钟呢？",
    )

    assert second.index == 2
    assert "## 追加讨论 1：" in second.handbook
    assert "## 追加讨论 2：" in second.handbook
    assert second.handbook.count("## 追加讨论 ") == 2
    assert "## 议题：追加讨论 1：" in second.transcript_text
    assert "## 议题：追加讨论 2：" in second.transcript_text


def test_next_index_uses_the_largest_number_not_the_count():
    handbook = "## 追加讨论 1：a\n\n## 追加讨论 3：b\n"
    assert followup.next_index(handbook) == 4
    assert followup.next_index("") == 1


def test_the_original_sections_and_personas_survive(blueprint, tmp_path):
    run_result = _seed(blueprint, tmp_path)
    result = _follow(blueprint, run_result)

    for topic in blueprint.topics:
        assert topic.section_title in result.handbook
    for persona in blueprint.personas:
        assert persona.name in result.handbook


# ---------------------------------------------------------------------------
# 不改文件的那些路径
# ---------------------------------------------------------------------------


def test_stopping_before_any_call_leaves_every_file_untouched(blueprint, tmp_path):
    run_result = _seed(blueprint, tmp_path)
    paths = [
        run_result.handbook_path,
        run_result.transcript_path,
        run_result.quick_path,
    ]
    before = {path: path.read_bytes() for path in paths if path is not None}

    with pytest.raises(RunStopped):
        _follow(blueprint, run_result, should_stop=lambda: True)

    assert {path: path.read_bytes() for path in before} == before


def test_a_blank_question_is_rejected_before_any_call(blueprint, tmp_path):
    run_result = _seed(blueprint, tmp_path)
    with pytest.raises(followup.FollowupError):
        _follow(blueprint, run_result, question="   ")


def test_an_overlong_question_is_rejected(blueprint, tmp_path):
    run_result = _seed(blueprint, tmp_path)
    with pytest.raises(followup.FollowupError):
        _follow(blueprint, run_result, question="问" * 2001)


def test_an_empty_handbook_is_rejected(blueprint, tmp_path):
    run_result = _seed(blueprint, tmp_path)
    with pytest.raises(followup.FollowupError):
        _follow(blueprint, run_result, handbook="   ")


def test_an_empty_digest_aborts_without_touching_the_handbook(
    blueprint, tmp_path, monkeypatch
):
    run_result = _seed(blueprint, tmp_path)
    before = run_result.handbook_path.read_bytes()
    monkeypatch.setattr(digest, "digest_section", lambda *a, **k: "")

    with pytest.raises(followup.FollowupError):
        _follow(blueprint, run_result)

    assert run_result.handbook_path.read_bytes() == before


# ---------------------------------------------------------------------------
# 上下文与统一轮
# ---------------------------------------------------------------------------


def test_the_question_and_existing_result_are_seeded_as_one_shot_context(
    blueprint, tmp_path, monkeypatch
):
    run_result = _seed(blueprint, tmp_path)
    captured: dict[str, str] = {}
    real = society.debate_topic

    def spy(topic, **kwargs):
        captured["context"] = kwargs.get("context") or ""
        return real(topic, **kwargs)

    monkeypatch.setattr(society, "debate_topic", spy)
    _follow(blueprint, run_result)

    assert QUESTION in captured["context"]
    assert "现有成果的要点与分歧摘要" in captured["context"]


def test_a_disagreement_in_the_new_section_gets_one_unification_round(
    blueprint, tmp_path, monkeypatch
):
    run_result = _seed(blueprint, tmp_path)
    proposer = blueprint.personas[0].name
    challenger = blueprint.personas[1].name

    monkeypatch.setattr(
        digest,
        "digest_section",
        lambda topic, *a, **k: f"## {topic.section_title}\n\n正文\n【本节分歧：有】",
    )

    def fake_converge(*, personas, backend, context, on_turn=None):
        result = ConvergenceResult(
            turns=[
                Turn(challenger, "我保留：会议里根本站不起来。", "挑战者"),
                Turn(proposer, "统一结论：改成坐着也能做的版本。", "主提出方"),
            ],
            unified="统一结论：改成坐着也能做的版本。",
            reservations=((challenger, "会议里根本站不起来。"),),
        )
        if on_turn:
            for turn in result.turns:
                on_turn(turn)
        return result

    monkeypatch.setattr(society, "converge_history", fake_converge)

    result = _follow(blueprint, run_result, mock=False, converge=True)

    assert "### 最终统一意见" in result.section
    assert "统一结论：改成坐着也能做的版本。" in result.section
    assert "会议里根本站不起来。" in result.section
    assert "### 保留意见" in result.section
    assert "【本节分歧：有】" not in result.handbook


def test_mock_mode_never_runs_the_unification_round(
    blueprint, tmp_path, monkeypatch
):
    run_result = _seed(blueprint, tmp_path)
    monkeypatch.setattr(
        digest,
        "digest_section",
        lambda topic, *a, **k: f"## {topic.section_title}\n\n正文\n【本节分歧：有】",
    )
    called = False

    def fail_if_called(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("mock 模式不该跑统一轮")

    monkeypatch.setattr(society, "converge_history", fail_if_called)

    result = _follow(blueprint, run_result, mock=True, converge=True)

    assert called is False
    assert "### 最终统一意见" not in result.section


# ---------------------------------------------------------------------------
# 拼装与小工具
# ---------------------------------------------------------------------------


def test_append_to_handbook_keeps_the_footer_last():
    handbook = "# 标题\n\n正文\n\n---\n\n*本文件由 camel-duo-forge 生成：说明。*\n"
    updated = followup.append_to_handbook(handbook, "## 追加讨论 1：新的一节")
    assert updated.index("## 追加讨论 1：") < updated.index("本文件由 camel-duo-forge 生成")
    assert updated.rstrip().endswith("说明。*")


def test_append_to_handbook_without_a_footer_appends_at_the_end():
    updated = followup.append_to_handbook("# 标题\n\n正文\n", "## 追加讨论 1：新的一节")
    assert updated.startswith("# 标题")
    assert "## 追加讨论 1：新的一节" in updated


def test_sibling_path_follows_the_handbook_naming():
    path = followup.sibling_path("/tmp/handbook-20260101-120000-neck.mock.md", "quick")
    assert path.name == "quick-20260101-120000-neck.mock.md"
    other = followup.sibling_path("/tmp/my-handbook.md", "transcript")
    assert other.name == "transcript-my-handbook.md"


def test_check_title_rejects_multiline(tmp_path):
    with pytest.raises(followup.FollowupError):
        followup.check_title("第一行\n第二行")


# ---------------------------------------------------------------------------
# 追问 → 议题
# ---------------------------------------------------------------------------


def test_forge_followup_topic_clips_the_question_into_the_brief():
    topic = forge_followup_topic(
        "问" * 500,
        backend=ScriptedAuthorBackend(),
        title="程序员颈椎保养手册",
        purpose="给程序员写一份能照做的保养手册。",
        index=2,
    )
    assert topic.key == "followup-2"
    assert topic.section_title.startswith("追加讨论 2：")
    assert len(topic.brief) == MAX_BRIEF
    assert topic.brief.endswith("…")


def test_a_user_supplied_section_title_wins():
    topic = forge_followup_topic(
        "连续开会时怎么办？",
        backend=ScriptedAuthorBackend(),
        title="程序员颈椎保养手册",
        purpose="给程序员写一份能照做的保养手册。",
        index=1,
        section_title_hint="开会时怎么做",
    )
    assert topic.section_title == "追加讨论 1：开会时怎么做"
