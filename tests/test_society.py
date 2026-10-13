"""CAMEL 编排层：轮次、顺序、以及提前收手。

搬运自 camel-fitness-nutrition-duo 的 tests/test_pipeline.py 里对应的那几组，
把写死的营养师/教练换成 conftest 里的康复师/老张。

提前收手那组全靠「奇数调用都是挑战者」这条规律：开场白不花模型调用，
之后每轮先挑战者（user agent）后提出方（assistant agent）。
假后端据此让挑战者在第 2 轮说「无异议」，于是轮数、条数、收手标志全都可复现。
"""

from __future__ import annotations

import pytest

from duo_forge import digest, society
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


def test_exactly_two_personas_are_required(blueprint, backend):
    """顺序即语义，多一个少一个都说明调用方搞错了。"""
    with pytest.raises(ValueError) as excinfo:
        society.debate_topic(
            blueprint.topics[0], personas=blueprint.personas[:1], backend=backend
        )
    assert "恰好两位" in str(excinfo.value)


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
