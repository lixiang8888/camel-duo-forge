"""蓝图：落盘、读回、给人看的那份，以及 `--only` 的语义。

蓝图是两段流程之间唯一的接口，它坏掉的话两头都跟着坏。
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from duo_forge import blueprint as blueprint_mod
from duo_forge.blueprint import BlueprintError, from_dict, load
from duo_forge.personas import Persona
from duo_forge.topics import CLOSING_KEY


# ---------------------------------------------------------------------------
# 往返
# ---------------------------------------------------------------------------


def test_json_round_trip(blueprint):
    back = from_dict(json.loads(blueprint.to_json()))
    assert back == blueprint


def test_json_is_human_editable(blueprint):
    """它是要被人动手改的，所以得是能看懂的 JSON，不能是一行。"""
    text = blueprint.to_json()
    assert text.endswith("\n")
    assert "\n  " in text
    raw = json.loads(text)
    assert raw["format"] == blueprint_mod.FORMAT
    assert set(raw) >= {"title", "slug", "purpose", "personas", "topics"}


def test_save_uses_the_slug_and_a_stamp(blueprint, tmp_path):
    path = blueprint.save(tmp_path)
    assert path.parent == tmp_path
    assert path.name.startswith("neck-care-")
    assert path.suffix == ".json"
    assert load(path) == blueprint


# ---------------------------------------------------------------------------
# 给人看的那份
# ---------------------------------------------------------------------------


def test_markdown_shows_the_full_prompts(blueprint):
    """这是「生成的提示词」的交付形态：各位专家实际拿到的 system message。"""
    text = blueprint.to_markdown()

    assert "neck-care" not in text  # slug 是给机器的，不往人看的这份里塞
    for persona in blueprint.personas:
        assert persona.name in text
        assert persona.system_message.strip() in text
    # 由代码追加的纪律也要摊开，否则看的人不知道模型到底被要求了什么
    assert "【发言纪律】" in text
    assert "【无异议】" in text


def test_markdown_lists_every_topic_plus_the_closing_one(blueprint):
    text = blueprint.to_markdown()
    for topic in blueprint.topics:
        assert topic.section_title in text
        assert topic.opening.splitlines()[0] in text
    assert blueprint.closing().section_title in text


# ---------------------------------------------------------------------------
# 读进来的东西不对时，要说清是哪不对
# ---------------------------------------------------------------------------


def test_bad_json_reports_the_line(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{不是 JSON", encoding="utf-8")
    with pytest.raises(BlueprintError) as excinfo:
        load(path)
    assert "不是合法 JSON" in str(excinfo.value)


def test_missing_file():
    with pytest.raises(BlueprintError) as excinfo:
        load("/nonexistent/nowhere.json")
    assert "读不出" in str(excinfo.value)


def test_future_format_version_is_refused(blueprint):
    raw = json.loads(blueprint.to_json())
    raw["format"] = 99
    with pytest.raises(BlueprintError) as excinfo:
        from_dict(raw)
    assert "99" in str(excinfo.value)


def test_a_blueprint_needs_two_to_five_personas(blueprint):
    raw = json.loads(blueprint.to_json())
    raw["personas"] = raw["personas"][:1]
    with pytest.raises(BlueprintError) as excinfo:
        from_dict(raw)
    assert "2–5 位" in str(excinfo.value)


def test_closing_key_is_reserved(blueprint):
    """收尾议题由代码生成，蓝图里放一个会撞 key。"""
    raw = json.loads(blueprint.to_json())
    raw["topics"][0]["key"] = CLOSING_KEY
    with pytest.raises(BlueprintError) as excinfo:
        from_dict(raw)
    assert "保留" in str(excinfo.value)


def test_missing_purpose(blueprint):
    raw = json.loads(blueprint.to_json())
    del raw["purpose"]
    with pytest.raises(BlueprintError) as excinfo:
        from_dict(raw)
    assert "purpose" in str(excinfo.value)


# ---------------------------------------------------------------------------
# --only 的语义
# ---------------------------------------------------------------------------


def test_select_defaults_to_everything(blueprint):
    topics = blueprint.select("")
    assert topics == blueprint.all_topics()
    assert topics[-1].key == CLOSING_KEY
    assert len(topics) == len(blueprint.topics) + 1


def test_select_keeps_the_requested_order(blueprint):
    keys = [t.key for t in blueprint.topics]
    topics = blueprint.select(f"{keys[1]},{keys[0]}")
    assert [t.key for t in topics] == [keys[1], keys[0], CLOSING_KEY]


def test_select_always_puts_closing_last(blueprint):
    """它不是排版偏好：收尾议题要读完前面所有对话才写得出来。"""
    topics = blueprint.select("reality")
    assert topics[-1].key == CLOSING_KEY
    assert [t.key for t in topics] == ["reality", CLOSING_KEY]


def test_select_closing_counts_only_the_selected_topics(blueprint):
    """收尾议题的「我们前面谈了 N 个」必须是本次选中的数量，不是蓝图里的总数。

    只跑一个议题却说谈了全部，是在骗读者，也会让整理环节去回顾没跑过的内容。
    """
    assert len(blueprint.topics) == 2
    closing = blueprint.select("reality")[-1]
    assert "前面 1 个议题" in closing.brief
    assert "我们前面谈了 1 个议题" in closing.opening


def test_select_rejects_an_unknown_key_and_lists_the_valid_ones(blueprint):
    with pytest.raises(BlueprintError) as excinfo:
        blueprint.select("baselin")
    message = str(excinfo.value)
    assert "baselin" in message
    assert "baseline" in message  # 列出来让人一眼看出拼错了


def test_closing_brief_counts_the_topics(blueprint):
    assert str(len(blueprint.topics)) in blueprint.closing().brief


# ---------------------------------------------------------------------------
# 用户手写的人格（含长背景与案例）
# ---------------------------------------------------------------------------


def test_a_manual_persona_round_trips(blueprint):
    manual = Persona(
        "许医师",
        "三甲医院康复科物理治疗师",
        "什么是对的",
        "【人格】\n只认证据。\n\n【专业立场】\n先评估。\n\n"
        "【你的盲区，不要掩饰】\n偏理想。\n\n【长期背景与案例】\n病例。",
    )
    edited = dataclasses.replace(blueprint, personas=(manual, blueprint.challenger))
    back = from_dict(json.loads(edited.to_json()))
    assert back.personas[0] == manual


def test_a_long_manual_message_loads(blueprint, tmp_path):
    """加载器保持宽松：蓝图是给人手改的，12000 字的上限只在创建时管。"""
    long_message = "【人格】\n" + "病例经过。\n" * 1500
    manual = Persona("甲", "身份", "立场", long_message)
    edited = dataclasses.replace(blueprint, personas=(manual, blueprint.challenger))
    path = edited.save(tmp_path)
    assert load(path).personas[0].system_message == long_message.strip()


def test_markdown_fence_survives_a_code_fence_in_the_persona(blueprint):
    tricky = Persona(
        "甲", "身份", "立场", "【人格】\n```python\nprint(1)\n```\n【专业立场】\n内容"
    )
    edited = dataclasses.replace(blueprint, personas=(tricky, blueprint.challenger))
    text = edited.to_markdown()
    # 正文里有三个反引号，围栏必须更长，否则会被提前闭合
    assert "````text" in text


def test_save_does_not_overwrite_a_same_second_blueprint(blueprint, tmp_path):
    first = blueprint.save(tmp_path, stamp="20260101-000000")
    second = blueprint.save(tmp_path, stamp="20260101-000000")
    assert first != second
    assert second.exists()
    assert load(second) == blueprint


# ---------------------------------------------------------------------------
# format 2 与旧 format 1
# ---------------------------------------------------------------------------


def _raw_with_extra_people(blueprint, extra: int):
    raw = json.loads(blueprint.to_json())
    for index in range(extra):
        raw["personas"].append(
            {
                "role": f"角色{index + 3}",
                "name": f"专家{index + 3}",
                "title": "身份",
                "stance": "负责什么",
                "system_message": "【人格】\n内容",
            }
        )
    return raw


def test_format_1_is_upgraded_with_default_roles(blueprint):
    raw = json.loads(blueprint.to_json())
    raw["format"] = 1
    for persona in raw["personas"]:
        persona.pop("role", None)

    back = from_dict(raw)

    assert back.people == 2
    assert back.personas[0].role == "主提出方"
    assert back.personas[1].role == "挑战者"
    assert back.to_dict()["format"] == blueprint_mod.FORMAT


def test_format_1_still_requires_exactly_two(blueprint):
    raw = _raw_with_extra_people(blueprint, 1)
    raw["format"] = 1
    with pytest.raises(BlueprintError) as excinfo:
        from_dict(raw)
    assert "恰好两位" in str(excinfo.value)


@pytest.mark.parametrize("people", [3, 4, 5])
def test_two_to_five_personas_round_trip(blueprint, people):
    raw = _raw_with_extra_people(blueprint, people - 2)
    back = from_dict(raw)

    assert back.people == people
    assert json.loads(back.to_json()) == raw


def test_duplicate_names_are_rejected(blueprint):
    raw = json.loads(blueprint.to_json())
    raw["personas"][1]["name"] = raw["personas"][0]["name"]
    with pytest.raises(BlueprintError) as excinfo:
        from_dict(raw)
    assert "重名" in str(excinfo.value)


def test_missing_role_in_format_2_falls_back_for_display(blueprint):
    raw = json.loads(blueprint.to_json())
    raw["personas"][0].pop("role")
    back = from_dict(raw)
    assert back.personas[0].role == ""
    assert back.personas[0].display_role == "专家"


def test_overlong_role_is_rejected(blueprint):
    raw = json.loads(blueprint.to_json())
    raw["personas"][0]["role"] = "长" * 13
    with pytest.raises(BlueprintError) as excinfo:
        from_dict(raw)
    assert "角色名太长" in str(excinfo.value)


def test_three_person_markdown_lists_roles(blueprint):
    back = from_dict(_raw_with_extra_people(blueprint, 1))
    text = back.to_markdown()
    assert "## 3 位专家" in text
    assert "成本把关人" not in text  # 这个是 fixture 里没有的，确认没有凭空出现
    assert "角色3" in text
