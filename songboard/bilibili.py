"""B 站直播弹幕监听。

不依赖「直播姬」进程，直连直播间长连接；只要在开播就能收到弹幕。
协议要点：16 字节包头 + 可选压缩包体，认证包 op=7，心跳 op=2 每 30s。
"""
from __future__ import annotations

import asyncio
import json
import random
import struct
import time
import urllib.parse
import urllib.request
import zlib
from typing import Any, Awaitable, Callable

try:
    import brotli  # type: ignore
except ImportError:  # pragma: no cover
    brotli = None  # type: ignore

import websockets

from .wbi import extract_keys, sign_query

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

HEADER = struct.Struct(">IHHII")
OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8
PROTO_JSON = 0
PROTO_HEARTBEAT = 0  # v1 前缀 op
PROTO_ZLIB = 2
PROTO_BROTLI = 3

DanmakuHandler = Callable[[dict[str, Any]], Awaitable[None]]
NoticeHandler = Callable[[str], Awaitable[None]]


# --------------------------------------------------------------------------- HTTP
def _http_json(url: str, params: dict[str, Any] | None = None, cookie: str = "") -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {
        "User-Agent": UA,
        "Referer": "https://live.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
    }
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


class WsHeaderCompat:
    """不同 websockets 版本的自定义请求头参数名不一样，这里探测一次。

    websockets >= 14 用 additional_headers，旧版本用 extra_headers。
    """

    _cached: str | None = None

    @classmethod
    def param(cls) -> str:
        if cls._cached:
            return cls._cached
        import inspect

        name = "additional_headers"
        try:
            sig = inspect.signature(websockets.connect)
            if "additional_headers" in sig.parameters:
                name = "additional_headers"
            elif "extra_headers" in sig.parameters:
                name = "extra_headers"
        except (TypeError, ValueError):  # pragma: no cover
            pass
        cls._cached = name
        return name

    @classmethod
    def kwargs(cls, headers: dict[str, str], *, timeout: float = 15) -> dict[str, Any]:
        """连接参数。

        关键：必须关掉 websockets 的协议层保活（ping_interval=None）。
        B 站弹幕服务器不响应 websocket ping 帧，开着的话库会每隔约 40 秒
        以 "keepalive ping timeout" 主动断开重连，正好把弹幕丢在重连窗口里。
        存活判断改用 B 站自己的心跳包（op=2 → 服务器回 op=3）。
        """
        return {
            cls.param(): headers,
            "open_timeout": timeout,
            "ping_interval": None,
            "close_timeout": 5,
        }

    @classmethod
    def flip(cls) -> None:
        cls._cached = "extra_headers" if cls.param() == "additional_headers" else "additional_headers"


