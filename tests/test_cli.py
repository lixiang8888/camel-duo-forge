"""命令行：三个子命令跑得通，参数给错时报的是人话。

全程 --mock，不联网、不需要 API key。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from duo_forge import blueprint as blueprint_mod
from duo_forge.cli import main
from duo_forge.topics import MAX_REFORGE_HINT

PURPOSE = "给刚上班、每天坐十小时的程序员写一份颈椎保养手册，要能真的照做。"


def _forge(tmp_path, *extra: str):
    return main(["forge", "--purpose", PURPOSE, "--mock", "--out", str(tmp_path), *extra])


# ---------------------------------------------------------------------------
# forge：功能一 + 功能二
# ---------------------------------------------------------------------------


def test_forge_writes_a_blueprint_json_and_a_readable_copy(tmp_path, capsys):
    assert _forge(tmp_path) == 0

    files = sorted(p.name for p in tmp_path.iterdir())
    assert len(files) == 2
    assert files[0].endswith(".json") and files[1].endswith(".md")

    made = blueprint_mod.load(tmp_path / files[0])
    assert made.origin == "mock"
    assert len(made.personas) == 2
    assert len(made.topics) >= 2


def test_forge_reports_the_result_and_the_next_step(tmp_path, capsys):
    _forge(tmp_path)
    out = capsys.readouterr().out

    assert "两位专家：" in out
    assert "议题（" in out
    for key in ("mock-baseline", "mock-reality"):
        assert key in out
    # 明确告诉人下一步该敲什么，别让人去翻 README
    assert "duo-forge run --blueprint" in out
    assert str(tmp_path) in out


def test_forge_says_out_loud_that_mock_is_worthless(tmp_path, capsys):
    _forge(tmp_path)
    assert "模拟模式" in capsys.readouterr().out


def test_forge_warns_on_the_readable_copy(tmp_path):
    _forge(tmp_path)
    markdown = next(tmp_path.glob("*.md")).read_text(encoding="utf-8")
    assert "mock" in markdown  # 来源标注
    assert "【发言纪律】" in markdown  # 完整的提示词都摊开了


def test_forge_honours_an_explicit_title(tmp_path):
    _forge(tmp_path, "--title", "我的颈椎手册")
    made = blueprint_mod.load(next(tmp_path.glob("*.json")))
    assert made.title == "我的颈椎手册"


def test_forge_accepts_a_purpose_file(tmp_path):
    path = tmp_path / "purpose.md"
    path.write_text(
        "# 颈椎手册\n\n给刚上班、每天坐十小时的程序员写一份保养手册。", encoding="utf-8"
    )
    assert (
        main(
            [
                "forge", "--purpose-file", str(path), "--mock",
                "--out", str(tmp_path),
            ]
        )
        == 0
    )
    assert blueprint_mod.load(next(tmp_path.glob("*.json"))).title == "颈椎手册"


# ---------------------------------------------------------------------------
# 参数给错时
# ---------------------------------------------------------------------------


def test_purpose_and_purpose_file_are_exclusive(tmp_path, capsys):
    path = tmp_path / "p.md"
    path.write_text(PURPOSE, encoding="utf-8")
    code = main(
        ["forge", "--purpose", PURPOSE, "--purpose-file", str(path), "--mock"]
    )
    assert code == 2
    assert "只能给一个" in capsys.readouterr().err


def test_no_purpose_at_all(tmp_path, capsys):
    assert main(["forge", "--mock", "--out", str(tmp_path)]) == 2
    assert "没有写目的" in capsys.readouterr().err


def test_a_too_short_purpose_is_refused(tmp_path, capsys):
    assert main(["forge", "--purpose", "写点", "--mock", "--out", str(tmp_path)]) == 2
    assert "太短" in capsys.readouterr().err


def test_a_missing_purpose_file(tmp_path, capsys):
    code = main(["forge", "--purpose-file", str(tmp_path / "nope.txt"), "--mock"])
    assert code == 2
    assert "读不出" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_from_a_blueprint(tmp_path, capsys):
    _forge(tmp_path)
    bp_path = next(tmp_path.glob("*.json"))
    out = tmp_path / "out"

    assert (
        main(["run", "--blueprint", str(bp_path), "--mock", "--rounds", "2",
              "--out", str(out)])
        == 0
    )

    produced = sorted(p.name for p in out.iterdir())
    assert len(produced) == 3
    assert any(name.startswith("handbook-") for name in produced)
    assert any(name.startswith("transcript-") for name in produced)
    assert any(name.startswith("quick-") for name in produced)
    assert "成果文件：" in capsys.readouterr().out


def test_run_without_anything_is_refused(capsys):
    assert main(["run", "--mock"]) == 2
    assert "没有写目的" in capsys.readouterr().err


def test_run_with_both_a_blueprint_and_a_purpose_is_refused(tmp_path, capsys):
    _forge(tmp_path)
    bp_path = next(tmp_path.glob("*.json"))
    code = main(
        ["run", "--blueprint", str(bp_path), "--purpose", PURPOSE, "--mock"]
    )
    assert code == 2
    assert "只能给一个" in capsys.readouterr().err


def test_run_with_an_unknown_topic_key(tmp_path, capsys):
    _forge(tmp_path)
    bp_path = next(tmp_path.glob("*.json"))
    code = main(
        ["run", "--blueprint", str(bp_path), "--only", "nope", "--mock",
         "--out", str(tmp_path / "out")]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "nope" in err
    assert "mock-baseline" in err  # 把可选的列出来


def test_run_from_a_purpose_saves_the_blueprint_it_used(tmp_path, capsys):
    """现生成的那份蓝图要存下来，否则想复现或想改一个议题重跑，都得再花两次调用。"""
    out = tmp_path / "out"
    assert (
        main(["run", "--purpose", PURPOSE, "--mock", "--rounds", "1",
              "--out", str(out)])
        == 0
    )
    assert list(out.glob("*.json")), "蓝图没落盘"
    assert "蓝图已存" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 自定义人格
# ---------------------------------------------------------------------------

_MANUAL_PERSONAS = {
    "proposer": {
        "mode": "manual",
        "name": "许医师",
        "title": "三甲医院康复科物理治疗师",
        "stance": "什么是对的",
        "system_message": (
            "【人格】\n只认证据。\n\n【专业立场】\n先评估再干预。\n\n"
            "【你的盲区，不要掩饰】\n方案偏理想。\n\n"
            "【长期背景与案例】\n看过的典型病例都在这里。"
        ),
    },
    "challenger": {
        "mode": "hint",
        "hint": "一位带过很多程序员的实战派教练",
    },
}


def _write_personas(tmp_path, data) -> Path:
    path = tmp_path / "personas.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_forge_accepts_a_personas_file(tmp_path):
    out = tmp_path / "out"
    path = _write_personas(tmp_path, _MANUAL_PERSONAS)

    assert (
        main(
            [
                "forge", "--purpose", PURPOSE, "--mock",
                "--out", str(out), "--personas-file", str(path),
            ]
        )
        == 0
    )

    made = blueprint_mod.load(next(out.glob("*.json")))
    assert made.personas[0].name == "许医师"
    assert "【长期背景与案例】" in made.personas[0].system_message
    # 另一位是 hint，由 mock 后端生成
    assert made.personas[1].name == "挑刺方"


def test_forge_reports_a_missing_personas_file(tmp_path, capsys):
    code = main(
        [
            "forge", "--purpose", PURPOSE, "--mock",
            "--personas-file", str(tmp_path / "nope.json"),
        ]
    )
    assert code == 2
    assert "读不出人格文件" in capsys.readouterr().err


def test_forge_rejects_an_invalid_personas_file_without_writing(tmp_path, capsys):
    out = tmp_path / "out"
    path = _write_personas(
        tmp_path, {"proposer": {"mode": "manual", "name": "只有名字"}}
    )

    code = main(
        [
            "forge", "--purpose", PURPOSE, "--mock",
            "--out", str(out), "--personas-file", str(path),
        ]
    )
    assert code == 2
    assert "第一位专家" in capsys.readouterr().err
    assert not out.exists() or not list(out.glob("*"))


def test_run_from_a_purpose_accepts_a_personas_file(tmp_path):
    out = tmp_path / "out"
    path = _write_personas(
        tmp_path,
        {
            "proposer": _MANUAL_PERSONAS["proposer"],
            "challenger": _MANUAL_PERSONAS["proposer"] | {"name": "老张"},
        },
    )

    assert (
        main(
            [
                "run", "--purpose", PURPOSE, "--mock", "--rounds", "1",
                "--out", str(out), "--personas-file", str(path),
            ]
        )
        == 0
    )
    made = blueprint_mod.load(next(out.glob("*.json")))
    assert made.personas[0].name == "许医师"
    assert made.personas[1].name == "老张"


def test_run_with_blueprint_and_personas_file_is_refused(tmp_path, capsys):
    _forge(tmp_path)
    bp_path = next(tmp_path.glob("*.json"))
    path = _write_personas(tmp_path, {})

    code = main(
        [
            "run", "--blueprint", str(bp_path),
            "--personas-file", str(path), "--mock",
        ]
    )
    assert code == 2
    assert "--personas-file" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_prints_the_readable_version(tmp_path, capsys):
    _forge(tmp_path)
    capsys.readouterr()  # 把 forge 的输出清掉，下面只看 show 自己印了什么
    bp_path = next(tmp_path.glob("*.json"))

    assert main(["show", "--blueprint", str(bp_path)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# 蓝图 · ")
    assert "## 两位专家" in out
    assert "## 议题" in out


def test_show_json_round_trips(tmp_path, capsys):
    """--json 打出来的必须能直接喂回 from_dict。"""
    _forge(tmp_path)
    capsys.readouterr()
    bp_path = next(tmp_path.glob("*.json"))

    assert main(["show", "--blueprint", str(bp_path), "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == json.loads(bp_path.read_text(encoding="utf-8"))


def test_show_reports_a_broken_blueprint(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("{不是 JSON", encoding="utf-8")
    assert main(["show", "--blueprint", str(path)]) == 2
    assert "不是合法 JSON" in capsys.readouterr().err


# ---------------------------------------------------------------------------


def test_no_subcommand_prints_help(capsys):
    assert main([]) == 1
    assert "forge" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 多人
# ---------------------------------------------------------------------------


def test_forge_with_three_people(tmp_path, capsys):
    assert _forge(tmp_path, "--people", "3") == 0

    made = blueprint_mod.load(next(tmp_path.glob("*.json")))
    assert made.people == 3
    assert len(made.personas) == 3
    out = capsys.readouterr().out
    assert "对话人数：3 位" in out
    assert "3 位专家" in out
    assert "3 次模型调用" in out


def test_forge_with_five_people(tmp_path):
    assert _forge(tmp_path, "--people", "5") == 0
    made = blueprint_mod.load(next(tmp_path.glob("*.json")))
    assert made.people == 5
    assert len({p.role for p in made.personas}) == 5


def test_forge_rejects_an_out_of_range_people(tmp_path, capsys):
    assert _forge(tmp_path, "--people", "6") == 2
    assert "2–5" in capsys.readouterr().err


def test_forge_accepts_a_team_hint(tmp_path):
    assert (
        _forge(tmp_path, "--people", "3", "--team-hint", "要有一个医生、一个患者、一个管理者")
        == 0
    )
    made = blueprint_mod.load(next(tmp_path.glob("*.json")))
    assert made.people == 3


def test_load_persona_plan_applies_the_team_hint():
    from duo_forge.cli import _load_persona_plan

    plan = _load_persona_plan(None, people=3, team_hint="  要有一个医生  ")
    assert plan.people == 3
    assert plan.team_hint == "要有一个医生"


def test_forge_accepts_a_personas_array(tmp_path):
    out = tmp_path / "out"
    path = _write_personas(
        tmp_path,
        {
            "personas": [
                _MANUAL_PERSONAS["proposer"],
                {"mode": "hint", "hint": "一位带过很多程序员的实战派教练"},
                {},
            ]
        },
    )
    assert (
        main(
            [
                "forge", "--purpose", PURPOSE, "--mock",
                "--out", str(out), "--personas-file", str(path),
            ]
        )
        == 0
    )
    made = blueprint_mod.load(next(out.glob("*.json")))
    assert made.people == 3
    assert made.personas[0].name == "许医师"
    assert made.personas[1].name == "挑刺方"


def test_a_personas_array_can_be_shorter_than_people(tmp_path):
    out = tmp_path / "out"
    path = _write_personas(
        tmp_path, {"personas": [_MANUAL_PERSONAS["proposer"]]}
    )
    assert (
        main(
            [
                "forge", "--purpose", PURPOSE, "--mock",
                "--out", str(out), "--personas-file", str(path),
                "--people", "4",
            ]
        )
        == 0
    )
    made = blueprint_mod.load(next(out.glob("*.json")))
    assert made.people == 4
    assert made.personas[0].name == "许医师"
    assert made.personas[1].name == "挑刺方"


def test_run_from_a_purpose_with_three_people(tmp_path):
    out = tmp_path / "out"
    assert (
        main(
            [
                "run", "--purpose", PURPOSE, "--mock", "--rounds", "1",
                "--people", "3", "--out", str(out),
            ]
        )
        == 0
    )
    made = blueprint_mod.load(next(out.glob("*.json")))
    assert made.people == 3


def test_run_with_blueprint_and_people_is_refused(tmp_path, capsys):
    _forge(tmp_path)
    bp_path = next(tmp_path.glob("*.json"))
    code = main(
        ["run", "--blueprint", str(bp_path), "--people", "3", "--mock"]
    )
    assert code == 2
    assert "--people" in capsys.readouterr().err


def test_run_with_blueprint_and_team_hint_is_refused(tmp_path, capsys):
    _forge(tmp_path)
    bp_path = next(tmp_path.glob("*.json"))
    code = main(
        ["run", "--blueprint", str(bp_path), "--team-hint", "要有一个医生", "--mock"]
    )
    assert code == 2
    assert "--team-hint" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# followup：读完成果后继续追问
# ---------------------------------------------------------------------------


def _run_for_handbook(tmp_path):
    """跑一轮最小的 mock run，返回 (蓝图路径, 成果目录)。"""
    _forge(tmp_path)
    bp_path = next(tmp_path.glob("*.json"))
    out = tmp_path / "out"
    assert (
        main(
            [
                "run", "--blueprint", str(bp_path), "--mock", "--rounds", "1",
                "--out", str(out),
            ]
        )
        == 0
    )
    return bp_path, out


def test_followup_appends_a_section_and_can_repeat(tmp_path, capsys):
    bp_path, out = _run_for_handbook(tmp_path)
    capsys.readouterr()

    handbook = next(out.glob("handbook-*.md"))
    transcript = next(out.glob("transcript-*.md"))
    quick = next(out.glob("quick-*.md"))

    assert (
        main(
            [
                "followup", "--blueprint", str(bp_path), "--handbook", str(handbook),
                "--question", "连续开会时做不到怎么办？", "--rounds", "1", "--mock",
            ]
        )
        == 0
    )

    text = handbook.read_text(encoding="utf-8")
    assert "## 追加讨论 1：" in text
    assert "连续开会时做不到怎么办？" in text
    assert "追加讨论 1" in transcript.read_text(encoding="utf-8")
    assert "模拟模式生成的占位速查" in quick.read_text(encoding="utf-8")
    out_text = capsys.readouterr().out
    assert "已追加第 1 节" in out_text
    assert "成果文件：" in out_text

    # 再来一次：编号递增；自己给的标题照用
    assert (
        main(
            [
                "followup", "--blueprint", str(bp_path), "--handbook", str(handbook),
                "--question", "那只能坐着做两分钟呢？", "--title", "开会时怎么做",
                "--rounds", "1", "--mock",
            ]
        )
        == 0
    )
    text = handbook.read_text(encoding="utf-8")
    assert "## 追加讨论 1：" in text
    assert "## 追加讨论 2：开会时怎么做" in text


def test_followup_refuses_a_missing_handbook(tmp_path, capsys):
    _forge(tmp_path)
    bp_path = next(tmp_path.glob("*.json"))

    code = main(
        [
            "followup", "--blueprint", str(bp_path),
            "--handbook", str(tmp_path / "nope.md"),
            "--question", "为什么？", "--mock",
        ]
    )
    assert code == 2
    assert "读不出成果手册" in capsys.readouterr().err


def test_followup_refuses_a_blank_question(tmp_path, capsys):
    bp_path, out = _run_for_handbook(tmp_path)
    handbook = next(out.glob("handbook-*.md"))

    code = main(
        [
            "followup", "--blueprint", str(bp_path), "--handbook", str(handbook),
            "--question", "  ", "--mock",
        ]
    )
    assert code == 2
    assert "追问" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# reforge：只重生成议题或人格
# ---------------------------------------------------------------------------


def test_reforge_topics_keeps_the_purpose_title_and_personas(tmp_path, capsys):
    _forge(tmp_path)
    capsys.readouterr()
    bp_path = next(tmp_path.glob("*.json"))
    before = blueprint_mod.load(bp_path)
    out = tmp_path / "reforged"

    assert (
        main(
            [
                "reforge", "--blueprint", str(bp_path), "--what", "topics",
                "--mock", "--out", str(out),
            ]
        )
        == 0
    )

    made = blueprint_mod.load(next(out.glob("*.json")))
    assert made.title == before.title
    assert made.purpose == before.purpose
    assert [p.name for p in made.personas] == [p.name for p in before.personas]
    assert made.topics
    assert made.origin == "mock"

    printed = capsys.readouterr().out
    assert "重新生成：议题" in printed
    assert "duo-forge run --blueprint" in printed


def test_reforge_personas_keeps_the_title_and_can_change_people(tmp_path):
    _forge(tmp_path)
    bp_path = next(tmp_path.glob("*.json"))
    before = blueprint_mod.load(bp_path)
    out = tmp_path / "reforged"

    assert (
        main(
            [
                "reforge", "--blueprint", str(bp_path), "--what", "personas",
                "--people", "3", "--mock", "--out", str(out),
            ]
        )
        == 0
    )

    made = blueprint_mod.load(next(out.glob("*.json")))
    assert made.people == 3
    assert made.title == before.title
    assert made.topics


def test_reforge_personas_uses_a_personas_file(tmp_path):
    _forge(tmp_path)
    bp_path = next(tmp_path.glob("*.json"))
    path = _write_personas(tmp_path, {"personas": [_MANUAL_PERSONAS["proposer"]]})
    out = tmp_path / "reforged"

    assert (
        main(
            [
                "reforge", "--blueprint", str(bp_path), "--what", "personas",
                "--personas-file", str(path), "--mock", "--out", str(out),
            ]
        )
        == 0
    )

    made = blueprint_mod.load(next(out.glob("*.json")))
    assert made.personas[0].name == "许医师"


def test_reforge_topics_refuses_persona_args(tmp_path, capsys):
    _forge(tmp_path)
    bp_path = next(tmp_path.glob("*.json"))

    code = main(
        [
            "reforge", "--blueprint", str(bp_path), "--what", "topics",
            "--people", "3", "--mock",
        ]
    )
    assert code == 2
    assert "--people" in capsys.readouterr().err


def test_reforge_with_a_missing_blueprint_is_refused(tmp_path, capsys):
    code = main(
        [
            "reforge", "--blueprint", str(tmp_path / "nope.json"),
            "--what", "topics", "--mock",
        ]
    )
    assert code == 2
    assert "读不出蓝图" in capsys.readouterr().err


def test_reforge_passes_the_hint_to_the_target(tmp_path, monkeypatch):
    from duo_forge import authoring

    captured: dict[str, object] = {}
    real = authoring.reforge_blueprint

    def spy(base, **kwargs):
        captured.update(kwargs)
        return real(base, **kwargs)

    monkeypatch.setattr(authoring, "reforge_blueprint", spy)
    _forge(tmp_path)
    bp_path = next(tmp_path.glob("*.json"))
    out = tmp_path / "reforged"

    assert (
        main(
            [
                "reforge", "--blueprint", str(bp_path), "--what", "topics",
                "--hint", "议题更偏执行", "--mock", "--out", str(out),
            ]
        )
        == 0
    )

    assert captured["topic_hint"] == "议题更偏执行"
    assert captured["persona_hint"] == ""


def test_reforge_rejects_an_overlong_hint(tmp_path, capsys):
    _forge(tmp_path)
    bp_path = next(tmp_path.glob("*.json"))

    code = main(
        [
            "reforge", "--blueprint", str(bp_path), "--what", "topics",
            "--hint", "长" * (MAX_REFORGE_HINT + 1), "--mock",
        ]
    )
    assert code == 2
    assert "重生成要求" in capsys.readouterr().err
