"""把 AwooNcmCefBridge.dll 注入网易云主进程，并等它握手。

为什么要有这个模块：桥是**注入**进去的，网易云一重启就没了。以前只能在命令行
跑 `tools/inject_bridge.py`，可主播开播前手忙脚乱的时候根本不记得，
结果就是"观众点了半天歌，一首都没进播放列表"。所以控制台要能一键重新注入 ——
这个模块就是控制台和命令行**共用**的那一份实现（两处各写一遍必然漂移）。

安全边界（和原来的命令行脚本完全一致，没有扩大）：
  * 只对 cloudmusic.exe 生效，且只挑持有 OrpheusBrowserHost 窗口的那个 pid
  * 注入后立刻用管道 HELLO 验证，桥自报 REFUSED 就算没成
  * 不碰磁盘上任何文件、不 hook 系统调用、不做持久化
"""

from __future__ import annotations

import ctypes
import os
import struct
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .ncmbridge import (BridgeClient, BridgeUnavailable, find_cloudmusic_pids,
                        find_main_pid)

#: 桥 DLL：仓库根目录 bridge/ 下的编译产物（不进仓库，所以要检查它在不在）
DLL_PATH = (Path(__file__).resolve().parent.parent / "bridge"
            / "AwooNcmCefBridge.dll")

PROCESS_CREATE_THREAD = 0x0002
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
WAIT_OBJECT_0 = 0
ERROR_ACCESS_DENIED = 5

ACCESS = (PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION
          | PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE)

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
_k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
_k32.VirtualAllocEx.restype = ctypes.c_void_p
_k32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                               ctypes.c_size_t, wintypes.DWORD]
_k32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t)]
_k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_k32.GetModuleHandleW.restype = wintypes.HMODULE
_k32.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_char_p]
_k32.GetProcAddress.restype = ctypes.c_void_p
_k32.CreateRemoteThread.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_k32.CreateRemoteThread.restype = wintypes.HANDLE
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.GetExitCodeThread.argtypes = [wintypes.HANDLE,
                                   ctypes.POINTER(wintypes.DWORD)]


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 —— 非 Windows 上不该炸
        return False


def pe_arch(path: str | os.PathLike[str]) -> str:
    """读 PE 头判断 DLL 位数 —— x86 的 DLL 注入 x64 进程必然失败。"""
    try:
        with open(path, "rb") as f:
            data = f.read(0x400)
    except OSError:
        return "unknown"
    if data[:2] != b"MZ":
        return "unknown"
    off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[off:off + 4] != b"PE\0\0":
        return "unknown"
    machine = struct.unpack_from("<H", data, off + 4)[0]
    return {0x8664: "x64", 0x14C: "x86", 0xAA64: "arm64"}.get(
        machine, f"0x{machine:X}")


def _is_64bit_process(handle) -> bool:
    wow = wintypes.BOOL()
    if not _k32.IsWow64Process(handle, ctypes.byref(wow)):
        raise OSError(f"IsWow64Process 失败: {ctypes.get_last_error()}")
    return not wow.value


def remote_load_library(pid: int, dll_path: str | os.PathLike[str],
                        free: bool = False) -> dict:
    """在目标进程里调 LoadLibraryW / FreeLibrary。失败抛 OSError。

    标准 Win32 注入四步：OpenProcess → VirtualAllocEx → WriteProcessMemory
    → CreateRemoteThread。
    """
    dll_path = os.path.abspath(str(dll_path))
    info: dict[str, Any] = {"pid": pid, "dll": dll_path, "free": free}

    handle = _k32.OpenProcess(ACCESS, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        # WinError 5 = 拒绝访问：网易云如果是**以管理员身份**启动的，
        # 普通权限的点歌板就注入不进去 —— 这条要单独说清楚，
        # 否则用户只会看到一句"OpenProcess 失败"。
        if err == ERROR_ACCESS_DENIED:
            raise OSError(
                f"打开网易云进程被拒绝（WinError 5）。"
                f"网易云如果是「以管理员身份运行」启动的，"
                f"点歌板也得用管理员身份启动才能注入。")
        raise OSError(f"OpenProcess(pid={pid}) 失败 WinError={err}")
    try:
        info["target_64bit"] = _is_64bit_process(handle)

        path_bytes = (dll_path + "\0").encode("utf-16-le")
        remote_buf = _k32.VirtualAllocEx(
            handle, None, len(path_bytes), MEM_COMMIT | MEM_RESERVE,
            PAGE_READWRITE)
        if not remote_buf:
            raise OSError(f"VirtualAllocEx 失败 WinError={ctypes.get_last_error()}")
        info["remote_buf"] = hex(remote_buf)
        try:
            written = ctypes.c_size_t(0)
            if not _k32.WriteProcessMemory(
                    handle, remote_buf, path_bytes, len(path_bytes),
                    ctypes.byref(written)):
                raise OSError(
                    f"WriteProcessMemory 失败 WinError={ctypes.get_last_error()}")
            info["written"] = written.value

            h_k32 = _k32.GetModuleHandleW("kernel32.dll")
            fn_name = b"FreeLibrary" if free else b"LoadLibraryW"
            fn_addr = _k32.GetProcAddress(h_k32, fn_name)
            if not fn_addr:
                raise OSError(f"找不到 kernel32!{fn_name.decode()}")
            info["fn"] = f"kernel32!{fn_name.decode()}"

            tid = wintypes.DWORD(0)
            thread = _k32.CreateRemoteThread(
                handle, None, 0, fn_addr, remote_buf, 0, ctypes.byref(tid))
            if not thread:
                raise OSError(
                    f"CreateRemoteThread 失败 WinError={ctypes.get_last_error()}")
            info["tid"] = tid.value
            try:
                if _k32.WaitForSingleObject(thread, 15000) != WAIT_OBJECT_0:
                    raise OSError("等待远程线程超时")
                code = wintypes.DWORD(0)
                _k32.GetExitCodeThread(thread, ctypes.byref(code))
                info["return_value"] = code.value
                info["return_hex"] = hex(code.value)
            finally:
                _k32.CloseHandle(thread)
        finally:
            _k32.VirtualFreeEx(handle, remote_buf, 0, MEM_RELEASE)
    finally:
        _k32.CloseHandle(handle)
    return info


def loaded_bridge_modules(pid: int) -> list[str]:
    """该进程里已加载的桥副本路径（卸载时按**实际路径**逐个来）。"""
    import ctypes.wintypes as wt

    TH32CS_SNAPMODULE = 0x00000008
    TH32CS_SNAPMODULE32 = 0x00000010
    MAX_PATH = 260
    INVALID = ctypes.c_void_p(-1).value

    class MODULEENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD),
            ("th32ProcessID", wt.DWORD), ("GlblcntUsage", wt.DWORD),
            ("ProccntUsage", wt.DWORD), ("modBaseAddr", ctypes.c_void_p),
            ("modBaseSize", wt.DWORD), ("hModule", wt.HMODULE),
            ("szModule", wt.WCHAR * 256), ("szExePath", wt.WCHAR * MAX_PATH),
        ]

    _k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    _k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    _k32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32)]
    _k32.Module32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32)]

    snap = _k32.CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == INVALID:
        return []
    found: list[str] = []
    try:
        entry = MODULEENTRY32()
        entry.dwSize = ctypes.sizeof(MODULEENTRY32)
        ok = _k32.Module32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szModule.lower() == "awooncmcefbridge.dll":
                found.append(entry.szExePath)
            ok = _k32.Module32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return found


