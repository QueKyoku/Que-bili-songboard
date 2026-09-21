"""判断"歌播完了没有"——三个只读信号源，按可靠性排序。

1) 系统媒体会话 GSMTC（最准，有进度条）
   Edge / Chrome / Spotify / PotPlayer 注册；**网易云 PC 客户端不注册**。

2) 窗口标题（网易云的兜底方案，有歌名没进度）
   实测网易云客户端的窗口：
       class=OrpheusBrowserHost  title=苦瓜 - 陈奕迅     ← 主窗口，就是当前曲目
       class=icon                title=苦瓜 - 陈奕迅
       class=MiniPlayer / DesktopLyrics                  ← 迷你播放器 / 桌面歌词
   读标题是纯只读操作，不用注入、不用挂钩子。

3) 时长计时（有歌名之后，靠"播了多久"兜底判完）

只用前两个拿到"现在是哪首歌"，用第 3 个判断"这首完了没"。
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
PS_SCRIPT = ROOT / "tools" / "gsm_probe.ps1"

POLL_INTERVAL = 2.0

# 网易云的窗口类名（实测）
NETEASE_WINDOW_CLASSES = ("OrpheusBrowserHost", "icon", "MiniPlayer", "DesktopLyrics")
NETEASE_PROCESS = "cloudmusic"

# ---------------------------------------------------------------- 文本匹配
_PUNCT = re.compile(r"[\s\-_·・.,，。!！?？:：;；'\"“”‘’()（）\[\]【】<>《》/\\|~～+&]+")
# 括号里的通常是版本标注：Live / 翻唱 / 伴奏 / 完整版…
_BRACKET = re.compile(r"[（(\[【][^）)\]】]*[）)\]】]")


def normalize(text: str) -> str:
    """归一化：统一 Unicode 形式、小写、去括号标注、去标点空格、去常见后缀。

    ⚠️ 第一步的 Unicode 规范化不能省。日文的浊音有**两种写法**：
    预组合的「ず」是 U+305A，组合形式是 U+3059 U+3099 —— 看起来一模一样，
    字节却不同。网易云返回来的是组合形式，用户打字/点歌用的是预组合形式，
    不统一的话相似度只有 0.33（实测「すずめ」vs「すずめ feat.十明」），
    播放对齐就认不出是同一首。带浊音的日文歌全中招。
    """
    t = unicodedata.normalize("NFC", text or "").lower()
    t = _BRACKET.sub("", t)
    t = _PUNCT.sub("", t)
    for suffix in ("official", "mv", "live", "完整版", "高音质", "无损", "cover", "翻唱"):
        t = t.replace(suffix, "")
    return t.strip()


def similarity(a: str, b: str) -> float:
    """0~1 的相似度：归一化后相等 1.0，互相包含 0.85，否则字符重合度。"""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.85
    sa, sb = set(na), set(nb)
    inter = len(sa & sb)
    return 2 * inter / (len(sa) + len(sb)) if sa and sb else 0.0


def split_title(raw: str) -> tuple[str, str]:
    """把 "苦瓜 - 陈奕迅" 拆成 (歌名, 艺人)。容忍 " - " / "-" / "—"。"""
    t = (raw or "").strip()
    for sep in (" - ", " – ", " — ", "-", "–", "—"):
        if sep in t:
            left, _, right = t.partition(sep)
            left, right = left.strip(), right.strip()
            if left and right:
                return left, right
    return t, ""


@dataclass
class MediaInfo:
    app: str = ""
    title: str = ""
    artist: str = ""
    playing: bool = False
    position: float = 0.0
    duration: float = 0.0
    source: str = ""            # gsm | window

    def key(self) -> str:
        return f"{normalize(self.title)}|{normalize(self.artist)}"

    @property
    def has_progress(self) -> bool:
        return self.duration > 0

    @property
    def remaining(self) -> float:
        """剩余秒数；没有时长信息时返回 -1（调用方要判 <0）。"""
        if self.duration <= 0:
            return -1.0
        return max(self.duration - self.position, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "app": self.app, "title": self.title, "artist": self.artist,
            "playing": self.playing, "position": round(self.position, 1),
            "duration": round(self.duration, 1), "source": self.source,
            "remaining": round(self.remaining, 1),
        }


# ---------------------------------------------------------------- 解析
_LINE_APP = re.compile(r"^\s*APP\s*:\s*(?P<app>.+?)\s*$")
_LINE_STATE = re.compile(
    r"^\s*STATE\s*:\s*(?P<state>\w+)\s+POS\s*:\s*(?P<pos>-?\d+)\s+DUR\s*:\s*(?P<dur>-?\d+)\s*$"
)
# TITLE 与 ARTIST 都可能为空，ARTIST 也可能整段缺失；只要有一个非空才算有效曲目
_LINE_TITLE = re.compile(
    r"^\s*TITLE\s*:\s*(?P<title>.*?)\s*(?:ARTIST\s*:\s*(?P<artist>.*?))?\s*$"
)

# 没有曲目信息时的占位文本，不能当成歌名
_TITLE_NOISE = {"", "-", "artist:", "title:", "未知", "unknown"}


def _clean_field(value: str | None) -> str:
    v = (value or "").strip()
    return "" if v.lower() in _TITLE_NOISE else v


def _usable(info: MediaInfo) -> bool:
    """这个会话值不值得保留。

    只要有**标题**（能判断是哪首歌）或**时长**（能判断播完没有）就有价值。
    实测网易云网页版在 Edge 里就是"有时长、没标题"，不能因为没标题就丢掉。
    """
    return bool(info.title or info.artist) or info.duration > 0


def parse_gsm_output(text: str) -> list[MediaInfo]:
    """解析探测进程输出，一段会话 = APP / STATE / TITLE 三行。"""
    items: list[MediaInfo] = []
    cur: MediaInfo | None = None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        m = _LINE_APP.match(line)
        if m:
            if cur and _usable(cur):
                items.append(cur)
            cur = MediaInfo(app=m.group("app"), source="gsm")
            continue
        if cur is None:
            continue
        m = _LINE_STATE.match(line)
        if m:
            cur.playing = m.group("state").lower() == "playing"
            cur.position = float(m.group("pos"))
            cur.duration = float(m.group("dur"))
            continue
        m = _LINE_TITLE.match(line)
        if m:
            cur.title = _clean_field(m.group("title"))
            cur.artist = _clean_field(m.group("artist"))
    if cur and _usable(cur):
        items.append(cur)
    return items


def pick_session(items: Iterable[MediaInfo], prefer: tuple[str, ...] = ()) -> MediaInfo | None:
    """挑一个最有用的会话。

    优先级：
      1) prefer 里列出的播放器，且有曲名
      2) 任何有曲名的会话
      3) 任何有进度（时长>0）的会话 —— 网易云网页版就是这样：
         进度是全的，但 title 是空的。**不能因为没标题就丢掉**。
    """
    usable = list(items)
    with_title = [i for i in usable if i.title]
    for key in prefer:
        for i in with_title:
            if key.lower() in i.app.lower() and i.playing:
                return i
    for i in with_title:
        if i.playing:
            return i
    if with_title:
        return with_title[0]
    with_progress = [i for i in usable if i.has_progress]
    for key in prefer:
        for i in with_progress:
            if key.lower() in i.app.lower() and i.playing:
                return i
    if with_progress:
        return with_progress[0]
    # 没标题也没进度：只有在正在播放时才勉强算数
    for i in usable:
        if i.playing:
            return i
    return None


# ---------------------------------------------------------------- GSMTC 读取
def read_sessions(timeout: float = 12) -> list[MediaInfo]:
    """读系统媒体会话。失败返回空列表，不抛异常。"""
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe or not PS_SCRIPT.exists():
        return []
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS_SCRIPT)],
            capture_output=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[media] 读取媒体会话失败：{exc!r}")
        return []
    out = _decode(proc.stdout)
    items = parse_gsm_output(out)
    if not items:
        items = parse_gsm_output(_decode(proc.stdout, fallback=True))
    return items


def _decode(raw: bytes | str | None, fallback: bool = False) -> str:
    """PowerShell 输出可能是 UTF-8，也可能被系统代码页（cp936）搅过，两种都试。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if fallback:
        try:
            return raw.decode("cp936", "ignore").encode("latin1", "ignore").decode("utf-8", "ignore")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp936", "replace")


