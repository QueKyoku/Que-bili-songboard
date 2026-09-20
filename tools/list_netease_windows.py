"""列出 cloudmusic 各进程与其顶层窗口，找出持有 OrpheusBrowserHost 的主进程。

只读。用于决定把桥 DLL 注入到哪个 pid。
"""

from __future__ import annotations

import ctypes
import subprocess
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
EnumWindows = user32.EnumWindows
EnumWindowsProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
GetClassNameW = user32.GetClassNameW
GetWindowTextW = user32.GetWindowTextW
IsWindowVisible = user32.IsWindowVisible
GetWindow = user32.GetWindow
GW_OWNER = 4


def windows_of_pid(pid: int) -> list[tuple[int, str, str, bool]]:
    result: list[tuple[int, str, str, bool]] = []

    def cb(hwnd, _lparam):
        wpid = wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid:
            return True
        cls = ctypes.create_unicode_buffer(256)
        GetClassNameW(hwnd, cls, 256)
        title = ctypes.create_unicode_buffer(512)
        GetWindowTextW(hwnd, title, 512)
        own = GetWindow(hwnd, GW_OWNER)
        result.append((hwnd, cls.value, title.value,
                       bool(IsWindowVisible(hwnd)) and not own))
        return True

    EnumWindows(EnumWindowsProc(cb), 0)
    return result


def pids() -> list[int]:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq cloudmusic.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout
    vals = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "cloudmusic.exe":
            try:
                vals.append(int(parts[1]))
            except ValueError:
                pass
    return sorted(vals)


def parent_of(pid: int) -> int | None:
    try:
        out = subprocess.run(
            ["wmic", "process", "where", f"ProcessId={pid}",
             "get", "ParentProcessId", "/value"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout
        for line in out.splitlines():
            if "ParentProcessId=" in line:
                return int(line.split("=", 1)[1].strip())
    except Exception:
        pass
    return None


if __name__ == "__main__":
    if not ctypes.windll.shell32.IsUserAnAdmin():
        print("!! 当前非管理员，注入会失败（需要 PROCESS_VM_WRITE 等权限）")
    print(f"{'PID':>7}  {'PPID':>7}  主窗口类 / 标题")
    print("-" * 88)
    for pid in pids():
        ppid = parent_of(pid)
        ws = windows_of_pid(pid)
        if not ws:
            print(f"{pid:>7}  {str(ppid):>7}  (无顶层窗口)")
        for hwnd, cls, title, visible in ws:
            flag = ""
            if cls == "OrpheusBrowserHost":
                flag = "   <<< 主进程候选"
            print(f"{pid:>7}  {str(ppid):>7}  0x{hwnd:X} [{cls}] "
                  f"{title[:52]!r} vis={visible}{flag}")
    print()
    print("说明：桥的 ResolveLiveHostObject() 需要 class=OrpheusBrowserHost "
          "且有 CefBrowserWindow 子窗口。")
