"""生成那一步：解析、校验、重试、以及「纪律由代码追加」这条契约。

全程离线（ScriptedAuthorBackend 吐的是合法 JSON，不联网、不花钱）。
"""

from __future__ import annotations

import json

import pytest

from duo_forge import authoring
from duo_forge.authoring import AuthoringError, forge, forge_personas, forge_topics
from duo_forge.backends import ScriptedAuthorBackend, ScriptedBackend
from duo_forge.personas import CHALLENGER_CONTRACT, Persona, full_system_message
from duo_forge.purpose import Purpose
from duo_forge.topics import MAX_TOPICS


@pytest.fixture
def author() -> ScriptedAuthorBackend:
    return ScriptedAuthorBackend()


# ---------------------------------------------------------------------------
# 从模型的回复里掏 JSON：宽松，但不能松到抓错东西
# ---------------------------------------------------------------------------


def test_extract_plain_json():
    assert authoring._extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_wrapped_in_a_fence():
    """模型很爱套一层 ```json。"""
    text = '```json\n{"a": 1}\n```'
    assert authoring._extract_json(text) == {"a": 1}


def test_extract_json_after_a_preamble():
    """也爱在前面写一句「好的，以下是设计」。"""
    text = '好的，以下是设计：\n\n{"a": 1}\n\n希望有帮助。'
    assert authoring._extract_json(text) == {"a": 1}


def test_extract_json_ignores_braces_inside_strings():
    """字符串里的花括号不能参与配对，否则会截断在半个对象上。"""
    text = '{"a": "这里有个 } 括号", "b": 2}'
    assert authoring._extract_json(text) == {"a": "这里有个 } 括号", "b": 2}


def test_extract_json_ignores_escaped_quotes():
    text = r'{"a": "他说\"好\"", "b": 2}'
    assert authoring._extract_json(text) == {"a": '他说"好"', "b": 2}


@pytest.mark.parametrize(
    "text, hint",
    [
        ("", "没有返回"),
        ("我写不出来。", "找不到 JSON"),
        ('{"a": 1', "没有闭合"),
        ('{"a": }', "不是合法 JSON"),
    ],
)
def test_extract_json_reports_what_is_wrong(text, hint):
    with pytest.raises(AuthoringError) as excinfo:
        authoring._extract_json(text)
    assert hint in str(excinfo.value)


# ---------------------------------------------------------------------------
# 功能一：生成两位专家
# ---------------------------------------------------------------------------


def test_forge_personas_offline(purpose, author):
    made = forge_personas(purpose, backend=author)

    assert made.title.strip()
    assert made.slug and made.slug.islower()
    assert made.proposer.name != made.challenger.name
    assert made.proposer.system_message != made.challenger.system_message


def test_the_two_personas_are_actually_opposed(purpose, author):
    """两个人格必须真的不同——这是整个项目产出有没有信息量的唯一来源。"""
    made = forge_personas(purpose, backend=author)
    assert made.proposer.stance != made.challenger.stance


def test_challenger_gets_the_concession_contract_and_proposer_does_not():
    """收手机制是 society 的硬契约，只该追加给挑战者。

    这条不是「提示词写得对不对」的问题：模型漏写一次不会有任何报错，
    只会让每个议题都闷头跑满轮数（慢、贵，而且看不出来）。
    所以它由代码追加，这里把它钉死。
    """
    persona = Persona("甲", "某身份", "某立场", "【人格】\n内容")
    as_challenger = full_system_message(persona, challenger=True)
    as_proposer = full_system_message(persona, challenger=False)

    assert CHALLENGER_CONTRACT in as_challenger
    assert "【无异议】" in as_challenger
    assert CHALLENGER_CONTRACT not in as_proposer
    assert "【无异议】" not in as_proposer
    # 发言纪律两边都有
    assert "【发言纪律】" in as_challenger
    assert "【发言纪律】" in as_proposer


def test_persona_missing_a_field_is_rejected():
    with pytest.raises(AuthoringError) as excinfo:
        authoring._parse_personas(
            {
                "title": "标题",
                "slug": "x",
                "proposer": {"name": "甲", "title": "身份", "stance": "立场"},
                "challenger": {"name": "乙", "title": "身份", "stance": "立场",
                               "system_message": "内容"},
            }
        )
    assert "第一位专家" in str(excinfo.value)
    assert "system_message" in str(excinfo.value)


def test_persona_blank_field_is_rejected():
    with pytest.raises(AuthoringError):
        authoring._parse_personas(
            {
                "title": "标题",
                "slug": "x",
                "proposer": {"name": "  ", "title": "身份", "stance": "立场",
                             "system_message": "内容"},
                "challenger": {"name": "乙", "title": "身份", "stance": "立场",
                               "system_message": "内容"},
            }
        )


def test_overlong_persona_field_is_rejected():
    with pytest.raises(AuthoringError) as excinfo:
        authoring._parse_personas(
            {
                "title": "标题",
                "slug": "x",
                "proposer": {"name": "甲" * 30, "title": "身份", "stance": "立场",
                             "system_message": "内容"},
                "challenger": {"name": "乙", "title": "身份", "stance": "立场",
                               "system_message": "内容"},
            }
        )
    assert "太长" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 功能二：生成议题
# ---------------------------------------------------------------------------


