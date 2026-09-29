"""扫码登录的业务逻辑：拿 unikey → 轮询 → 拿到 cookie。

单独抽出来是因为有两个入口要用：
  * `tools/login_qrcode.py` —— 命令行版（终端画二维码）
  * `tools/扫码登录.pyw`     —— 图形界面版（由根目录的 扫码登录.bat 拉起）
两边共用这一份，免得写两遍、改一处漏一处。
"""
from __future__ import annotations

from typing import Any

from .cookies import build_cookie
from .netease import weapi_post, weapi_post_capture

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
        self.hops: list[int] = []
        #: 上一次 poll 的原始材料（跳转链 / Set-Cookie / 接口返回），排错用
        self.debug: dict[str, Any] = {}

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
        self.hops = []
        return self.url

    def poll(self) -> tuple[int, str]:
        """查一次扫码状态。返回 (状态码, 给人看的一句话)。

        ⚠️ 判"成功"看的是**有没有真的拿到 MUSIC_U**，而不是只看 body 里的 code：
        凭据是 `Set-Cookie` 下发的，而且可能挂在跳转链的中间那一跳上
        （见 netease.weapi_post_capture，这里栽过一次）。
        所以先找凭据，找到就算 803；找不到再看 body 的 code 是多少。
        """
        if not self.unikey:
            raise RuntimeError("还没调用 start()")

        try:
            res, set_cookies, hops = weapi_post_capture(
                "/login/qrcode/client/login", {"type": 1, "key": self.unikey}, "")
        except Exception as exc:  # noqa: BLE001 —— 让上层能看到原因
            raise RuntimeError(f"查询扫码状态失败：{exc}") from exc

        self.raw_headers = list(set_cookies)
        self.hops = list(hops)
        self.debug = {"result": res, "set_cookies": set_cookies, "hops": hops}

        # 凭据可能在这些地方：每一跳的 Set-Cookie、body 的 cookie 字段
        firsts = "; ".join(c.split(";", 1)[0].strip() for c in set_cookies)
        body_cookie = res.get("cookie") or {}
        body_firsts = "; ".join(f"{k}={v}" for k, v in body_cookie.items()
                                if isinstance(v, str) and v)
        cookie = build_cookie(firsts) or build_cookie(body_firsts)

        if cookie and "MUSIC_U" in cookie:
            self.cookie = cookie
            return CONFIRMED, STATUS_TEXT[CONFIRMED]

        code = int(res.get("code") or 0)
        if code == CONFIRMED:
            # 803 但一个凭据都没捞到 —— 这是最糟的情况（用户以为成了）
            raise RuntimeError(
                f"手机那边显示登录成功（803），但没收到登录凭据。"
                f"跳转链={self.hops}，Set-Cookie={set_cookies or '（一条都没有）'}，"
                f"响应={str(res)[:200]}")
        return code, STATUS_TEXT.get(code, f"接口返回 code={code}")
