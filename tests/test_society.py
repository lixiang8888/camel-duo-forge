"""CAMEL 编排层：轮次、顺序、以及提前收手。

搬运自 camel-fitness-nutrition-duo 的 tests/test_pipeline.py 里对应的那几组，
把写死的营养师/教练换成 conftest 里的康复师/老张。

提前收手那组全靠「奇数调用都是挑战者」这条规律：开场白不花模型调用，
之后每轮先挑战者（user agent）后提出方（assistant agent）。
假后端据此让挑战者在第 2 轮说「无异议」，于是轮数、条数、收手标志全都可复现。
"""

from __future__ import annotations

import pytest

from duo_forge import digest, personas, society
from duo_forge.backends import ScriptedBackend


def test_debate_alternates_and_keeps_the_opening(blueprint, backend):
    topic = blueprint.topics[0]
    transcript = society.debate_topic(
        topic, personas=blueprint.personas, backend=backend, rounds=2
    )

    # 1 条开场 + 2 轮 × 2 人
    assert len(transcript.turns) == 5
    assert [t.speaker for t in transcript.turns] == [
        blueprint.proposer.name,
        blueprint.challenger.name,
        blueprint.proposer.name,
        blueprint.challenger.name,
        blueprint.proposer.name,
    ]
    # 开场白原样保留，没有被模型改写
    assert transcript.turns[0].content == topic.opening


def test_debate_rounds_are_configurable(blueprint, backend):
    transcript = society.debate_topic(
        blueprint.topics[1], personas=blueprint.personas, backend=backend, rounds=4
    )
    assert len(transcript.turns) == 1 + 4 * 2


def test_two_to_five_personas_are_required(blueprint, backend):
    """顺序即语义，多一个少一个都说明调用方搞错了。"""
    with pytest.raises(ValueError) as excinfo:
        society.debate_topic(
            blueprint.topics[0], personas=blueprint.personas[:1], backend=backend
        )
    assert "2–5 位" in str(excinfo.value)


def test_mock_backend_never_claims_to_be_real(blueprint, backend):
    transcript = society.debate_topic(
        blueprint.topics[0], personas=blueprint.personas, backend=backend, rounds=1
    )
    generated = [t.content for t in transcript.turns[1:]]
    assert all("离线模拟回复" in text for text in generated)


# ---------------------------------------------------------------------------
# 提前收手
# ---------------------------------------------------------------------------


class ConcedingBackend(ScriptedBackend):
    """第 2 轮起，挑战者（奇数调用）只回一句「无异议」，其他照旧。"""

    def _reply(self, messages):
        self._calls += 1
        if self._calls >= 3 and self._calls % 2 == 1:
            return society.NO_MORE_OBJECTION
        return f"[假后端] 第 {self._calls} 次调用，这里本应是一段具体论证。"


class VerboselyConcedingBackend(ScriptedBackend):
    """每轮都回一长段，**其中包含**「无异议」字样——但它明显还有话说。"""

    def _reply(self, messages):
        self._calls += 1
        return (
            f"{society.NO_MORE_OBJECTION}——不过我还是有两点想补充："
            "第一，这个方案的时间成本你算过没有；第二，连着开会的人怎么办。"
        )


def _debate(blueprint, backend, rounds, early_stop):
    return society.debate_topic(
        blueprint.topics[0],
        personas=blueprint.personas,
        backend=backend,
        rounds=rounds,
        early_stop=early_stop,
    )


def test_early_stop_is_off_by_default(blueprint, backend):
    """默认关。直调 debate_topic 的行为必须是可预期的。"""
    transcript = _debate(blueprint, backend, rounds=4, early_stop=False)

    assert len(transcript.turns) == 1 + 4 * 2
    assert transcript.stopped_early is False
    assert transcript.planned_rounds == 4


def test_concession_stops_the_debate(blueprint):
    transcript = _debate(blueprint, ConcedingBackend(), rounds=4, early_stop=True)

    # 开场 + 2 整轮。触发收手的那一轮也是完整的：camel 的一轮往返
    # （挑战者先说、提出方后答）中途掐不断，所以收手总落在轮次边界上。
    assert len(transcript.turns) == 1 + 2 * 2
    assert transcript.stopped_early is True
    assert transcript.rounds_run == 2
    assert transcript.planned_rounds == 4


def test_concession_is_recorded_not_dropped(blueprint):
    """宣布收手的那一句必须留在记录里——先 record 再判断，不丢字。"""
    transcript = _debate(blueprint, ConcedingBackend(), rounds=4, early_stop=True)

    concession = [t for t in transcript.turns if t.content == society.NO_MORE_OBJECTION]
    assert len(concession) == 1
    assert concession[0].speaker == blueprint.challenger.name

    assert "收手" in transcript.to_markdown()
    # 喂给整理环节的是纯对话，收手这件事不能渗进去——否则 digest 会自己加话
    assert "收手" not in transcript.as_dialogue()


