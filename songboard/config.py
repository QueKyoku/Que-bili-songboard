"""配置：从 config.json 读取，缺省项自动补全并回写。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "room_id": 0,
    # demo = 不连直播间，本地随机生成弹幕，用来预览点歌板效果
    "mode": "demo",
    "http_host": "127.0.0.1",
    "http_port": 8765,
    "danmaku": {
        # 点歌指令前缀，多个任选其一。{song} 是歌名占位
        "prefixes": ["点歌", "!点歌", "#点歌", "求歌"],
        "skip_keywords": ["切歌", "下一首", "跳过"],
        "cancel_keywords": ["取消点歌", "撤销点歌"],
        "query_keywords": ["查询点歌", "我的点歌", "点歌查询"],
    },
    "queue": {
        "max_size": 30,          # 队列上限，满了之后的点歌进备选
        "per_user_limit": 2,     # 每人同时最多在队列里的歌
        "cooldown_seconds": 30,  # 同一人两次点歌的最小间隔
        "max_song_name_len": 40,
    },
    "board": {
        "title": "点歌板",
        "subtitle": "弹幕发送「点歌 歌名」",
        "show_size": 8,          # 叠加层显示几条
        "theme": "dark",
    },
    "extapi": {
        # 外部媒体信息源：能读到"精确进度"的第三方本机服务
        # （如 Metabox-Nexus-PlayerCap / now-playing-service 之类）。
        # 网易云客户端自己不提供进度，这是唯一能拿到进度的路子。
        "enabled": False,
        # 可以填多个地址，程序会按顺序取并合并成一条曲目信息。
        # Metabox-Nexus-PlayerCap 实测需要两个（单接口都缺字段）：
        #   song_info  有 name/singer，没有时长
        #   all_lyrics 有 duration/position，没有纯歌名
        "urls": [],
        "url": "",              # 兼容只填一个的情况
        "poll_seconds": 2,
        "timeout": 3,
        "max_bytes": 8388608,   # 对方可能内嵌封面 base64（实测单次 4.2MB），给大一点
        # 键名认不出来时手写路径，例如
        # {"title": "data.name", "artist": "data.singer",
        #  "position": "data.position", "duration": "data.duration"}
        "fields": {},
    },
    "media": {
        # 自动下一首：靠"读到当前在放什么" + "播了多久"判断
        "auto_next": True,
        "watch": True,                  # 轮询正在播放状态（只读窗口标题/系统媒体会话）
        "poll_seconds": 2,
        "duration_fallback": 300,       # 查不到真实时长时，按这个秒数算播完（宁可长一点，别提前切）
        "grace_seconds": 9,             # 歌名变化后等几秒再判定（过滤切歌瞬间的抖动）
        "min_playing_seconds": 25,      # 至少播了这么久，才认可是"自然人换歌"而非自动跳过
        "almost_done_seconds": 3,       # 播放器进度剩这么多秒就当作播完
        # ⚠️ 浏览器会话可能没有标题，分不清"音乐页"和"B站视频页"。
        # 开启后浏览器进度才会参与自动切歌——只在"浏览器里没有别的音频"时才安全。
        "trust_browser_progress": False,
        "match_threshold": 0.5,         # 歌名相似度阈值
        "prefer_apps": ["msedge", "chrome", "firefox", "spotify", "potplayer", "cloudmusic"],
    },
    "netease": {
        # 读歌单/查时长用。开着才能用这些能力。
        "enabled": False,
        # ⚠️ 是否**自动**把点歌写进歌单。关掉后程序只读不写，
        # 加歌完全由主播手动操作（避免程序乱动你的歌单）。
        "auto_add": False,
        "playlist_id": "",
        "cookie": "",
        "driver": "playlist",
        # 重排歌单顺序（让点歌顺序 = 歌单顺序）。
        # 只在 auto_add 开着时才有意义。
        "reorder": True,
        # 加歌策略：只把**队头那一首**写进歌单，等它开始播再写下一首。
        # 这样永远只追加一首，绕开"加歌永远插第 1 位"导致的顺序颠倒，
        # 同时把接口调用从 O(N) 降到 O(1)。
        # 关掉后会退回"整个队列一次性写入"的旧行为（顺序会反）。
        "queue_head_only": True,
        # 是否额外把"还没播的歌"按点歌顺序整体重排（默认关，队头模式已保证顺序）
        "order_playlist": False,
        # 新歌加到歌单**末尾**（网易云默认插第 1 位，这里用"删除+倒序重加"纠正）
        "append_last": True,
        # 歌单最多保留多少首；超出时自动清理，避免歌单过长导致客户端卡顿
        "max_tracks": 5,
        # 清理时优先删"已经播过的"歌（按播放状态判断）
        "prune_played": True,
        # ⚠️ 是否允许程序**动你的歌单**（加歌 / 删歌 / 重排）。
        # 设 false 后：只往"播放队列"插歌，歌单一个字节都不改，
        # 你原有的歌单顺序和内容完全保持原样。
        "write_playlist": False,
    },
    "queue_only": {
        # 「只插播放队列」模式：点歌只进播放队列，绝不碰歌单。
        #
        # 为什么这样更好（实测结论）：
        #   1. 往歌单加歌**不会**改变播放队列，两条路本来就要分别做
        #   2. 往歌单加歌永远插在**第 1 位**，要"追加到末尾"只能
        #      "全部删除 + 倒序重加"，是最重的操作，还容易出错
        #   3. 网易云播放队列里每条有 scene 字段，桥插进去的是
        #      "orpheusMessage"，和歌单来的 "playlist" 区分得开
        #
        # 插队顺序（对照实验确认，别搞错）：
        #   `addToNext` 把曲目**插入到"当前播放曲目之后"**，原有队列完整保留，
        #   不会挤掉谁。实测 ADD_NEXT(起风了,原位置[0]) → 起风了跑到[6]（当前在[5]），
        #   曲目集合完全没变。
        #   但**后插的会排在前面**：当前=A，依次插 B、C → 结果是 A,C,B。
        #   所以一次插多首必然得到反序（实测踩过）。
        # 因此只保证**队头**在"下一首"：A 在播时插 B，等 B 自然播起来再插 C。
        # 顺序永远正确，每首只插一次。
        "enabled": True,
        # 队列空着（没在放任何歌）时，直接开始播放第一首点歌
        "play_if_idle": True,
    },
    "ncm_bridge": {
        # 网易云**播放队列**桥接（可选增强）。
        #
        # 为什么需要：实测"加入歌单"**不会**改变播放队列——队列只在客户端自己
        # 动作时才重建。所以只写歌单的话，主播还得手动去点播放。
        #
        # 原理：注入 AwooNcmCefBridge.dll 进 cloudmusic.exe，用 CEF DevTools
        # 在页面里执行 {cmd:'playingList',type:'addToNext',...}。
        # 本程序只走命名管道发命令，不做注入。
        # 前置条件：先跑 tools/inject_bridge.py，且桥自报 OK READY。
        "enabled": False,
        # 点歌入队后，把它插到播放队列的"下一首"（当前曲目之后）
        "insert_next": True,
        # 队列空着（没在放任何歌）时，直接开始播放第一首点歌
        "play_if_idle": True,
        # 管道响应超时（秒）
        "timeout": 3.0,
        # 桥可用性缓存秒数（避免每轮轮询都开管道）
        "probe_seconds": 30,
    },
    "playback": {
        # 点歌板"正在播放"以谁为准：
        #   netease —— 以网易云实际播放状态为准（推荐，主播手动放什么就显示什么）
        #   board   —— 以点歌板自己的队列为准
        "authority": "netease",
        # 网易云读不到时，回退到点歌板自己的顺序
        "fallback_to_board": True,
        # 曲名必须"严格匹配"才认可是同一首（避免翻唱误判，如 青花瓷 命中 刘芳版）
        "strict_match": True,
    },
    "update_check": {
        # 启动时去 GitHub 查一下有没有新版本，有就在控制台顶上提醒。
        #
        # 为什么要默认开：主播下的是 zip，没人会隔几天回仓库看有没有新版，
        # 而这个项目的坑（cookie 过期、房间短号收不到弹幕、脚本 BOM 乱码）
        # 大多是别人先撞上才修的 —— 提示一句"该更新了"能省掉一堆"我这是不是坏了"。
        #
        # ⚠️ 查更新**永远不会影响启动**：断网/超时/被限流一律静默当"不知道"，
        #    不报错、不重试、不卡住（见 songboard/version_check.py）。
        "enabled": True,
        # 隔多久再查一次（小时）。0 = 只在启动时查一次。
        # 一场直播动辄几小时，只在启动查的话播到一半发新版就看不见了。
        "interval_hours": 6,
        # 单次请求超时（秒）。查不到就当"不知道"，所以别设太大。
        "timeout": 5.0,
    },
    "gift_gate": {
        # 「送礼物才能点歌」门槛。默认**关闭**，不开时行为和以前完全一样。
        #
        # 为什么做成可配：门槛会随直播节奏变 —— 开播初期宽一点、人气高了收紧，
        # 所以规则由主播在控制台里调，不写死。
        "enabled": False,
        # 判定模式：
        #   min_total  —— 累计金额达到 min_coin 就能点（最常用）
        #   any_paid   —— 送过任意付费礼物就行，不看金额
        #   per_send   —— 每次点歌消耗 min_coin 额度，送礼物充值
        #   guard_only —— 只有舰长及以上能点
        "mode": "min_total",
        # 门槛金额，单位"瓜子"（1000 瓜子 = 1 元）。
        # 只对 min_total / per_send 有意义，any_paid 会忽略它。
        "min_coin": 1000,
        # 时效：送礼后多少秒内有效。0 = 永久有效。
        # 想防"刷一次点一整天"就设成正数（例：300 = 5 分钟）。
        "window_seconds": 0,
        # 是否只认付费礼物。⚠️ 建议保持 True ——
        # 银瓜子能送免费礼物，不过滤的话门槛形同虚设。
        "require_paid": True,
        # 舰长及以上是否直接放行（不受金额/时效限制）
        "guard_always_ok": True,
        # 舰长级别门槛：1=总督 2=提督 3=舰长（**数字越小级别越高**）
        "guard_min_level": 3,
        # 醒目留言（SC，本身是付费的）是否直接放行
        "sc_always_ok": True,
        # 若 sc_always_ok = false，SC 需要达到这个金额
        "sc_min_coin": 0,
    },
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data: dict[str, Any], path: Path) -> None:
        self._data = data
        self.path = path

    @classmethod
    def load(cls, path: Path) -> "Config":
        path = Path(path)
        raw: dict[str, Any] = {}
        if path.exists():
            # ⚠️ 用 utf-8-sig：记事本/某些编辑器默认存成「UTF-8 带 BOM」，
            # 文件开头多一个 \ufeff，json.loads 会直接报
            # "Unexpected UTF-8 BOM"，报错信息完全看不出是 BOM 的问题。
            text = path.read_text(encoding="utf-8-sig") or "{}"
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SystemExit(
                    f"{path} 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列"
                    f" {exc.msg}\n"
                    f"  常见原因：多了个逗号 / 少了引号 / 中文引号「」"
                    f" / 注释（jsonc 只是文档写法，真的 JSON 不支持注释）"
                ) from exc
            if not isinstance(raw, dict):
                raise SystemExit(f"{path} 的最外层必须是一个 {{ }} 对象")
        merged = _merge(DEFAULT_CONFIG, raw)
        cfg = cls(merged, path)
        if not path.exists():
            cfg.save()
        return cfg

    def save(self) -> None:
        # 统一写成不带 BOM 的 UTF-8，免得别的工具读到 BOM 再踩一次坑
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self._data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        """支持点号路径，与 get() 对称。

        ⚠️ 这里曾经只做 `self._data[key] = value`，于是

            cfg["queue.cooldown_seconds"] = 0      # 存成字面键 "queue.cooldown_seconds"
            cfg.get("queue.cooldown_seconds")      # 却去找 _data["queue"]["cooldown_seconds"]

        两边不对称，**写入静默失效**：读回来还是旧值/默认值。
        所有用点号写法设置嵌套配置的代码（测试脚本、集成检查）都因此
        在测一个根本没生效的配置，白白浪费排查时间。
        """
        if "." not in key:
            self._data[key] = value
            return
        parts = key.split(".")
        cur = self._data
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        cur[parts[-1]] = value

    def as_dict(self) -> dict[str, Any]:
        return self._data
