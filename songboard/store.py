"""点歌队列：去重、冷却、上限、备选池、持久化。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Awaitable

from .config import Config
from .models import SongRequest, SongState

Listener = Callable[[dict[str, Any]], Awaitable[None]]


class QueueFull(Exception):
    pass


class QueueStore:
    def __init__(self, cfg: Config, data_dir: Path, *, persist: bool = True) -> None:
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        self.persist_path = self.data_dir / "queue_state.json"
        self.persist = persist
        self.current: SongRequest | None = None
        self.played: list[SongRequest] = []
        self.pending: list[SongRequest] = []      # 队列满时的备选池
        self._listeners: list[Listener] = []
        self._last_by_user: dict[str, float] = {}

    # ---------- 事件 ----------
    def subscribe(self, fn: Listener) -> None:
        self._listeners.append(fn)

    async def _emit(self, event: str, **kw: Any) -> None:
        payload = {"event": event, "snapshot": self.snapshot(), **kw}
        for fn in list(self._listeners):
            try:
                await fn(payload)
            except Exception as exc:  # 一个订阅者坏掉不能影响队列
                print(f"[store] listener error: {exc!r}")

    # ---------- 查询 ----------
    @property
    def waiting(self) -> list[SongRequest]:
        return [s for s in self.played if s.state is SongState.WAITING]

    @property
    def playing(self) -> list[SongRequest]:
        return [s for s in self.played if s.state is SongState.PLAYING]

    def playing_elapsed(self) -> float:
        if self.current is None or self.current.started_at is None:
            return 0.0
        return time.time() - self.current.started_at

    def should_auto_advance(
        self, *, fallback: float = 300.0, grace: float = 9.0,
        remaining: float | None = None, almost_done: float = 3.0,
    ) -> str:
        """判断当前这首是不是该自动下一首了。

        返回原因字符串（用于日志），不该切就返回空串：
          "progress" —— 播放器进度快到底了（最准，需要播放器提供进度）
          "done"     —— 播放时间超过估算时长（没有进度信号时用）
        """
        cur = self.current
        if cur is None or cur.started_at is None:
            return ""
        # 最优先：播放器自己报的剩余时间。只有确认"正在放的就是这首"才用它，
        # 否则（比如主播切到了别的歌）用进度判断会把队列切乱。
        if remaining is not None and remaining >= 0 and cur.detected_title:
            if remaining <= almost_done:
                return "progress"
        elapsed = time.time() - cur.started_at
        limit = cur.duration if cur.duration > 0 else fallback
        if elapsed >= limit + grace:
            return "done"
        return ""

    def active(self) -> list[SongRequest]:
        """在队列里等待 + 正在播放的条目。"""
        items = [s for s in self.played if s.state in (SongState.PLAYING, SongState.WAITING)]
        return items

    def user_waiting_count(self, user: str, uid: int = 0) -> int:
        key = self._user_key(user, uid)
        return sum(1 for s in self.played
                   if s.state in (SongState.WAITING, SongState.PLAYING)
                   and self._user_key(s.user, s.uid) == key)

    @staticmethod
    def _user_key(user: str, uid: int) -> str:
        return str(uid) if uid else user

    @staticmethod
    def _norm(song: str) -> str:
        return "".join(song.lower().split())

    def find_duplicate(self, song: str) -> SongRequest | None:
        target = self._norm(song)
        for s in self.played:
            if s.state in (SongState.WAITING, SongState.PLAYING) and self._norm(s.song) == target:
                return s
        return None

    # ---------- 写入 ----------
    async def add(
        self,
        song: str,
        user: str,
        uid: int = 0,
        *,
        source: str = "danmaku",
        force: bool = False,
        _cooldown_checked: bool = False,
    ) -> tuple[SongRequest, str]:
        """返回 (条目, 结果说明)。结果说明用于回弹幕提示。"""
        song = song.strip()[: int(self.cfg.get("queue.max_song_name_len", 40))]
        if not song:
            raise ValueError("歌名不能为空")

        if not force:
            dup = self.find_duplicate(song)
            if dup:
                return dup, "duplicate"

            cooldown = float(self.cfg.get("queue.cooldown_seconds", 30))
            key = self._user_key(user, uid)
            last = self._last_by_user.get(key, 0.0)
            if not _cooldown_checked and cooldown > 0 and time.time() - last < cooldown:
                return SongRequest(song=song, user=user, uid=uid), "cooldown"

            per_user = int(self.cfg.get("queue.per_user_limit", 2))
            if per_user > 0 and self.user_waiting_count(user, uid) >= per_user:
                items = [s for s in self.played
                         if s.state in (SongState.WAITING, SongState.PLAYING)
                         and self._user_key(s.user, s.uid) == key]
                return items[0] if items else SongRequest(song=song, user=user, uid=uid), "user_limit"

        req = SongRequest(song=song, user=user, uid=uid, source=source)
        max_size = int(self.cfg.get("queue.max_size", 30))
        if not force and len(self.active()) >= max_size:
            req.state = SongState.PENDING
            self.pending.append(req)
            await self._emit("pending_added", item=req.to_dict())
            self._save()
            return req, "queue_full"

        req.state = SongState.WAITING
        self.played.append(req)
        self._last_by_user[self._user_key(user, uid)] = time.time()
        if self.current is None:
            # 空闲时点的第一首：直接成为"正在播放"。
            # 这里必须先把 current 设好，再广播，否则监听者拿到的快照还是 idle。
            req.state = SongState.PLAYING
            req.started_at = time.time()
            self.current = req
            await self._emit("playing", item=req.to_dict(), reason="auto")
        else:
            await self._emit("added", item=req.to_dict())
        self._save()
        return req, "ok"

    async def add_many(self, entries: list[dict[str, Any]], *, source: str = "control") -> int:
        n = 0
        for e in entries:
            try:
                await self.add(
                    str(e.get("song", "")), str(e.get("user", "主播")),
                    int(e.get("uid", 0) or 0), source=source, force=bool(e.get("force", False)),
                )
                n += 1
            except ValueError:
                continue
        return n

    # ---------- 播放控制 ----------
    async def _advance(self, reason: str) -> SongRequest | None:
        """把 current 标记完成，并从队列取下一首。"""
        if self.current is not None:
            self.current.state = (
                SongState.SKIPPED if reason == "skip" else SongState.PLAYED
            )
            self.current.finished_at = time.time()
            done = self.current
            await self._emit("finished", item=done.to_dict(), reason=reason)

        nxt = next((s for s in self.played if s.state is SongState.WAITING), None)
        if nxt is None and self.pending:
            nxt = self.pending.pop(0)
            nxt.state = SongState.WAITING
            self.played.append(nxt)
        if nxt is not None:
            nxt.state = SongState.PLAYING
            nxt.started_at = time.time()
            self.current = nxt
            await self._emit("playing", item=nxt.to_dict(), reason=reason)
        else:
            self.current = None
            await self._emit("idle", reason=reason)
        return self.current

    async def next(self, *, reason: str = "skip") -> SongRequest | None:
        item = await self._advance(reason=reason)
        self._save()
        return item

    async def play_specific(self, item_id: str, *, detected_title: str = "") -> SongRequest | None:
        """把队列里指定的某一首直接设为正在播放（播放器检测到"就是这首"时用）。"""
        target = next((s for s in self.played if s.id == item_id), None)
        if target is None or target.state not in (SongState.WAITING, SongState.PLAYING):
            return None
        if target is self.current:
            if detected_title:
                target.detected_title = detected_title
            return target
        if self.current is not None:
            self.current.state = SongState.PLAYED
            self.current.finished_at = time.time()
        target.state = SongState.PLAYING
        target.started_at = time.time()
        if detected_title:
            target.detected_title = detected_title
        self.current = target
        await self._emit("playing", item=target.to_dict(), reason="detected")
        self._save()
        return target

    async def remove(self, item_id: str) -> bool:
        for i, s in enumerate(self.played):
            if s.id == item_id and s.state in (SongState.WAITING, SongState.PLAYING):
                was_current = s.state is SongState.PLAYING
                s.state = SongState.REJECTED
                s.finished_at = time.time()
                if was_current:
                    self.current = None
                    await self._advance(reason="removed")
                else:
                    await self._emit("removed", item=s.to_dict())
                self._save()
                return True
        for i, s in enumerate(self.pending):
            if s.id == item_id:
                s.state = SongState.REJECTED
                self.pending.pop(i)
                await self._emit("removed", item=s.to_dict())
                self._save()
                return True
        return False

    async def move(self, item_id: str, to_top: bool = True) -> bool:
        idx = next((i for i, s in enumerate(self.played)
                    if s.id == item_id and s.state is SongState.WAITING), None)
        if idx is None:
            return False
        item = self.played.pop(idx)
        if to_top:
            insert_at = 0
            for i, s in enumerate(self.played):
                if s.state is SongState.PLAYING:
                    insert_at = i + 1
            self.played.insert(insert_at, item)
        else:
            self.played.append(item)
        await self._emit("moved", item=item.to_dict())
        self._save()
        return True

    async def clear(self) -> None:
        for s in self.played:
            if s.state in (SongState.WAITING, SongState.PLAYING):
                s.state = SongState.REJECTED
                s.finished_at = time.time()
        self.current = None
        self.played = [s for s in self.played if s.state in (SongState.PLAYED, SongState.SKIPPED)]
        self.pending.clear()
        self._last_by_user.clear()
        await self._emit("cleared")
        self._save()

    def reset_cooldown(self, user: str | None = None) -> None:
        if user is None:
            self._last_by_user.clear()
        else:
            self._last_by_user.pop(user, None)

    async def set_current_by_title(
        self, title: str, *, artist: str = "", strict: bool = True,
    ) -> tuple[SongRequest | None, str]:
        """把"正在播放"改成队列里匹配这个曲名的那首（不改变播放位置）。

        用于"以网易云实际播放状态为准"：
        主播自己放了一首点歌单里的歌时，点歌板要跟着显示它，
        但**不能**顺手把队列状态改掉（那会破坏"还没播"的判定）。

        返回 (命中的条目, 说明)。说明为 "" 表示没匹配上。
        """
        from .media import normalize, similarity

        target = (title or "").strip()
        if not target:
            return None, "曲名空"

        active = self.active()
        if not active:
            return None, "队列里没有歌"

        # 严格模式：归一化后要求相等或互相包含（"句号" vs "句号 - G.E.M.邓紫棋"）
        def score(item: SongRequest) -> float:
            if strict:
                a, b = normalize(item.song), normalize(target)
                if not a or not b:
                    return 0.0
                return 1.0 if (a == b or a in b or b in a) else 0.0
            return similarity(item.song, target)

        best = max(active, key=score)
        if score(best) <= 0:
            return None, "队列里没有这首歌"

        if best is self.current:
            if artist:
                best.detected_title = target
            return best, "已经是当前曲目"
        # 只改"当前指向"，不改任何条目的播放状态：
        # 否则会破坏"这首还没播"的判定，导致点歌队列错乱。
        self.current = best
        best.detected_title = target
        await self._emit("current_changed", item=best.to_dict(), source="netease")
        self._save()
        return best, f"当前曲目已指到队列里的《{best.song}》"

    def set_netease(self, item_id: str, track_id: int, name: str = "") -> bool:
        """回填"这首歌在网易云歌单里对应哪一首"，用于后续重排顺序。"""
        for s in self.played:
            if s.id == item_id:
                s.netease_id = int(track_id)
                if name:
                    s.netease_name = name
                self._save()
                return True
        return False

    def queued_netease_ids(self) -> list[tuple[str, int]]:
        """还没播的歌里，已经写进网易云的（保持点歌队列顺序）。

        返回 [(item_id, netease_track_id)]，**顺序就是应该播放的顺序**。
        """
        out: list[tuple[str, int]] = []
        for s in self.played:
            if s.state is SongState.WAITING and s.netease_id:
                out.append((s.id, int(s.netease_id)))
        return out

    def next_up(self) -> SongRequest | None:
        """队头：下一首该播的歌。

        定义：当前"正在播放"那首之后，第一个还在等待的。
        如果当前没有指向任何歌，就是第一个等待中的。
        """
        active = self.active()
        if not active:
            return None
        started = False
        for s in active:
            if s is self.current:
                started = True
                continue
            if started and s.state is SongState.WAITING:
                return s
        # 当前指向为空或不在队列里时，第一个等待的即队头
        if self.current is None:
            for s in active:
                if s.state is SongState.WAITING:
                    return s
        return None

    def has_pending_work(self) -> bool:
        """还有没有"该播但还没进网易云歌单"的歌。"""
        return any(s.state is SongState.WAITING and not s.netease_id for s in self.played)

    # ---------- 快照与持久化 ----------
    def snapshot(self) -> dict[str, Any]:
        active = self.active()
        return {
            "current": self.current.to_dict() if self.current else None,
            "queue": [s.to_dict() for s in active],
            "pending": [s.to_dict() for s in self.pending],
            "counts": {
                "queue": len(active),
                "waiting": max(len(active) - (1 if self.current else 0), 0),
                "pending": len(self.pending),
                "played": sum(1 for s in self.played if s.state is SongState.PLAYED),
            },
        }

    def _save(self) -> None:
        if not self.persist:
            return
        keep = [s for s in self.played
                if s.state in (SongState.PLAYING, SongState.WAITING, SongState.PENDING)]
        payload = {"current": self.current.to_dict() if self.current else None,
                   "items": [s.to_dict() for s in keep],
                   "pending": [s.to_dict() for s in self.pending]}
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.persist_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            print(f"[store] 持久化失败: {exc}")

    def load_persisted(self) -> None:
        if not self.persist or not self.persist_path.exists():
            return
        try:
            raw = json.loads(self.persist_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for d in raw.get("items", []):
            self.played.append(SongRequest(
                song=d.get("song", ""), user=d.get("user", ""), uid=int(d.get("uid", 0) or 0),
                state=SongState.WAITING, id=d.get("id") or SongRequest("").id,
                created_at=float(d.get("created_at", time.time())), source=d.get("source", "control"),
            ))
        for d in raw.get("pending", []):
            self.pending.append(SongRequest(
                song=d.get("song", ""), user=d.get("user", ""), uid=int(d.get("uid", 0) or 0),
                state=SongState.PENDING, created_at=float(d.get("created_at", time.time())),
            ))
        if self.played and self.current is None:
            self.played[0].state = SongState.PLAYING
            self.played[0].started_at = time.time()
            self.current = self.played[0]
