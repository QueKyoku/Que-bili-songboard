"""用 CDP 网络监听抓网易云客户端的 API 调用（只记录，不改行为）。

和 XHR 钩子互补：这个能看到真实发出的请求 + 响应，包括带 cookie 的。
"""
import asyncio
import json
import sys
import time
import urllib.request

import websockets

WATCH_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 90

# 只关心可能的"写队列/加歌"接口
KEYS = ("playlist", "queue", "track", "manipulate", "batch", "song/detail", "np", "radio",
        "insert", "next")


async def main():
    with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=8) as r:
        pages = json.loads(r.read().decode("utf-8", "replace"))
    target = next((p for p in pages if "app.html" in (p.get("url") or "")), None)
    if not target:
        print("CDP 不可用")
        return

    async with websockets.connect(target["webSocketDebuggerUrl"],
                                  max_size=64 * 1024 * 1024) as ws:
        counter = [0]
        pending = {}

        async def send(method, params=None):
            counter[0] += 1
            mid = counter[0]
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            return mid

        await send("Network.enable", {"maxTotalBufferSize": 64 * 1024 * 1024})
        await send("Runtime.enable")
        print(f"网络监听已开启，观察 {WATCH_SECONDS} 秒…")
        print("请现在在网易云里做：右键任意歌曲 →「下一首播放」\n")

        t0 = time.time()
        hits = []
        while time.time() - t0 < WATCH_SECONDS:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            method = msg.get("method")
            params = msg.get("params") or {}

            if method == "Network.requestWillBeSent":
                req = params.get("request") or {}
                url = req.get("url") or ""
                m = req.get("method") or "GET"
                if m == "POST" or any(k in url.lower() for k in KEYS):
                    rid = params.get("requestId")
                    pending[rid] = {"url": url, "method": m,
                                    "postData": (req.get("postData") or "")[:400],
                                    "t": time.time()}
                    if any(k in url.lower() for k in ("manipulate", "queue", "insert")):
                        print(f"  ★ [{m}] {url[:140]}")
                        if req.get("postData"):
                            print(f"      body: {req['postData'][:250]}")

            elif method == "Network.responseReceived":
                rid = params.get("requestId")
                if rid in pending:
                    resp = params.get("response") or {}
                    info = pending.pop(rid)
                    info["status"] = resp.get("status")
                    hits.append(info)

        print(f"\n=== 观察结束，共捕获 {len(hits)} 个写/相关请求 ===")
        for h in hits:
            mark = "★" if any(k in h["url"].lower()
                              for k in ("manipulate", "queue", "insert")) else " "
            print(f" {mark} [{h['method']}] {h['status']} {h['url'][:120]}")
            if h.get("postData"):
                print(f"      body: {h['postData'][:200]}")


asyncio.run(main())
