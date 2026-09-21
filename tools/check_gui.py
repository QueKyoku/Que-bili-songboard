"""验证 tools/扫码登录.pyw 能正常起来、二维码真的画出来了。

    python tools/check_gui.py

改动界面之后跑一下这个：不靠截图，直接问 tkinter ——
窗口建起来了吗？状态文字变了吗？画布上有二维码方块吗？
三种状态（等待扫码 / 已扫码 / 过期）都能正确更新吗？
"""
import importlib.util
import pathlib
import sys
import time
import tkinter

ROOT = pathlib.Path(__file__).resolve().parent.parent   # tools/ 的上一级才是项目根

spec = importlib.util.spec_from_file_location("gui", ROOT / "tools" / "扫码登录.pyw")
gui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui)          # __name__ != "__main__"，不会自动跑 main()

print("=== 建窗口 ===")
root = tkinter.Tk()
app = gui.App(root)
print(f"  标题：{root.title()}")
print(f"  初始状态：{app.status.cget('text')!r}")

print("\n=== 等后台线程拿到 unikey 并把二维码画出来 ===")
ok = False
for i in range(80):                    # 最多 8 秒
    root.update()
    time.sleep(0.1)
    text = app.status.cget("text")
    items = len(app.canvas.find_all())
    if i % 10 == 0:
        print(f"  {i / 10:.1f}s  状态={text!r}  画布元素={items}")
    if "等待扫码" in text and items > 100:
        ok = True
        break

status = app.status.cget("text")
detail = app.detail.cget("text")
items = len(app.canvas.find_all())
print(f"\n  最终状态：{status!r}")
print(f"  说明文字：{detail!r}")
print(f"  画布元素数：{items}（二维码方块，一个模块一个矩形）")

bbox = app.canvas.bbox("all")
print(f"  二维码外框：{bbox}")

print("\n=== 判定 ===")
checks = [
    ("窗口建起来了", bool(root.title())),
    ("状态从「正在准备」变成了「等待扫码」", "等待扫码" in status),
    ("二维码画出来了（画布上几百个方块）", items > 100),
    ("二维码是正方形且居中", bbox is not None and
     abs((bbox[2] - bbox[0]) - (bbox[3] - bbox[1])) <= 2),
    ("有给用户的提示文字", len(detail) > 5),
    ("「重新生成二维码」按钮在", app.btn_retry.winfo_exists() == 1),
    ("没装 qrcode 时才会出现的安装按钮被隐藏了",
     app.btn_install.winfo_manager() == ""),
]
allok = True
for name, good in checks:
    allok &= good
    print(f"  [{'PASS' if good else 'FAIL'}] {name}")

print("\n=== 模拟「已扫码」状态更新 ===")
app.on_poll(802, "已扫码 —— 请在手机上点「确认登录」")
root.update()
print(f"  状态：{app.status.cget('text')!r}")
print(f"  说明：{app.detail.cget('text')!r}")
print(f"  已扫码时给了额外提示：{'确认框' in app.detail.cget('text')}")

print("\n=== 模拟「二维码过期」===")
app.on_poll(800, "二维码过期了，点「重新生成」再扫一次")
root.update()
print(f"  状态：{app.status.cget('text')!r}")
print(f"  说明：{app.detail.cget('text')!r}")
root.update()

# ─────────────────────────────────────────────────────────
# 下面两条是「最容易碰到、但平时测不到」的分支
# ─────────────────────────────────────────────────────────
print("\n=== 模拟「没装 qrcode」（新用户第一次用最可能遇到）===")
# ⚠️ 要 mock `gui.qrcode_available`，不能 mock `songboard.qrlogin.qrcode_available`：
#    .pyw 里是 `from songboard.qrlogin import qrcode_available`，
#    它拿到的是**自己命名空间里的那个引用**，改源模块没用（第一次就写错了，
#    结果测试假失败 —— 好在测试自己也会被验证）。
import queue as _queue                      # noqa: E402
real_avail = gui.qrcode_available
root2 = tkinter.Tk()
try:
    gui.qrcode_available = lambda: False          # type: ignore
    app2 = gui.App(root2)
    for _ in range(6):
        root2.update()
        time.sleep(0.1)
    st2 = app2.status.cget("text")
    dt2 = app2.detail.cget("text")
    btn_visible = app2.btn_install.winfo_manager() != ""
    print(f"  状态：{st2!r}")
    print(f"  说明：{dt2!r}")
    print(f"  「自动安装」按钮出现了：{btn_visible}")
    checks += [
        ("没装 qrcode 时明确说了这件事", "qrcode" in st2),
        ("给出了「自动安装」按钮", btn_visible),
        ("告诉用户装完会自动继续", "自动" in dt2),
    ]
finally:
    gui.qrcode_available = real_avail            # type: ignore
    root2.destroy()

print("\n=== 模拟「打包成 exe 后点自动安装」===")
# 打包后 sys.executable 是 exe 自己，拿它跑 -m pip 会出事（甚至把程序再启动一次），
# 所以那条分支必须拦住 —— 这里故意把 sys.frozen 设成 True 来验它。
root3 = tkinter.Tk()
try:
    app3 = gui.App.__new__(gui.App)              # 不走 __init__（它会去请求网络）
    app3.root = root3
    app3.busy = False
    app3.status = tkinter.Label(root3, text="")
    app3.detail = tkinter.Label(root3, text="")
    app3.q = _queue.Queue()
    real_frozen = getattr(sys, "frozen", None)
    sys.frozen = True                            # type: ignore
    try:
        app3.install_qrcode()
    finally:
        if real_frozen is None:
            del sys.frozen                       # type: ignore
        else:
            sys.frozen = real_frozen             # type: ignore
    st3 = app3.status.cget("text")
    dt3 = app3.detail.cget("text")
    print(f"  状态：{st3!r}")
    print(f"  说明：{dt3!r}")
    checks += [
        ("打包版不会去跑 pip（那会把 exe 自己当 python）", "装不了" in st3),
        ("告诉用户改用 .bat 或重新打包", "build_gui.ps1" in dt3),
    ]
finally:
    root3.destroy()

root.destroy()
print("\n=== 汇总 ===")
allok = True
for name, good in checks:
    allok &= good
    print(f"  [{'PASS' if good else 'FAIL'}] {name}")
print(f"\n全部通过：{allok}")
sys.exit(0 if allok else 1)
