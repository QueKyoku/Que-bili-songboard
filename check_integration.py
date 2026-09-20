"""验证集成路径：点歌入队 → 写歌单 → 桥插入播放队列。

用 mock 驱动替代真实歌单写入（避免往你的歌单加测试歌），
但**桥是真的**——真的去问真播放器当前状态，真的调 insert_next 的逻辑。
所以只验证"接线是否正确 + 真实桥状态判断是否正确"，
不产生任何歌单/队列副作用（insert_next 被替换成记录调用）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from songboard.config import Config  # noqa: E402
from songboard.main import App  # noqa: E402
from songboard.netease import MusicDriver  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


class FakeDriver(MusicDriver):
    """假驱动：不碰真实歌单，只记录调用。"""

    name = "fake"

    def __init__(self) -> None:
        self.appended: list[str] = []

    async def test(self):
        return True, "fake ok"

    async def add_song(self, song, user=""):
        return True, f"fake added {song}", {"id": 999001, "name": song,
                                            "artists": "测试"}

    async def append_song(self, song, user=""):
        self.appended.append(song)
        return True, f"fake appended {song}", {"id": 999001, "name": song,
                                               "artists": "测试"}

    def playlist_state(self):
        return [(999001, "测试曲")]

    def status(self):
        return {"driver": self.name, "enabled": True}


async def main() -> int:
    root = Path(__file__).resolve().parent
    cfg = Config.load(root / "config.json")
    cfg["mode"] = "demo"
    cfg["netease.enabled"] = True
    cfg["netease.auto_add"] = True
    cfg["netease.max_tracks"] = 50        # 别触发清理
    cfg["ncm_bridge.enabled"] = True

    app = App(cfg, persist=False)
    fake = FakeDriver()
    app.driver = fake

    print("== 1. 桥可用性（真实探测）==")
    avail = app.bridge.available(refresh_after=0.0)
    check("真实桥可用", avail, app.bridge.describe_safe())
    st = app.bridge.status()
    check("status() 里 available=True", st["available"] is True,
          json.dumps(st, ensure_ascii=False))

    print("\n== 2. 真实读取播放器状态（now_playing）==")
    now = app.bridge.now_playing()
    check("能读到当前播放曲目", now is not None and bool(now.get("track_id")),
          f"{now.get('name')} - {now.get('artist')}" if now else "None")
    check("能读到下一首", now is not None and bool(now.get("next_track_id")),
          f"{now.get('next_name')}" if now else "None")
    idle = now is None or not now.get("track_id")
    print(f"  (当前 idle={idle} → 集成逻辑会走 "
          f"{'play_if_idle' if idle else 'insert_next'} 分支)")

    print("\n== 3. 集成接线：_queue_via_bridge 真的调到桥 ==")
    calls: list[tuple[str, object]] = []

    def fake_insert_next(song_id):
        calls.append(("insert_next", song_id))
        return True, "fake inserted"

    def fake_play_now(song_id):
        calls.append(("play_now", song_id))
        return True, "fake played"

    app.bridge.insert_next = fake_insert_next      # type: ignore[method-assign]
    app.bridge.play_now = fake_play_now            # type: ignore[method-assign]

    class Item:
        song = "接线测试曲"

    await app._queue_via_bridge(Item(), 999001)
    check("确实调用了桥（insert_next 或 play_now）", len(calls) == 1, str(calls))
    if calls:
        kind, sid = calls[0]
        check("传进去的曲目 id 正确", sid == 999001, f"{kind}({sid})")
        expect = "play_now" if idle else "insert_next"
        check(f"走了正确的分支（期望 {expect}）", kind == expect, kind)

    print("\n== 4. 桥关闭时必须静默跳过 ==")
    calls.clear()
    app.cfg["ncm_bridge.enabled"] = False
    app.bridge.enabled = False
    await app._queue_via_bridge(Item(), 999001)
    check("桥关闭时不调用任何桥方法", not calls, str(calls))

    print("\n== 5. 桥异常时不能拖垮点歌流程 ==")
    app.cfg["ncm_bridge.enabled"] = True
    app.bridge.enabled = True

    def boom(*_a, **_k):
        raise RuntimeError("模拟桥爆炸")

    app.bridge.available = boom                    # type: ignore[method-assign]
    try:
        await app._queue_via_bridge(Item(), 999001)
        check("桥抛异常时 _queue_via_bridge 不向外抛", True)
    except Exception as exc:  # noqa: BLE001
        check("桥抛异常时 _queue_via_bridge 不向外抛", False, repr(exc))

    failed = [r for r in results if not r[1]]
    print(f"\n{'=' * 56}\n共 {len(results)} 项，通过 {len(results)-len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  FAIL: {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
