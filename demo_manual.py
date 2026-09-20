"""按主播要求的流程做完整模拟：只插队列，切歌由**人**来切。

流程（主播原话）：
  1. 先把歌放到点歌板里
  2. A 在播的时候把 B 加到"下一首"
  3. 等 B 在播的时候再加 C
  4. 播放列表里已有的、又被点第二次的，无所谓之前有没有，照样执行"加到下一首"

⚠️ 本脚本**不会**用 PLAY 强切歌（之前那样做是错的，等于替主播按了下一首）。
   它只负责点歌 + 观察；切歌由你在网易云里按"下一首"（或等它自然播完）。
   每次检测到"正在放"变了，就打印当前状态。
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
SONGS = ["消愁", "成都", "刚好遇见你", "追光者", "夜空中最亮的星"]
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


def playlist():
    d = build_driver(Config.load(Path(__file__).resolve().parent / "config.json"))
    return [n for _t, n in d.playlist_state()]


def qnames():
    try:
        items = json.load(open(QUEUE, encoding="utf-8")).get("list") or []
    except Exception:
        return []
    items = sorted(items, key=lambda x: x.get("displayOrder", 0))
    return [(it.get("track") or {}).get("name") or "?" for it in items]


br = nb.NeteaseBridge(enabled=True)

print("=" * 78)
print("完整模拟（切歌由人来做，程序只插队列）")
print("=" * 78)
pl0 = playlist()
q0 = qnames()
say(f"歌单基线: {pl0}")
say(f"队列基线({len(q0)}): " + " | ".join(q0))
st = br.now_playing() or {}
say(f"网易云: 正在放={st.get('name')}  下一首={st.get('next_name')}")
say()

print("=" * 78)
print("阶段一：把五首放进点歌板（程序只插『下一首』，不切歌）")
print("=" * 78)
for i, song in enumerate(SONGS, 1):
    res = api("/api/simulate", {"text": f"点歌 {song}", "user": f"观众{i}"})
    say(f"{i}. 点歌《{song}》 -> {res.get('reply')}")
    # 等状态循环处理（查时长 + 搜索 + 插队，实测要 5~10 秒）
    last_next = None
    for _ in range(14):
        time.sleep(1.5)
        s = br.now_playing() or {}
        nxt = s.get("next_name")
        if nxt != last_next:
            say(f"     正在放={s.get('name')}  下一首={nxt}")
            last_next = nxt
        if nxt and song[:2] in str(nxt):
            break
    say()

say("点歌板队列:")
d = api("/api/state")
for it in d.get("queue") or []:
    say(f"    {it['song']}  by {it['user']}  state={it['state']}  "
        f"netease_id={it['netease_id']}")
say()
say(f"队列({len(qnames())}): " + " | ".join(qnames()))
say()

print("=" * 78)
print("阶段二：现在轮到你了 —— 请在网易云里按『下一首』（或等它播完）")
print("        每切一首，程序会把下一首排上去；我在这里盯着记录")
print("=" * 78)
say(f"当前: 正在放={(br.now_playing() or {}).get('name')}  "
    f"下一首={(br.now_playing() or {}).get('next_name')}")
say()

seen: list[str] = []
prev_cur = None
deadline = time.time() + 420          # 最多观察 7 分钟
while time.time() < deadline:
    time.sleep(2.0)
    s = br.now_playing() or {}
    cur, nxt = s.get("name"), s.get("next_name")
    if cur != prev_cur:
        prev_cur = cur
        if cur:
            seen.append(str(cur))
        say(f"正在放={cur}  下一首={nxt}")
    # 五首都放过就收工
    if len([x for x in seen if any(e[:2] in x for e in SONGS)]) >= len(SONGS):
        say("五首都已经播放过，结束观察")
        break

say()
print("=" * 78)
print("结果")
print("=" * 78)
say(f"依次播放的歌: {seen}")
pl1 = playlist()
q1 = qnames()
say()
say(f"队列({len(q1)}): " + " | ".join(q1))
added = [n for n in q1 if n not in q0]
say(f"新增到队列的: {added}")
say()
say(f"歌单({len(pl1)}): {pl1}")
if pl1 == pl0:
    say("✅ 歌单完全没变")
else:
    say(f"❌ 歌单被改了！前={pl0} 后={pl1}")
