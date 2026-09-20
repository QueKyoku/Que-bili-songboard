"""完整模拟：按「江南 → 天下 → 单车 → 富士山下 → 苦瓜」顺序点歌。

验证两件事：
  1. 歌单一个字节都不动
  2. 这五首按点歌顺序进入播放队列的"下一首"位置

推进方式：点完五首后，用桥依次"开始播放"每一首，模拟网易云自然播完切下一首，
每一步都记录"下一首"是谁。
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
# ⚠️ 必须挑**当前播放队列里没有的**歌。
# 否则旧残留可能恰好补上位置，让"看起来顺序对了"变成巧合，
# 测不出插入是否真的发生（实测踩过：单车/苦瓜 本来就在队列里）。
SONGS = ["刚好遇见你", "消愁", "成都", "体面", "追光者"]
T0 = time.time()


def say(m=""):
    print(f"[{time.time()-T0:5.1f}s] {m}" if m else "")


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
print("完整模拟：江南 → 天下 → 单车 → 富士山下 → 苦瓜")
print("=" * 78)

pl_before = playlist()
say(f"歌单基线: {pl_before}")
q_before = qnames()
say(f"队列基线({len(q_before)}): " + " | ".join(q_before))
st = br.now_playing()
say(f"网易云: 正在放={st.get('name')}  下一首={st.get('next_name')}")
say()
# 用集合差验证：点歌前后队列的**新增曲目**必须正好是这五首
missing_check = [s for s in SONGS if any(s in n for n in q_before)]
if missing_check:
    say(f"⚠️ 这些歌已经在队列里了，会把结论搞成巧合：{missing_check}")
    say("   （建议换掉再跑）")
    say()

# ---------- 依次点歌 ----------
for i, song in enumerate(SONGS, 1):
    say(f">>> [{i}/{len(SONGS)}] 点歌：{song}（观众{i}）")
    res = api("/api/simulate", {"text": f"点歌 {song}", "user": f"观众{i}"})
    say(f"    点歌板回复: {res.get('reply')}")
    # 等状态循环把队头插进去
    for _ in range(8):
        time.sleep(1.0)
        st = br.now_playing()
        if st.get("next_name"):
            say(f"    +{_}s  正在放={st.get('name')}  下一首={st.get('next_name')}")
        if st.get("next_name") and song[:2] in str(st["next_name"]):
            break
    say()

say("=== 五首点完后 ===")
st = br.now_playing()
say(f"网易云: 正在放={st.get('name')}  下一首={st.get('next_name')}")
d = api("/api/state")
say(f"点歌板队列({d['counts'].get('queue')}):")
for it in d.get("queue") or []:
    say(f"    {it['song']}  by {it['user']}  state={it['state']}  "
        f"netease_id={it['netease_id']}")
say()

# ---------- 推进播放：依次让每一首开播 ----------
# ⚠️ 必须耐心等：队头要先查时长、再搜索、才插队（实测要 5~10 秒）。
#    之前等 2.5 秒就判定，导致"下一首还是空的"被误读成失败。
say("=== 推进播放（模拟网易云依次播完切下一首）===")
order_seen: list[str] = []
for round_no in range(len(SONGS) + 2):
    # 等"下一首"出现（最多 20 秒）
    st = {}
    for _ in range(20):
        st = br.now_playing() or {}
        if st.get("next_track_id"):
            break
        time.sleep(1.0)
    nxt = st.get("next_name")
    say(f"  第{round_no}轮: 正在放={st.get('name')}  下一首={nxt}")
    tid = st.get("next_track_id")
    if not tid:
        say("    （等不到下一首了，结束）")
        break
    if nxt:
        order_seen.append(str(nxt))
    say(f"    >>> 让它开始播放（PLAY {tid}）")
    ok, msg = br.play_now(int(tid))
    if not ok:
        say(f"    !! 播放失败：{msg}")
        break
    time.sleep(3.0)

say()
print("=" * 78)
print("结果")
print("=" * 78)
say(f"依次出现在'下一首'的歌: {order_seen}")
expected = SONGS
matched = [s for s in order_seen if any(e[:2] in s for e in expected)]
say(f"匹配到点歌顺序的部分: {matched}")
say()

# 检查顺序：五首是否按点歌顺序依次出现
pos = []
for e in expected:
    idx = next((i for i, s in enumerate(order_seen) if e[:2] in s), None)
    pos.append((e, idx))
say("每首第一次出现在'下一首'的轮次:")
for e, idx in pos:
    say(f"    {e:8} -> {'第 ' + str(idx) + ' 轮' if idx is not None else '未出现 ✗'}")
ok_order = all(idx is not None for _e, idx in pos) and \
    [i for _e, i in pos] == sorted(i for _e, i in pos)
say()
say(("✅ 五首按点歌顺序依次进入播放队列" if ok_order
     else "⚠️ 顺序或完整性有问题（见上表）"))
say()
say(f"最终队列({len(qnames())}): " + " | ".join(qnames()))
q_after = qnames()

# ---------- 严格校验：队列新增的曲目必须正好是这五首，且顺序一致 ----------
say()
say("=== 严格校验（集合差）===")
added = [n for n in q_after if n not in q_before]
say(f"  队列里**新增**的曲目({len(added)}): {added}")
# 把新增项对应回点歌顺序
seq = []
for n in added:
    hit = next((s for s in SONGS if s[:3] in n or n[:3] in s), None)
    if hit and hit not in seq:
        seq.append(hit)
say(f"  新增项按队列顺序对应的点歌: {seq}")
expected_seq = [s for s in SONGS if s in seq]
if seq == expected_seq and len(seq) == len(SONGS):
    say(f"  ✅ 新增的 {len(seq)} 首**正好就是**这五首，且顺序与点歌一致")
else:
    say(f"  ⚠️ 顺序或组成有偏差（期望 {SONGS}，实得 {seq}）")

pl_after = playlist()
say()
say(f"歌单({len(pl_after)}): {pl_after}")
if pl_after == pl_before:
    say("✅ 歌单完全没变")
else:
    say(f"❌ 歌单被改了！前={pl_before} 后={pl_after}")
