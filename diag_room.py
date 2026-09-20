"""直连诊断：把你房间收到的每一帧都打出来，看弹幕到底有没有下来。

用法：python diag_room.py <房间号> [观察秒数]

⚠️ 房间号必须显式给出，不设默认值——否则仓库里就写死了某个人自己的房间号。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import websockets  # noqa: E402

from songboard.bilibili import (  # noqa: E402
    BilibiliDanmaku, HEADER, OP_AUTH, OP_HEARTBEAT, OP_MESSAGE, WsHeaderCompat,
    _http_json, _ws_headers, encode_packet, iter_packets,
)

if len(sys.argv) < 2 or not sys.argv[1].isdigit():
    print(__doc__)
    print("例：python diag_room.py 12345 60")
    sys.exit(2)

ROOM = int(sys.argv[1])
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 60


async def main() -> int:
    print(f"=== 直连诊断：房间 {ROOM}，观察 {SECONDS} 秒 ===\n")

    # 1) 房间信息
    info = _http_json("https://api.live.bilibili.com/room/v1/Room/room_init", {"id": ROOM})
    d = info.get("data") or {}
    print(f"[房间] code={info.get('code')} 真实房间号={d.get('room_id')} "
          f"主播uid={d.get('uid')} 直播状态={d.get('live_status')}（1=正在直播）")
    real_room = int(d.get("room_id") or ROOM)

    # 2) 我连的这个房间现在收了多少人在看 / 最近有没有弹幕历史
    try:
        hist = _http_json("https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory",
                          {"roomid": real_room})
        rooms = (hist.get("data") or {}).get("room") or []
        print(f"[历史弹幕] 接口 code={hist.get('code')}，能拉到最近 {len(rooms)} 条")
        for one in rooms[-5:]:
            print(f"           {one.get('nickname')}: {one.get('text')}")
        if not rooms:
            print("           ⚠️ 历史弹幕也是空的——说明这个房间确实没有弹幕产生，")
            print("              或者你发弹幕的地方不是这个房间号")
    except Exception as exc:
        print(f"[历史弹幕] 查询失败 {exc!r}")

    # 3) 直连弹幕服务器，逐帧打印
    dm = BilibiliDanmaku(ROOM, lambda ev: asyncio.sleep(0), None)
    token, hosts = await dm.fetch_token()
    url = f"wss://{hosts[0]['host']}:{hosts[0].get('wss_port', 443)}/sub"
    print(f"\n[连接] {hosts[0]['host']} / roomid={dm.room_id} token={len(token)}字符")

    cmds: dict[str, int] = {}
    danmaku: list[str] = []
    t0 = time.time()

    async with websockets.connect(url, **WsHeaderCompat.kwargs(
            _ws_headers("", "https://live.bilibili.com"))) as ws:
        await ws.send(encode_packet({"uid": 0, "roomid": dm.room_id, "protover": 3,
                                    "platform": "web", "type": 2, "key": token, "buvid": ""},
                                    op=OP_AUTH, proto=1))

        async def beat():
            while True:
                await asyncio.sleep(30)
                await ws.send(encode_packet(b"", op=OP_HEARTBEAT, proto=1))
                print(f"  [{time.time()-t0:5.1f}s] → 已发心跳")

        hb = asyncio.create_task(beat())
        try:
            while time.time() - t0 < SECONDS:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=SECONDS)
                except asyncio.TimeoutError:
                    break
                if isinstance(raw, str):
                    raw = raw.encode()
                total, _hl, proto, op, _seq = HEADER.unpack_from(raw, 0)
                msgs = list(iter_packets(raw))
                names = [str(m.get("cmd")) for m in msgs]
                for n in names:
                    cmds[n] = cmds.get(n, 0) + 1
                tag = "DANMU!" if any(n.startswith("DANMU_MSG") for n in names) else "      "
                print(f"  [{time.time()-t0:5.1f}s] {tag} {len(raw):5d}B proto={proto} op={op} "
                      f"→ {names}")
                for m in msgs:
                    if str(m.get("cmd", "")).startswith("DANMU_MSG"):
                        info2 = m.get("info") or []
                        line = f"{info2[2][1]}: {info2[1]}"
                        danmaku.append(line)
                        print(f"           ★ 弹幕内容 → {line}")
        finally:
            hb.cancel()

    print(f"\n=== 观察结束（{time.time()-t0:.0f}s）===")
    print(f"收到帧的消息类型统计：{cmds if cmds else '（一个包都没收到）'}")
    print(f"其中弹幕 {len(danmaku)} 条：{danmaku[:10]}")
    if not cmds:
        print("\n⚠️ 一个包都没收到：连接建立了但服务器不推任何数据。")
        print("   这通常意味着这个房间号当前没有活跃的消息流，")
        print("   或者你的弹幕发在了另一个房间。")
    elif not danmaku:
        print("\n⚠️ 收到了其他消息但没有弹幕。请确认你发弹幕的房间号是不是这个。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
