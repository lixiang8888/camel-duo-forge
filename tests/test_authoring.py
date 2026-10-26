"""生成那一步：解析、校验、重试、以及「纪律由代码追加」这条契约。

全程离线（ScriptedAuthorBackend 吐的是合法 JSON，不联网、不花钱）。
"""

from __future__ import annotations

import json

import pytest

from duo_forge import authoring
from duo_forge.authoring import (
    AuthoringError,
    distill_material,
    forge,
    forge_personas,
    forge_topics,
)
from duo_forge.backends import ScriptedAuthorBackend, ScriptedBackend
from duo_forge.personas import (
    CONCESSION_CONTRACT,
    Persona,
    PersonaPlan,
    full_system_message,
    parse_persona_plan,
)
from duo_forge.purpose import MAX_MATERIAL, MAX_PURPOSE, Purpose
from duo_forge.topics import MAX_REFORGE_HINT, MAX_TOPICS


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
# 功能一：生成专家
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


def test_responders_get_the_concession_contract_and_the_opener_does_not():
    """收手机制是 society 的硬契约，只该追加给回应者。

    这条不是「提示词写得对不对」的问题：模型漏写一次不会有任何报错，
    只会让每个议题都闷头跑满轮数（慢、贵，而且看不出来）。
    所以它由代码追加，这里把它钉死。
    """
    persona = Persona("甲", "某身份", "某立场", "【人格】\n内容")
    as_opener = full_system_message(persona, opener=True)
    as_responder = full_system_message(persona, opener=False)

    assert CONCESSION_CONTRACT in as_responder
    assert "【无异议】" in as_responder
    assert CONCESSION_CONTRACT not in as_opener
    assert "【无异议】" not in as_opener
    # 发言纪律两边都有
    assert "【发言纪律】" in as_responder
    assert "【发言纪律】" in as_opener


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


# ---------------------------------------------------------------------------
# 用户自定义人格
# ---------------------------------------------------------------------------

_MANUAL_PROPOSER = {
    "mode": "manual",
    "name": "许医师",
    "title": "三甲医院康复科物理治疗师",
    "stance": "什么是对的",
    "system_message": (
        "【人格】\nUNIQUE_MANUAL_MEMORY_MARKER_9f3c\n\n"
        "【专业立场】\n先评估再干预。"
    ),
}

_MANUAL_CHALLENGER = {
    "mode": "manual",
    "name": "老张",
    "title": "带过两百个程序员的健身教练",
    "stance": "他到底做不做得到",
    "system_message": "【人格】\n务实。\n\n【专业立场】\n能坚持的六十分胜过两周的一百分。",
}


class RecordingAuthorBackend(ScriptedAuthorBackend):
    """记录每次提示词，再交给模拟后端回话。"""

    def __init__(self) -> None:
        super().__init__()
        self.seen_prompts: list[str] = []

    def _reply(self, messages):
        self.seen_prompts.append(
            " ".join(
                str(m.get("content", "")) for m in messages if isinstance(m, dict)
            )
        )
        return super()._reply(messages)


def test_a_manual_persona_is_used_verbatim(purpose):
    plan = parse_persona_plan(
        {
            "proposer": _MANUAL_PROPOSER,
            "challenger": {"mode": "hint", "hint": "一位带过很多程序员的实战派教练"},
        }
    )
    made = forge_personas(purpose, backend=ScriptedAuthorBackend(), plan=plan)

    assert made.proposer.name == "许医师"
    assert made.proposer.title == "三甲医院康复科物理治疗师"
    assert made.proposer.system_message == _MANUAL_PROPOSER["system_message"].strip()
    # 另一位仍由模型生成
    assert made.challenger.name == "挑刺方"


def test_the_prompt_gets_the_manual_summary_not_the_whole_memory(purpose):
    """手写侧的完整背景可能有上万字，不该塞进人格提示词里。"""
    plan = parse_persona_plan(
        {
            "proposer": _MANUAL_PROPOSER,
            "challenger": {"mode": "hint", "hint": "一位带过很多程序员的实战派教练"},
        }
    )
    backend = RecordingAuthorBackend()
    forge_personas(purpose, backend=backend, plan=plan)

    prompt = backend.seen_prompts[0]
    assert "许医师" in prompt
    assert "三甲医院康复科物理治疗师" in prompt
    assert "什么是对的" in prompt
    assert "UNIQUE_MANUAL_MEMORY_MARKER_9f3c" not in prompt
    assert "一位带过很多程序员的实战派教练" in prompt