# ---------------------------------------------------------------- 窗口标题读取
_gdi32 = ctypes.windll.gdi32 if sys.platform == "win32" else None
_user32 = ctypes.windll.user32 if sys.platform == "win32" else None


def read_netease_title(processes: tuple[str, ...] = (NETEASE_PROCESS,)) -> MediaInfo | None:
    """读网易云客户端窗口标题，拿到当前曲目。

    纯只读：EnumWindows + GetWindowText，不注入、不挂钩子、不改动客户端。
    """
    if sys.platform != "win32":
        return None
    text = _read_window_titles(processes)
    for cls, title in text:
        if cls in NETEASE_WINDOW_CLASSES and title and title not in ("迷你播放器", "桌面歌词"):
            song, artist = split_title(title)
            if song:
                return MediaInfo(app=processes[0], title=song, artist=artist,
                                 playing=True, source="window")
    return None


_WIN_TITLES_PS = r"""
$ErrorActionPreference='SilentlyContinue'
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$procs = @({PROCS})
$pids = @()
foreach ($p in $procs) { $pids += (Get-Process -Name $p).Id }
Add-Type -Language CSharp @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class WT {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  public static List<string> Go(int[] pids) {
    var set = new HashSet<uint>();
    foreach (var p in pids) set.Add((uint)p);
    var res = new List<string>();
    EnumWindows((h, l) => {
      uint pid; GetWindowThreadProcessId(h, out pid);
      if (!set.Contains(pid)) return true;
      var t = new StringBuilder(512); GetWindowTextW(h, t, 512);
      var c = new StringBuilder(256); GetClassNameW(h, c, 256);
      res.Add(c.ToString() + "\t" + t.ToString());
      return true;
    }, IntPtr.Zero);
    return res;
  }
}
"@
foreach ($line in [WT]::Go($pids)) { Write-Output $line }
"""


