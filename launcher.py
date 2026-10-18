# -*- coding: utf-8 -*-
"""launcher.py —— 网页启动器：目的 → 锻造蓝图 → 审阅 → 对谈出成果

跑法：
    .venv/bin/python launcher.py            # 起服务，并自动打开浏览器
    .venv/bin/python launcher.py --mock     # 页面默认勾上模拟模式：不花钱、不联网

**日常用法是双击桌面快捷方式**（见 make_shortcut.py），不用敲命令。

和前作 camel-fitness-nutrition-duo 的 launcher 是同一套骨架（广播、SSE、单实例、
WSL 开浏览器），但流程不一样：那边人格和议题是写死的，选完就直接跑；这边要先
`forge` 出蓝图，让人审一眼、勾选议题，再 `run`。所以页面分成两步。

两件为「双击」做的事：
1. `_open_browser()` 走 WSL 互操作调 Windows 的 explorer.exe——WSL 里 webbrowser
   找的是 Linux 侧的图形浏览器，基本没装，指望不上。
2. `_existing_instance()` + `/health`：**重复双击不会起第二个服务**，只会把浏览器
   指到已经在跑的那个。

**零新增依赖**，只用标准库。

分层红线
--------
`launcher.py` 可以 import `duo_forge.*`，但 **`duo_forge` 里的任何模块都不 import
launcher**。所以命令行 `duo-forge run` 在没有浏览器、没有 Windows 的情况下照样能跑。
根目录的 launcher.py 也不进 wheel（pyproject 只打包 src/duo_forge）。

线程模型（改这个文件之前先读完）
--------------------------------
    HTTP 线程池（ThreadingHTTPServer，每个连接一个线程）
       ├─ GET  /events   → 阻塞在自己的订阅队列上，逐条 SSE 写出
       └─ POST /stop     → job.request_stop()

    daemon worker 线程：**同步**调用 authoring / pipeline
       └─ 各 hook → bus.publish(...)

1. **worker 线程里一个 HTTP 调用都不许发**——只往广播里放。
2. 跨线程的裸状态只有下面几个，而且都有明确的同步方式：
       server.job         HTTP 准入线程写，/health、/stop、准入读  → server._lock
       server.blueprint   forge worker 成功后写（锁内），路由读      → server._lock
       job.stopped        只置位，pipeline 在轮次边界读             → threading.Event
       job.finished       worker 终态写，准入读                     → threading.Event
   **准入必须在 server._lock 里做「检查 + 构造 + 赋值」**：否则两个 /forge 可能
   同时通过检查，起两个付费 worker。
3. **worker 线程必须 daemon，而且永不 join**——join 会把「关掉页面」变成
   「等模型把这一轮跑完」。
4. 终态事件先 publish `done`，再 `finished.set()`。反过来的话，新 job 的
   `bus.reset()` 可能夹在中间，旧 job 的 `done` 会落进新 job 的历史里。
"""

from __future__ import annotations

import argparse
import json
import queue
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from duo_forge import pipeline
from duo_forge.blueprint import Blueprint, BlueprintError
from duo_forge.config import (
    BLUEPRINT_DIR,
    OUTPUT_DIR,
    MissingApiKey,
    build_author_backend,
    build_backend,
)
from duo_forge.personas import (
    MAX_MANUAL_SYSTEM_MESSAGE,
    MAX_NAME,
    MAX_PERSONA_HINT,
    MAX_PERSONA_TITLE,
    MAX_STANCE,
    PersonaPlan,
    PersonaSpecError,
    full_system_message,
    parse_persona_plan,
)
from duo_forge.purpose import (
    MAX_PURPOSE,
    MIN_PURPOSE,
    InvalidPurpose,
    Purpose,
    make_purpose,
)
from duo_forge.topics import (
    CLOSING_KEY,
    DEFAULT_TOPIC_COUNT,
    MAX_TOPICS,
    closing_topic,
)

#: /health 里报的身份。启动时用它认「这个端口上是不是已经有一个我了」。
_APP_ID = "camel-duo-forge-launcher"

#: 默认端口。前作 camel-fitness-nutrition-duo 用 8775，autogen-travel-agent 用 8765；
#: 这里再错开一个，两个项目同时开时单实例扫描不会认错人。
DEFAULT_PORT = 8785

#: 对谈轮数的上限/下限。下限 1 是「一问一答一次」，不是 2——MIN_ROUNDS=2 只约束
#: 「提前收手」最早能在第几轮发生，不约束结构轮数。
MIN_ROUNDS = 1
MAX_ROUNDS = 6


# ---------------------------------------------------------------------------
# 1. 广播：worker 发一条，所有开着页面的浏览器各收一份
# ---------------------------------------------------------------------------


