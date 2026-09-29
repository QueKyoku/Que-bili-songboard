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


# 注入的核心实现都在 songboard/bridge_inject.py —— 控制台那个「重新注入桥」
# 按钮走的就是同一份。这里只负责 CLI 的输出排版，不再自己实现一遍，
# 免得两边各写一份、改一处漏一处。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from songboard.bridge_inject import (  # noqa: E402
    DLL_PATH, is_admin, loaded_bridge_modules, pe_arch, remote_load_library,
)


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