def _read_window_titles(processes: tuple[str, ...]) -> list[tuple[str, str]]:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return []
    script = _WIN_TITLES_PS.replace("{PROCS}", ", ".join(f"'{p}'" for p in processes))
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[media] 读窗口标题失败：{exc!r}")
        return []
    out = _decode(proc.stdout)
    pairs = []
    for line in out.splitlines():
        if "\t" in line:
            cls, _, title = line.partition("\t")
            pairs.append((cls.strip(), title.strip()))
    return pairs


# ---------------------------------------------------------------- 统一入口
def read_now_playing(
    prefer: tuple[str, ...] = ("msedge", "chrome", "firefox", "spotify"),
) -> tuple[MediaInfo | None, MediaInfo | None]:
    """返回 (曲目信息, 进度信息)。

    两者刻意分开，因为它们的可靠性完全不同：

    · 曲目信息（用来判断"换歌了没有"）——**必须带标题**。
      浏览器会话常常没有标题（实测网易云网页版在 Edge 里就是空的），
      拿它当曲目来源会把"看 B 站视频"误判成"换歌了"。
      网易云客户端的窗口标题是可靠来源。

    · 进度信息（用来精确判断"播完了没有"）——只要有时长就算数。
      但没有标题时**无法确认它是音乐还是视频**，所以调用方要谨慎采信
      （见 config 的 trust_browser_progress）。
    """
    sessions = read_sessions()

    # 曲目来源：优先"会话里带标题的"，否则读网易云窗口标题
    title_info = pick_session(sessions, prefer) if any(s.title for s in sessions) else None
    if title_info is None:
        title_info = read_netease_title()

    # 进度来源：带标题的会话优先，其次任意有时长且正在播放的会话
    timeline = title_info if (title_info is not None and title_info.has_progress) else None
    if timeline is None:
        with_progress = pick_all_with_progress(sessions, prefer)
        timeline = with_progress[0] if with_progress else None

    return title_info, timeline


def pick_all_with_progress(
    items: Iterable[MediaInfo], prefer: tuple[str, ...] = (),
) -> list[MediaInfo]:
    """按 prefer 排序，挑出所有"正在播放且有时长"的会话。"""
    prog = [i for i in items if i.has_progress and i.playing]
    ordered: list[MediaInfo] = []
    for key in prefer:
        for i in prog:
            if key.lower() in i.app.lower() and i not in ordered:
                ordered.append(i)
    for i in prog:
        if i not in ordered:
            ordered.append(i)
    return ordered


def available() -> bool:
    title, timeline = read_now_playing()
    return title is not None or timeline is not None


# ---------------------------------------------------------------- 播放队列（本地缓存）
def read_play_queue() -> list[dict[str, Any]]:
    """读网易云客户端的播放队列缓存（WebData/file/playingList）。

    纯只读。返回按播放顺序排好的列表，每条含：
        track_id / name / played / order

    `played` 来自客户端自己打的 isPlayedOnce 标记，用来判断
    "歌单里哪些歌已经放过了、可以清掉"。
    """
    import json
    import os
    import time  # noqa: F401  (保留便于调试)

    path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Netease", "CloudMusic",
                        "WebData", "file", "playingList")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            data = json.loads(fh.read())
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("list") or []
    if not isinstance(items, list):
        return []

    def order_key(it: Any) -> float:
        v = (it or {}).get("displayOrder")
        return v if isinstance(v, (int, float)) else 10 ** 9

    out: list[dict[str, Any]] = []
    for it in sorted(items, key=order_key):
        if not isinstance(it, dict):
            continue
        tid = it.get("trackId") or it.get("id")
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            continue
        track = it.get("track") if isinstance(it.get("track"), dict) else {}
        name = str((track or {}).get("name") or "")
        artists = (track or {}).get("ar") or (track or {}).get("artists") or []
        artist = "/".join(a.get("name", "") for a in artists
                          if isinstance(a, dict)) if isinstance(artists, list) else ""
        out.append({
            "track_id": tid, "name": name, "artist": artist,
            "played": bool(it.get("isPlayedOnce")),
            "order": it.get("displayOrder"),
        })
    return out


def played_track_ids() -> set[int]:
    """队列缓存里被标记为"已播过"的 track id 集合。"""
    return {q["track_id"] for q in read_play_queue() if q["played"]}


if __name__ == "__main__":  # pragma: no cover
    info = read_now_playing()
    print(json.dumps(info.to_dict() if info else {"title": None}, ensure_ascii=False, indent=2))
    print("窗口标题原始值：")
    for cls, title in _read_window_titles((NETEASE_PROCESS,)):
        if title:
            print(f"  {cls:24s} {title}")