class OnlyChallengerBackend(ScriptedAuthorBackend):
    """人格请求只回一半：故意漏掉手写侧。"""

    def _reply(self, messages):
        text = " ".join(
            str(m.get("content", "")) for m in messages if isinstance(m, dict)
        )
        if '"proposer"' in text:
            self._calls += 1
            return json.dumps(
                {
                    "title": "只回了一半",
                    "slug": "half",
                    "challenger": {
                        "name": "乙",
                        "title": "身份乙",
                        "stance": "立场乙",
                        "system_message": "【人格】\n乙",
                    },
                },
                ensure_ascii=False,
            )
        return super()._reply(messages)


def test_a_missing_manual_side_in_the_model_output_is_fine(purpose):
    """模型不需要复述用户手写的那位；漏掉不该触发重试。"""
    plan = parse_persona_plan(
        {"proposer": _MANUAL_PROPOSER, "challenger": {"mode": "auto"}}
    )
    made = forge_personas(purpose, backend=OnlyChallengerBackend(), plan=plan)

    assert made.proposer.name == "许医师"
    assert made.challenger.name == "乙"
    assert made.title == "只回了一半"


class ExplodingBackend(ScriptedBackend):
    def _reply(self, messages):
        raise AssertionError("全部手写时不该调用人格模型")


def test_both_manual_makes_no_persona_call(purpose):
    plan = parse_persona_plan(
        {"proposer": _MANUAL_PROPOSER, "challenger": _MANUAL_CHALLENGER}
    )
    made = forge_personas(purpose, backend=ExplodingBackend(), plan=plan)

    assert made.proposer.name == "许医师"
    assert made.challenger.name == "老张"
    assert made.title == purpose.title  # 用户给了标题就用
    assert made.slug


def test_both_manual_without_a_title_falls_back(purpose):
    bare = Purpose(text=purpose.text, title="")
    plan = parse_persona_plan(
        {"proposer": _MANUAL_PROPOSER, "challenger": _MANUAL_CHALLENGER}
    )
    made = forge_personas(bare, backend=ExplodingBackend(), plan=plan)

    assert made.title == "对谈成果"
    assert made.slug == "duo"


def test_forge_with_both_manual_only_calls_the_topic_model(purpose):
    plan = parse_persona_plan(
        {"proposer": _MANUAL_PROPOSER, "challenger": _MANUAL_CHALLENGER}
    )
    backend = RecordingAuthorBackend()
    made = forge(purpose, backend=backend, plan=plan)

    assert backend._calls == 1, "人格由用户提供，只剩议题那一次调用"
    assert made.personas[0].name == "许医师"
    assert made.personas[1].name == "老张"


class DisciplineThenGoodBackend(ScriptedAuthorBackend):
    """第一次回带纪律标记的人格，第二次回干净的。"""

    def __init__(self) -> None:
        super().__init__()
        self._n = 0
        self.seen_prompts: list[str] = []

    def _reply(self, messages):
        self.seen_prompts.append(
            " ".join(
                str(m.get("content", "")) for m in messages if isinstance(m, dict)
            )
        )
        self._n += 1
        if self._n == 1:
            self._calls += 1
            bad = json.loads(json.dumps(_GOOD_PERSONAS, ensure_ascii=False))
            bad["proposer"]["system_message"] += "\n【发言纪律】\n不要写这一段。"
            return json.dumps(bad, ensure_ascii=False)
        return super()._reply(messages)


def test_generated_discipline_is_rejected_and_retried(purpose):
    backend = DisciplineThenGoodBackend()
    made = forge_personas(purpose, backend=backend)

    assert backend._n == 2
    assert "【发言纪律】" not in made.proposer.system_message
    assert "发言纪律" in backend.seen_prompts[1]  # 重试提示里说清了哪里错


# ---------------------------------------------------------------------------
# 三人及以上：先骨架，再一次性展开
# ---------------------------------------------------------------------------


def test_three_person_forge_uses_the_skeleton_and_expands(purpose, author):
    made = forge_personas(purpose, backend=author, plan=PersonaPlan(people=3))

    assert len(made.personas) == 3
    assert made.personas[0].role == "主提出方"
    assert len({p.role for p in made.personas}) == 3
    assert len({p.name for p in made.personas}) == 3


