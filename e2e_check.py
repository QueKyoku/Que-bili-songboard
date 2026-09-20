"""端到端验证：起服务后连网页 API + WebSocket，确认点歌板真的会长。

    python e2e_check.py [http://127.0.0.1:8765]

⚠️ 这个脚本会往**正在运行的那个服务**加歌、切歌、清空队列。
   如果那个服务的 netease.auto_add = true，它会连带着往你**真实的网易云歌单**
   写歌（而且写的是"端到端测试曲"这种搜不到的名字，会一直失败重试刷日志）。
   所以默认检测到 auto_add 开着就直接拒绝运行。
   确实想跑：先关掉 config.json 里的 netease.auto_add，或设
   DSH_E2E_ALLOW_NETEASE_WRITE=1 强行继续（不推荐）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"

try:
    import websockets
except ImportError:
    print("需要 websockets 库")
    sys.exit(2)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def get(path: str) -> tuple[int, str]:
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "e2e"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status, r.read().decode("utf-8", "replace")


def post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def guard_netease_write() -> None:
    """检测目标服务是否会自动往真实歌单写歌；是的话拒绝运行。"""
    try:
        _, raw = get("/api/config")
        cfg = json.loads(raw)
    except Exception:
        try:
            _, raw = get("/api/status")
            cfg = json.loads(raw).get("status", {})
        except Exception as exc:  # noqa: BLE001
            print(f"!! 读不到服务配置，为安全起见拒绝运行：{exc!r}")
            sys.exit(3)

    ne = cfg.get("netease") or {}
    auto = bool(ne.get("auto_add"))
    enabled = bool(ne.get("enabled"))
    if auto and enabled and not os.environ.get("DSH_E2E_ALLOW_NETEASE_WRITE"):
        print("=" * 70)
        print("!! 已拒绝运行")
        print("=" * 70)
        print(f"目标服务 {BASE} 的 netease.auto_add = true 且 enabled = true，")
        print("意味着本脚本加进去的『端到端测试曲』会真的写进你的网易云歌单")
        print(f"（playlist_id={ne.get('playlist_id')!r}）。")
        print()
        print("它根本搜不到这首歌，所以只会反复失败刷日志，什么也写不进去，")
        print("但会白白打网易云接口。")
        print()
        print("要做端到端检查，请先把 config.json 里的 netease.auto_add 关掉，")
        print("或在**演示模式**下起一个独立实例再测。")
        print()
        print("确实要强行继续：设 DSH_E2E_ALLOW_NETEASE_WRITE=1")
        sys.exit(3)
    if auto and enabled:
        print("  ⚠️ 已按 DSH_E2E_ALLOW_NETEASE_WRITE 强行继续，"
              "你的真实歌单**会**被写入。")


async def main() -> int:
    print(f"== 端到端检查 {BASE} ==")
    guard_netease_write()
    code, html = get("/overlay")
    check("叠加层页面可访问", code == 200 and "点歌板" in html, f"{code}, {len(html)}B")
    code, html = get("/control")
    check("控制台页面可访问", code == 200 and "控制台" in html, f"{code}, {len(html)}B")
    code, raw = get("/api/state")
    state = json.loads(raw)
    check("状态接口返回快照", code == 200 and "queue" in state and "counts" in state,
          f"{code} counts={state.get('counts')}")
    code, raw = get("/api/status")
    st = json.loads(raw)
    check("状态接口含连接信息", code == 200 and st["status"].get("mode") in ("demo", "live"),
          f"mode={st['status'].get('mode')} connected={st['status'].get('connected')}")

    ws_url = BASE.replace("http://", "ws://") + "/ws"
    events: list[dict] = []
    async with websockets.connect(ws_url) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
        check("WebSocket 握手并推送 hello", hello.get("event") == "hello" and "snapshot" in hello)

        async def collect():
            try:
                while True:
                    events.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=20)))
            except (asyncio.TimeoutError, Exception):
                return

        worker = asyncio.create_task(collect())

        # 手动加点一首，验证 REST -> 广播 通路
        res = post("/api/add", {"song": "端到端测试曲", "user": "e2e", "force": True})
        check("手动加歌成功", bool(res.get("ok")), json.dumps(res, ensure_ascii=False)[:120])
        await asyncio.sleep(1.5)

        # 等演示模式自动产生弹幕点歌
        await asyncio.sleep(6)
        worker.cancel()

    kinds = [e.get("event") for e in events]
    check("广播收到 added/playing 事件", any(k in ("added", "playing") for k in kinds),
          str(kinds[:12]))
    snapshot = next((e.get("snapshot") for e in reversed(events) if e.get("snapshot")), None)
    if snapshot:
        songs = [s["song"] for s in snapshot["queue"]]
        check("队列里能看到刚加的歌", "端到端测试曲" in songs, str(songs))
    else:
        check("广播里带快照", False, "没有收到快照")

    # 切歌 + 清空
    res = post("/api/next", {"reason": "skip"})
    check("切歌接口可用", bool(res.get("ok")), json.dumps(res, ensure_ascii=False)[:100])
    res = post("/api/clear", {})
    check("清空接口可用", bool(res.get("ok")))
    _, raw = get("/api/state")
    check("清空后队列为空", json.loads(raw)["counts"]["queue"] == 0)

    failed = [r for r in results if not r[1]]
    print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  FAIL: {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
