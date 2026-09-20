"""用无头浏览器真跑一遍叠加层，确认它**真的渲染出来了**。

为什么需要它：`node --check` 只能证明语法没错，
`selftest.py` 只能证明服务端数据对。而叠加层是浏览器里的东西：
脚本一旦在运行时抛异常（比如引用了不存在的元素、或者整个 script 因为
重复声明被浏览器丢弃），页面会**安安静静地停在初始占位文本上**，
服务端一切正常、日志一句不报 —— 直播时才发现"点歌板不动"。

用法：

    python tools/check_overlay_dom.py                      # 默认 8765
    python tools/check_overlay_dom.py http://127.0.0.1:8800/overlay
    python tools/check_overlay_dom.py --control            # 顺便检查控制台

判定依据（都是"脚本跑没跑"的硬证据）：

* `?bg=1` 时脚本会给 <body> 打上 `data-bg="1"` —— 静态 HTML 里没有这个属性，
  所以看到它 = 脚本至少执行到了那一行
* `#boardTitle` 的文本来自 `/api/config` —— 有内容说明 fetch + 渲染走了
* `#currentSong` 和 `/api/state` 对得上 —— 说明 WebSocket 快照真的渲染完了

⚠️ 只认 `--dump-dom` 序列化出来的**属性**和**文本内容**。
JS 直接赋的 `.value`（输入框）不会出现在 dump 里，别拿它当判据。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"  — {detail}" if detail else ""))


def find_browser() -> str | None:
    for p in CANDIDATES:
        if os.path.exists(p):
            return p
    for name in ("msedge", "chrome", "chromium", "google-chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


def dump_dom(browser: str, url: str, budget_ms: int = 9000) -> str:
    """无头加载页面、等 JS 跑完、把最终 DOM 打出来。"""
    profile = tempfile.mkdtemp(prefix="songboard-ovl-")
    try:
        out = subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", f"--user-data-dir={profile}",
             f"--virtual-time-budget={budget_ms}", "--dump-dom", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=max(30, budget_ms / 1000 + 30),
        )
        return out.stdout or ""
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def state(base: str) -> dict:
    with urllib.request.urlopen(base + "/api/state", timeout=10) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def text_of(dom: str, eid: str) -> str:
    m = re.findall(r'id="' + eid + r'"[^>]*>(.*?)<', dom, re.S)
    return m[0].strip() if m else ""


def check_overlay(browser: str, base: str, seconds: int = 9,
                  tries: int = 3) -> None:
    print("\n== 叠加层（浏览器里真跑） ==")

    # 拿服务端状态当标准答案（页面里应该显示的就是它）
    try:
        st = state(base)
    except Exception as exc:  # noqa: BLE001
        check("能读到服务端 /api/state", False, repr(exc))
        return
    want = ((st.get("current") or {}).get("song") or "").strip()

    # ⚠️ 快照是服务端每 2 秒广播一次的，而 --virtual-time-budget 跑得比真实
    #    时间快，所以偶尔会在第一条广播到达之前就把 DOM 打出来了
    #    （表现是"服务端有歌、页面还是占位"）。这种假失败要重试，不能报错。
    dom = ""
    got = ""
    items: list = []
    for attempt in range(1, tries + 1):
        dom = dump_dom(browser, base + "/overlay?bg=1", budget_ms=seconds * 1000)
        if not dom:
            continue
        got = text_of(dom, "currentSong")
        items = re.findall(r'<li[^>]*data-id="(\d+)"[^>]*>(.*?)</li>', dom, re.S)
        if not want or got == want:
            break
        print(f"      第 {attempt} 次没对上（服务端={want!r} 页面={got!r}），"
              f"可能只是快照还没广播到，重试…")

    if not dom:
        check("无头浏览器拿到了 DOM", False, "输出为空（浏览器启动失败？）")
        return
    check("无头浏览器拿到了 DOM", True, f"{len(dom)} 字节")

    # 1) 脚本到底跑没跑
    body = re.search(r"<body[^>]*>", dom)
    body_tag = body.group(0) if body else ""
    check('内联脚本真的执行了（<body data-bg="1">）',
          'data-bg="1"' in body_tag, body_tag[:80])

    # 2) /api/config 回来了（标题是脚本填的，静态 HTML 里是空的）
    title = text_of(dom, "boardTitle")
    sub = text_of(dom, "boardSub")
    check("fetch('/api/config') 生效（标题/副标题被填上）",
          bool(title), f"title={title!r} sub={sub!r}")

    # 3) WebSocket 快照渲染完了
    print(f"      服务端正在播放：{want or '（空闲）'}   "
          f"叠加层显示：{got or '（空）'}   队列条目：{len(items)}")
    if want:
        check("WebSocket 快照渲染出来了（当前曲目和服务端一致）",
              got == want, f"服务端={want!r} 叠加层={got!r}")
        check("没有卡在初始化占位文本上（“等待点歌…”）",
              got != "等待点歌…", f"currentSong={got!r}")
    else:
        # 队列空着的时候，占位文本本来就是对的 —— 两边一致同样说明渲染通了
        check("服务端空闲时叠加层也显示占位（两边一致）",
              got == "等待点歌…", f"叠加层={got!r}")
        print("      ⚠️ 服务里现在没有正在播放的歌，"
              "「曲目是否对上」这条测不出来。"
              "想测完整流程就先点一首（或 python demo_full.py），再跑一次。")


def check_control(browser: str, base: str, seconds: int = 9) -> None:
    print("\n== 控制台（浏览器里真跑） ==")
    dom = dump_dom(browser, base + "/control", budget_ms=seconds * 1000)
    check("无头浏览器拿到了 DOM", bool(dom), f"{len(dom)} 字节")
    check("点歌门槛卡片渲染出来了", "点歌门槛（送礼物才能点）" in dom)

    # 只查文本内容：JS 直接赋的 .value（输入框）不会出现在 dump 里
    tag = text_of(dom, "ggTag")
    check("门槛状态标签被实时填上（renderStatus 跑了）",
          bool(tag), f"ggTag={tag!r}")
    check("门槛统计文本渲染出来了",
          ("已知" in dom and "付费礼物" in dom and "免费礼物" in dom),
          str([k for k in ("已知", "付费礼物", "免费礼物") if k not in dom]))

    # 顺带确认控制台没有把"写歌单"的老开关又露出来。
    # 只在**代码/JSON** 里找（带引号或是键名），注释里提到不算问题。
    leaked = re.findall(r'["\'](auto_add|playlist_id|write_playlist)["\']'
                        r'|(\b(?:auto_add|playlist_id|write_playlist)\s*:)', dom)
    leaked = [a or b for a, b in leaked]
    check("控制台没有写歌单的开关（只插队列）", not leaked, str(leaked))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default="http://127.0.0.1:8765",
                    help="服务地址，默认 http://127.0.0.1:8765")
    ap.add_argument("--control", action="store_true", help="顺便检查控制台")
    ap.add_argument("--seconds", type=int, default=9,
                    help="给页面多少秒虚拟时间跑 JS（默认 9）")
    args = ap.parse_args()
    base = args.url.rstrip("/")
    if base.endswith("/overlay"):
        base = base[: -len("/overlay")]

    browser = find_browser()
    if not browser:
        print("找不到 Edge/Chrome，没法做浏览器检查。")
        return 2
    print(f"浏览器：{browser}")
    print(f"服务  ：{base}")

    if not reachable(base + "/overlay"):
        print(f"连不上 {base}/overlay，先把服务跑起来：python -m songboard")
        return 1

    t0 = time.time()
    check_overlay(browser, base, args.seconds)
    if args.control:
        check_control(browser, base, args.seconds)

    failed = [r for r in results if not r[1]]
    print(f"\n{'=' * 50}\n共 {len(results)} 项，通过 "
          f"{len(results) - len(failed)}，失败 {len(failed)}"
          f"（{time.time() - t0:.1f}s）")
    for name, _, detail in failed:
        print(f"  FAIL: {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
