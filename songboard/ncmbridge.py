r"""网易云播放队列桥接（可选增强）。

背景：网易云的"加入歌单"和"播放队列"是两回事——实测往歌单加歌**不会**改变
播放队列，队列只在客户端自己动作时才重建。所以只写歌单的话，主播还得手动去点。

这条路怎么来的：上游 AwooMusicBot 早期用 `orpheus_ipc` 共享内存 + `WM 0x8001`
投递 `{cmd:'playingList',type:'addToNext',...}`，但那个通道在新版客户端上已经
不工作了（实测连非法命令都返回同样的结果，说明没有接收方在解析）。现在的做法是
注入 `AwooNcmCefBridge.dll` 进 cloudmusic.exe，用 CEF DevTools 在页面里
**执行同一段 JS**，走命名管道通信。payload 一直是对的，坏的是投递通道。

本模块只做管道 I/O 和进程发现，**不负责注入**（注入见 tools/inject_bridge.py）。
所有失败都降级为"不可用"，绝不影响点歌板主流程。

协议（源自上游 native/Netease/AwooNcmCefBridge.cpp）：
  命令管道  \\.\pipe\AwooNcmCefBridge-v1-{pid}
  事件管道  \\.\pipe\AwooNcmCefBridge-events-v1-{pid}
  命令      HELLO 1 / PAUSE / RESUME / PLAY <id> / ADD_NEXT <id>
            GET_TRACK_EVENT / GET_DEVTOOLS_DIAGNOSTICS
  每条命令一个连接，响应以 \\n 结尾。
"""
from __future__ import annotations

import base64
import ctypes
import json
import subprocess
import time
from ctypes import wintypes
from typing import Any

# ---------------------------------------------------------------- Win32
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_PIPE_BUSY = 231
ERROR_FILE_NOT_FOUND = 2

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_k32.WaitNamedPipeW.restype = wintypes.BOOL
_k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k32.ReadFile.restype = wintypes.BOOL
_k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k32.WriteFile.restype = wintypes.BOOL
_k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_EnumWindows = _user32.EnumWindows
_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                      wintypes.LPARAM)
_GetWindowThreadProcessId = _user32.GetWindowThreadProcessId
_GetClassNameW = _user32.GetClassNameW

#: 桥宿主窗口类名：ResolveLiveHostObject() 找的就是它
HOST_WINDOW_CLASS = "OrpheusBrowserHost"


class BridgeUnavailable(RuntimeError):
    """桥 DLL 没注入、还没就绪，或管道连不上。调用方应静默降级。"""


