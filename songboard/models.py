"""点歌条目与状态枚举。"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class SongState(str, Enum):
    WAITING = "waiting"    # 在队列里等待
    PLAYING = "playing"    # 当前播放
    PLAYED = "played"      # 已播完
    SKIPPED = "skipped"    # 被跳过
    REJECTED = "rejected"  # 被主播/规则拒绝
    PENDING = "pending"    # 队列满，进备选池


@dataclass
class SongRequest:
    song: str
    user: str
    uid: int = 0
    state: SongState = SongState.WAITING
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    source: str = "danmaku"   # danmaku | control | demo
    note: str = ""
    matched: dict[str, Any] | None = None  # P3: 网易云匹配结果
    duration: float = 0.0     # 估算时长（秒），0 = 未知，用配置里的兜底值
    detected_title: str = ""  # 从播放器读到的实际曲目（用于"播完了"判定）
    netease_id: int = 0       # 网易云 track id（写进歌单后回填，用于重排顺序）
    netease_name: str = ""    # 实际加到歌单里的曲名（可能和点歌名不同，如命中翻唱）

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        d["created_at_str"] = time.strftime("%H:%M:%S", time.localtime(self.created_at))
        if self.started_at:
            elapsed = time.time() - self.started_at
            d["playing_elapsed"] = round(elapsed, 1)
            limit = self.duration if self.duration > 0 else 0.0
            d["remaining"] = round(max(limit - elapsed, 0.0), 1) if limit else None
        return d