def test_min_rounds_floor_protects_the_first_exchange(blueprint):
    """第 1 轮是「挑战者挑刺 + 提出方回应」，是最小的有效交锋，不许砍。"""
    transcript = _debate(blueprint, ConcedingBackend(), rounds=2, early_stop=True)

    assert transcript.stopped_early is False
    assert len(transcript.turns) == 1 + 2 * 2


def test_a_long_turn_merely_containing_the_token_does_not_count(blueprint):
    """宁可漏判（多花一轮的钱），也不能误判（把一整段发言抹掉）。"""
    transcript = _debate(
        blueprint, VerboselyConcedingBackend(), rounds=4, early_stop=True
    )

    assert transcript.stopped_early is False
    assert len(transcript.turns) == 1 + 4 * 2


def test_scripted_backend_never_trips_the_concession(blueprint):
    """模拟后端的固定模板里没有那句话——这正是 pipeline 关掉它的底气。

    表征测试：将来谁把 mock 的文案改成含「无异议」，这条会先红。
    """
    transcript = _debate(blueprint, ScriptedBackend(), rounds=4, early_stop=True)

    assert transcript.stopped_early is False
    assert len(transcript.turns) == 1 + 4 * 2


def test_is_concession_ignores_only_padding():
    assert society._is_concession(society.NO_MORE_OBJECTION) is True
    assert society._is_concession(f"  {society.NO_MORE_OBJECTION}。 ") is True
    # 后面还有话 → 不算数
    assert society._is_concession(f"{society.NO_MORE_OBJECTION}不过还有两点") is False
    assert society._is_concession("") is False
    assert society._is_concession("我同意这个方向。") is False


# ---------------------------------------------------------------------------
# 整理环节
# ---------------------------------------------------------------------------


def test_digest_produces_a_section(blueprint, backend):
    topic = blueprint.topics[0]
    transcript = society.debate_topic(
        topic, personas=blueprint.personas, backend=backend, rounds=1
    )
    section = digest.digest_section(
        topic, transcript.as_dialogue(), backend=backend, title=blueprint.title
    )
    assert section.strip()


def test_assemble_puts_everything_together():
    handbook = digest.assemble(
        ["## 第一节\n内容甲"], "## 附录 A\n内容乙", header="# 标题"
    )
    assert "# 标题" in handbook
    assert "内容甲" in handbook
    assert "内容乙" in handbook
    assert "不构成专业意见" in handbook


def test_assemble_quick_keeps_the_disclaimer():
    """免责声明必须跟着速查版走。

    它是「一页纸」——正因如此最容易被单独转出去，所以那句话不能只留在完整版里。
    泛化之后这条更要紧：原来只知道是健身饮食，现在什么目的都可能进来。
    """
    quick = digest.assemble_quick("## 最要紧的几条\n- 每小时起身", header="# 手册 · 速查版")
    assert "每小时起身" in quick
    assert "# 手册 · 速查版" in quick
    assert "不构成专业意见" in quick


def test_the_editor_is_told_the_title(backend):
    """编辑环节对领域一无所知，标题是它唯一知道的上下文。"""
    assert "《颈椎手册》" in digest.editor_system("颈椎手册")
    assert "《颈椎手册》" in digest.QUICK_SYSTEM.format(title="颈椎手册")


def test_quick_prompt_feeds_the_handbook_and_forbids_additions(backend, monkeypatch):
    """压缩那一步的提示里，完整版全文和「只许删不许加」得同时在场。"""
    captured: dict[str, str] = {}
    real = digest.ChatAgent

    def spy_agent(*, system_message, model):
        captured["system"] = system_message
        agent = real(system_message=system_message, model=model)
        inner = agent.step

        def step(prompt):
            captured["prompt"] = prompt
            return inner(prompt)

        agent.step = step
        return agent

    monkeypatch.setattr(digest, "ChatAgent", spy_agent)
    digest.digest_quick("# 完整版\n正文甲", backend=backend, title="颈椎手册")

    assert "# 完整版\n正文甲" in captured["prompt"]
    assert "严禁添加完整版里没有的" in captured["system"]


# ---------------------------------------------------------------------------
# 三人：固定轮转、全员收手
# ---------------------------------------------------------------------------


