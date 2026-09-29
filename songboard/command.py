"""弹幕指令解析。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import Config


@dataclass
class Command:
    action: str            # add | cancel | query | none
    song: str = ""
    raw: str = ""
    reason: str = ""       # 命中哪个关键词


_LEADING_NOISE = re.compile(r"^[\s:：,，、\-—]+")


class CommandParser:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.reload()

    def reload(self) -> None:
        self.prefixes = [p for p in self.cfg.get("danmaku.prefixes", ["点歌"]) if p]
        self.cancel_kw = self.cfg.get("danmaku.cancel_keywords", [])
        self.query_kw = self.cfg.get("danmaku.query_keywords", [])
        # 长前缀优先，避免「点歌」先吃掉「!点歌」
        self.prefixes.sort(key=len, reverse=True)

    def parse(self, text: str) -> Command:
        raw = (text or "").strip()
        if not raw:
            return Command("none", raw=raw)

        # 关键词列表为空 = 该指令关闭（在 config.json 里留空即可禁用）
        for kw in self.cancel_kw:
            if kw and raw.startswith(kw):
                return Command("cancel", raw=raw, reason=kw)
        for kw in self.query_kw:
            if kw and raw.startswith(kw):
                return Command("query", raw=raw, reason=kw)

        for p in self.prefixes:
            if not p or not raw.startswith(p):
                continue
            song = _LEADING_NOISE.sub("", raw[len(p):]).strip()
            return Command("add", song=song, raw=raw, reason=p)

        return Command("none", raw=raw)

    def help_text(self) -> str:
        p = self.prefixes[0] if self.prefixes else "点歌"
        return f"发送「{p} 歌名」点歌"