class _Bus:
    """极简广播 + 历史回放。

    历史回放是为了**刷新页面不丢内容**：新订阅者一上来先把已有事件补给它。
    事件带一个**单调递增、永不重置**的 seq，前端用它去重，所以重连/重放不会
    把消息渲染两遍；`reset()` 也不重置 seq。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self._history: list[dict] = []
        self._seq = 0

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
            replay = list(self._history)
        for event in replay:
            q.put(event)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            self._seq += 1
            event = {**event, "seq": self._seq}
            self._history.append(event)
            subs = list(self._subs)
        for q in subs:
            q.put(event)

    def reset(self) -> None:
        """开始新的一轮：清掉历史，但 **seq 不重置**（否则前端去重会误杀新事件）。"""
        with self._lock:
            self._history.clear()


# ---------------------------------------------------------------------------
# 2. 两类后台任务：forge 和 run
# ---------------------------------------------------------------------------


class _Job:
    """一次后台任务。每次点按钮新建一个，用完即弃。

    `stopped` 给 pipeline 用（在轮次边界收手）；`cancelled` 给 forge 用
    （authoring 没有 stop 钩子，只能在调用返回后丢弃）。
    """

    kind = "job"
    #: 出错时终端/页面上的标签。子类覆盖。
    failure_label = "任务失败"

    def __init__(self, bus: _Bus) -> None:
        self.bus = bus
        self.stopped = threading.Event()
        self.cancelled = threading.Event()
        #: worker 的终态。准入检查读它，所以必须在终态事件 publish **之后** set。
        self.finished = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        self.stopped.set()
        self.cancelled.set()

    def _worker(self) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def _emit(self, event_type: str, **fields) -> None:
        """发一条事件。`job` 字段统一从这里注入，子类不再手写。

        参数名避开 `kind`：notice 事件自己带一个 `kind`（warning/final）。
        """
        self.bus.publish({"type": event_type, "job": self.kind, **fields})

    def _fail(self, text: str, *, full: bool = True) -> None:
        """把错误放上页面；`full=True` 时完整内容留在终端（网页只放首行）。"""
        print(f"\n[{self.failure_label}]\n{text}\n", file=sys.stderr, flush=True)
        if full:
            first = text.splitlines()[0] if text else "未知错误"
            if len(first) > 300:
                first = first[:300] + "…"
            text = f"{first}（完整信息见终端）"
        self._emit("notice", kind="warning", text=text)

    def _finish(self) -> None:
        """终态事件先 publish，再 `finished.set()`——顺序不能反。"""
        try:
            self._emit("done")
        finally:
            self.finished.set()


class _ForgeJob(_Job):
    """目的 → 蓝图。

    正常两次模型调用（人格 + 议题，重试时最多四次）；只自定义一位仍是两次；
    两位都手写时只有一次（议题）。
    """

    kind = "forge"
    failure_label = "锻造失败"

    def __init__(
        self,
        server: "_Server",
        *,
        purpose: Purpose,
        topic_count: int,
        mock: bool,
        plan: PersonaPlan,
    ) -> None:
        super().__init__(server.bus)
        self.server = server
        self.purpose = purpose
        self.topic_count = topic_count
        self.mock = mock
        #: 用户对两位专家的自定义意图。准入时冻结，worker 里只读。
        self.plan = plan

    def _cancelled_or_superseded(self) -> bool:
        # 需要在锁内调用。被取代（已经有了新 job）和主动取消一样，都不该再写状态。
        with self.server._lock:
            return self.cancelled.is_set() or self.server.job is not self

    def _cancel_if_needed(self) -> bool:
        if not self._cancelled_or_superseded():
            return False
        self._emit("forge_cancelled", text="已取消。蓝图没有保存。")
        return True

    def _worker(self) -> None:
        try:
            from duo_forge import authoring  # 惰性 import：会拖进 camel

            self._emit(
                "forge_start",
                purpose=self.purpose.summary(),
                topics=self.topic_count,
                mock=self.mock,
            )
            backend = build_author_backend(mock=self.mock)

            # 两位都手写时没有「设计专家」这一步，别在页面上报一个不存在的阶段。
            if not self.plan.both_manual:
                self._emit("forge_stage", stage="personas")
            forged = authoring.forge_personas(
                self.purpose, backend=backend, plan=self.plan
            )
            if self._cancel_if_needed():
                return

            self._emit("forge_stage", stage="topics")
            topics = authoring.forge_topics(
                self.purpose,
                forged.pair,
                backend=backend,
                count=self.topic_count,
            )
            if self._cancel_if_needed():
                return

            # 标题/slug 的规则只有 authoring.assemble_blueprint 一份，
            # 避免和命令行各写一遍、日后漂移。
            bp = authoring.assemble_blueprint(
                self.purpose,
                forged,
                topics,
                origin="mock" if self.mock else "model",
            )

            json_path, _ = bp.save_bundle(self.server.blueprint_dir)

            with self.server._lock:
                if self.cancelled.is_set() or self.server.job is not self:
                    # 文件可能已经写了，但绝不把它当成「可用的蓝图」公布出去。
                    return
                self.server.blueprint = bp
                self.server.blueprint_path = json_path

            # 先赋值、再广播：页面收到 forge_done 后可能立刻 POST /run。
            self._emit(
                "forge_done",
                blueprint=_blueprint_payload(bp, json_path),
            )
        except MissingApiKey as exc:
            self._fail(str(exc).strip(), full=False)
        except Exception as exc:  # noqa: BLE001
            self._fail(str(exc).strip())
        finally:
            self._finish()


class _RunJob(_Job):
    """蓝图 → 对谈 → 成果。"""

    kind = "run"
    failure_label = "运行失败"

    def __init__(
        self,
        bus: _Bus,
        *,
        blueprint: Blueprint,
        topics,
        rounds: int,
        mock: bool,
        converge: bool,
        out_dir: Path,
    ) -> None:
        super().__init__(bus)
        # 准入时就把输入冻结下来。worker 里不再回头读 server.blueprint——
        # 否则一次新的 forge 可能把正在跑的这一轮换掉。
        self.blueprint = blueprint
        self.topics = topics
        self.rounds = rounds
        self.mock = mock
        self.converge = converge
        self.out_dir = Path(out_dir)
        #: run_pipeline 的完整结果。下载路由直接从这里取正文与路径，
        #: 不再把六个字段逐个镜像一遍。
        self.result: Optional[pipeline.RunResult] = None

    def _hooks(self) -> pipeline.PipelineHooks:
        emit = self._emit
        proposer = self.blueprint.proposer
        challenger = self.blueprint.challenger

        def run_start(total: int, rounds: int) -> None:
            emit(
                "run_start",
                total=total,
                rounds=rounds,
                mock=self.mock,
                converge=self.converge,
                topics=[
                    {"key": t.key, "title": t.section_title} for t in self.topics
                ],
                proposer={
                    "name": proposer.name,
                    "title": proposer.title,
                    "stance": proposer.stance,
                },
                challenger={
                    "name": challenger.name,
                    "title": challenger.title,
                    "stance": challenger.stance,
                },
            )

        def topic_start(index: int, total: int, topic) -> None:
            emit(
                "topic_start",
                index=index,
                total=total,
                key=topic.key,
                title=topic.section_title,
            )

        def turn(topic, t) -> None:
            emit(
                "turn",
                topic=topic.key,
                speaker=t.speaker,
                side="a" if t.speaker == proposer.name else "b",
                text=t.content,
            )

        def topic_done(topic, transcript) -> None:
            emit(
                "topic_done",
                topic=topic.key,
                turns=len(transcript.turns),
                early=transcript.stopped_early,
                roundsRun=transcript.rounds_run,
            )

        def section_start(topic, is_closing: bool) -> None:
            emit(
                "section_start",
                topic=topic.key,
                title=topic.section_title,
                closing=is_closing,
            )

        def section_done(topic, is_closing: bool, markdown: str) -> None:
            emit(
                "section_done",
                topic=topic.key,
                title=topic.section_title,
                closing=is_closing,
            )

        def quick_start() -> None:
            emit("quick_start")

        return pipeline.PipelineHooks(
            on_run_start=run_start,
            on_topic_start=topic_start,
            on_turn=turn,
            on_topic_done=topic_done,
            on_section_start=section_start,
            on_section_done=section_done,
            on_quick_start=quick_start,
        )

    def _worker(self) -> None:
        try:
            backend = build_backend(mock=self.mock)
            result = pipeline.run_pipeline(
                blueprint=self.blueprint,
                backend=backend,
                rounds=self.rounds,
                topics=self.topics,
                out_dir=self.out_dir,
                mock=self.mock,
                hooks=self._hooks(),
                should_stop=self.stopped.is_set,
                converge=self.converge,
            )
            # 先赋值、再广播 run_done：页面收到它之后会立刻来取这三份正文。
            self.result = result
            self._emit(
                "run_done",
                handbook=str(result.handbook_path),
                quick=str(result.quick_path) if result.quick_path else "",
                transcript=str(result.transcript_path),
            )
        except pipeline.RunStopped:
            self._emit(
                "notice",
                kind="final",
                text="已停止。本轮作废，没有写出任何文件。",
            )
        except MissingApiKey as exc:
            self._fail(str(exc).strip(), full=False)
        except Exception as exc:  # noqa: BLE001
            self._fail(str(exc).strip())
        finally:
            self._finish()


# ---------------------------------------------------------------------------
# 3. 蓝图的 JSON 表示（页面、下载共用）
# ---------------------------------------------------------------------------


def _blueprint_payload(bp: Blueprint, path: Optional[Path] = None) -> dict:
    return {
        "ready": True,
        "origin": bp.origin,
        "title": bp.title,
        "slug": bp.slug,
        "path": str(path) if path else "",
        "markdown": bp.to_markdown(),
        "purpose": {"text": bp.purpose.text, "title": bp.purpose.title},
        "personas": [
            {
                "name": p.name,
                "title": p.title,
                "stance": p.stance,
                "system_message": full_system_message(p, challenger=(index == 1)),
            }
            for index, p in enumerate(bp.personas)
        ],
        "topics": [
            {
                "key": t.key,
                "section_title": t.section_title,
                "brief": t.brief,
                "goal": t.goal,
            }
            for t in bp.topics
        ],
        "closing": {
            "key": CLOSING_KEY,
            "section_title": bp.closing().section_title,
        },
    }


# ---------------------------------------------------------------------------
# 4. HTTP
# ---------------------------------------------------------------------------


def _pick_doc(job: Optional[_RunJob], kind: str) -> tuple[str, Optional[Path]]:
    """取某一轮 run 的某一份产物。JSON 路由和 .md 下载共用它，
    免得「网页上看到的」和「下载到的」各写一份取数逻辑。"""
    if job is None or job.result is None:
        return "", None
    result = job.result
    if kind == "handbook":
        return result.handbook, result.handbook_path
    if kind == "quick":
        return result.quick, result.quick_path
    return result.transcript, result.transcript_path


class _Handler(BaseHTTPRequestHandler):
    server_version = "DuoForgeLauncher"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args) -> None:  # 别把每条请求刷到终端
        pass

    # ---- 工具 ----

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _json(self, payload: dict) -> None:
        self._send(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _send(
        self,
        blob: bytes,
        ctype: str,
        *,
        filename: Optional[str] = None,
        cache: str = "no-store",
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", cache)
        if filename:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"'
            )
        self.end_headers()
        self.wfile.write(blob)

    # ---- GET ----

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(_build_page(self.server), "text/html; charset=utf-8")
        elif path == "/health":
            job = self.server.job
            running = self.server.busy()
            self._json(
                {
                    "app": _APP_ID,
                    "running": running,
                    "stage": job.kind if running else "idle",
                }
            )
        elif path == "/events":
            self._stream()
        elif path == "/blueprint":
            with self.server._lock:
                bp = self.server.blueprint
                bp_path = self.server.blueprint_path
            if bp is None:
                self._json({"ready": False})
            else:
                self._json(_blueprint_payload(bp, bp_path))
        elif path in ("/blueprint.json", "/blueprint.md"):
            with self.server._lock:
                bp = self.server.blueprint
            if bp is None:
                # 状态行必须 ASCII：http.server 按 latin-1 编码，中文会直接抛。
                self.send_error(404, "No blueprint yet")
                return
            if path.endswith(".json"):
                blob = bp.to_json().encode("utf-8")
                ctype, name = "application/json; charset=utf-8", f"{bp.slug}.json"
            else:
                blob = bp.to_markdown().encode("utf-8")
                ctype, name = "text/markdown; charset=utf-8", f"{bp.slug}.md"
            self._send(blob, ctype, filename=name)
        elif path in ("/handbook", "/quick", "/transcript"):
            kind = path.lstrip("/")
            text, target = _pick_doc(self.server.last_run, kind)
            self._json(
                {"markdown": text, "path": str(target) if target else ""}
            )
        elif path in ("/handbook.md", "/quick.md", "/transcript.md"):
            kind = path[1:-3]
            text, target = _pick_doc(self.server.last_run, kind)
            if not text:
                self.send_error(404, f"No {kind} yet")
                return
            name = target.name if target else f"{kind}.md"
            self._send(
                text.encode("utf-8"),
                "text/markdown; charset=utf-8",
                filename=name,
            )
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    # ---- POST ----

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        # 请求体必须在**任何提前返回之前**读掉，这是 HTTP/1.1 keep-alive 的硬要求。
        # 没读完的字节会留在 socket 上，和下一个请求的请求行粘在一起。
        body = self._body()

        if path == "/forge":
            self._forge(body)
        elif path == "/run":
            self._run(body)
        elif path == "/stop":
            self._stop()
        else:
            self.send_error(404)

    def _forge(self, body: dict) -> None:
        server = self.server
        try:
            purpose = make_purpose(
                str(body.get("purpose") or ""),
                str(body.get("title") or ""),
            )
        except InvalidPurpose as exc:
            self._json({"ok": False, "why": str(exc)})
            return

        # 和目的一样，人格设定必须在准入/清历史之前校验完：
        # 写错一个字不该把上一轮蓝图和对话历史擦掉。
        try:
            plan = parse_persona_plan(body.get("personas"))
        except PersonaSpecError as exc:
            self._json({"ok": False, "why": str(exc)})
            return

        try:
            count = int(body.get("topics", DEFAULT_TOPIC_COUNT))
        except (TypeError, ValueError):
            count = DEFAULT_TOPIC_COUNT
        count = max(1, min(MAX_TOPICS, count))
        mock = bool(body.get("mock"))

        with server._lock:
            if server.busy():
                self._json({"ok": False, "why": "已经有一轮在跑了，等它结束。"})
                return
            job = _ForgeJob(
                server,
                purpose=purpose,
                topic_count=count,
                mock=mock,
                plan=plan,
            )
            server.job = job
            # 校验都过了才清历史：填错一个字就把上一轮蓝图的对话历史擦掉太难受。
            server.bus.reset()
        job.start()
        self._json({"ok": True, "topics": count, "mock": mock})

    def _run(self, body: dict) -> None:
        server = self.server

        raw_rounds = body.get("rounds", 3)
        try:
            rounds = int(raw_rounds)
        except (TypeError, ValueError):
            self._json({"ok": False, "why": "轮数必须是整数。"})
            return
        rounds = max(MIN_ROUNDS, min(MAX_ROUNDS, rounds))

        mock = bool(body.get("mock"))
        converge = bool(body.get("converge", True))

        keys: list[str] = []
        seen: set[str] = set()
        for raw_key in body.get("keys") or []:
            key = str(raw_key).strip()
            # 前端可能把固定勾选的收尾议题也带上；它不是蓝图里的正文议题。
            if not key or key == CLOSING_KEY or key in seen:
                continue
            seen.add(key)
            keys.append(key)
        if not keys:
            self._json({"ok": False, "why": "至少要勾一个议题。"})
            return

        with server._lock:
            if server.busy():
                self._json({"ok": False, "why": "已经有一轮在跑了，等它结束。"})
                return
            bp = server.blueprint
            if bp is None:
                self._json({"ok": False, "why": "还没有蓝图。先锻造一份。"})
                return
            try:
                topics = bp.select(",".join(keys))
            except BlueprintError as exc:
                self._json({"ok": False, "why": str(exc)})
                return
            job = _RunJob(
                server.bus,
                blueprint=bp,
                topics=topics,
                rounds=rounds,
                mock=mock,
                converge=converge,
                out_dir=server.out_dir,
            )
            server.job = job
            server.last_run = job
            server.bus.reset()

        warning = ""
        if (bp.origin == "mock") != mock:
            warning = (
                "注意：蓝图是模拟生成的，但这一轮用真实模型跑"
                if bp.origin == "mock"
                else "注意：蓝图是真实模型生成的，但这一轮是模拟模式。"
            )
        job.start()
        self._json(
            {
                "ok": True,
                "rounds": rounds,
                "mock": mock,
                "converge": converge,
                "topics": [t.key for t in topics],
                "warning": warning,
            }
        )

    def _stop(self) -> None:
        job = self.server.job
        if job is None or job.finished.is_set():
            self._json({"ok": False, "why": "还没开始跑。"})
            return
        job.request_stop()
        self._json({"ok": True, "kind": job.kind})

    # ---- SSE ----

    def _stream(self) -> None:
        """连接**不主动关闭**：EventSource 在连接断掉时会自动重连，如果我们在
        `done` 之后就断开，浏览器会重连、拿到历史、又立刻断开——无限重连循环。
        所以让连接一直挂着，靠前端的 seq 去重处理重放。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q = self.server.bus.subscribe()
        try:
            while True:
                event = q.get()
                chunk = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # 关页/刷新，正常现象
        finally:
            self.server.bus.unsubscribe(q)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def busy(self) -> bool:
        """当前是否有任务在跑。准入检查和 /health 共用同一个判定。"""
        return self.job is not None and not self.job.finished.is_set()

    def __init__(
        self,
        addr,
        handler,
        *,
        mock_default: bool = False,
        blueprint_dir: Path | str = BLUEPRINT_DIR,
        out_dir: Path | str = OUTPUT_DIR,
    ) -> None:
        super().__init__(addr, handler)
        self.bus = _Bus()
        self._lock = threading.Lock()
        #: 当前（或最近一次）任务。准入检查和赋值都在 _lock 里。
        self.job: Optional[_Job] = None
        #: 最近一次 forge 出来的蓝图。两段之间唯一的交接点。
        self.blueprint: Optional[Blueprint] = None
        self.blueprint_path: Optional[Path] = None
        #: 最近一次 run 的 job，下载路由从这里取正文。
        self.last_run: Optional[_RunJob] = None
        self.mock_default = mock_default
        self.blueprint_dir = Path(blueprint_dir)
        self.out_dir = Path(out_dir)


