"""内置 HTTP 服务：静态叠加层 + 控制台 + WebSocket 实时推送。

只用标准库（http.server + 手写 RFC6455），避免额外安装依赖。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --------------------------------------------------------------------------- WebSocket 帧
def _ws_encode(payload: bytes, opcode: int = 0x1) -> bytes:
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + payload


def _ws_read(rfile) -> tuple[int, bytes]:
    """读一帧。返回 (opcode, payload)；opcode 0x8 表示关闭。"""
    hdr = rfile.read(2)
    if len(hdr) < 2:
        return 0x8, b""
    opcode = hdr[0] & 0x0F
    masked = hdr[1] & 0x80
    length = hdr[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", rfile.read(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", rfile.read(8))[0]
    mask = rfile.read(4) if masked else b""
    data = rfile.read(length) if length else b""
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return opcode, data


# --------------------------------------------------------------------------- Hub
class WsHub:
    """管理叠加层/控制台的 WebSocket 连接，负责广播与处理客户端消息。"""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.clients: set[Any] = set()
        self.lock = threading.Lock()
        self.on_client_message = None  # async callable(dict) -> None
        self._snapshot_provider = lambda: {}

    def set_snapshot_provider(self, fn) -> None:
        self._snapshot_provider = fn

    def register(self, wfile) -> None:
        with self.lock:
            self.clients.add(wfile)

    def unregister(self, wfile) -> None:
        with self.lock:
            self.clients.discard(wfile)

    def broadcast(self, payload: dict) -> None:
        """线程安全：从任意线程调用。"""
        data = _ws_encode(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        with self.lock:
            targets = list(self.clients)
        dead = []
        for wf in targets:
            try:
                wf.write(data)
                wf.flush()
            except Exception:
                dead.append(wf)
        if dead:
            with self.lock:
                for wf in dead:
                    self.clients.discard(wf)


# --------------------------------------------------------------------------- Handler
class BoardHandler(BaseHTTPRequestHandler):
    server_version = "SongBoard/0.1"
    web_root: Path
    hub: WsHub | None = None
    ctx: dict[str, Any] = {}

    # 必须声明 HTTP/1.1，否则 101 升级响应不带 Connection 头，WebSocket 握手会失败
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # 静音默认访问日志
        if os.environ.get("SONGBOARD_VERBOSE"):
            print(f"[http] {fmt % args}")

    # ---------- helpers ----------
    def _send_json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, ctype: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404, "not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _run_async(self, coro):
        """把协程丢回主事件循环执行并等结果。"""
        loop: asyncio.AbstractEventLoop = self.ctx["loop"]
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=20)

    def _snapshot(self) -> dict:
        return self.ctx["snapshot"]()

    # ---------- routes ----------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        root: Path = self.web_root

        if route in ("/", "/overlay", "/overlay.html"):
            return self._send_file(root / "overlay.html", "text/html; charset=utf-8")
        if route in ("/control", "/control.html"):
            return self._send_file(root / "control.html", "text/html; charset=utf-8")
        if route == "/ws":
            return self._handle_ws()
        if route == "/api/state":
            return self._send_json(self._snapshot())
        if route == "/api/config":
            cfg = dict(self.ctx["config"].as_dict())
            netease = dict(cfg.get("netease") or {})
            if netease.get("cookie"):
                netease["cookie"] = "***"
            cfg["netease"] = netease
            return self._send_json(cfg)
        if route == "/api/netease/test":
            return self._send_json(self._run_async(self.ctx["netease_test"]()))
        if route == "/api/status":
            return self._send_json({
                "status": self.ctx["status"](),
                "log": list(self.ctx.get("log", []))[-200:],
            })
        if route == "/api/log":
            return self._send_json({"lines": list(self.ctx.get("log", []))[-200:]})
        return self.send_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._send_json({"ok": False, "error": "bad json"}, 400)

        store = self.ctx["store"]
        try:
            if parsed.path == "/api/add":
                item, result = self._run_async(store.add(
                    str(body.get("song", "")), str(body.get("user", "主播")),
                    int(body.get("uid", 0) or 0), source="control",
                    force=bool(body.get("force", False)),
                ))
                return self._send_json({"ok": result in ("ok", "queue_full"), "result": result,
                                        "item": item.to_dict()})
            if parsed.path == "/api/next":
                item = self._run_async(store.next(reason=str(body.get("reason", "skip"))))
                return self._send_json({"ok": True, "current": item.to_dict() if item else None})
            if parsed.path == "/api/remove":
                ok = self._run_async(store.remove(str(body.get("id", ""))))
                return self._send_json({"ok": ok})
            if parsed.path == "/api/clear":
                self._run_async(store.clear())
                return self._send_json({"ok": True})
            if parsed.path == "/api/move":
                ok = self._run_async(store.move(str(body.get("id", "")), bool(body.get("top", True))))
                return self._send_json({"ok": ok})
            if parsed.path == "/api/extapi":
                res = self._run_async(self.ctx["set_extapi"](
                    bool(body.get("enabled", False)), str(body.get("url", "")),
                ))
                return self._send_json(res)
            if parsed.path == "/api/extapi/probe":
                res = self._run_async(self.ctx["extapi_probe"](str(body.get("url", ""))))
                return self._send_json(res)
            if parsed.path == "/api/auto_next":
                self._run_async(self.ctx["set_auto_next"](bool(body.get("enabled", False))))
                return self._send_json({"ok": True})
            if parsed.path == "/api/duration":
                ok = self._run_async(self.ctx["set_duration"](float(body.get("seconds", 0) or 0)))
                return self._send_json({"ok": ok})
            if parsed.path == "/api/mark_done":
                return self._send_json(self._run_async(self.ctx["mark_done"]()))
            if parsed.path == "/api/simulate":
                # kind 允许模拟弹幕以外的付费事件（gift / guard / super_chat），
                # 这样调"送礼物才能点歌"的门槛时不用真的去送礼
                res = self._run_async(self.ctx["simulate_danmaku"](
                    str(body.get("text", "")), str(body.get("user", "测试观众")),
                    kind=str(body.get("kind", "danmaku")),
                    coin=int(body.get("coin", 0) or 0),
                    paid=bool(body.get("paid", True)),
                ))
                return self._send_json(res)
            if parsed.path == "/api/mode":
                mode = str(body.get("mode", ""))
                if mode in ("live", "demo"):
                    self._run_async(self.ctx["set_mode"](mode))
                    return self._send_json({"ok": True, "mode": mode})
                return self._send_json({"ok": False, "error": "mode 只能是 live/demo"}, 400)
            if parsed.path == "/api/room":
                room = int(body.get("room_id", 0) or 0)
                self._run_async(self.ctx["set_room"](room))
                return self._send_json({"ok": True, "room_id": room})
            if parsed.path == "/api/netease/enable":
                # ⚠️ 只允许开关"搜索/查时长"和 cookie。
                # 歌单写入相关（auto_add / playlist_id）**故意不再暴露**：
                # 现在只插播放队列，控制器上也没有对应入口了，
                # 留着这个口子就等于留了一条"误开写歌单"的路。
                enabled = bool(body.get("enabled", False))
                was = bool(self.ctx["config"].get("netease.enabled", False))
                self.ctx["config"]["netease"]["enabled"] = enabled
                if body.get("cookie"):
                    self.ctx["config"]["netease"]["cookie"] = str(body["cookie"])
                self.ctx["config"].save()
                # 保存后立刻验一次 cookie，并把结果回给控制台 ——
                # 不然主播只能盯着日志猜"我粘的 cookie 到底对不对"。
                result = self._run_async(self.ctx["reload_netease"]())
                if was != enabled:
                    self.ctx["log_change"](
                        "网易云搜索/查时长：" + ("已开启" if enabled else "已关闭"))
                ok, msg = result if isinstance(result, tuple) else (True, "")
                return self._send_json({"ok": True, "enabled": enabled,
                                        "search_ok": bool(ok), "message": msg})
            if parsed.path == "/api/gift_gate":
                # 礼物门槛：规则由主播在控制台里配。
                # 只接受白名单字段，且做强类型转换 —— 前端传来的都是字符串，
                # 直接写进配置会让 GiftLedger 里 int()/bool() 出错。
                cfg = self.ctx["config"]
                gg = cfg["gift_gate"]
                if "enabled" in body:
                    gg["enabled"] = bool(body["enabled"])
                if "mode" in body:
                    mode = str(body["mode"])
                    if mode not in ("min_total", "any_paid", "per_send",
                                    "guard_only"):
                        return self._send_json(
                            {"ok": False, "error": f"未知模式: {mode}"}, 400)
                    gg["mode"] = mode
                for key in ("min_coin", "window_seconds", "sc_min_coin",
                            "guard_min_level"):
                    if key in body:
                        try:
                            gg[key] = max(0, int(float(body[key] or 0)))
                        except (TypeError, ValueError):
                            return self._send_json(
                                {"ok": False, "error": f"{key} 不是数字"}, 400)
                for key in ("require_paid", "guard_always_ok", "sc_always_ok"):
                    if key in body:
                        gg[key] = bool(body[key])
                cfg.save()
                self.ctx["log_change"](
                    "礼物门槛：" + ("已开启" if gg.get("enabled") else "已关闭")
                    + f"（模式={gg.get('mode')}，门槛={gg.get('min_coin')} 瓜子）"
                )
                # 让 App 里的账本立刻按新配置工作
                reload_gate = self.ctx.get("reload_gift_gate")
                if reload_gate:
                    reload_gate()
                return self._send_json({"ok": True, "gift_gate": dict(gg)})
            if parsed.path == "/api/gift_gate/reset":
                reset = self.ctx.get("reset_gift_gate")
                if reset:
                    reset()
                self.ctx["log_change"]("礼物门槛：贡献记录已清空")
                return self._send_json({"ok": True})
        except Exception as exc:
            return self._send_json({"ok": False, "error": repr(exc)}, 500)
        return self.send_error(404, "not found")

    # ---------- websocket ----------
    def _handle_ws(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or self.hub is None:
            return self.send_error(400, "websocket only")
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()
        ).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        self.hub.register(self.wfile)
        try:
            self.wfile.write(_ws_encode(json.dumps(
                {"event": "hello", "snapshot": self._snapshot()}, ensure_ascii=False
            ).encode("utf-8")))
            self.wfile.flush()
            while True:
                opcode, data = _ws_read(self.rfile)
                if opcode == 0x8:
                    break
                if opcode == 0x9:  # ping -> pong
                    self.wfile.write(_ws_encode(data, 0xA))
                    self.wfile.flush()
                    continue
                if opcode == 0x1 and data:
                    try:
                        msg = json.loads(data.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    handler = self.hub.on_client_message
                    if handler:
                        self._run_async(handler(msg))
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            self.hub.unregister(self.wfile)


# --------------------------------------------------------------------------- 服务器
class BoardServer:
    def __init__(self, host: str, port: int, web_root: Path, ctx: dict[str, Any]) -> None:
        self.host = host
        self.port = port
        self.web_root = Path(web_root)
        self.ctx = ctx
        self.loop = ctx["loop"]
        self.hub = WsHub(self.loop)
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        handler = type("Handler", (BoardHandler,), {
            "web_root": self.web_root, "hub": self.hub, "ctx": self.ctx,
        })
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    def broadcast(self, payload: dict) -> None:
        self.hub.broadcast(payload)
