"""扫码登录的业务逻辑：拿 unikey → 轮询 → 拿到 cookie。

单独抽出来是因为有两个入口要用：
  * `tools/login_qrcode.py` —— 命令行版（终端画二维码）
  * `tools/扫码登录.pyw`     —— 图形界面版（由根目录的 扫码登录.bat 拉起）
两边共用这一份，免得写两遍、改一处漏一处。
"""
from __future__ import annotations

from typing import Any

from .cookies import cookies_from_response_headers
from .netease import weapi_post, weapi_post_raw

# 轮询返回的状态码
WAITING = 801        # 还没扫
SCANNED = 802        # 扫了，等手机上点确认
CONFIRMED = 803      # 登录成功
EXPIRED = 800        # 二维码过期

STATUS_TEXT = {
    WAITING: "等待扫码…",
    SCANNED: "已扫码 —— 请在手机上点「确认登录」",
    CONFIRMED: "登录成功",
    EXPIRED: "二维码过期了，点「重新生成」再扫一次",
}


def qr_matrix(url: str, *, border: int = 2) -> list[list[bool]]:
    """把 URL 编成二维码，返回布尔矩阵（True = 黑块）。

    为什么返回矩阵而不是图片：
      * 命令行版用 `qrcode` 自带的 print_ascii 直接打到终端
      * 图形界面版拿这个矩阵在 Canvas 上画方块
    这样**不需要 Pillow** —— 少一个依赖，打包出来的 exe 也小一圈。
    """
    import qrcode
    qr = qrcode.QRCode(border=border,
                       error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.make(fit=True)
    return qr.get_matrix()


def qrcode_available() -> bool:
    try:
        import qrcode  # noqa: F401
        return True
    except ImportError:
        return False


class QrLogin:
    """一次扫码登录的过程。

        s = QrLogin()
        url = s.start()              # 拿到二维码内容，去画二维码
        code, msg = s.poll()         # 反复调用，直到 code == CONFIRMED
        print(s.cookie)              # 成功后的 cookie
    """

    def __init__(self) -> None:
        self.unikey = ""
        self.url = ""
        self.cookie = ""
        self.raw_headers: list[str] = []

    def start(self) -> str:
        """申请一个二维码。返回二维码里应该编的 URL。"""
        res = weapi_post("/login/qrcode/unikey", {"type": 1}, "")
        key = str(res.get("unikey") or "")
        if not key:
            raise RuntimeError(f"没能拿到二维码凭据，接口返回：{res}")
        self.unikey = key
        self.url = f"https://music.163.com/login?codekey={key}"
        self.cookie = ""
        self.raw_headers = []
        return self.url

    def poll(self) -> tuple[int, str]:
        """查一次扫码状态。返回 (状态码, 给人看的一句话)。"""
        if not self.unikey:
            raise RuntimeError("还没调用 start()")
        res, headers = weapi_post_raw("/login/qrcode/client/login",
                                      {"type": 1, "key": self.unikey}, "")
        code = int(res.get("code") or 0)

        if code == CONFIRMED:
            # 凭据在 Set-Cookie 响应头里，不在 body
            self.raw_headers = list(headers.get_all("Set-Cookie") or []) \
                if headers else []
            self.cookie = cookies_from_response_headers(headers)
            if not self.cookie:
                # 兜底：有些版本会把 cookie 放在 body 的 cookie 字段里
                body_cookie = res.get("cookie") or {}
                self.cookie = "; ".join(
                    f"{k}={v}" for k, v in body_cookie.items()
                    if k in ("MUSIC_U", "__csrf") and v)
        return code, STATUS_TEXT.get(code, f"接口返回 code={code}")
