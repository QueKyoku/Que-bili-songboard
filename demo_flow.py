"""模拟点歌全流程（你看着）：

观众发弹幕 → 解析 → 入队 → 加进网易云歌单末尾 → 超上限自动清理

每一步都同时打印【弹幕】【点歌板】【网易云歌单】，方便对照。
全程走真实的解析与队列逻辑，只把"弹幕来源"换成模拟。
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, ".")

from songboard.config import Config
from songboard.netease import NeteasePlaylistDriver, weapi_post

cfg = Config.load(Path("config.json"))
d = NeteasePlaylistDriver(cfg)
LIMIT = int(cfg.get("netease.max_tracks", 5) or 5)


def api_get(p):
    with urllib.request.urlopen("http://127.0.0.1:8765" + p, timeout=20) as r:
        return json.loads(r.read().decode())


def api_post(p, body):
    req = urllib.request.Request("http://127.0.0.1:8765" + p,
                                data=json.dumps(body, ensure_ascii=False).encode(),
                                method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def playlist():
    res = weapi_post("/v6/playlist/detail", {"id": d.playlist_id, "n": 1000, "s": 8}, d.cookie)
    return [t.get("name") for t in (((res.get("playlist") or {}).get("tracks")) or [])]


def show():
    st = api_get("/api/state")
    cur = (st.get("current") or {}).get("song") or "（无）"
    q = [x["song"] for x in st["queue"] if x["state"] != "playing"]
    print(f"    ┌ 点歌板当前: {cur}")
    print(f"    ├ 点歌板队列: {q if q else '（空）'}")
    print(f"    └ 网易云歌单: {playlist()}")


print("=" * 70)
print("清场：清空点歌板队列和网易云歌单")
print("=" * 70)
api_post("/api/clear", {})
cur_pl = playlist()
if cur_pl:
    res = weapi_post("/v6/playlist/detail", {"id": d.playlist_id, "n": 1000, "s": 8}, d.cookie)
    ids = [int(t["id"]) for t in (((res.get("playlist") or {}).get("tracks")) or [])]
    d.delete_tracks(ids)
    time.sleep(3)
print("歌单:", playlist())
show()

# 模拟弹幕序列：正常点歌 + 干扰 + 边界
SCRIPT = [
    ("夜航船", "点歌 十年"),
    ("小满", "点歌 红玫瑰"),
    ("路过的骑士", "主播今天好帅"),            # 不是点歌指令，应被忽略
    ("三千", "点歌 单车"),
    ("阿岚", "点歌 好久不见"),
    ("老张", "点歌 十年"),                    # 重复点歌，应被去重
    ("小满", "点歌 浮夸"),                    # 同一人第二首
    ("夜航船", "我的点歌"),                   # 查询指令（已关闭 → 应无反应）
    ("Kirara", "点歌 K歌之王"),               # 第 6 首 → 触发上限清理
    ("阿岚", "点歌 富士山下"),                # 第 7 首 → 再触发一次清理
]

for i, (user, text) in enumerate(SCRIPT, 1):
    print()
    print("=" * 70)
    print(f"第 {i} 条弹幕   【{user}】: {text}")
    print("=" * 70)
    r = api_post("/api/simulate", {"text": text, "user": user})
    if r.get("reply"):
        print(f"    → 程序回复: {r['reply']}")
    else:
        print("    → （无回复：不是点歌指令）")
    time.sleep(6)
    show()

print()
print("=" * 70)
print("流程结束。等 8 秒看最终清理状态")
print("=" * 70)
time.sleep(8)
show()

print()
print("=== 点歌板日志（最近 20 条）===")
for line in api_get("/api/status")["log"][-20:]:
    print("  ", time.strftime("%H:%M:%S", time.localtime(line["ts"])), line["text"])