# ---------------------------------------------------------------------------
# 5. 浏览器与单实例
# ---------------------------------------------------------------------------


def _existing_instance(port: int) -> bool:
    """这个端口上已经有一个我们自己起的启动器吗？

    认的是 /health 里的 _APP_ID，而不是「端口通不通」——不然会把别人占用的
    端口误判成自己人，然后把浏览器指到一个不相干的页面上。
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=0.6
        ) as resp:
            return json.loads(resp.read()).get("app") == _APP_ID
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False


def _open_browser(url: str) -> None:
    """尽力在**用户的**浏览器里打开这个 URL。

    WSL 里 `webbrowser` 基本指望不上——它找的是 Linux 侧的图形浏览器，多半没装。
    真正管用的是走 WSL 互操作调 Windows 的 `explorer.exe`：它拿 URL 当参数时
    会用 Windows 的**默认浏览器**打开。所以顺序是 Windows 优先、webbrowser 兜底。
    """
    for argv in (["explorer.exe", url], ["cmd.exe", "/c", "start", "", url]):
        exe = shutil.which(argv[0])
        if not exe:
            continue
        try:
            subprocess.run(
                [exe, *argv[1:]],
                check=False,
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except (OSError, subprocess.SubprocessError):
            continue
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 6. 页面
# ---------------------------------------------------------------------------


def _boot_payload(server: _Server) -> dict:
    return {
        "mockDefault": server.mock_default,
        "defaultTopics": DEFAULT_TOPIC_COUNT,
        "maxTopics": MAX_TOPICS,
        "minPurpose": MIN_PURPOSE,
        "maxPurpose": MAX_PURPOSE,
        "defaultRounds": 3,
        "minRounds": MIN_ROUNDS,
        "maxRounds": MAX_ROUNDS,
        "maxManualSystemMessage": MAX_MANUAL_SYSTEM_MESSAGE,
        "maxPersonaHint": MAX_PERSONA_HINT,
        "maxName": MAX_NAME,
        "maxPersonaTitle": MAX_PERSONA_TITLE,
        "maxStance": MAX_STANCE,
        "closing": {
            "key": CLOSING_KEY,
            "title": closing_topic(0).section_title,
        },
    }


def _build_page(server: _Server) -> bytes:
    boot = json.dumps(_boot_payload(server), ensure_ascii=False)
    return _PAGE.replace("__BOOT__", boot).encode("utf-8")


_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>智能体人格与议题生成器</title>
<style>
  :root {
    --bg: #f6f7f9;      --panel: #ffffff;   --fg: #1f2328;
    --muted: #59636e;   --border: #d8dee4;  --accent: #1f6feb;
    --accent-hi: #1a5fd0; --warn: #9a6700;  --bad: #b42318;
    --a: #0969da;       --b: #1a7f37;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 14px/1.65 -apple-system, "Segoe UI", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
  }
  .wrap { max-width: 980px; margin: 0 auto; padding: 20px 16px 40px; }
  header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 16px; }
  h1 { font-size: 17px; font-weight: 600; margin: 0; white-space: nowrap; }
  #status { margin-left: auto; font-size: 13px; color: var(--muted);
            text-align: right; max-width: 70%; white-space: pre-wrap; }
  #status.warn { color: var(--warn); }
  #status.bad { color: var(--bad); }
  #status.live { color: var(--accent); }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; }
  .card + .card, .tabs + .card { margin-top: 12px; }
  .pad { padding: 12px 14px; }
  .hidden { display: none; }
  .stephead { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; font-size: 13px; }
  .stepnum { width: 20px; height: 20px; border-radius: 50%; background: var(--accent);
             color: #fff; font-size: 12px; display: inline-flex; align-items: center;
             justify-content: center; font-weight: 600; }
  textarea, input[type=text], select {
    font: inherit; color: var(--fg); background: var(--panel);
    border: 1px solid var(--border); border-radius: 6px; padding: 7px 9px; outline: none;
  }
  textarea:focus, input[type=text]:focus, select:focus { border-color: var(--accent); }
  #purpose { width: 100%; min-height: 84px; resize: vertical; }
  #title { width: 210px; }
  .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .row .spacer { flex: 1; }
  .row + .row { margin-top: 9px; }
  button {
    font: inherit; border: 1px solid var(--border); border-radius: 6px;
    padding: 7px 14px; background: var(--panel); color: var(--fg); cursor: pointer;
  }
  button:hover:not(:disabled) { background: var(--bg); }
  button:disabled { opacity: .45; cursor: default; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover:not(:disabled) { background: var(--accent-hi); }
  .hint { font-size: 12.5px; color: var(--muted); margin-top: 8px; }
  .hint.cost { color: var(--warn); }
  details.custom { margin-top: 10px; border-top: 1px dashed var(--border); padding-top: 10px; }
  details.custom > summary { font-size: 13px; color: var(--accent); cursor: pointer; user-select: none; }
  .persona-editors { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }
  .editor { flex: 1 1 380px; border: 1px solid var(--border); border-radius: 6px; padding: 9px 11px; }
  .editor .ehead { font-weight: 600; font-size: 13px; margin-bottom: 6px; }
  .editor label { display: block; font-size: 12.5px; color: var(--muted); margin: 8px 0 3px; }
  .editor input[type=text], .editor textarea, .editor select {
    width: 100%; font: inherit; color: var(--fg); background: var(--panel);
    border: 1px solid var(--border); border-radius: 6px; padding: 6px 9px; outline: none;
  }
  .editor textarea { resize: vertical; min-height: 52px; }
  .editor textarea.e-msg { min-height: 150px; }
  .editor input:focus, .editor textarea:focus, .editor select:focus { border-color: var(--accent); }
  .editor .ecount { font-size: 11.5px; color: var(--muted); text-align: right; }
  .editor .ecount.over { color: var(--bad); }
  .editor .grid { display: flex; flex-wrap: wrap; gap: 8px; }
  .editor .grid > * { flex: 1 1 120px; min-width: 0; }
  .personas { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
  .persona { flex: 1 1 300px; border: 1px solid var(--border); border-radius: 6px; padding: 9px 11px; }
  .persona .nm { font-weight: 600; }
  .persona .tt { color: var(--muted); font-size: 12.5px; }
  .persona .st { font-size: 12.5px; margin-top: 3px; }
  .persona details { margin-top: 6px; }
  .persona summary { font-size: 12.5px; color: var(--accent); cursor: pointer; }
  .persona pre { white-space: pre-wrap; word-wrap: break-word; font-size: 12.5px;
                 background: var(--bg); border-radius: 5px; padding: 8px; margin: 6px 0 0; }
  .topics { display: flex; flex-wrap: wrap; gap: 6px 16px; margin: 4px 0 10px; }
  .topics label { font-size: 13px; cursor: pointer; display: flex; align-items: center; gap: 5px; }
  .topics label.fixed { color: var(--muted); cursor: default; }
  #activity { color: var(--accent); }
  #counter, #elapsed, #eta { color: var(--muted); font-variant-numeric: tabular-nums; }
  #eta { color: var(--accent); }
  #progress { font-size: 13px; color: var(--accent); min-height: 20px; white-space: pre-wrap; }
  #notice { font-size: 13px; white-space: pre-wrap; margin-top: 6px; }
  #notice:empty { display: none; }
  #notice.warn { color: var(--warn); }
  #notice.bad { color: var(--bad); }
  .tabs { display: flex; gap: 4px; margin-top: 12px; }
  .tab { padding: 8px 16px; border: 1px solid var(--border); border-bottom: none;
         border-radius: 8px 8px 0 0; background: var(--bg); cursor: pointer; font-size: 13px; }
  .tab.on { background: var(--panel); font-weight: 600; }
  .pane { display: none; }
  .pane.on { display: block; }
  #chat { height: calc(100vh - 430px); min-height: 220px; overflow-y: auto; padding: 6px 14px 14px; }
  .topic-head { font-size: 13px; font-weight: 600; color: var(--muted); margin: 14px 0 4px;
                padding-top: 10px; border-top: 1px solid var(--border); }
  .topic-head:first-child { border-top: 0; margin-top: 4px; padding-top: 0; }
  .msg { padding: 8px 0 2px; white-space: pre-wrap; }
  .msg .src { display: block; font-size: 12px; letter-spacing: .04em; margin-bottom: 2px; font-weight: 600; }
  .msg.a .src { color: var(--a); }
  .msg.b .src { color: var(--b); }
  .empty { color: var(--muted); padding: 22px 0; text-align: center; }
  #bpView, #out { padding: 4px 18px 18px; max-height: calc(100vh - 380px); overflow-y: auto; }
  #bpView h1, #out h1 { font-size: 20px; margin: 18px 0 8px; white-space: normal; }
  #bpView h2, #out h2 { font-size: 17px; margin: 20px 0 8px; padding-top: 8px; border-top: 1px solid var(--border); }
  #bpView h3, #out h3 { font-size: 14.5px; margin: 14px 0 6px; }
  #bpView h4, #out h4 { font-size: 13.5px; margin: 12px 0 4px; }
  #bpView p, #out p { margin: 8px 0; }
  #bpView ul, #out ul, #bpView ol, #out ol { margin: 6px 0; padding-left: 22px; }
  #bpView li, #out li { margin: 3px 0; }
  #out ul.check { list-style: none; padding-left: 4px; }
  #out ul.check input { margin-right: 7px; vertical-align: 1px; }
  #bpView blockquote, #out blockquote {
    margin: 8px 0; padding: 4px 12px; border-left: 3px solid var(--border); color: var(--muted);
  }
  #bpView hr, #out hr { border: 0; border-top: 1px solid var(--border); margin: 16px 0; }
  #bpView pre, #out pre { white-space: pre-wrap; word-wrap: break-word; font-size: 13px;
                          background: var(--bg); border-radius: 5px; padding: 10px; }
  .mockwarn { background: #fff8e5; border: 1px solid #f0d9a0; border-radius: 6px;
              padding: 8px 12px; margin: 10px 0; font-size: 13px; color: var(--warn); }
  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
         background: var(--muted); margin-right: 6px; vertical-align: 1px; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .18; } }
  .dot.live { background: var(--accent); animation: pulse 1.1s ease-in-out infinite; }
  .dot.warn { background: var(--warn); animation: pulse 1.1s ease-in-out infinite; }
  .dot.bad { background: var(--bad); }
  #activity.busy::after {
    content: ""; display: inline-block; width: 1em; text-align: left;
    animation: dots 1.4s steps(4, end) infinite;
  }
  @keyframes dots { 0% { content: ""; } 25% { content: "·"; }
                    50% { content: "··"; } 75% { content: "···"; } }
  .dl { margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--border); }
  .dl a { display: inline-block; font-size: 13px; text-decoration: none; color: var(--fg);
          border: 1px solid var(--border); border-radius: 6px; padding: 6px 12px;
          background: var(--panel); margin: 3px 4px 3px 0; }
  .dl a:hover { background: var(--bg); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>智能体人格与议题生成器</h1>
    <span id="status"><span class="dot" id="dot"></span><span id="statustext">就绪</span></span>
  </header>

  <div class="card pad" id="step1">
    <div class="stephead">
      <span class="stepnum">1</span><strong>给一个目的</strong>
      <span class="spacer"></span>
      <span class="hint" id="purposeCount" style="margin:0"></span>
    </div>
    <textarea id="purpose" maxlength="2000"
      placeholder="说清「给谁、要解决什么、想要什么形态的成果」，例如：给刚上班、每天坐十小时的程序员写一份颈椎保养手册，要能真的照做。"></textarea>
    <div class="row">
      <input type="text" id="title" maxlength="60" placeholder="标题（留空由模型起）">
      <span style="font-size:13px">生成</span>
      <select id="topicCount"></select>
      <span style="font-size:13px">个议题</span>
      <label style="font-size:13px;display:flex;align-items:center;gap:5px;cursor:pointer">
        <input type="checkbox" id="forgeMock"> 模拟模式（不花钱）
      </label>
      <span class="spacer"></span>
      <button class="primary" id="forgeBtn">锻造蓝图</button>
    </div>
    <div class="hint cost" id="forgeHint">
      正常调用模型两次（人格 + 议题；格式不对会重试，最多四次）。两位专家都由你手写时
      只调用一次（议题）。模拟模式不联网、不花钱，产出是占位内容。
    </div>

    <details id="customBox" class="custom">
      <summary>自定义人格（可选）—— 选「交给模型生成」或留空就由模型生成</summary>
      <div class="persona-editors" id="personaEditors"></div>
      <div class="hint">
        手写的人格设定里不要写「发言纪律」「收手纪律」——这两段由系统自动追加，写了会重复。
        人格设定最多 <span id="manualLimit">12000</span> 字；它会随每个议题、每一轮发给模型，
        越长越贵。长期背景与案例直接写进人格设定里即可。
      </div>
    </details>
  </div>

  <div class="card pad hidden" id="step2">
    <div class="stephead">
      <span class="stepnum">2</span><strong>审蓝图，然后开跑</strong>
      <span class="spacer"></span>
      <span class="hint" id="bpMeta" style="margin:0"></span>
    </div>
    <div class="personas" id="personas"></div>
    <div class="topics" id="topics"></div>
    <div class="row">
      <span style="font-size:13px">每个议题最多</span>
      <select id="rounds"></select>
      <label style="font-size:13px;display:flex;align-items:center;gap:5px;cursor:pointer"
             title="挑战者提不出新的反对意见时，这个议题就到此为止，不再跑满上面的轮数">
        <input type="checkbox" id="converge" checked> 谈拢了就收手
      </label>
      <label style="font-size:13px;display:flex;align-items:center;gap:5px;cursor:pointer">
        <input type="checkbox" id="runMock"> 模拟模式（不花钱）
      </label>
      <span class="spacer"></span>
      <button class="primary" id="runBtn">开始对谈</button>
      <button id="stopBtn" disabled>停止</button>
    </div>
    <div class="hint" id="runHint"></div>
  </div>

  <div class="card pad">
    <div class="row">
      <span id="activity">就绪</span>
      <span class="spacer"></span>
      <span id="counter"></span>
      <span id="elapsed"></span>
      <span id="eta"></span>
    </div>
    <div id="progress"></div>
    <div id="notice"></div>
  </div>

  <div class="tabs">
    <div class="tab on" id="tab-chat">对话</div>
    <div class="tab" id="tab-bp">蓝图</div>
    <div class="tab" id="tab-out">成果</div>
  </div>
  <div class="card">
    <div class="pane on" id="pane-chat">
      <div id="chat"><div class="empty" id="empty">锻造出蓝图后，点「开始对谈」。</div></div>
    </div>
    <div class="pane" id="pane-bp">
      <div id="bpView"><div class="empty">还没有蓝图。</div></div>
    </div>
    <div class="pane" id="pane-out">
      <div id="out"><div class="empty">跑完才有成果。</div></div>
    </div>
  </div>
</div>

<script>
const BOOT = __BOOT__;
const $ = (id) => document.getElementById(id);
let lastSeq = -1;          // 去重：重连/重放时同一事件不会渲染两遍
let bp = null;             // 当前蓝图的结构化数据
let running = false;
let jobKind = "";          // "forge" | "run"

// ---- 初始化控件的可选项 ----
for (let n = 1; n <= BOOT.maxTopics; n++) {
  const o = document.createElement("option");
  o.value = String(n); o.textContent = String(n);
  $("topicCount").appendChild(o);
}
$("topicCount").value = String(Math.min(BOOT.defaultTopics, BOOT.maxTopics));
for (let n = BOOT.minRounds; n <= BOOT.maxRounds; n++) {
  const o = document.createElement("option");
  o.value = String(n); o.textContent = n + " 轮";
  $("rounds").appendChild(o);
}
$("rounds").value = String(BOOT.defaultRounds);
$("forgeMock").checked = !!BOOT.mockDefault;
$("runMock").checked = !!BOOT.mockDefault;

// ---- 目的草稿：只防刷新丢失，不是数据源 ----
const PSTORE = "duo-forge-purpose";
try { $("purpose").value = localStorage.getItem(PSTORE) || ""; } catch (e) { /* 忽略 */ }
function savePurpose() {
  try { localStorage.setItem(PSTORE, $("purpose").value); } catch (e) { /* 忽略 */ }
}
function updatePurposeCount() {
  const n = $("purpose").value.trim().length;
  $("purposeCount").textContent = n + " / " + BOOT.maxPurpose;
  $("purposeCount").style.color =
    n > 0 && (n < BOOT.minPurpose || n > BOOT.maxPurpose) ? "var(--bad)" : "";
}
$("purpose").addEventListener("input", () => { savePurpose(); updatePurposeCount(); });
updatePurposeCount();

// ---- 自定义人格 ----
// 每位专家独立选 auto / hint / manual。只把非 auto 的侧发给后端；
// 后端会用同一套 personas.parse_persona_plan 再校验一遍（前端校验只为早点提示）。
const PERSONA_STORE = "duo-forge-personas";
const SIDES = [
  { key: "proposer", label: "第一位 · 方案提出方",
    hint: "例如：三甲医院康复科物理治疗师，只认证据和量化" },
  { key: "challenger", label: "第二位 · 挑战者",
    hint: "例如：带过两百个程序员的实战派教练，只问做不做得到" },
];
const MANUAL_TEMPLATE =
  "【人格】\n…\n\n【专业立场】\n…\n\n【你的盲区，不要掩饰】\n…\n\n" +
  "【长期背景与案例】（可选）\n…";

function makeEditor(side) {
  function textInput(cls, ph, max) {
    const i = document.createElement("input");
    i.type = "text"; i.className = cls; i.placeholder = ph; i.maxLength = max;
    return i;
  }

  const box = document.createElement("div");
  box.className = "editor";
  box.dataset.side = side.key;

  const head = document.createElement("div");
  head.className = "ehead"; head.textContent = side.label;
  box.appendChild(head);

  const row = document.createElement("div");
  row.className = "row";
  const modeLabel = document.createElement("span");
  modeLabel.style.fontSize = "12.5px";
  modeLabel.style.color = "var(--muted)";
  modeLabel.textContent = "这一位";
  const mode = document.createElement("select");
  mode.className = "e-mode";
  [["auto", "交给模型生成"], ["hint", "给个方向，模型展开"], ["manual", "我自己写"]]
    .forEach(([v, t]) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = t; mode.appendChild(o);
    });
  row.appendChild(modeLabel); row.appendChild(mode);
  box.appendChild(row);

  const hintBox = document.createElement("div");
  hintBox.className = "e-hint hidden";
  const hintLabel = document.createElement("label");
  hintLabel.textContent = "给模型的方向（一两句话）";
  const hint = document.createElement("textarea");
  hint.className = "e-hint-text";
  hint.maxLength = BOOT.maxPersonaHint;
  hint.placeholder = side.hint;
  const hintCount = document.createElement("div");
  hintCount.className = "ecount";
  hintBox.appendChild(hintLabel); hintBox.appendChild(hint); hintBox.appendChild(hintCount);
  box.appendChild(hintBox);

  const manualBox = document.createElement("div");
  manualBox.className = "e-manual hidden";
  const grid = document.createElement("div");
  grid.className = "grid";
  const name = textInput("e-name", "称呼（≤" + BOOT.maxName + " 字）", BOOT.maxName);
  const title = textInput("e-title", "身份（≤" + BOOT.maxPersonaTitle + " 字）", BOOT.maxPersonaTitle);
  const stance = textInput("e-stance", "负责什么（≤" + BOOT.maxStance + " 字）", BOOT.maxStance);
  grid.appendChild(name); grid.appendChild(title); grid.appendChild(stance);
  manualBox.appendChild(grid);
  const msgLabel = document.createElement("label");
  msgLabel.textContent = "人格设定（三块；可再加「长期背景与案例」）";
  const msg = document.createElement("textarea");
  msg.className = "e-msg";
  msg.maxLength = BOOT.maxManualSystemMessage;
  msg.placeholder = MANUAL_TEMPLATE;
  const msgCount = document.createElement("div");
  msgCount.className = "ecount";
  manualBox.appendChild(msgLabel); manualBox.appendChild(msg); manualBox.appendChild(msgCount);
  box.appendChild(manualBox);

  function refresh() {
    const v = mode.value;
    hintBox.classList.toggle("hidden", v !== "hint");
    manualBox.classList.toggle("hidden", v !== "manual");
    hintCount.textContent = hint.value.trim().length + " / " + BOOT.maxPersonaHint;
    const n = msg.value.trim().length;
    msgCount.textContent = n + " / " + BOOT.maxManualSystemMessage;
    msgCount.className = "ecount" + (n > BOOT.maxManualSystemMessage ? " over" : "");
    savePersonaDrafts();
  }
  mode.onchange = refresh;
  [hint, msg, name, title, stance].forEach((el) => el.addEventListener("input", refresh));
  box._refresh = refresh;
  box._fields = { mode, hint, name, title, stance, msg };
  return box;
}

function readEditor(box) {
  const f = box._fields;
  return {
    mode: f.mode.value,
    hint: f.hint.value,
    name: f.name.value,
    title: f.title.value,
    stance: f.stance.value,
    system_message: f.msg.value,
  };
}

function writeEditor(box, data) {
  const f = box._fields;
  f.mode.value = data.mode || "auto";
  f.hint.value = data.hint || "";
  f.name.value = data.name || "";
  f.title.value = data.title || "";
  f.stance.value = data.stance || "";
  f.msg.value = data.system_message || "";
  box._refresh();
}

function collectAllEditors() {
  const out = {};
  document.querySelectorAll("#personaEditors .editor").forEach((box) => {
    out[box.dataset.side] = readEditor(box);
  });
  return out;
}

function collectPersonas() {
  const out = {};
  for (const [side, d] of Object.entries(collectAllEditors())) {
    if (d.mode === "hint") {
      out[side] = { mode: "hint", hint: d.hint.trim() };
    } else if (d.mode === "manual") {
      out[side] = {
        mode: "manual",
        name: d.name.trim(), title: d.title.trim(),
        stance: d.stance.trim(), system_message: d.system_message.trim(),
      };
    }
  }
  return out;
}

function savePersonaDrafts() {
  try {
    localStorage.setItem(PERSONA_STORE, JSON.stringify(collectAllEditors()));
  } catch (e) { /* 隐私模式等，忽略 */ }
}

function restorePersonaDrafts() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(PERSONA_STORE) || "{}"); }
  catch (e) { saved = {}; }
  document.querySelectorAll("#personaEditors .editor").forEach((box) => {
    if (saved[box.dataset.side]) { writeEditor(box, saved[box.dataset.side]); }
  });
}

const editors = $("personaEditors");
for (const side of SIDES) { editors.appendChild(makeEditor(side)); }
$("manualLimit").textContent = String(BOOT.maxManualSystemMessage);
restorePersonaDrafts();
document.querySelectorAll("#personaEditors .editor").forEach((box) => box._refresh());

// ---- 状态与提示 ----
function setStatus(text, tone) {
  $("statustext").textContent = text;
  $("status").className = tone || "";
  $("dot").className = "dot" + (tone ? " " + tone : "");
}
function setActivity(text, busy) {
  $("activity").textContent = text || "";
  $("activity").className = busy ? "busy" : "";
}
function notice(text, tone) {
  $("notice").className = tone || "";
  $("notice").textContent = text || "";
}
const LOCKED_WHILE_RUNNING = [
  "forgeBtn", "purpose", "title", "topicCount", "forgeMock",
  "runBtn", "rounds", "converge", "runMock",
];
function setRunning(on) {
  running = on;
  for (const id of LOCKED_WHILE_RUNNING) { $(id).disabled = on; }
  $("stopBtn").disabled = !on;
  $("topics").querySelectorAll("input").forEach((cb) => {
    cb.disabled = on || cb.dataset.fixed === "1";
  });
  document.querySelectorAll(
    "#personaEditors input, #personaEditors select, #personaEditors textarea"
  ).forEach((el) => { el.disabled = on; });
}

// ---- 时间与进度 ----
let startedAt = 0, timerId = null, turnsDone = 0, turnsTotal = 0;
function tickElapsed() {
  if (!startedAt) { return; }
  const s = Math.floor((Date.now() - startedAt) / 1000);
  const m = Math.floor(s / 60);
  $("elapsed").textContent = "已用 " + (m ? m + " 分 " + (s % 60) + " 秒" : s + " 秒");
  // forge 只有两次调用（含重试），估不准，不报 ETA；run 才按发言数估。
  $("eta").textContent = jobKind === "run" ? etaText(s) : "";
}
function etaText(elapsedSec) {
  if (!turnsTotal || !turnsDone) { return ""; }
  const left = (turnsTotal - turnsDone) * (elapsedSec / turnsDone);
  if (left < 45) { return ""; }
  return "· 预计还需约 " + Math.ceil(left / 60) + " 分钟";
}
function updateCounter() {
  $("counter").textContent = turnsTotal ? "发言 " + turnsDone + "/" + turnsTotal : "";
}
function beginRun(total, rounds) {
  turnsTotal = total * (1 + rounds * 2);
  turnsDone = 0;
  startedAt = Date.now();
  clearInterval(timerId);
  timerId = setInterval(tickElapsed, 1000);
  tickElapsed();
  updateCounter();
}
function endRun() {
  clearInterval(timerId);
  timerId = null;
  startedAt = 0;
  $("eta").textContent = "";
}

// ---- 对话渲染 ----
function appendTopicHead(ev) {
  const empty = $("empty");
  if (empty) { empty.remove(); }
  const el = document.createElement("div");
  el.className = "topic-head";
  el.textContent = "[" + ev.index + "/" + ev.total + "] " + ev.title;
  $("chat").appendChild(el);
  $("chat").scrollTop = $("chat").scrollHeight;
}
function appendTurn(ev) {
  const empty = $("empty");
  if (empty) { empty.remove(); }
  const el = document.createElement("div");
  el.className = "msg " + (ev.side || "a");
  const s = document.createElement("span");
  s.className = "src";
  s.textContent = ev.speaker + (ev.side === "b" ? " · 挑战者" : " · 方案方");
  el.appendChild(s);
  el.appendChild(document.createTextNode(ev.text));
  const chat = $("chat");
  const atBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 60;
  chat.appendChild(el);
  if (atBottom) { chat.scrollTop = chat.scrollHeight; }
}

// ---- 蓝图渲染 ----
function renderBlueprint(payload) {
  bp = payload;
  $("step2").classList.remove("hidden");
  $("bpMeta").textContent =
    payload.title + " · " + payload.slug + (payload.origin === "mock" ? " · 模拟生成" : "");

  const host = $("personas");
  host.innerHTML = "";
  for (const p of payload.personas) {
    const box = document.createElement("div");
    box.className = "persona";
    const nm = document.createElement("div");
    nm.className = "nm"; nm.textContent = p.name;
    const tt = document.createElement("div");
    tt.className = "tt"; tt.textContent = p.title + " · 负责「" + p.stance + "」";
    const det = document.createElement("details");
    const sum = document.createElement("summary");
    sum.textContent = "看他拿到的完整 system message";
    const pre = document.createElement("pre");
    pre.textContent = p.system_message;
    det.appendChild(sum); det.appendChild(pre);
    box.appendChild(nm); box.appendChild(tt); box.appendChild(det);
    host.appendChild(box);
  }

  const topics = $("topics");
  topics.innerHTML = "";
  for (const t of payload.topics) {
    const lab = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = true; cb.dataset.key = t.key;
    lab.appendChild(cb);
    lab.appendChild(document.createTextNode(t.section_title));
    lab.title = t.brief;
    topics.appendChild(lab);
  }
  const fixed = document.createElement("label");
  fixed.className = "fixed";
  const fcb = document.createElement("input");
  fcb.type = "checkbox"; fcb.checked = true; fcb.disabled = true; fcb.dataset.fixed = "1";
  fixed.appendChild(fcb);
  fixed.appendChild(document.createTextNode(payload.closing.section_title + "（自动追加）"));
  topics.appendChild(fixed);

  // 蓝图生成的模式决定对谈模式的默认值：模拟蓝图就默认接着模拟。
  $("runMock").checked = payload.origin === "mock";

  $("bpView").innerHTML =
    mdToHtml(payload.markdown) +
    '<p class="dl"><a href="/blueprint.json">下载蓝图 .json（可手改）</a>' +
    '<a href="/blueprint.md">下载可读版 .md</a></p>';
  updateRunHint();
}

function selectedKeys() {
  return [...$("topics").querySelectorAll("input")]
    .filter((cb) => cb.checked && !cb.disabled)
    .map((cb) => cb.dataset.key);
}

function updateRunHint() {
  const on = $("runMock").checked;
  const n = selectedKeys().length;
  $("runHint").className = on ? "hint" : "hint cost";
  $("runHint").textContent = on
    ? "模拟模式：不调用模型、不联网、不花钱。产出是占位内容，仅用来验证流程。"
    : "真实模式：调用模型，按用量计费，而且慢——最多 " + n + " 个正文议题 × "
      + $("rounds").value + " 轮，每轮两次发言、每个议题一次整理，最后再压一次速查。"
      + "轮数是上限：挑战者提不出新反对意见的议题会提前收手。";
}

// ---- markdown：只覆盖 digest.py / blueprint.py 实际产出的语法 ----
// 先转义再做行内替换——正文是模型生成的，不转义就是给自己开 HTML 注入。
function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function inline(s) {
  return esc(s)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}
function mdToHtml(md) {
  const out = [];
  let list = null;
  let fence = false;
  const closeList = () => { if (list) { out.push("</" + list + ">"); list = null; } };
  for (const raw of String(md || "").split("\n")) {
    const line = raw.replace(/\s+$/, "");
    if (/^\s*```/.test(line)) {
      closeList();
      if (fence) { out.push("</pre>"); fence = false; }
      else { out.push("<pre>"); fence = true; }
      continue;
    }
    if (fence) { out.push(esc(raw)); continue; }
    if (!line.trim()) { closeList(); continue; }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      closeList();
      out.push("<h" + m[1].length + ">" + inline(m[2]) + "</h" + m[1].length + ">");
    } else if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      closeList(); out.push("<hr>");
    } else if ((m = line.match(/^\s*[-*]\s+\[([ xX])\]\s+(.*)$/))) {
      if (list !== "ul") { closeList(); out.push('<ul class="check">'); list = "ul"; }
      const on = m[1].toLowerCase() === "x" ? " checked" : "";
      out.push('<li><input type="checkbox" disabled' + on + ">" + inline(m[2]) + "</li>");
    } else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push("<li>" + inline(m[1]) + "</li>");
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push("<li>" + inline(m[1]) + "</li>");
    } else if ((m = line.match(/^>\s?(.*)$/))) {
      closeList(); out.push("<blockquote>" + inline(m[1]) + "</blockquote>");
    } else {
      closeList(); out.push("<p>" + inline(line) + "</p>");
    }
  }
  if (fence) { out.push("</pre>"); }
  closeList();
  return out.join("\n");
}

// ---- 成果 ----
let hbRaw = "", quickRaw = "", trRaw = "";
async function loadResults() {
  const [hb, qk, tr] = await Promise.all([
    (await fetch("/handbook")).json(),
    (await fetch("/quick")).json(),
    (await fetch("/transcript")).json(),
  ]);
  hbRaw = hb.markdown || "";
  quickRaw = qk.markdown || "";
  trRaw = tr.markdown || "";
  if (!hbRaw.trim() && !quickRaw.trim()) { return; }
  renderResults();
}
function renderResults() {
  const warn = $("runMock").checked
    ? '<div class="mockwarn">模拟模式：以下成果是占位内容，没有真实结论——不是渲染坏了。</div>'
    : "";
  let html = warn;
  if (quickRaw.trim()) { html += mdToHtml(quickRaw) + "<hr>"; }
  html += mdToHtml(hbRaw);
  html += '<p class="dl">';
  if (quickRaw.trim()) { html += '<a href="/quick.md">下载速查版 .md</a>'; }
  html += '<a href="/handbook.md">下载完整版 .md</a>';
  html += '<a href="/transcript.md">下载对话实录 .md</a>';
  html += '<a href="/blueprint.json">下载蓝图 .json</a>';
  html += "</p>";
  $("out").innerHTML = html;
}

// ---- 事件 ----
function handle(ev) {
  if (ev.seq <= lastSeq) { return; }
  lastSeq = ev.seq;

  if (ev.type === "forge_start") {
    jobKind = "forge";
    setRunning(true);       // 刷新页面时靠重放的历史事件恢复「运行中」状态
    bp = null;
    $("step2").classList.add("hidden");
    $("bpView").innerHTML = '<div class="empty">还没有蓝图。</div>';
    $("chat").innerHTML = '<div class="empty" id="empty">蓝图生成后才会开始对谈。</div>';
    $("out").innerHTML = '<div class="empty">跑完才有成果。</div>';
    $("progress").textContent = "";
    $("counter").textContent = "";
    turnsTotal = 0; turnsDone = 0;
    notice("");
    startedAt = Date.now();
    clearInterval(timerId);
    timerId = setInterval(tickElapsed, 1000);
    tickElapsed();
    setActivity("准备调用模型", true);
    setStatus("锻造中", "live");
    pickTab("chat");
  } else if (ev.type === "forge_stage") {
    setActivity(ev.stage === "personas" ? "正在设计两位专家" : "正在设计议题", true);
  } else if (ev.type === "forge_done") {
    renderBlueprint(ev.blueprint);
    setActivity("蓝图已生成");
    setStatus("蓝图就绪", "");
    $("progress").textContent = "✓ 蓝图已保存：" + ev.blueprint.path;
    notice(
      ev.blueprint.origin === "mock"
        ? "模拟模式：模型生成的人格/议题是占位内容；你自己写的人格不受影响。"
        : "",
      ev.blueprint.origin === "mock" ? "warn" : ""
    );
    endRun();
  } else if (ev.type === "forge_cancelled") {
    notice(ev.text || "已取消。", "warn");
    setActivity("已取消");
    setStatus("已取消", "warn");
    endRun();
    restoreBlueprint();
  } else if (ev.type === "run_start") {
    jobKind = "run";
    setRunning(true);
    $("chat").innerHTML = "";
    $("out").innerHTML = '<div class="empty">跑完才有成果。</div>';
    hbRaw = ""; quickRaw = ""; trRaw = "";
    notice("");
    $("progress").textContent = "";
    beginRun(ev.total, ev.rounds);
    setActivity("准备中", true);
    setStatus("运行中", "live");
    pickTab("chat");
    updateRunHint();
  } else if (ev.type === "topic_start") {
    appendTopicHead(ev);
    setActivity("正在生成下一位的发言", true);
    $("progress").textContent = "[" + ev.index + "/" + ev.total + "] " + ev.title;
    setStatus("对谈中", "live");
  } else if (ev.type === "topic_done") {
    if (ev.early) {
      $("progress").textContent += "　→ 挑战者已无新意见，第 " + ev.roundsRun + " 轮收手";
    }
  } else if (ev.type === "turn") {
    appendTurn(ev);
    turnsDone += 1;
    updateCounter();
    setActivity("正在生成下一位的发言", true);
  } else if (ev.type === "section_start") {
    setActivity(ev.closing ? "正在整理分歧备忘与自检清单" : "正在把这一节整理成成果", true);
    setStatus("整理中", "live");
    $("progress").textContent += ev.closing ? "　→ 整理附录中" : "　→ 整理成章节中";
  } else if (ev.type === "section_done") {
    setActivity("已整理好一节", true);
  } else if (ev.type === "quick_start") {
    setActivity("正在把整份成果压成一页速查", true);
    setStatus("压缩中", "live");
    $("progress").textContent += "　→ 正在生成一页速查";
  } else if (ev.type === "notice") {
    notice(ev.text, ev.kind === "warning" ? "bad" : "warn");
    if (ev.kind === "warning") { setStatus("出错了", "bad"); }
    setActivity("");
    // 锻造失败时，服务端那份旧蓝图还在；把它放回页面，别让用户以为丢了。
    if (ev.job === "forge" && ev.kind === "warning") { restoreBlueprint(); }
  } else if (ev.type === "run_done") {
    $("progress").textContent = "✓ 完成：" + ev.handbook;
    setStatus("完成", "");
    setActivity("完成");
    endRun();
    loadResults();
  } else if (ev.type === "done") {
    setRunning(false);
    endRun();
    if ($("activity").textContent.indexOf("正在") === 0) { setActivity(""); }
  }
}

function connect() {
  const es = new EventSource("/events");
  es.onmessage = (e) => handle(JSON.parse(e.data));
}

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return r.json();
}

// ---- 按钮 ----
$("forgeBtn").onclick = async () => {
  const purpose = $("purpose").value.trim();
  if (purpose.length < BOOT.minPurpose) {
    notice("目的太短了：至少 " + BOOT.minPurpose + " 字，要说清「给谁、解决什么、要什么」。", "bad");
    return;
  }

  // 前端先挡明显的漏填；权威校验在服务端（同一套 parse_persona_plan）。
  const personas = collectPersonas();
  for (const [side, spec] of Object.entries(personas)) {
    const label = side === "proposer" ? "第一位专家" : "第二位专家";
    if (spec.mode === "hint" && !spec.hint) {
      $("customBox").open = true;
      notice(label + "选了「给方向」，先写一两句方向。", "bad");
      return;
    }
    if (spec.mode === "manual") {
      const missing = ["name", "title", "stance", "system_message"].filter((k) => !spec[k]);
      if (missing.length) {
        $("customBox").open = true;
        notice(label + "的手写人格还没填完（称呼 / 身份 / 负责什么 / 人格设定）。", "bad");
        return;
      }
    }
  }

  setRunning(true);
  setActivity("启动中", true);
  const res = await post("/forge", {
    purpose: purpose,
    title: $("title").value.trim(),
    topics: parseInt($("topicCount").value, 10),
    mock: $("forgeMock").checked,
    personas: personas,
  });
  if (!res.ok) {
    setRunning(false);
    setActivity("");
    notice(res.why || "启动失败", "bad");
  }
};

$("runBtn").onclick = async () => {
  const keys = selectedKeys();
  if (!keys.length) {
    notice("至少要勾一个议题。", "bad");
    return;
  }
  setRunning(true);
  setActivity("启动中", true);
  const res = await post("/run", {
    keys: keys,
    rounds: parseInt($("rounds").value, 10),
    converge: $("converge").checked,
    mock: $("runMock").checked,
  });
  if (!res.ok) {
    setRunning(false);
    setActivity("");
    notice(res.why || "启动失败", "bad");
  } else if (res.warning) {
    notice(res.warning, "warn");
  }
};

$("stopBtn").onclick = async () => {
  setStatus("停止中…（会在当前这轮结束后收手）", "warn");
  await post("/stop");
};

$("runMock").onchange = updateRunHint;
$("rounds").onchange = updateRunHint;

// ---- Tab ----
function pickTab(which) {
  for (const w of ["chat", "bp", "out"]) {
    const on = w === which;
    $("#tab-" + w).className = "tab" + (on ? " on" : "");
    $("#pane-" + w).className = "pane" + (on ? " on" : "");
  }
}
$("tab-chat").onclick = () => pickTab("chat");
$("tab-bp").onclick = () => pickTab("bp");
$("tab-out").onclick = () => pickTab("out");

// 刷新页面时主动补一次状态：run 一开始 bus 就 reset 了，历史里不再有 forge_done；
// 不主动拉 /blueprint 的话，审阅面板会凭空消失。
async function restoreBlueprint() {
  if (bp) { return; }  // SSE 重放或上一次渲染已经在了，不必再拉一遍
  try {
    const r = await (await fetch("/blueprint")).json();
    if (r.ready) { renderBlueprint(r); }
  } catch (e) { /* 忽略：服务刚起、或请求被关掉 */ }
}
async function bootstrap() {
  await restoreBlueprint();
  try { await loadResults(); } catch (e) { /* 还没有成果，正常 */ }
  try {
    const h = await (await fetch("/health")).json();
    if (h.running) {
      jobKind = h.stage;
      setRunning(true);
      setStatus(h.stage === "forge" ? "锻造中" : "运行中", "live");
    }
  } catch (e) { /* 忽略 */ }
}
updateRunHint();
connect();
bootstrap();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 7. 入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="智能体人格与议题生成器 · 网页启动器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  .venv/bin/python launcher.py            # 起服务，然后开浏览器\n"
            "  .venv/bin/python launcher.py --mock     # 页面默认勾上模拟模式，不花钱\n"
        ),
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="页面上的「模拟模式」默认勾上（不调用真实模型）",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"端口，默认 {DEFAULT_PORT}（被占用就往后顺延）",
    )
    parser.add_argument("--no-browser", action="store_true", help="不要试着自动开浏览器")
    args = parser.parse_args(argv)

    # 已经有一个在跑？把浏览器指过去就完事，**不要再起第二个**。
    for port in range(args.port, args.port + 11):
        if _existing_instance(port):
            url = f"http://localhost:{port}/"
            print(f"启动器已经在跑了，直接把浏览器指过去：\n\n    {url}\n", flush=True)
            if not args.no_browser:
                _open_browser(url)
            return 0

    server = None
    for port in range(args.port, args.port + 11):
        try:
            server = _Server(("127.0.0.1", port), _Handler, mock_default=args.mock)
            break
        except OSError:
            continue
    if server is None:
        print(
            f"\n{args.port}–{args.port + 10} 都被占用了，用 --port 换一个。",
            file=sys.stderr,
        )
        return 2

    port = server.server_address[1]
    url = f"http://localhost:{port}/"
    # flush 是必须的：stdout 不是 TTY 时（`| tee`、后台跑）Python 会块缓冲。
    print(f"\n{'=' * 72}\n智能体人格与议题生成器已就绪：\n\n    {url}\n", flush=True)
    print(
        "正在打开浏览器……没反应就手动复制上面这个地址。"
        if not args.no_browser
        else "（--no-browser：不自动开浏览器，手动复制上面这个地址。）",
        flush=True,
    )
    if args.mock:
        print("（--mock：页面上的「模拟模式」默认已勾选，不花钱。）", flush=True)
    print(f"{'=' * 72}\n关掉这个窗口、或者按 Ctrl-C，都会停掉服务。", flush=True)

    if not args.no_browser:
        # socket 在 _Server(...) 里已经 bind+listen，浏览器连上来只会进 backlog。
        threading.Timer(0.4, _open_browser, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
