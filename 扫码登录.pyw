"""网易云扫码登录（图形界面版）—— 双击这个文件就能用。

给不想碰命令行的人：手机上装网易云 App，扫一下窗口里的二维码，
cookie 会自动写进 config.json 并当场验证。

    · 双击本文件即可（.pyw 后缀不会弹出黑色控制台窗口）
    · 没装 qrcode 也没关系，界面上有「自动安装」按钮
    · 想打包成单个 exe：见 tools/build_gui.ps1

命令行版本在 login_qrcode.py（功能一样，只是在终端里画二维码）。
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont

ROOT = Path(__file__).resolve().parent
# ⚠️ 打包成 exe 之后（PyInstaller --onefile），__file__ 指向的是解包出来的
#    临时目录，config.json 不在那儿。所以要改用 exe 自己的所在目录 ——
#    也就是说 **exe 要放在项目根目录**（和 config.json 同一个文件夹）。
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
sys.path.insert(0, str(ROOT))

from songboard.config import Config  # noqa: E402
from songboard.netease import account_info  # noqa: E402
from songboard.qrlogin import (  # noqa: E402
    CONFIRMED, EXPIRED, QrLogin, STATUS_TEXT, qr_matrix, qrcode_available,
)

BG = "#1b1d26"
FG = "#e8ecf5"
DIM = "#8b93a7"
ACCENT = "#ff6b9d"
OK = "#3ddc97"
WARN = "#ffcc55"
ERR = "#ff6b6b"
QR_ZOOM = 8          # 每个二维码模块画多少像素


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.session: QrLogin | None = None
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.polling = False

        root.title("网易云扫码登录 · 点歌板")
        root.configure(bg=BG)
        root.geometry("440x640")
        root.minsize(420, 600)

        big = tkfont.Font(family="Microsoft YaHei UI", size=13, weight="bold")
        mid = tkfont.Font(family="Microsoft YaHei UI", size=10)
        small = tkfont.Font(family="Microsoft YaHei UI", size=9)

        tk.Label(root, text="网易云扫码登录", bg=BG, fg=FG,
                 font=tkfont.Font(family="Microsoft YaHei UI", size=15,
                                  weight="bold")).pack(pady=(18, 4))
        tk.Label(root, text="用手机上的【网易云音乐】App 扫下面的码",
                 bg=BG, fg=DIM, font=mid).pack()

        # 二维码画布
        self.canvas = tk.Canvas(root, width=280, height=280, bg="#ffffff",
                                highlightthickness=0)
        self.canvas.pack(pady=14)
        self.canvas.create_text(140, 140, text="正在准备…", fill="#666",
                                font=mid, tags="hint")

        self.status = tk.Label(root, text="", bg=BG, fg=FG, font=big,
                               wraplength=400, justify="center")
        self.status.pack(pady=(2, 2))
        self.detail = tk.Label(root, text="", bg=BG, fg=DIM, font=small,
                               wraplength=400, justify="center")
        self.detail.pack()

        row = tk.Frame(root, bg=BG)
        row.pack(pady=14)
        # 「自动安装 qrcode」只在真缺依赖时才显示 —— 由 start_login() 决定
        # pack 不 pack，所以这里先建出来但不摆上去（否则启动瞬间会闪一下）。
        self.btn_retry = self._button(row, "重新生成二维码", self.start_login,
                                      place=True)
        self.btn_install = self._button(row, "自动安装 qrcode",
                                        self.install_qrcode, place=False)
        self._button(row, "关闭", root.destroy, place=True)

        self.log = tk.Label(root, text="", bg=BG, fg=DIM, font=small,
                            wraplength=400, justify="left")
        self.log.pack(side="bottom", pady=10)

        root.after(100, self._drain)
        self.start_login()

    # ---------- 小工具 ----------
    def _button(self, parent, text, cmd, *, place: bool = True) -> tk.Button:
        b = tk.Button(parent, text=text, command=cmd, bg="#2a2e3d", fg=FG,
                      activebackground=ACCENT, activeforeground="#fff",
                      relief="flat", padx=12, pady=6,
                      font=tkfont.Font(family="Microsoft YaHei UI", size=10))
        if place:
            b.pack(side="left", padx=4)
        return b

    def set_status(self, text: str, color: str = FG) -> None:
        self.status.config(text=text, fg=color)

    def set_detail(self, text: str) -> None:
        self.detail.config(text=text)

    def add_log(self, text: str) -> None:
        old = self.log.cget("text")
        self.log.config(text=(old + "\n" + text).strip()[-600:])

    # ---------- 画二维码 ----------
    def draw_qr(self, matrix) -> None:
        self.canvas.delete("all")
        n = len(matrix)
        size = n * QR_ZOOM
        off = (280 - size) // 2
        for y, row in enumerate(matrix):
            for x, dark in enumerate(row):
                if dark:
                    x0 = off + x * QR_ZOOM
                    y0 = off + y * QR_ZOOM
                    self.canvas.create_rectangle(
                        x0, y0, x0 + QR_ZOOM, y0 + QR_ZOOM,
                        fill="#000000", outline="", width=0)

    # ---------- 装依赖 ----------
    def install_qrcode(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.set_status("正在安装 qrcode…", WARN)
        self.set_detail("装完会自动继续，不用管这个窗口")

        def work():
            try:
                r = subprocess.run(
                    [sys.executable, "-m", "pip", "install",
                     "--disable-pip-version-check", "qrcode"],
                    capture_output=True, text=True, timeout=180)
                self.q.put(("installed", r.returncode == 0,
                            (r.stderr or r.stdout or "")[-300:]))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("installed", False, repr(exc)))

        threading.Thread(target=work, daemon=True).start()

    # ---------- 主流程 ----------
    def start_login(self) -> None:
        if self.polling:
            return
        self.canvas.delete("all")
        self.canvas.create_text(140, 140, text="正在准备…", fill="#666",
                                font=tkfont.Font(family="Microsoft YaHei UI",
                                                 size=10), tags="hint")
        if not qrcode_available():
            self.btn_install.pack(side="left", padx=4)
            self.set_status("还没装 qrcode", WARN)
            self.set_detail("点上面的「自动安装 qrcode」，装完会自动继续")
            return
        self.btn_install.pack_forget()
        self.set_status("正在申请二维码…", DIM)
        self.set_detail("")
        self.busy = True

        def work():
            try:
                s = QrLogin()
                url = s.start()
                self.q.put(("qr", True, (s, url)))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("qr", False, repr(exc)))

        threading.Thread(target=work, daemon=True).start()

    def after_qr(self, session: QrLogin) -> None:
        self.session = session
        try:
            self.draw_qr(qr_matrix(session.url))
        except Exception as exc:  # noqa: BLE001
            self.set_status("二维码画不出来", ERR)
            self.set_detail(repr(exc))
            return
        self.busy = False
        self.set_status("等待扫码…", FG)
        self.set_detail("扫完在手机上点「确认登录」，二维码 3 分钟内有效")
        self.polling = True
        self.root.after(300, self.do_poll)

    def do_poll(self) -> None:
        if not self.polling or not self.session:
            return
        session = self.session

        def work():
            try:
                code, msg = session.poll()
                self.q.put(("poll", True, (code, msg)))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("poll", False, repr(exc)))

        threading.Thread(target=work, daemon=True).start()

    def on_poll(self, code: int, msg: str) -> None:
        color = {CONFIRMED: OK, EXPIRED: WARN}.get(code, FG)
        self.set_status(msg, color)
        if code == CONFIRMED:
            self.polling = False
            self.finish()
            return
        if code == EXPIRED:
            self.polling = False
            self.set_detail("点「重新生成二维码」再来一次")
            return
        # 已扫码 → 换句话提醒，别让用户以为卡住了
        if code == 802:
            self.set_detail("手机上会弹出确认框，点一下就好")
        self.root.after(1200, self.do_poll)

    def finish(self) -> None:
        session = self.session
        assert session is not None
        cookie = session.cookie
        if not cookie:
            self.set_status("登录成功但没拿到 cookie", ERR)
            self.set_detail("收到的 Set-Cookie：" +
                            (", ".join(h.split(";")[0]
                                       for h in session.raw_headers[:5]) or "（空）"))
            return
        try:
            cfg = Config.load(ROOT / "config.json")
            cfg["netease"]["cookie"] = cookie
            cfg["netease"]["enabled"] = True
            cfg.save()
        except Exception as exc:  # noqa: BLE001
            self.set_status("写入配置失败", ERR)
            self.set_detail(repr(exc))
            return
        self.add_log(f"✅ cookie 已写入 config.json（{len(cookie)} 字符）")
        self.set_detail("正在验证…")
        self.set_status("已写入，验证中…", WARN)

        def work():
            try:
                info = account_info(cookie)
            except Exception as exc:  # noqa: BLE001
                info = {}
                self.q.put(("log", True, f"⚠️ 验证出错：{exc!r}"))
            self.q.put(("done", True, info))

        threading.Thread(target=work, daemon=True).start()

    def on_done(self, info: dict) -> None:
        if info:
            name = info.get("nickname") or info.get("user_id")
            self.set_status(f"✅ 已登录：{name}", OK)
            self.set_detail("搞定，关掉这个窗口就行。服务在跑的话会读新配置。")
            self.add_log(f"✅ 验证通过，登录账号：{name}")
        else:
            self.set_status("cookie 写进去了但没验证通过", WARN)
            self.set_detail("可以点「重新生成二维码」再扫一次")

    # ---------- 主线程里处理后台线程的结果 ----------
    def _drain(self) -> None:
        try:
            while True:
                kind, ok, payload = self.q.get_nowait()
                if kind == "qr":
                    if ok:
                        self.after_qr(payload[0])
                    else:
                        self.busy = False
                        self.set_status("申请二维码失败", ERR)
                        self.set_detail(str(payload))
                elif kind == "poll":
                    if ok:
                        self.on_poll(*payload)
                    else:
                        # 网络抖动就继续轮询，别把界面搞死
                        self.add_log(f"⚠️ 轮询出错（继续试）：{payload}")
                        self.root.after(1500, self.do_poll)
                elif kind == "installed":
                    self.busy = False
                    if ok:
                        self.add_log("✅ qrcode 装好了")
                        self.start_login()
                    else:
                        self.set_status("自动安装失败", ERR)
                        self.set_detail("可以手动装：在项目目录执行 "
                                        "python -m pip install qrcode")
                elif kind == "done":
                    self.on_done(payload)
                elif kind == "log":
                    self.add_log(str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._drain)


def _crash_log(exc: BaseException) -> Path | None:
    """把崩溃信息写到文件 + 弹个框。

    图形界面程序最坑的地方：出错时用户**什么都看不到**（窗口一闪就没了），
    而我们自己也没法让用户"把控制台输出发过来" —— 打包成 exe 后压根没有控制台。
    所以任何异常都要落到一个日志文件里。
    """
    import traceback
    text = (f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Python：{sys.version}\n"
            f"打包运行：{getattr(sys, 'frozen', False)}\n"
            f"程序目录：{ROOT}\n\n"
            + traceback.format_exc())
    path = None
    try:
        path = ROOT / "扫码登录_错误日志.txt"
        path.write_text(text, encoding="utf-8")
    except Exception:  # noqa: BLE001
        path = None
    try:
        import tkinter.messagebox as mb
        mb.showerror(
            "扫码登录出错了",
            f"{type(exc).__name__}: {exc}\n\n"
            + (f"详细信息已写到：\n{path}" if path else "详细信息写不进文件"))
    except Exception:  # noqa: BLE001
        pass
    return path


def main() -> int:
    root = None
    try:
        root = tk.Tk()
        try:
            # 高 DPI 屏上文字别糊
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
        App(root)
        root.mainloop()
    except BaseException as exc:  # noqa: BLE001
        # 注意连 KeyboardInterrupt 一起兜住：图形程序里任何未处理异常
        # 都等于"窗口消失、用户一脸问号"。
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        _crash_log(exc)
        return 1
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
