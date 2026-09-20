"""点歌板：弹幕监听 + 队列 + 网页叠加层 + 音乐驱动。

用法：
    python -m songboard --room 12345          # 连直播间
    python -m songboard                        # 按 config.json（默认演示模式）
    python -m songboard --room 12345 --open    # 同时打开控制台
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

from .bilibili import BilibiliDanmaku, DemoDanmaku
from .command import CommandParser
from .config import Config
from .extapi import ExtApiSource
from .giftgate import GiftLedger
from .media import MediaInfo, played_track_ids, read_now_playing, similarity
from .ncmbridge import NeteaseBridge
from .netease import build_driver, search_song
from .store import QueueStore
from .webui import BoardServer

ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "web"
DATA_DIR = ROOT / "data"

HELP = """
可用指令：
  回车            查看状态
  n / next        下一首（切歌）
  a 歌名          手动加入队列         例：a 稻香
  r 序号          移除队列里第 N 首
  top 序号        把第 N 首置顶
  clear           清空队列
  mode demo|live  切换演示模式 / 直播间模式
  room 房间号     设置直播间号并重连
  ne              查看网易云歌单驱动状态
  gift            查看点歌门槛设置
  gift on|off     开关「送礼物才能点」
  gift min 2000   门槛改成 2000 瓜子
  gift list       看谁送了多少、谁有资格
  h / help        显示本帮助
  q / quit        退出
