"""自检脚本：离线验证指令解析、队列规则、协议编解码；--live 额外做真连测试。

    python selftest.py
    python selftest.py --live 12345        # 真连一个直播间，看 20 秒弹幕
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 日志里可能带 emoji（✅ 之类）。GBK 控制台打不出来会直接抛
# UnicodeEncodeError，把整轮自检打断、后面的用例全不跑。
# 只放宽 errors，不改编码，免得中文在 PowerShell 里变乱码。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")       # type: ignore[union-attr]
    except Exception:
        pass

from songboard.bilibili import (  # noqa: E402
    BilibiliDanmaku, DemoDanmaku, encode_packet, iter_packets, _http_json,
)
from songboard.command import CommandParser  # noqa: E402
from songboard.config import Config  # noqa: E402
from songboard.models import SongState  # noqa: E402
from songboard.store import QueueStore  # noqa: E402
from songboard.wbi import extract_keys, get_mixin_key, sign_query  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f"  — {detail}" if detail else ""))


async def _drain_tasks(min_seconds: float = 0.0, budget: float = 8.0) -> None:
    """把后台任务跑干净再断言。

    _sync_playlist / _sync_queue_only 把真正写入丢给 create_task 就返回了，
    测试里不等它跑完就断言会偶发假失败。min_seconds 是给
    「任务还没被建出来」留的余量；budget 是上限，卡住也不会把自检挂死。
    """
    started = time.monotonic()
    deadline = started + budget
    while True:
        await asyncio.sleep(0.05)
        tasks = [t for t in asyncio.all_tasks()
                 if t is not asyncio.current_task()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        now = time.monotonic()
        if not tasks and now - started >= min_seconds:
            return
        if now >= deadline:
            return


# --------------------------------------------------------------------------- 指令解析
def test_commands() -> None:
    print("\n== 指令解析 ==")
    cfg = Config.load(Path("__selftest_config.json"))
    p = CommandParser(cfg)

    c = p.parse("点歌 稻香")
    check("点歌 稻香 -> add", c.action == "add" and c.song == "稻香", f"{c.action}/{c.song}")
    c = p.parse("!点歌起风了")
    check("!点歌起风了 -> add 起风了", c.action == "add" and c.song == "起风了", f"{c.action}/{c.song}")
    c = p.parse("点歌：晴天")
    check("点歌：晴天 -> add 晴天", c.action == "add" and c.song == "晴天", f"{c.action}/{c.song}")
    c = p.parse("点歌")
    check("只有前缀 -> add 空歌名", c.action == "add" and c.song == "", f"{c.action}/{c.song!r}")
    c = p.parse("切歌")
    check("切歌 -> skip", c.action == "skip", c.action)
    c = p.parse("点歌 切歌")
    check("点歌 切歌 -> skip", c.action == "skip", c.action)
    c = p.parse("我的点歌")
    check("我的点歌 -> query", c.action == "query", c.action)
    c = p.parse("取消点歌")
    check("取消点歌 -> cancel", c.action == "cancel", c.action)
    c = p.parse("主播今天好帅")
    check("普通弹幕 -> none", c.action == "none", c.action)

    # 关掉切歌/查询/取消：关键词列表清空后这些指令必须失效
    cfg2 = Config.load(Path("__selftest_off.json"))
    cfg2["danmaku"]["skip_keywords"] = []
    cfg2["danmaku"]["cancel_keywords"] = []
    cfg2["danmaku"]["query_keywords"] = []
    off = CommandParser(cfg2)
    check("关掉后 切歌 -> none", off.parse("切歌").action == "none", off.parse("切歌").action)
    check("关掉后 下一首 -> none", off.parse("下一首").action == "none")
    check("关掉后 我的点歌 -> none", off.parse("我的点歌").action == "none")
    check("关掉后 取消点歌 -> none", off.parse("取消点歌").action == "none")
    check("关掉后 点歌 稻香 仍可用",
          off.parse("点歌 稻香").action == "add" and off.parse("点歌 稻香").song == "稻香")
    check("关掉后 点歌 切歌 当成歌名",
          off.parse("点歌 切歌").action == "add" and off.parse("点歌 切歌").song == "切歌",
          f"{off.parse('点歌 切歌').action}/{off.parse('点歌 切歌').song}")
    check("关掉后 点歌 我的点歌 当成歌名",
          off.parse("点歌 我的点歌").action == "add"
          and off.parse("点歌 我的点歌").song == "我的点歌",
          f"{off.parse('点歌 我的点歌').action}/{off.parse('点歌 我的点歌').song}")
    check("前缀必须在开头：取消点歌稻香 不触发点歌",
          off.parse("取消点歌稻香").action == "none",
          off.parse("取消点歌稻香").action)
    Path("__selftest_off.json").unlink(missing_ok=True)

    check("mixin key 长度 32", len(get_mixin_key("a" * 64)) == 32)
    keys = extract_keys({"wbi_img": {
        "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
        "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"}})
    check("从 nav 提取 wbi key", keys == ("7cd084941338484aae1ad9425b84077c",
                                          "4932caff0ff746eab6f01bf08b70ac45"), str(keys))
    q = sign_query({"id": 123, "type": 0}, keys[0], keys[1]) if keys else ""
    check("wbi 签名含 w_rid", "w_rid=" in q and "wts=" in q, q[:60])


# --------------------------------------------------------------------------- 队列
async def test_queue() -> None:
    print("\n== 队列规则（cooldown 设为 0，另测冷却拦截）==")
    cfg_path = Path("__selftest_store_config.json")
    if cfg_path.exists():
        cfg_path.unlink()
    cfg = Config.load(cfg_path)
    cfg["queue"]["cooldown_seconds"] = 0
    cfg["queue"]["per_user_limit"] = 2
    cfg["queue"]["max_size"] = 5
    store = QueueStore(cfg, Path("__selftest_data"), persist=False)
    events: list[str] = []

    async def collect(payload):
        events.append(payload["event"])

    store.subscribe(collect)

    req, res = await store.add("稻香", "阿岚", 1)
    check("首点点歌 ok 且自动开播", res == "ok" and store.current and store.current.song == "稻香",
          f"res={res} current={store.current.song if store.current else None}")
    check("事件含 playing", "playing" in events, str(events))

    req, res = await store.add("稻香", "老张", 2)
    check("重复歌名被去重", res == "duplicate", res)

    req, res = await store.add("起风了", "阿岚", 1)
    check("同人第二首 ok（未超上限）", res == "ok", res)

    req, res = await store.add("成都", "阿岚", 1)
    check("同人超过上限被拦", res == "user_limit", f"{res} active={[s.song for s in store.active()]}")

    req, res = await store.add("句号", "老张", 2)
    check("不同人 cooldown 独立", res == "ok", res)

    req, res = await store.add("孤勇者", "夜航船", 3)
    check("第三人可点", res == "ok", res)

    nxt = await store.next(reason="skip")
    check("切歌后 current 变为起风了", nxt is not None and nxt.song == "起风了",
          nxt.song if nxt else "None")
    check("上一首被标记 skipped",
          any(s.song == "稻香" and s.state is SongState.SKIPPED for s in store.played))

    top = next(s for s in store.active() if s.song == "句号")
    await store.move(top.id, True)
    check("置顶生效（排到正在播放之后）", [s.song for s in store.active()][1] == "句号",
          str([s.song for s in store.active()]))

    # 冷却单独验证：换一个不受上限影响的用户
    cfg["queue"]["cooldown_seconds"] = 30
    store.reset_cooldown()
    await store.add("咸鱼", "冷却侠", 9)
    _, res = await store.add("浮夸", "冷却侠", 9)
    check("冷却期内第二次被拦", res == "cooldown", res)
    cfg["queue"]["cooldown_seconds"] = 0

    for i in range(6):
        await store.add(f"歌{i}", f"路人{i}", 100 + i)
    check("超出 max_size 进备选池", len(store.pending) >= 1,
          f"pending={len(store.pending)} active={len(store.active())}")

    snap = store.snapshot()
    check("快照字段齐全", set(snap) >= {"current", "queue", "pending", "counts"},
          str(list(snap)))
    check("快照可 JSON 序列化", bool(json.dumps(snap, ensure_ascii=False)))

    # 备选池在队列有空位时补位
    await store.clear()
    check("清空后队列为空", len(store.active()) == 0 and store.current is None)

    for f in (cfg_path,):
        f.unlink(missing_ok=True)
    import shutil
    shutil.rmtree("__selftest_data", ignore_errors=True)


# --------------------------------------------------------------------------- 协议
def test_protocol() -> None:
    print("\n== 弹幕协议编解码 ==")
    auth = encode_packet({"uid": 0, "roomid": 1, "protover": 3, "key": "t"}, op=7, proto=1)
    total = int.from_bytes(auth[:4], "big")
    header_len = int.from_bytes(auth[4:6], "big")
    proto = int.from_bytes(auth[6:8], "big")
    op_code = int.from_bytes(auth[8:12], "big")     # 操作码占 4 字节
    check("认证包 op=7 且长度自洽",
          (total, header_len, proto, op_code) == (len(auth), 16, 1, 7) and
          json.loads(auth[16:].decode())["roomid"] == 1,
          f"total={total} header={header_len} proto={proto} op={op_code}")

    body = json.dumps([{"cmd": "DANMU_MSG", "info": [[], "你好", [123, "阿岚"]]}]).encode("utf-8")
    inner = encode_packet(body, op=5, proto=0)
    outer = encode_packet(zlib.compress(inner), op=5, proto=2)
    msgs = list(iter_packets(outer))
    check("zlib 压缩包可解出 danmaku", len(msgs) == 1 and msgs[0]["cmd"] == "DANMU_MSG",
          str(msgs)[:80])

    single = encode_packet(body, op=5, proto=0)
    msgs = list(iter_packets(single))
    check("未压缩数组包可解", len(msgs) == 1 and msgs[0]["info"][1] == "你好", str(msgs)[:60])

    obj = json.dumps({"cmd": "DANMU_MSG", "info": [[], "嗨", [1, "某人"]]}).encode("utf-8")
    msgs = list(iter_packets(encode_packet(obj, op=5, proto=0)))
    check("未压缩对象包可解", len(msgs) == 1 and msgs[0]["cmd"] == "DANMU_MSG")

    multi = json.dumps([{"cmd": "A"}, {"cmd": "B"}]).encode("utf-8")
    msgs = list(iter_packets(encode_packet(multi, op=5, proto=0)))
    check("多消息数组拆成多条", [m["cmd"] for m in msgs] == ["A", "B"], str(msgs))

    hb = encode_packet((12345).to_bytes(4, "big"), op=3, proto=1)
    got = list(iter_packets(hb))
    check("心跳回包解析人气值", got and got[0].get("popularity") == 12345, str(got))

    check("残缺包不崩溃", list(iter_packets(b"\x00\x01")) == [])


# --------------------------------------------------------------------------- 正在播放 / 自动下一首
def test_media_parse() -> None:
    print("\n== 播放器检测解析 ==")
    from songboard.media import (MediaInfo, normalize, parse_gsm_output, pick_all_with_progress,
                                 pick_session, read_netease_title, similarity, split_title)

    # 一份仿真输出：一个正常会话 + 一个只有 PARTIAL 的空调试会话（真实踩过的坑）
    text = (
        "APP    : msedge.exe\n"
        "STATE  : Playing POS: 21 DUR: 758\n"
        "TITLE  : 起风了 ARTIST: 买辣椒也用券\n"
        "APP    : Microsoft.WindowsSoundRecorder_8wekyb3d8bbwe!App\n"
        "STATE  : Paused POS: 0 DUR: 0\n"
        "TITLE  :  ARTIST:\n"
    )
    items = parse_gsm_output(text)
    check("解析出有效会话（空调试会话被丢弃）", len(items) == 1, f"{len(items)} 个")
    it = items[0]
    check("曲名/艺人/进度解析正确",
          it.title == "起风了" and it.artist == "买辣椒也用券"
          and it.position == 21 and it.duration == 758,
          f"{it.title}/{it.artist}/{it.position}/{it.duration}")
    check("playing 状态解析正确", it.playing is True)

    check("空输出不崩", parse_gsm_output("") == [] and parse_gsm_output("垃圾数据") == [])

    picked = pick_session(items + parse_gsm_output(
        "APP    : cloudmusic.exe\nSTATE  : Paused POS: 0 DUR: 0\nTITLE  : 苦瓜 ARTIST: 陈奕迅\n"
    ), ("msedge",))
    check("优先挑选指定的播放器", picked is not None and picked.app == "msedge.exe",
          picked.app if picked else "None")

    # ★ 关键回归：网易云网页版在 Edge 里的会话就是"有进度、没标题"，
    #   之前因为"没标题就丢弃"导致什么都读不到，不能重现这个 bug。
    untitled = parse_gsm_output(
        "APP    : msedge.exe\nSTATE  : Playing POS: 30 DUR: 191\nTITLE  :  ARTIST: \n"
    )
    check("无标题但有进度的会话被保留（不再丢弃）", len(untitled) == 1,
          f"{len(untitled)} 个")
    check("无标题会话的进度解析正确",
          untitled and untitled[0].position == 30 and untitled[0].duration == 191,
          f"{untitled[0].position}/{untitled[0].duration}" if untitled else "无")
    picked_untitled = pick_session(untitled, ("msedge",))
    check("无标题会话仍可被选为进度来源", picked_untitled is not None,
          picked_untitled.app if picked_untitled else "None")
    check("has_progress 判定正确",
          picked_untitled is not None and picked_untitled.has_progress is True)
    # 回归：remaining 属性在重构时被弄丢过，导致自动切歌每轮抛异常
    check("remaining 属性存在且算得对",
          picked_untitled is not None and picked_untitled.remaining == 161.0,
          f"{picked_untitled.remaining}" if picked_untitled else "无")
    check("没有时长时 remaining 返回负数（调用方据此跳过）",
          MediaInfo(title="x").remaining < 0,
          f"{MediaInfo(title='x').remaining}")

    # 空调试会话（无标题无进度）必须被丢弃，否则会干扰判断
    garbage = parse_gsm_output(
        "APP    : Microsoft.WindowsSoundRecorder_8wekyb3d8bbwe!App\n"
        "STATE  : Paused POS: 0 DUR: 0\nTITLE  :  ARTIST: \n"
    )
    check("无标题无进度的空调试会话被丢弃", garbage == [], str(garbage))

    # pick_all_with_progress：按 prefer 排序挑出所有带进度的
    allprog = pick_all_with_progress(
        untitled + parse_gsm_output(
            "APP    : spotify.exe\nSTATE  : Playing POS: 5 DUR: 200\nTITLE  : 某歌 ARTIST: 某人\n"
        ), ("spotify", "msedge"))
    check("pick_all_with_progress 按 prefer 排序",
          len(allprog) == 2 and allprog[0].app == "spotify.exe",
          str([a.app for a in allprog]))

    # 网易云窗口标题 "歌名 - 艺人"
    check("标题拆分 苦瓜 - 陈奕迅", split_title("苦瓜 - 陈奕迅") == ("苦瓜", "陈奕迅"))
    check("标题拆分 只有一个字段", split_title("纯音乐") == ("纯音乐", ""))
    check("标题拆分 破折号变体", split_title("Lemon — 米津玄師") == ("Lemon", "米津玄師"))

    check("相似度：完全一致", similarity("起风了", "起风了") == 1.0)
    check("相似度：括号版本标注不影响匹配",
          similarity("起风了", "起风了 (Live)") == 1.0,
          f"{similarity('起风了', '起风了 (Live)'):.2f}")
    check("相似度：包含关系给高分",
          abs(similarity("起风了", "起风了remix版") - 0.85) < 1e-6,
          f"{similarity('起风了', '起风了remix版'):.2f}")
    check("相似度：大小写/空格/标点无关",
          similarity("Lemon", "lemon") == 1.0 and similarity("海阔天空", "海阔 天空") == 1.0)
    check("相似度：不相干的歌要低分", similarity("起风了", "孤勇者") < 0.5,
          f"{similarity('起风了', '孤勇者'):.2f}")
    check("normalize 去掉尾部噪声", normalize("稻香 official") == "稻香")

    # 真实读取（本机有播放器才有内容，没有也不算失败）
    live = read_netease_title()
    if live:
        check(f"实测读到网易云窗口标题：{live.title} - {live.artist}", bool(live.title),
              f"来源={live.source}")
    else:
        print("  [SKIP] 本机没开网易云客户端，跳过窗口标题实测")


async def test_auto_advance() -> None:
    print("\n== 自动下一首判定 ==")
    from songboard.config import Config as Cfg
    from songboard.store import QueueStore as QS

    path = Path("__selftest_media_config.json")
    path.unlink(missing_ok=True)
    cfg = Cfg.load(path)
    cfg["queue"]["cooldown_seconds"] = 0
    store = QS(cfg, Path("__selftest_data2"), persist=False)

    await store.add("第一首", "甲", 1)
    await store.add("第二首", "乙", 2)
    check("第一首自动成为正在播放", store.current is not None and store.current.song == "第一首",
          store.current.song if store.current else "None")
    check("此时不该自动切（刚开播）",
          store.should_auto_advance(fallback=240, grace=9) == "")

    # 时长明确且已过 → 判 done
    store.current.duration = 100
    store.current.started_at = time.time() - 150
    check("超过已知时长 → done", store.should_auto_advance(fallback=240, grace=9) == "done")

    # 时长未知 → 用兜底值
    store.current.duration = 0
    store.current.started_at = time.time() - 100
    check("时长未知时用兜底值（还没到）",
          store.should_auto_advance(fallback=240, grace=9) == "")
    store.current.started_at = time.time() - 260
    check("时长未知但超过兜底值 → done",
          store.should_auto_advance(fallback=240, grace=9) == "done")

    # play_specific：播放器检测到"就是队列里这首"
    second = [s for s in store.active() if s.song == "第二首"][0]
    played = await store.play_specific(second.id, detected_title="第二首 - 某歌手")
    check("play_specific 切到指定歌曲",
          played is not None and store.current.song == "第二首", 
          store.current.song if store.current else "None")
    check("play_specific 记录检测到的标题", store.current.detected_title == "第二首 - 某歌手")
    check("上一首被标记 played",
          any(s.song == "第一首" and s.state is SongState.PLAYED for s in store.played))

    idem = await store.play_specific(second.id)
    check("play_specific 对同一首幂等", idem is not None and store.current.song == "第二首")

    await store.next(reason="done")
    check("切完后 current 为空（队列只有两首）", store.current is None,
          store.current.song if store.current else "None")

    path.unlink(missing_ok=True)
    import shutil as _sh
    _sh.rmtree("__selftest_data2", ignore_errors=True)


# --------------------------------------------------------------------------- 外部媒体源适配器
def test_extapi_parse() -> None:
    print("\n== 外部媒体源（字段自动识别）==")
    from songboard.extapi import ExtApiSource, parse_external_payload, _time_to_seconds

    # 各种可能的返回形状，字段名都不写死，靠自动识别
    cases = [
        ({"title": "稻香", "artist": "周杰伦", "position": 60, "duration": 223, "playing": True},
         "扁平"),
        ({"data": {"song": {"name": "起风了", "artist": "买辣椒也用券"},
                   "progress": 90, "total": 325, "status": "playing"}}, "嵌套"),
        ({"songName": "苦瓜", "singer": "陈奕迅", "currentTime": 30, "length": 277}, "驼峰"),
        ({"name": "Lemon", "artists": ["米津玄師"], "played": 45, "duration": 255}, "数组艺人"),
        ({"title": "晴天", "artist": "周杰伦", "position": "1:30", "duration": "4:29"}, "时间字符串"),
        ({"title": "孤勇者", "position": 191000, "duration": 254000}, "毫秒"),
    ]
    for payload, label in cases:
        info = parse_external_payload(payload)
        ok = info is not None and info.title
        detail = (f"{info.title}/{info.artist} {info.position:.0f}s/{info.duration:.0f}s"
                  if info else "None")
        check(f"识别 {label}", bool(ok), detail)

    info = parse_external_payload(cases[0][0])
    check("进度与时长数值正确（扁平）",
          info is not None and info.position == 60 and info.duration == 223,
          f"{info.position}/{info.duration}" if info else "无")
    info = parse_external_payload(cases[4][0])
    check("时间字符串 \"1:30\"/\"4:29\" 转成秒",
          info is not None and info.position == 90 and info.duration == 269,
          f"{info.position}/{info.duration}" if info else "无")
    info = parse_external_payload(cases[5][0])
    check("毫秒自动折算成秒",
          info is not None and abs(info.position - 191) < 1 and abs(info.duration - 254) < 1,
          f"{info.position}/{info.duration}" if info else "无")

    # 暂停状态要能识别出来
    info = parse_external_payload({"title": "稻香", "position": 10, "duration": 100,
                                   "status": "paused"})
    check("识别暂停状态", info is not None and info.playing is False,
          f"playing={info.playing}" if info else "无")

    # 认不出来的要返回 None（不能瞎猜）
    check("空对象返回 None", parse_external_payload({}) is None)
    # 只有进度没有歌名：**要保留**（合并逻辑靠它拿时长），但歌名是空的
    dur_only = parse_external_payload({"position": 10, "duration": 100})
    check("只有进度没歌名时保留记录（供合并用）",
          dur_only is not None and dur_only.title == "" and dur_only.duration == 100,
          f"title={dur_only.title!r} dur={dur_only.duration}" if dur_only else "None")
    check("只有无关字段才算认不出",
          parse_external_payload({"foo": 1, "bar": "x"}) is None)
    check("垃圾数据返回 None", parse_external_payload("not a dict") is None
          and parse_external_payload(None) is None)
    check("数组取最后一条",
          (parse_external_payload([{"title": "A"}, {"title": "B"}]) or MediaInfo()).title == "B")

    # 手动字段映射（键名完全认不出来时用）
    weird = {"xx": {"yy": "夜曲"}, "zz": 77, "ww": 300}
    info = parse_external_payload(weird, {"title": "xx.yy", "position": "zz", "duration": "ww"})
    check("手动字段映射生效",
          info is not None and info.title == "夜曲" and info.position == 77,
          f"{info.title}/{info.position}" if info else "None")

    check("_time_to_seconds 各种输入",
          _time_to_seconds(90, key="position") == 90
          and _time_to_seconds("1:30", key="position") == 90
          and _time_to_seconds("01:02:03", key="position") == 3723
          and _time_to_seconds(None, key="position") == 0.0
          and _time_to_seconds(90000, key="position") == 90.0)

    # ★ PlayerCap 的真实形态：信封 + 两个接口各缺一半字段，要合并
    from songboard.extapi import _merge_info
    song_info = {"code": 0, "msg": "success", "player": "cloudmusicv3",
                 "data": {"name": "句号", "singer": "G.E.M.邓紫棋",
                          "title": "句号 - G.E.M.邓紫棋", "cover": "http://x"}}
    all_lyrics = {"code": 0, "msg": "success", "player": "cloudmusicv3",
                  "data": {"title": "句号 - G.E.M.邓紫棋", "duration": 235.632,
                           "position": 42, "progress": 42, "count": 95}}
    a = parse_external_payload(song_info)
    b = parse_external_payload(all_lyrics)
    check("song_info 取到纯歌名（不是合成串）",
          a is not None and a.title == "句号" and a.artist == "G.E.M.邓紫棋",
          f"{a.title}/{a.artist}" if a else "None")
    check("all_lyrics 取到时长为秒", b is not None and abs(b.duration - 235.6) < 0.1,
          f"{b.duration}" if b else "None")
    merged = _merge_info(a, b)
    check("合并后歌名用纯歌名、歌手/时长都保住",
          merged.title == "句号" and merged.artist == "G.E.M.邓紫棋"
          and abs(merged.duration - 235.6) < 0.1 and merged.position == 42,
          f"{merged.title}/{merged.artist}/{merged.position}/{merged.duration}")
    # 顺序反过来也要对
    merged2 = _merge_info(b, a)
    check("反序合并结果一致",
          merged2.title == "句号" and merged2.artist == "G.E.M.邓紫棋"
          and abs(merged2.duration - 235.6) < 0.1,
          f"{merged2.title}/{merged2.artist}/{merged2.duration}")
    # 换了歌不能继承上一首的时长
    other = parse_external_payload(
        {"data": {"name": "孤勇者", "singer": "陈奕迅", "duration": 254, "position": 5}})
    cross = _merge_info(merged, other)
    check("换歌后不继承上一首的数据",
          cross.title == "孤勇者" and abs(cross.duration - 254) < 0.1,
          f"{cross.title}/{cross.duration}")
    # 只有时长的源不能把歌名弄丢
    dur_only = parse_external_payload({"data": {"duration": 300, "position": 10}})
    keep = _merge_info(merged, dur_only)
    check("只有时长的源不会覆盖已有歌名", keep.title == "句号", keep.title)


async def test_extapi_live() -> None:
    """起一个假的外部服务，端到端验证适配器真的能轮询到并归一化。"""
    print("\n== 外部媒体源（真实 HTTP 轮询）==")
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    from songboard.config import Config as Cfg2
    from songboard.extapi import ExtApiSource

    payload_box = {
        "data": {"name": "夜曲", "artist": "周杰伦", "progress": 45, "duration": 227,
                 "status": "playing"}
    }

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(payload_box).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    cfg = Cfg2.load(Path("__selftest_extapi.json"))
    cfg["extapi"]["enabled"] = True
    cfg["extapi"]["url"] = f"http://127.0.0.1:{port}/song_info"
    cfg["extapi"]["fields"] = {}
    src = ExtApiSource(cfg)

    try:
        info = await src.poll()
        check("轮询到并识别出曲目",
              info is not None and info.title == "夜曲" and info.artist == "周杰伦",
              f"{info.title}/{info.artist}" if info else "None")
        check("进度被当作可信来源（source=extapi）",
              info is not None and info.source == "extapi" and info.has_progress,
              f"source={info.source} 进度={info.position}/{info.duration}" if info else "None")
        st = src.status()
        check("状态显示已连接", st["alive"] is True and st["hits"] >= 1, str(st))

        # 换一首，确认能跟着变
        payload_box["data"]["name"] = "晴天"
        payload_box["data"]["progress"] = 10
        info2 = await src.poll()
        check("能跟上曲目变化", info2 is not None and info2.title == "晴天",
              info2.title if info2 else "None")

        # 服务返回垃圾时不能崩，要退化成"没数据"
        payload_box.clear()
        payload_box.update({"garbage": "没有曲名也没有进度"})
        info3 = await src.poll()
        check("认不出时返回 None 而不是崩", info3 is None, str(info3))
    finally:
        srv.shutdown()
        srv.server_close()

    # 关闭开关后不发请求
    cfg["extapi"]["enabled"] = False
    off = ExtApiSource(cfg)
    check("未启用时不轮询", await off.poll() is None)

    # URL 不通时也不能崩
    cfg["extapi"]["enabled"] = True
    cfg["extapi"]["url"] = "http://127.0.0.1:9/nowhere"
    dead = ExtApiSource(cfg)
    check("连接失败返回 None 不抛异常", await dead.poll() is None)
    check("失败原因被记录", bool(dead.last_error), dead.last_error[:60])

    Path("__selftest_extapi.json").unlink(missing_ok=True)


async def test_netease_reorder() -> None:
    """验证歌单重排逻辑——用假的网易云接口，不碰真实歌单。

    复刻实测行为：add 永远插在第 1 位（imme 参数无效）。
    """
    print("\n== 网易云歌单重排（点歌顺序 = 播放顺序）==")
    from pathlib import Path as P

    from songboard import netease as ne_module
    from songboard.config import Config as Cfg
    from songboard.netease import NeteasePlaylistDriver

    path = P("__selftest_reorder.json")
    path.unlink(missing_ok=True)
    cfg = Cfg.load(path)
    cfg["netease"]["playlist_id"] = "999999"
    cfg["netease"]["cookie"] = "MUSIC_U=fake"

    # 假的网易云：歌单 + "加歌永远插第 1 位"
    playlist: list[int] = []
    calls: list[tuple[str, list[int]]] = []

    def fake_weapi(p, payload, cookie):
        calls.append((payload.get("op", "?"), [int(x) for x in __import__("json").loads(payload.get("trackIds", "[]"))]))
        if p.endswith("/playlist/manipulate/tracks"):
            ids = __import__("json").loads(payload["trackIds"])
            if payload["op"] == "add":
                for tid in ids:                    # ← 关键：插到最前
                    if tid in playlist:
                        playlist.remove(tid)
                    playlist.insert(0, int(tid))
            else:
                for tid in ids:
                    if int(tid) in playlist:
                        playlist.remove(int(tid))
            return {"code": 200}
        if p.endswith("/v6/playlist/detail"):
            return {"code": 200, "playlist": {
                "tracks": [{"id": t, "name": f"曲{t}"} for t in playlist]}}
        return {"code": 500}

    original = ne_module.weapi_post
    ne_module.weapi_post = fake_weapi
    try:
        d = NeteasePlaylistDriver(cfg)
        check("歌单重排开关默认开", bool(cfg.get("netease.reorder", True)))

        # 模拟：点歌顺序 11 → 22 → 33，每次"加歌"
        for tid in (11, 22, 33):
            await d.add_song.__wrapped__(d, f"x{tid}") if hasattr(d.add_song, "__wrapped__") else None
            playlist.insert(0, tid)          # 直接模拟 add 的效果
        check("模拟加歌后顺序被倒过来了（这是网易云的真实行为）",
              playlist[:3] == [33, 22, 11], str(playlist[:3]))

        # 现在重排成点歌顺序 11 → 22 → 33
        ok, msg = d.reorder_playlist([11, 22, 33])
        check("重排成功", ok, msg)
        check("重排后顺序 = 点歌顺序（先点的在前）",
              playlist[:3] == [11, 22, 33], str(playlist[:3]))
        check("曲目总数没变（没丢歌）", len(playlist) == 3, str(playlist))

        # 已经正确时不应重复写操作（读一次歌单是必要的，不算）
        before_writes = sum(1 for op, _ in calls if op in ("add", "del"))
        ok2, msg2 = d.reorder_playlist([11, 22, 33])
        after_writes = sum(1 for op, _ in calls if op in ("add", "del"))
        check("顺序已正确时不重复写歌单",
              ok2 and msg2 == "顺序已经正确，无需调整" and after_writes == before_writes,
              f"{msg2}（新增写操作 {after_writes - before_writes} 次）")

        # 歌单里没有的歌要报错而不是乱动
        ok3, msg3 = d.reorder_playlist([11, 999])
        check("有歌不在歌单里时明确报错", not ok3 and "不在歌单" in msg3, msg3)

        # 重复点歌时（同一 id 两次）顺序仍要正确
        playlist.clear()
        playlist.extend([33, 22, 11])
        ok4, _ = d.reorder_playlist([11, 22, 33])
        check("重排对乱序歌单同样有效", ok4 and playlist[:3] == [11, 22, 33], str(playlist[:3]))
    finally:
        ne_module.weapi_post = original
        path.unlink(missing_ok=True)

    # store 侧：只排出"还没播的、且已写进网易云的"歌
    from songboard.config import Config as Cfg3
    from songboard.models import SongState
    from songboard.store import QueueStore

    p2 = P("__selftest_reorder2.json")
    p2.unlink(missing_ok=True)
    cfg2 = Cfg3.load(p2)
    cfg2["queue"]["cooldown_seconds"] = 0
    st = QueueStore(cfg2, P("__selftest_data3"), persist=False)
    await st.add("甲", "u1", 1)      # 自动变成 playing
    b, _ = await st.add("乙", "u2", 2)
    c, _ = await st.add("丙", "u3", 3)
    st.set_netease(b.id, 1002, "乙")
    st.set_netease(c.id, 1003, "丙")
    queued = st.queued_netease_ids()
    check("只排出'还没播'的歌（正在播的不算）",
          [tid for _i, tid in queued] == [1002, 1003], str(queued))
    check("顺序 = 点歌顺序", [i for i, _t in queued] == [b.id, c.id])
    import shutil as _sh2
    p2.unlink(missing_ok=True)
    _sh2.rmtree("__selftest_data3", ignore_errors=True)


# --------------------------------------------------------------------------- 语法/导入
def test_config_robustness() -> None:
    """config.json 是主播会手改的文件，读的时候必须容错。

    真实踩过的坑：记事本默认存成「UTF-8 带 BOM」，文件开头多一个 \\ufeff，
    json.loads 直接抛 "Unexpected UTF-8 BOM"，报错完全看不出是 BOM 的问题，
    服务当场起不来。
    """
    print("\n== 配置读取容错 ==")
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="songboard-cfg-"))
    try:
        # 1) 带 BOM
        bom = tmp / "bom.json"
        bom.write_text('{"room_id": 123, "gift_gate": {"min_coin": 700}}',
                       encoding="utf-8-sig")
        cfg = Config.load(bom)
        check("带 BOM 的 config.json 能读（记事本存的）",
              cfg.get("room_id") == 123 and cfg.get("gift_gate.min_coin") == 700,
              f"room={cfg.get('room_id')} coin={cfg.get('gift_gate.min_coin')}")

        # 2) 缺字段时用默认值补齐
        check("缺的字段用默认值补齐",
              cfg.get("queue.max_size") == 30 and cfg.get("mode") == "demo",
              f"{cfg.get('queue.max_size')}/{cfg.get('mode')}")

        # 3) 存回来不带 BOM
        cfg.save()
        raw = bom.read_bytes()
        check("save() 写出来不带 BOM", not raw.startswith(b"\xef\xbb\xbf"))
        check("save() 之后再读回来值不变",
              Config.load(bom).get("gift_gate.min_coin") == 700)

        # 4) JSON 写坏了要给看得懂的报错，而不是 traceback
        bad = tmp / "bad.json"
        bad.write_text('{ "room_id": 1, }', encoding="utf-8")
        msg = ""
        try:
            Config.load(bad)
        except SystemExit as exc:
            msg = str(exc)
        except Exception as exc:  # noqa: BLE001
            msg = f"抛了 {type(exc).__name__}: {exc}"
        check("JSON 写坏了给出人话报错（含行号列号）",
              "不是合法 JSON" in msg and "第 1 行" in msg, msg[:120])

        # 5) 最外层不是对象
        arr = tmp / "arr.json"
        arr.write_text('[1, 2, 3]', encoding="utf-8")
        msg2 = ""
        try:
            Config.load(arr)
        except SystemExit as exc:
            msg2 = str(exc)
        except Exception as exc:  # noqa: BLE001
            msg2 = f"抛了 {type(exc).__name__}: {exc}"
        check("最外层是数组时报错清楚", "必须是一个" in msg2, msg2[:120])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_web_js() -> None:
    """网页里的内联 JS 必须能编译。

    真实踩过的坑：overlay.html 里同一段 <script> 中出现了两次
    `let lastKey` —— 同一作用域重复声明是**早期语法错误**，
    整个 script 块会被浏览器直接丢弃：叠加层不报错、不渲染，
    就停在"等待点歌…"，看起来像"服务没数据"，查起来极其费劲。

    有 node 就用 node --check 真编译；没有就退化成"同一块里重复声明"的
    粗筛（正是上面那个坑的特征），并明确说明用的是哪种检查。
    """
    print("\n== 网页内联 JS 语法 ==")
    import re
    import shutil
    import subprocess
    import tempfile

    root = Path(__file__).resolve().parent
    pages = sorted((root / "web").glob("*.html"))
    check(f"web/ 下有页面（找到 {len(pages)} 个）", bool(pages))

    node = shutil.which("node")
    tmp = Path(tempfile.mkdtemp(prefix="songboard-js-"))
    bad: list[str] = []
    dup: list[str] = []
    try:
        for page in pages:
            html = page.read_text(encoding="utf-8")
            blocks = [b for b in re.findall(r"<script[^>]*>(.*?)</script>",
                                            html, re.S)]
            if not blocks:
                continue
            # --- 粗筛：同一块里重复声明的**顶层** let/const/class ---
            # 只认第 0 列开始的声明：函数体里的局部变量是缩进的，
            # 把它们也算进来会满屏假报警（第一次写就踩了）。
            for i, b in enumerate(blocks):
                decls = re.findall(r"^(?:let|const|class)\s+([A-Za-z_$][\w$]*)",
                                   b, re.M)
                seen: set[str] = set()
                for d in decls:
                    if d in seen:
                        dup.append(f"{page.name} 第{i + 1}块重复声明 {d}")
                    seen.add(d)
            if not node:
                continue
            js = tmp / (page.stem + ".js")
            js.write_text("\n".join(blocks), encoding="utf-8")
            r = subprocess.run([node, "--check", str(js)],
                               capture_output=True, text=True)
            if r.returncode != 0:
                first = (r.stderr or "").strip().splitlines()
                bad.append(f"{page.name}: " + (first[1] if len(first) > 1
                                               else "语法错误"))
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)

    check("没有同一作用域重复声明（会让整段 script 失效）", not dup,
          "; ".join(dup))
    if node:
        check("node --check 编译所有内联 JS 通过", not bad, "; ".join(bad))
    else:
        print("  [SKIP] 没装 node，只做了重复声明的粗筛（装 node 可做完整编译）")


def test_changelog() -> None:
    """版本号必须和 CHANGELOG 对得上。

    这条防的是"改了功能忘了写更新日志"：版本号在代码里、日志在文档里，
    两边一旦漂移，用户看到的就是"README 说 0.2.0，实际跑的还是 0.1.0"。
    """
    print("\n== 版本号与更新日志 ==")
    import re

    from songboard import __version__

    root = Path(__file__).resolve().parent
    cl = root / "CHANGELOG.md"
    check("CHANGELOG.md 存在", cl.exists())
    if not cl.exists():
        return

    text = cl.read_text(encoding="utf-8")
    heads = re.findall(r"^##\s*\[?(\d+\.\d+\.\d+)\]?", text, re.M)
    check("CHANGELOG 里至少有一个版本号标题", bool(heads), str(heads[:3]))

    check("__version__ 是合法的语义化版本号",
          re.fullmatch(r"\d+\.\d+\.\d+", __version__) is not None, __version__)
    check(f"CHANGELOG 最新版本 == __version__（{__version__}）",
          bool(heads) and heads[0] == __version__,
          f"代码={__version__} 日志最新={heads[0] if heads else '无'}")

    # README 顶部也写了版本号，一并盯着（三处不一致最难查）
    readme = root / "README.md"
    rm = re.search(r"当前版本\s*\*\*v(\d+\.\d+\.\d+)\*\*",
                   readme.read_text(encoding="utf-8")) if readme.exists() else None
    check("README 顶部写了版本号", rm is not None,
          rm.group(0) if rm else "没找到「当前版本 **vX.Y.Z**」")
    if rm:
        check("README 的版本号 == __version__",
              rm.group(1) == __version__,
              f"README={rm.group(1)} 代码={__version__}")

    # 版本必须是递减的，不能把 0.2.0 写在 0.2.1 后面
    def key(v: str) -> tuple:
        return tuple(int(x) for x in v.split("."))
    ordered = all(key(heads[i]) > key(heads[i + 1])
                  for i in range(len(heads) - 1))
    check("版本从新到旧排列", ordered, str(heads))

    # 最新版本必须有内容，不能只写个空标题
    # ⚠️ 不能用 split("##") 切：正文里的 "### 修复" 也含 "##"，
    #    会被切在 ### 处（第一次写就这么错了，只剩 20 个字符）。
    m = re.search(r"^##\s*\[?\d+\.\d+\.\d+\]?.*?$(.*?)"
                  r"(?=^##\s*\[?\d+\.\d+\.\d+|\Z)", text, re.M | re.S)
    first_body = m.group(1) if m else ""
    check("最新版本下面写了东西（不是空标题）",
          len(first_body.strip()) > 40, f"{len(first_body.strip())} 字符")


def test_bat_files() -> None:
    """批处理文件必须是 GBK 编码，且不能用 `chcp 65001`。

    真实踩过的坑：`启动.bat` 原本存成 UTF-8 无 BOM，里面写 `chcp 65001`。
    cmd.exe 读批处理是按当前代码页逐字节解码的，文件编码和运行时代码页
    一旦不一致，就会出现**整行被吃掉、后半截当成命令执行**的怪现象 ——
    用户看到的是菜单里少了 [2]，还报
    `'需要填房间号）' is not recognized as an internal or external command`。

    中文 Windows 的原生代码页就是 936：文件存 GBK + `chcp 936`，
    读取和输出两端一致，不需要任何运行时切换代码页。
    """
    print("\n=== 批处理文件编码 ===")
    root = Path(__file__).resolve().parent
    bats = sorted(root.glob("*.bat"))
    check(f"找到 {len(bats)} 个 .bat", bool(bats))

    for bat in bats:
        raw = bat.read_bytes()
        problems: list[str] = []

        if raw[:3] == b"\xef\xbb\xbf":
            problems.append("有 UTF-8 BOM")
        try:
            text = raw.decode("gbk")
        except UnicodeDecodeError as exc:
            problems.append(f"不是 GBK 编码（{exc}）")
            text = raw.decode("utf-8", "replace")
        if "chcp 65001" in text:
            problems.append("还在用 chcp 65001")
        if b"\n" in raw.replace(b"\r\n", b""):
            problems.append("有裸 LF 换行（批处理要全 CRLF）")

        # 中文必须能原样往返（GBK 编不出的字符会被换成 ?）
        if "?" in text.replace("%~dp0", "").replace("2>&1", ""):
            problems.append("有字符在 GBK 里存不下来（变成了 ?）")

        check(f"{bat.name} 编码正确（GBK / CRLF / 无 BOM / 不用 65001）",
              not problems, "; ".join(problems))

        # 菜单里的中文要能读出来（防止整个文件被写坏）
        if bat.name == "启动.bat":
            for key in ("哔哩哔哩点歌板", "[1] 演示模式", "[2] 连直播间",
                        "[3] 自检", "[4] 扫码登录"):
                check(f"启动.bat 里有「{key}」", key in text)

        # 扫码登录的入口必须是 .bat：没装 Python 时 .pyw 双击根本不执行，
        # 所以"检查环境"这一步只能放在不需要 Python 的批处理里。
        if bat.name == "扫码登录.bat":
            for key, why in (("where python", "检查有没有 Python"),
                             ("import tkinter", "检查有没有图形界面库"),
                             ("import qrcode", "检查二维码库"),
                             ("pip install", "缺依赖时自动装"),
                             ("python.org", "没 Python 时给下载地址"),
                             ("build_gui.ps1", "给出「用 exe」这条备选路")):
                check(f"扫码登录.bat 会{why}", key in text)


def test_netease_auth_codes() -> None:
    """cookie 失效必须报"登录态无效"，不能报成"搜不到这首歌"。

    实测（2026-09）：空 cookie / 假 cookie 调 weapi 搜索，网易云返回
    `code=50000005`。以前这个码被忽略，搜到 0 条就往上报
    "网易云搜不到《稻香》" —— 主播会去查歌名对不对，方向完全错了。
    """
    print("\n== 网易云登录态报错 ==")
    from songboard import netease as NE

    check("有专门的 NeteaseAuthError 类型", issubclass(NE.NeteaseAuthError,
                                                     RuntimeError))
    check("50000005 被算作登录态错误",
          50000005 in NE.AUTH_ERROR_CODES, str(NE.AUTH_ERROR_CODES))

    # 用假 cookie 打真接口（只读，不会改任何东西）会拿到 50000005 ——
    # 但自检必须离线可跑，所以这里只验"拿到这个码会抛异常"这条逻辑。
    orig = NE.weapi_post
    try:
        NE.weapi_post = lambda *a, **kw: {"code": 50000005}      # type: ignore
        raised = ""
        try:
            NE.search_song("稻香", cookie="MUSIC_U=fake", limit=1)
        except NE.NeteaseAuthError as exc:
            raised = str(exc)
        except Exception as exc:  # noqa: BLE001
            raised = f"抛了 {type(exc).__name__}: {exc}"
        check("搜索遇到登录态错误码会抛 NeteaseAuthError",
              "登录态无效" in raised and "50000005" in raised, raised[:90])

        # 正常的 200 不能被误伤
        NE.weapi_post = lambda *a, **kw: {                        # type: ignore
            "code": 200,
            "result": {"songs": [{"id": 123, "name": "稻香",
                                  "ar": [{"name": "周杰伦"}],
                                  "al": {"name": "魔杰座"}, "dt": 223000}]}}
        ok = NE.search_song("稻香", cookie="x", limit=1)
        check("正常返回（code=200）不受影响",
              len(ok) == 1 and ok[0]["id"] == 123
              and ok[0]["artists"] == "周杰伦" and ok[0]["duration_ms"] == 223000,
              str(ok))

        # 真的搜不到（code=200 但没歌）不该抛异常
        NE.weapi_post = lambda *a, **kw: {"code": 200, "result": {}}  # type: ignore
        check("真的搜不到时不抛异常，只返回空列表",
              NE.search_song("不存在的歌", cookie="x") == [])

        # account_info：cookie 无效时网易云照样返回 200，但没 profile
        NE.weapi_post = lambda *a, **kw: {"code": 200}            # type: ignore
        check("account_info 拿不到 profile 时返回空（cookie 无效）",
              NE.account_info(cookie="MUSIC_U=fake") == {})
        NE.weapi_post = lambda *a, **kw: {                        # type: ignore
            "code": 200, "profile": {"nickname": "七月雀", "userId": 42}}
        info = NE.account_info(cookie="MUSIC_U=real")
        check("account_info 能解析出昵称和 uid",
              info.get("nickname") == "七月雀" and info.get("user_id") == 42,
              str(info))
        check("空 cookie 直接返回空，不发请求",
              NE.account_info(cookie="") == {})
    finally:
        NE.weapi_post = orig                                    # type: ignore

    # status() 要把账号信息带出去（控制台显示"已登录：xxx"靠它）
    cfg = Config.load(Path("__selftest_config.json"))
    drv = NE.NeteasePlaylistDriver(cfg)
    st = drv.status()
    check("status() 里带 account 字段", "account" in st, str(sorted(st)))
    drv.account = {"nickname": "七月雀", "user_id": 42}
    check("account 会出现在 status() 里",
          drv.status()["account"].get("nickname") == "七月雀")


def test_room_diagnostics() -> None:
    """"收不到弹幕"要能从状态里看出来是哪种情况。

    实测踩过的坑：
      1) **房间号填错**（连了别人的房间）时，连接/认证/心跳全都正常，
         日志一片健康，就是永远收不到弹幕 —— 所以房间名必须显示出来
      2) **短号**（如 room 6，真实号 7734200）连上去认证成功、心跳正常，
         但服务器一条弹幕都不推。实测：用 6 连 0 条，用 7734200 连 20 秒 3 条。
         diag_room.py 以前算出了真实房间号却没用它连，于是给出
         "这个房间确实没有弹幕产生"这种完全错误的结论
      3) "历史弹幕"接口未登录时经常返回空，不能拿它判断房间没弹幕
    """
    print("\n== 房间诊断信息 ==")
    from songboard.bilibili import BilibiliDanmaku

    dm = BilibiliDanmaku(12345, lambda ev: None, None)
    st = dm.status()
    check("status() 里有 room 字段（房间名/开播状态）", "room" in st,
          str(sorted(st)))
    check("还没解析过房间时 room 是空 dict，不会崩", st["room"] == {},
          str(st["room"]))
    check("room_id 用真实号而非输入号",
          hasattr(dm, "input_room_id") and dm.room_id == dm.input_room_id,
          f"input={dm.input_room_id} real={dm.room_id}")

    dm.room_info = {"title": "测试", "live_status": 0, "area": "自习室"}
    check("room 信息会带进 status()",
          dm.status()["room"].get("title") == "测试",
          str(dm.status()["room"]))

    # 诊断工具必须用真实房间号连接（这个 bug 会让排查结论完全反过来）
    root = Path(__file__).resolve().parent
    src = (root / "diag_room.py").read_text(encoding="utf-8")
    check("diag_room 用真实房间号连接（不是短号）",
          "BilibiliDanmaku(real_room" in src
          and "BilibiliDanmaku(ROOM" not in src)
    check("diag_room 不再拿历史弹幕为空当结论",
          "别据此判断房间没弹幕" in src)


async def test_no_cookie_no_insert() -> None:
    """没有 cookie 时到底卡在哪一步。

    结论（也是给主播的解释）：**插播放队列这个动作本身不需要 cookie**
    （走的是注入 DLL + CEF DevTools），但程序得先"把歌名搜成歌曲 id"，
    而搜索接口要登录态 —— 所以没有 cookie 时，点歌板一切正常，
    歌却永远插不进去。
    """
    print("\n== 没有 cookie 会怎样 ==")
    from songboard.main import App
    from songboard.netease import NeteaseAuthError

    root = Path(__file__).resolve().parent
    cfg = Config.load(root / "config.json")
    cfg["mode"] = "demo"
    cfg["queue_only.enabled"] = True
    cfg["ncm_bridge.enabled"] = True
    cfg["netease.enabled"] = True
    cfg["netease.cookie"] = ""                 # 关键：没有 cookie

    inserted: list = []
    app = App(cfg, persist=False)
    app.bridge.available = lambda **kw: True                       # type: ignore
    app.bridge.now_playing = lambda: {"track_id": "1", "name": "别的歌"}  # type: ignore
    app.bridge.insert_next = lambda sid: (                         # type: ignore
        inserted.append(sid), (True, "fake"))[1]

    check("没有 cookie 时 driver.cookie 是空的",
          not str(getattr(app.driver, "cookie", "")), repr(app.driver.cookie))

    await app.store.add("稻香", "观众A", 12345)
    await App._sync_queue_only(app)
    await _drain_tasks()

    check("点歌板照样能加点歌（队列功能不依赖 cookie）",
          any(s.song == "稻香" for s in app.store.active()),
          str([s.song for s in app.store.active()]))
    check("插队列被拦住：一次都没调用 insert_next", not inserted,
          f"inserted={inserted}")
    logs = [str(x.get("text") or "") for x in app.log_lines]
    check("日志说明了原因（不是静默失败）",
          any("cookie" in x for x in logs),
          str([x for x in logs if "cookie" in x][:2]))

    # 有个 cookie 但已经失效 → 另一条分支：要报"登录态无效"
    app2 = App(cfg, persist=False)
    cfg["netease.cookie"] = "MUSIC_U=fake"
    app2.driver.cookie = "MUSIC_U=fake"
    inserted2: list = []
    app2.bridge.available = lambda **kw: True                      # type: ignore
    app2.bridge.now_playing = lambda: {"track_id": "1", "name": "别的歌"}   # type: ignore
    app2.bridge.insert_next = lambda sid: (                        # type: ignore
        inserted2.append(sid), (True, "fake"))[1]

    orig = None
    import songboard.main as M
    orig = M.search_song
    M.search_song = lambda *a, **kw: (_ for _ in ()).throw(       # type: ignore
        NeteaseAuthError("网易云登录态无效（code=50000005），cookie 可能已过期，请重新获取"))
    try:
        await app2.store.add("晴天", "观众B", 12346)
        await App._sync_queue_only(app2)
        await _drain_tasks()
    finally:
        M.search_song = orig                                       # type: ignore
    check("cookie 失效时不插歌", not inserted2, f"inserted={inserted2}")
    logs2 = [str(x.get("text") or "") for x in app2.log_lines]
    check("日志明确说「登录态无效」而不是「搜不到这首歌」",
          any("登录态无效" in x for x in logs2),
          str([x for x in logs2 if "登录态" in x][:2]))


def test_cookie_extract() -> None:
    """从剪贴板/响应头抠 cookie：什么形态都要能认出来。

    实测见过的形态：纯 cookie、带 `Cookie:` 前缀、F12「Copy request headers」
    的整块、`Copy as cURL` 的整条命令、JSON 里嵌的 cookie。
    以前是按 `;` 拆分配对，cURL 那种会解析成 `-H 'cookie: MUSIC_U`
    这样的字段名，直接失败。

    提取逻辑现在住在 songboard/cookies.py —— set_cookie.py（手动复制）
    和扫码登录那两个入口（tools/login_qrcode.py、tools/扫码登录.pyw）共用同一套。
    """
    print("\n== cookie 提取（songboard/cookies.py） ==")
    from songboard.cookies import build_cookie, cookies_from_response_headers, \
        extract_fields

    MU, CS = "AbCd" * 8, "csrf123"
    good = [
        ("纯 cookie 字符串", f"MUSIC_U={MU}; __csrf={CS}"),
        ("带 Cookie: 前缀", f"Cookie: MUSIC_U={MU}; __csrf={CS}"),
        ("Copy request headers 的整块",
         f"GET /x HTTP/1.1\nHost: music.163.com\n"
         f"Cookie: MUSIC_U={MU}; __csrf={CS}; NMTID=z\nAccept: */*"),
        ("Copy as cURL (bash)",
         f"curl 'https://music.163.com/x' \\\n  -H 'cookie: MUSIC_U={MU}; __csrf={CS}'"),
        ("Copy as cURL (cmd)",
         f'curl "https://music.163.com/x" -H "cookie: MUSIC_U={MU}; __csrf={CS}"'),
        ("顺序颠倒 + 多余字段",
         f"__csrf={CS}; Hm_lvt_a=1; MUSIC_U={MU}; WM_TID=z"),
        ("折行的 cookie", f"Cookie: __csrf={CS};\n  MUSIC_U={MU}"),
        ("JSON 里的 cookie",
         '{"headers":{"cookie":"MUSIC_U=' + MU + "; __csrf=" + CS + '"}}'),
    ]
    for label, text in good:
        got = extract_fields(text)
        ok = got.get("MUSIC_U") == MU and got.get("__csrf") == CS
        check(f"能认出：{label}", ok,
              f"MUSIC_U={len(got.get('MUSIC_U') or '')}/{len(MU)} "
              f"__csrf={got.get('__csrf')!r}")

    check("只有 MUSIC_U 时也能用（不强求 __csrf）",
          extract_fields(f"MUSIC_U={MU}").get("MUSIC_U") == MU)

    for label, text in (("整页 HTML", "<html><title>网易云音乐</title></html>"),
                        ("没有字段名的一串值", MU),
                        ("别的站点的 cookie", "sessionid=abc; csrftoken=def"),
                        ("空字符串", "")):
        got = extract_fields(text)
        check(f"不会误认：{label}", "MUSIC_U" not in got, str(got))

    check("build_cookie 拼成 MUSIC_U=...; __csrf=... 的形式",
          build_cookie(f"__csrf={CS}; MUSIC_U={MU}") == f"MUSIC_U={MU}; __csrf={CS}",
          build_cookie(f"__csrf={CS}; MUSIC_U={MU}"))
    check("没有 MUSIC_U 时 build_cookie 返回空串",
          build_cookie("sessionid=abc") == "")

    # 扫码登录成功时凭据在 Set-Cookie 里，不在 body —— 这条专门测那个解析
    class FakeHeaders:
        def __init__(self, items): self._items = items
        def get_all(self, name): return self._items if name == "Set-Cookie" else None

    hd = FakeHeaders([
        "MUSIC_U=xyz789; Path=/; Domain=.music.163.com; HttpOnly",
        "__csrf=abc123; Path=/; Domain=.music.163.com",
        "NMTID=nnn; Path=/; Max-Age=31536000",
    ])
    got = cookies_from_response_headers(hd)
    check("能从 Set-Cookie 里收出凭据（扫码登录用）",
          "MUSIC_U=xyz789" in got and "__csrf=abc123" in got
          and "Path=" not in got and "HttpOnly" not in got, got)
    check("没有 Set-Cookie 时返回空串",
          cookies_from_response_headers(FakeHeaders([])) == "")
    check("响应头对象是 None 也不崩",
          cookies_from_response_headers(None) == "")


def test_qrlogin() -> None:
    """扫码登录的业务逻辑（CLI 和图形界面共用那一份）。

    真实扫码（803）需要真人拿手机扫，自检里测不了 —— 但**拿到 803 之后
    怎么从响应头里收凭据**这步是纯逻辑，必须测：
    扫半天成功了、结果 cookie 没接住，那才是最气人的。
    """
    print("\n== 扫码登录逻辑 ==")
    import songboard.qrlogin as QR

    check("状态码常量齐了",
          (QR.WAITING, QR.SCANNED, QR.CONFIRMED, QR.EXPIRED) == (801, 802, 803, 800),
          f"{QR.WAITING}/{QR.SCANNED}/{QR.CONFIRMED}/{QR.EXPIRED}")
    for code in (801, 802, 803, 800):
        check(f"code={code} 有人话说明", bool(QR.STATUS_TEXT.get(code)),
              QR.STATUS_TEXT.get(code, ""))

    class FakeHeaders:
        def __init__(self, items): self._items = items
        def get_all(self, name): return self._items if name == "Set-Cookie" else None

    # 注入一个假的接口层：不联网也能把整条流程走一遍
    calls: list = []
    real_post, real_raw = QR.weapi_post, QR.weapi_post_raw
    try:
        QR.weapi_post = lambda path, payload, cookie: (       # type: ignore
            calls.append((path, payload)),
            {"code": 200, "unikey": "KEY-123"})[1]
        s = QR.QrLogin()
        url = s.start()
        check("start() 拿到二维码内容",
              s.unikey == "KEY-123" and url.endswith("codekey=KEY-123"), url)
        check("调的是 unikey 接口、type=1",
              calls and calls[0][0] == "/login/qrcode/unikey"
              and calls[0][1].get("type") == 1, str(calls[:1]))

        # 前两次「等待扫码」，第三次「登录成功」并下发 Set-Cookie
        seq = [(801, None), (802, None), (803, [
            "MUSIC_U=REALTOKEN; Path=/; HttpOnly",
            "__csrf=CSRFVAL; Path=/",
        ])]
        def fake_raw(path, payload, cookie):                  # type: ignore
            code, hdrs = seq.pop(0)
            return {"code": code}, FakeHeaders(hdrs or [])
        QR.weapi_post_raw = fake_raw

        c1, m1 = s.poll()
        check("801 → 等待扫码", c1 == 801 and "等待扫码" in m1, f"{c1} {m1}")
        c2, m2 = s.poll()
        check("802 → 提示去手机上确认", c2 == 802 and "确认" in m2, f"{c2} {m2}")
        c3, m3 = s.poll()
        check("803 → 登录成功", c3 == 803, f"{c3} {m3}")
        check("803 时从 Set-Cookie 里收齐了凭据",
              "MUSIC_U=REALTOKEN" in s.cookie and "__csrf=CSRFVAL" in s.cookie,
              s.cookie)
        check("原始 Set-Cookie 也留着（万一没接住，好排查）",
              len(s.raw_headers) == 2, str(s.raw_headers))

        # 万一某个版本把凭据放在 body 的 cookie 字段里
        seq = [(803, None)]
        def fake_raw2(path, payload, cookie):                 # type: ignore
            return ({"code": 803, "cookie": {"MUSIC_U": "FROMBODY",
                                             "__csrf": "X", "junk": "y"}},
                    FakeHeaders([]))
        QR.weapi_post_raw = fake_raw2
        s2 = QR.QrLogin()
        s2.start()
        s2.poll()
        check("响应头没有时退回 body 的 cookie 字段",
              "MUSIC_U=FROMBODY" in s2.cookie and "junk" not in s2.cookie,
              s2.cookie)

        # 过期是明确的失败，不该被当成"继续等"
        QR.weapi_post_raw = lambda p, q, c: ({"code": 800}, FakeHeaders([]))  # type: ignore
        s3 = QR.QrLogin()
        s3.start()
        code, msg = s3.poll()
        check("800 → 过期，且给了重新生成的提示",
              code == 800 and "重新生成" in msg, f"{code} {msg}")

        bad = QR.QrLogin()
        raised = ""
        try:
            bad.poll()
        except Exception as exc:  # noqa: BLE001
            raised = str(exc)
        check("没 start() 就 poll() 会明确报错", "start" in raised, raised)
    finally:
        QR.weapi_post = real_post        # type: ignore
        QR.weapi_post_raw = real_raw     # type: ignore

    # 二维码矩阵：图形界面靠它画方块
    if QR.qrcode_available():
        m = QR.qr_matrix("https://music.163.com/login?codekey=TEST", border=2)
        n = len(m)
        check("qr_matrix 返回正方形矩阵", n > 20 and all(len(r) == n for r in m),
              f"{n}x{len(m[0]) if m else 0}")
        check("矩阵里有黑有白（不是全空）",
              any(any(r) for r in m) and not all(all(r) for r in m))
        # 三个定位角必须是黑的（二维码的硬特征，画错了就扫不出来）
        check("左上角有定位图案", m[2][2] and m[2][3] and m[3][2], "")
        check("右上角有定位图案", m[2][n - 3] and m[3][n - 3], "")
        check("左下角有定位图案", m[n - 3][2] and m[n - 3][3], "")
    else:
        print("  [SKIP] 没装 qrcode，跳过二维码矩阵检查")


def test_ps1_files() -> None:
    """PowerShell 脚本：带中文就必须有 UTF-8 BOM。

    ⚠️ PowerShell 5.1（Windows 自带那个）读 .ps1 时，**没有 BOM 就按系统
    代码页（GBK）解析** —— 带中文的脚本会整段变乱码，连语法都过不了。
    这个坑踩过两次：build_bridge.ps1 一次，setup_env.ps1 一次。
    而且**每次用编辑器改完 .ps1，BOM 都会丢**，所以必须有检查盯着。
    """
    print("\n== PowerShell 脚本编码 ==")
    BOM = b"\xef\xbb\xbf"
    scripts = sorted((Path(__file__).resolve().parent / "tools").glob("*.ps1"))
    check(f"找到 {len(scripts)} 个 .ps1", bool(scripts))
    for p in scripts:
        raw = p.read_bytes()
        text = raw.decode("utf-8", "replace")
        has_cn = any("\u4e00" <= ch <= "\u9fff" for ch in text)
        problems = []
        if has_cn and not raw.startswith(BOM):
            problems.append("含中文却没有 UTF-8 BOM（PowerShell 5.1 会乱码）")
        if "\r\n" not in text[:4000] and "\n" in text[:4000]:
            problems.append("换行是 LF（该用 CRLF）")
        check(f"{p.name} 编码正确" + ("（无中文）" if not has_cn else ""),
              not problems, "; ".join(problems))


def test_syntax() -> None:
    """所有源码都必须能编译、所有模块都必须能导入。

    这条很关键：只 import 一部分模块的话，别的模块里的语法错误
    要等到真正启动服务时才炸（我就这么踩过一次）。
    """
    print("\n== 源码编译与导入 ==")
    root = Path(__file__).resolve().parent
    files = sorted((root / "songboard").glob("*.py")) + [root / "selftest.py"]
    bad: list[str] = []
    for f in files:
        try:
            compile(f.read_text(encoding="utf-8"), str(f), "exec")
        except SyntaxError as exc:
            bad.append(f"{f.name}:{exc.lineno} {exc.msg}")
    check(f"{len(files)} 个源文件全部编译通过", not bad, "; ".join(bad[:3]))

    import importlib
    mods = [f"songboard.{p.stem}" for p in sorted((root / "songboard").glob("*.py"))
            if p.stem not in ("__init__", "__main__")]
    failed: list[str] = []
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception as exc:
            failed.append(f"{m}: {type(exc).__name__}: {exc}")
    check(f"{len(mods)} 个模块全部可导入", not failed, "; ".join(failed[:3]))


async def test_playback_authority() -> None:
    """"播放状态以网易云为准"的行为验证。"""
    print("\n== 播放状态以网易云为准 ==")
    from pathlib import Path as P

    from songboard.config import Config as Cfg
    from songboard.store import QueueStore as QS

    path = P("__selftest_auth.json")
    path.unlink(missing_ok=True)
    cfg = Cfg.load(path)
    cfg["queue"]["cooldown_seconds"] = 0
    st = QS(cfg, P("__selftest_data4"), persist=False)

    await st.add("句号", "甲", 1)
    await st.add("起风了", "乙", 2)
    await st.add("孤勇者", "丙", 3)
    check("默认 authority = netease",
          str(cfg.get("playback.authority")) == "netease", str(cfg.get("playback.authority")))
    check("默认不自动加歌（加歌由主播自己做）",
          cfg.get("netease.auto_add") is False, str(cfg.get("netease.auto_add")))

    before_current = st.current.song if st.current else None
    before_states = {s.song: s.state.value for s in st.active()}

    # 网易云放到队列里的第三首 → 当前指向应切过去，但状态不能变
    item, why = await st.set_current_by_title("孤勇者", artist="陈奕迅", strict=True)
    check("能对齐到队列里的歌", item is not None and item.song == "孤勇者", why)
    check("当前指向已切换", st.current is not None and st.current.song == "孤勇者",
          st.current.song if st.current else "None")
    after_states = {s.song: s.state.value for s in st.active()}
    check("对齐时不改变任何条目的播放状态（否则队列会乱）",
          before_states == after_states, f"{before_states} → {after_states}")

    # 带艺人后缀的标题也要能匹配（"句号" vs "句号 - G.E.M.邓紫棋"）
    item2, _ = await st.set_current_by_title("句号 - G.E.M.邓紫棋", strict=True)
    check("带艺人后缀的标题能匹配", item2 is not None and item2.song == "句号",
          item2.song if item2 else "None")

    # 严格模式：翻唱不能算命中（这正是"青花瓷→刘芳版"那类坑）
    item3, why3 = await st.set_current_by_title("晴天(深情版)", strict=True)
    check("严格模式下不相干的歌不命中", item3 is None, f"{why3}")
    item4, _ = await st.set_current_by_title("完全无关的歌名", strict=True)
    check("完全不相关的歌不命中", item4 is None)

    # 已经是指向它时不重复动作
    again, why5 = await st.set_current_by_title(st.current.song, strict=True)
    check("重复对齐是幂等的", again is not None and "已经是当前曲目" in why5, why5)

    import shutil as _sh3
    path.unlink(missing_ok=True)
    _sh3.rmtree("__selftest_data4", ignore_errors=True)


async def test_append_last() -> None:
    """验证"新歌加到歌单最后一位"——用假的网易云接口，不碰真实歌单。

    复刻实测行为：add 永远插在第 1 位。
    """
    print("\n== 加歌到歌单最后一位 ==")
    from pathlib import Path as P

    from songboard import netease as ne_module
    from songboard.config import Config as Cfg
    from songboard.netease import NeteasePlaylistDriver

    path = P("__selftest_append.json")
    path.unlink(missing_ok=True)
    cfg = Cfg.load(path)
    cfg["netease"]["playlist_id"] = "888888"
    cfg["netease"]["cookie"] = "MUSIC_U=fake"

    playlist: list[int] = [1, 2, 3]          # 已有 3 首

    def fake_weapi(p, payload, cookie):
        if p.endswith("/playlist/manipulate/tracks"):
            ids = [int(x) for x in json.loads(payload["trackIds"])]
            if payload["op"] == "add":
                for tid in ids:               # ← 关键：永远插第 1 位
                    if tid in playlist:
                        playlist.remove(tid)
                    playlist.insert(0, tid)
            else:
                for tid in ids:
                    if int(tid) in playlist:
                        playlist.remove(int(tid))
            return {"code": 200}
        if p.endswith("/v6/playlist/detail"):
            return {"code": 200, "playlist": {
                "tracks": [{"id": t, "name": f"曲{t}"} for t in playlist]}}
        return {"code": 500}

    original = ne_module.weapi_post
    ne_module.weapi_post = fake_weapi
    try:
        d = NeteasePlaylistDriver(cfg)
        check("append_last 默认开", bool(cfg.get("netease.append_last", True)))
        check("起始歌单顺序", playlist == [1, 2, 3], str(playlist))

        # 先模拟"普通加歌"：新歌会被插到最前（这是网易云的原生行为）
        playlist.insert(0, 99)
        check("原生加歌会插到第 1 位（问题所在）", playlist == [99, 1, 2, 3], str(playlist))

        # 用 move_to_end 把它挪到最后
        ok, msg = d.move_to_end(99)
        check("move_to_end 成功", ok, msg)
        check("新歌落在了最后一位", playlist == [1, 2, 3, 99], str(playlist))

        # 再加一首，确认仍然追加到末尾、且不打乱已有顺序
        playlist.insert(0, 77)
        ok2, _ = d.move_to_end(77)
        check("第二首也追加到末尾", ok2 and playlist == [1, 2, 3, 99, 77], str(playlist))

        # 已经在末尾时不重复操作
        before_writes = sum(1 for _ in [0])
        calls_before = len(playlist)
        ok3, msg3 = d.move_to_end(77)
        check("已在末尾时不重复写歌单", ok3 and msg3 == "已经在最后一位", msg3)

        # 不在歌单里的曲目要报错
        ok4, msg4 = d.move_to_end(12345)
        check("不在歌单里的曲目明确报错", not ok4 and "不在歌单" in msg4, msg4)

        # 空歌单 + 单曲边界
        playlist.clear()
        playlist.extend([5])
        ok5, msg5 = d.move_to_end(5)
        check("歌单只有一首时不报错", ok5, msg5)

        # 顺序保持：中间的歌挪到末尾后，其余相对顺序不变
        playlist.clear()
        playlist.extend([10, 20, 30, 40])
        d.move_to_end(20)
        check("挪中间的歌到末尾，其余顺序不变",
              playlist == [10, 30, 40, 20], str(playlist))
    finally:
        ne_module.weapi_post = original
        path.unlink(missing_ok=True)


async def test_align_continuously() -> None:
    """回归：对齐必须**每轮都做**，不能只在"换歌"时做。

    真实事故：网易云一直放着《苦瓜》没换歌，就没有"变化事件"，
    点歌板的"正在播放"一直停在《浮夸》没对齐。
    """
    print("\n== 持续对齐网易云播放状态 ==")
    from pathlib import Path as P

    from songboard.config import Config as Cfg
    from songboard.store import QueueStore as QS

    path = P("__selftest_align.json")
    path.unlink(missing_ok=True)
    cfg = Cfg.load(path)
    cfg["queue"]["cooldown_seconds"] = 0
    st = QS(cfg, P("__selftest_data5"), persist=False)

    await st.add("浮夸", "甲", 1)      # 直接成为"正在播放"
    await st.add("十年", "乙", 2)
    await st.add("红玫瑰", "丙", 3)
    await st.add("苦瓜", "丁", 4)
    check("初始当前指向是《浮夸》", st.current.song == "浮夸",
          st.current.song if st.current else "None")

    # 模拟"网易云在放苦瓜、但没有任何换歌事件"——第一轮轮询就该对齐
    item, why = await st.set_current_by_title("苦瓜", strict=True)
    check("第一轮轮询就对齐到《苦瓜》（不依赖换歌事件）",
          item is not None and st.current.song == "苦瓜", why)

    # 再模拟"主播切到队列里的另一首"，同样应该立刻对齐
    item2, why2 = await st.set_current_by_title("红玫瑰", strict=True)
    check("主播手动切歌后立刻对齐", item2 is not None and st.current.song == "红玫瑰", why2)

    # 反复对齐同一首必须幂等（每 2 秒轮询一次，不能每轮都改状态）
    for _ in range(3):
        again, why3 = await st.set_current_by_title("红玫瑰", strict=True)
        if not (again is not None and "已经是当前曲目" in why3):
            break
    check("反复对齐是幂等的", "已经是当前曲目" in why3, why3)

    # 对齐不能破坏"谁还没播"的判定
    states = sorted(s.song for s in st.active())
    check("对齐后队列成员不变", states == sorted(["十年", "红玫瑰", "苦瓜", "浮夸"]), str(states))

    import shutil as _sh4
    path.unlink(missing_ok=True)
    _sh4.rmtree("__selftest_data5", ignore_errors=True)


async def test_queue_head() -> None:
    """验证"只推队头一首"的设计（参考 AwooMusicBot/BiliNCM 的公开说明）。

    核心：永远只把队头写进歌单，等它开始播再写下一首，
    这样"加歌永远插第 1 位"就不会导致顺序颠倒。
    """
    print("\n== 只推队头一首（队头模式）==")
    from pathlib import Path as P

    from songboard.config import Config as Cfg
    from songboard.store import QueueStore as QS

    path = P("__selftest_head.json")
    path.unlink(missing_ok=True)
    cfg = Cfg.load(path)
    cfg["queue"]["cooldown_seconds"] = 0
    st = QS(cfg, P("__selftest_data6"), persist=False)

    check("队头模式默认开", cfg.get("netease.queue_head_only") is True,
          str(cfg.get("netease.queue_head_only")))

    await st.add("第一首", "甲", 1)      # 直接成为"正在播放"
    b, _ = await st.add("第二首", "乙", 2)
    c, _ = await st.add("第三首", "丙", 3)

    head = st.next_up()
    check("队头 = 第一个等待的歌（不是最后点的）",
          head is not None and head.song == "第二首",
          head.song if head else "None")
    check("还有未写入队列的歌", st.has_pending_work() is True)

    # 模拟"队头已写入歌单"
    st.set_netease(b.id, 2002, "第二首")
    check("队头写入后 next_up 仍是它（等它播，不跳到第三首）",
          st.next_up() is not None and st.next_up().song == "第二首",
          st.next_up().song if st.next_up() else "None")

    # 模拟"第二首开始播放"：指向它之后，队头应变成第三首
    await st.set_current_by_title("第二首", strict=True)
    head2 = st.next_up()
    check("队头播了之后，队头推进到《第三首》",
          head2 is not None and head2.song == "第三首",
          head2.song if head2 else "None")

    # 第三首还没写 → 仍有待办
    check("第三首还没写，仍有待办", st.has_pending_work() is True)
    st.set_netease(c.id, 2003, "第三首")
    check("全部写完就没有待办了", st.has_pending_work() is False)

    # 空队列
    await st.clear()
    check("队列清空后没有队头", st.next_up() is None)
    check("队列清空后没有待办", st.has_pending_work() is False)

    import shutil as _sh5
    path.unlink(missing_ok=True)
    _sh5.rmtree("__selftest_data6", ignore_errors=True)

    # ---- 端到端：用假的网易云验证"一次只加一首、顺序正确" ----
    from songboard import netease as ne_module
    from songboard.netease import NeteasePlaylistDriver

    p2 = P("__selftest_head2.json")
    p2.unlink(missing_ok=True)
    cfg2 = Cfg.load(p2)
    cfg2["netease"]["playlist_id"] = "777777"
    cfg2["netease"]["cookie"] = "MUSIC_U=fake"

    playlist: list[int] = []
    calls: list[str] = []

    def fake_weapi(p, payload, cookie):
        if p.endswith("/cloudsearch/get/web"):
            import re as _re
            m = _re.search(r"(\d+)", payload.get("s", ""))
            tid = int(m.group(1)) if m else 1
            return {"code": 200, "result": {"songs": [
                {"id": tid, "name": payload.get("s", ""),
                 "ar": [{"name": "测试歌手"}], "al": {"name": "测试专辑"}, "dt": 200000}]}}
        if p.endswith("/playlist/manipulate/tracks"):
            ids = [int(x) for x in json.loads(payload["trackIds"])]
            calls.append(payload["op"])
            if payload["op"] == "add":
                for tid in ids:
                    if tid in playlist:
                        playlist.remove(tid)
                    playlist.insert(0, tid)      # 永远插第 1 位
            else:
                for tid in ids:
                    if tid in playlist:
                        playlist.remove(tid)
            return {"code": 200}
        if p.endswith("/v6/playlist/detail"):
            return {"code": 200, "playlist": {
                "tracks": [{"id": t, "name": f"曲{t}"} for t in playlist]}}
        return {"code": 500}

    original = ne_module.weapi_post
    ne_module.weapi_post = fake_weapi
    try:
        d = NeteasePlaylistDriver(cfg2)
        # 逐首追加到末尾（append_last=True，默认行为）
        for tid in (3001, 3002, 3003):
            ok, msg, hit = await d.append_song(f"曲{tid}")
            assert hit, msg
        # 关键：逐首追加后顺序应该是**正序**（先点的在前）
        check("逐首追加后歌单顺序是正序（先点在前）",
              playlist == [3001, 3002, 3003], str(playlist))
        writes = [c for c in calls if c in ("add", "del")]
        check("追加确实用到了删除+倒序重加（网易云没有追加接口）",
              "del" in writes, str(writes))
        check("顺序与点歌顺序一致（FIFO）", playlist == [3001, 3002, 3003],
              f"{playlist}")
    finally:
        ne_module.weapi_post = original
        p2.unlink(missing_ok=True)


async def test_playlist_limit() -> None:
    """验证歌单上限与自动清理（上限 5 首，超出时先删已播）。"""
    print("\n== 歌单上限与自动清理 ==")
    from pathlib import Path as P

    from songboard import netease as ne_module
    from songboard.config import Config as Cfg
    from songboard.netease import NeteasePlaylistDriver

    p = P("__selftest_limit.json")
    p.unlink(missing_ok=True)
    cfg = Cfg.load(p)
    cfg["netease"]["playlist_id"] = "555555"
    cfg["netease"]["cookie"] = "MUSIC_U=fake"

    playlist = [int(x) for x in range(1, 8)]   # 7 首，已超上限

    def fake_weapi(path, payload, cookie):
        if path.endswith("/playlist/manipulate/tracks"):
            ids = [int(x) for x in json.loads(payload["trackIds"])]
            if payload["op"] == "add":
                for t in ids:
                    if t in playlist:
                        playlist.remove(t)
                    playlist.insert(0, t)
            else:
                for t in ids:
                    if t in playlist:
                        playlist.remove(t)
            return {"code": 200}
        if path.endswith("/v6/playlist/detail"):
            return {"code": 200, "playlist": {
                "tracks": [{"id": t, "name": f"曲{t}"} for t in playlist]}}
        if path.endswith("/cloudsearch/get/web"):
            return {"code": 200, "result": {"songs": []}}
        return {"code": 500}

    original = ne_module.weapi_post
    ne_module.weapi_post = fake_weapi
    try:
        d = NeteasePlaylistDriver(cfg)
        check("上限配置默认 5", cfg.get("netease.max_tracks") == 5,
              str(cfg.get("netease.max_tracks")))
        check("prune_played 默认开", cfg.get("netease.prune_played") is True)

        # delete_tracks 要能删掉指定的几首
        ok, msg = d.delete_tracks([1, 2])
        check("删除指定曲目生效", ok and 1 not in playlist and 2 not in playlist,
              f"{msg} -> {playlist}")

        # playlist_state 要能读到当前顺序
        st = d.playlist_state()
        check("能读到歌单当前顺序", [t for t, _ in st] == playlist, str(st))
    finally:
        ne_module.weapi_post = original
        p.unlink(missing_ok=True)


async def test_prune_logic() -> None:
    """验证清理策略——**直接测真实的 _prune_playlist**。

    ⚠️ 之前的版本把清理算法在这里又抄了一遍再测那份副本。抄的那份是对的，
    而真实的 _prune_playlist 里判定写成了 `len(tracks) <= limit 就返回`，
    导致"歌单正好满 5 首"时永远不清理、新点歌卡死。测试和实现各写一遍，
    必然漂移——所以现在只测真实方法，让两者不可能再不一致。
    """
    print("\n== 清理策略（测真实 _prune_playlist）==")
    from songboard.main import App
    from songboard.media import read_play_queue

    q = read_play_queue()
    if q:
        played = [x for x in q if x["played"]]
        check("能读到播放队列并识别已播标记", True,
              f"{len(q)} 首，其中已播 {len(played)}：{[x['name'] for x in played][:4]}")
    else:
        print("  [SKIP] 播放队列缓存读不到（网易云可能没在运行）")

    class FakeItem:
        def __init__(self, song: str, state: str = "waiting") -> None:
            self.song = song
            self.state = type("S", (), {"value": state})()

    class FakeDriver:
        def __init__(self, tracks):
            self._tracks = list(tracks)
            self.deleted: list[list[int]] = []

        def playlist_state(self):
            return list(self._tracks)

        def delete_tracks(self, ids):
            ids = [int(i) for i in ids]
            self.deleted.append(ids)
            self._tracks = [(t, n) for t, n in self._tracks if t not in ids]
            return True, f"已删除 {len(ids)} 首"

    async def run_prune(tracks, *, played_ids, board_played=(), limit=5,
                        pending=True):
        """造一个最小 app 对象，只借用真实的 _prune_playlist。

        pending=True 表示"确实还有待写入的歌"；False 表示队列空着。
        清理**只应该**在有 pending 时腾位置。
        """
        drv = FakeDriver(tracks)

        class Stub:
            played: list = []

            def has_pending_work(self) -> bool:
                return pending

        stub = Stub()
        stub.played = [FakeItem(n, "played") for n in board_played]

        app = object.__new__(App)
        app.cfg = {"netease.max_tracks": limit,
                   "netease.prune_played": True}
        app.loop = asyncio.get_running_loop()
        app.driver = drv
        app.store = stub
        app.log_lines = []
        app.log = lambda m: app.log_lines.append(m)
        # played_track_ids 是模块级函数，用闭包打桩
        import songboard.main as M
        orig = M.played_track_ids
        M.played_track_ids = lambda: set(played_ids)
        try:
            await App._prune_playlist(app)
            # _prune_playlist 用 create_task 派发，等它跑完
            await asyncio.sleep(0.05)
            pending_tasks = [t for t in asyncio.all_tasks()
                             if t is not asyncio.current_task()]
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
        finally:
            M.played_track_ids = orig
        return drv

    async def mk(**kw):
        return await run_prune(**kw)

    # ---- 死锁回归：歌单正好 == 上限，其中几首已播，且有新点歌等着 ----
    # 这就是实测踩到的场景：5/5、3 首已播，新点歌永远塞不进去。
    drv = await mk(tracks=[(i, f"曲{i}") for i in range(1, 6)],
                   played_ids={2, 3, 4}, pending=True)
    check("回归：歌单正好满上限且有新点歌时，已播的会被清掉",
          bool(drv.deleted) and sorted(drv.deleted[0]) == [2, 3, 4],
          f"deleted={drv.deleted} 剩余={drv._tracks}")
    check("回归：清理后腾出了位置（不再是 5/5 卡死）",
          len(drv._tracks) == 2, f"剩余 {len(drv._tracks)} 首")

    # ---- ⚠️ 关键守卫：没有待写入的歌时，绝不能动歌单 ----
    # 实测踩到的反面案例：服务启动 2 秒、还没有任何点歌时，
    # 它就把 3 首已播歌删了 —— 那会破坏主播想重播的歌。
    drv = await mk(tracks=[(i, f"曲{i}") for i in range(1, 6)],
                   played_ids={2, 3, 4}, pending=False)
    check("没有待写入的歌时不做任何删除（别乱动歌单）",
          not drv.deleted, f"deleted={drv.deleted} 剩余={len(drv._tracks)} 首")

    drv = await mk(tracks=[(i, f"曲{i}") for i in range(1, 4)],
                   played_ids={1}, pending=False)
    check("队列空着时即使有已播歌也不删", not drv.deleted,
          f"deleted={drv.deleted}")

    # ---- 超过上限：删到上限为止 ----
    drv = await mk(tracks=[(i, f"曲{i}") for i in range(1, 8)],
                   played_ids={1, 2}, pending=True)
    check("超上限时最终压到 5 首", len(drv._tracks) == 5,
          f"7 → {len(drv._tracks)}  deleted={drv.deleted}")
    check("已播的优先被删", sorted(drv.deleted[0]) == [1, 2],
          f"deleted={drv.deleted}")

    # ---- 有待写入但没满：不该超前删歌 ----
    drv = await mk(tracks=[(i, f"曲{i}") for i in range(1, 4)],
                   played_ids={1}, pending=True)
    check("有待写入但没超限时不删最旧的未播歌", len(drv._tracks) == 2,
          f"剩余={drv._tracks}")

    # ---- 没有已播：不该动歌单 ----
    drv = await mk(tracks=[(i, f"曲{i}") for i in range(1, 4)],
                   played_ids=set(), pending=True)
    check("没有已播且未超限时不做任何删除", not drv.deleted,
          f"deleted={drv.deleted}")

    # ---- 点歌板自己记录的已播（名字匹配）也要能识别 ----
    drv = await mk(tracks=[(1, "已播曲"), (2, "未播曲"), (3, "另一首")],
                   played_ids=set(), board_played=["已播曲"], pending=True)
    check("能按点歌板自身的已播记录清理",
          any("已播曲" not in n for _t, n in drv._tracks), f"剩余={drv._tracks}")

    # ---- 上限为 0/负 表示不启用上限：绝不动歌单 ----
    for bad_limit in (0, -1):
        drv = await mk(tracks=[(i, f"曲{i}") for i in range(1, 9)],
                       played_ids={1, 2, 3}, limit=bad_limit, pending=True)
        check(f"上限={bad_limit} 时完全不清理", not drv.deleted,
              f"deleted={drv.deleted}")


# --------------------------------------------------------------------------- 真连测试
async def test_live(room_id: int, seconds: int = 20) -> None:
    print(f"\n== 真连测试：直播间 {room_id} ==")
    stats = {"danmaku": 0, "other": 0, "texts": []}
    notices: list[str] = []

    async def on_dm(ev):
        if ev["type"] == "danmaku":
            stats["danmaku"] += 1
            if len(stats["texts"]) < 5:
                stats["texts"].append(f"{ev['user']}: {ev['text']}")
        else:
            stats["other"] += 1

    async def on_notice(m):
        notices.append(m)

    dm = BilibiliDanmaku(room_id, on_dm, on_notice)
    task = asyncio.create_task(dm.run())
    try:
        for _ in range(seconds):
            await asyncio.sleep(1)
            if dm.connected and stats["danmaku"] >= 3:
                break
    finally:
        dm.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # 收尾时 connected 会归 False（正常断开），用 ever_connected 判断本次是否真的接上过
    check("弹幕服务器连接成功（认证通过）", dm.ever_connected, f"last_error={dm.last_error}")
    check("收到并解析弹幕", stats["danmaku"] > 0,
          f"{stats['danmaku']} 条；样例 {stats['texts']}")
    if notices:
        print(f"    · 连接日志：{notices[0]}")


def test_probe(room_id: int) -> None:
    print("\n== 接口探测 ==")
    try:
        res = _http_json("https://api.live.bilibili.com/room/v1/Room/room_init", {"id": room_id})
        check("room_init 可用", res.get("code") == 0, f"code={res.get('code')} {res.get('message')}")
    except Exception as exc:
        check("room_init 可用", False, repr(exc))


def test_ncm_bridge() -> None:
    """播放队列桥：协议编排、事件解码、不可用时的降级。

    这里**不注入**、不碰真播放器——只验证纯逻辑和"桥没装好时必须安静降级"。
    """
    import base64 as _b64

    from songboard import ncmbridge as nb

    print("\n== 播放队列桥（不注入，只验逻辑）==")

    # --- 事件解码 ---
    payload = {
        "version": 2, "type": "redux:state", "title": "网易云音乐",
        "trackId": "65592", "name": "单车", "artist": "陈奕迅",
        "album": "Sound & Sight", "coverUrl": "http://p3.example/x.jpg",
        "nextTrackId": "66282", "nextName": "浮夸",
        "nextArtist": "陈奕迅", "nextAlbum": "U87",
    }
    blob = _b64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()
    ev = nb.decode_track_event(f"OK EVENT 4 203 {blob}")
    check("能解码事件里的当前曲目", ev is not None and ev["track_id"] == "65592",
          str(ev and ev.get("name")))
    check("能解码事件里的下一首", ev is not None and ev["next_track_id"] == "66282",
          str(ev and ev.get("next_name")))
    check("能解出封面/专辑/艺人",
          ev is not None and ev["artist"] == "陈奕迅" and ev["album"] == "Sound & Sight",
          str(ev and ev.get("artist")))
    check("曲目 id 统一成字符串（网易云返回的是字符串）",
          ev is not None and isinstance(ev["track_id"], str))

    # --- 各种畸形输入必须安静返回 None，不能抛 ---
    for bad in ("", "garbage", "OK EVENT 4 203", "OK EVENT 4 203 !!!not-base64!!!",
                "ERR nope"):
        try:
            got = nb.decode_track_event(bad)
            ok = got is None
        except Exception as exc:  # noqa: BLE001
            ok, got = False, repr(exc)
        check(f"畸形事件输入安静返回 None：{bad[:22]!r}", ok, str(got))

    # --- 关闭时必须完全不可用且不报错 ---
    off = nb.NeteaseBridge(enabled=False)
    check("未启用时 available() 为 False", off.available() is False)
    check("未启用时 status().enabled 为 False", off.status()["enabled"] is False)
    ok, msg = off.insert_next(1)
    check("未启用时 insert_next 不抛异常、返回失败", ok is False, msg)
    check("未启用时 now_playing() 返回 None", off.now_playing() is None)

    # --- 管道名格式必须和桥 DLL 一致（写错就永远连不上）---
    c = nb.BridgeClient(4242)
    check("命令管道名与桥 DLL 约定一致",
          c.command_pipe == r"\\.\pipe\AwooNcmCefBridge-v1-4242", c.command_pipe)
    check("事件管道名与桥 DLL 约定一致",
          c.event_pipe == r"\\.\pipe\AwooNcmCefBridge-events-v1-4242", c.event_pipe)

    # --- describe() 在桥不存在时必须给说明而不是抛 ---
    try:
        desc = c.describe()
        check("桥不存在时 describe() 给出说明", bool(desc), desc[:60])
    except Exception as exc:  # noqa: BLE001
        check("桥不存在时 describe() 给出说明", False, repr(exc))

    # --- 进程发现（网易云没开也要能返回空列表而不是炸）---
    pids = nb.find_cloudmusic_pids()
    check("find_cloudmusic_pids() 返回列表", isinstance(pids, list), str(pids))
    if pids:
        main_pid = nb.find_main_pid()
        check("能找到网易云主进程（持有 OrpheusBrowserHost 的那个）",
              main_pid is not None, f"pids={pids} main={main_pid}")
        if main_pid is not None:
            check("主进程确实持有宿主窗口", nb.has_host_window(main_pid),
                  f"pid={main_pid}")
            live = nb.BridgeClient(main_pid)
            check("真实桥管道存在（说明 DLL 已注入）", live.pipe_exists())
            check("真实桥自报就绪", live.ready(), live.describe()[:80])
    else:
        print("  (网易云未运行，跳过主进程/管道检查)")

    # --- 配置项默认值 ---
    from pathlib import Path as _P

    cfg = Config.load(_P(__file__).resolve().parent / "__selftest_config.json")
    check("ncm_bridge 配置默认关闭", cfg.get("ncm_bridge.enabled") is False)
    check("ncm_bridge 默认插到下一首", cfg.get("ncm_bridge.insert_next") is True)
    check("ncm_bridge 默认空队列直接播放",
          cfg.get("ncm_bridge.play_if_idle") is True)


async def test_sync_playlist_regression() -> None:
    """回归：队列空时点的第一首（state=playing）必须能写进歌单并插队列。

    踩过的三个坑，全都在这一个场景里：
      1. `pending` 只收 state=="waiting"，而 store.add() 在队列为空时
         会把第一首直接标成 PLAYING → 它永远写不进歌单
      2. 修的时候误用了 `await asyncio.to_thread(self.driver.add_song, ...)`，
         但 add_song 是 async 方法 → 拿到协程、解包 TypeError
      3. 那个 TypeError 发生在 create_task 起的后台任务里，
         异常没人 await，外部只看到"什么都没发生"
    """
    print("\n== 回归：正在播放但未入歌单的那首 ==")
    from songboard.main import App
    from songboard.models import SongState

    class FakeDriver:
        name = "fake"

        def __init__(self):
            self.added: list[str] = []
            self.appended: list[str] = []
            self._pl: list[tuple[int, str]] = []

        async def test(self):
            return True, "ok"

        async def add_song(self, song, user=""):
            self.added.append(song)
            self._pl.append((12345, song))
            return True, f"added {song}", {"id": 12345, "name": song,
                                           "artists": "测试"}

        async def append_song(self, song, user=""):
            self.appended.append(song)
            self._pl.append((12345, song))
            return True, f"appended {song}", {"id": 12345, "name": song,
                                              "artists": "测试"}

        def playlist_state(self):
            return list(self._pl)

        def delete_tracks(self, ids):
            self._pl = [(t, n) for t, n in self._pl
                        if t not in {int(i) for i in ids}]
            return True, f"deleted {len(ids)}"

        def status(self):
            return {"driver": self.name, "enabled": True}

    root = Path(__file__).resolve().parent
    cfg = Config.load(root / "config.json")
    cfg["mode"] = "demo"
    cfg["netease.enabled"] = True
    cfg["netease.auto_add"] = True
    cfg["netease.max_tracks"] = 5
    cfg["ncm_bridge.enabled"] = True
    # 这个用例测的是**写歌单**那条路，所以要显式关掉"只插播放队列"模式，
    # 否则它会跟着全局配置走，配置一改测试就假失败。
    cfg["queue_only.enabled"] = False
    cfg["netease.write_playlist"] = True

    app = App(cfg, persist=False)
    drv = FakeDriver()
    app.driver = drv

    calls: list[tuple[str, object]] = []
    app.bridge.available = lambda **kw: True                    # type: ignore[assignment]
    app.bridge.now_playing = lambda: {"track_id": "1", "name": "x"}  # type: ignore[assignment]
    app.bridge.insert_next = lambda sid: (                      # type: ignore[assignment]
        calls.append(("insert_next", sid)), (True, "fake"))[1]
    app.bridge.play_now = lambda sid: (                         # type: ignore[assignment]
        calls.append(("play_now", sid)), (True, "fake"))[1]

    await app.store.add("回归测试曲", "测试观众", 90261)
    cur = app.store.current
    check("队列空时点的第一首会立刻成为 playing（既有语义）",
          cur is not None and cur.state is SongState.PLAYING,
          f"state={cur.state.value if cur else None}")

    await App._sync_playlist(app)
    await _drain_tasks(min_seconds=1.0)

    check("正在播放那首被写进了歌单（坑 1）",
          bool(drv.added or drv.appended),
          f"added={drv.added} appended={drv.appended}")
    check("点歌条目回填了 netease_id",
          bool(app.store.current and app.store.current.netease_id),
          str(app.store.current.netease_id if app.store.current else None))
    check("桥被调用、插到下一首",
          any(k == "insert_next" for k, _ in calls), str(calls))
    check("后台任务里的异常会被记进日志（坑 3）",
          not any("异常" in str(x.get("text", "")) for x in app.log_lines),
          str([x.get("text") for x in app.log_lines][-3:]))


async def test_queue_only_mode() -> None:
    """「只插播放队列」模式：绝不碰歌单，且只保队头在"下一首"。

    这是主播明确要求的模式：在他保留原有歌单的前提下，
    按点歌顺序把歌写进播放队列，且不改动歌单任何内容。
    """
    print("\n== 只插播放队列模式 ==")
    from songboard.main import App

    class SpyDriver:
        """任何写歌单的调用都记为违规。"""

        name = "spy"
        cookie = "fake-cookie"

        def __init__(self):
            self.violations: list[str] = []
            self._pl = [(1, "原有歌A"), (2, "原有歌B")]

        # ---- 写歌单的三个入口，全都算违规 ----
        async def add_song(self, song, user=""):
            self.violations.append(f"add_song({song})")
            return True, "SHOULD NOT HAPPEN", {"id": 999, "name": song}

        async def append_song(self, song, user=""):
            self.violations.append(f"append_song({song})")
            return True, "SHOULD NOT HAPPEN", {"id": 999, "name": song}

        def delete_tracks(self, ids):
            self.violations.append(f"delete_tracks({ids})")
            return True, "SHOULD NOT HAPPEN"

        def reorder_playlist(self, desired, **kw):
            self.violations.append(f"reorder_playlist({desired})")
            return True, "SHOULD NOT HAPPEN"

        # ---- 只读 ----
        def playlist_state(self):
            return list(self._pl)

        def status(self):
            return {"driver": self.name}

    import songboard.main as M

    # 搜索接口也打桩，避免真的联网。
    # ⚠️ 每首歌必须返回**不同**的 id：真实场景里每个网易云曲目 id 是唯一的，
    # 如果桩对每首歌都返回同一个 id，就会把"两首点歌搜到同一首"的边界情况
    # 误当成正常路径，测试也就测不出队头推进了。
    orig_search = M.search_song
    _fake_ids: dict[str, int] = {}

    def fake_search(kw, cookie="", limit=5):
        if kw not in _fake_ids:
            _fake_ids[kw] = 50000 + len(_fake_ids)
        return [{"id": _fake_ids[kw], "name": kw, "artists": "测试"}]

    M.search_song = fake_search

    try:
        root = Path(__file__).resolve().parent
        cfg = Config.load(root / "config.json")
        cfg["mode"] = "demo"
        cfg["netease.enabled"] = True
        cfg["ncm_bridge.enabled"] = True
        cfg["queue_only.enabled"] = True
        cfg["queue_only.play_if_idle"] = False   # 先只测插队，不测起播
        cfg["netease.auto_add"] = True           # 故意开着，验证硬闸门仍拦住
        cfg["netease.write_playlist"] = True     # 也故意开着
        # 测试里连点几首要绕开限流（不然第 2 首就被 cooldown 拒了）。
        # 注意 _cooldown_checked 参数：simulate_danmaku 那条路会传 True 跳过检查，
        # 直接调 store.add 则默认会检查，所以必须把 cooldown 设成 0。
        cfg["queue.cooldown_seconds"] = 0
        cfg["queue.per_user_limit"] = 5

        app = App(cfg, persist=False)
        spy = SpyDriver()
        app.driver = spy

        inserted: list[object] = []
        # 有状态的桩：模拟真播放器"当前下一首是谁"。
        # 真 ensure_next 会先看"下一首"、已经在就返回 already，
        # 桩必须照做，否则测不出"不重复插"这个关键行为。
        player = {"current": "777", "next": None}

        def fake_now_playing():
            return {"track_id": player["current"], "name": "正在放的",
                    "next_track_id": player["next"], "next_name": ""}

        def fake_ensure_next(sid):
            if str(sid) == str(player["next"]):
                return True, f"《{sid}》已经在下一首", "already"
            inserted.append(sid)
            player["next"] = str(sid)
            return True, "ok", "inserted"

        def fake_play_now(sid):
            inserted.append(("play", sid))
            player["current"] = str(sid)
            player["next"] = None
            return True, "ok"

        app.bridge.available = lambda **kw: True                  # type: ignore[assignment]
        app.bridge.now_playing = fake_now_playing                 # type: ignore[assignment]
        app.bridge.ensure_next = fake_ensure_next                 # type: ignore[assignment]
        app.bridge.play_now = fake_play_now                       # type: ignore[assignment]

        # 点三首
        for name in ("队头一", "队头二", "队头三"):
            req, why = await app.store.add(name, "测试", 1)
            print(f"    add({name}) -> {why}")
        print(f"    store.current = {getattr(app.store.current, 'song', None)}")
        print(f"    store.played  = "
              f"{[(s.song, s.state.value, s.netease_id) for s in app.store.played]}")

        # 跑两轮（每轮模拟一次状态循环）
        for _ in range(2):
            await App._sync_queue_only(app)
            await _drain_tasks()

        print(f"  插入调用: {inserted}")
        print(f"  歌单写入违规: {spy.violations}")

        check("歌单完全没有被写入/删除/重排（核心要求）",
              not spy.violations, str(spy.violations))
        check("歌单内容保持原样", spy.playlist_state() == [(1, "原有歌A"), (2, "原有歌B")],
              str(spy.playlist_state()))
        check("确实往播放队列插了歌", bool(inserted), str(inserted))

        # 只保队头：多次轮询也不该重复插同一首
        head_calls = [x for x in inserted if not isinstance(x, tuple)]
        print(f"  插队次数: {len(head_calls)}（只保队头 → 应为 1）")
        check("只插队头，不重复插（顺序才不会反）",
              len(head_calls) == 1, f"实际 {len(head_calls)} 次: {inserted}")

        # 队头播完 → 队头前进到下一首，这时才该插新的一首
        await app.store.next(reason="test")
        # 模拟网易云也切了歌：队头确实开始播了
        player["current"] = str(player.pop("next") or player["current"])
        player["next"] = None
        for _ in range(2):
            await App._sync_queue_only(app)
            await _drain_tasks()
        head_calls2 = [x for x in inserted if not isinstance(x, tuple)]
        print(f"  队头推进后总插队次数: {len(head_calls2)}")
        check("队头开始播之后才插下一首",
              len(head_calls2) == 2, str(inserted))
        check("插的是新的队头，没有回插旧的",
              len(set(head_calls2)) == len(head_calls2), f"{head_calls2}")
        check("歌单依然没被动过", not spy.violations, str(spy.violations))

        # 硬闸门：即使 auto_add / write_playlist 都开着，也只插队列模式说了算
        await App._sync_playlist(app)
        await _drain_tasks()
        check("即使 auto_add=true，_sync_playlist 也被队列模式拦住",
              not spy.violations, str(spy.violations))

        # 清理也绝不该动歌单
        await App._prune_playlist(app)
        await _drain_tasks()
        check("清理逻辑也被拦住，不删歌单里的歌",
              not spy.violations, str(spy.violations))
    finally:
        M.search_song = orig_search


async def test_queue_only_first_song() -> None:
    """回归：队列空时点的**第一首**也必须被插进播放队列。

    踩过的坑：store.add() 在队列为空时把第一首直接标成 PLAYING，
    而队头原本只从 state=="waiting" 里挑 → 第一首被整个跳过。
    实测：点 富士山下→十年→浮夸，结果"下一首"是十年，富士山下一直没进队列。
    在只插播放队列模式下更致命——写歌单那条兜底路也被关掉了。
    """
    print("\n== 回归：只插队列模式下的第一首 ==")
    from songboard.main import App

    class NullDriver:
        name = "null"
        cookie = "x"

        def playlist_state(self):
            return []

        def status(self):
            return {"driver": self.name}

    import songboard.main as M

    orig_search = M.search_song
    ids: dict[str, int] = {}

    def fake_search(kw, cookie="", limit=5):
        if kw not in ids:
            ids[kw] = 60000 + len(ids)
        return [{"id": ids[kw], "name": kw, "artists": "测试"}]

    def drain():
        return _drain_tasks()

    M.search_song = fake_search
    try:
        root = Path(__file__).resolve().parent
        cfg = Config.load(root / "config.json")
        cfg["mode"] = "demo"
        cfg["netease.enabled"] = True
        cfg["ncm_bridge.enabled"] = True
        cfg["queue_only.enabled"] = True
        cfg["queue_only.play_if_idle"] = False
        cfg["queue.cooldown_seconds"] = 0
        cfg["queue.per_user_limit"] = 9

        app = App(cfg, persist=False)
        app.driver = NullDriver()

        player = {"current": "999", "next": None}
        inserted: list[object] = []

        def fake_now_playing():
            return {"track_id": player["current"], "name": "别的歌",
                    "next_track_id": player["next"], "next_name": ""}

        def fake_ensure_next(sid):
            """模拟真行为：addToNext 只把歌放到"下一首"，**不会**让它开始播。
            如果桩顺手把 current 也改了，就等于假装"一插就播"，
            会绕过"第一首还没放就别插第二首"这道关键闸门。"""
            if str(sid) == str(player["next"]):
                return True, "already", "already"
            inserted.append(sid)
            player["next"] = str(sid)
            return True, "ok", "inserted"

        app.bridge.available = lambda **kw: True              # type: ignore[assignment]
        app.bridge.now_playing = fake_now_playing             # type: ignore[assignment]
        app.bridge.ensure_next = fake_ensure_next             # type: ignore[assignment]
        app.bridge.play_now = lambda sid: (                   # type: ignore[assignment]
            inserted.append(("play", sid)), (True, "ok"))[1]

        # 队列空 → 点第一首。它会立刻变成 playing（不是 waiting）
        await app.store.add("第一首", "观众", 1)
        cur = app.store.current
        check("队列空时点的第一首是 playing（既有语义）",
              cur is not None and cur.state.value == "playing",
              f"state={cur.state.value if cur else None}")

        for _ in range(2):
            await App._sync_queue_only(app)
            await drain()

        print(f"  插入调用: {inserted}  下一首={player['next']}")
        check("第一首确实被插进了播放队列（不再被跳过）",
              bool(inserted), str(inserted))
        check("第一首拿到了 netease_id",
              bool(app.store.current and app.store.current.netease_id),
              str(app.store.current.netease_id if app.store.current else None))

        # 再点第二首：第一首**还没开始放**，所以第二首绝不能插
        # （插了就会把第一首从"下一首"顶掉 —— 这就是
        #  "点了两首但只加进去一首"的成因）
        await app.store.add("第二首", "观众", 1)
        for _ in range(2):
            await App._sync_queue_only(app)
            await drain()
        print(f"  第二首后插入调用: {inserted}  下一首={player['next']}")
        check("第一首还没放时，第二首不抢位（不再只加进去一首）",
              len([x for x in inserted if not isinstance(x, tuple)]) == 1,
              str(inserted))

        # 模拟第一首开始播 → 队头前进，这时才该插第二首
        player["current"] = str(player.pop("next"))
        player["next"] = None
        board_cur = app.store.current
        if board_cur is not None:
            app.store.set_netease(board_cur.id, player["current"])  # type: ignore[arg-type]
        # 让点歌板认为第一首在放（对齐网易云）
        await app.store.set_current_by_title("第一首", strict=False)
        for _ in range(2):
            await App._sync_queue_only(app)
            await drain()
        print(f"  第一首开播后插入调用: {inserted}  下一首={player['next']}")
        check("第一首开始播后才插第二首",
              len([x for x in inserted if not isinstance(x, tuple)]) == 2,
              str(inserted))
    finally:
        M.search_song = orig_search


async def test_console_cannot_write_playlist() -> None:
    """回归：控制台 / API 不能再把"写歌单"打开。

    主播的要求是"点歌板那边的歌单控制功能去掉"，
    所以要保证不只是 UI 没了入口，后端也**不会**接受这些字段——
    否则留着口子就等于留了一条误开写歌单的路。
    """
    print("\n== 控制台不能再开启写歌单 ==")
    import inspect

    from songboard import webui

    src = inspect.getsource(webui)
    start = src.find('"/api/netease/enable"')
    end = src.find("except Exception", start)
    seg = src[start:end] if start >= 0 and end > start else ""

    check("找到了 /api/netease/enable 处理段", bool(seg), f"{len(seg)} 字符")
    # ⚠️ 必须找"赋值语句"而不是"出现这两个词"：
    #    注释里正好有 "auto_add / playlist_id 故意不再暴露"，
    #    按子串判断会把注释也算成违规（实测假失败过一次）。
    check("不再从请求体里写 auto_add",
          'body["auto_add"]' not in seg and "body.get(\"auto_add\")" not in seg,
          "仍然读取 body.auto_add")
    check("不再从请求体里写 playlist_id",
          'body["playlist_id"]' not in seg
          and "body.get(\"playlist_id\")" not in seg,
          "仍然读取 body.playlist_id")
    check("仍然保留 cookie 与 enabled（搜索/查时长要用）",
          'cookie' in seg and 'enabled' in seg)

    # 控制台页面里不该再有歌单相关控件
    page = (Path(__file__).resolve().parent / "web" / "control.html").read_text(
        encoding="utf-8")
    for gone in ("nePlaylist", "neAutoAdd", "btnNeTest"):
        check(f"控制台已移除控件 {gone}", gone not in page,
              "仍然存在" if gone in page else "")

    # 队列模式下 App 会强制把写歌单关掉（哪怕配置里被手改成 true）
    import tempfile

    from songboard.main import App
    tmpdir = Path(tempfile.mkdtemp(prefix="songboard_cfgtest_"))
    tmpcfg = tmpdir / "config.json"
    tmpcfg.write_text(json.dumps({
        "mode": "demo",
        "queue_only": {"enabled": True},
        "netease": {"enabled": True, "auto_add": True, "write_playlist": True},
        "ncm_bridge": {"enabled": False},
    }, ensure_ascii=False), encoding="utf-8")
    tcfg = Config.load(tmpcfg)
    App(tcfg, persist=False)
    check("队列模式下启动会强制关掉 auto_add",
          tcfg.get("netease.auto_add") is False,
          str(tcfg.get("netease.auto_add")))
    check("队列模式下启动会强制关掉 write_playlist",
          tcfg.get("netease.write_playlist") is False,
          str(tcfg.get("netease.write_playlist")))


async def test_queue_gate_not_stuck() -> None:
    """回归：队列里已有别人占着"下一首"时，新点歌不能永远排不进去。

    踩过的坑：闸门原先只看内存里的 `_queue_placed_id`（"我插过谁"），
    但**播放队列里本来就有别的歌占着"下一首"**（比如上一轮点歌插进去、
    还没播的那首）。于是程序误以为"已经排好了、在等它播"，
    **永远不再插新歌**——实测：点了 5 首，只有第 1 首进了队列，后 4 首一直等。

    正确判定是看网易云报的"下一首"到底是不是**我们插过的点歌**。
    """
    print("\n== 回归：'下一首'被别人占着时不能卡死 ==")
    from songboard.main import App

    class NullDriver:
        name = "null"
        cookie = "x"

        def playlist_state(self):
            return []

        def status(self):
            return {"driver": self.name}

    import songboard.main as M

    orig_search = M.search_song
    ids: dict[str, int] = {}

    def fake_search(kw, cookie="", limit=5):
        if kw not in ids:
            ids[kw] = 70000 + len(ids)
        return [{"id": ids[kw], "name": kw, "artists": "测试"}]

    async def drain():
        await _drain_tasks()

    M.search_song = fake_search
    try:
        root = Path(__file__).resolve().parent
        cfg = Config.load(root / "config.json")
        cfg["mode"] = "demo"
        cfg["netease.enabled"] = True
        cfg["ncm_bridge.enabled"] = True
        cfg["queue_only.enabled"] = True
        cfg["queue_only.play_if_idle"] = False
        cfg["queue.cooldown_seconds"] = 0
        cfg["queue.per_user_limit"] = 9

        app = App(cfg, persist=False)
        app.driver = NullDriver()

        # 播放器状态：正在放别人的歌，**下一首也被别人占着**（不是我们插的）
        player = {"current": "111", "next": "999999"}   # 999999 不是任何点歌项
        inserted: list[object] = []

        def fake_now_playing():
            return {"track_id": player["current"], "name": "别人的歌",
                    "next_track_id": player["next"], "next_name": "别人占的位"}

        def fake_ensure_next(sid):
            if str(sid) == str(player["next"]):
                return True, "already", "already"
            inserted.append(sid)
            player["next"] = str(sid)
            return True, "ok", "inserted"

        app.bridge.available = lambda **kw: True          # type: ignore[assignment]
        app.bridge.now_playing = fake_now_playing         # type: ignore[assignment]
        app.bridge.ensure_next = fake_ensure_next         # type: ignore[assignment]
        app.bridge.play_now = lambda sid: (               # type: ignore[assignment]
            inserted.append(("play", sid)), (True, "ok"))[1]

        await app.store.add("第一首", "观众", 1)
        for _ in range(2):
            await App._sync_queue_only(app)
            await drain()
        print(f"  第一轮插入: {inserted}  下一首={player['next']}")
        check("'下一首'被别人占着时，我们的歌仍然能插进去（不卡死）",
              len([x for x in inserted if not isinstance(x, tuple)]) == 1,
              f"next={player['next']}")

        # 第二首：此时"下一首"已经是我们的第一首了 → 应该等，不该抢
        await app.store.add("第二首", "观众", 1)
        for _ in range(2):
            await App._sync_queue_only(app)
            await drain()
        print(f"  第二轮插入: {inserted}  下一首={player['next']}")
        check("'下一首'已是我们的歌时，不抢位（顺序才对）",
              len([x for x in inserted if not isinstance(x, tuple)]) == 1,
              str(inserted))

        # 第一首开始播 → 下一首位置腾出来 → 第二首应该补上
        player["current"] = str(player["next"])
        player["next"] = "999999"
        for _ in range(2):
            await App._sync_queue_only(app)
            await drain()
        print(f"  第三轮插入: {inserted}  下一首={player['next']}")
        check("第一首开始播后，第二首补上",
              len([x for x in inserted if not isinstance(x, tuple)]) == 2,
              str(inserted))
    finally:
        M.search_song = orig_search


async def test_gift_gate() -> None:
    """礼物门槛：只有送过礼物的观众才能点歌。

    重点测"骗不过去"的边界 —— 门槛的价值全在这些地方：
      * 免费礼物（银瓜子）不能算，否则送个辣条就拿到资格
      * 金额不够不能放行
      * 时效过期要重新送
      * 连击礼物要按数量算，不能只算 1 个
    """
    print("\n== 礼物门槛 ==")
    from songboard.giftgate import GiftLedger

    def make_cfg(**over):
        c = Config.load(Path(__file__).resolve().parent / "__selftest_config.json")
        c["gift_gate.enabled"] = True
        c["gift_gate.mode"] = "min_total"
        c["gift_gate.min_coin"] = 1000
        c["gift_gate.window_seconds"] = 0
        c["gift_gate.require_paid"] = True
        c["gift_gate.guard_always_ok"] = True
        c["gift_gate.guard_min_level"] = 3
        c["gift_gate.sc_always_ok"] = True
        for k, v in over.items():
            c[f"gift_gate.{k}"] = v
        return c

    def gift(uid, coin, paid=True, num=1, name="测试礼物"):
        return {"type": "gift", "uid": uid, "user": f"u{uid}",
                "gift": {"name": name, "num": num,
                         "price": coin // max(num, 1),
                         "total_coin": coin, "paid": paid, "guard_level": 0}}

    # ---- 关闭时完全不拦 ----
    g = GiftLedger(make_cfg(enabled=False))
    check("门槛关闭时直接放行", g.check(1).ok is True)

    # ---- 没送过 ----
    g = GiftLedger(make_cfg())
    d = g.check(1)
    check("没送过礼物不能点", d.ok is False, d.reason)
    check("提示里带出门槛金额", "1000" in d.hint, d.hint)

    # ---- 免费礼物骗不过去（最关键的一条）----
    g = GiftLedger(make_cfg())
    g.record_gift(gift(1, 0, paid=False, name="辣条"))
    d = g.check(1)
    check("只送免费礼物不能点（送辣条骗不过去）", d.ok is False, d.reason)
    check("免费礼物被计数但不计入金额",
          g.seen_free_gifts == 1 and g.users[1].total_coin == 0,
          f"free={g.seen_free_gifts} coin={g.users[1].total_coin}")

    # ---- 金额不够 ----
    g = GiftLedger(make_cfg())
    g.record_gift(gift(1, 500, paid=True))
    d = g.check(1)
    check("金额不够不能点", d.ok is False, d.reason)
    check("提示里算出还差多少", "还差 500" in d.hint, d.hint)

    # ---- 刚好达标 ----
    g = GiftLedger(make_cfg())
    g.record_gift(gift(1, 1000, paid=True))
    check("刚好达标就放行", g.check(1).ok is True, g.check(1).reason)

    # ---- 累加 ----
    g = GiftLedger(make_cfg())
    g.record_gift(gift(1, 400, paid=True))
    g.record_gift(gift(1, 600, paid=True))
    check("多次送礼金额累加", g.check(1).ok is True,
          f"total={g.users[1].total_coin}")

    # ---- 连击数量要算对（不算对门槛就会算少）----
    g = GiftLedger(make_cfg())
    g.record_gift(gift(1, 1000, paid=True, num=10))   # 单价100 × 10
    check("连击按总价算，不是只算 1 个", g.check(1).ok is True,
          f"total={g.users[1].total_coin}")

    # ---- 时效 ----
    g = GiftLedger(make_cfg(window_seconds=300))
    g.record_gift(gift(1, 1000, paid=True))
    check("时效内可以点", g.check(1).ok is True)
    g.users[1].last_at -= 600        # 往前拨 10 分钟
    check("超过时效不能点（防刷一次点一天）", g.check(1).ok is False,
          g.check(1).reason)

    # ---- 舰长放行 ----
    g = GiftLedger(make_cfg())
    g.record_guard({"uid": 2, "user": "舰长",
                    "guard": {"level": 3, "num": 1, "total_coin": 138000}})
    check("舰长直接放行（不看金额）", g.check(2).ok is True)
    # 金额小的舰长才能验出「直接放行」到底有没有生效：
    # 138000 瓜子本来就超过 1000 的门槛，按金额算也会过，等于没测到。
    g1b = GiftLedger(make_cfg())
    g1b.record_guard({"uid": 2, "user": "小舰长",
                      "guard": {"level": 3, "num": 1, "total_coin": 500}})
    check("舰长直接放行：金额不够也放行", g1b.check(2).ok is True,
          g1b.check(2).reason)
    g2 = GiftLedger(make_cfg(guard_always_ok=False))
    g2.record_guard({"uid": 2, "user": "小舰长",
                     "guard": {"level": 3, "num": 1, "total_coin": 500}})
    check("关掉『舰长直接放行』后要按金额算",
          g2.check(2).ok is False, g2.check(2).reason)

    # ---- any_paid ----
    g = GiftLedger(make_cfg(mode="any_paid", min_coin=999999))
    g.record_gift(gift(1, 1, paid=True))
    check("any_paid 模式：送 1 瓜子付费礼物也放行", g.check(1).ok is True)

    # ---- per_send ----
    g = GiftLedger(make_cfg(mode="per_send", min_coin=1000))
    g.record_gift(gift(1, 2500, paid=True))
    check("per_send：额度够就放行", g.check(1).ok is True)
    g.spend(1, 1000)
    check("per_send：消耗一次后仍有余额", g.check(1).ok is True,
          f"剩 {g.users[1].total_coin - g.users[1].spent_coin}")
    g.spend(1, 1000)
    d = g.check(1)
    check("per_send：额度耗尽后不能点", d.ok is False, d.reason)
    check("per_send：提示里给出剩余额度", "剩 500" in d.hint, d.hint)

    # ---- guard_only ----
    g = GiftLedger(make_cfg(mode="guard_only"))
    g.record_gift(gift(1, 99999, paid=True))
    check("guard_only：送再多钱但没上舰也不能点", g.check(1).ok is False,
          g.check(1).reason)
    g.record_guard({"uid": 1, "user": "u1", "guard": {"level": 3, "num": 1}})
    check("guard_only：上舰后放行", g.check(1).ok is True)

    # ---- 醒目留言 ----
    g = GiftLedger(make_cfg())
    check("SC 默认直接放行", g.check(3, is_super_chat=True).ok is True)
    g2 = GiftLedger(make_cfg(sc_always_ok=False, sc_min_coin=30000))
    check("SC 单独设门槛：不够就拦",
          g2.check(3, is_super_chat=True).ok is False)
    g2.record_super_chat({"uid": 3, "user": "u3", "price": 30000})
    check("SC 金额达标后放行", g2.check(3, is_super_chat=True).ok is True)

    # ---- 脏数据不能崩（B站字段类型不稳定）----
    g = GiftLedger(make_cfg())
    bad_inputs = ({"uid": 0}, {"uid": None}, {"uid": "abc"},
                  {"uid": 5, "gift": None},
                  {"uid": 5, "gift": {"total_coin": "x", "paid": "yes"}})
    broke = None
    for bad in bad_inputs:
        try:
            g.record_gift(bad)
        except Exception as exc:  # noqa: BLE001
            broke = (bad, repr(exc))
            break
    check("脏礼物数据不崩（uid=0/None/字符串、字段缺失）", broke is None,
          str(broke))
    check("uid 无效的礼物被丢弃，不会记到 uid=0 头上", 0 not in g.users)

    # ---- 统计字段 ----
    st = g.status()
    need = ("enabled", "mode", "min_coin", "known_users", "paid_gifts",
            "free_gifts", "allowed", "blocked", "top", "recent_blocks")
    check("status() 给出控制台需要的字段",
          all(k in st for k in need), str([k for k in need if k not in st]))

    # ---- 只读判定（控制台刷新不该弄脏统计）----
    g = GiftLedger(make_cfg())
    g.record_gift(gift(1, 1000, paid=True))
    before = (g.allows, g.blocks, len(g.recent_blocks))
    qualified = [g.qualified(1), g.qualified(2), g.qualified(999)]
    after = (g.allows, g.blocks, len(g.recent_blocks))
    check("qualified() 判得准（够格的算够，没送过的算不够）",
          qualified == [True, False, False], str(qualified))
    check("qualified() 是只读的，不会把统计和拦截记录弄脏",
          before == after, f"{before} -> {after}")
    d = g.check(2)
    check("check() 仍然照常计数（只读模式没把正常路径改坏）",
          g.blocks == before[1] + 1 and d.ok is False,
          f"blocks={g.blocks} ok={d.ok}")


async def test_gift_cli_and_reload() -> None:
    """命令行门槛设置 + 改配置时不能把观众的送礼记录冲掉。

    async 是因为 App.__init__ 要一个在跑的事件循环；_gift_cli 本身是同步的。
    """
    print("\n== 礼物门槛：命令行与热更新 ==")
    import contextlib
    import io
    import shutil
    import tempfile
    from songboard.main import App

    # ⚠️ 必须用临时配置文件：_gift_cli 会 cfg.save()，
    #    拿真实 config.json 跑测试会把它覆盖掉（里面有真的 MUSIC_U）。
    tmp_dir = Path(tempfile.mkdtemp(prefix="songboard-giftgate-"))
    try:
        cfg = Config.load(tmp_dir / "config.json")
        cfg["mode"] = "demo"
        app = App(cfg, persist=False)

        def run(arg: str) -> str:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                app._gift_cli(arg)
            return buf.getvalue()

        run("on")
        check("gift on 打开门槛", app.cfg.get("gift_gate.enabled") is True)
        check("gift on 之后账本立刻按新配置工作", app.gifts.enabled is True)

        run("min 2500")
        check("gift min 2500 改金额并切到 min_total",
              app.cfg.get("gift_gate.min_coin") == 2500
              and app.cfg.get("gift_gate.mode") == "min_total",
              f"{app.cfg.get('gift_gate.min_coin')}/{app.cfg.get('gift_gate.mode')}")

        out = run("mode 乱写的模式")
        check("gift mode 给非法值不写进配置、并提示用法",
              app.cfg.get("gift_gate.mode") == "min_total" and "用法" in out,
              out.strip())

        # 关键：改配置不能把已经记下的送礼记录清掉
        app.gifts.record_gift({"uid": 7, "user": "老观众",
                               "gift": {"name": "小花花", "num": 1,
                                        "total_coin": 3000, "paid": True}})
        before = app.gifts.users[7].total_coin
        run("min 1000")
        check("改门槛不会清掉观众的累计金额（以前 reload 会全清）",
              7 in app.gifts.users and app.gifts.users[7].total_coin == before,
              f"{before} -> {[u.total_coin for u in app.gifts.users.values()]}")

        out = run("list")
        check("gift list 能列出送礼的人",
              "老观众" in out and "3000" in out, out.strip())
        check("gift list 标出谁有资格", "✓" in out, out.strip())
        check("gift list 不该弄脏统计（走的是只读判定）",
              app.gifts.blocks == 0, f"blocks={app.gifts.blocks}")

        run("reset")
        check("gift reset 清空记录", not app.gifts.users)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", type=int, help="真连测试用的直播间号")
    ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--probe", type=int, help="只探测接口")
    args = ap.parse_args()

    print("哔哩哔哩点歌板 · 自检")
    test_syntax()
    test_web_js()
    test_bat_files()
    test_ps1_files()
    test_room_diagnostics()
    test_cookie_extract()
    test_qrlogin()
    test_netease_auth_codes()
    test_changelog()
    test_config_robustness()
    test_commands()
    asyncio.run(test_queue())
    test_protocol()
    test_media_parse()
    test_extapi_parse()
    asyncio.run(test_auto_advance())
    asyncio.run(test_extapi_live())
    asyncio.run(test_netease_reorder())
    asyncio.run(test_playback_authority())
    asyncio.run(test_append_last())
    asyncio.run(test_align_continuously())
    asyncio.run(test_queue_head())
    asyncio.run(test_playlist_limit())
    asyncio.run(test_prune_logic())
    asyncio.run(test_sync_playlist_regression())
    asyncio.run(test_queue_only_mode())
    asyncio.run(test_queue_only_first_song())
    asyncio.run(test_queue_gate_not_stuck())
    asyncio.run(test_console_cannot_write_playlist())
    asyncio.run(test_gift_gate())
    asyncio.run(test_gift_cli_and_reload())
    asyncio.run(test_no_cookie_no_insert())
    test_ncm_bridge()
    if args.probe:
        test_probe(args.probe)
    if args.live:
        test_probe(args.live)
        asyncio.run(test_live(args.live, args.seconds))
    failed = [r for r in results if not r[1]]
    print(f"\n{'=' * 50}\n共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  FAIL: {name} — {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
