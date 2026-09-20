"""一键设置网易云 Cookie：读剪贴板 → 挑出需要的字段 → 写入配置 → 立刻验证。

用法：
    python set_cookie.py                     # 从剪贴板读（推荐）
    python set_cookie.py --cookie "MUSIC_U=..."   # 直接给
    python set_cookie.py --test              # 只验证当前配置里的 cookie
    python set_cookie.py --search 稻香        # 顺带搜一首歌看看通不通

它是"傻瓜式"的：不管你是复制了纯 cookie、带 `Cookie:` 前缀的一行、
F12 里"Copy request headers"的**一整块**、还是"Copy as cURL"的**整条命令**，
都能提取出来 —— 靠的是按字段名搜值，而不是按分隔符拆。

安全说明：Cookie 会写进 config.json，请勿提交到 git、勿发给别人。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from songboard.config import Config  # noqa: E402
from songboard.cookies import KEEP, extract_fields  # noqa: E402
from songboard.netease import NeteaseAuthError, account_info, search_song  # noqa: E402

ROOT = Path(__file__).resolve().parent


def build_cookie(raw: str) -> str:
    """挑出需要的字段并整理成 `MUSIC_U=...; __csrf=...`；没找到就返回空。

    提取逻辑在 songboard/cookies.py（扫码登录那边也在用同一套）。
    """
    text = raw.strip()
    if not text:
        return ""
    found = extract_fields(text)
    if "MUSIC_U" not in found:
        print("⚠️ 没在内容里找到 MUSIC_U。")
        loose = re.findall(r"([A-Za-z_][A-Za-z0-9_]{2,})=", text)
        print(f"   这段文字里出现的字段有：{list(dict.fromkeys(loose))[:15]}")
        print("   常见原因：")
        print("     · 复制成了别的东西（比如整页 HTML、或者只是 MUSIC_U 的值）")
        print("     · 复制的地方不是 music.163.com（换了个标签页？）")
        print("   正确的拿法见 README 第九章「快捷获取 Cookie」，")
        print("   或者干脆用扫码登录：python login_qrcode.py")
        return ""
    order = [k for k in KEEP if k in found]
    print(f"✅ 提取到 {len(found)} 个字段：{order}")
    return "; ".join(f"{k}={found[k]}" for k in order)


def from_clipboard() -> str:
    """读剪贴板。tkinter 是标准库；万一不可用就退到 PowerShell。"""
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        text = root.clipboard_get()
        root.destroy()
        return text or ""
    except Exception:  # noqa: BLE001
        pass
    try:
        import subprocess
        r = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=15)
        return r.stdout or ""
    except Exception as exc:  # noqa: BLE001
        print(f"读剪贴板失败：{exc!r}")
        print('可以改用：python set_cookie.py --cookie "MUSIC_U=..."')
        return ""


def verify(cookie: str, *, quiet: bool = False) -> bool:
    """验证 cookie 有没有效 —— 问网易云"我是谁"，能拿到昵称就算通过。"""
    if not cookie:
        print("❌ 没有 cookie，没法验证")
        return False
    if not quiet:
        print("正在向网易云验证…")
    try:
        info = account_info(cookie)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 验证请求失败：{exc!r}")
        return False
    if not info:
        print("❌ cookie 无效或已过期（网易云没认出登录态）")
        print("   常见原因：复制漏了字符 / MUSIC_U 已过期 / 复制成了别的站点的 cookie")
        return False
    name = info.get("nickname") or f"uid {info.get('user_id')}"
    print(f"✅ cookie 有效，已登录：{name}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="一键设置网易云 Cookie")
    ap.add_argument("--cookie", help="直接给 cookie 字符串（不给就从剪贴板读）")
    ap.add_argument("--test", action="store_true", help="只验证当前配置里的 cookie")
    ap.add_argument("--search", help="顺带搜一首歌，验证搜索接口可用")
    args = ap.parse_args()

    cfg = Config.load(ROOT / "config.json")

    if args.test:
        return 0 if verify(str(cfg.get("netease.cookie", "") or "")) else 1

    if args.cookie:
        raw = args.cookie
    else:
        print("请先复制好 Cookie（F12 → Network → 右键请求 → Copy → Copy request headers），")
        print("然后在**这个窗口**按回车（或 Ctrl+C 取消）…")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return 1
        raw = from_clipboard()

    if not raw.strip():
        print("剪贴板是空的。先把 Cookie 复制好再运行。")
        return 1

    cookie = build_cookie(raw)
    if not cookie:
        return 1

    cfg["netease"]["cookie"] = cookie
    cfg["netease"]["enabled"] = True
    cfg.save()
    print(f"✅ 已写入 config.json（{len(cookie)} 字符），并自动开启「搜索歌曲 / 查时长」")
    print("⚠️  config.json 现在含有账号凭据：别提交到 git、别截图、别发给别人")

    # 写完立刻验证 —— 不然用户只能去控制台猜有没有生效
    ok = verify(cookie)
    if not ok:
        print("   提示：cookie 已写入但没通过验证，服务里也用不了；重新复制一份再试。")

    if args.search:
        try:
            hits = search_song(args.search, cookie, 5)
        except NeteaseAuthError as exc:
            print(f"❌ 搜索被拒：{exc}")
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 搜索失败：{exc!r}")
            return 1
        if not hits:
            print("❌ 搜索没有结果（换首歌试试，或者 cookie 已失效）")
            return 1
        print(f"\n搜索「{args.search}」前 {len(hits)} 条：")
        for h in hits:
            print(f"  · {h['name']} - {h['artists']}  (id={h['id']})")

    if ok:
        print("\n好了。服务在跑的话它会读新配置；没跑就直接启动：")
        print("    python -m songboard")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