"""


class App:
    def __init__(self, cfg: Config, *, persist: bool = True) -> None:
        self.cfg = cfg
        self.loop = asyncio.get_event_loop()
        self.parser = CommandParser(cfg)
        self.store = QueueStore(cfg, DATA_DIR, persist=persist)
        if persist:
            self.store.load_persisted()
        self.driver = build_driver(cfg)
        # 礼物门槛：只有送过礼物的观众才能点歌。规则由主播在控制台里配。
        # 默认关闭，不开时行为和以前完全一样。
        self.gifts = GiftLedger(cfg)
        # 播放队列桥（可选）：点歌后直接插进网易云的播放队列。
        # 不可用时全部静默降级，不影响点歌板本身。
        self.bridge = NeteaseBridge(
            enabled=bool(cfg.get("ncm_bridge.enabled", False)),
            timeout=float(cfg.get("ncm_bridge.timeout", 3.0) or 3.0),
        )

        # ⚠️ 只插播放队列模式下，强制关掉"写歌单"，并把它写回配置。
        # 这是我们唯一能保证"主播的歌单一个字节都不动"的地方：
        # 少写一处判断就可能漏掉一条写歌单的路径（实测踩过好几次）。
        # 想做这件事是因为配置可能被手改、或旧版本残留了 auto_add=true。
        if cfg.get("queue_only.enabled", False):
            if cfg.get("netease.auto_add", False) or \
                    cfg.get("netease.write_playlist", True):
                cfg["netease"]["auto_add"] = False
                cfg["netease"]["write_playlist"] = False
                try:
                    cfg.save()
                except Exception:
                    pass
        self.listener: Any = None
        self.server: BoardServer | None = None
        self.log_lines: list[dict[str, Any]] = []
        self.reply_queue: list[str] = []   # 计划回给弹幕的提示（P4 接弹幕发送）
        self._mode = str(cfg.get("mode", "demo"))
        self._status_task: asyncio.Task | None = None
        # 正在播放检测（自动下一首）
        self.media: MediaInfo | None = None        # 曲目来源（一定带标题）
        self.timeline: MediaInfo | None = None     # 进度来源（可能没标题，可能是视频）
        self.extapi = ExtApiSource(cfg)            # 外部精确进度源（可选）
        self._media_error = ""
        self._media_error_logged = ""
        self._last_align_msg = ""
        # 歌单写入（填到上限）的状态
        self._playlist_busy = False
        self._head_fail_id = ""
        self._head_fail_at = 0.0
        self._playlist_dirty = False
        # 「只插播放队列」模式的队头记录：用于判断队头是否变过
        self._queue_head_id = ""
        # 已经插进过播放队列的点歌条目 id（防止回插、保证顺序稳定）
        self._queue_inserted: set[str] = set()
        # 已确认在网易云里开始播放的点歌条目 id
        self._queue_playing_id = ""
        # 已经排在"下一首"、但**还没开始播**的那首的 id（同时只允许一首）
        self._queue_placed_id = ""
        self._media_key = ""
        self._media_since = 0.0
        self._media_seen_at = 0.0
        self._track_task: asyncio.Task | None = None

    # ---------- 日志与广播 ----------
    def log(self, text: str) -> None:
        entry = {"ts": time.time(), "text": text}
        self.log_lines.append(entry)
        print(f"[songboard] {text}")
        if self.server:
            self.server.broadcast({"event": "log", "log": entry})

    async def _notify(self, payload: dict[str, Any]) -> None:
        """订阅 QueueStore 的事件，转发给网页客户端，并生成弹幕回复文案。"""
        if self.server:
            self.server.broadcast(payload)
        event = payload.get("event")
        item = payload.get("item") or {}
        if event == "playing" and item:
            self.log(f"▶ 正在播放《{item.get('song')}》（{item.get('user')} 点）")
            self.driver_task(item)
            # 播放推进了 → 队头变了，可能要写下一首
            self._playlist_dirty = True
        elif event == "added" and item:
            self.log(f"＋ 排队《{item.get('song')}》by {item.get('user')}")
            # 队列变了 → 可能要推进队头
            self._playlist_dirty = True
        elif event == "pending_added" and item:
            self.log(f"队列已满，《{item.get('song')}》进入备选池")
            self._playlist_dirty = True

    def driver_task(self, item: dict[str, Any]) -> None:
        """切到新歌时：查时长（让自动切歌更准）。写歌单由 _sync_playlist 负责。"""
        song = str(item.get("song", ""))
        item_id = str(item.get("id", ""))

        async def run() -> None:
            # 时长：查到了就能让自动切歌更准（只读，不需要写歌单权限）
            try:
                secs = await self.driver.song_duration(song)
            except Exception:
                secs = 0.0
            if secs > 0:
                for s in self.store.active():
                    if s.id == item_id:
                        s.duration = secs
                        self.log(f"⏱ 《{song}》时长 {secs/60:.1f} 分钟，自动切歌按此计时")
                        break
            else:
                cur = self.store.current
                if cur is not None and cur.id == item_id:
                    fb = float(self.cfg.get("media.duration_fallback", 300) or 300)
                    self.log(f"⏱ 《{song}》查不到时长，按兜底 {fb/60:.1f} 分钟计时")

            # ⚠️ 写歌单不在这里做，由 _sync_playlist 负责（只推队头一首）。
            # 否则直接开始播放的那首会绕过入队事件，或者被加两次。

        self.loop.create_task(run())

    # ---------- 歌单写入：只推队头 ----------
    async def _sync_queue_only(self) -> None:
        """「只插播放队列」模式：绝不碰歌单，只保证**队头**在"下一首"。

        为什么只保队头：
          `addToNext` 只能把歌放到"下一首"，**后插的会把先插的挤到后面**。
          实测一次插 4 首得到的是反序：
              点歌顺序 苦瓜 → 单车 → 红玫瑰 → 阴天快乐
              队列结果 阴天快乐 → 红玫瑰 → 单车 → 苦瓜
          要拿到正序，要么每次把整排倒着重插一遍（O(N²) 调用），
          要么只保住队头、等它播完再插下一首（O(1)，顺序永远对）。
          这里选后者。

        自愈：每轮都看一眼 NetEase 报的"下一首"，和队头不一致就补插。
        所以就算主播手动切歌、或程序重启，顺序也会被拉回来。
        """
        if not self.cfg.get("queue_only.enabled", False):
            return
        if not self.bridge.enabled or self._playlist_busy:
            return

        # ── 队头判定 ──────────────────────────────────────────────
        # 关于 `addToNext` 的真实语义（对照实验确认，别再搞错）：
        #   它把目标曲目**插入到"当前播放曲目之后"**，原有队列完整保留，
        #   不会挤掉谁。
        #   实测：ADD_NEXT(稻香, 原位置[0]) → 稻香跑到 [6]（当前曲目在 [5]），
        #         曲目集合完全没变。
        #
        # 那为什么还要"一次只插一首"？因为**顺序会反**：
        #   当前=A，依次 ADD_NEXT(B)、ADD_NEXT(C) → 结果是 A, C, B
        #   （后插的排在前面）。一次插多首必然得到反序。
        #
        # 正确做法（主播明确要求的流程）：
        #   A 在播时把 B 插到"下一首" → 等 B 自然播起来 → 再插 C。
        #   于是永远只有一个"已插好、等它播"的占位，顺序天然正确。
        #
        # ⚠️ 判定"有没有开始放"必须用**网易云报告的当前曲目**，
        #    绝不能用点歌板的 store.current —— 那个会被 _align_to_netease
        #    对齐逻辑改来改去（新点歌一入队就可能被指成 current），
        #    用它当依据会让闸门误判"已在播放"从而放行，把顺序搞乱。
        #    （实测就是这么错的：第二首一入队就被当成"在播"，于是去插了第三首。）

        # 队头 = 第一个还没插过的点歌（waiting/playing 都算，先点在前）
        head = None
        for s in self.store.played:
            if (s.state.value in ("waiting", "playing")
                    and s.id not in self._queue_inserted):
                head = s
                break

        if head is None:
            # 没有待处理的了 → 清空记录，等下一次点歌重新开始
            if not [s for s in self.store.played
                    if s.state.value in ("waiting", "playing")]:
                self._queue_inserted.clear()
                self._queue_playing_id = ""
                self._queue_placed_id = ""
            return

        if not await asyncio.to_thread(self.bridge.available):
            return

        # 网易云现在到底在放哪首？用它来判断队头有没有真的开始播。
        state = await asyncio.to_thread(self.bridge.now_playing)
        if state is None:
            # ⚠️ 读不到播放器状态时**必须直接放弃这一轮**，不能往下走。
            # 否则 `next_track_id` 会是空字符串 → 闸门误判成"下一首位置空着"
            # → 放行插入。这会在"已经有一首排队等播"的情况下又插一首，
            # 破坏"一次只插一首"的不变量，顺序就乱了。
            # （实测日志里出现过：两首插入只隔 4 秒，中间那首还没播。）
            # 下一轮状态恢复后会自愈，所以这里跳过是安全的。
            return

        playing_tid = str(state.get("track_id") or "")
        if playing_tid:
            for s in self.store.played:
                if s.netease_id and str(s.netease_id) == playing_tid:
                    self._queue_playing_id = s.id
                    break

        # ⚠️ 关键闸门（不变量）：**同一时间只允许一首"我们插的、还没开始播"的歌。**
        #
        # 为什么需要：`addToNext` 是"移到下一首"，不是"追加"。
        # 连插两首会得到反序（当前=A，插 B 再插 C → A,C,B），所以必须一次只插一首，
        # 等它真正开始播，才轮到下一首。
        #
        # ⚠️ 但判定**必须看播放队列的真实状态**，不能只看内存里的
        #    `_queue_placed_id`。踩过的坑：队列里本来就有别的歌占着"下一首"
        #    （比如上一轮点歌插进去、还没播的那首），只看内存标志的话
        #    程序会误以为"已经排好了"，于是**永远不再插新歌**——
        #    实测就是这样卡死的：点了 5 首，只有第 1 首进队列，后面 4 首一直等。
        #
        # 正确判定：看网易云现在报的"下一首"，是不是**我们插过的某首点歌**。
        #   * 是  → 那首在排队等播，什么都不做
        #   * 不是 → 下一首位置是别人（或空的），队头可以插进去
        next_tid = str((state or {}).get("next_track_id") or "")
        next_is_ours = False
        if next_tid:
            for s in self.store.played:
                if s.id in self._queue_inserted and s.netease_id \
                        and str(s.netease_id) == next_tid:
                    next_is_ours = True
                    break
        if next_is_ours:
            return
        # 下一首不是我们的（说明我们插的那首已经播掉、或被切走了）→ 解除占用
        self._queue_placed_id = ""
        # 诊断用：把闸门当时看到的输入记下来，插队日志里会带上。
        # 顺序出问题时，这条能直接说明"当时下一首是谁、我们插过谁"。
        gate_note = (f"放={playing_tid or '-'} 下={next_tid or '-'} "
                     f"已插{len(self._queue_inserted)}首")

        # 队头变了才重新计算；否则每轮只做一次廉价的"下一首"核对
        head_changed = self._queue_head_id != head.id
        self._queue_head_id = head.id

        self._playlist_busy = True

        async def run() -> None:
            try:
                # 还没搜到网易云 id 的，先搜（只读接口，不动歌单）
                if not head.netease_id:
                    cookie = str(getattr(self.driver, "cookie", "") or "")
                    if not cookie:
                        # 没有登录态就没法搜歌。这不是异常，
                        # 只是"这条路现在走不通"，说清楚即可，
                        # 不要每 2 秒刷一条 AttributeError 那样的报错。
                        self.log("⚠️ 没有网易云登录态（cookie），"
                                 "无法搜索歌曲；请在配置里填 cookie")
                        return
                    try:
                        hits = await asyncio.to_thread(
                            search_song, head.song, cookie, 5)
                    except Exception as exc:  # noqa: BLE001
                        self.log(f"⚠️ 搜索《{head.song}》失败：{exc!r}")
                        return
                    if not hits:
                        self.log(f"⚠️ 网易云搜不到《{head.song}》，"
                                 f"跳过这首，等下一首")
                        return
                    pick = hits[0]
                    self.store.set_netease(head.id, int(pick["id"]),
                                           str(pick.get("name") or ""))
                    if str(pick.get("name") or "") != head.song:
                        self.log(f"ℹ️ 「{head.song}」实际命中"
                                 f"《{pick['name']}》"
                                 f"{' - ' + str(pick.get('artists')) if pick.get('artists') else ''}")

                sid = int(head.netease_id)

                # 队列空着（没在放歌）→ 直接起播第一首
                if bool(self.cfg.get("queue_only.play_if_idle", True)):
                    state = await asyncio.to_thread(self.bridge.now_playing)
                    if state is None or not state.get("track_id"):
                        ok, msg = await asyncio.to_thread(
                            self.bridge.play_now, sid)
                        if ok:
                            self._queue_inserted.add(head.id)
                            # 已在播放 = 立刻开始播，占位直接解除
                            self._queue_playing_id = head.id
                            self.log(f"▶️ 队列空着，直接播放"
                                     f"《{head.song}》（{head.user} 点）")
                        else:
                            self.log(f"⚠️ 播放《{head.song}》失败：{msg}")
                        return

                # 保证队头是"下一首"（已经在就不重复插）
                ok, msg, action = await asyncio.to_thread(
                    self.bridge.ensure_next, sid)
                if not ok:
                    self.log(f"⚠️ 插队失败《{head.song}》：{msg}")
                    return
                # 标记为"已插过"，并**占用"下一首"这个位置**：
                # 在它真的开始播之前，不会再插第二首（否则会把它顶掉）。
                self._queue_inserted.add(head.id)
                self._queue_placed_id = head.id
                if action == "inserted":
                    # 带上闸门当时的输入，顺序出问题时一眼能看出原因
                    self.log(f"⚡ 队列：《{head.song}》已排在下一首"
                             f"（{head.user} 点）[{gate_note}]")
                elif action == "already":
                    # ⚠️ 这里必须校验一下"下一首"到底是不是这首歌。
                    # 两首不同的点歌完全可能搜到**同一个**网易云曲目
                    # （比如"十年"和"陈奕迅 十年"），那样就会误判成
                    # "已经是下一首"，队头永远推不动、后面的歌全被卡住。
                    if head_changed:
                        nxt = (self.bridge.now_playing() or {}).get("next_name") or ""
                        if nxt and str(nxt) != str(head.netease_name or head.song):
                            self.log(f"⚠️ 队头《{head.song}》与下一首"
                                     f"《{nxt}》不一致：可能有多个点歌"
                                     f"搜到了同一首，先跳过")
                    else:
                        self.log(f"⚡ 队列：《{head.song}》已在下一首")
            except Exception as exc:  # noqa: BLE001
                self.log(f"⚠️ 插队过程异常（已忽略）：{exc!r}")
            finally:
                self._playlist_busy = False

        self.loop.create_task(run())

    async def _sync_playlist(self) -> None:
        """把待播队列里的歌写进网易云歌单，并保证两件事：

          1. 新歌加到**末尾**（网易云默认插第 1 位，这里用"删除+倒序重加"纠正）
          2. 歌单总长不超过 max_tracks，超了先清已播的

        填法：**把歌单填到上限为止**（而不只是推队头一首）。
        因为主播可能没有在放歌，如果只推一首、等它播完再推下一首，
        歌单就永远填不满，上限也失去意义。
        """
        if not self.cfg.get("netease.auto_add", False):
            return
        if not self.cfg.get("netease.enabled", False):
            return
        # ⚠️ 硬闸门：只插播放队列模式 / 显式禁止写歌单 → 绝不碰歌单。
        # 这两条必须在这里，不能只靠调用方判断——写歌单是最不可逆的操作。
        if self.cfg.get("queue_only.enabled", False):
            return
        if not self.cfg.get("netease.write_playlist", True):
            return

        # ⚠️ 同样不能写 `int(x or 5)`：会把 0 变成 5，让"设 0 关掉上限"失效。
        _raw_limit = self.cfg.get("netease.max_tracks", 5)
        limit = 5 if _raw_limit is None else int(_raw_limit)
        if self._playlist_busy:
            return

        # 待播（还没写进歌单的），按点歌顺序
        # ⚠️ 不能只看 waiting！
        # store.add() 在"队列为空时点的第一首"会直接把它标成 PLAYING
        # （store.py：空闲时点的第一首直接成为"正在播放"）。那首歌同样没有
        # netease_id，如果把它排除在外，它就**永远不会被写进歌单**，
        # 也就永远不会被插进播放队列 —— 实测就是这样，点歌后什么都没发生。
        cur = self.store.current
        cur_unwritten = (
            cur is not None
            and not cur.netease_id
            and cur.state.value in ("playing", "waiting")
        )

        pending = [s for s in self.store.played
                   if s.state.value == "waiting" and not s.netease_id]
        if not pending and not cur_unwritten:
            await self._prune_playlist()
            return

        # 失败退避：同一首连续失败就先别急着重试
        head = pending[0] if pending else cur
        if head is not None and (time.time() - self._head_fail_at) < 30 \
                and self._head_fail_id == head.id:
            return

        self._playlist_busy = True

        async def run() -> None:
            try:
                # 先看歌单当前几首，决定还能塞几首
                try:
                    current = await asyncio.to_thread(self.driver.playlist_state)
                except Exception as exc:
                    self.log(f"⚠️ 读歌单失败，跳过本轮写入：{exc!r}")
                    return
                room = max(0, limit - len(current))
                if room <= 0:
                    await self._prune_playlist()
                    return

                # (0) 正在播放、但还没进歌单的那首：它已经在放了，所以
                #     **不能去改正在播放的曲目**，只把它写进歌单并插到"下一首"。
                if cur_unwritten and cur is not None:
                    # ⚠️ add_song 是 **async** 方法，必须直接 await。
                    # 丢进 asyncio.to_thread 只会拿到一个协程，解包即 TypeError
                    # （实测就是这样炸的，而且异常发生在后台 task 里，
                    #  外面只看到"什么都没发生"，很难发现）。
                    ok, msg, hit = await self.driver.add_song(cur.song, cur.user)
                    if not ok:
                        self.driver.last_error = msg
                        self._head_fail_id = cur.id
                        self._head_fail_at = time.time()
                        self.log(f"⚠️ 网易云（当前播放）：{msg}")
                    elif hit and hit.get("id"):
                        self.store.set_netease(cur.id, int(hit["id"]),
                                               str(hit.get("name") or ""))
                        self.log(f"✅ 网易云：正在播放的《{cur.song}》"
                                 f"也已加入歌单")
                        if self.cfg.get("ncm_bridge.enabled", False):
                            # 插到"下一首"而不是抢播：它本来就在放了，
                            # 插一次是幂等的，只是让队列顺序对得上。
                            await self._queue_via_bridge(cur, int(hit["id"]))
                        room = max(0, room - 1)

                batch = pending[:room]
                # 只为"正在播放那首"跑了一轮时 batch 可能是空的，别打无意义的日志
                if batch:
                    self.log(f"📥 往歌单写入 {len(batch)} 首"
                             f"（当前 {len(current)}/{limit} 首）")
                for s in batch:
                    ok, msg, hit = await self.driver.append_song(s.song, s.user)
                    if not ok:
                        self.driver.last_error = msg
                        self._head_fail_id = s.id
                        self._head_fail_at = time.time()
                        self.log(f"⚠️ 网易云：{msg}")
                        break
                    if hit and hit.get("id"):
                        self.store.set_netease(s.id, int(hit["id"]),
                                               str(hit.get("name") or ""))
                    if hit and hit.get("name") and str(hit["name"]) != s.song:
                        self.log(f"ℹ️ 「{s.song}」实际命中《{hit['name']}》"
                                 f"{' - ' + str(hit.get('artists')) if hit.get('artists') else ''}"
                                 f"（可能不是原版）")
                    self.log(f"✅ 网易云：已加入歌单：{s.song} → 已排在末尾")

                    # 写歌单**不会**改变播放队列，所以再走桥把这首插到"下一首"。
                    # 桥不可用时静默跳过，不影响上面写歌单的结果。
                    if hit and hit.get("id") and self.cfg.get(
                            "ncm_bridge.enabled", False):
                        await self._queue_via_bridge(s, int(hit["id"]))
                self._head_fail_id = ""
                await self._prune_playlist()
            except Exception as exc:  # noqa: BLE001
                # ⚠️ 必须自己兜住：这是 create_task 起的后台任务，
                # 异常不会有人 await 到，只会变成"什么都没发生"的静默失败。
                # （实测：这里曾因 add_song 被误用 asyncio.to_thread 抛
                #  TypeError，外部完全看不出来。）
                self.log(f"⚠️ 写歌单过程异常（已忽略，不影响点歌）：{exc!r}")
            finally:
                self._playlist_busy = False

        self.loop.create_task(run())

    async def _prune_playlist(self) -> None:
        """清理歌单里的**已播**歌曲，并保证总长不超上限。

        ⚠️ 这里曾经有个死锁 bug：判定写成了 `len(tracks) <= limit` 就返回，
        而"歌单正好 5 首、上限也是 5"时 `5 <= 5` 为真 → **永远不清理**。
        后果是歌单满了以后再也塞不进新点歌，新歌一直卡在 waiting。
        （实测：歌单 5/5、其中 3 首已播，点歌后歌单和播放队列都毫无变化。）

        正确语义（主播当初的要求："达到上限时自动清除已播放的歌，
        直到只剩上限首数"）：
          ⚠️ 关键在"**达到上限时**"。清理的目的是**给新点歌腾位置**，
             绝不是定期把已播歌删掉。所以：
               * 有已播的 **且** 确实还有待写入的歌 → 删掉已播的腾位置
               * 没有待写入的歌 → **什么都不做**（否则会把你想重播的歌也删了，
                 实测就是这样：服务启动 2 秒、还没有任何点歌时，
                 就把 3 首已播歌删了）
          1. **已播的**歌删掉（它们已经放过了，留着只是占坑）
          2. 还不够就再从最旧的未播歌开始删，压到上限

        上限设为 0 或负数 = 不限制（不清理）。
        """
        # ⚠️ 硬闸门：只插播放队列模式下，绝不删歌单里的任何东西。
        if self.cfg.get("queue_only.enabled", False):
            return
        if not self.cfg.get("netease.write_playlist", True):
            return
        # ⚠️ 别写成 `int(x or 5)`：那会把 0 也变成 5，导致"设 0 关掉上限"失效。
        raw_limit = self.cfg.get("netease.max_tracks", 5)
        limit = 5 if raw_limit is None else int(raw_limit)
        if limit <= 0:
            return

        # ⚠️ 没有待写入的歌时绝不清理。这一步是"腾位置"，
        # 不是"定期打扫"——否则你歌单里的歌会被无声删掉。
        try:
            if not self.store.has_pending_work():
                return
        except Exception:
            return

        async def run() -> None:
            try:
                tracks = await asyncio.to_thread(self.driver.playlist_state)
            except Exception as exc:
                self.log(f"⚠️ 读歌单失败，跳过清理：{exc!r}")
                return

            # 已播集合：客户端队列缓存的 isPlayedOnce + 点歌板自己记录的已播
            played_ids: set[int] = set()
            if self.cfg.get("netease.prune_played", True):
                try:
                    played_ids = await asyncio.to_thread(played_track_ids)
                except Exception:
                    played_ids = set()

            names_played = {s.song for s in self.store.played
                            if s.state.value in ("played", "skipped")}

            def is_played(tid: int, name: str) -> bool:
                if tid in played_ids:
                    return True
                return bool(name) and name in names_played

            remaining = len(tracks)
            removed: list[str] = []

            # 1) 已播的一律删掉（只要有，就腾位置）
            for tid, name in tracks:
                if is_played(tid, name):
                    removed.append(str(tid))
                    remaining -= 1

            # 2) 删完已播仍然超上限 → 再从最旧的未播歌开始删
            if remaining > limit:
                for tid, name in tracks:
                    if remaining <= limit:
                        break
                    if str(tid) in removed:
                        continue
                    removed.append(str(tid))
                    remaining -= 1
                    self.log(f"ℹ️ 歌单超上限，删除最早的未播歌《{name}》")

            if not removed:
                return
            ok, msg = await asyncio.to_thread(self.driver.delete_tracks,
                                             [int(x) for x in removed])
            if ok:
                self.log(f"🧹 歌单清理：{msg}（上限 {limit} 首，"
                         f"腾出 {len(removed)} 个位置）")
            else:
                self.log(f"⚠️ 歌单清理失败：{msg}")

        self.loop.create_task(run())

    async def _queue_via_bridge(self, item: Any, netease_id: int) -> None:
        """把一首已加入歌单的歌，再插进网易云的**播放队列**下一首。

        为什么单独做这一步：实测"加入歌单"不改变播放队列（队列只在客户端自己
        动作时重建）。不插队列的话，主播必须手动去歌单里点，点歌板就只是块看板。

        两种情况：
          * 有计划在放歌 → 插到"下一首"（addToNext，不改动歌单内容）
          * 队列空着没在放 → 直接开始播放这一首
        """
        try:
            if not await asyncio.to_thread(self.bridge.available):
                self.log("ℹ️ 播放队列桥不可用，只写歌单（需手动点播放）")
                return

            # 先看网易云到底在不在放歌
            state = await asyncio.to_thread(self.bridge.now_playing)
            idle = state is None or not state.get("track_id")

            if idle and bool(self.cfg.get("ncm_bridge.play_if_idle", True)):
                ok, msg = await asyncio.to_thread(
                    self.bridge.play_now, netease_id)
                if ok:
                    self.log(f"▶️ 播放队列桥：队列空着，直接播放《{item.song}》")
                else:
                    self.log(f"⚠️ 播放队列桥：{msg}")
                return

            if not bool(self.cfg.get("ncm_bridge.insert_next", True)):
                return
            ok, msg = await asyncio.to_thread(
                self.bridge.insert_next, netease_id)
            if ok:
                self.log(f"⚡ 播放队列桥：《{item.song}》已插到下一首")
            else:
                self.log(f"⚠️ 播放队列桥：{msg}")
        except Exception as exc:  # noqa: BLE001 - 桥永远不该拖垮点歌流程
            self.log(f"⚠️ 播放队列桥异常（已忽略）：{exc!r}")

    async def _sync_playlist_order(self) -> None:
        """按点歌顺序重排歌单里"还没播"的歌。

        只在 netease.order_playlist = true 时启用；默认关，
        因为"只推队头"已经保证顺序正确，不需要再动歌单。
        """
        if not self.cfg.get("netease.order_playlist", False):
            return
        # 只插播放队列模式下不重排歌单
        if self.cfg.get("queue_only.enabled", False):
            return
        if not self.cfg.get("netease.write_playlist", True):
            return
        if not self.cfg.get("netease.auto_add", False):
            return
        if not self.cfg.get("netease.enabled", False):
            return
        pairs = self.store.queued_netease_ids()
        if len(pairs) < 2:
            return
        desired = [tid for _id, tid in pairs]

        async def do() -> None:
            try:
                ok, msg = await self.driver.reorder_playlist(desired)
            except Exception as exc:
                self.log(f"⚠️ 歌单排序失败（不影响点歌）：{exc!r}")
                return
            if ok and "无需调整" not in msg:
                self.log(f"🔀 歌单顺序已按点歌顺序重排：{msg}")
            elif not ok:
                self.log(f"⚠️ 歌单排序未生效：{msg}")

        self.loop.create_task(do())

    # ---------- 弹幕处理 ----------
    async def on_danmaku(self, ev: dict[str, Any]) -> None:
        text = str(ev.get("text") or "")
        user = str(ev.get("user") or "未知")
        uid = int(ev.get("uid") or 0)
        kind = str(ev.get("type") or "")

        # ---------- 先把"付过费"的事件记进礼物账本 ----------
        # ⚠️ 必须在这里记账，不能只在点歌时查 —— 送礼和点歌是两条独立的消息，
        #    观众可能先送礼再隔几分钟点歌。
        if kind == "gift":
            c = self.gifts.record_gift(ev)
            g = ev.get("gift") or {}
            if c is not None and not bool(g.get("paid", True)) and self.gifts.require_paid:
                self.log(f"🎁 {user} 送了免费礼物《{g.get('name')}》"
                         f"（不计入门槛）")
            elif c is not None:
                self.log(f"🎁 {user} 送了《{g.get('name')}》x{g.get('num')} "
                         f"= {g.get('total_coin')} 瓜子"
                         f"（累计 {c.total_coin}）")
            return
        if kind == "guard":
            c = self.gifts.record_guard(ev)
            gd = ev.get("guard") or {}
            names = {1: "总督", 2: "提督", 3: "舰长"}
            if c is not None:
                self.log(f"👑 {user} 开通了"
                         f"{names.get(int(gd.get('level') or 0), '舰长')}")
            return

        if kind == "super_chat":
            self.gifts.record_super_chat(ev)
            if text:
                await self.store.add(text, user, uid, source="super_chat", force=True)
            return

        cmd = self.parser.parse(text)
        if cmd.action == "none":
            return
        # 双保险：对应关键词列表为空时，该指令视为关闭
        if cmd.action == "skip" and not self.cfg.get("danmaku.skip_keywords"):
            return
        if cmd.action == "query" and not self.cfg.get("danmaku.query_keywords"):
            return
        if cmd.action == "cancel" and not self.cfg.get("danmaku.cancel_keywords"):
            return
        if cmd.action == "skip":
            item = await self.store.next(reason="skip")
            self.log(f"⏭ {user} 触发了切歌" + (f"，切到《{item.song}》" if item else "，队列已空"))
            self.reply_queue.append(f"@{user} 已切歌")
            return
        if cmd.action == "query":
            n = self.store.user_waiting_count(user, uid)
            self.log(f"❓ {user} 查询点歌：{n} 首在队列")
            self.reply_queue.append(f"@{user} 你目前有 {n} 首在队列里")
            return
        if cmd.action == "cancel":
            mine = [s for s in self.store.active()
                    if s.user == user and s.state.value == "waiting"]
            if not mine:
                self.reply_queue.append(f"@{user} 没有找到你的点歌")
                return
            await self.store.remove(mine[-1].id)
            self.log(f"↩ {user} 取消了《{mine[-1].song}》")
            return
        if cmd.action == "add":
            if not cmd.song:
                self.reply_queue.append(f"@{user} 请在「点歌」后面写歌名")
                return
            # ---------- 礼物门槛 ----------
            # 门槛未启用时 check() 直接放行，行为和以前完全一样。
            decision = self.gifts.check(uid)
            if not decision.ok:
                # 日志里带上原因和当前累计，方便主播判断门槛是不是定得太严
                contrib = decision.contribution
                extra = f"（累计 {contrib.total_coin} 瓜子）" if contrib else "（无记录）"
                self.log(f"🚫 {user} 点《{cmd.song}》被门槛拦下："
                         f"{decision.reason}{extra}")
                self.reply_queue.append(f"@{user} {decision.hint}")
                return
            try:
                req, result = await self.store.add(cmd.song, user, uid)
            except ValueError:
                return
            # 按次消耗模式：点歌成功才扣额度
            if result == "ok" and self.gifts.enabled \
                    and self.gifts.mode == "per_send":
                self.gifts.spend(uid, self.gifts.min_coin)
                self.log(f"💸 {user} 消耗 {self.gifts.min_coin} 瓜子额度")
            hints = {
                "ok": f"@{user} 《{req.song}》已加入队列（第 {len(self.store.active())} 位）",
                "duplicate": f"@{user} 《{req.song}》已经在队列里啦",
                "cooldown": f"@{user} 点歌太快啦，休息一下再来",
                "user_limit": f"@{user} 你已经有歌在队列里了，等这首放完吧",
                "queue_full": f"@{user} 队列满啦，《{req.song}》先放进备选",
            }
            self.reply_queue.append(hints.get(result, ""))
            if result not in ("ok",):
                self.log(f"· {user} 点《{cmd.song}》被拦下（{result}）")

    # ---------- 模式 ----------
    def stop_listener(self) -> None:
        if self.listener is not None:
            self.listener.stop()

    async def start_listener(self, mode: str, room_id: int | None = None) -> None:
        self.stop_listener()
        await asyncio.sleep(0.05)
        if mode == "demo":
            self.listener = DemoDanmaku(self.on_danmaku, self._notice)
        else:
            rid = int(room_id or self.cfg.get("room_id", 0) or 0)
            if not rid:
                self.log("没有房间号：请在控制台填直播间号，或先用演示模式")
                self.listener = DemoDanmaku(self.on_danmaku, self._notice)
                self._mode = "demo"
                return
            self.listener = BilibiliDanmaku(
                rid, self.on_danmaku, self._notice,
                cookie=str(self.cfg.get("bilibili.cookie", "") or ""),
            )
        self._mode = mode
        asyncio.create_task(self.listener.run())
        self.log(f"监听已启动：{'演示模式' if mode == 'demo' else f'直播间 {room_id or self.cfg.get('room_id')}'}")

    async def _notice(self, msg: str) -> None:
        self.log(msg)

    # ---------- 正在播放检测 / 自动下一首 ----------
    async def _track_loop(self) -> None:
        """轮询"现在在放什么"，据此判断点歌板的当前这首是否播完。

        只用只读信号：
          · 系统媒体会话（GSMTC）—— Edge/Chrome/Spotify 等，带进度条
          · 网易云客户端窗口标题（class=OrpheusBrowserHost）—— 只有歌名
        """
        poll = float(self.cfg.get("media.poll_seconds", 2) or 2)
        authority = str(self.cfg.get("playback.authority", "netease") or "netease")
        if authority == "netease":
            self.log("播放状态以【网易云实际播放】为准：你在网易云放什么，点歌板就显示什么"
                     "（不会自动推进队列）")
        else:
            self.log("自动下一首已启用：正在监视播放器（只读窗口标题/系统媒体会话）")
        while True:
            await asyncio.sleep(poll)
            try:
                await self._poll_media()
                self._media_error = ""
            except Exception as exc:
                # 不能只写日志：轮询里出错会让"自动切歌"静默失效，
                # 所以记到状态里，控制台会显示出来。
                self._media_error = f"{type(exc).__name__}: {exc}"
                if self._media_error != self._media_error_logged:
                    self._media_error_logged = self._media_error
                    self.log(f"⚠️ 播放检测出错（自动切歌本轮失效）：{self._media_error}")

    async def _poll_media(self) -> None:
        prefer = tuple(self.cfg.get("media.prefer_apps", []) or ())

        # 1) 外部精确进度源优先：它自带歌名 + 进度，最完整
        ext = await self.extapi.poll()
        if ext is not None and ext.title:
            self._apply_media(ext)
            await self._align_to_netease(ext)
            await self._maybe_auto_advance()
            return

        # 2) 回退：窗口标题（歌名）+ 系统媒体会话（进度）
        title_info, timeline = await asyncio.to_thread(read_now_playing, prefer)
        self.timeline = timeline
        if title_info is None or not title_info.title:
            self.media = None
            return
        self._apply_media(title_info)
        await self._align_to_netease(title_info)
        await self._maybe_auto_advance()

    def _apply_media(self, info: MediaInfo) -> None:
        """记录当前曲目；只有"换歌了"才触发换歌处理。"""
        self.media = info
        self._media_seen_at = time.time()
        key = f"{info.source}|{info.key()}"
        if key != self._media_key:
            self._media_key = key
            self._media_since = time.time()

    async def _align_to_netease(self, info: MediaInfo) -> None:
        """把点歌板的"正在播放"对齐到网易云实际播放。

        ⚠️ 必须每轮都做，不能只在"换歌"时做：
        网易云一直在放同一首歌（或主播手动切到队列里的另一首）时，
        没有"变化事件"，只靠事件驱动的话点歌板会一直停在旧状态。
        """
        if str(self.cfg.get("playback.authority", "netease") or "netease") != "netease":
            return
        if not self.store.active():
            return
        strict = bool(self.cfg.get("playback.strict_match", True))
        item, why = await self.store.set_current_by_title(
            info.title, artist=info.artist, strict=strict,
        )
        if item is None:
            return
        changed = self.store.current is not None and self.store.current.id == item.id
        # 只在"当前指向真的变了"时打日志，否则每 2 秒刷屏
        if changed and why != "已经是当前曲目" and why != self._last_align_msg:
            self._last_align_msg = why
            self.log(f"🎵 点歌板已对齐网易云：正在放《{info.title}》→ {why}")

    async def _maybe_auto_advance(self) -> None:
        """判断要不要自动切下一首。

        ⚠️ 在 playback.authority = netease 模式下**不做任何自动切歌**：
        既然播放状态以网易云为准，队列就该由主播手动放来推进，
        程序只在每一轮轮询时把点歌板的显示对齐（见 _align_to_netease）。
        否则会出现"程序以为播完了、把队列推进了，但网易云其实还在放"的错位。
        """
        if str(self.cfg.get("playback.authority", "netease") or "netease") == "netease":
            return
        if not self.cfg.get("media.auto_next", True):
            return
        if self.store.current is None:
            return
        fallback = float(self.cfg.get("media.duration_fallback", 300) or 300)
        grace = float(self.cfg.get("media.grace_seconds", 9) or 9)
        almost = float(self.cfg.get("media.almost_done_seconds", 3) or 3)

        # (a) 播放器进度快到底。这里必须非常小心：
        #     浏览器媒体会话常常没有标题，分不清是"网易云网页版"还是"B站视频"，
        #     直接采信会导致看视频时把点歌队列切乱。所以默认只在
        #     ①配置显式开启 trust_browser_progress，或
        #     ②会话自带标题且与当前歌匹配 时，才采信进度。
        cur = self.store.current
        remaining = None
        tl = self.timeline
        if tl is not None and tl.has_progress and cur is not None:
            threshold = float(self.cfg.get("media.match_threshold", 0.5) or 0.5)
            trusted = bool(self.cfg.get("media.trust_browser_progress", False))
            if tl.title:
                # 进度来源自带标题：必须与当前这首匹配才采信
                if similarity(cur.song, tl.title) >= threshold:
                    remaining = tl.remaining
            else:
                # 进度来源没标题（浏览器最常见：网易云网页版和B站视频都这样）。
                # 交叉验证：只有"正在播放的曲目来源"确认是这首时，
                # 才把这份进度算到这首头上。否则可能是B站视频的进度。
                music_confirmed = (
                    self.media is not None
                    and bool(self.media.title)
                    and similarity(cur.song, self.media.title) >= threshold
                )
                if (music_confirmed or (trusted and cur.detected_title)):
                    remaining = tl.remaining
        reason = self.store.should_auto_advance(
            fallback=fallback, grace=grace, remaining=remaining, almost_done=almost,
        )
        if not reason:
            # (b) 播放器换了别的歌，且当前这首已经播了足够久 → 认定它播完了
            info = self.media
            if info is not None:
                cur = self.store.current
                threshold = float(self.cfg.get("media.match_threshold", 0.5) or 0.5)
                min_play = float(self.cfg.get("media.min_playing_seconds", 25) or 25)
                changed_away = similarity(cur.song, info.title) < threshold
                settled = time.time() - self._media_since >= grace
                long_enough = self.store.playing_elapsed() >= min_play
                if changed_away and settled and long_enough:
                    reason = "switched"
        if reason:
            item = await self.store.next(reason=reason)
            if item is not None:
                self.log(f"⏭ 自动下一首（{reason}）→ 《{item.song}》")
            else:
                self.log(f"⏭ 自动下一首（{reason}），队列已空")

    def media_status(self) -> dict[str, Any]:
        info = self.media
        tl = self.timeline
        cur = self.store.current
        # 这份进度到底算不算当前这首的？和自动切歌用同一套判断，避免"显示和实际不一致"
        threshold = float(self.cfg.get("media.match_threshold", 0.5) or 0.5)
        trusted = bool(self.cfg.get("media.trust_browser_progress", False))
        attributed = False
        if tl is not None and tl.has_progress and cur is not None:
            if tl.source == "extapi":
                # 外部源是直接的播放器读数：歌名+进度同源，可信
                attributed = True
            elif tl.title:
                attributed = similarity(cur.song, tl.title) >= threshold
            else:
                music_confirmed = (info is not None and bool(info.title)
                                   and similarity(cur.song, info.title) >= threshold)
                attributed = music_confirmed or (trusted and bool(cur.detected_title))
        return {
            "auto_next": bool(self.cfg.get("media.auto_next", True)),
            "watch": bool(self.cfg.get("media.watch", True)),
            "detected": info.to_dict() if info else None,
            "timeline": tl.to_dict() if tl else None,
            "timeline_attributed": attributed,
            "timeline_trusted": trusted,
            "extapi": self.extapi.status(),
            "error": self._media_error,
            "detected_since": round(time.time() - self._media_since, 1) if self._media_since else 0,
            "playing_elapsed": round(self.store.playing_elapsed(), 1),
            "current_duration": (cur.duration if cur else 0.0),
        }

    async def set_auto_next(self, enabled: bool) -> None:
        self.cfg["media"]["auto_next"] = bool(enabled)
        self.cfg.save()
        self.log(f"自动下一首已{'开启' if enabled else '关闭'}")

    async def set_current_duration(self, seconds: float) -> bool:
        """手动给当前这首设定时长（读不到真实时长时用）。0 = 清除。"""
        if self.store.current is None:
            return False
        self.store.current.duration = max(float(seconds), 0.0)
        self.log(f"当前歌曲时长设为 {seconds:.0f} 秒" if seconds else "已清除当前歌曲时长")
        return True

    async def mark_current_done(self) -> dict[str, Any]:
        """手动告诉程序"这首播完了"，立刻下一首。"""
        item = await self.store.next(reason="manual_done")
        self.log(f"⏭ 手动确认播完 → {'《' + item.song + '》' if item else '队列已空'}")
        return {"ok": True, "current": item.to_dict() if item else None}

    async def set_mode(self, mode: str) -> None:
        if mode == "live":
            self.cfg["mode"] = "live"
        else:
            self.cfg["mode"] = "demo"
        self.cfg.save()
        await self.start_listener(mode, int(self.cfg.get("room_id", 0) or 0))

    async def set_room(self, room_id: int) -> None:
        self.cfg["room_id"] = int(room_id)
        self.cfg["mode"] = "live"
        self.cfg.save()
        await self.start_listener("live", room_id)

    def reload_gift_gate(self) -> None:
        """配置改动后让礼物账本立刻生效。

        账本本来就持有 cfg 引用（规则是每次判定时现读的），重建只是兜底。
        **送礼记录必须搬过去**：主播在直播中途把门槛从 1000 调到 2000，
        不该顺手把观众已经送过的礼物清零。
        """
        old = self.gifts
        self.gifts = GiftLedger(self.cfg)
        self.gifts.users = old.users
        self.gifts.seen_gifts = old.seen_gifts
        self.gifts.seen_free_gifts = old.seen_free_gifts
        self.gifts.blocks = old.blocks
        self.gifts.allows = old.allows
        self.gifts.recent_blocks = old.recent_blocks

    def reset_gift_gate(self) -> None:
        """清空贡献记录（主播换规则时可能想重新算）。"""
        self.gifts.reset()

    async def reload_netease(self) -> None:
        self.driver = build_driver(self.cfg)
        self.bridge = NeteaseBridge(
            enabled=bool(self.cfg.get("ncm_bridge.enabled", False)),
            timeout=float(self.cfg.get("ncm_bridge.timeout", 3.0) or 3.0),
        )
        ok, msg = await self.driver.test()
        self.log(("网易云歌单驱动：" if ok else "网易云歌单驱动不可用：") + msg)
        if self.cfg.get("ncm_bridge.enabled", False):
            if self.bridge.available(refresh_after=0.0):
                self.log(f"⚡ 播放队列桥：{self.bridge.describe_safe()}")
            else:
                self.log(f"⚠️ 播放队列桥不可用：{self.bridge.status()['message']}")

    async def simulate_danmaku(self, text: str, user: str = "测试观众",
                              uid: int = 0, *, kind: str = "danmaku",
                              coin: int = 0, paid: bool = True) -> dict[str, Any]:
        """本地注入一条事件，**走真实的解析与队列逻辑**。

        未开播时没有弹幕流，用这个可以完整演练点歌流程
        （和真实弹幕唯一的区别是"来源"，不绕过任何业务逻辑）。

        kind 支持 danmaku / gift / guard / super_chat，
        这样调"送礼物才能点歌"的门槛时不用真的去送礼。
        """
        text = (text or "").strip()
        who = user or "测试观众"
        who_uid = uid or abs(hash(who)) % 100000
        before = len(self.reply_queue)

        if kind == "gift":
            await self.on_danmaku({
                "type": "gift", "text": "", "user": who, "uid": who_uid,
                "gift": {"name": text or "测试礼物", "num": 1, "price": coin,
                         "total_coin": coin, "paid": bool(paid),
                         "guard_level": 0, "combo": False},
                "raw": {},
            })
            self.log(f"🧪 模拟礼物 {who}：{text or '测试礼物'} {coin} 瓜子"
                     f"{'（免费）' if not paid else ''}")
            return {"ok": True, "kind": "gift", "user": who, "coin": coin,
                    "paid": bool(paid), "reply": ""}

        if kind == "guard":
            await self.on_danmaku({
                "type": "guard", "text": "", "user": who, "uid": who_uid,
                "guard": {"level": int(text or 3), "num": 1,
                          "total_coin": coin, "name": "舰长"},
                "raw": {},
            })
            self.log(f"🧪 模拟上舰 {who}：等级 {text or 3}")
            return {"ok": True, "kind": "guard", "user": who, "reply": ""}

        if kind == "super_chat":
            await self.on_danmaku({
                "type": "super_chat", "text": text, "user": who, "uid": who_uid,
                "price": coin or 30, "raw": {},
            })
            self.log(f"🧪 模拟醒目留言 {who}：{text}（{coin or 30} 元）")
            return {"ok": True, "kind": "super_chat", "user": who, "reply": ""}

        if not text:
            return {"ok": False, "error": "弹幕内容不能为空"}
        await self.on_danmaku({
            "type": "danmaku", "text": text, "user": who,
            "uid": who_uid, "medal": None, "raw": {},
        })
        self.log(f"🧪 模拟弹幕 {who}：{text}")
        reply = self.reply_queue[-1] if len(self.reply_queue) > before else ""
        return {"ok": True, "text": text, "user": who, "reply": reply}

    async def set_extapi(self, enabled: bool, url: str = "") -> dict[str, Any]:
        """开启/关闭外部精确进度源，并立刻探测一次。"""
        self.cfg["extapi"]["enabled"] = bool(enabled)
        if url:
            self.cfg["extapi"]["urls"] = [u.strip() for u in url.replace("\n", ",").split(",") if u.strip()]
            self.cfg["extapi"]["url"] = ""
        self.cfg.save()
        if not enabled:
            self.log("外部媒体信息源已关闭")
            return {"ok": True, "message": "已关闭"}
        info = await self.extapi.poll()
        if info is not None:
            bits = [f"《{info.title}》"]
            if info.artist:
                bits.append(f"- {info.artist}")
            if info.duration > 0:
                bits.append(f"{info.position:.0f}s/{info.duration:.0f}s")
            msg = "连接成功：" + " ".join(bits)
            self.log("外部媒体源" + msg)
            return {"ok": True, "message": msg, "detected": info.to_dict()}
        err = self.extapi.last_error or "返回里没有识别出曲名"
        self.log(f"外部媒体源未就绪：{err}")
        return {"ok": False, "message": f"未就绪：{err}"}

    async def extapi_probe(self, url: str = "") -> dict[str, Any]:
        """只探测一次（可临时填 URL，逗号分隔多个），不改启用状态。"""
        if url:
            self.cfg["extapi"]["urls"] = [u.strip() for u in url.replace("\n", ",").split(",") if u.strip()]
        if not self.extapi.urls:
            return {"ok": False, "message": "还没填 URL"}
        old_enabled = self.cfg["extapi"]["enabled"]
        self.cfg["extapi"]["enabled"] = True
        try:
            info = await self.extapi.poll()
        finally:
            self.cfg["extapi"]["enabled"] = old_enabled
        top_keys: list[str] = []
        payload = self.extapi.last_payload
        if isinstance(payload, dict):
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            top_keys = list(data.keys())[:30]
        if info is not None:
            return {"ok": True, "message": f"识别到《{info.title}》", "detected": info.to_dict(),
                    "top_keys": top_keys, "urls": self.extapi.urls}
        return {"ok": False, "message": self.extapi.last_error or "没识别出曲名",
                "top_keys": top_keys, "urls": self.extapi.urls}

    async def netease_test(self) -> dict[str, Any]:
        ok, msg = await self.driver.test()
        return {"ok": ok, "message": msg, **self.driver.status()}

    def status(self) -> dict[str, Any]:
        st = self.listener.status() if self.listener else {"mode": self._mode, "connected": False}
        st["mode"] = self._mode
        st["netease"] = self.driver.status()
        # 礼物门槛状态：放在这里控制台才能实时显示"谁达标了、谁被拦了"
        st["gift_gate"] = self.gifts.status()
        # 桥状态：只在启用时才探测（探测会开管道，别拖慢每 2 秒的状态轮询）
        if self.bridge.enabled:
            self.bridge.available()
        st["ncm_bridge"] = self.bridge.status()
        st["room_id"] = self.cfg.get("room_id", 0)
        st["media"] = self.media_status()
        return st

    # ---------- 启停 ----------
    async def run(self, *, open_browser: bool = False) -> None:
        ctx: dict[str, Any] = {
            "loop": self.loop,
            "store": self.store,
            "config": self.cfg,
            "snapshot": lambda: {**self.store.snapshot(), "status": self.status()},
            "status": self.status,
            "set_mode": self.set_mode,
            "set_room": self.set_room,
            "netease_test": self.netease_test,
            "simulate_danmaku": self.simulate_danmaku,
            "log_change": self.log,
            "set_auto_next": self.set_auto_next,
            "set_extapi": self.set_extapi,
            "extapi_probe": self.extapi_probe,
            "set_duration": self.set_current_duration,
            "mark_done": self.mark_current_done,
            "simulate_danmaku": self.simulate_danmaku,
            "reload_netease": self.reload_netease,
            "reload_gift_gate": self.reload_gift_gate,
            "reset_gift_gate": self.reset_gift_gate,
            "log": self.log_lines,
        }
        self.store.subscribe(self._notify)

        self.server = BoardServer(
            str(self.cfg.get("http_host", "127.0.0.1")),
            int(self.cfg.get("http_port", 8765)),
            WEB_ROOT, ctx,
        )
        self.server.hub.set_snapshot_provider(ctx["snapshot"])
        self.server.hub.on_client_message = self._on_client_message
        try:
            self.server.start()
        except OSError as exc:
            self.log(f"端口 {self.cfg.get('http_port')} 不可用：{exc!r}；请在 config.json 换端口")
            return

        base = self.server.url
        print("=" * 62)
        print("  哔哩哔哩点歌板 已启动")
        print(f"  控制台：   {base}/control")
        print(f"  叠加层：   {base}/overlay        （加进直播姬的浏览器源）")
        print(f"  叠加层预览：{base}/overlay?bg=1  （带背景，方便先看效果）")
        print("=" * 62)
        self.log(f"控制台已就绪 {base}/control")

        await self.start_listener(self._mode, int(self.cfg.get("room_id", 0) or 0))
        self._status_task = asyncio.create_task(self._status_loop())
        if self.cfg.get("media.watch", True):
            self._track_task = asyncio.create_task(self._track_loop())
        if open_browser:
            webbrowser.open(f"{base}/control")

        await self._repl()

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            if self.server:
                self.server.broadcast({"event": "status", "status": self.status(),
                                       "snapshot": self.store.snapshot()})
            # 每轮都检查：既要推进队头，也要在歌单超限时清理。
            # ⚠️ 之前只在"队列有变动"时才跑，导致点歌板队列为空时
            #    歌单超限也不会被清理（实测踩过）。
            self._playlist_dirty = False
            try:
                if self.cfg.get("queue_only.enabled", False):
                    # 只插播放队列模式：歌单一个字节都不动
                    await self._sync_queue_only()
                else:
                    await self._sync_playlist()
            except Exception as exc:
                self.log(f"⚠️ 同步出错（不影响点歌）：{exc!r}")

    async def _on_client_message(self, msg: dict[str, Any]) -> None:
        if msg.get("type") == "ping" and self.server:
            self.server.broadcast({"event": "pong"})

    def _gift_cli(self, arg: str) -> None:
        """命令行版的礼物门槛设置（等价于控制台那张卡片）。

            gift                看当前设置
            gift on / off       开关门槛
            gift min 2000       改门槛金额（会切到 min_total）
            gift mode any_paid  改门槛方式
            gift window 600     送礼后 600 秒内有效（0=永久）
            gift paid on|off    是否只认付费礼物
            gift guard on|off   舰长是否直接放行
            gift list           看谁已经达标
            gift reset          清空累计
        """
        parts = arg.split()
        sub = parts[0].lower() if parts else ""
        rest = parts[1:]
        MODES = ("min_total", "any_paid", "per_send", "guard_only")

        def show() -> None:
            gg = self.cfg.get("gift_gate", {}) or {}
            win = gg.get("window_seconds") or 0
            print(f"  门槛：{'开启' if gg.get('enabled') else '关闭'}"
                  f"  方式：{gg.get('mode')}"
                  f"  金额：{gg.get('min_coin')} 瓜子"
                  f"  时效：{'永久' if not win else str(win) + ' 秒'}"
                  f"  只认付费礼物：{'是' if gg.get('require_paid') else '否'}")

        def change(**kw: Any) -> None:
            for key, val in kw.items():
                self.cfg[f"gift_gate.{key}"] = val
            self.cfg.save()
            # 账本持有 cfg 引用，但重建一次更稳；reload 会保留已记下的记录
            self.reload_gift_gate()
            show()

        if sub in ("", "status"):
            show()
            return
        if sub in ("on", "off"):
            change(enabled=sub == "on")
            return
        if sub == "min" and rest and rest[0].isdigit():
            change(min_coin=int(rest[0]), mode="min_total")
            return
        if sub == "window" and rest and rest[0].isdigit():
            change(window_seconds=int(rest[0]))
            return
        if sub == "mode" and rest and rest[0] in MODES:
            change(mode=rest[0])
            return
        if sub in ("paid", "guard") and rest and rest[0] in ("on", "off"):
            key = "require_paid" if sub == "paid" else "guard_always_ok"
            change(**{key: rest[0] == "on"})
            return
        if sub == "list":
            st = self.gifts.status()
            top = st.get("top") or []
            if not top:
                print("  还没有人送过礼")
            for row in top:
                mark = "✓" if self.gifts.qualified(int(row.get("uid") or 0)) else "·"
                print(f"  {mark} {row.get('name')}  累计 {row.get('coin')} 瓜子"
                      f"  礼物 {row.get('gifts')} 个"
                      + (f"  舰长{row.get('guard')}" if row.get("guard") else ""))
            blocked = st.get("recent_blocks") or []
            if blocked:
                print("  最近被拦：")
                for row in blocked[:5]:
                    print(f"    {row.get('user')}  {row.get('reason')}")
            return
        if sub == "reset":
            self.gifts.reset()
            print("  贡献记录已清空")
            return
        print("  用法：gift [on|off|min 2000|mode any_paid|window 600"
              "|paid on|guard on|list|reset]")

    async def _repl(self) -> None:
        """命令行控制台，和网页控制台等价。

        没有可用终端（后台启动、管道、nohup）时不能因为 EOF 就退出——
        否则服务会跟着一起停掉。EOF 只在第一次出现，之后转为常驻模式。
        """
        loop = asyncio.get_event_loop()
        interactive = sys.stdin is not None
        if interactive:
            try:
                interactive = bool(sys.stdin.isatty())
            except Exception:
                interactive = False
        if interactive:
            print(HELP)
        else:
            self.log("未检测到交互终端，命令行控制台已禁用；请用网页控制台操作")
        while True:
            if not interactive:
                await asyncio.sleep(3600)
                continue
            try:
                line = await loop.run_in_executor(None, lambda: input("点歌板> ").strip())
            except (EOFError, KeyboardInterrupt):
                interactive = False
                print()
                self.log("命令行输入已结束（无终端/管道关闭），服务继续运行，请用网页控制台")
                continue
            except Exception as exc:  # 输入通道异常，退回常驻模式
                interactive = False
                self.log(f"命令行输入不可用（{exc!r}），改用网页控制台")
                continue
            if not line:
                snap = self.store.snapshot()
                cur = snap["current"]
                print(f"  当前：{cur['song'] if cur else '（空闲）'}")
                for i, s in enumerate(snap["queue"], 1):
                    if s["state"] == "playing":
                        continue
                    print(f"   {i}. {s['song']}  @{s['user']}")
                continue
            parts = line.split(maxsplit=1)
            cmd, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
            if cmd in ("q", "quit", "exit"):
                break
            elif cmd in ("h", "help"):
                print(HELP)
            elif cmd in ("n", "next"):
                item = await self.store.next(reason="skip")
                print(f"  切歌：{'《' + item.song + '》' if item else '队列已空'}")
            elif cmd in ("a", "add"):
                if arg:
                    req, result = await self.store.add(arg, "主播", source="control", force=True)
                    print(f"  {result}：《{req.song}》")
            elif cmd == "r":
                items = [s for s in self.store.active() if s.state.value == "waiting"]
                if arg.isdigit() and 1 <= int(arg) <= len(items):
                    await self.store.remove(items[int(arg) - 1].id)
                    print("  已移除")
                else:
                    print("  序号不对")
            elif cmd == "top":
                items = [s for s in self.store.active() if s.state.value == "waiting"]
                if arg.isdigit() and 1 <= int(arg) <= len(items):
                    await self.store.move(items[int(arg) - 1].id, True)
                    print("  已置顶")
                else:
                    print("  序号不对")
            elif cmd == "clear":
                await self.store.clear()
                print("  队列已清空")
            elif cmd == "mode":
                await self.set_mode("live" if arg == "live" else "demo")
            elif cmd == "room":
                if arg.isdigit():
                    await self.set_room(int(arg))
                else:
                    print("  用法：room 12345")
            elif cmd == "ne":
                st = self.driver.status()
                ok, msg = await self.driver.test()
                print(f"  {json.dumps(st, ensure_ascii=False)}\n  {msg}")
                if self.cfg.get("ncm_bridge.enabled", False):
                    self.bridge.available(refresh_after=0.0)
                    bs = self.bridge.status()
                    print(f"  播放队列桥：{json.dumps(bs, ensure_ascii=False)}")
                    print(f"    {self.bridge.describe_safe()}")
                else:
                    print("  播放队列桥：未启用（ncm_bridge.enabled=false）")
            elif cmd == "gift":
                self._gift_cli(arg)
            else:
                print("  未知指令，输入 h 看帮助")

        self.shutdown()

    def shutdown(self) -> None:
        self.log("正在退出…")
        self.stop_listener()
        if self.server:
            self.server.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="songboard", description="哔哩哔哩直播点歌板")
    ap.add_argument("--room", type=int, help="直播间号（不是 UID）")
    ap.add_argument("--port", type=int, help="本地网页端口，默认 8765")
    ap.add_argument("--mode", choices=["demo", "live"], help="演示模式 / 直播间模式")
    ap.add_argument("--config", default=str(ROOT / "config.json"), help="配置文件路径")
    ap.add_argument("--open", action="store_true", help="启动后打开控制台")
    ap.add_argument("--no-persist", action="store_true", help="不保存队列状态到磁盘")
    args = ap.parse_args(argv)

    cfg = Config.load(Path(args.config))
    if args.room:
        cfg["room_id"] = args.room
        cfg["mode"] = "live"
    if args.mode:
        cfg["mode"] = args.mode
    if args.port:
        cfg["http_port"] = args.port
    if args.room or args.mode or args.port:
        cfg.save()

    async def amain() -> None:
        app = App(cfg, persist=not args.no_persist)
        try:
            await app.run(open_browser=args.open)
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\n已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
