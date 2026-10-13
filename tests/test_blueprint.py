"""蓝图：落盘、读回、给人看的那份，以及 `--only` 的语义。

蓝图是两段流程之间唯一的接口，它坏掉的话两头都跟着坏。
"""

from __future__ import annotations

import json

import pytest

from duo_forge import blueprint as blueprint_mod
from duo_forge.blueprint import BlueprintError, from_dict, load
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
    """这是「生成的提示词」的交付形态：两位专家实际拿到的 system message。"""
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


def test_a_blueprint_needs_exactly_two_personas(blueprint):
    raw = json.loads(blueprint.to_json())
    raw["personas"] = raw["personas"][:1]
    with pytest.raises(BlueprintError) as excinfo:
        from_dict(raw)
    assert "恰好两位" in str(excinfo.value)


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


def test_select_rejects_an_unknown_key_and_lists_the_valid_ones(blueprint):
    with pytest.raises(BlueprintError) as excinfo:
        blueprint.select("baselin")
    message = str(excinfo.value)
    assert "baselin" in message
    assert "baseline" in message  # 列出来让人一眼看出拼错了


def test_closing_brief_counts_the_topics(blueprint):
    assert str(len(blueprint.topics)) in blueprint.closing().brief
