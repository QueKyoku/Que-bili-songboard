"""从本机浏览器的 cookie 里取网易云的登录凭据。

为什么要有这个：
  · 手动从 F12 复制 cookie 太麻烦（要开开发者工具、找请求、复制一整行）；
  · 扫码登录**被网易云风控拦了** —— 第三方客户端普遍如此，
    返回 `8821 请切换其他登录方式或升级新版本再试`，反复扫也没用。
  而浏览器里你本来就是登录状态，直接读出来最省事。

⚠️ 隐私边界（在代码里就限死，不靠自觉）：
  * **只查 `music.163.com` 这一个域名**，不读别的网站
  * **只保留 `MUSIC_U` / `__csrf` / `NMTID`** 这几个必需字段，其余全部丢掉
  * 不做任何网络上传，不写日志，除了写进 config.json 那一份之外不落盘

原理：Chrome/Edge 127 起 cookie 值改成 App-Bound Encryption（v20 前缀），
传统的"拿 DPAPI 密钥自己解密"读不出来了。rookiepy 是 Rust 写的提取库，
在浏览器**自己能解密**的前提下（同一个 Windows 用户、权限足够）能把明文取出来。
实测（2026-09）：从 Edge 里成功读出了有效的 MUSIC_U。
"""

from __future__ import annotations

from typing import Any, Callable

from .cookies import extract_fields

#: 试的顺序：先 Edge（Windows 自带、最常见），再 Chrome 等
BROWSERS = ("edge", "chrome", "brave", "firefox", "chromium", "vivaldi", "opera")

#: 只留这几个 —— build_cookie 也是按这个白名单挑的
KEEP = ("MUSIC_U", "__csrf", "NMTID")

#: 提示里用中文名，别把 `edge` / RuntimeError 这种甩给用户看
NAME_CN = {"edge": "Edge", "chrome": "Chrome", "brave": "Brave",
           "firefox": "Firefox", "chromium": "Chromium",
           "vivaldi": "Vivaldi", "opera": "Opera"}

#: 只查这一个域名
DOMAIN = "music.163.com"


def available() -> bool:
    """本机装没装 rookiepy（它是可选依赖，不做硬要求）。"""
    try:
        import rookiepy  # noqa: F401
        return True
    except ImportError:
        return False


def read(browsers: tuple[str, ...] = BROWSERS) -> dict[str, Any]:
    """挨个浏览器试，读到 MUSIC_U 就用它。

    **永远不抛异常**（控制台要拿返回值直接显示），失败时 message 里说清
    "是哪个浏览器、因为什么没读到"，并且明确告诉用户
    **去浏览器里从网页端登录一次** —— 那是最常见的失败原因。
    """
    if not available():
        return {"ok": False,
                "message": "没装 rookiepy，这个功能用不了。"
                           "在项目目录跑一次 pip install rookiepy 就行。"}
    import rookiepy

    tried: list[str] = []
    for name in browsers:
        fn: Callable | None = getattr(rookiepy, name, None)
        if fn is None:
            continue
        try:
            items = fn([DOMAIN])
        except Exception:  # noqa: BLE001
            # 没装这个浏览器 / 没有 profile / 读不了，都归到"换下一个试"。
            # 异常类名（RuntimeError 之类）对用户没意义，所以只说"读不到"。
            tried.append(f"{NAME_CN.get(name, name)}（读不到）")
            continue
        raw = "; ".join(f"{c.get('name')}={c.get('value')}"
                        for c in items if c.get("name"))
        found = extract_fields(raw)
        if "MUSIC_U" in found:
            cookie = "; ".join(f"{k}={v}" for k, v in found.items() if k in KEEP)
            return {"ok": True, "cookie": cookie, "browser": name,
                    "fields": [k for k in found if k in KEEP]}
        tried.append(f"{NAME_CN.get(name, name)}（没有网易云登录凭据）")

    tried_text = "、".join(tried) if tried else "没有可读的浏览器"
    return {"ok": False,
            "message": f"没在浏览器里找到网易云的登录凭据（试过：{tried_text}）。\n"
                       f"解决：用你平时上网的那个浏览器打开 music.163.com，"
                       f"从网页端登录一次（登录完不用管它，也不用关浏览器），"
                       f"再回来点这个按钮。"}
