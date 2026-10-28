"""网页启动器：广播、HTTP 路由、两段式准入、keep-alive。

全部离线：forge 用 ScriptedAuthorBackend，run 用 ScriptedBackend，都不联网、不花钱。
不测 SSE 端到端（/events 故意永不关闭），只测 _Bus；也不靠 sleep 等后台任务，
一律用 queue.get(timeout=...) 等具体事件。
"""

from __future__ import annotations

import http.client
import json
import queue
import re
import threading
import urllib.error
import urllib.request

import pytest

import launcher


# ---------------------------------------------------------------------------
# 帮助函数
# ---------------------------------------------------------------------------


def _get(port: int, path: str) -> str:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}{path}", timeout=5
    ) as resp:
        return resp.read().decode("utf-8")


def _get_json(port: int, path: str) -> dict:
    return json.loads(_get(port, path))


def _get_status(port: int, path: str) -> int:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=5
        ) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _post(port: int, path: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_for(q: queue.Queue, kind: str, timeout: float = 15.0) -> dict:
    import time

    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise AssertionError(f"等不到 {kind} 事件")
        event = q.get(timeout=remaining)
        if event.get("type") == kind:
            return event


@pytest.fixture
def live_server(tmp_path):
    server = launcher._Server(
        ("127.0.0.1", 0),
        launcher._Handler,
        blueprint_dir=tmp_path / "blueprints",
        out_dir=tmp_path / "outputs",
    )
    port = server.server_address[1]
    # 默认 poll_interval 是 0.5s，shutdown() 每个测试都要等它醒来；降到 50ms。
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    thread.start()
    try:
        yield server, port
    finally:
        job = server.job
        if job is not None:
            job.request_stop()
            job.finished.wait(2)
        server.shutdown()
        server.server_close()


@pytest.fixture
def forged(live_server):
    """已 forge 出一份 mock 蓝图的服务。返回 (server, port, blueprint payload)。"""
    server, port = live_server
    q = server.bus.subscribe()
    res = _post(
        port,
        "/forge",
        {"purpose": "给刚上班的程序员写一份颈椎保养手册，要能真的照做。", "topics": 3, "mock": True},
    )
    assert res["ok"], res
    event = _wait_for(q, "forge_done")
    assert _wait_for(q, "done")
    # done 是 publish 出来的，finished.set() 紧随其后；不等它的话，紧接着的
    # /run 可能被准入检查误判成「还有任务在跑」。
    assert server.job.finished.wait(2)
    return server, port, event["blueprint"]


class _NeverFinishes:
    kind = "forge"

    def __init__(self) -> None:
        self.finished = threading.Event()
        self.stopped = threading.Event()
        self.cancelled = threading.Event()

    def request_stop(self) -> None:
        self.stopped.set()
        self.cancelled.set()


# ---------------------------------------------------------------------------
# 广播
# ---------------------------------------------------------------------------


def test_bus_publish_reaches_subscriber_with_increasing_seq():
    bus = launcher._Bus()
    q = bus.subscribe()
    bus.publish({"type": "a"})
    bus.publish({"type": "b"})

    first = q.get(timeout=1)
    second = q.get(timeout=1)
    assert [first["type"], second["type"]] == ["a", "b"]
    assert second["seq"] > first["seq"]


def test_bus_replays_history_to_late_subscriber():
    bus = launcher._Bus()
    bus.publish({"type": "a"})
    q = bus.subscribe()
    assert q.get(timeout=1)["type"] == "a"


def test_bus_reset_clears_history_but_keeps_seq():
    bus = launcher._Bus()
    bus.publish({"type": "a"})
    bus.reset()
    bus.publish({"type": "b"})

    q = bus.subscribe()
    event = q.get(timeout=1)
    assert event["type"] == "b"
    assert event["seq"] > 1  # seq 单调递增、不随 reset 归零


def test_bus_unsubscribe_stops_delivery():
    bus = launcher._Bus()
    q = bus.subscribe()
    bus.unsubscribe(q)
    bus.publish({"type": "a"})
    with pytest.raises(queue.Empty):
        q.get(timeout=0.05)


# ---------------------------------------------------------------------------
# 页面与健康检查
# ---------------------------------------------------------------------------


def test_health_reports_app_id(live_server):
    _, port = live_server
    assert _get_json(port, "/health") == {
        "app": launcher._APP_ID,
        "running": False,
        "stage": "idle",
    }


def test_page_has_boot_injected(live_server):
    _, port = live_server
    html = _get(port, "/")
    assert "__BOOT__" not in html
    match = re.search(r"const BOOT = (.*?);\n", html)
    assert match, "页面里找不到注入的 BOOT"
    boot = json.loads(match.group(1))
    assert boot["closing"]["key"] == "closing"
    assert boot["maxTopics"] >= 1


def test_page_boot_reflects_mock_flag(tmp_path):
    server = launcher._Server(
        ("127.0.0.1", 0), launcher._Handler, mock_default=True,
        blueprint_dir=tmp_path / "b", out_dir=tmp_path / "o",
    )
    try:
        html = launcher._build_page(server).decode("utf-8")
    finally:
        server.server_close()
    boot = json.loads(re.search(r"const BOOT = (.*?);\n", html).group(1))
    assert boot["mockDefault"] is True


def test_health_reports_running_stage(live_server):
    server, port = live_server
    server.job = _NeverFinishes()
    health = _get_json(port, "/health")
    assert health["running"] is True
    assert health["stage"] == "forge"


# ---------------------------------------------------------------------------
# forge
# ---------------------------------------------------------------------------


def test_forge_with_mock_creates_and_exposes_a_blueprint(live_server):
    server, port = live_server
    q = server.bus.subscribe()
    res = _post(
        port,
        "/forge",
        {"purpose": "给刚上班的程序员写一份颈椎保养手册，要能真的照做。", "topics": 3, "mock": True},
    )
    assert res["ok"], res

    event = _wait_for(q, "forge_done")
    bp = event["blueprint"]
    assert bp["ready"] is True
    assert bp["origin"] == "mock"
    assert bp["topics"], "mock 后端也该产出至少一个议题"
    assert bp["personas"][0]["system_message"]
    assert bp["personas"][1]["system_message"]

    # 结构化 payload 是页面勾选议题的依据
    payload = _get_json(port, "/blueprint")
    assert payload["ready"] is True
    assert [t["key"] for t in payload["topics"]] == [t["key"] for t in bp["topics"]]
    assert payload["closing"]["key"] == "closing"

    # 落盘：JSON + 给人看的那份
    files = list(server.blueprint_dir.glob("*"))
    assert any(p.suffix == ".json" for p in files)
    assert any(p.suffix == ".md" for p in files)

    # 两种下载都从内存出，不依赖文件还在不在
    assert _get(port, "/blueprint.json").startswith("{")
    assert "# 蓝图" in _get(port, "/blueprint.md")
    assert _wait_for(q, "done")


def test_forge_rejects_an_invalid_purpose_without_touching_the_previous_blueprint(
    forged,
):
    server, port, _ = forged
    before = _get_json(port, "/blueprint")
    history = list(server.bus._history)

    res = _post(port, "/forge", {"purpose": "太短", "mock": True})
    assert res["ok"] is False
    assert "太短" in res["why"] or "至少" in res["why"]

    assert _get_json(port, "/blueprint")["title"] == before["title"]
    assert server.bus._history == history


def test_a_second_forge_is_rejected_while_a_job_is_running(live_server):
    server, port = live_server
    server.job = _NeverFinishes()
    res = _post(
        port,
        "/forge",
        {"purpose": "给刚上班的程序员写一份颈椎保养手册，要能真的照做。", "mock": True},
    )
    assert res["ok"] is False
    assert "已经有一轮" in res["why"]


# ---------------------------------------------------------------------------
# run：准入与议题选择
# ---------------------------------------------------------------------------


def test_run_without_a_blueprint_is_rejected(live_server):
    server, port = live_server
    res = _post(port, "/run", {"keys": ["anything"], "mock": True})
    assert res["ok"] is False
    assert "还没有蓝图" in res["why"]
    assert server.bus._history == []


def test_run_with_empty_selection_is_rejected(forged):
    _, port, _ = forged
    res = _post(port, "/run", {"keys": [], "mock": True})
    assert res["ok"] is False
    assert "至少" in res["why"]


def test_run_rejects_an_unknown_topic_and_lists_the_valid_ones(forged):
    _, port, bp = forged
    res = _post(port, "/run", {"keys": ["nope"], "mock": True})
    assert res["ok"] is False
    assert "nope" in res["why"]
    assert bp["topics"][0]["key"] in res["why"]


def test_run_dedupes_and_strips_the_closing_key(forged):
    """前端会把固定勾选的收尾议题也带上，它不该被当成正文议题。"""
    server, port, bp = forged
    first = bp["topics"][0]["key"]
    q = server.bus.subscribe()

    res = _post(
        port,
        "/run",
        {"keys": [first, "closing", first, "  "], "rounds": 1, "mock": True},
    )
    assert res["ok"], res
    assert res["topics"] == [first, "closing"]

    start = _wait_for(q, "run_start")
    assert [t["key"] for t in start["topics"]] == [first, "closing"]
    assert _wait_for(q, "done")


def test_rounds_are_clamped(forged):
    server, port, bp = forged
    first = bp["topics"][0]["key"]

    q = server.bus.subscribe()
    res = _post(port, "/run", {"keys": [first], "rounds": 99, "mock": True})
    assert res["ok"], res
    assert res["rounds"] == launcher.MAX_ROUNDS
    assert _wait_for(q, "run_start")["rounds"] == launcher.MAX_ROUNDS
    assert _wait_for(q, "done")
    assert server.job.finished.wait(2)

    q = server.bus.subscribe()
    res = _post(port, "/run", {"keys": [first], "rounds": 0, "mock": True})
    assert res["ok"], res
    assert res["rounds"] == launcher.MIN_ROUNDS
    assert _wait_for(q, "done")


def test_a_second_run_is_rejected_while_a_job_is_running(forged):
    server, port, _ = forged
    server.job = _NeverFinishes()
    res = _post(port, "/run", {"keys": ["whatever"], "mock": True})
    assert res["ok"] is False
    assert "已经有一轮" in res["why"]


def test_a_real_run_emits_the_converge_events(forged, monkeypatch):
    """全部议题结束后还有分歧时，页面要能收到统一轮的开始、结束和保留意见名单。"""
    from duo_forge import digest, society
    from duo_forge.backends import ScriptedBackend
    from duo_forge.society import ConvergenceResult, Turn

    server, port, bp = forged
    first = bp["topics"][0]["key"]
    proposer = bp["personas"][0]["name"]
    challenger = bp["personas"][1]["name"]

    monkeypatch.setattr(
        launcher, "build_backend", lambda mock=False: ScriptedBackend()
    )
    monkeypatch.setattr(
        digest,
        "digest_section",
        lambda *args, **kwargs: "## 节\n内容\n【本节分歧：无】",
    )
    monkeypatch.setattr(
        digest,
        "digest_closing",
        lambda *args, **kwargs: "## 附录 A\n内容\n【未解决分歧：有】",
    )

    def fake_converge(*, personas, backend, context, on_turn=None):
        result = ConvergenceResult(
            turns=[
                Turn(challenger, "我保留：成本没人算。", "挑战者"),
                Turn(proposer, "统一结论：定稿。", "主提出方"),
            ],
            unified="统一结论：定稿。",
            reservations=((challenger, "成本没人算。"),),
        )
        if on_turn:
            for turn in result.turns:
                on_turn(turn)
        return result

    monkeypatch.setattr(society, "converge_history", fake_converge)

    q = server.bus.subscribe()
    res = _post(
        port,
        "/run",
        {"keys": [first], "rounds": 1, "mock": False, "converge": True},
    )
    assert res["ok"], res

    start = _wait_for(q, "converge_start")
    assert start["people"] == 2
    done = _wait_for(q, "converge_done")
    assert done["allConceded"] is False
    assert done["reservations"] == [challenger]
    run_done = _wait_for(q, "run_done")
    assert run_done["handbook"]
    assert server.job.finished.wait(2)


# ---------------------------------------------------------------------------
# 产物与下载
# ---------------------------------------------------------------------------


def test_outputs_are_empty_before_any_run(live_server):
    _, port = live_server
    assert _get_json(port, "/handbook") == {"markdown": "", "path": ""}
    assert _get_json(port, "/quick") == {"markdown": "", "path": ""}
    assert _get_json(port, "/transcript") == {"markdown": "", "path": ""}
    assert _get_status(port, "/handbook.md") == 404
    assert _get_status(port, "/quick.md") == 404
    assert _get_status(port, "/transcript.md") == 404
    assert _get_status(port, "/blueprint.json") == 404


def test_a_mock_run_produces_downloadable_outputs(forged):
    server, port, bp = forged
    first = bp["topics"][0]["key"]
    q = server.bus.subscribe()

    res = _post(port, "/run", {"keys": [first], "rounds": 1, "mock": True})
    assert res["ok"], res
    assert _wait_for(q, "run_done")
    assert _wait_for(q, "done")

    # 三份正文都能从内存取到，不依赖文件还在不在
    handbook = _get_json(port, "/handbook")["markdown"]
    transcript = _get_json(port, "/transcript")["markdown"]
    assert handbook
    assert transcript
    assert _get_status(port, "/handbook.md") == 200
    assert _get_status(port, "/transcript.md") == 200
    # 速查版在 mock 下也可能产出；有才要求能下载
    if _get_json(port, "/quick")["markdown"]:
        assert _get_status(port, "/quick.md") == 200

    # 产物确实写进了注入的 out_dir，而不是仓库的 outputs/
    assert list(server.out_dir.glob("handbook-*.md"))
    assert list(server.out_dir.glob("transcript-*.md"))


# ---------------------------------------------------------------------------
# keep-alive：每个 POST 必须在提前返回前把请求体读干净
# ---------------------------------------------------------------------------


def test_early_returns_do_not_corrupt_the_keep_alive_connection(live_server):
    _, port = live_server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

    def request(method: str, path: str, payload: dict | None = None) -> tuple[int, bytes]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()

    try:
        # 非法目的：校验失败，提前返回
        assert request("POST", "/forge", {"purpose": "短"})[0] == 200
        assert request("GET", "/health")[0] == 200

        # 没有蓝图：提前返回
        assert request("POST", "/run", {"keys": ["x"]})[0] == 200
        assert request("GET", "/health")[0] == 200

        # 追问太短：校验失败，提前返回
        assert request("POST", "/followup", {"question": "x"})[0] == 200
        assert request("GET", "/health")[0] == 200

        # 不认识的重生成目标：提前返回
        assert request("POST", "/reforge", {"what": "nope"})[0] == 200
        assert request("GET", "/health")[0] == 200

        # 手动编辑蓝图：请求体不是蓝图对象，提前返回
        assert request("POST", "/blueprint/edit", {})[0] == 200
        assert request("GET", "/health")[0] == 200

        # 没有任务在跑：停止也提前返回
        assert request("POST", "/stop", {})[0] == 200
        assert request("GET", "/health")[0] == 200

        # 未知 POST 路径
        assert request("POST", "/nope", {"a": 1})[0] == 404
        assert request("GET", "/health")[0] == 200
    finally:
        conn.close()


def test_worker_thread_is_a_daemon(live_server):
    server, port = live_server
    _post(
        port,
        "/forge",
        {"purpose": "给刚上班的程序员写一份颈椎保养手册，要能真的照做。", "mock": True},
    )
    job = server.job
    assert job is not None
    assert job._thread is not None
    assert job._thread.daemon is True


# ---------------------------------------------------------------------------
# 停止
# ---------------------------------------------------------------------------


def test_stopping_a_run_writes_nothing(forged, monkeypatch):
    server, port, bp = forged
    first = bp["topics"][0]["key"]

    blocker = threading.Event()

    def fake_run_pipeline(*, should_stop, **kwargs):
        while not should_stop():
            blocker.wait(0.02)
        raise launcher.pipeline.RunStopped()

    monkeypatch.setattr(launcher.pipeline, "run_pipeline", fake_run_pipeline)

    q = server.bus.subscribe()
    res = _post(port, "/run", {"keys": [first], "rounds": 1, "mock": True})
    assert res["ok"], res

    assert _post(port, "/stop", {})["ok"] is True
    notice = _wait_for(q, "notice")
    assert "没有写出任何文件" in notice["text"]
    assert _wait_for(q, "done")
    assert list(server.out_dir.glob("*")) == []


# ---------------------------------------------------------------------------
# 不认识的路径
# ---------------------------------------------------------------------------


def test_unknown_path_404s(live_server):
    _, port = live_server
    assert _get_status(port, "/nope") == 404


# ---------------------------------------------------------------------------
# 自定义人格
# ---------------------------------------------------------------------------

_MANUAL_PROPOSER = {
    "mode": "manual",
    "name": "许医师",
    "title": "三甲医院康复科物理治疗师",
    "stance": "什么是对的",
    "system_message": (
        "【人格】\n只认证据。\n\n【专业立场】\n先评估再干预。\n\n"
        "【你的盲区，不要掩饰】\n方案偏理想。\n\n"
        "【长期背景与案例】\n看过的典型病例都在这里。"
    ),
}

_MANUAL_CHALLENGER = {
    "mode": "manual",
    "name": "老张",
    "title": "带过两百个程序员的健身教练",
    "stance": "他到底做不做得到",
    "system_message": "【人格】\n务实。\n\n【专业立场】\n能坚持的六十分更值钱。",
}


def test_forge_accepts_a_manual_persona(live_server):
    server, port = live_server
    q = server.bus.subscribe()
    res = _post(
        port,
        "/forge",
        {
            "purpose": "给刚上班的程序员写一份颈椎保养手册，要能真的照做。",
            "topics": 3,
            "mock": True,
            "personas": {
                "proposer": _MANUAL_PROPOSER,
                "challenger": {"mode": "hint", "hint": "一位带过很多程序员的实战派教练"},
            },
        },
    )
    assert res["ok"], res

    event = _wait_for(q, "forge_done")
    bp = event["blueprint"]
    assert bp["personas"][0]["name"] == "许医师"
    assert "【长期背景与案例】" in bp["personas"][0]["system_message"]
    assert "【发言纪律】" in bp["personas"][0]["system_message"], "纪律仍由代码追加"
    # hint 侧由 mock 后端生成
    assert bp["personas"][1]["name"] == "挑刺方"
    assert _wait_for(q, "done")


def test_invalid_personas_do_not_touch_the_previous_blueprint(forged):
    server, port, _ = forged
    before = _get_json(port, "/blueprint")
    history = list(server.bus._history)

    res = _post(
        port,
        "/forge",
        {
            "purpose": "给刚上班的程序员写一份颈椎保养手册，要能真的照做。",
            "mock": True,
            "personas": {"proposer": {"mode": "manual", "name": "只有名字"}},
        },
    )
    assert res["ok"] is False
    assert "第一位专家" in res["why"]

    assert _get_json(port, "/blueprint")["title"] == before["title"]
    assert server.bus._history == history, "校验失败不该清掉历史"


def test_both_manual_still_generates_topics_and_runs(live_server):
    server, port = live_server
    q = server.bus.subscribe()
    res = _post(
        port,
        "/forge",
        {
            "purpose": "给刚上班的程序员写一份颈椎保养手册，要能真的照做。",
            "topics": 3,
            "mock": True,
            "personas": {
                "proposer": _MANUAL_PROPOSER,
                "challenger": _MANUAL_CHALLENGER,
            },
        },
    )
    assert res["ok"], res

    event = _wait_for(q, "forge_done")
    bp = event["blueprint"]
    assert bp["personas"][0]["name"] == "许医师"
    assert bp["personas"][1]["name"] == "老张"
    assert bp["topics"], "全部手写时议题仍要由模型生成"
    assert _wait_for(q, "done")
    assert server.job.finished.wait(2)

    first = bp["topics"][0]["key"]
    q2 = server.bus.subscribe()
    res = _post(port, "/run", {"keys": [first], "rounds": 1, "mock": True})
    assert res["ok"], res
    assert _wait_for(q2, "run_done")
    assert _wait_for(q2, "done")
    assert _get_json(port, "/handbook")["markdown"]


# ---------------------------------------------------------------------------
# 多人：人数、团队指令、发言侧
# ---------------------------------------------------------------------------


def _forge_three(server, port, **extra):
    q = server.bus.subscribe()
    payload = {
        "purpose": "给刚上班的程序员写一份颈椎保养手册，要能真的照做。",
        "topics": 2,
        "mock": True,
        "people": 3,
        "personas": [{}, {}, {}],
    }
    payload.update(extra)
    res = _post(port, "/forge", payload)
    assert res["ok"], res
    event = _wait_for(q, "forge_done")
    assert _wait_for(q, "done")
    assert server.job.finished.wait(2)
    return q, event["blueprint"]


def test_forge_with_three_people_exposes_three_personas(live_server):
    server, port = live_server
    _, bp = _forge_three(server, port)

    assert bp["people"] == 3
    assert len(bp["personas"]) == 3
    assert len({p["role"] for p in bp["personas"]}) == 3
    assert bp["personas"][0]["position"] == "主提出方"

    payload = _get_json(port, "/blueprint")
    assert payload["people"] == 3
    assert len(payload["personas"]) == 3


def test_forge_rejects_an_out_of_range_people(live_server):
    _, port = live_server
    res = _post(
        port,
        "/forge",
        {
            "purpose": "给刚上班的程序员写一份颈椎保养手册，要能真的照做。",
            "mock": True,
            "people": 6,
        },
    )
    assert res["ok"] is False
    assert "2–5" in res["why"]


def test_forge_accepts_a_team_hint(live_server):
    server, port = live_server
    _, bp = _forge_three(
        server, port, teamHint="要有一个医生、一个患者、一个管理者"
    )
    assert bp["people"] == 3


def test_forge_accepts_a_manual_array_shorter_than_people(live_server):
    server, port = live_server
    q = server.bus.subscribe()
    res = _post(
        port,
        "/forge",
        {
            "purpose": "给刚上班的程序员写一份颈椎保养手册，要能真的照做。",
            "topics": 2,
            "mock": True,
            "people": 3,
            "personas": [_MANUAL_PROPOSER],
        },
    )
    assert res["ok"], res
    bp = _wait_for(q, "forge_done")["blueprint"]
    assert bp["people"] == 3
    assert bp["personas"][0]["name"] == "许医师"
    assert "【发言纪律】" in bp["personas"][0]["system_message"]
    assert _wait_for(q, "done")


def test_three_person_run_reports_personas_and_sides(live_server):
    server, port = live_server
    _, bp = _forge_three(server, port)

    first = bp["topics"][0]["key"]
    q = server.bus.subscribe()
    res = _post(port, "/run", {"keys": [first], "rounds": 1, "mock": True})
    assert res["ok"], res

    start = _wait_for(q, "run_start")
    assert start["people"] == 3
    assert len(start["personas"]) == 3
    assert start["personas"][0]["position"] == "主提出方"

    sides = set()
    roles = set()
    while True:
        event = q.get(timeout=15)
        if event.get("type") == "turn":
            sides.add(event["side"])
            roles.add(event["role"])
        if event.get("type") == "done":
            break

    assert sides == {"s0", "s1", "s2"}
    assert roles == {"主提出方", "挑战者", "成本把关人"}


def test_page_boot_has_people_bounds(live_server):
    _, port = live_server
    html = _get(port, "/")
    boot = json.loads(re.search(r"const BOOT = (.*?);\n", html).group(1))
    assert boot["minPeople"] == 2
    assert boot["maxPeople"] == 5
    assert boot["defaultPeople"] == 2
    assert boot["maxRole"] > 0
    assert boot["maxTeamHint"] > 0


# ---------------------------------------------------------------------------
# 拖入材料：先提炼目的，再锻造
# ---------------------------------------------------------------------------


def test_forge_with_material_distills_then_forges(live_server):
    server, port = live_server
    q = server.bus.subscribe()

    res = _post(
        port,
        "/forge",
        {
            "material": "# 颈椎保养\n\n给刚上班、每天坐十小时的程序员写一份能照做的手册。",
            "files": ["brief.md", "notes.md"],
            "topics": 2,
            "mock": True,
        },
    )
    assert res["ok"], res
    assert res["material"] is True
    assert res["files"] == ["brief.md", "notes.md"]

    start = _wait_for(q, "forge_start")
    assert start["material"] is True
    assert start["files"] == ["brief.md", "notes.md"]

    distilled = _wait_for(q, "material_distilled")
    assert distilled["title"]
    assert distilled["purpose"]
    assert distilled["files"] == ["brief.md", "notes.md"]

    event = _wait_for(q, "forge_done")
    assert event["blueprint"]["purpose"]["text"] == distilled["purpose"]
    assert event["blueprint"]["title"] == distilled["title"]

    assert _wait_for(q, "done")
    assert server.job.finished.wait(2)


def test_forge_rejects_too_long_material(live_server):
    _, port = live_server
    res = _post(
        port,
        "/forge",
        {"material": "长" * (launcher.MAX_MATERIAL + 1), "mock": True},
    )
    assert res["ok"] is False
    assert "材料太长" in res["why"]


def test_forge_requires_a_purpose_or_material(live_server):
    _, port = live_server
    res = _post(port, "/forge", {"mock": True})
    assert res["ok"] is False
    assert "没有写目的" in res["why"]


def test_page_boot_has_material_limits(live_server):
    _, port = live_server
    html = _get(port, "/")
    boot = json.loads(re.search(r"const BOOT = (.*?);\n", html).group(1))
    assert boot["maxMaterial"] == launcher.MAX_MATERIAL
    assert boot["maxMaterialFiles"] == launcher.MAX_MATERIAL_FILES
    assert boot["maxMaterialFileBytes"] == launcher.MAX_MATERIAL_FILE_BYTES


def test_forge_accepts_material_above_the_old_limit(live_server):
    """30k → 50k 之后，4 万字的材料应该能正常开始锻造。"""
    server, port = live_server
    q = server.bus.subscribe()
    material = "材" * 40000

    res = _post(
        port,
        "/forge",
        {"material": material, "files": ["big.md"], "topics": 1, "mock": True},
    )
    assert res["ok"], res

    assert _wait_for(q, "material_distilled")
    assert _wait_for(q, "forge_done")
    assert _wait_for(q, "done")
    assert server.job.finished.wait(2)


# ---------------------------------------------------------------------------
# 追加讨论：读完成果后的追问
# ---------------------------------------------------------------------------


def _run_once(server, port, bp, *, rounds: int = 1) -> None:
    """在 forged 之后跑一轮最小的 mock run，拿到一份可追加的成果。"""
    first = bp["topics"][0]["key"]
    q = server.bus.subscribe()
    res = _post(port, "/run", {"keys": [first], "rounds": rounds, "mock": True})
    assert res["ok"], res
    assert _wait_for(q, "run_done")
    assert _wait_for(q, "done")
    assert server.job.finished.wait(2)


def test_followup_without_a_finished_run_is_rejected(live_server):
    server, port = live_server
    res = _post(port, "/followup", {"question": "这个到底怎么做？"})
    assert res["ok"] is False
    assert "还没有跑完" in res["why"]
    assert server.bus._history == []


def test_followup_rejects_a_blank_question(forged):
    server, port, bp = forged
    _run_once(server, port, bp)
    history = list(server.bus._history)

    res = _post(port, "/followup", {"question": "   "})
    assert res["ok"] is False
    assert "追问" in res["why"]
    assert server.bus._history == history, "校验失败不该清掉历史"


def test_followup_appends_to_the_handbook_and_can_repeat(forged):
    server, port, bp = forged
    _run_once(server, port, bp)
    before = _get_json(port, "/handbook")["markdown"]

    q = server.bus.subscribe()
    res = _post(
        port,
        "/followup",
        {"question": "连续开会时做不到怎么办？", "rounds": 1, "mock": True},
    )
    assert res["ok"], res
    assert res["index"] == 1
    start = _wait_for(q, "followup_start")
    assert start["index"] == 1
    done = _wait_for(q, "followup_done")
    assert done["index"] == 1
    assert _wait_for(q, "done")
    assert server.job.finished.wait(2)

    after = _get_json(port, "/handbook")["markdown"]
    assert "## 追加讨论 1：" in after
    assert "连续开会时做不到怎么办？" in after
    # 原正文还在，新一节插在免责声明之前
    before_body = before.split("---\n\n*本文件由 camel-duo-forge 生成")[0].rstrip()
    assert before_body in after
    assert after.index("## 追加讨论 1：") < after.rindex(
        "本文件由 camel-duo-forge 生成"
    )
    assert "## 议题：追加讨论 1：" in _get_json(port, "/transcript")["markdown"]
    assert "## 追加讨论 1：" in _get(port, "/handbook.md")
    assert _get_status(port, "/handbook.md") == 200

    # 再来一次：编号递增，两节都在。
    # 订阅放在 POST 之后：准入时 bus.reset() 会清掉第一次追加的历史，
    # 这时候 replay 里才只有这一次的事件（否则会先读到上一次的 followup_done）。
    res2 = _post(
        port,
        "/followup",
        {"question": "那只能坐着做两分钟呢？", "rounds": 1, "mock": True},
    )
    assert res2["ok"], res2
    assert res2["index"] == 2
    q2 = server.bus.subscribe()
    assert _wait_for(q2, "followup_done")["index"] == 2
    assert _wait_for(q2, "done")
    assert server.job.finished.wait(2)

    final = _get_json(port, "/handbook")["markdown"]
    assert "## 追加讨论 1：" in final
    assert "## 追加讨论 2：" in final
    assert final.count("## 追加讨论 ") == 2


def test_a_second_followup_is_rejected_while_a_job_is_running(forged):
    server, port, bp = forged
    _run_once(server, port, bp)
    server.job = _NeverFinishes()

    res = _post(port, "/followup", {"question": "还能再问一个吗？"})
    assert res["ok"] is False
    assert "已经有一轮" in res["why"]


def test_stopping_a_followup_leaves_the_handbook_untouched(forged, monkeypatch):
    server, port, bp = forged
    _run_once(server, port, bp)
    before = {path: path.read_bytes() for path in server.out_dir.glob("*")}

    blocker = threading.Event()

    def fake_followup(*, should_stop, **kwargs):
        while not should_stop():
            blocker.wait(0.02)
        raise launcher.pipeline.RunStopped()

    monkeypatch.setattr(launcher.followup_mod, "run_followup", fake_followup)

    q = server.bus.subscribe()
    res = _post(
        port,
        "/followup",
        {"question": "这个问题先别跑", "rounds": 1, "mock": True},
    )
    assert res["ok"], res

    assert _post(port, "/stop", {})["ok"] is True
    notice = _wait_for(q, "notice")
    assert "成果文件没有被改动" in notice["text"]
    assert _wait_for(q, "done")
    assert server.job.finished.wait(2)

    after = {path: path.read_bytes() for path in server.out_dir.glob("*")}
    assert after == before


def test_page_boot_has_followup_limits(live_server):
    _, port = live_server
    html = _get(port, "/")
    boot = json.loads(re.search(r"const BOOT = (.*?);\n", html).group(1))
    assert boot["minFollowup"] == launcher.MIN_FOLLOWUP_QUESTION
    assert boot["maxFollowup"] == launcher.MAX_FOLLOWUP_QUESTION
    assert boot["maxFollowupTitle"] == launcher.MAX_TITLE


# ---------------------------------------------------------------------------
# 材料 + 用户目的：材料只是参考
# ---------------------------------------------------------------------------


def test_forge_with_material_and_a_seed_uses_the_users_purpose(live_server):
    server, port = live_server
    q = server.bus.subscribe()

    res = _post(
        port,
        "/forge",
        {
            "material": "一些和颈椎有关的参考资料。",
            "files": ["ref.md"],
            "purpose": "给刚上班、每天坐十小时的程序员写一份能照做的颈椎保养手册。",
            "title": "我的颈椎手册",
            "materialMode": "seed",
            "topics": 2,
            "mock": True,
        },
    )
    assert res["ok"], res
    assert res["seeded"] is True
    assert res["materialMode"] == "seed"

    start = _wait_for(q, "forge_start")
    assert start["seeded"] is True
    assert start["materialMode"] == "seed"
    distilled = _wait_for(q, "material_distilled")
    assert distilled["seeded"] is True

    event = _wait_for(q, "forge_done")
    assert event["blueprint"]["title"] == "我的颈椎手册"
    assert "具体化" in event["blueprint"]["purpose"]["text"]
    assert _wait_for(q, "done")
    assert server.job.finished.wait(2)


def test_forge_with_material_seed_mode_requires_a_purpose(live_server):
    server, port = live_server
    res = _post(
        port,
        "/forge",
        {"material": "一些参考材料", "materialMode": "seed", "mock": True},
    )
    assert res["ok"] is False
    assert "先写一个目的" in res["why"]
    assert server.bus._history == []


def test_forge_material_distill_mode_ignores_the_provided_purpose(live_server):
    server, port = live_server
    q = server.bus.subscribe()

    res = _post(
        port,
        "/forge",
        {
            "material": "一些参考材料",
            "purpose": "给刚上班、每天坐十小时的程序员写一份能照做的手册。",
            "materialMode": "distill",
            "topics": 2,
            "mock": True,
        },
    )
    assert res["ok"], res
    assert res["seeded"] is False
    assert _wait_for(q, "forge_start")["seeded"] is False
    event = _wait_for(q, "forge_done")
    assert event["blueprint"]["title"] == "模拟材料对谈手册"
    assert "从拖入材料提炼出的目的" in event["blueprint"]["purpose"]["text"]
    assert _wait_for(q, "done")
    assert server.job.finished.wait(2)


# ---------------------------------------------------------------------------
# 按部件重生成：议题 / 人格
# ---------------------------------------------------------------------------


def test_reforge_topics_keeps_the_purpose_title_and_personas(forged):
    server, port, bp = forged
    q = server.bus.subscribe()

    res = _post(
        port,
        "/reforge",
        {"what": "topics", "topics": 2, "mock": True},
    )
    assert res["ok"], res
    assert res["what"] == "topics"

    start = _wait_for(q, "reforge_start")
    assert start["what"] == "topics"
    event = _wait_for(q, "forge_done")
    assert event["reforge"] is True
    new = event["blueprint"]
    assert new["ready"] is True
    assert new["title"] == bp["title"]
    assert new["purpose"] == bp["purpose"]
    assert [p["name"] for p in new["personas"]] == [
        p["name"] for p in bp["personas"]
    ]
    assert new["topics"]
    assert _wait_for(q, "done")
    assert server.job.finished.wait(2)

    # 新蓝图落盘，旧蓝图文件仍然在
    files = list(server.blueprint_dir.glob("*.json"))
    assert len(files) >= 2
    assert _get_json(port, "/blueprint")["title"] == bp["title"]


def test_reforge_personas_keeps_the_purpose_and_title(forged):
    server, port, bp = forged
    q = server.bus.subscribe()

    res = _post(
        port,
        "/reforge",
        {
            "what": "personas",
            "people": 3,
            "personas": [{}, {}, {}],
            "mock": True,
        },
    )
    assert res["ok"], res

    start = _wait_for(q, "reforge_start")
    assert start["people"] == 3
    event = _wait_for(q, "forge_done")
    new = event["blueprint"]
    assert new["people"] == 3
    assert new["title"] == bp["title"]
    assert new["purpose"] == bp["purpose"]
    assert new["topics"]
    assert _wait_for(q, "done")
    assert server.job.finished.wait(2)


def test_reforge_requires_a_blueprint(live_server):
    server, port = live_server
    res = _post(port, "/reforge", {"what": "topics", "mock": True})
    assert res["ok"] is False
    assert "还没有蓝图" in res["why"]
    assert server.bus._history == []


def test_reforge_rejects_an_unknown_target(forged):
    _, port, _ = forged
    res = _post(port, "/reforge", {"what": "purpose", "mock": True})
    assert res["ok"] is False
    assert "topics 或 personas" in res["why"]


def test_a_second_reforge_is_rejected_while_a_job_is_running(forged):
    server, port, _ = forged
    server.job = _NeverFinishes()
    res = _post(port, "/reforge", {"what": "topics", "mock": True})
    assert res["ok"] is False
    assert "已经有一轮" in res["why"]


def test_reforge_passes_the_hint_to_the_selected_target(forged, monkeypatch):
    from duo_forge import authoring

    captured: dict[str, object] = {}
    real = authoring.reforge_blueprint

    def spy(base, **kwargs):
        captured.update(kwargs)
        return real(base, **kwargs)

    monkeypatch.setattr(authoring, "reforge_blueprint", spy)
    server, port, _ = forged
    q = server.bus.subscribe()

    res = _post(
        port,
        "/reforge",
        {"what": "topics", "hint": "议题更偏执行、少谈理论", "mock": True},
    )
    assert res["ok"], res
    assert res["hint"] == "议题更偏执行、少谈理论"
    assert _wait_for(q, "reforge_start")["hint"] == "议题更偏执行、少谈理论"
    assert _wait_for(q, "forge_done")
    assert _wait_for(q, "done")
    assert server.job.finished.wait(2)

    assert captured["topic_hint"] == "议题更偏执行、少谈理论"
    assert captured["persona_hint"] == ""


def test_reforge_personas_uses_the_hint_for_personas(forged, monkeypatch):
    from duo_forge import authoring

    captured: dict[str, object] = {}
    real = authoring.reforge_blueprint

    def spy(base, **kwargs):
        captured.update(kwargs)
        return real(base, **kwargs)

    monkeypatch.setattr(authoring, "reforge_blueprint", spy)
    server, port, _ = forged
    q = server.bus.subscribe()

    res = _post(
        port,
        "/reforge",
        {
            "what": "personas",
            "people": 3,
            "personas": [{}, {}, {}],
            "hint": "换一位成本把关人",
            "mock": True,
        },
    )
    assert res["ok"], res
    assert _wait_for(q, "forge_done")
    assert _wait_for(q, "done")
    assert server.job.finished.wait(2)

    assert captured["persona_hint"] == "换一位成本把关人"
    assert captured["topic_hint"] == ""


def test_reforge_rejects_an_overlong_hint(forged):
    server, port, _ = forged
    history = list(server.bus._history)

    res = _post(
        port,
        "/reforge",
        {"what": "topics", "hint": "长" * (launcher.MAX_REFORGE_HINT + 1), "mock": True},
    )
    assert res["ok"] is False
    assert "重生成要求" in res["why"]
    assert server.bus._history == history


def test_page_boot_has_reforge_hint_limit(live_server):
    _, port = live_server
    html = _get(port, "/")
    boot = json.loads(re.search(r"const BOOT = (.*?);\n", html).group(1))
    assert boot["maxReforgeHint"] == launcher.MAX_REFORGE_HINT


# ---------------------------------------------------------------------------
# 手动编辑蓝图
# ---------------------------------------------------------------------------


def test_blueprint_edit_updates_and_saves_a_new_blueprint(forged):
    server, port, _ = forged
    raw = _get_json(port, "/blueprint")["raw"]
    raw["title"] = "手动改过的标题"
    raw["topics"][0]["section_title"] = "一、手动改过的议题"

    res = _post(port, "/blueprint/edit", {"blueprint": raw})
    assert res["ok"], res

    edited = res["blueprint"]
    assert edited["title"] == "手动改过的标题"
    assert edited["topics"][0]["section_title"] == "一、手动改过的议题"
    assert _get_json(port, "/blueprint")["title"] == "手动改过的标题"
    assert "手动改过的标题" in _get(port, "/blueprint.md")

    # 旧蓝图文件不被覆盖，新蓝图另存一份
    assert len(list(server.blueprint_dir.glob("*.json"))) >= 2


def test_blueprint_edit_rejects_an_invalid_blueprint(forged):
    server, port, bp = forged
    raw = _get_json(port, "/blueprint")["raw"]
    raw["personas"] = raw["personas"][:1]

    res = _post(port, "/blueprint/edit", {"blueprint": raw})
    assert res["ok"] is False
    assert "2–5" in res["why"]
    assert _get_json(port, "/blueprint")["title"] == bp["title"]


def test_blueprint_edit_is_rejected_while_a_job_is_running(forged):
    server, port, _ = forged
    raw = _get_json(port, "/blueprint")["raw"]
    server.job = _NeverFinishes()

    res = _post(port, "/blueprint/edit", {"blueprint": raw})
    assert res["ok"] is False
    assert "已经有一轮" in res["why"]