def _ws_headers(cookie: str, origin: str) -> dict[str, str]:
    headers = {"User-Agent": UA, "Origin": origin}
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _as_int(value: Any) -> int:
    """把 B 站消息里的数值字段安全转成 int。

    为什么需要：同一个字段在不同消息里可能是 int / str / float / None，
    直接 int() 会抛异常，而异常发生在解包循环里会**掐断整个消息处理**。
    礼物金额算错会直接影响"送礼才能点歌"的门槛，所以这里必须稳。
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- 包体
def encode_packet(body: bytes | dict, op: int = OP_MESSAGE, proto: int = 1) -> bytes:
    """包头 16 字节：总长(4) 头长(2) 协议版本(2) 操作码(4) 序号(4)。

    注意 op 与 proto 是相邻参数，调用时一律写关键字，别用位置参数。
    """
    if isinstance(body, dict):
        body = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return HEADER.pack(HEADER.size + len(body), HEADER.size, proto, op, 1) + body


def iter_packets(data: bytes):
    """把一个（可能被压缩的）网络包拆成若干消息 dict。"""
    offset = 0
    while offset + HEADER.size <= len(data):
        total, header_len, proto, op, _seq = HEADER.unpack_from(data, offset)
        if total < header_len or offset + total > len(data):
            break
        payload = data[offset + header_len: offset + total]
        offset += total

        if op == OP_MESSAGE:
            if proto == PROTO_ZLIB:
                try:
                    payload = zlib.decompress(payload)
                except zlib.error:
                    continue
                yield from iter_packets(payload)
                continue
            if proto == PROTO_BROTLI:
                if brotli is None:
                    continue
                try:
                    payload = brotli.decompress(payload)
                except Exception:
                    continue
                yield from iter_packets(payload)
                continue
            try:
                parsed = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            # 压缩包解出来的是一个消息数组，普通包是单个消息对象
            if isinstance(parsed, list):
                for one in parsed:
                    if isinstance(one, dict):
                        yield one
            elif isinstance(parsed, dict):
                yield parsed
        elif op == OP_HEARTBEAT_REPLY:
            popularity = int.from_bytes(payload[:4], "big") if len(payload) >= 4 else 0
            yield {"cmd": "_HEARTBEAT", "popularity": popularity}
        elif op == OP_AUTH_REPLY:
            try:
                yield {"cmd": "_AUTH", **json.loads(payload.decode("utf-8") or "{}")}
            except (UnicodeDecodeError, json.JSONDecodeError):
                yield {"cmd": "_AUTH"}


# --------------------------------------------------------------------------- 监听器
class BilibiliDanmaku:
    def __init__(
        self,
        room_id: int,
        on_danmaku: DanmakuHandler,
        on_notice: NoticeHandler | None = None,
        *,
        cookie: str = "",
    ) -> None:
        self.input_room_id = int(room_id)
        self.room_id = int(room_id)
        self.on_danmaku = on_danmaku
        self.on_notice = on_notice
        self.cookie = cookie
        self.ws = None
        self.connected = False
        self.ever_connected = False
        self.popularity = 0
        self.last_heartbeat = 0.0
        self.danmaku_count = 0
        self.last_error = ""
        self._stop = asyncio.Event()
        self._uid = 0
        #: 房间信息（标题/主播/开播状态/人气），由 resolve_room() 填
        self.room_info: dict[str, Any] = {}

    async def log(self, msg: str) -> None:
        print(f"[danmaku] {msg}")
        if self.on_notice:
            try:
                await self.on_notice(msg)
            except Exception:
                pass

    # ---------- 握手 ----------
    def _resolve_room_sync(self) -> dict:
        return _http_json(
            "https://api.live.bilibili.com/room/v1/Room/room_init",
            {"id": self.input_room_id},
        )

    def _room_extra_sync(self) -> dict:
        """再问一次房间信息：标题、主播名、开播状态、人气。

        为什么要多花这一次请求：**房间号填错**（填成别人的房间）时，
        连接、认证、心跳全都正常，日志一片健康，就是永远收不到弹幕 ——
        这是实测最容易让人卡住的一种情况（实测：填了别人的房间，
        50 秒 0 条弹幕，连历史弹幕接口也是 0 条）。
        把房间名和开播状态显示出来，一眼就能看出"这不是我的房间"。
        """
        try:
            res = _http_json(
                "https://api.live.bilibili.com/room/v1/Room/get_info",
                {"room_id": self.room_id},
            )
        except Exception:  # noqa: BLE001
            return {}
        if res.get("code") != 0:
            return {}
        d = res.get("data") or {}
        return {
            "title": str(d.get("title") or ""),
            "uname": str(d.get("uname") or ""),
            "live_status": _as_int(d.get("live_status")),
            "online": _as_int(d.get("online")),
            "area": str(d.get("area_name") or ""),
        }

    async def resolve_room(self) -> bool:
        try:
            res = await asyncio.to_thread(self._resolve_room_sync)
        except Exception as exc:
            await self.log(f"房间号解析失败（网络问题？）：{exc!r}")
            return False
        if res.get("code") != 0:
            await self.log(f"房间号 {self.input_room_id} 无效：{res.get('message')}")
            return False
        data = res.get("data") or {}
        self.room_id = int(data.get("room_id") or self.input_room_id)
        self._uid = int(data.get("uid") or 0)
        self.room_info = await asyncio.to_thread(self._room_extra_sync)
        live = "正在直播" if data.get("live_status") == 1 else "未开播"
        title = self.room_info.get("title")
        who = self.room_info.get("uname")
        extra = (f"《{title}》" if title else "")
        if who:
            extra += f" 主播 {who}"
        await self.log(f"房间 {self.room_id} {extra}（{live}），主播 uid={self._uid}")
        if data.get("live_status") != 1:
            await self.log("⚠️ 这个房间没在直播 —— 没开播就没有弹幕流，"
                           "连上了也一条都收不到（这是 B 站的机制，不是故障）")
        return True

    def _danmu_info_sync(self) -> dict:
        params: dict[str, Any] = {"id": self.room_id, "type": 0}
        last_err = ""
        # 带 WBI 签名
        try:
            nav = _http_json("https://api.bilibili.com/x/web-interface/nav", cookie=self.cookie)
            keys = extract_keys(nav.get("data") or {})
            if keys:
                query = sign_query(params, keys[0], keys[1])
                res = _http_json(
                    "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo"
                    f"?{query}",
                    cookie=self.cookie,
                )
                if res.get("code") == 0 and (res.get("data") or {}).get("token"):
                    return res
                last_err = f"wbi 签名请求返回 {res.get('code')} {res.get('message')}"
        except Exception as exc:
            last_err = repr(exc)
        # 退化：不带签名（部分时段仍可用）
        res = _http_json(
            "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo",
            params, cookie=self.cookie,
        )
        if res.get("code") != 0:
            raise RuntimeError(
                f"getDanmuInfo 失败：{res.get('code')} {res.get('message')}（前次：{last_err}）"
            )
        return res

    async def fetch_token(self) -> tuple[str, list[dict]]:
        res = await asyncio.to_thread(self._danmu_info_sync)
        data = res.get("data") or {}
        token = data.get("token") or ""
        hosts = data.get("host_list") or []
        if not token or not hosts:
            raise RuntimeError("getDanmuInfo 未返回 token/host_list")
        return token, hosts

    # ---------- 连接 ----------
    async def run(self) -> None:
        """带重连的主循环。"""
        backoff = 3
        while not self._stop.is_set():
            try:
                if not await self.resolve_room():
                    await self._sleep_or_stop(30)
                    continue
                token, hosts = await self.fetch_token()
                host = random.choice(hosts[:3])
                url = f"wss://{host['host']}:{host.get('wss_port', 443)}/sub"
                await self.log(f"连接弹幕服务器 {host['host']} …")

                ws = await self._connect(url)
                try:
                    self.ws = ws
                    auth = {
                        "uid": 0,
                        "roomid": self.room_id,
                        "protover": 3,
                        "platform": "web",
                        "type": 2,
                        "key": token,
                        "buvid": "",
                    }
                    await ws.send(encode_packet(auth, op=OP_AUTH, proto=1))
                    self.last_heartbeat = time.time()
                    hb = asyncio.create_task(self._heartbeat_loop(ws))
                    wd = asyncio.create_task(self._stale_watchdog(ws))
                    backoff = 3
                    try:
                        async for raw in ws:
                            if isinstance(raw, str):
                                raw = raw.encode("utf-8")
                            for msg in iter_packets(raw):
                                await self._dispatch(msg)
                    finally:
                        hb.cancel()
                        wd.cancel()
                finally:
                    self.connected = False
                    self.ws = None
                    try:
                        await ws.close()
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = repr(exc)
                await self.log(f"连接中断：{exc!r}，{backoff}s 后重连")
                await self._sleep_or_stop(backoff)
                backoff = min(backoff * 2, 60)

    async def _connect(self, url: str):
        """连接弹幕服务器，自动适配 websockets 新旧版本的请求头参数。"""
        headers = _ws_headers(self.cookie, "https://live.bilibili.com")
        try:
            return await websockets.connect(url, **WsHeaderCompat.kwargs(headers))
        except TypeError as exc:
            # 参数名不兼容（版本差异），换一个再试一次
            self.last_error = repr(exc)
            WsHeaderCompat.flip()
            await self.log(f"请求头参数名不兼容，改用 {WsHeaderCompat.param()} 重试")
            return await websockets.connect(url, **WsHeaderCompat.kwargs(headers))

    async def _heartbeat_loop(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(30)
                await ws.send(encode_packet(b"", op=OP_HEARTBEAT, proto=1))
        except (asyncio.CancelledError, Exception):
            return

    async def _stale_watchdog(self, ws, *, limit: float = 95) -> None:
        """兜底：B 站心跳连续多次没回，说明连接已经死了，主动重连。

        正常情况下约 30 秒会收到一次心跳回包（op=3）。静默房间也是这个节奏，
        所以「超过 limit 秒没有回包」能可靠区分「房间安静」和「连接已死」。
        """
        try:
            while True:
                await asyncio.sleep(15)
                quiet = time.time() - (self.last_heartbeat or time.time())
                if quiet > limit:
                    await self.log(f"心跳 {quiet:.0f}s 无回应，判定连接已死，主动重连")
                    self.last_error = f"心跳超时 {quiet:.0f}s"
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
        except (asyncio.CancelledError, Exception):
            return

    async def _dispatch(self, msg: dict) -> None:
        cmd = str(msg.get("cmd", ""))
        if cmd == "_AUTH":
            self.connected = True
            self.ever_connected = True
            await self.log("弹幕连接已建立 ✅")
            return
        if cmd == "_HEARTBEAT":
            self.popularity = int(msg.get("popularity") or 0)
            self.last_heartbeat = time.time()
            return

        if cmd.startswith("DANMU_MSG"):
            info = msg.get("info") or []
            try:
                text = info[1]
                user = (info[2] or [])
                uid = int(user[0]) if user else 0
                uname = str(user[1]) if len(user) > 1 else "未知"
                fans_medal = None
                medal = (info[3] or [])
                if medal and isinstance(medal, list) and len(medal) > 1 and medal[1]:
                    fans_medal = {"name": medal[1], "level": medal[0]}
            except (IndexError, TypeError, ValueError):
                return
            self.danmaku_count += 1
            await self.on_danmaku({
                "type": "danmaku", "text": str(text), "user": uname,
                "uid": uid, "medal": fans_medal, "raw": msg,
            })
            return

        if cmd.startswith("SUPER_CHAT_MESSAGE"):
            data = msg.get("data") or {}
            user = (data.get("user_info") or {})
            await self.on_danmaku({
                "type": "super_chat", "text": str(data.get("message") or ""),
                "user": str(user.get("uname") or "未知"),
                "uid": int(data.get("uid") or 0),
                "price": data.get("price"), "raw": msg,
            })
            return

        if cmd.startswith("SEND_GIFT"):
            data = msg.get("data") or {}
            # ⚠️ 礼物的金额必须算对，否则"送礼才能点歌"的门槛形同虚设：
            #   * price      单个礼物的价格（金瓜子）
            #   * num        数量
            #   * total_coin 有时才有（连击/聚合消息里）
            #   * paid       免费礼物（银瓜子送的）是 False，必须过滤掉，
            #                否则观众送个免费辣条就拿到点歌资格
            price = _as_int(data.get("price"))
            num = _as_int(data.get("num")) or 1
            total = _as_int(data.get("total_coin"))
            if total <= 0:
                total = price * num
            await self.on_danmaku({
                "type": "gift", "text": "", "user": str(data.get("uname") or "未知"),
                "uid": _as_int(data.get("uid")),
                "gift": {
                    "name": str(data.get("giftName") or ""),
                    "num": num,
                    "price": price,
                    "total_coin": total,
                    "paid": bool(data.get("paid")),
                    "guard_level": _as_int(data.get("guard_level")),
                    "combo": bool(data.get("combo_gift") or data.get("batch_combo_id")),
                },
                "raw": msg,
            })
            return

        if cmd.startswith("GUARD_BUY"):
            data = msg.get("data") or {}
            # 舰长价格随等级不同，用 price*num；拿不到就按 num 记
            price = _as_int(data.get("price"))
            num = _as_int(data.get("num")) or 1
            await self.on_danmaku({
                "type": "guard", "text": "",
                "user": str(data.get("username") or "未知"),
                "uid": _as_int(data.get("uid")),
                "guard": {
                    "level": _as_int(data.get("guard_level")),
                    "num": num,
                    "total_coin": price * num if price > 0 else 0,
                    "name": str(data.get("gift_name") or ""),
                },
                "raw": msg,
            })
            return

        # ⚠️ 连击礼物走的是另一个 cmd（COMBO_SEND）。
        # 不处理的话"连送 10 个"只会按 1 个记账，门槛会算少。
        # 它的字段名和 SEND_GIFT 不完全一样（uid/uname 可能为 0/空）。
        if cmd.startswith("COMBO_SEND"):
            data = msg.get("data") or {}
            price = _as_int(data.get("price"))
            num = _as_int(data.get("combo_num")) or _as_int(data.get("gift_num")) or 1
            await self.on_danmaku({
                "type": "gift", "text": "",
                "user": str(data.get("uname") or "未知"),
                "uid": _as_int(data.get("uid")),
                "gift": {
                    "name": str(data.get("gift_name") or ""),
                    "num": num,
                    "price": price,
                    "total_coin": price * num,
                    "paid": bool(data.get("paid", True)),
                    "guard_level": _as_int(data.get("guard_level")),
                    "combo": True,
                },
                "raw": msg,
            })
            return

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        return {
            "mode": "live", "room_id": self.room_id, "connected": self.connected,
            "ever_connected": self.ever_connected, "popularity": self.popularity,
            "danmaku_count": self.danmaku_count, "last_error": self.last_error,
            # 房间名/开播状态：诊断"为什么收不到弹幕"最关键的两个信息
            "room": dict(self.room_info),
        }


# --------------------------------------------------------------------------- 演示模式
DEMO_SONGS = [
    "稻香", "起风了", "晴天花", "漠河舞厅", "小城夏天", "海阔天空", "夜空中最亮的星",
    "孤勇者", "蜜雪冰城", "すずめ", "カタオモイ", "Lemon", "春天里", "平凡之路",
    "成都", "句号", "罗刹海市", "海底", "悬溺", "11", "易燃易爆炸",
]
DEMO_USERS = ["路过的骑士", "爱吃辣的猫", "夜航船", "三千", "阿岚", "小满", "Kirara", "老张"]


class DemoDanmaku:
    """不连网络，按随机间隔生成点歌弹幕，用来预览点歌板。"""

    def __init__(self, on_danmaku: DanmakuHandler, on_notice: NoticeHandler | None = None) -> None:
        self.on_danmaku = on_danmaku
        self.on_notice = on_notice
        self.connected = True
        self.danmaku_count = 0
        self.last_error = ""
        self._stop = asyncio.Event()

    async def run(self) -> None:
        if self.on_notice:
            await self.on_notice("演示模式：本地随机生成弹幕，不连接直播间")
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=random.uniform(2.5, 6.0))
                break
            except asyncio.TimeoutError:
                pass
            roll = random.random()
            user = random.choice(DEMO_USERS)
            if roll < 0.72:
                text = f"点歌 {random.choice(DEMO_SONGS)}"
            elif roll < 0.80:
                text = "切歌"
            elif roll < 0.86:
                text = "老板大气"
            elif roll < 0.93:
                text = f"#{random.choice(DEMO_USERS).lower()}"
            else:
                text = "点歌 稻香"  # 触发重复点歌，验证去重提示
            self.danmaku_count += 1
            await self.on_danmaku({
                "type": "danmaku", "text": text, "user": user,
                "uid": abs(hash(user)) % 100000, "medal": None, "raw": {},
            })

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        return {
            "mode": "demo", "room_id": 0, "connected": self.connected,
            "popularity": 0, "danmaku_count": self.danmaku_count, "last_error": "",
        }
