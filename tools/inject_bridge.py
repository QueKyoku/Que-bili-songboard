"""把 AwooNcmCefBridge.dll 注入 cloudmusic.exe 主进程。

做法是标准 Win32 注入：OpenProcess -> VirtualAllocEx -> WriteProcessMemory
-> CreateRemoteThread(LoadLibraryW) 。不修改磁盘上任何文件，不 hook 系统调用。

安全设计：
  * 只对 cloudmusic.exe 生效，且默认只挑持有 OrpheusBrowserHost 的那个 pid。
  * 注入后立刻用管道 HELLO 验证；若桥自报 REFUSED，可用 --eject 卸载。
  * --dry-run 只做全部检查，不实际注入。

用法：
    python inject_bridge.py --dry-run
    python inject_bridge.py
    python inject_bridge.py --pid 99800
    python inject_bridge.py --eject
"""

from __future__ import annotations

import argparse
import ctypes
import os
import struct
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bridge_client

DLL_PATH = str(Path(__file__).resolve().parent.parent / "bridge"
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
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0

ACCESS = (PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION
          | PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE)

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenProcess.restype = wintypes.HANDLE
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.IsWow64Process.argtypes = [wintypes.HANDLE,
                               ctypes.POINTER(wintypes.BOOL)]
k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                               ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualFreeEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                              ctypes.c_size_t, wintypes.DWORD]
k32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t)]
k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
k32.GetModuleHandleW.restype = wintypes.HMODULE
k32.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_char_p]
k32.GetProcAddress.restype = ctypes.c_void_p
k32.CreateRemoteThread.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
    ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
k32.CreateRemoteThread.restype = wintypes.HANDLE
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.GetExitCodeThread.argtypes = [wintypes.HANDLE,
                                  ctypes.POINTER(wintypes.DWORD)]


def is_64bit_process(handle) -> bool:
    wow = wintypes.BOOL()
    if not k32.IsWow64Process(handle, ctypes.byref(wow)):
        raise OSError(f"IsWow64Process 失败: {ctypes.get_last_error()}")
    return not wow.value


def pe_arch(path: str) -> str:
    with open(path, "rb") as f:
        data = f.read(0x400)
    if data[:2] != b"MZ":
        return "unknown"
    off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[off:off + 4] != b"PE\0\0":
        return "unknown"
    machine = struct.unpack_from("<H", data, off + 4)[0]
    return {0x8664: "x64", 0x14C: "x86", 0xAA64: "arm64"}.get(
        hex(machine) and machine, f"0x{machine:X}")


def loaded_bridge_modules(pid: int) -> list[str]:
    """该进程里所有已加载的 AwooNcmCefBridge.dll 的完整路径。

    ⚠️ 重要：Windows 的 LoadLibrary 是**按路径**计引用的。如果曾经从 A 路径
    注入过、又从 B 路径注入一次，进程里就会有**两份**同名模块（各自有独立的
    全局状态和管道服务器）。所以卸载必须按"实际已加载的路径"逐个来，
    不能拿配置里的路径去 FreeLibrary——那样只会是一次静默无效操作。
    """
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

    k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32)]
    k32.Module32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32)]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32,
                                        pid)
    if snap == INVALID:
        return []
    found: list[str] = []
    try:
        entry = MODULEENTRY32()
        entry.dwSize = ctypes.sizeof(MODULEENTRY32)
        ok = k32.Module32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szModule.lower() == "awooncmcefbridge.dll":
                found.append(entry.szExePath)
            ok = k32.Module32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return found


