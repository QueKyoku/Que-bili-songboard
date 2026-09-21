"""外部媒体信息源适配器。

有些工具能从播放器里**精确读出进度**（网易云客户端本身不给，这是我方
窗口标题方案的硬伤）。已知的如 Metabox-Nexus-PlayerCap、now-playing-service
这类服务，会在本机开一个 HTTP/WebSocket 接口，返回当前曲目 + 播放进度。

本模块只做一件事：**轮询对方的一个 HTTP JSON 接口，把结果归一化成 MediaInfo**，
喂给主程序已有的"进度归属"逻辑。不安装、不管理、不修改对方程序。

字段名各家不同，所以：
  · 默认按常见键名**自动识别**（title/song/name、artist/singer、progress/position、duration…）
  · 也支持在 config.json 里写死字段路径，例如
        "fields": {"title": "data.song.name", "position": "data.progress", "duration": "data.duration"}
  · 认不出来就返回 None，主程序自动回退到窗口标题方案，不会崩。

⚠️ 注意：外部服务提供的进度会被当作"确定属于当前歌曲"（因为它是直接的播放器读数），
   所以它会**直接参与自动切歌**。前提是你确认那个服务真的是在读音乐播放器。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

from .media import MediaInfo

# 候选键名（按优先级），自动识别时按顺序找第一个存在的。
# ⚠️ name 必须排在 title 前面：Metabox-Nexus-PlayerCap 的 song_info 同时给了
#    name="句号" 和 title="句号 - G.E.M.邓紫棋"（合成串），拿 title 当歌名
#    会让歌名匹配永远失败。track 这类也是「纯歌名」语义。
TITLE_KEYS = ("songname", "song_name", "name", "song", "trackname", "track", "musicname", "title")
ARTIST_KEYS = ("artist", "singer", "artists", "author", "performer")
POSITION_KEYS = ("position", "progress", "current", "currenttime", "current_time",
                 "elapsed", "played", "playtime", "play_time", "curtime", "time",
                 "positionms", "progressms")
DURATION_KEYS = ("duration", "totaltime", "total_time", "total", "length", "endtime",
                 "end_time", "totalseconds", "durationms", "totalduration")
PLAYING_KEYS = ("playing", "isplaying", "is_playing", "playingstatus", "status", "state",
                "playstatus", "play_status")

_HHMMSS = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:\.(\d+))?$")


def _time_to_seconds(value: Any, *, key: str, default_unit: float = 1.0) -> float:
    """把各种形态的时间转成秒。

    支持：数字（按 key 名判断毫秒/秒）、"3:45"、"01:02:03.5"、None。
    """
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        num = float(value)
        if num <= 0:
            return 0.0
        lk = key.lower()
        if "ms" in lk or "millis" in lk:
            return num / 1000.0
        # key 名没说是毫秒，但数值大得离谱 → 按毫秒处理（比如 191000）
        if num > 20000:
            return num / 1000.0
        return num * default_unit
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        m = _HHMMSS.match(text)
        if m:
            hh, mm, ss, frac = m.groups()
            total = int(mm or 0) * 60 + int(ss or 0)
            if hh:
                total += int(hh) * 3600
            if frac:
                total += float("0." + frac)
            return float(total)
        try:
            return _time_to_seconds(float(text), key=key, default_unit=default_unit)
        except ValueError:
            return 0.0
    return 0.0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "playing", "play", "yes", "on", "resumed")
    return False


def _walk(obj: Any, prefix: str = "") -> dict[str, Any]:
    """把嵌套 dict 摊平成 {"a.b.c": value}，方便按路径取值。"""
    flat: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                flat.update(_walk(v, key))
            else:
                flat[key] = v
    return flat


def _unwalk(flat: dict[str, Any]) -> dict[str, Any]:
    """_walk 的反操作，把 {"a.b": 1} 还原成 {"a": {"b": 1}}。"""
    out: dict[str, Any] = {}
    for path, value in flat.items():
        parts = path.split(".")
        cur = out
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        cur[parts[-1]] = value
    return out


def _find(flat: dict[str, Any], keys: tuple[str, ...], *, allow_suffix: bool = True) -> tuple[str, Any]:
    """在摊平后的字典里找第一个命中的键。返回 (键路径, 值)。

    先按 keys 的顺序逐个别名做精确匹配（忽略大小写、下划线、连字符），
    全部精确匹配都落空后才退到包含匹配——保证 name 优先于 title 这类别名优先级。
    """
    def norm(s: str) -> str:
        return s.lower().replace("_", "").replace("-", "")

    # 1) 精确匹配（按别名优先级）
    for want in keys:
        for path, value in flat.items():
            leaf = path.rsplit(".", 1)[-1]
            if norm(leaf) == norm(want):
                return path, value
    # 2) 包含匹配（例如 data.songName、info.currentTime）
    if allow_suffix:
        for want in keys:
            for path, value in flat.items():
                leaf = norm(path.rsplit(".", 1)[-1])
                if norm(want) in leaf:
                    return path, value
    return "", None


def parse_external_payload(
    payload: Any, fields: dict[str, str] | None = None,
) -> MediaInfo | None:
    """把外部服务的 JSON 归一化成 MediaInfo。认不出曲名就返回 None。

    兼容常见的两种形态：
      · 裸对象：{"title": "起风了", "position": 60, ...}
      · 信封对象：{"code":0, "msg":"success", "player":"cloudmusicv3", "data": {...}}
        （Metabox-Nexus-PlayerCap 就是这种，真实数据在 data 里）
    """
    if payload is None:
        return None
    if isinstance(payload, list):
        # 有些服务返回 [ {…} ] 或最近一条状态
        if not payload:
            return None
        payload = payload[-1] if isinstance(payload[-1], dict) else payload[0]
    if not isinstance(payload, dict):
        return None

    # 拆信封：data/payload/result 里是真正的内容。
    # 注意不能无条件替换——只有当外层确实没有曲名时才下钻，
    # 否则会丢掉 data 之外也可能存在的字段。
    outer_flat = _walk(payload)
    def _has_title(flat: dict[str, Any]) -> bool:
        _k, v = _find(flat, TITLE_KEYS)
        return isinstance(v, str) and bool(v.strip())

    if not _has_title(outer_flat):
        for wrapper in ("data", "payload", "result", "song", "current"):
            inner = payload.get(wrapper)
            if isinstance(inner, dict) and inner:
                inner_flat = _walk(inner)
                if _has_title(inner_flat):
                    # 信封外的状态字段（如 status_update）合并进来做兜底
                    merged = dict(inner_flat)
                    for k, v in outer_flat.items():
                        if not k.startswith(wrapper + ".") and k != wrapper:
                            merged.setdefault(k, v)
                    payload = _unwalk(merged)
                    break
            elif isinstance(inner, list) and inner and isinstance(inner[-1], dict):
                payload = inner[-1]
                break

    flat = _walk(payload)
    fields = fields or {}

    def pick(key: str, keys: tuple[str, ...]) -> tuple[str, Any]:
        manual = fields.get(key)
        if manual:
            if manual in flat:
                return manual, flat[manual]
            # 支持 "a.b" 直接深取
            cur: Any = payload
            for part in manual.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    cur = None
                    break
            return manual, cur
        return _find(flat, keys)

    _tk, title = pick("title", TITLE_KEYS)
    if not isinstance(title, str) or not title.strip():
        # 没有歌名，但**有时长/进度**时也要保留：
        # 有些接口（如 PlayerCap 的 /all_lyrics）只给进度不给纯歌名，
        # 丢掉它就拿不到进度了。这类记录由合并逻辑补歌名。
        pk0, pos0 = pick("position", POSITION_KEYS)
        dk0, dur0 = pick("duration", DURATION_KEYS)
        if dur0 is not None or pos0 is not None:
            sk0, playing0 = pick("playing", PLAYING_KEYS)
            return MediaInfo(
                app="extapi", title="", artist="",
                playing=_truthy(playing0) if sk0 else True,
                position=_time_to_seconds(pos0, key=pk0 or "position"),
                duration=_time_to_seconds(dur0, key=dk0 or "duration"),
                source="extapi",
            )
        return None
    title = title.strip()

    _ak, artist = pick("artist", ARTIST_KEYS)
    if not isinstance(artist, str):
        artist = "" if artist is None else str(artist)
    # artist 可能是数组
    if isinstance(pick("artist", ARTIST_KEYS)[1], list):
        artist = "/".join(str(x) for x in pick("artist", ARTIST_KEYS)[1])

    pk, pos_raw = pick("position", POSITION_KEYS)
    dk, dur_raw = pick("duration", DURATION_KEYS)
    sk, playing_raw = pick("playing", PLAYING_KEYS)

    position = _time_to_seconds(pos_raw, key=pk or "position")
    duration = _time_to_seconds(dur_raw, key=dk or "duration")
    if duration and position > duration * 1.5:
        # 位置比总时长还大很多，说明单位判断错了，重算一次
        position = _time_to_seconds(pos_raw, key="positionms" if "ms" not in pk.lower() else "position")

    playing = _truthy(playing_raw) if sk else True
    # status/state 这类字段如果是 "paused" 字符串，_truthy 会自然返回 False
    return MediaInfo(
        app="extapi", title=title, artist=artist,
        playing=playing, position=position, duration=duration, source="extapi",
    )


# ---------------------------------------------------------------- 轮询
class ExtApiSource:
    """轮询一个或多个本机 HTTP JSON 接口，合并成一条曲目信息。

    为什么支持多个 URL：实测 Metabox-Nexus-PlayerCap 把信息拆在两个接口里——
      · /cloudmusicv3/song_info  → name（纯歌名）、singer，**没有时长**
      · /cloudmusicv3/all_lyrics → title（"歌名 - 歌手"）、duration、position，**没有纯歌名**
    单靠任何一个都拼不出完整信息，所以要合并。

    任何异常都吞掉并退化为"没数据"，绝不影响主程序。
    """

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self.last_ok = 0.0
        self.last_error = ""
        self.last_payload: Any = None
        self.hits = 0
        self._merged: MediaInfo | None = None
        self._sources: dict[str, Any] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("extapi.enabled", False))

    @property
    def urls(self) -> list[str]:
        """支持 extapi.url 单个字符串，或 extapi.urls 多个。"""
        many = self.cfg.get("extapi.urls", None) or []
        if isinstance(many, str):
            many = [many]
        one = str(self.cfg.get("extapi.url", "") or "")
        out = [u for u in [one, *many] if u]
        # 去重且保序
        seen: set[str] = set()
        return [u for u in out if not (u in seen or seen.add(u))]

    @urls.setter
    def urls(self, value: list[str]) -> None:
        self.cfg["extapi"]["urls"] = list(value)
        self.cfg["extapi"]["url"] = ""

    def _fetch_sync(self, url: str) -> Any:
        req = urllib.request.Request(
            url, headers={"User-Agent": "songboard/0.1", "Accept": "application/json"},
        )
        timeout = float(self.cfg.get("extapi.timeout", 3) or 3)
        limit = int(self.cfg.get("extapi.max_bytes", 8388608) or 8388608)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(limit + 1)
        if len(raw) > limit:
            # 读满了说明可能被截断，截断的 JSON 解析出来的报错很难懂，这里说明白
            raise ValueError(
                f"响应超过 {limit} 字节可能被截断（对方接口可能内嵌了封面 base64，"
                f"可在 config.json 调大 extapi.max_bytes）"
            )
        return json.loads(raw.decode("utf-8", "replace"))

    async def poll(self) -> MediaInfo | None:
        import asyncio

        if not self.enabled or not self.urls:
            return None
        fields = dict(self.cfg.get("extapi.fields", {}) or {})
        # 每一轮重新合并：跨轮次保留会留下上一首歌的残留数据
        self._merged = None
        self._sources = {}
        errors: list[str] = []
        for url in self.urls:
            try:
                payload = await asyncio.to_thread(self._fetch_sync, url)
            except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
                errors.append(f"{url}: {exc!r}")
                continue
            self.last_payload = payload
            self._sources[url] = payload
            info = parse_external_payload(payload, fields)
            if info is not None:
                self._merged = _merge_info(self._merged, info)
        self.last_error = " | ".join(errors)[:300]
        if self._merged is None:
            return None
        self.last_ok = time.time()
        self.hits += 1
        return self._merged

    def status(self) -> dict[str, Any]:
        alive = bool(self.last_ok) and (time.time() - self.last_ok) < 30
        return {
            "enabled": self.enabled,
            "urls": self.urls,
            "alive": alive,
            "hits": self.hits,
            "last_ok": round(time.time() - self.last_ok, 1) if self.last_ok else None,
            "last_error": self.last_error,
            "merged": self._merged.to_dict() if self._merged else None,
        }


def _merge_info(base: MediaInfo | None, new: MediaInfo) -> MediaInfo:
    """把两次读取合并成更完整的一条。

    关键两点：
    1) **只在确认是同一首歌时才合并**（歌名相似度），否则新歌会继承上一首的时长。
    2) **字段各自择优**：某个来源字段是空的，就用另一个来源的。
       实测 PlayerCap：song_info 给了 name="句号" + singer，
       all_lyrics 给了 duration + position 但 title 是合成串"句号 - G.E.M.邓紫棋"，
       所以歌名要挑"更短更像纯歌名"的那个，歌手要保住 song_info 的。
    """
    if base is None:
        return new
    if not new.title:
        # 这一源没有歌名（例如只有时长的 /all_lyrics）：只补它缺的字段
        return MediaInfo(
            app=base.app or new.app,
            title=base.title,
            artist=base.artist or new.artist,
            playing=new.playing,
            position=new.position if new.has_progress else base.position,
            duration=new.duration if new.has_progress else base.duration,
            source=base.source or new.source,
        )
    if not base.title:
        return new
    if not _same_song(base, new):
        return new
    # 歌名择优：更短的那个通常是纯歌名（"句号" 优于 "句号 - G.E.M.邓紫棋"）
    title = new.title if len(new.title) < len(base.title) else base.title
    return MediaInfo(
        app=new.app or base.app,
        title=title,
        artist=new.artist or base.artist,
        playing=new.playing,
        position=new.position if new.has_progress else base.position,
        duration=new.duration if new.has_progress else base.duration,
        source=new.source or base.source,
    )


def _score(a: str, b: str) -> float:
    from .media import similarity
    return similarity(a, b)


def _same_song(a: MediaInfo, b: MediaInfo) -> bool:
    """两条记录是不是同一首歌。歌名互相包含即算同一首（"句号" vs "句号 - G.E.M.邓紫棋"）。"""
    if not a.title or not b.title:
        return True
    return _score(a.title, b.title) >= 0.5
