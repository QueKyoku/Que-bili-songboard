"""对着**正在运行的服务**模拟送礼物 / 上舰 / 醒目留言，验证点歌门槛。

为什么需要它：门槛规则依赖 B 站的付费事件（`SEND_GIFT` / `GUARD_BUY` /
`SUPER_CHAT_MESSAGE`），这些只有真开播、真有人送才收得到。想调门槛
（"设 1000 瓜子到底合不合适"）总不能每次都求观众刷一个礼物。
这个脚本走的是 `/api/simulate`，和服务收到真弹幕时**同一条业务路径**，
只是事件来源是本地造的。

用法：

    python demo_gift.py 小明 1500              # 小明送 1500 瓜子的付费礼物
    python demo_gift.py 小明 1500 --ask 稻香    # 再让小明点一首，看放不放行
    python demo_gift.py 小明 0 --free          # 免费礼物（银色瓜子）
    python demo_gift.py 小明 --guard 3         # 小明上舰（3=舰长）
    python demo_gift.py 小明 --sc 30           # 小明发 30 元醒目留言
    python demo_gift.py --status               # 只看当前门槛和排行
    python demo_gift.py --reset                # 清空所有贡献记录

端口不是 8765 时：`python demo_gift.py 小明 1500 --port 8800`
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def api(base: str, path: str, payload: dict | None = None) -> dict:
    if payload is None:
        req = urllib.request.Request(base + path)
    else:
        req = urllib.request.Request(
            base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def show_gate(st: dict) -> None:
    gg = (st.get("status") or {}).get("gift_gate") or {}
    if not gg:
        print("  服务没返回 gift_gate 状态（版本太旧？）")
        return
    mode_cn = {"min_total": "累计金额达标", "any_paid": "送过付费礼物即可",
               "per_send": "每次消耗额度", "guard_only": "仅舰长"}.get(
        gg.get("mode"), str(gg.get("mode")))
    win = gg.get("window_seconds") or 0
    print(f"  门槛：{'开启' if gg.get('enabled') else '关闭'}"
          f"  方式：{mode_cn}  金额：{gg.get('min_coin')} 瓜子"
          f"  时效：{'永久' if not win else str(win) + ' 秒'}"
          f"  只认付费礼物：{'是' if gg.get('require_paid') else '否'}")
    print(f"  统计：送礼 {gg.get('paid_gifts')} 笔"
          f"（其中免费 {gg.get('free_gifts')} 笔）"
          f"  放行 {gg.get('allowed')} 次  拦下 {gg.get('blocked')} 次"
          f"  记了 {gg.get('known_users')} 个人")
    top = gg.get("top") or []
    if top:
        print("  贡献排行：")
        for row in top:
            tail = []
            if row.get("guard"):
                tail.append("舰长" + str(row.get("guard")))
            if row.get("spent"):
                tail.append(f"已用 {row.get('spent')}")
            extra = ("  " + " ".join(tail)) if tail else ""
            print(f"    {row.get('name')}  累计 {row.get('coin')} 瓜子"
                  f"  礼物 {row.get('gifts')} 个{extra}")
    blocked = gg.get("recent_blocks") or []
    if blocked:
        print("  最近被拦：")
        for row in blocked[:5]:
            print(f"    {row.get('user')}  {row.get('reason')}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="模拟送礼物，验证点歌门槛（对着运行中的服务）")
    ap.add_argument("user", nargs="?", default="测试观众", help="送礼的人")
    ap.add_argument("coin", nargs="?", type=int, default=1000,
                    help="礼物金额（瓜子，1 元 = 1000）")
    ap.add_argument("--free", action="store_true", help="送免费礼物（银色瓜子）")
    ap.add_argument("--guard", type=int, metavar="LEVEL",
                    help="模拟上舰：1=总督 2=提督 3=舰长")
    ap.add_argument("--sc", type=int, metavar="元", help="模拟醒目留言（元）")
    ap.add_argument("--ask", metavar="歌名", help="送完再让这个人点一首，看放不放行")
    ap.add_argument("--status", action="store_true", help="只看门槛状态，不送东西")
    ap.add_argument("--reset", action="store_true", help="清空所有贡献记录")
    ap.add_argument("--port", type=int, default=8765, help="服务端口（默认 8765）")
    args = ap.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    try:
        st = api(base, "/api/state")
    except urllib.error.URLError as exc:
        print(f"连不上 {base}：{exc}\n先把服务跑起来：python -m songboard")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"读取服务状态失败：{exc!r}")
        return 1

    print("=" * 70)
    print(f"点歌门槛 · {base}")
    print("=" * 70)

    if args.reset:
        api(base, "/api/gift_gate/reset", {})
        print("已清空所有贡献记录。")
        show_gate(api(base, "/api/state"))
        return 0

    if args.status:
        show_gate(st)
        return 0

    # ---- 造一笔付费事件 ----
    if args.guard:
        payload = {"kind": "guard", "text": str(args.guard),
                   "user": args.user, "coin": args.coin}
        what = f"上舰（等级 {args.guard}）"
    elif args.sc:
        payload = {"kind": "super_chat", "text": "测试醒目留言",
                   "user": args.user, "coin": args.sc}
        what = f"醒目留言 {args.sc} 元"
    else:
        payload = {"kind": "gift", "text": "测试礼物", "user": args.user,
                   "coin": args.coin, "paid": not args.free}
        what = f"{'免费' if args.free else '付费'}礼物 {args.coin} 瓜子"

    print(f">>> 模拟：{args.user} {what}")
    try:
        api(base, "/api/simulate", payload)
    except Exception as exc:  # noqa: BLE001
        print(f"模拟失败：{exc!r}")
        return 1

    show_gate(api(base, "/api/state"))

    # ---- 可选：让这个人点一首，直接看门槛判定的结果 ----
    if args.ask:
        print()
        print(f">>> 模拟：{args.user} 发「点歌 {args.ask}」")
        res = api(base, "/api/simulate",
                  {"text": f"点歌 {args.ask}", "user": args.user})
        reply = res.get("reply") or ""
        print(f"    点歌板回复：{reply or '（无）'}")
        # 被门槛拦下时回复里不会出现"已加入队列"
        if "已加入" in reply:
            print("    ✅ 放行")
        elif reply:
            print("    🚫 被拦下（上面 show_gate 里有原因）")
        else:
            print("    ⚠️ 没有回复 —— 可能指令没被识别，或门槛拦下时"
                  "日志里才有原因（看控制台）")

    print()
    print("提示：真实开播时这些事件来自 B 站服务端，判定路径和这里完全一样；")
    print("      贡献记录只在内存里，服务重启就清空。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