def remote_load_library(pid: int, dll_path: str, free: bool = False) -> dict:
    """在目标进程里调用 LoadLibraryW / FreeLibrary。返回诊断字典。"""
    dll_path = os.path.abspath(dll_path)
    info: dict = {"pid": pid, "dll": dll_path, "free": free}

    handle = k32.OpenProcess(ACCESS, False, pid)
    if not handle:
        raise OSError(
            f"OpenProcess(pid={pid}) 失败 WinError={ctypes.get_last_error()}。"
            "注入需要管理员权限。")
    try:
        info["target_64bit"] = is_64bit_process(handle)

        # 1) 在目标进程分配空间写入 DLL 路径（UTF-16）
        path_bytes = (dll_path + "\0").encode("utf-16-le")
        remote_buf = k32.VirtualAllocEx(
            handle, None, len(path_bytes), MEM_COMMIT | MEM_RESERVE,
            PAGE_READWRITE)
        if not remote_buf:
            raise OSError(f"VirtualAllocEx 失败 WinError={ctypes.get_last_error()}")
        info["remote_buf"] = hex(remote_buf)
        try:
            written = ctypes.c_size_t(0)
            if not k32.WriteProcessMemory(
                    handle, remote_buf, path_bytes, len(path_bytes),
                    ctypes.byref(written)):
                raise OSError(
                    f"WriteProcessMemory 失败 WinError={ctypes.get_last_error()}")
            info["written"] = written.value

            # 2) 解析本进程 kernel32!LoadLibraryW 地址（同位数下 ASLR 偏移一致）
            h_k32 = k32.GetModuleHandleW("kernel32.dll")
            fn_name = b"FreeLibrary" if free else b"LoadLibraryW"
            fn_addr = k32.GetProcAddress(h_k32, fn_name)
            if not fn_addr:
                raise OSError(f"找不到 kernel32!{fn_name.decode()}")
            info["fn"] = f"kernel32!{fn_name.decode()}"
            info["fn_addr"] = hex(fn_addr)

            # 3) 远程线程调用
            tid = wintypes.DWORD(0)
            thread = k32.CreateRemoteThread(
                handle, None, 0, fn_addr, remote_buf, 0, ctypes.byref(tid))
            if not thread:
                raise OSError(
                    f"CreateRemoteThread 失败 WinError={ctypes.get_last_error()}")
            info["tid"] = tid.value
            try:
                if k32.WaitForSingleObject(thread, 15000) != WAIT_OBJECT_0:
                    raise OSError("等待远程线程超时")
                code = wintypes.DWORD(0)
                k32.GetExitCodeThread(thread, ctypes.byref(code))
                info["return_value"] = code.value
                info["return_hex"] = hex(code.value)
            finally:
                k32.CloseHandle(thread)
        finally:
            k32.VirtualFreeEx(handle, remote_buf, 0, MEM_RELEASE)
    finally:
        k32.CloseHandle(handle)
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=None,
                    help="目标 pid；默认自动找持有 OrpheusBrowserHost 的那个")
    ap.add_argument("--dll", default=DLL_PATH)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--eject", action="store_true",
                    help="卸载桥 DLL（FreeLibrary）")
    args = ap.parse_args()

    admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    print("=" * 90)
    print("桥 DLL 注入")
    print("=" * 90)
    print("管理员权限 :", admin)
    print("DLL        :", args.dll)
    print("DLL 存在   :", os.path.exists(args.dll))
    if os.path.exists(args.dll):
        print("DLL 架构   :", pe_arch(args.dll))
    print()

    pids = bridge_client.find_cloudmusic_pids()
    print("cloudmusic pids:", pids)
    if not pids:
        print("!! 网易云未运行")
        return 1

    # 找主进程
    import list_netease_windows as lw
    target = args.pid
    host_owner = None
    for pid in pids:
        for hwnd, cls, title, _vis in lw.windows_of_pid(pid):
            if cls == "OrpheusBrowserHost":
                host_owner = pid
                print(f"  发现 OrpheusBrowserHost: pid={pid} hwnd=0x{hwnd:X} "
                      f"title={title!r}")
    if target is None:
        target = host_owner
    if target is None:
        print("!! 找不到 OrpheusBrowserHost 窗口，无法确定主进程")
        return 1
    print("目标 pid   :", target)
    print()

    if args.dry_run:
        print("[dry-run] 不做任何写入。检查通过。")
        return 0

    if args.eject:
        print("--- 卸载桥 DLL ---")
        mods = loaded_bridge_modules(target)
        print(f"  进程里已加载的桥副本: {len(mods)}")
        for m in mods:
            print(f"    {m}")
        if not mods:
            print("  没有已加载的桥，无需卸载。")
            return 0
        rc = 0
        for m in mods:
            print(f"\n  >>> FreeLibrary 卸载: {m}")
            try:
                info = remote_load_library(target, m, free=True)
            except OSError as exc:
                print("    !! 卸载失败:", exc)
                rc = 2
                continue
            print(f"    return_value = {info['return_value']} (0=成功)")
            if info["return_value"] != 0:
                print("    ⚠️ 返回非 0，可能仍有引用未释放")
        time.sleep(1.0)
        left = loaded_bridge_modules(target)
        print()
        print(f"卸载后仍在进程里的桥副本: {len(left)}")
        for m in left:
            print(f"    {m}")
        if not left:
            print("  ✅ 已全部卸载")
            print()
            print("卸载后管道状态:")
            for pid in pids:
                print(f"  pid={pid}: {bridge_client.BridgeClient(pid).describe()}")
            return rc

        print()
        print("  ⚠️ FreeLibrary 返回成功但模块仍然驻留。")
        print("     原因：桥的 DllMain 启动了常驻 worker 线程（管道服务器 +")
        print("     播放状态监听），进程里存在对它的额外引用，从外部无法可靠卸载。")
        print("     不要反复尝试——重复 FreeLibrary 可能破坏引用计数。")
        print()
        print("     要彻底移除桥，**正常关闭并重新打开网易云**即可：")
        print("     DLL 是内存注入，网易云一退出就没了。")
        print()
        print("     卸载后管道状态（预期仍然可用）:")
        for pid in pids:
            print(f"       pid={pid}: {bridge_client.BridgeClient(pid).describe()}")
        return rc

    print("--- 注入 ---")
    try:
        info = remote_load_library(target, args.dll)
    except OSError as exc:
        print("!! 注入失败:", exc)
        return 2
    for k, v in info.items():
        print(f"  {k} = {v}")

    print()
    print("--- 等待管道出现并握手（最多 12 秒）---")
    client = bridge_client.BridgeClient(target)
    deadline = time.time() + 12
    last = ""
    while time.time() < deadline:
        try:
            raw = client.hello()
        except bridge_client.BridgeUnavailable as exc:
            raw = f"(管道未就绪: {exc})"
        if raw != last:
            print(f"  [{time.strftime('%H:%M:%S')}] {raw}")
            last = raw
        if raw.startswith(("OK READY", "REFUSED")):
            break
        time.sleep(0.7)

    print()
    print("最终状态:", client.describe())
    if last.startswith("OK READY"):
        print()
        print("--- DevTools 诊断 ---")
        try:
            print(client.diagnostics())
        except Exception as exc:
            print("  诊断读取失败:", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
