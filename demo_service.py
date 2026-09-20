"""对着**正在运行的服务**模拟一次真实点歌，实时展示歌单与播放队列的变化。

和 demo_live.py 的区别（也是它的错误所在）：
  demo_live.py 自己 new 了一个 App 实例，而"写歌单 → 桥插队列"是跑在
  服务的 _status_loop 里的，新建实例根本没有那个循环，所以什么都不发生。
  这里改为通过 REST 打**那台真正在跑的服务**（叠加层连的同一台）。

用法：python demo_service.py "点歌 富士山下" 测试观众
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8765"
QUEUE = os.path.join(os.environ["LOCALAPPDATA"], "Netease", "CloudMusic",
                     "WebData", "file", "playingList")
T0 = time.time()


def stamp() -> str:
    return f"{time.time() - T0:5.1f}s"


def say(msg: str = "") -> None:
    print(f"[{stamp()}] {msg}" if msg else "")


def api(path: str, payload: dict | None = None) -> dict:
    if payload is None:
        req = urllib.request.Request(BASE + path)
    else:
        req = urllib.request.Request(
            BASE + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def play_queue() -> list[tuple[str, str, bool]]:
    if not os.path.exists(QUEUE):
        return []
    try:
        data = json.load(open(QUEUE, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        # 网易云正在写这个文件时会读到空壳/半截 JSON，不是错误，忽略这一轮
        return []
    # ⚠️ 必须用 .get：写入瞬间文件里可能还没有 "list" 键
    items = data.get("list") or []
    try:
        items = sorted(items, key=lambda x: x.get("displayOrder", 0))
    except Exception:  # noqa: BLE001
        pass
    return [(str(it.get("trackId")), (it.get("track") or {}).get("name") or "",
             bool(it.get("isPlayedOnce"))) for it in items]


def show(tag: str, st: dict) -> None:
    say(f"── {tag} ──")
    counts = st.get("counts") or {}
    say(f"  点歌板计数: {json.dumps(counts, ensure_ascii=False)}")
    for it in st.get("queue") or []:
        say(f"    ・{it.get('song')}  by {it.get('user')}  "
            f"state={it.get('state')}  网易云id={it.get('netease_id')}")
    q = play_queue()
    say(f"  网易云队列({len(q)}): " + " | ".join(
        f"{n}{'[已播]' if p else ''}" for _t, n, p in q))
    say()


def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else "点歌 富士山下"
    user = sys.argv[2] if len(sys.argv) > 2 else "测试观众"
    target_name = text.split()[-1]

    print("=" * 76)
    print(f"对着运行中的服务模拟点歌：{user} 发送「{text}」")
    print("=" * 76)

    st = api("/api/state")
    status = st.get("status") or {}
    say(f"服务: mode={status.get('mode')} room={status.get('room_id')} "
        f"connected={status.get('connected')}")
    say(f"桥  : {(status.get('ncm_bridge') or {}).get('message')}")
    say()

    show("点歌前", st)

    say(f">>> POST /api/simulate  {user}：{text}")
    res = api("/api/simulate", {"text": text, "user": user})
    say(f"    点歌板回复: {res.get('reply')!r}")
    say()

    say("实时观察（服务的状态循环每 2 秒跑一轮）…")
    seen_stages: list[str] = []
    last_q = None
    for i in range(20):
        time.sleep(1.0)
        st = api("/api/state")
        status = st.get("status") or {}
        q = play_queue()
        counts = st.get("counts") or {}
        item = next((x for x in (st.get("queue") or [])
                     if x.get("song") == target_name), None)
        nid = item.get("netease_id") if item else None
        say(f"  +{i+1:2}s  计数={json.dumps(counts, ensure_ascii=False)}  "
            f"《{target_name}》netease_id={nid}  队列="
            + "/".join(n for _t, n, _p in q))
        if q != last_q:
            last_q = q
            say(f"        ↑ 队列发生变化")

        # 收敛条件：已经拿到 netease_id，且队列里出现了目标歌
        if nid and any(target_name in n for _t, n, _p in q):
            if i >= 2:
                break

    say()
    st = api("/api/state")
    show("点歌后", st)

    status = st.get("status") or {}
    br = status.get("ncm_bridge") or {}
    ne = status.get("netease") or {}
    say(f"桥统计  : 已插入 {br.get('inserted')} 首   可用={br.get('available')}")
    say(f"歌单驱动: 已加 {ne.get('added')} 首   重排 {ne.get('reorders')} 次")
    say()

    print("=" * 76)
    print("服务日志（最后 12 条）")
    print("=" * 76)
    try:
        log = api("/api/log")
        lines = log.get("log") or log.get("lines") or []
        for L in lines[-12:]:
            txt = L.get("text") if isinstance(L, dict) else str(L)
            print("  " + str(txt))
    except Exception as exc:  # noqa: BLE001
        print(f"  读取日志失败: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
