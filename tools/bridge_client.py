r"""AwooNcmCefBridge 的命名管道客户端（纯 Python，无第三方依赖）。

协议（源自上游 native/Netease/AwooNcmCefBridge.cpp）：
  命令管道  \\.\pipe\AwooNcmCefBridge-v1-{pid}         每连接一条命令，响应以 \n 结尾
  事件管道  \\.\pipe\AwooNcmCefBridge-events-v1-{pid}   WAIT_EVENT <seq> <timeout_ms>

命令：
  HELLO 1                  -> "OK READY cef=... route=internal-devtools events=..."
                              "REFUSED <原因>" 表示校验失败
                              "WAIT <原因>"    表示桥在等宿主对象
  PAUSE / RESUME           -> {cmd:'pause'} / {cmd:'resume'}
  PLAY <songId>            -> {cmd:'play',type:'song',id:'<id>'}
  ADD_NEXT <songId>        -> {cmd:'playingList',type:'addToNext',value:'<id>'}
  GET_TRACK_EVENT          -> 最近一次播放事件
  GET_DEVTOOLS_DIAGNOSTICS -> DevTools 诊断信息

本模块只做管道 I/O，不做任何注入。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_PIPE_BUSY = 231

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
]
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_k32.WaitNamedPipeW.restype = wintypes.BOOL
_k32.ReadFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
_k32.ReadFile.restype = wintypes.BOOL
_k32.WriteFile.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
_k32.WriteFile.restype = wintypes.BOOL
_k32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


class BridgeUnavailable(RuntimeError):
    """桥 DLL 未注入，或命令管道不存在。"""


class BridgeClient:
    #: 上游桥支持的 CEF 版本（硬闸门必须与目标 libcef 一致）
    EXPECTED_CEF = "91.2.2+4472.169"

    def __init__(self, pid: int, timeout: float = 8.0):
        self.pid = int(pid)
        self.timeout = float(timeout)

    @property
    def command_pipe(self) -> str:
        return rf"\\.\pipe\AwooNcmCefBridge-v1-{self.pid}"

    @property
    def event_pipe(self) -> str:
        return rf"\\.\pipe\AwooNcmCefBridge-events-v1-{self.pid}"

    # ------------------------------------------------------------------ 底层
    def _open(self, pipe_name: str, timeout: float | None = None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        last_err = 0
        while True:
            handle = _k32.CreateFileW(
                pipe_name, GENERIC_READ | GENERIC_WRITE, 0, None,
                OPEN_EXISTING, 0, None,
            )
            if handle != INVALID_HANDLE_VALUE:
                return handle
            last_err = ctypes.get_last_error()
            if last_err != ERROR_PIPE_BUSY:
                raise BridgeUnavailable(
                    f"无法连接管道 {pipe_name}（WinError {last_err}）。"
                    "桥 DLL 可能未注入或已退出。"
                )
            if time.monotonic() >= deadline:
                raise BridgeUnavailable(f"管道 {pipe_name} 一直忙（超时）")
            _k32.WaitNamedPipeW(pipe_name, 200)

    def _transact(self, payload: bytes, pipe_name: str | None = None,
                  timeout: float | None = None, read: bool = True) -> str:
        pipe_name = pipe_name or self.command_pipe
        handle = self._open(pipe_name, timeout)
        try:
            written = wintypes.DWORD(0)
            buf = ctypes.create_string_buffer(payload, len(payload))
            if not _k32.WriteFile(handle, buf, len(payload),
                                  ctypes.byref(written), None):
                raise BridgeUnavailable(
                    f"写入管道失败（WinError {ctypes.get_last_error()}）")
            _k32.FlushFileBuffers(handle)
            if not read:
                return ""
            out = ctypes.create_string_buffer(4096)
            n = wintypes.DWORD(0)
            if not _k32.ReadFile(handle, out, 4095, ctypes.byref(n), None):
                raise BridgeUnavailable(
                    f"读取管道失败（WinError {ctypes.get_last_error()}）")
            self.last_error = ctypes.get_last_error()
            return out.raw[: n.value].decode("utf-8", "replace").strip()
        finally:
            _k32.CloseHandle(handle)

    # ------------------------------------------------------------------ 命令
    def hello(self) -> str:
        """握手。返回原始响应字符串：
        'OK READY ...' / 'WAIT ...' / 'REFUSED ...'
        """
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

    def play(self, song_id) -> str:
        return self._transact(f"PLAY {int(song_id)}".encode())

    def add_next(self, song_id) -> str:
        return self._transact(f"ADD_NEXT {int(song_id)}".encode())

    def get_track_event(self) -> str:
        return self._transact(b"GET_TRACK_EVENT")

    def diagnostics(self) -> str:
        return self._transact(b"GET_DEVTOOLS_DIAGNOSTICS")

    def wait_event(self, after_sequence: int, timeout_ms: int = 30000) -> str:
        timeout_ms = max(1000, min(60000, int(timeout_ms)))
        payload = f"WAIT_EVENT {int(after_sequence)} {timeout_ms}".encode()
        # 事件管道会阻塞到事件到达或超时，读超时给足余量
        return self._transact(payload, pipe_name=self.event_pipe,
                              timeout=timeout_ms / 1000.0 + 5.0)

    # ------------------------------------------------------------------ 便捷
    def describe(self) -> str:
        """把 HELLO 响应整理成一句话，便于日志/UI 显示。"""
        try:
            raw = self.hello()
        except BridgeUnavailable as exc:
            return f"桥不可用：{exc}"
        if raw.startswith("OK READY"):
            return f"桥就绪：{raw}"
        if raw.startswith("REFUSED"):
            return f"桥拒绝加载：{raw[len('REFUSED'):].strip()}"
        if raw.startswith("WAIT"):
            return f"桥等待宿主：{raw[len('WAIT'):].strip()}"
        return f"未知响应：{raw!r}"


def find_cloudmusic_pids() -> list[tuple[int, str]]:
    """返回 [(pid, exe路径)]，按 pid 升序。用于找出应该注入的主进程。"""
    import subprocess
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq cloudmusic.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "cloudmusic.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return sorted(pids)


if __name__ == "__main__":
    import sys
    pids = find_cloudmusic_pids()
    print("cloudmusic pids:", pids)
    for pid in pids:
        c = BridgeClient(pid)
        print(f"  pid={pid}: {c.describe()}")