def test_five_person_forge_offline(purpose, author):
    made = forge_personas(purpose, backend=author, plan=PersonaPlan(people=5))

    assert len(made.personas) == 5
    assert len({p.role for p in made.personas}) == 5
    assert made.personas[0].role == "主提出方"


def test_multi_person_forge_calls_the_model_three_times(purpose):
    """骨架 + 展开 + 议题，正常三次。两人模式仍是两次。"""
    backend = RecordingAuthorBackend()
    made = forge(purpose, backend=backend, plan=PersonaPlan(people=3))

    assert backend._calls == 3
    assert made.people == 3


def test_team_hint_reaches_both_persona_prompts(purpose):
    plan = parse_persona_plan({"team": "要有一个医生、一个患者、一个管理者"}, people=3)
    backend = RecordingAuthorBackend()
    forge_personas(purpose, backend=backend, plan=plan)

    assert "一个医生、一个患者、一个管理者" in backend.seen_prompts[0]
    assert "一个医生、一个患者、一个管理者" in backend.seen_prompts[1]


def test_manual_slots_are_not_regenerated_in_multi_person(purpose):
    plan = parse_persona_plan(
        {"personas": [_MANUAL_PROPOSER, {}, {}]}, people=3
    )
    made = forge_personas(purpose, backend=ScriptedAuthorBackend(), plan=plan)

    assert made.personas[0].name == "许医师"
    assert made.personas[0].role == "主提出方"  # 手写侧没写 role，按位置补
    assert len(made.personas) == 3


def test_duplicate_roles_in_multi_person_are_rejected():
    raw = {
        "title": "标题",
        "slug": "x",
        "personas": [
            {"role": "专家", "name": "甲", "title": "身份", "stance": "立场",
             "system_message": "内容"},
            {"role": "专家", "name": "乙", "title": "身份", "stance": "立场",
             "system_message": "内容"},
            {"role": "丙", "name": "丙", "title": "身份", "stance": "立场",
             "system_message": "内容"},
        ],
    }
    with pytest.raises(AuthoringError) as excinfo:
        authoring._parse_team_personas(raw, plan=PersonaPlan(people=3))
    assert "角色名" in str(excinfo.value)


def test_skeleton_count_mismatch_is_rejected():
    raw = {
        "team": [
            {"role": "甲", "name": "甲", "title": "身份", "stance": "立场"},
            {"role": "乙", "name": "乙", "title": "身份", "stance": "立场"},
        ]
    }
    with pytest.raises(AuthoringError) as excinfo:
        authoring._parse_skeleton(raw, plan=PersonaPlan(people=3))
    assert "应该有 3 位" in str(excinfo.value)


def test_three_person_blueprint_has_three_roles(purpose, author):
    made = forge(purpose, backend=author, topic_count=3, plan=PersonaPlan(people=3))

    assert made.people == 3
    assert len(made.personas) == 3
    assert all(p.role for p in made.personas)


# ---------------------------------------------------------------------------
# 网页拖入的材料：先提炼成目的
# ---------------------------------------------------------------------------


def test_distill_material_offline():
    result = distill_material(
        "# 颈椎保养\n\n给刚上班、每天坐十小时的程序员写一份能照做的手册。",
        backend=ScriptedAuthorBackend(),
        files=["brief.md"],
    )

    assert result.title == "模拟材料对谈手册"
    assert result.text
    assert len(result.text) <= MAX_PURPOSE


def test_distill_material_rejects_empty():
    with pytest.raises(AuthoringError) as excinfo:
        distill_material("   ", backend=ScriptedAuthorBackend())
    assert "材料是空的" in str(excinfo.value)


def test_distill_material_rejects_too_long():
    with pytest.raises(AuthoringError) as excinfo:
        distill_material("长" * (MAX_MATERIAL + 1), backend=ScriptedAuthorBackend())
    assert "材料太长" in str(excinfo.value)


def test_distill_material_sends_files_and_material():
    backend = RecordingAuthorBackend()
    material = "# 材料标题\n\nUNIQUE_MATERIAL_MARKER_7c1e\n\n这是正文。"

    distill_material(material, backend=backend, files=["a.md", "b.md"])

    prompt = backend.seen_prompts[0]
    assert "a.md" in prompt
    assert "b.md" in prompt
    assert "UNIQUE_MATERIAL_MARKER_7c1e" in prompt


