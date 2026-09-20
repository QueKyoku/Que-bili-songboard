"""完整跑一遍：点歌板里放 5 首，观察队列是否按顺序接上。

⚠️ 这个脚本**不会**用 PLAY 强切歌。它只做三件事：
   1. 清空点歌板
   2. 依次把 5 首点进点歌板
   3. 观察"正在放/下一首"，并在网易云自然切歌时记录

切歌由你在网易云里按「下一首」或等它自然播完。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from songboard import ncmbridge as nb  # noqa: E402
from songboard.config import Config  # noqa: E402
from songboard.netease import build_driver  # noqa: E402

BASE = "http://127.0.0.1:8765"
QUEUE = os.path.join(os.environ["LOCALAPPDATA"], "Netease", "CloudMusic",
                     "WebData", "file", "playingList")
SONGS = ["十年", "浮夸", "单车", "富士山下", "苦瓜"]
T0 = time.time()


def say(m=""):
    print(f"[{time.time()-T0:6.1f}s] {m}" if m else "", flush=True)


def api(path, payload=None):
    if payload is None:
        req = urllib.request.Request(BASE + path)
    else:
        req = urllib.request.Request(
            BASE + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def playlist_names():
    d = build_driver(Config.load(Path(__file__).resolve().parent / "config.json"))
    return [n for _t, n in d.playlist_state()]


def board():
    d = api("/api/state")
    return [(it["song"], it["state"], it["netease_id"]) for it in (d.get("queue") or [])]


br = nb.NeteaseBridge(enabled=True)

print("=" * 74)
print("完整模拟：十年 → 浮夸 → 单车 → 富士山下 → 苦瓜")
print("=" * 74)
pl0 = playlist_names()
say(f"歌单基线: {pl0}")
st = br.now_playing() or {}
say(f"网易云: 正在放={st.get('name') or '(空闲)'}  下一首={st.get('next_name') or '(无)'}")
say()

say(">>> 清空点歌板")
api("/api/clear", {})
time.sleep(1.0)
say(f"    清空后: {board()}")
say()

print("=" * 74)
print("阶段一：把 5 首点进点歌板（程序只插『下一首』，不切歌）")
print("=" * 74)
for i, song in enumerate(SONGS, 1):
    res = api("/api/simulate", {"text": f"点歌 {song}", "user": f"观众{i}"})
    say(f"{i}. 点歌《{song}》 -> {res.get('reply')}")
    # 队头要先查时长+搜索才插队，耐心等（实测 5~10 秒）
    for _ in range(16):
        time.sleep(1.5)
        s = br.now_playing() or {}
        if s.get("next_name"):
            break
    s = br.now_playing() or {}
    say(f"     正在放={s.get('name') or '(空闲)'}  下一首={s.get('next_name') or '(无)'}")
say()

say("点歌板队列:")
for name, state, nid in board():
    say(f"    {name:12} {state:8} id={nid}")
say()
st = br.now_playing() or {}
say(f"网易云: 正在放={st.get('name')}  下一首={st.get('next_name')}")
say()

print("=" * 74)
print("阶段二：轮到你了 —— 在网易云里按「下一首」或等它自然播完")
print("        每切一首，程序会把下一首排上去。我在旁边盯着记录。")
print("        （最多观察 8 分钟，五首都放过就自动收工）")
print("=" * 74)
say()

seen: list[str] = []
prev = None
deadline = time.time() + 480
while time.time() < deadline:
    time.sleep(2.0)
    s = br.now_playing() or {}
    cur, nxt = s.get("name"), s.get("next_name")
    if cur != prev:
        prev = cur
        if cur:
            seen.append(str(cur))
        say(f"正在放={cur or '(空闲)'}   下一首={nxt or '(无)'}")
    if len([x for x in seen if any(e[:2] in x for e in SONGS)]) >= len(SONGS):
        say("五首都已播放过，收工")
        break

say()
print("=" * 74)
print("结果")
print("=" * 74)
say(f"依次播放: {seen}")
pl1 = playlist_names()
say()
say("点歌板最终状态:")
for name, state, nid in board():
    say(f"    {name:12} {state:8} id={nid}")
say()
say(f"歌单({len(pl1)}): {pl1}")
if pl1 == pl0:
    say("✅ 歌单完全没变")
else:
    say(f"❌ 歌单被改了！前={pl0} 后={pl1}")