def test_three_person_turn_order(blueprint3, backend):
    topic = blueprint3.topics[0]
    transcript = society.debate_topic(
        topic, personas=blueprint3.personas, backend=backend, rounds=2
    )

    names = [p.name for p in blueprint3.personas]
    assert len(transcript.turns) == 1 + 2 * 3
    assert [t.speaker for t in transcript.turns] == [
        names[0], names[1], names[2], names[0],
        names[1], names[2], names[0],
    ]
    assert transcript.turns[0].content == topic.opening
    assert transcript.turns[0].role == "主提出方"


class ThreePersonConcedingBackend(ScriptedBackend):
    """三人模式：第 2 轮的两个回应者都回「无异议」。"""

    def _reply(self, messages):
        self._calls += 1
        if self._calls in (4, 5):
            return society.NO_MORE_OBJECTION
        return f"[假后端] 第 {self._calls} 次调用，这里本应是一段具体论证。"


class ThreePersonOneDissentBackend(ScriptedBackend):
    """第 2 轮只有第一位回应者收手，第二位仍有意见。"""

    def _reply(self, messages):
        self._calls += 1
        if self._calls == 4:
            return society.NO_MORE_OBJECTION
        if self._calls == 5:
            return "我还有一点：这个方案的成本没人算过。"
        return f"[假后端] 第 {self._calls} 次调用，这里本应是一段具体论证。"


def test_all_responders_conceding_stops_the_debate(blueprint3):
    transcript = society.debate_topic(
        blueprint3.topics[0],
        personas=blueprint3.personas,
        backend=ThreePersonConcedingBackend(),
        rounds=4,
        early_stop=True,
    )

    assert transcript.stopped_early is True
    assert transcript.rounds_run == 2
    assert len(transcript.turns) == 1 + 2 * 3
    assert transcript.conceding == ("老张", "算账方")
    assert "已无新的反对意见" in transcript.to_markdown()


def test_one_dissenting_responder_keeps_the_debate_going(blueprint3):
    transcript = society.debate_topic(
        blueprint3.topics[0],
        personas=blueprint3.personas,
        backend=ThreePersonOneDissentBackend(),
        rounds=4,
        early_stop=True,
    )

    assert transcript.stopped_early is False
    assert transcript.rounds_run == 4
    assert len(transcript.turns) == 1 + 4 * 3


def test_a_three_person_debate_can_only_stop_after_min_rounds(blueprint3):
    class EarlyConceders(ScriptedBackend):
        def _reply(self, messages):
            self._calls += 1
            # 第 1 轮两个回应者就收手（调用 1、2），第 2 轮再次收手（4、5）
            return society.NO_MORE_OBJECTION

    transcript = society.debate_topic(
        blueprint3.topics[0],
        personas=blueprint3.personas,
        backend=EarlyConceders(),
        rounds=4,
        early_stop=True,
    )

    assert transcript.stopped_early is True
    assert transcript.rounds_run == 2


def test_roles_appear_in_the_transcript(blueprint3, backend):
    transcript = society.debate_topic(
        blueprint3.topics[0], personas=blueprint3.personas, backend=backend, rounds=1
    )

    dialogue = transcript.as_dialogue()
    assert "康复师（主提出方）" in dialogue
    assert "老张（挑战者）" in dialogue
    assert "算账方（成本把关人）" in dialogue
    assert "（主提出方）" in transcript.to_markdown()


# ---------------------------------------------------------------------------
# 全部议题结束后的最终统一轮
# ---------------------------------------------------------------------------


class RepliesBackend(ScriptedBackend):
    """按顺序吐出预设回复，用来精确控制统一轮里谁说了什么。"""

    def __init__(self, replies):
        super().__init__()
        self.replies = list(replies)

    def _reply(self, messages):
        self._calls += 1
        return self.replies.pop(0) if self.replies else "[假后端] 补充"


def test_converge_history_collects_conclusion_and_reservations(blueprint):
    backend = RepliesBackend(
        [
            "我还有一点：成本没人算过。",
            "统一结论：采纳成本条款，其余按原方案定稿。",
        ]
    )
    result = society.converge_history(
        personas=blueprint.personas, backend=backend, context="【最终成果摘要】"
    )

    # 回应者先说、主提出方最后给结论；固定只有这一轮。
    assert [t.speaker for t in result.turns] == ["老张", "康复师"]
    assert result.unified.startswith("统一结论")
    assert result.reservations == (("老张", "我还有一点：成本没人算过。"),)
    assert result.all_conceded is False
    assert "老张" in result.to_markdown()


def test_converge_history_records_full_agreement(blueprint):
    backend = RepliesBackend(
        [society.NO_MORE_OBJECTION, "统一结论：维持现有成果。"]
    )
    result = society.converge_history(
        personas=blueprint.personas, backend=backend, context="【最终成果摘要】"
    )

    assert result.all_conceded is True
    assert result.reservations == ()
    assert "所有回应者都表示接受统一结论" in result.to_markdown()


