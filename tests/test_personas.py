"""用户自定义人格的解析与校验。

这层不依赖 camel：CLI 和网页启动器共用同一套规则，写错时的报错也要能直接给人看。
"""

from __future__ import annotations

import pytest

from duo_forge.personas import (
    MAX_MANUAL_SYSTEM_MESSAGE,
    MAX_NAME,
    MAX_PERSONA_HINT,
    MAX_PERSONA_TITLE,
    MAX_STANCE,
    PersonaPlan,
    PersonaSpecError,
    parse_persona_plan,
    parse_persona_spec,
)

LABEL = "第一位专家（方案提出方）"


def _manual(**overrides) -> dict:
    data = {
        "mode": "manual",
        "name": "许医师",
        "title": "三甲医院康复科物理治疗师",
        "stance": "什么是对的",
        "system_message": "【人格】\n只认证据。\n\n【专业立场】\n先评估再干预。\n\n"
                          "【你的盲区，不要掩饰】\n方案偏理想。",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# 缺省与 auto
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, {}])
def test_missing_plan_means_both_auto(raw):
    plan = parse_persona_plan(raw)
    assert plan.all_auto
    assert not plan.has_manual
    assert not plan.both_manual


def test_a_missing_side_is_auto():
    plan = parse_persona_plan({"proposer": _manual()})
    assert plan.proposer.mode == "manual"
    assert plan.challenger.mode == "auto"
    assert plan.has_manual


def test_an_empty_side_is_auto():
    plan = parse_persona_plan({"proposer": {}, "challenger": {}})
    assert plan.all_auto


def test_explicit_auto_is_auto():
    plan = parse_persona_plan({"proposer": {"mode": "auto"}})
    assert plan.all_auto


def test_auto_with_fields_is_rejected():
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_plan({"proposer": {"mode": "auto", "hint": "随便写"}})
    assert "auto" in str(excinfo.value)


def test_fields_without_a_mode_are_rejected():
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_plan({"proposer": {"hint": "一位中医师"}})
    assert "没写 mode" in str(excinfo.value)


# ---------------------------------------------------------------------------
# hint
# ---------------------------------------------------------------------------


def test_hint_is_accepted_and_stripped():
    plan = parse_persona_plan(
        {"challenger": {"mode": "hint", "hint": "  一位带过很多程序员的教练  "}}
    )
    assert plan.challenger.mode == "hint"
    assert plan.challenger.hint == "一位带过很多程序员的教练"


@pytest.mark.parametrize("hint", ["", "   ", None, 123])
def test_hint_needs_text(hint):
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_spec({"mode": "hint", "hint": hint}, LABEL)
    assert "方向描述" in str(excinfo.value)


def test_hint_has_a_length_limit():
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_spec(
            {"mode": "hint", "hint": "长" * (MAX_PERSONA_HINT + 1)}, LABEL
        )
    assert "太长" in str(excinfo.value)


def test_hint_cannot_carry_persona_fields():
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_spec(
            {"mode": "hint", "hint": "方向", "name": "甲"}, LABEL
        )
    assert "不能再写" in str(excinfo.value)


@pytest.mark.parametrize("marker", ["【发言纪律】", "【收手纪律】"])
def test_hint_cannot_contain_the_discipline(marker):
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_spec({"mode": "hint", "hint": marker + "随便写"}, LABEL)
    assert marker in str(excinfo.value)


# ---------------------------------------------------------------------------
# manual
# ---------------------------------------------------------------------------


def test_manual_preserves_every_field():
    spec = parse_persona_spec(_manual(), LABEL)
    assert spec.mode == "manual"
    assert spec.name == "许医师"
    assert spec.title == "三甲医院康复科物理治疗师"
    assert spec.stance == "什么是对的"
    assert "【人格】" in spec.system_message


def test_manual_allows_a_long_background_and_cases():
    """「人格记忆」就是这一段：背景 + 几千字案例，最简单直接的注入口。"""
    memory = "【长期背景与案例】\n" + "病例经过。\n" * 1000
    message = _manual()["system_message"] + "\n\n" + memory
    assert len(message) < MAX_MANUAL_SYSTEM_MESSAGE
    spec = parse_persona_spec(_manual(system_message=message), LABEL)
    assert "【长期背景与案例】" in spec.system_message


def test_manual_message_has_its_own_limit():
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_spec(
            _manual(system_message="长" * (MAX_MANUAL_SYSTEM_MESSAGE + 1)), LABEL
        )
    assert "太长" in str(excinfo.value)


@pytest.mark.parametrize(
    "field, hint",
    [
        ("name", "称呼"),
        ("title", "身份"),
        ("stance", "负责什么"),
        ("system_message", "人格设定"),
    ],
)
def test_manual_needs_every_field(field, hint):
    data = _manual()
    data[field] = ""
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_spec(data, LABEL)
    assert hint in str(excinfo.value)


@pytest.mark.parametrize(
    "field, limit",
    [
        ("name", MAX_NAME),
        ("title", MAX_PERSONA_TITLE),
        ("stance", MAX_STANCE),
    ],
)
def test_manual_short_fields_have_limits(field, limit):
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_spec(_manual(**{field: "长" * (limit + 1)}), LABEL)
    assert "太长" in str(excinfo.value)


@pytest.mark.parametrize("field", ["name", "title", "stance"])
def test_manual_short_fields_must_be_single_line(field):
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_spec(_manual(**{field: "第一行\n第二行"}), LABEL)
    assert "单行" in str(excinfo.value)


@pytest.mark.parametrize("marker", ["【发言纪律】", "【收手纪律】"])
def test_manual_message_cannot_contain_the_discipline(marker):
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_spec(
            _manual(system_message="【人格】\n" + marker + "\n内容"), LABEL
        )
    assert marker in str(excinfo.value)


def test_manual_cannot_carry_a_hint():
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_spec(_manual(hint="方向"), LABEL)
    assert "不能再写 hint" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 结构错误
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, hint",
    [
        ({"proposer": {"mode": "unknown"}}, "mode 不合法"),
        ({"proposer": {"mode": "manual", "typo": "x"}}, "不认识的字段"),
        ({"proposer": "不是对象"}, "必须是一个 JSON 对象"),
        ({"personas": []}, "不认识的键"),
        ([], "必须是一个 JSON 对象"),
    ],
)
def test_structural_errors_are_reported(raw, hint):
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_plan(raw)
    assert hint in str(excinfo.value)


def test_both_sides_may_be_manual():
    plan = parse_persona_plan({"proposer": _manual(), "challenger": _manual()})
    assert plan.both_manual
    assert plan.has_manual
    assert not plan.all_auto


def test_the_default_plan_is_auto():
    assert PersonaPlan().all_auto