def test_distill_material_retries_on_a_bad_first_answer():
    good = {
        "title": "重试提炼的标题",
        "distilled_purpose": "一份足够长的目的，说明给谁、解决什么、想要什么成果。",
    }
    backend = FlakyBackend(good)

    result = distill_material("一些材料", backend=backend)

    assert result.title == "重试提炼的标题"
    assert backend._calls == 2
    assert "上一次的输出不能用了" in backend.seen_prompts[1]


def test_distill_parse_rejects_an_overlong_purpose():
    with pytest.raises(AuthoringError) as excinfo:
        authoring._parse_distilled(
            {"title": "标题", "distilled_purpose": "长" * (MAX_PURPOSE + 1)}
        )
    assert "太长" in str(excinfo.value)


def test_distill_parse_rejects_a_short_purpose():
    with pytest.raises(AuthoringError) as excinfo:
        authoring._parse_distilled(
            {"title": "标题", "distilled_purpose": "太短"}
        )
    assert "太短" in str(excinfo.value)


def test_distill_parse_rejects_a_multiline_title():
    with pytest.raises(AuthoringError) as excinfo:
        authoring._parse_distilled(
            {
                "title": "第一行\n第二行",
                "distilled_purpose": "一份足够长的目的，说明给谁、解决什么、想要什么成果。",
            }
        )
    assert "单行" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 目的 + 材料：用户给目的，材料只作参考
# ---------------------------------------------------------------------------


def test_distill_material_with_a_seed_uses_the_users_title():
    seed = Purpose(text="给刚上班、每天坐十小时的程序员写一份能照做的手册。", title="我的颈椎手册")

    result = distill_material(
        "# 参考资料\n\n一些和颈椎有关的背景。",
        backend=ScriptedAuthorBackend(),
        seed=seed,
    )

    assert result.title == "我的颈椎手册"
    assert "具体化" in result.text


def test_distill_material_with_a_seed_sends_both_purpose_and_material():
    backend = RecordingAuthorBackend()
    seed = Purpose(text="UNIQUE_SEED_PURPOSE_9f2a：给程序员写一份能照做的手册。")

    distill_material(
        "UNIQUE_MATERIAL_MARKER_7c1e",
        backend=backend,
        files=["ref.md"],
        seed=seed,
    )

    prompt = backend.seen_prompts[0]
    assert "UNIQUE_SEED_PURPOSE_9f2a" in prompt
    assert "UNIQUE_MATERIAL_MARKER_7c1e" in prompt
    assert "ref.md" in prompt
    assert "目的具体化师" in prompt


def test_distill_material_with_a_seed_keeps_the_model_title_when_the_user_did_not_give_one():
    result = distill_material(
        "一些参考材料",
        backend=ScriptedAuthorBackend(),
        seed=Purpose(text="给刚上班、每天坐十小时的程序员写一份能照做的手册。"),
    )

    assert result.title == "模拟目的细化手册"


def test_distill_material_rejects_an_empty_seed():
    with pytest.raises(AuthoringError) as excinfo:
        distill_material(
            "一些材料",
            backend=ScriptedAuthorBackend(),
            seed=Purpose(text="   "),
        )
    assert "目的为空" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 只重生成一部分：议题 / 人格
# ---------------------------------------------------------------------------


def test_reforge_topics_keeps_purpose_title_personas(blueprint, monkeypatch):
    calls: dict[str, int] = {}
    real = authoring.forge_topics

    def spy(purpose, personas, *, backend, count, hint=""):
        calls["count"] = count
        return real(purpose, personas, backend=backend, count=count)

    monkeypatch.setattr(authoring, "forge_topics", spy)

    made = authoring.reforge_blueprint(
        blueprint,
        backend=ScriptedAuthorBackend(),
        what="topics",
        origin="mock",
    )

    assert calls["count"] == len(blueprint.topics)
    assert made.purpose == blueprint.purpose
    assert made.title == blueprint.title
    assert made.slug == blueprint.slug
    assert made.personas == blueprint.personas
    assert made.topics
    assert made.origin == "mock"
    # 重新生成的议题不是原来那批
    assert {t.key for t in made.topics} != {t.key for t in blueprint.topics}


