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
    assert bp["topics"], "两位都手写时议题仍要由模型生成"
    assert _wait_for(q, "done")
    assert server.job.finished.wait(2)

    first = bp["topics"][0]["key"]
    q2 = server.bus.subscribe()
    res = _post(port, "/run", {"keys": [first], "rounds": 1, "mock": True})
    assert res["ok"], res
    assert _wait_for(q2, "run_done")
    assert _wait_for(q2, "done")
    assert _get_json(port, "/handbook")["markdown"]
