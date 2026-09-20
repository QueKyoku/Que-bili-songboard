"""从剪贴板提取网易云 Cookie 并写入配置 + 测试歌单是否可写。

用法：
    python set_cookie.py                    # 从剪贴板读取
    python set_cookie.py --cookie "MUSIC_U=..."   # 直接给
    python set_cookie.py --playlist 123456789     # 顺带设置歌单 ID
    python set_cookie.py --test                   # 只测试当前配置

安全说明：Cookie 会写进 config.json，请勿提交到 git、勿发给别人。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from songboard.config import Config  # noqa: E402
from songboard.netease import NeteasePlaylistDriver, search_song  # noqa: E402

ROOT = Path(__file__).resolve().parent

# 只保留需要的字段，避免把无关的第三方 cookie 一起存进去
KEEP = ("MUSIC_U", "__csrf", "NMTID", "__remember_me", "MUSIC_A", "_ntes_nuid")


def clean_cookie(raw: str) -> str:
    """从一坨文本里挑出 cookie 字段。容忍直接粘贴整行 'Cookie: xxx' 或 dict 形式。"""
    text = raw.strip()
    # 去掉 'Cookie:' 前缀
    text = re.sub(r"^\s*cookie\s*:\s*", "", text, flags=re.I)
    # 去掉换行（cookie 头可能被折行）
    text = re.sub(r"[\r\n]+", " ", text)

    pairs: dict[str, str] = {}
    for item in text.split(";"):
        if "=" not in item:
            continue
        k, _, v = item.partition("=")
        k, v = k.strip(), v.strip()
        if k:
            pairs[k] = v

    picked = {k: v for k, v in pairs.items() if k in KEEP and v}
    if "MUSIC_U" not in picked:
        # 没识别到 MUSIC_U：可能用户只复制了值，或者格式特殊
        print("⚠️ 没在内容里找到 MUSIC_U 字段。")
        print(f"   识别到的字段有：{list(pairs)[:15]}")
        print("   请确认复制的是 music.163.com 请求头里的整行 Cookie。")
        return ""
    order = [k for k in KEEP if k in picked]
    result = "; ".join(f"{k}={picked[k]}" for k in order)
    print(f"✅ 提取到 {len(picked)} 个字段：{order}")
    return result


def from_clipboard() -> str:
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        text = root.clipboard_get()
        root.destroy()
        return text or ""
    except Exception as exc:
        print(f"读剪贴板失败：{exc!r}")
        print("可以改用：python set_cookie.py --cookie \"MUSIC_U=...\"")
        return ""


def test(cfg: Config) -> int:
    driver = NeteasePlaylistDriver(cfg)
    st = driver.status()
    print(f"\n歌单 ID   : {st['playlist_id'] or '（未填）'}")
    print(f"Cookie    : {'已配置' if st['has_cookie'] else '未配置'}")
    if not st["has_cookie"] or not st["playlist_id"]:
        print("→ 两项都填好才能测试")
        return 1
    ok, msg = _run(driver.test())
    print(f"测试结果  : {'✅ ' if ok else '❌ '}{msg}")
    return 0 if ok else 1


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def main() -> int:
    ap = argparse.ArgumentParser(description="设置网易云 Cookie / 歌单 ID")
    ap.add_argument("--cookie", help="直接给 cookie 字符串")
    ap.add_argument("--playlist", help="歌单 ID（歌单链接 id= 后面那串）")
    ap.add_argument("--test", action="store_true", help="只测试当前配置")
    ap.add_argument("--search", help="顺带搜一首歌，验证搜索接口是否可用")
    args = ap.parse_args()

    cfg = Config.load(ROOT / "config.json")

    if args.test:
        return test(cfg)

    if args.playlist:
        cfg["netease"]["playlist_id"] = args.playlist.strip()
        cfg.save()
        print(f"✅ 歌单 ID 已写入：{args.playlist.strip()}")

    if args.cookie or args.cookie == "":
        raw = args.cookie
    else:
        print("请先在浏览器里复制好那一整行 Cookie，再回车（或 Ctrl+C 取消）…")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            return 1
        raw = from_clipboard()

    if raw:
        cookie = clean_cookie(raw)
        if not cookie:
            return 1
        cfg["netease"]["cookie"] = cookie
        cfg["netease"]["enabled"] = True
        cfg.save()
        print(f"✅ 已写入 config.json（{len(cookie)} 字符），并自动启用网易云驱动")
        print("⚠️  config.json 现在含有账号凭据，别提交到 git、别发给别人")

    if args.search:
        cookie = cfg["netease"]["cookie"]
        try:
            hits = search_song(args.search, cookie, 5)
        except Exception as exc:
            print(f"❌ 搜索失败：{exc!r}")
            return 1
        if not hits:
            print("❌ 搜索没有结果（cookie 可能已失效）")
            return 1
        print(f"\n搜索「{args.search}」前 {len(hits)} 条：")
        for h in hits:
            print(f"  · {h['name']} - {h['artists']}  (id={h['id']})")

    if cfg["netease"]["playlist_id"]:
        return test(cfg)
    print("\n还没设置歌单 ID。如果你已经复制了歌单链接，可以用 --playlist 加上，例如：")
    print('  python set_cookie.py --playlist 123456789')
    return 0


if __name__ == "__main__":
    sys.exit(main())