# ---------------------------------------------------------------- 进程发现
def find_cloudmusic_pids() -> list[int]:
    """当前所有 cloudmusic.exe 的 pid，升序。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq cloudmusic.exe",
             "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10,
        ).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "cloudmusic.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return sorted(pids)


def has_host_window(pid: int) -> bool:
    """该进程是否持有 OrpheusBrowserHost 窗口（即真正能注入的那个）。"""
    found = []

    def cb(hwnd, _lparam):
        wpid = wintypes.DWORD()
        _GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid:
            return True
        buf = ctypes.create_unicode_buffer(256)
        _GetClassNameW(hwnd, buf, 256)
        if buf.value == HOST_WINDOW_CLASS:
            found.append(hwnd)
            return False
        return True

    try:
        _EnumWindows(_EnumWindowsProc(cb), 0)
    except Exception:
        return False
    return bool(found)


def find_main_pid() -> int | None:
    """找出应该注入/已注入桥的那个主进程 pid。"""
    for pid in find_cloudmusic_pids():
        if has_host_window(pid):
            return pid
    # 桥已注入但窗口枚举失败时，退而求其次：看谁有管道
    for pid in find_cloudmusic_pids():
        if BridgeClient(pid).pipe_exists():
            return pid
    return None


# ---------------------------------------------------------------- 客户端
class BridgeClient:
    """命名管道客户端。每次调用一个连接，失败抛 BridgeUnavailable。"""

    def __init__(self, pid: int, timeout: float = 3.0) -> None:
        self.pid = int(pid)
        self.timeout = float(timeout)

    @property
    def command_pipe(self) -> str:
        return rf"\\.\pipe\AwooNcmCefBridge-v1-{self.pid}"

    @property
    def event_pipe(self) -> str:
        return rf"\\.\pipe\AwooNcmCefBridge-events-v1-{self.pid}"

    def pipe_exists(self) -> bool:
        try:
            handle = _k32.CreateFileW(
                self.command_pipe, GENERIC_READ | GENERIC_WRITE, 0, None,
                OPEN_EXISTING, 0, None)
        except Exception:
            return False
        if handle == INVALID_HANDLE_VALUE:
            return False
        _k32.CloseHandle(handle)
        return True

    def _open(self, pipe_name: str, timeout: float | None = None):
        deadline = time.monotonic() + (self.timeout if timeout is None
                                       else timeout)
        while True:
            handle = _k32.CreateFileW(pipe_name, GENERIC_READ | GENERIC_WRITE,
                                      0, None, OPEN_EXISTING, 0, None)
            if handle != INVALID_HANDLE_VALUE:
                return handle
            err = ctypes.get_last_error()
            # 桥的管道服务器每处理完一条命令就 CloseHandle 再重建，
            # 所以两次事务之间会有一个极短的空档（ERROR_FILE_NOT_FOUND）。
            # 这不是错误，重试即可；只有一直不存在才算真的不可用。
            if err not in (ERROR_PIPE_BUSY, ERROR_FILE_NOT_FOUND):
                raise BridgeUnavailable(
                    f"管道不可用（WinError {err}）：{pipe_name}")
            if time.monotonic() >= deadline:
                raise BridgeUnavailable(
                    f"管道不存在或繁忙超时（WinError {err}）：{pipe_name}")
            _k32.WaitNamedPipeW(pipe_name, 120)
            time.sleep(0.05)

    def _transact(self, payload: bytes, pipe_name: str | None = None,
                  timeout: float | None = None) -> str:
        handle = self._open(pipe_name or self.command_pipe, timeout)
        try:
            written = wintypes.DWORD(0)
            buf = ctypes.create_string_buffer(payload, len(payload))
            if not _k32.WriteFile(handle, buf, len(payload),
                                  ctypes.byref(written), None):
                raise BridgeUnavailable(
                    f"写管道失败 WinError={ctypes.get_last_error()}")
            _k32.FlushFileBuffers(handle)
            out = ctypes.create_string_buffer(8192)
            n = wintypes.DWORD(0)
            if not _k32.ReadFile(handle, out, 8191, ctypes.byref(n), None):
                raise BridgeUnavailable(
                    f"读管道失败 WinError={ctypes.get_last_error()}")
            return out.raw[:n.value].decode("utf-8", "replace").strip()
        finally:
            _k32.CloseHandle(handle)

    # ---------- 命令 ----------
    def hello(self) -> str:
        return self._transact(b"HELLO 1")

    def ready(self) -> bool:
        try:
            return self.hello().startswith("OK READY")
        except BridgeUnavailable:
            return False

    def pause(self) -> str:
        return self._transact(b"PAUSE")

    def resume(self) -> str:
        return self._transact(b"RESUME")

    def play(self, song_id: int | str) -> str:
        return self._transact(f"PLAY {int(song_id)}".encode())

    def add_next(self, song_id: int | str) -> str:
        """把曲目插到**当前播放曲目之后**（不改动歌单内容）。"""
        return self._transact(f"ADD_NEXT {int(song_id)}".encode())

    def track_event(self) -> str:
        return self._transact(b"GET_TRACK_EVENT")

    def diagnostics(self) -> str:
        return self._transact(b"GET_DEVTOOLS_DIAGNOSTICS")

    def describe(self) -> str:
        try:
            raw = self.hello()
        except BridgeUnavailable as exc:
            return f"桥未连接（{exc}）"
        if raw.startswith("OK READY"):
            return f"桥就绪（{raw}）"
        if raw.startswith("REFUSED"):
            return f"桥被拒绝：{raw[7:].strip()}"
        if raw.startswith("WAIT"):
            return f"桥等待宿主：{raw[4:].strip()}"
        return f"桥响应异常：{raw!r}"


# ---------------------------------------------------------------- 事件解码
def decode_track_event(raw: str) -> dict[str, Any] | None:
    """把 GET_TRACK_EVENT 的响应解成状态字典。

    响应形如  `OK EVENT <seq> <len> <base64>`，base64 里是一个 JSON：
        {"version":2,"type":"redux:state","title":"网易云音乐",
         "trackId":"65592","name":"单车","artist":"陈奕迅","album":"...",
         "coverUrl":"http://...","nextTrackId":"66282","nextName":"浮夸",...}
    解析失败返回 None。
    """
    if not raw:
        return None
    parts = raw.split()
    if len(parts) < 5 or parts[0] != "OK" or parts[1] != "EVENT":
        return None
    blob = parts[-1]
    try:
        data = json.loads(base64.b64decode(blob).decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "track_id": str(data.get("trackId") or "") or None,
        "name": data.get("name") or "",
        "artist": data.get("artist") or "",
        "album": data.get("album") or "",
        "cover": data.get("coverUrl") or "",
        "next_track_id": str(data.get("nextTrackId") or "") or None,
        "next_name": data.get("nextName") or "",
        "next_artist": data.get("nextArtist") or "",
        "next_album": data.get("nextAlbum") or "",
        "sequence": parts[2],
    }


# ---------------------------------------------------------------- 驱动外观
class NeteaseBridge:
    """高层封装：只管"能不能用"和"插下一首"，其余交给调用方决定。"""

    def __init__(self, enabled: bool = False, timeout: float = 3.0) -> None:
        self.enabled = bool(enabled)
        self.timeout = float(timeout)
        self.pid: int | None = None
        self.last_error = ""
        self.inserted = 0
        self.played = 0
        self._last_probe = 0.0
        self._probe_ok = False
        self._probe_msg = "未探测"

    # ---------- 状态 ----------
    def client(self) -> BridgeClient | None:
        if self.pid is None:
            self.pid = find_main_pid()
        if self.pid is None:
            return None
        return BridgeClient(self.pid, self.timeout)

    def available(self, *, refresh_after: float = 30.0) -> bool:
        """能不能用。结果缓存 refresh_after 秒，避免每轮都开管道。"""
        if not self.enabled:
            return False
        now = time.time()
        if now - self._last_probe < refresh_after:
            return self._probe_ok
        self._last_probe = now
        c = self.client()
        if c is None:
            self._probe_ok = False
            self._probe_msg = "找不到网易云主进程"
            return False
        try:
            raw = c.hello()
        except BridgeUnavailable as exc:
            self._probe_ok = False
            self._probe_msg = f"桥不可用：{exc}"
            return False
        if raw.startswith("OK READY"):
            self._probe_ok = True
            self._probe_msg = raw
            return True
        self._probe_ok = False
        self._probe_msg = raw or "桥无响应"
        return False

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": self._probe_ok,
            "message": self._probe_msg,
            "pid": self.pid,
            "inserted": self.inserted,
            "played": self.played,
            "last_error": self.last_error,
        }

    # ---------- 动作 ----------
    def describe_safe(self) -> str:
        """一句话描述桥状态，绝不抛异常（给日志/UI 用）。"""
        c = self.client()
        if c is None:
            return self._probe_msg or "找不到网易云主进程"
        try:
            return c.describe()
        except Exception as exc:  # noqa: BLE001 - 描述失败不该影响主流程
            return f"桥状态读取失败：{exc!r}"

    def insert_next(self, song_id: int | str) -> tuple[bool, str]:
        """把曲目插到播放队列的"下一首"。返回 (是否成功, 说明)。"""
        if not self.available():
            return False, self._probe_msg
        c = self.client()
        if c is None:
            return False, "找不到网易云主进程"
        try:
            resp = c.add_next(song_id)
        except BridgeUnavailable as exc:
            self.last_error = str(exc)
            self._probe_ok = False
            return False, f"插入失败：{exc}"
        if resp.startswith("OK"):
            self.inserted += 1
            return True, f"已插入播放队列下一首（{resp}）"
        self.last_error = resp
        return False, f"插入被拒：{resp}"

    def play_now(self, song_id: int | str) -> tuple[bool, str]:
        """立刻播放指定曲目。"""
        if not self.available():
            return False, self._probe_msg
        c = self.client()
        if c is None:
            return False, "找不到网易云主进程"
        try:
            resp = c.play(song_id)
        except BridgeUnavailable as exc:
            self.last_error = str(exc)
            self._probe_ok = False
            return False, f"播放失败：{exc}"
        if resp.startswith("OK"):
            self.played += 1
            return True, f"已开始播放（{resp}）"
        self.last_error = resp
        return False, f"播放被拒：{resp}"

    def ensure_next(self, song_id: int | str) -> tuple[bool, str, str]:
        """保证指定曲目是"下一首"，但**已经在了就不重复插**。

        返回 (是否可用, 说明, 动作)，动作 ∈ {"inserted", "already", "failed"}。

        为什么要先看再插：`addToNext` 是"移到下一首"语义，
        对同一首歌反复调用虽然幂等，但会一直操作播放器；
        而且我们需要那个"已经是下一首了"的观测结果来推进队头。
        """
        state = self.now_playing()
        sid = str(int(song_id))
        if state is not None and str(state.get("next_track_id") or "") == sid:
            return True, f"《{state.get('next_name') or sid}》已经在下一首", "already"
        ok, msg = self.insert_next(song_id)
        return ok, msg, "inserted" if ok else "failed"

    def now_playing(self) -> dict[str, Any] | None:
        """读网易云当前播放状态（含下一首）。读不到返回 None。"""
        c = self.client()
        if c is None or not self.available():
            return None
        try:
            return decode_track_event(c.track_event())
        except BridgeUnavailable:
            return None
