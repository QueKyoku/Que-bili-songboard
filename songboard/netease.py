"""网易云音乐歌单适配器（P3 预留，现在实现「加歌进歌单」这条路）。

设计：MusicDriver 是统一接口，未来接本地音乐库 / QQ 音乐只需再写一个实现。
注意：网易云没有官方第三方 API，这里用的是网页端接口，可能随官方改动失效。
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import random
import time
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from .config import Config

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
NONCE = "0CoJUm6Qyw8W8jud"
IV = "0102030405060708"
PUBKEY = "010001"
# 网易云网页端固定公钥指数 e=65537，模数 n 为 1024bit（小端输出）
MODULUS = (
    "e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280"
    "104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)

#: 网易云"没认出登录态"时返回的 code（实测 2026-09：空 cookie / 假 cookie
#: 都返回 50000005；301 是历史上见过的"需要登录"）
AUTH_ERROR_CODES = (50000005, 301)


class NeteaseAuthError(RuntimeError):
    """cookie 无效或已过期。

    单独一个异常类型，是为了让上层能把它和"网断了""这首歌真搜不到"
    区分开 —— 以前一律报"搜不到《X》"，主播会去查歌名，方向完全错了。
    """


class MusicDriver(ABC):
    name = "base"
    #: 网易云登录态。基类给个空值，免得调用方（比如只插播放队列模式）直接
    #: 访问 driver.cookie 时抛 AttributeError —— 只有网易云驱动才用得着它。
    cookie = ""

    @abstractmethod
    async def test(self) -> tuple[bool, str]:
        """检查登录态/歌单是否可用。"""

    @abstractmethod
    async def add_song(self, song: str, user: str = "") -> tuple[bool, str, dict]:
        """把一首歌加进目标歌单。返回 (是否成功, 说明, 命中的曲目信息)。"""

    async def song_duration(self, song: str) -> float:
        """查这首歌的时长（秒）。查不到返回 0，调用方用兜底值。"""
        return 0.0

    async def append_song(self, song: str, user: str = "") -> tuple[bool, str, dict]:
        """把歌追加到歌单**最后一位**。不支持追加的驱动退化为普通加歌。"""
        return await self.add_song(song, user)

    async def reorder_playlist(self, desired: list[int], **kw) -> tuple[bool, str]:
        """把歌单顺序排成 desired（点歌顺序 = 播放顺序）。"""
        return False, "该驱动不支持重排顺序"

    def playlist_order(self) -> list[tuple[int, str]]:
        return []

    def status(self) -> dict[str, Any]:
        return {"driver": self.name, "enabled": False}


class NullDriver(MusicDriver):
    """未启用时的空实现。"""
    name = "none"

    async def test(self) -> tuple[bool, str]:
        return False, "未启用音乐驱动"

    async def add_song(self, song: str, user: str = "") -> tuple[bool, str, dict]:
        return False, "未启用音乐驱动", {}

    async def song_duration(self, song: str) -> float:
        return 0.0


# --------------------------------------------------------------------------- 加密
def _pkcs7(data: bytes, block: int = 16) -> bytes:
    pad = block - len(data) % block
    return data + bytes([pad]) * pad


def _aes_cbc_encrypt(text: str, key: str, iv: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    enc = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).encryptor()
    out = enc.update(_pkcs7(text.encode())) + enc.finalize()
    return base64.b64encode(out).decode()


def _rsa_encrypt(text: str) -> str:
    """网易云用的是无填充（textbook）RSA，这里手写大数幂模，避免额外依赖。"""
    n = int.from_bytes(binascii.unhexlify(MODULUS), "big")
    e = int(PUBKEY, 16)
    m = int.from_bytes(text[::-1].encode(), "big")
    c = pow(m, e, n)
    return binascii.hexlify(c.to_bytes((c.bit_length() + 7) // 8, "big")).decode()


def weapi_params(payload: dict[str, Any]) -> dict[str, str]:
    """把 payload 转成 weapi 需要的 params / encSecKey。"""
    secret = "".join(random.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
                     for _ in range(16))
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    first = _aes_cbc_encrypt(text, NONCE, IV)
    params = _aes_cbc_encrypt(first, secret, IV)
    return {"params": params, "encSecKey": _rsa_encrypt(secret)}


def weapi_post_raw(path: str, payload: dict[str, Any],
                   cookie: str) -> tuple[dict, Any]:
    """和 weapi_post 一样，但**连响应头一起返回**。

    扫码登录成功时网易云是通过 `Set-Cookie` 下发 MUSIC_U 的，body 里没有 ——
    所以那一步必须能读到响应头。
    """
    url = f"https://music.163.com/weapi{path}"
    data = urllib.parse.urlencode(weapi_params(payload)).encode()
    headers = {
        "User-Agent": UA,
        "Referer": "https://music.163.com/",
        "Origin": "https://music.163.com",
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": cookie,
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", "replace")
        resp_headers = resp.headers
    try:
        return json.loads(body), resp_headers
    except json.JSONDecodeError:
        return {"code": -1, "raw": body[:200]}, resp_headers


def weapi_post(path: str, payload: dict[str, Any], cookie: str = "") -> dict:
    return weapi_post_raw(path, payload, cookie)[0]


def search_song(keyword: str, cookie: str = "", limit: int = 5) -> list[dict]:
    payload = {"s": keyword, "type": 1, "offset": 0, "total": True, "limit": limit}
    res = weapi_post("/cloudsearch/get/web", payload, cookie)
    code = res.get("code")
    if code in AUTH_ERROR_CODES:
        # ⚠️ 这个码是"没认出登录态"。以前它会被当成"这首歌搜不到"，
        #    主播看到的是"网易云搜不到《起风了》"—— 完全查错方向。
        raise NeteaseAuthError(
            f"网易云登录态无效（code={code}），cookie 可能已过期，请重新获取")
    songs = ((res.get("result") or {}).get("songs")) or []
    out = []
    for s in songs:
        artists = "/".join(a.get("name", "") for a in (s.get("ar") or s.get("artists") or []))
        out.append({
            "id": s.get("id"),
            "name": s.get("name"),
            "artists": artists,
            "album": ((s.get("al") or s.get("album") or {}) or {}).get("name", ""),
            "fee": s.get("fee"),
            "duration_ms": int(s.get("dt") or s.get("duration") or 0),
        })
    return out


def account_info(cookie: str) -> dict[str, Any]:
    """问网易云"我是谁"——用来确认 cookie 到底有没有效。

    实测（2026-09）：cookie 有效时这个接口返回 profile.nickname / userId；
    cookie 无效或没填时照样返回 code=200，但**没有 profile** ——
    所以判据是"能不能拿到 userId"，不是看 code。
    """
    if not cookie:
        return {}
    try:
        res = weapi_post("/w/nuser/account/get", {}, cookie)
    except Exception:  # noqa: BLE001
        return {}
    prof = res.get("profile") or res.get("account") or {}
    uid = prof.get("userId")
    if not uid:
        return {}
    return {
        "nickname": str(prof.get("nickname") or ""),
        "user_id": int(uid),
    }


# --------------------------------------------------------------------------- 驱动
class NeteasePlaylistDriver(MusicDriver):
    """把点歌写进一个网易云歌单；主播自己在该歌单上按顺序播放。"""
    name = "netease-playlist"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.cookie = _resolve_cookie(str(cfg.get("netease.cookie", "")))
        self.playlist_id = str(cfg.get("netease.playlist_id", "") or "")
        self.last_error = ""
        #: 登录的账号信息（昵称/uid），由 test() 填；控制台用它显示"已登录：xxx"
        self.account: dict[str, Any] = {}
        self.added = 0
        self.reorders = 0
        self.cache: dict[str, dict] = {}

    def status(self) -> dict[str, Any]:
        return {
            "driver": self.name,
            "enabled": bool(self.cfg.get("netease.enabled", False)),
            "playlist_id": self.playlist_id,
            "has_cookie": bool(self.cookie),
            "account": dict(self.account),
            "added": self.added,
            "reorders": self.reorders,
            "last_error": self.last_error,
        }

    # ---------- 歌单顺序 ----------
    def playlist_order(self) -> list[tuple[int, str]]:
        """读歌单当前顺序，返回 [(track_id, name)]（只读）。"""
        res = weapi_post("/v6/playlist/detail",
                         {"id": self.playlist_id, "n": 1000, "s": 8}, self.cookie)
        if res.get("code") != 200:
            raise RuntimeError(f"读歌单失败 code={res.get('code')}")
        tracks = ((res.get("playlist") or {}).get("tracks")) or []
        return [(int(t.get("id")), str(t.get("name"))) for t in tracks]

    def _manipulate(self, op: str, ids: list[int]) -> bool:
        res = weapi_post("/playlist/manipulate/tracks",
                         {"op": op, "pid": self.playlist_id,
                          "trackIds": json.dumps([int(i) for i in ids])},
                         self.cookie)
        code = res.get("code")
        if code not in (200, None):
            self.last_error = f"{op} code={code} {res.get('message') or res.get('msg') or ''}"
            return False
        return True

    def move_to_end(self, track_id: int, *, already_tried: int = 0) -> tuple[bool, str]:
        """把指定曲目挪到歌单**最后一位**。

        为什么需要这么做：网易云"加歌"永远插在第 1 位（实测 imme=true/false 都一样），
        没有"追加到末尾"的接口。唯一可用的手段是"删掉 → 按倒序重加"：
            del 全部 → 按 [其余…, 目标] 的**倒序**依次 add
        因为每次 add 都插到第 1 位，倒序添加后得到的顺序就是正序。

        只动目标这一首，其余曲目保持原有相对顺序。
        """
        try:
            current = [tid for tid, _ in self.playlist_order()]
        except Exception as exc:
            self.last_error = repr(exc)
            return False, f"读歌单失败：{exc!r}"
        if track_id not in current:
            return False, f"曲目 {track_id} 不在歌单里"
        if current[-1] == track_id and len(current) > 1:
            return True, "已经在最后一位"

        order = [tid for tid in current if tid != track_id]
        order.append(track_id)
        if not self._manipulate("del", order):
            return False, f"删除失败：{self.last_error}"
        for tid in reversed(order):
            if not self._manipulate("add", [tid]):
                return False, f"重加失败：{self.last_error}"
        self.reorders += 1
        return True, f"已挪到歌单最后一位（共 {len(order)} 首）"

    # ---------- 歌单容量管理 ----------
    def playlist_state(self) -> list[tuple[int, str]]:
        """读歌单当前顺序 [(track_id, name)]（只读）。"""
        return self.playlist_order()

    def delete_tracks(self, ids: list[int]) -> tuple[bool, str]:
        """从歌单删除若干曲目。"""
        ids = [int(i) for i in ids]
        if not ids:
            return True, "没有要删的"
        if not self._manipulate("del", ids):
            return False, f"删除失败：{self.last_error}"
        return True, f"已删除 {len(ids)} 首"

    def create_playlist(self, name: str) -> tuple[bool, str]:
        """新建一个歌单（用于"歌单太长"时换一个干净的，避免频繁删歌）。"""
        try:
            res = weapi_post("/api/playlist/create", {"name": name}, self.cookie)
        except Exception as exc:
            return False, f"建歌单失败：{exc!r}"
        if res.get("code") != 200:
            return False, f"建歌单返回 code={res.get('code')} {res.get('message') or ''}"
        pid = res.get("id") or (res.get("playlist") or {}).get("id")
        if not pid:
            return False, "建歌单成功但没返回 id"
        return True, str(pid)

    def reorder_playlist(self, desired: list[int], *, min_moved: int = 1) -> tuple[bool, str]:
        """把歌单顺序排成 desired（按 track id，顺序即播放顺序）。

        为什么需要这个：网易云"加歌"**永远插在第 1 位**（实测 imme=true/false 都一样），
        所以点歌顺序会被倒过来——最后点的人排最前，先点的人被挤到最后。
        实测可行的手段是"删掉 → 按倒序重加"，这样能精确复现任意顺序。

        desired 只应包含**还没播的点歌**，当前正在播的那首不动。
        """
        if not desired:
            return True, "没有需要排序的歌"
        try:
            current = [tid for tid, _ in self.playlist_order()]
        except Exception as exc:
            self.last_error = repr(exc)
            return False, f"读歌单失败：{exc!r}"

        present = [tid for tid in desired if tid in current]
        if len(present) != len(desired):
            missing = [tid for tid in desired if tid not in current]
            return False, f"有 {len(missing)} 首歌不在歌单里（可能被移除了）：{missing}"

        # 现状里这些歌的相对顺序
        in_playlist_order = [tid for tid in current if tid in set(desired)]
        if in_playlist_order == desired:
            return True, "顺序已经正确，无需调整"

        # 需要移动的曲目：先删掉，再倒序加回去
        if not self._manipulate("del", desired):
            return False, f"删除失败：{self.last_error}"
        for tid in reversed(desired):
            if not self._manipulate("add", [tid]):
                return False, f"重加失败：{self.last_error}"
        self.reorders += 1
        return True, f"已重排 {len(desired)} 首（点歌顺序 → 播放顺序）"

    async def test(self) -> tuple[bool, str]:
        """检查"搜歌 / 查时长"这条路通不通 —— 也就是 cookie 到底有没有效。

        ⚠️ 以前这里靠读**歌单**来验 cookie。可歌单方案早就废弃了（只插播放
        队列、一个字节都不写歌单），于是没填歌单 ID 时它会直接报
        "缺少歌单 ID" —— 对现在的用法纯属误导：主播根本没打算填歌单。

        现在改成问网易云"我是谁"：cookie 有效就能拿到昵称，
        无效则什么都拿不到（实测这个接口两种情况都返回 code=200，
        所以判据是"有没有 profile.userId"，不是看 code）。
        """
        if not self.cookie:
            self.account = {}
            return False, ("没有填 cookie，搜不了歌（点歌板能用，但歌插不进"
                           "网易云，因为拿不到歌曲 id）")
        try:
            import asyncio
            info = await asyncio.to_thread(account_info, self.cookie)
        except Exception as exc:  # noqa: BLE001
            self.last_error = repr(exc)
            return False, f"接口调用失败：{exc!r}"
        if not info:
            self.account = {}
            self.last_error = "cookie 无效"
            return False, ("cookie 无效或已过期（网易云没认出登录态），"
                           "请重新复制一份")
        self.account = info
        name = info.get("nickname") or f"uid {info.get('user_id')}"
        return True, f"搜索可用，已登录：{name}"

    async def append_song(self, song: str, user: str = "") -> tuple[bool, str, dict]:
        """把歌加进歌单，并保证它落在**最后一位**（追加语义）。

        网易云原生只会插到第 1 位，所以加完再用 move_to_end 挪到末尾。
        """
        import asyncio

        ok, msg, hit = await self.add_song(song, user)
        if not ok or not hit or not hit.get("id"):
            return ok, msg, hit
        # 队头模式下也要"挪到末尾"：网易云加歌永远插第 1 位，
        # 只有挪到末尾才能形成"先点在前"的顺序。
        if not bool(self.cfg.get("netease.append_last", False)):
            return True, msg, hit
        try:
            moved, mmsg = await asyncio.to_thread(self.move_to_end, int(hit["id"]))
        except Exception as exc:
            self.last_error = repr(exc)
            return True, msg + f"（挪到末尾失败：{exc!r}）", hit
        if moved:
            return True, f"{msg} → 已排在歌单最后一位", hit
        return True, f"{msg}（未能挪到末尾：{mmsg}）", hit

    async def song_duration(self, song: str) -> float:
        """搜索这首歌并返回时长（毫秒 → 秒）。失败返回 0，调用方用兜底值。"""
        import asyncio

        try:
            hits = self.cache.get(song) or await asyncio.to_thread(
                search_song, song, self.cookie, 5,
            )
        except Exception as exc:
            self.last_error = repr(exc)
            return 0.0
        if not hits:
            return 0.0
        self.cache[song] = hits
        ms = int(hits[0].get("duration_ms") or 0)
        return round(ms / 1000.0, 1) if ms > 0 else 0.0

    async def add_song(self, song: str, user: str = "") -> tuple[bool, str, dict]:
        """把歌加进歌单。返回 (是否成功, 说明, 命中的曲目信息)。

        曲目信息里有 id/name/artists，调用方拿它回填到点歌条目上，
        后续才能对歌单做"重排顺序"。
        """
        import asyncio

        if not self.cookie or not self.playlist_id:
            return False, "未配置 cookie 或歌单 ID", {}
        try:
            hits = self.cache.get(song) or await asyncio.to_thread(
                search_song, song, self.cookie, 5,
            )
        except Exception as exc:
            self.last_error = repr(exc)
            return False, f"搜索失败：{exc!r}", {}
        if not hits:
            return False, f"没搜到「{song}」", {}
        self.cache[song] = hits
        pick = hits[0]
        try:
            res = await asyncio.to_thread(
                weapi_post, "/playlist/manipulate/tracks",
                {"op": "add", "pid": self.playlist_id, "trackIds": f"[{pick['id']}]", "imme": "true"},
                self.cookie,
            )
        except Exception as exc:
            self.last_error = repr(exc)
            return False, f"加歌失败：{exc!r}", {}
        code = res.get("code")
        # 部分版本成功时返回空体或 code=200
        if code in (200, None) or res == {}:
            self.added += 1
            return True, f"已加入歌单：{pick['name']} - {pick['artists']}", pick
        self.last_error = str(code)
        return False, f"加歌返回 code={code} {res.get('message') or ''}".strip(), {}


def _resolve_cookie(raw: str) -> str:
    """cookie 可以写成明文，也可以写成 MUSIC_U=xxx;__csrf=yyy。支持从环境变量读取。"""
    if raw.startswith("env:"):
        return os.environ.get(raw[4:], "")
    return raw.strip()


def build_driver(cfg: Config) -> MusicDriver:
    if not cfg.get("netease.enabled", False):
        return NullDriver()
    return NeteasePlaylistDriver(cfg)
