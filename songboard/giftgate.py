"""礼物门槛：只有送过礼物的观众才能点歌，规则由主播在控制台里配。

为什么单独一个模块：
  门槛规则会随直播节奏变（开播初期宽一点、人气高了收紧），所以**必须可配**，
  不能写死。同时判定逻辑要能单独测（用假礼物消息测边界），
  不能和网络代码、队列代码缠在一起。

支持的规则（可组合）：
  * 必须送过**付费**礼物（免费礼物不算 —— 否则送个免费辣条就拿到资格）
  * 累计金额达到门槛（瓜子，1000 瓜子 = 1 元）
  * 时效：送礼后 N 秒内有效（0 = 永久有效）
  * 舰长及以上直接放行
  * 醒目留言（SC）可单独设门槛或直接放行
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


def _as_int(value: Any) -> int:
    """把 B 站消息里的数值字段安全转成 int。

    ⚠️ 这个必须放在这里，不能只在 bilibili.py 里做：
    账本可能被直接喂原始消息（自检、控制台模拟、以后接别的弹幕源），
    而 B 站同一字段在不同消息里可能是 int / str / float / None。
    直接 int() 会抛 ValueError —— 异常发生在消息处理循环里会**掐断整条处理**，
    表现就是"送了好几个礼物但一个都没记上"。
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


@dataclass
class Contribution:
    """一个观众累计的贡献。"""

    uid: int
    name: str = ""
    total_coin: int = 0          # 累计金额（瓜子）
    last_at: float = 0.0         # 最后一次送礼时间
    last_coin: int = 0           # 最后一次送礼的金额
    gifts: int = 0               # 送礼次数
    paid_once: bool = False      # 送过付费礼物（含金额为 0 但标记为付费的）
    guard_level: int = 0         # 0=无 1=总督 2=提督 3=舰长
    guard_at: float = 0.0
    sc_count: int = 0            # 醒目留言次数
    spent_coin: int = 0          # 已消耗掉的额度（按次消耗模式用）


@dataclass
class GateDecision:
    """判定结果。ok=False 时 reason 是给主播看的原因，hint 是给观众的提示。"""

    ok: bool
    reason: str = ""
    hint: str = ""
    #: 剩余额度（按次消耗模式），None 表示该模式不适用
    remaining: int | None = None
    contribution: Contribution | None = None


