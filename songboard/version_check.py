"""检查点歌板自己是不是最新版本，好让控制台提醒主播"该更新了"。

为什么要有：
    主播拿到的是从 GitHub 下的 zip，没人会隔几天回仓库看一眼有没有新版。
    而这个项目踩过的坑（cookie 验不过、房间短号收不到弹幕、脚本 BOM 乱码）
    基本都是别人先撞上才修的 —— 能在控制台顶上看到一行"有新版本"，
    比让他自己猜"是不是我这版有毛病"强得多。

比的是 git tag，不是 GitHub Releases：
    本项目的发版方式就是"改 CHANGELOG + 打 tag + push"，
    Releases 页面一直是空的。所以查 /tags 接口，取版本号最大的那个 tag。

硬性要求 —— **绝不能拖慢或拖垮启动**：
    断网、超时、被 GitHub 限流（403）、接口改名、tag 命名不规范……
    一律当成"不知道"：静默返回，不抛异常、不重试、不卡住。
    查更新是锦上添花，不能因为它让点歌板起不来。
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, Callable, Iterable

from . import __version__

#: 上游仓库（fork 出去改代码的话，这里也一起改，否则会查上游的版本）
REPO = "QueKyoku/Que-bili-songboard"

TAGS_API = f"https://api.github.com/repos/{REPO}/tags?per_page=100"
REPO_URL = f"https://github.com/{REPO}"
CHANGELOG_URL = f"https://github.com/{REPO}/blob/main/CHANGELOG.md"

#: 只认 v1 / v1.2 / v1.2.3 这类；`latest`、`test` 这种 tag 直接忽略。
#: 大小写都收 —— 有人习惯打 `V1.0`，为此判成"不是版本号"太冤。
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)$", re.IGNORECASE)

#: 版本号补零到这么多段再比较，免得 1.2 和 1.2.0 谁大谁小说不清
_PAD = 8


def parse_version(text: Any) -> tuple[int, ...] | None:
    """`'v0.5.0'` / `'0.5'` / `'1.2.3.4'` → 数字元组；不是版本号就 None。"""
    if not isinstance(text, str):
        return None
    m = _VERSION_RE.match(text.strip())
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def _key(ver: tuple[int, ...]) -> tuple[int, ...]:
    return ver + (0,) * max(0, _PAD - len(ver))


def bare(name: Any) -> str:
    """去掉 `v` 前缀，只留数字部分 —— 给人看的时候统一加一次 `v` 就够了。

    tag 名字里带 `v`（`v0.5.1`），显示时又要写成 `v0.5.1`，
    不归一化就会印出 `vv0.5.1`（第一版就是这么错的）。
    """
    return str(name or "?").strip().lstrip("vV")


def is_newer(latest: Any, current: Any) -> bool:
    """latest 是不是比 current 新。任何一个解析不了就返回 False。

    ⚠️ **不能拿字符串比**：`'0.10.0' < '0.9.9'` 在字符串比较下是 True（错的，
    因为 `'1' < '9'`）。必须切成数字逐段比，所以先 parse 再补零对齐。
    """
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    return _key(a) > _key(b)


def pick_latest(names: Iterable[Any]) -> str | None:
    """从一堆 tag 名里挑出版本号最大的那个（原样返回，保留 `v` 前缀）。"""
    best: str | None = None
    best_key: tuple[int, ...] | None = None
    for name in names or ():
        ver = parse_version(name)
        if ver is None:
            continue
        k = _key(ver)
        if best_key is None or k > best_key:
            best, best_key = name, k
    return best


def fetch_tags(timeout: float = 5.0) -> list[str]:
    """拉上游 tag 列表。失败就抛 —— 由 check() 统一兜住。"""
    req = urllib.request.Request(
        TAGS_API,
        headers={
            # ⚠️ GitHub 对没有 User-Agent 的请求直接回 403，必须带
            "User-Agent": f"bili-songboard/{__version__}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, list):
        return []
    return [t.get("name", "") for t in data if isinstance(t, dict)]


def initial(current: str | None = None) -> dict[str, Any]:
    """「还没查过」的状态 —— 控制台靠 checked=False 区分「不知道」和「已是最新」。

    ⚠️ 别省上面这两个「」：如果改用成对的半角引号，docstring 里会出现
    四个连续引号，前三个被当成定界符，读起来像语法错误、改的时候也容易改坏
    （这个模块第一版就是那么写的，真踩了）。
    """
    return {
        "checked": False,
        "current": current or __version__,
        "latest": None,
        "has_update": False,
        "url": CHANGELOG_URL,
        "error": None,
    }


def check(current: str | None = None, timeout: float = 5.0,
          fetcher: Callable[[float], list[str]] | None = None) -> dict[str, Any]:
    """查一次，返回可以直接塞进 status 的 dict。**永不抛异常。**

    `fetcher` 只是给测试用的注入点（默认走真实网络）。

    字段含义：
        checked    —— 这次真的问到结果了吗。False = 不知道，**别显示"已是最新"**
        current    —— 当前跑着的版本
        latest     —— 查到的最新版本（不带 `v`，例 `0.5.1`），不知道时 None
        has_update —— 要不要提示更新。解析不出来时一律 False（宁可不提醒，
                      也不能瞎报"有新版"让主播白折腾一趟）
        url        —— 更新日志地址，控制台给个链接让他看改了什么
        error      —— 没查成的原因（异常类名），给排错用，不弹给用户
    """
    state = initial(current)
    try:
        names = (fetcher or fetch_tags)(timeout)
    except Exception as exc:  # noqa: BLE001 —— 查更新绝不允许影响主流程
        state["error"] = type(exc).__name__
        return state

    latest = pick_latest(names)
    state["checked"] = True
    # 存归一化后的数字（`0.5.1`，不带 v）—— 控制台和日志都会自己补一个 `v`
    state["latest"] = bare(latest) if latest is not None else None
    if latest is not None:
        state["has_update"] = is_newer(latest, state["current"])
    return state


def message(state: dict[str, Any]) -> str:
    """给命令行/日志用的一句话。"""
    cur = state.get("current") or "?"
    if state.get("has_update"):
        return (f"🔔 有新版本 v{bare(state.get('latest'))}（当前 v{cur}）"
                f"，更新日志：{state.get('url')}")
    if state.get("checked"):
        return f"版本检查：已是最新 v{cur}"
    return f"版本检查：暂时查不到（{state.get('error') or '未检查'}）"
