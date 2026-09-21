"""Cookie 提取：从"一坨文本"里抠出需要的那几个字段。

被两个地方用：
  * `set_cookie.py`   —— 用户从 F12 复制一堆东西进来
  * 扫码登录（tools/login_qrcode.py、tools/扫码登录.pyw）—— 成功后从
    响应的 Set-Cookie 里收凭据

⚠️ 不能按 `;` 拆分再配对。实测用户会粘进来的形态太多：

    MUSIC_U=xxx; __csrf=yyy                     （纯 cookie）
    Cookie: MUSIC_U=xxx; __csrf=yyy             （带前缀的一行）
    Host: music.163.com\\nCookie: MUSIC_U=xxx    （整块请求头，含换行）
    curl '...' -H 'cookie: MUSIC_U=xxx; ...'    （Copy as cURL 的整条命令）

按分隔符拆的话，cURL 那种会把第一个字段名解析成 `-H 'cookie: MUSIC_U`，
后面全都错位。所以改成**按字段名正则搜值**，值一直取到分号、空白或引号为止。
"""
from __future__ import annotations

import re

#: 只保留需要的字段，避免把无关的第三方 cookie 一起存进配置
KEEP = ("MUSIC_U", "__csrf", "NMTID", "__remember_me", "MUSIC_A", "_ntes_nuid")


def extract_fields(raw: str) -> dict[str, str]:
    """从任意文本里抓出 KEEP 里那些字段的值。"""
    found: dict[str, str] = {}
    for key in KEEP:
        m = re.search(rf"""(?:^|[;:,\s'"]){re.escape(key)}=([^;'"\s]+)""", raw)
        if m and m.group(1):
            found[key] = m.group(1).strip()
    return found


def build_cookie(raw: str) -> str:
    """整理成 `MUSIC_U=...; __csrf=...` 的形式；没有 MUSIC_U 就返回空串。"""
    found = extract_fields(raw)
    order = [k for k in KEEP if k in found]
    return "; ".join(f"{k}={found[k]}" for k in order)


def cookies_from_response_headers(headers) -> str:
    """从 HTTP 响应头里收 Set-Cookie，拼成 cookie 字符串。

    扫码登录成功（code=803）时，网易云是通过 Set-Cookie 下发 MUSIC_U 的，
    不在 body 里 —— 所以这一步必须读响应头。
    """
    ones = headers.get_all("Set-Cookie") if headers else None
    if not ones:
        return ""
    # 每条形如 "MUSIC_U=xxx; Path=/; HttpOnly; Expires=..."，只取第一个 k=v
    parts: list[str] = []
    for one in ones:
        first = one.split(";", 1)[0].strip()
        if "=" in first:
            parts.append(first)
    return build_cookie("; ".join(parts))