def test_reforge_personas_also_regenerates_topics(blueprint, monkeypatch):
    calls = {"personas": 0, "topics": 0}
    real_personas = authoring.forge_personas
    real_topics = authoring.forge_topics

    def spy_personas(purpose, *, backend, plan=None, extra_hint=""):
        calls["personas"] += 1
        return real_personas(purpose, backend=backend, plan=plan)

    def spy_topics(purpose, personas, *, backend, count, hint=""):
        calls["topics"] += 1
        return real_topics(purpose, personas, backend=backend, count=count)

    monkeypatch.setattr(authoring, "forge_personas", spy_personas)
    monkeypatch.setattr(authoring, "forge_topics", spy_topics)

    made = authoring.reforge_blueprint(
        blueprint,
        backend=ScriptedAuthorBackend(),
        what="personas",
    )

    assert calls == {"personas": 1, "topics": 1}
    assert made.purpose == blueprint.purpose
    assert made.title == blueprint.title
    assert made.slug == blueprint.slug
    assert made.topics


def test_reforge_personas_can_use_a_manual_plan(blueprint):
    plan = parse_persona_plan(
        {
            "personas": [
                _MANUAL_PROPOSER,
                {"mode": "hint", "hint": "一位带过很多程序员的实战派教练"},
            ]
        }
    )
    made = authoring.reforge_blueprint(
        blueprint,
        backend=ScriptedAuthorBackend(),
        what="personas",
        plan=plan,
    )

    assert made.personas[0].name == "许医师"
    assert made.personas[1].name == "挑刺方"


def test_reforge_rejects_an_unknown_target(blueprint):
    with pytest.raises(AuthoringError) as excinfo:
        authoring.reforge_blueprint(
            blueprint, backend=ScriptedAuthorBackend(), what="purpose"
        )
    assert "topics 或 personas" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 重生成时的自定义要求
# ---------------------------------------------------------------------------


def test_forge_topics_sends_the_reforge_hint(purpose, personas):
    backend = RecordingAuthorBackend()

    forge_topics(
        purpose,
        personas,
        backend=backend,
        count=2,
        hint="UNIQUE_TOPIC_HINT_5a3f：议题更偏执行、少谈理论。",
    )

    prompt = backend.seen_prompts[0]
    assert "UNIQUE_TOPIC_HINT_5a3f" in prompt
    assert "本次对议题的额外要求" in prompt


def test_forge_topics_without_a_hint_keeps_the_prompt_clean(purpose, personas):
    backend = RecordingAuthorBackend()

    forge_topics(purpose, personas, backend=backend, count=2)

    assert "本次对议题的额外要求" not in backend.seen_prompts[0]


def test_forge_personas_sends_the_reforge_hint(purpose):
    backend = RecordingAuthorBackend()

    forge_personas(
        purpose,
        backend=backend,
        extra_hint="UNIQUE_PERSONA_HINT_7b21：换一位成本把关人。",
    )

    prompt = backend.seen_prompts[0]
    assert "UNIQUE_PERSONA_HINT_7b21" in prompt
    assert "本次重生成要求" in prompt


def test_reforge_topics_passes_the_hint(blueprint, monkeypatch):
    seen: dict[str, str] = {}
    real = authoring.forge_topics

    def spy(purpose, personas, *, backend, count, hint=""):
        seen["hint"] = hint
        return real(purpose, personas, backend=backend, count=count)

    monkeypatch.setattr(authoring, "forge_topics", spy)

    authoring.reforge_blueprint(
        blueprint,
        backend=ScriptedAuthorBackend(),
        what="topics",
        topic_hint="议题更偏执行",
    )

    assert seen["hint"] == "议题更偏执行"


def test_reforge_personas_passes_the_hint(blueprint, monkeypatch):
    seen: dict[str, str] = {}
    real = authoring.forge_personas

    def spy(purpose, *, backend, plan=None, extra_hint=""):
        seen["hint"] = extra_hint
        return real(purpose, backend=backend, plan=plan)

    monkeypatch.setattr(authoring, "forge_personas", spy)

    authoring.reforge_blueprint(
        blueprint,
        backend=ScriptedAuthorBackend(),
        what="personas",
        persona_hint="换一位成本把关人",
    )

    assert seen["hint"] == "换一位成本把关人"


def test_reforge_rejects_an_overlong_hint(blueprint):
    with pytest.raises(AuthoringError) as excinfo:
        authoring.reforge_blueprint(
            blueprint,
            backend=ScriptedAuthorBackend(),
            what="topics",
            topic_hint="长" * (MAX_REFORGE_HINT + 1),
        )
    assert "重生成要求太长" in str(excinfo.value)