class GiftLedger:
    """记录谁送过礼、送了多少，并据此判定点歌资格。

    线程模型：就在 asyncio 事件循环里用，不需要加锁。
    """

    #: 最多记多少人，防止长期直播内存无限涨
    MAX_USERS = 5000

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        #: uid -> Contribution
        self.users: dict[int, Contribution] = {}
        #: 统计，给控制台显示
        self.seen_gifts = 0
        self.seen_free_gifts = 0
        self.blocks = 0
        self.allows = 0
        #: 最近被拦下的记录（控制台显示，方便主播判断门槛是不是太严）
        self.recent_blocks: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- 配置
    def _get(self, key: str, default: Any) -> Any:
        return self.cfg.get(f"gift_gate.{key}", default)

    @property
    def enabled(self) -> bool:
        return bool(self._get("enabled", False))

    @property
    def mode(self) -> str:
        return str(self._get("mode", "min_total") or "min_total")

    @property
    def min_coin(self) -> int:
        return int(self._get("min_coin", 1000) or 0)

    @property
    def window_seconds(self) -> int:
        return int(self._get("window_seconds", 0) or 0)

    @property
    def require_paid(self) -> bool:
        return bool(self._get("require_paid", True))

    @property
    def guard_always_ok(self) -> bool:
        return bool(self._get("guard_always_ok", True))

    @property
    def guard_min_level(self) -> int:
        """1=总督 2=提督 3=舰长；`guard_always_ok` 或 `mode=guard_only` 时生效。"""
        return int(self._get("guard_min_level", 3) or 3)

    @property
    def sc_always_ok(self) -> bool:
        return bool(self._get("sc_always_ok", True))

    @property
    def sc_min_coin(self) -> int:
        return int(self._get("sc_min_coin", 0) or 0)

    # ---------------------------------------------------------------- 记账
    def _contribution(self, uid: int, name: str = "") -> Contribution:
        c = self.users.get(uid)
        if c is None:
            c = Contribution(uid=uid, name=name or f"uid{uid}")
            self.users[uid] = c
            if len(self.users) > self.MAX_USERS:
                # 简单淘汰：丢掉最久没送礼的一批
                oldest = sorted(self.users.values(), key=lambda x: x.last_at)
                for gone in oldest[: len(self.users) - self.MAX_USERS]:
                    self.users.pop(gone.uid, None)
        elif name:
            c.name = name
        return c

    def record_gift(self, ev: dict[str, Any]) -> Contribution | None:
        """记一笔礼物。返回更新后的贡献，uid 无效时返回 None。"""
        gift = ev.get("gift") or {}
        uid = _as_int(ev.get("uid"))
        name = str(ev.get("user") or "")
        if uid <= 0:
            # 连击聚合消息里 uid 可能是 0；没有 uid 就没法归到人头上，
            # 只能丢弃（记一下，方便主播发现"连击没算进去"）
            return None

        coin = _as_int(gift.get("total_coin"))
        paid = bool(gift.get("paid", True))
        if not paid:
            self.seen_free_gifts += 1
            if self.require_paid:
                # 免费礼物仍要记住这个人，但**不计入**金额
                c = self._contribution(uid, name)
                c.gifts += 1
                return c
        self.seen_gifts += 1
        c = self._contribution(uid, name)
        c.gifts += 1
        c.total_coin += coin
        c.last_coin = coin
        c.last_at = time.time()
        if paid:
            c.paid_once = True
        gl = _as_int(gift.get("guard_level"))
        if gl:
            c.guard_level = gl
        return c

    def record_guard(self, ev: dict[str, Any]) -> Contribution | None:
        """记一笔上舰。"""
        guard = ev.get("guard") or {}
        uid = _as_int(ev.get("uid"))
        if uid <= 0:
            return None
        c = self._contribution(uid, str(ev.get("user") or ""))
        c.guard_level = _as_int(guard.get("level"))
        c.guard_at = time.time()
        coin = _as_int(guard.get("total_coin"))
        if coin > 0:
            c.total_coin += coin
            c.paid_once = True
            c.last_coin = coin
            c.last_at = time.time()
        return c

    def record_super_chat(self, ev: dict[str, Any]) -> Contribution | None:
        """记一笔醒目留言（也是付费的）。"""
        uid = _as_int(ev.get("uid"))
        if uid <= 0:
            return None
        c = self._contribution(uid, str(ev.get("user") or ""))
        c.sc_count += 1
        coin = _as_int(ev.get("price"))
        if coin > 0:
            c.total_coin += coin
            c.paid_once = True
            c.last_coin = coin
            c.last_at = time.time()
        return c

    def spend(self, uid: int, coin: int) -> None:
        """按次消耗模式：扣掉额度。"""
        c = self.users.get(uid)
        if c is not None:
            c.spent_coin += max(0, _as_int(coin))

    # ---------------------------------------------------------------- 判定
    def check(self, uid: int, *, is_super_chat: bool = False,
              record: bool = True) -> GateDecision:
        """判定这个观众能不能点歌。

        `record=False` 是只读判定：不累加 allowed/blocked，也不记进
        "最近被拦" 列表。控制台想显示"谁有资格"时用这个，
        否则光是刷新一下面板就会把统计和拦截记录弄脏。
        """
        if not self.enabled:
            return GateDecision(True, reason="门槛未启用")

        def allow(reason: str, **kw: Any) -> GateDecision:
            if record:
                self.allows += 1
            return GateDecision(True, reason=reason, **kw)

        def deny(c: Contribution | None, reason: str, hint: str,
                 remaining: int | None = None) -> GateDecision:
            if record:
                return self._block(c, reason, hint, remaining)
            return GateDecision(False, reason=reason, hint=hint,
                                remaining=remaining, contribution=c)

        c = self.users.get(_as_int(uid)) if uid else None

        # 醒目留言：单独放行/单独门槛
        if is_super_chat:
            if self.sc_always_ok:
                return allow("醒目留言放行", contribution=c)
            if self.sc_min_coin > 0 and (c is None or c.total_coin < self.sc_min_coin):
                return deny(c, f"醒目留言金额不足 {self.sc_min_coin}",
                            f"醒目留言满 {self.sc_min_coin} 瓜子才能点歌")

        if c is None:
            return deny(None, "没送过礼物", self._hint())

        # 舰长放行
        if self.guard_always_ok and c.guard_level > 0 \
                and c.guard_level <= self.guard_min_level:
            return allow("舰长放行", contribution=c)

        # 时效过期？
        if self.window_seconds > 0:
            age = time.time() - c.last_at
            if age > self.window_seconds:
                return deny(
                    c, f"上次送礼已过 {int(age)} 秒（限 {self.window_seconds} 秒）",
                    f"需要 {self.window_seconds // 60} 分钟内送过礼物才能点歌")

        # 必须送过付费礼物
        if self.require_paid and not c.paid_once:
            return deny(c, "只送过免费礼物", "免费礼物不算哦，需要付费礼物")

        mode = self.mode
        if mode == "any_paid":
            if c.paid_once:
                return allow("送过付费礼物", contribution=c)
            return deny(c, "没送过付费礼物", "送任意付费礼物即可点歌")

        if mode == "guard_only":
            if c.guard_level > 0 and c.guard_level <= self.guard_min_level:
                return allow("舰长", contribution=c)
            name = {1: "总督", 2: "提督", 3: "舰长"}.get(self.guard_min_level, "舰长")
            return deny(c, "不是舰长", f"需要开通{name}才能点歌")

        if mode == "per_send":
            # 按次消耗：可用额度 = 累计 - 已消耗
            if self.min_coin <= 0:
                return allow("未设门槛", contribution=c)
            avail = c.total_coin - c.spent_coin
            if avail >= self.min_coin:
                return allow(f"额度充足（剩 {avail}）", remaining=avail,
                             contribution=c)
            return deny(
                c, f"额度不足（剩 {avail} < {self.min_coin}）",
                f"每次点歌需要 {self.min_coin} 瓜子，当前剩 {avail}",
                remaining=avail)

        # 默认：min_total —— 累计金额达标即可（window 已在上面校验过）
        if c.total_coin >= self.min_coin:
            return allow(f"累计 {c.total_coin} 瓜子（门槛 {self.min_coin}）",
                         remaining=c.total_coin - c.spent_coin, contribution=c)
        return deny(
            c, f"累计 {c.total_coin} < 门槛 {self.min_coin}",
            f"还差 {self.min_coin - c.total_coin} 瓜子（累计 {c.total_coin}/{self.min_coin}）",
            remaining=c.total_coin - c.spent_coin)

    def qualified(self, uid: int) -> bool:
        """只读判定：这个人现在有没有资格点歌（不动任何统计）。"""
        return self.check(uid, record=False).ok

    def _hint(self) -> str:
        if self.mode == "guard_only":
            name = {1: "总督", 2: "提督", 3: "舰长"}.get(self.guard_min_level, "舰长")
            return f"需要开通{name}才能点歌"
        if self.mode == "any_paid":
            return "送任意付费礼物即可点歌"
        if self.min_coin > 0:
            return f"送满 {self.min_coin} 瓜子（{self.min_coin / 1000:.0f} 元）即可点歌"
        return "需要先送礼物才能点歌"

    def _block(self, c: Contribution | None, reason: str, hint: str,
               remaining: int | None = None) -> GateDecision:
        self.blocks += 1
        self.recent_blocks.append({
            "at": time.time(),
            "user": (c.name if c else "未知"),
            "uid": (c.uid if c else 0),
            "reason": reason,
        })
        del self.recent_blocks[:-30]
        return GateDecision(False, reason=reason, hint=hint,
                            remaining=remaining, contribution=c)

    # ---------------------------------------------------------------- 展示
    def status(self) -> dict[str, Any]:
        top = sorted(self.users.values(), key=lambda x: -x.total_coin)[:10]
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "min_coin": self.min_coin,
            "window_seconds": self.window_seconds,
            "require_paid": self.require_paid,
            "guard_always_ok": self.guard_always_ok,
            "guard_min_level": self.guard_min_level,
            "sc_always_ok": self.sc_always_ok,
            "known_users": len(self.users),
            "paid_gifts": self.seen_gifts,
            "free_gifts": self.seen_free_gifts,
            "allowed": self.allows,
            "blocked": self.blocks,
            "top": [
                {
                    "name": x.name, "uid": x.uid, "coin": x.total_coin,
                    "spent": x.spent_coin,
                    "remaining": x.total_coin - x.spent_coin,
                    "guard": x.guard_level,
                    "gifts": x.gifts,
                    "last_at": x.last_at,
                }
                for x in top
            ],
            "recent_blocks": list(reversed(self.recent_blocks[-8:])),
        }

    def reset(self) -> None:
        """清空所有贡献记录（主播换规则时可能想重新算）。"""
        self.users.clear()
        self.recent_blocks.clear()
        self.seen_gifts = 0
        self.seen_free_gifts = 0
        self.blocks = 0
        self.allows = 0
