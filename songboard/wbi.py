"""B 站 WebSocket 协议需要的 WBI 签名（部分接口如 getDanmuInfo 需要）。"""
from __future__ import annotations

import functools
import hashlib
import time
import urllib.parse

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def get_mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def _filter_value(value: str) -> str:
    return "".join(ch for ch in value if ch not in "!'()*")


def enc_wbi(params: dict[str, str | int], img_key: str, sub_key: str) -> dict[str, str]:
    """返回带 wts + w_rid 的参数字典。"""
    mixin_key = get_mixin_key(img_key + sub_key)
    params = dict(params)
    params["wts"] = int(time.time())
    params = {k: _filter_value(str(v)) for k, v in sorted(params.items())}
    query = urllib.parse.urlencode(params)
    params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return params  # type: ignore[return-value]


def sign_query(params: dict[str, str | int], img_key: str, sub_key: str) -> str:
    return urllib.parse.urlencode(enc_wbi(params, img_key, sub_key))


def extract_keys(nav_data: dict) -> tuple[str, str] | None:
    """从 nav 接口响应里取出 img_key / sub_key。"""
    wbi = (nav_data or {}).get("wbi_img") or {}
    img_url = wbi.get("img_url") or ""
    sub_url = wbi.get("sub_url") or ""
    if not img_url or not sub_url:
        return None
    img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
    if not img_key or not sub_key:
        return None
    return img_key, sub_key
