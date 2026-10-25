"""用户自定义人格的解析与校验。

这层不依赖 camel：CLI 和网页启动器共用同一套规则，写错时的报错也要能直接给人看。
"""

from __future__ import annotations

import pytest

from duo_forge.personas import (
    MAX_MANUAL_SYSTEM_MESSAGE,
    MAX_NAME,
    MAX_PEOPLE,
    MAX_PERSONA_HINT,
    MAX_PERSONA_TITLE,
    MAX_ROLE,
    MAX_STANCE,
    MAX_TEAM_HINT,
    MIN_PEOPLE,
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


@pytest.mark.parametrize("marker", ["【发言纪律】", "【收手纪律】", "【统一纪律】"])
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


@pytest.mark.parametrize("marker", ["【发言纪律】", "【收手纪律】", "【统一纪律】"])
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
        ({"unknown": 1}, "不认识的键"),
        ({"personas": "不是数组"}, "必须是一个数组"),
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


# ---------------------------------------------------------------------------
# 多人：人数、数组、角色名、团队要求
# ---------------------------------------------------------------------------


def test_people_is_inferred_from_the_personas_array():
    plan = parse_persona_plan(
        {
            "personas": [
                _manual(name="甲"),
                {"mode": "hint", "hint": "一位实用派教练"},
                {},
            ]
        }
    )
    assert plan.people == 3
    assert len(plan.specs) == 3
    assert plan.specs[0].mode == "manual"
    assert plan.specs[1].mode == "hint"
    assert plan.specs[2].is_auto


def test_people_pads_missing_specs_with_auto():
    plan = parse_persona_plan({"personas": [_manual()]}, people=4)
    assert plan.people == 4
    assert [spec.mode for spec in plan.specs] == [
        "manual", "auto", "auto", "auto"
    ]


def test_people_accepts_an_array_shorter_than_the_count():
    plan = parse_persona_plan({"personas": [_manual(), _manual(name="乙")]}, people=5)
    assert plan.people == 5
    assert len(plan.specs) == 5
    assert all(spec.is_auto for spec in plan.specs[2:])


def test_people_rejects_more_specs_than_the_count():
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_plan(
            {"personas": [_manual(), _manual(name="乙"), _manual(name="丙")]},
            people=2,
        )
    assert "超过了人数" in str(excinfo.value)


@pytest.mark.parametrize("people", [1, 6, 0, "3", True])
def test_people_out_of_range_is_rejected(people):
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_plan({}, people=people)
    assert f"{MIN_PEOPLE}–{MAX_PEOPLE}" in str(excinfo.value)


def test_missing_people_defaults_to_two():
    plan = parse_persona_plan({})
    assert plan.people == 2
    assert plan.all_auto


def test_empty_personas_array_means_all_auto():
    plan = parse_persona_plan({"personas": []})
    assert plan.people == 2
    assert plan.all_auto


def test_array_wins_over_legacy_keys():
    plan = parse_persona_plan(
        {
            "personas": [_manual(name="甲")],
            "proposer": _manual(name="乙"),
            "challenger": _manual(name="丙"),
        },
        people=2,
    )
    assert plan.specs[0].name == "甲"
    assert plan.specs[1].is_auto


def test_manual_role_is_parsed():
    spec = parse_persona_spec(_manual(role="成本把关人"), LABEL)
    assert spec.role == "成本把关人"


def test_manual_role_has_a_length_limit():
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_spec(_manual(role="长" * (MAX_ROLE + 1)), LABEL)
    assert "角色名" in str(excinfo.value)


def test_manual_role_must_be_single_line():
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_spec(_manual(role="第一行\n第二行"), LABEL)
    assert "单行" in str(excinfo.value)


def test_manual_without_role_gets_a_positional_default():
    spec = parse_persona_spec(_manual(), LABEL)
    assert spec.role == ""
    assert spec.to_persona(1, 3).role == "主提出方"
    assert spec.to_persona(2, 3).role == "专家 2"
    assert spec.to_persona(2, 2).role == "挑战者"


def test_team_hint_is_parsed_and_stripped():
    plan = parse_persona_plan({"team": "  要有一个医生、一个患者、一个管理者  "})
    assert plan.team_hint == "要有一个医生、一个患者、一个管理者"


def test_team_hint_can_be_empty():
    assert parse_persona_plan({}).team_hint == ""
    assert parse_persona_plan({"team": ""}).team_hint == ""


def test_team_hint_has_a_length_limit():
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_plan({"team": "长" * (MAX_TEAM_HINT + 1)})
    assert "团队分工要求太长" in str(excinfo.value)


@pytest.mark.parametrize("marker", ["【发言纪律】", "【收手纪律】", "【统一纪律】"])
def test_team_hint_cannot_contain_the_discipline(marker):
    with pytest.raises(PersonaSpecError) as excinfo:
        parse_persona_plan({"team": marker + "随便写"})
    assert marker in str(excinfo.value)


def test_persona_plan_pads_itself():
    plan = PersonaPlan(people=3)
    assert plan.people == 3
    assert len(plan.specs) == 3
    assert plan.all_auto


def test_persona_plan_rejects_too_many_specs():
    with pytest.raises(PersonaSpecError):
        PersonaPlan(
            specs=(parse_persona_spec(_manual(), LABEL),) * 3,
            people=2,
        )


def test_three_manual_personas_are_all_manual():
    plan = parse_persona_plan(
        {"personas": [_manual(), _manual(name="乙"), _manual(name="丙")]},
        people=3,
    )
    assert plan.all_manual
    assert plan.has_manual
    assert not plan.all_auto
    assert not plan.both_manual