def test_converge_history_keeps_three_person_turn_order(blueprint3):
    backend = RepliesBackend(
        [
            society.NO_MORE_OBJECTION,
            "我保留：未成年提示的强度还不够。",
            "统一结论：采纳未成年提示加强，其余不变。",
        ]
    )
    result = society.converge_history(
        personas=blueprint3.personas, backend=backend, context="【最终成果摘要】"
    )

    assert [t.speaker for t in result.turns] == ["老张", "算账方", "康复师"]
    assert result.reservations == (("算账方", "我保留：未成年提示的强度还不够。"),)


def test_a_responder_who_only_says_no_objection_is_not_a_reservation(blueprint):
    backend = RepliesBackend(
        [society.NO_MORE_OBJECTION, "统一结论：按原方案定稿。"]
    )
    result = society.converge_history(
        personas=blueprint.personas, backend=backend, context="摘要"
    )
    assert "老张" not in dict(result.reservations)


def test_a_vague_yes_is_still_recorded_as_a_reservation(blueprint):
    """宁可把模糊话记成保留，也不能把真正的反对吞掉。"""
    backend = RepliesBackend(
        ["我基本同意，不过成本还是没人算。", "统一结论：定稿。"]
    )
    result = society.converge_history(
        personas=blueprint.personas, backend=backend, context="摘要"
    )
    assert result.all_conceded is False
    assert "成本" in dict(result.reservations)["老张"]


def test_convergence_system_message_uses_the_unification_discipline(blueprint):
    message = personas.build_convergence_system_message(blueprint.personas[1])
    opener = personas.build_convergence_system_message(
        blueprint.personas[0], opener=True
    )

    assert "【统一纪律】" in message
    assert "【无异议】" in message
    assert "你是回应者" in message
    assert "你是主提出方" in opener
    # 普通收手纪律说「还有意见就必须继续提」，和统一轮要的「只留最后一条」冲突。
    assert "【收手纪律】" not in message
    assert "【收手纪律】" not in opener


# ---------------------------------------------------------------------------
# 分歧标记与统一轮的整理
# ---------------------------------------------------------------------------


def test_disagreement_markers_are_detected_and_stripped():
    text = "## 一节\n内容\n【本节分歧：有】\n"
    assert digest.has_disagreement(text)

    cleaned = digest.strip_disagreement_markers(text)
    assert "【本节分歧：有】" not in cleaned
    assert "内容" in cleaned

    # 模型偶尔会用半角冒号；标记的语义不变，代码不该因此漏判。
    half_width = "## 一节\n内容\n【未解决分歧:有】"
    assert digest.has_disagreement(half_width)
    assert "【未解决分歧:有】" not in digest.strip_disagreement_markers(half_width)


def test_a_missing_marker_counts_as_no_disagreement():
    """标记是格式契约；没写就按无分歧处理，不为格式错误多跑一轮全员调用。"""
    assert digest.has_disagreement("## 一节\n内容") is False
    assert digest.has_disagreement("## 一节\n【本节分歧：无】") is False


def test_convergence_excerpt_keeps_points_disagreements_and_appendix_a():
    handbook = """# 标题

## 一、节

### 要点
- 要点甲
- 要点乙

### 具体怎么做
- 这一大段统一轮不需要

### 各方分歧
- 分歧甲

## 附录 A · 分歧备忘
- 未解决甲

## 附录 B · 红线信号
- 红线甲
"""
    excerpt = digest.convergence_excerpt(handbook)

    assert "## 一、节" in excerpt
    assert "要点甲" in excerpt
    assert "分歧甲" in excerpt
    assert "未解决甲" in excerpt
    assert "这一大段统一轮不需要" not in excerpt
    assert "红线甲" not in excerpt


def test_assemble_convergence_lists_reservations():
    section = digest.assemble_convergence("统一结论甲", (("老张", "保留甲"),))

    assert "## 附录 D · 最终统一意见" in section
    assert "统一结论甲" in section
    assert "**老张**：保留甲" in section


def test_assemble_convergence_says_when_everyone_agreed():
    section = digest.assemble_convergence("统一结论甲", ())
    assert "所有回应者都明确表示接受统一结论" in section


def test_header_mentions_appendix_d_only_when_it_exists(blueprint):
    without = digest.header("标题", blueprint.personas)
    with_d = digest.header("标题", blueprint.personas, unified=True)

    assert "附录 D" not in without
    assert "附录 D" in with_d
