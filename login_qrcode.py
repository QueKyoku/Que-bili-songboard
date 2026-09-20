"""扫码登录网易云，自动把 cookie 写进配置 —— 不用 F12、不用手拼字符串。

为什么需要它：cookie 是唯一要手动准备的凭据，而**从浏览器里读已经不现实**了。
Chrome / Edge 127+ 给 cookie 加了 App-Bound Encryption（值以 `v20` 开头），
密钥绑死浏览器自身身份，第三方程序解不开（实测：能拿到 DPAPI 密钥、
但每条 cookie 都是 v20，一条都解不出来）。扫码是唯一"全自动"的路子。

用法：
    python login_qrcode.py

流程：
    拿 unikey → 终端画出二维码 → 手机上的【网易云音乐】App 扫码并确认
    → 自动写入 config.json → 立刻验证并打印登录的昵称

依赖：`qrcode`（纯 Python 小库，只在扫码登录时用得到）
    pip install qrcode

不想装的话，就用 `set_cookie.py` 从浏览器手动复制（见 README 第九章）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 扫码时要实时看到「等待扫码 → 已扫码请确认」的进度。默认情况下
# 输出被重定向/走管道时是块缓冲的，进度会一直卡着不显示（实测踩过），
# 所以强制行缓冲。
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:  # noqa: BLE001
    pass

from songboard.config import Config  # noqa: E402
from songboard.cookies import extract_fields  # noqa: E402
from songboard.netease import account_info  # noqa: E402
from songboard.qrlogin import (  # noqa: E402
    CONFIRMED, EXPIRED, QrLogin, STATUS_TEXT,
)

#: 二维码过期前给用户多久时间扫
WAIT_SECONDS = 180


def show_qr(url: str) -> None:
    import qrcode
    qr = qrcode.QRCode(border=1,
                       error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def main() -> int:
    try:
        import qrcode  # noqa: F401
    except ImportError:
        print("这个脚本要画二维码，需要 qrcode 这个库（纯 Python，很小）：")
        print()
        print("    pip install qrcode")
        print()
        print("不想装也行 —— 用 set_cookie.py 从浏览器手动复制 cookie，")
        print("步骤见 README 第九章「快捷获取 Cookie」。")
        return 2

    cfg = Config.load(ROOT / "config.json")

    # ---- 1) 申请二维码 ----
    session = QrLogin()
    try:
        url = session.start()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 申请二维码失败：{exc}")
        return 1

    print()
    print("=" * 62)
    print("  用手机上的【网易云音乐】App 扫下面这个码")
    print("=" * 62)
    print()
    show_qr(url)
    print()
    print("扫完在手机上点「确认登录」。二维码 3 分钟内有效。")
    print(f"（万一看不清二维码，也可以在手机浏览器里打开：{url}）")
    print()

    # ---- 2) 轮询 ----
    deadline = time.time() + WAIT_SECONDS
    last_code = None
    while time.time() < deadline:
        try:
            code, msg = session.poll()
        except Exception as exc:  # noqa: BLE001
            print(f"  轮询出错（继续试）：{exc!r}")
            time.sleep(2)
            continue
        if code != last_code:
            print("  " + msg)
            last_code = code
        if code == CONFIRMED:
            break
        if code == EXPIRED:
            print("\n二维码过期了，重新运行本脚本即可。")
            return 1
        time.sleep(1.5)
    else:
        print(f"\n等了 {WAIT_SECONDS} 秒没等到扫码，先退出了。")
        return 1

    cookie = session.cookie
    if not cookie:
        # 登录成功了却没读到凭据 —— 把线索都打出来，方便排查
        print("  ⚠️ 登录成功了，但没能读到 MUSIC_U。")
        print(f"     收到的 Set-Cookie 条目数：{len(session.raw_headers)}")
        for one in session.raw_headers[:8]:
            print(f"       {one.split(';')[0]}")
        print("     请把上面几行发给我，好定位。")
        return 1

    fields = extract_fields(cookie)
    print(f"\n✅ 从 Set-Cookie 收到：{list(fields)}")

    # ---- 3) 写配置 + 立刻验证 ----
    cfg["netease"]["cookie"] = cookie
    cfg["netease"]["enabled"] = True
    cfg.save()
    print(f"✅ 已写入 config.json（{len(cookie)} 字符），并自动开启「搜索歌曲 / 查时长」")
    print("⚠️  config.json 现在含有账号凭据：别提交到 git、别截图、别发给别人")

    try:
        info = account_info(cookie)
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 验证时出错：{exc!r}")
        info = {}
    if info:
        print(f"✅ cookie 有效，已登录：{info.get('nickname') or info.get('user_id')}")
        print("\n好了。服务在跑的话它会读新配置；没跑就直接启动：python -m songboard")
        return 0
    print("⚠️ cookie 已写入，但验证没通过 —— 服务里可能也用不了，重扫一次试试。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