def test_forge_topics_offline(purpose, personas, author):
    topics = forge_topics(purpose, personas, backend=author)
    assert len(topics) >= 2
    for topic in topics:
        assert topic.key and topic.section_title and topic.brief
        assert topic.goal and topic.opening


def test_topic_keys_are_unique_and_avoid_the_reserved_one():
    """模型给重了、或者撞上收尾议题的保留 key，都得在这里让开。"""
    raw = {
        "topics": [
            {"key": "same", "section_title": "一", "brief": "b", "goal": "g", "opening": "o"},
            {"key": "same", "section_title": "二", "brief": "b", "goal": "g", "opening": "o"},
            {"key": "closing", "section_title": "三", "brief": "b", "goal": "g", "opening": "o"},
        ]
    }
    keys = [t.key for t in authoring._parse_topics(raw)]
    assert keys == ["same", "same-2", "closing-2"]
    assert len(set(keys)) == 3


def test_topic_key_is_slugified():
    raw = {"topics": [{"key": "Sleep Debt! 周末", "section_title": "一", "brief": "b",
                       "goal": "g", "opening": "o"}]}
    assert authoring._parse_topics(raw)[0].key == "sleep-debt"


def test_topic_without_a_usable_key_still_gets_one():
    raw = {"topics": [{"key": "议题", "section_title": "一", "brief": "b",
                       "goal": "g", "opening": "o"}]}
    assert authoring._parse_topics(raw)[0].key == "topic-1"


def test_too_many_topics_are_truncated_not_fatal():
    """模型多给几个不算错——砍掉就是了，为这个重试一次不值得。"""
    raw = {
        "topics": [
            {"key": f"t{i}", "section_title": f"第{i}", "brief": "b", "goal": "g",
             "opening": "o"}
            for i in range(MAX_TOPICS + 5)
        ]
    }
    assert len(authoring._parse_topics(raw)) == MAX_TOPICS


def test_empty_topic_list_is_rejected():
    with pytest.raises(AuthoringError) as excinfo:
        authoring._parse_topics({"topics": []})
    assert "topics" in str(excinfo.value)


def test_overlong_topic_brief_is_rejected():
    """brief 每个议题每一轮都要进 system message，超长是有真金白银的。"""
    raw = {"topics": [{"key": "a", "section_title": "一", "brief": "长" * 500,
                       "goal": "g", "opening": "o"}]}
    with pytest.raises(AuthoringError) as excinfo:
        authoring._parse_topics(raw)
    assert "brief" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 重试：格式错一次不该让整个流程作废
# ---------------------------------------------------------------------------


class FlakyBackend(ScriptedBackend):
    """第一次回一段没用的东西，第二次回合法 JSON。"""

    def __init__(self, good: dict, bad_times: int = 1) -> None:
        super().__init__()
        self._good = good
        self._bad_times = bad_times
        self.seen_prompts: list[str] = []

    def _reply(self, messages):
        self._calls += 1
        self.seen_prompts.append(
            " ".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
        )
        if self._calls <= self._bad_times:
            return "抱歉，我想不出来。"
        return json.dumps(self._good, ensure_ascii=False)


_GOOD_PERSONAS = {
    "title": "重试出来的标题",
    "slug": "retry",
    "proposer": {"name": "甲", "title": "身份甲", "stance": "什么是对的",
                 "system_message": "【人格】\n甲"},
    "challenger": {"name": "乙", "title": "身份乙", "stance": "做不做得到",
                   "system_message": "【人格】\n乙"},
}


def test_a_bad_first_answer_is_retried(purpose):
    backend = FlakyBackend(_GOOD_PERSONAS)
    made = forge_personas(purpose, backend=backend)

    assert made.title == "重试出来的标题"
    assert backend._calls == 2


def test_the_retry_tells_the_model_what_was_wrong(purpose):
    """重试提示里必须带上具体的错，否则模型多半原样再错一次。"""
    backend = FlakyBackend(_GOOD_PERSONAS)
    forge_personas(purpose, backend=backend)

    second = backend.seen_prompts[1]
    assert "上一次的输出不能用了" in second
    assert "找不到 JSON" in second


def test_two_bad_answers_raise(purpose):
    backend = FlakyBackend(_GOOD_PERSONAS, bad_times=99)
    with pytest.raises(AuthoringError) as excinfo:
        forge_personas(purpose, backend=backend)
    assert "人格生成失败" in str(excinfo.value)
    assert backend._calls == 2, "重试次数是有限的，不能一直要"


# ---------------------------------------------------------------------------
# 合起来：目的 → 蓝图
# ---------------------------------------------------------------------------


def test_forge_returns_a_complete_blueprint(purpose, author):
    made = forge(purpose, backend=author, topic_count=5)

    assert made.title == purpose.title, "用户给了标题就该照用"
    assert len(made.personas) == 2
    assert len(made.topics) >= 2
    # 收尾议题不在 topics 里，由 all_topics 补
    assert all(t.key != "closing" for t in made.topics)
    assert made.all_topics()[-1].key == "closing"
    assert made.all_topics()[-1].brief.count(str(len(made.topics))) > 0


def test_forge_uses_the_title_the_model_suggested(purpose, author):
    """没给标题时，用模型起的那个。"""
    bare = Purpose(text=purpose.text, title="")
    made = forge(bare, backend=author)
    assert made.title
    assert made.purpose.title == ""