def check(dll: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """注入前的可行性检查。**不写入任何东西。**

    返回里 blocks 非空就表示现在注入一定失败，每条都是给人看的原因。
    """
    path = Path(dll or DLL_PATH)
    pid = find_main_pid()
    pids = find_cloudmusic_pids()
    result: dict[str, Any] = {
        "dll": str(path),
        "dll_exists": path.exists(),
        "dll_arch": pe_arch(path) if path.exists() else "",
        "admin": is_admin(),
        "cloudmusic_pids": pids,
        "pid": pid,
        "blocks": [],
    }
    if not path.exists():
        result["blocks"].append(
            f"找不到桥 DLL：{path}。它是编译产物（不进仓库），"
            f"先跑一次 tools/build_bridge.ps1 编译。")
    elif result["dll_arch"] != "x64":
        result["blocks"].append(
            f"桥 DLL 是 {result['dll_arch']} 的，网易云是 x64，注入不进去。")
    if not pids:
        result["blocks"].append("网易云客户端没在运行 —— 先打开网易云音乐。")
    elif pid is None:
        result["blocks"].append(
            "找到了 cloudmusic 进程，但都没有 OrpheusBrowserHost 窗口。"
            "可能刚启动还没建好窗口，等一下再试；"
            "或者网易云版本不支持这种方式。")
    return result


def inject(dll: str | os.PathLike[str] | None = None,
           timeout: float = 15.0) -> dict[str, Any]:
    """注入并等桥握手。**永不抛异常** —— 控制台要拿结果去显示。

    返回 {ok, message, pid, detail}；message 是给人看的一句话。
    """
    pre = check(dll)
    detail: dict[str, Any] = {"check": pre}
    if pre["blocks"]:
        return {"ok": False, "pid": pre.get("pid"),
                "message": pre["blocks"][0], "detail": detail}

    target = int(pre["pid"])
    detail["already_loaded"] = loaded_bridge_modules(target)

    try:
        detail["inject"] = remote_load_library(target, pre["dll"])
    except OSError as exc:
        return {"ok": False, "pid": target,
                "message": f"注入失败：{exc}", "detail": detail}

    # 等管道出现并握手。桥自报 OK READY 才算真的成。
    client = BridgeClient(target)
    deadline = time.time() + max(3.0, timeout)
    last = ""
    while time.time() < deadline:
        try:
            last = client.hello()
        except BridgeUnavailable as exc:
            last = f"（管道未就绪：{exc}）"
        if last.startswith(("OK READY", "REFUSED")):
            break
        time.sleep(0.7)
    detail["hello"] = last

    if last.startswith("OK READY"):
        return {"ok": True, "pid": target,
                "message": f"桥已注入并握手成功（pid={target}）",
                "detail": detail}
    if last.startswith("REFUSED"):
        return {"ok": False, "pid": target,
                "message": f"桥拒绝提供服务：{last}。"
                           f"多半是 CEF 版本校验没过，看 README 第 3.5 节。",
                "detail": detail}
    return {"ok": False, "pid": target,
            "message": f"注入动作执行了，但等 {timeout:.0f} 秒也没等到桥就绪"
                       f"（{last}）。可以再点一次，或直接关了网易云重开再试。",
            "detail": detail}
