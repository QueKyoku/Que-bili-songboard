"""验证「扫码登录.pyw」能正常起来、二维码真的画出来了。

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

spec = importlib.util.spec_from_file_location("gui", ROOT / "扫码登录.pyw")
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

root.destroy()
print(f"\n全部通过：{allok}")
sys.exit(0 if allok else 1)
