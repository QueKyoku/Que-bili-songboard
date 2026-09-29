"""扫码登录的业务逻辑：拿 unikey → 轮询 → 拿到 cookie。

单独抽出来是因为有几个入口要用：
  * 网页控制台（songboard/main.py 的 qrcode_start / qrcode_poll）
  * `tools/login_qrcode.py` —— 命令行版（终端画二维码）
  * `tools/扫码登录.pyw`     —— 图形界面版（由根目录的 扫码登录.bat 拉起）
共用这一份，免得写两遍、改一处漏一处。

⚠️ **必须保持同一个会话**（cookie jar）：unikey 是发给那个会话的，
后续轮询得带着同一份 cookie（NMTID 等）回去。不保持的话服务端不认识
这个 key —— 表现是"扫了码，状态永远停在 801，什么都不发生"，
而且从日志上看一切正常（请求都成功、返回都是 801）。
"""
from __future__ import annotations

import http.cookiejar
import urllib.request
from typing import Any

from .cookies import build_cookie
from .netease import weapi_post_capture

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
        #: ⚠️ 整个扫码流程共用这一个 cookie jar —— unikey 和会话绑定，
        #:    轮询不带同一份 cookie 的话服务端不认识它，状态永远停在 801。
        self.jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def _post(self, path: str, payload: dict[str, Any]) -> tuple[dict, list[str], list[int]]:
        """走**带 cookie jar** 的 opener（会话保持的关键就在这）。"""
        return weapi_post_capture(path, payload, "", opener=self._opener)

    def start(self) -> str:
        """申请一个二维码。返回二维码里应该编的 URL。"""
        res, _cookies, _hops = self._post("/login/qrcode/unikey", {"type": 1})
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
        凭据是 Set-Cookie 下发的，而跳转或者接口改动都可能让 body 里没有 code，
        但凭据其实已经拿到了。反过来，code 是 803 也可能一个凭据都没捞到。
        """
        if not self.unikey:
            raise RuntimeError("还没调用 start()")

        try:
            res, set_cookies, hops = self._post(
                "/login/qrcode/client/login", {"type": 1, "key": self.unikey})
        except Exception as exc:  # noqa: BLE001 —— 让上层能看到原因
            raise RuntimeError(f"查询扫码状态失败：{exc}") from exc

        self.raw_headers = list(set_cookies)
        self.hops = list(hops)
        self.debug = {"result": res, "set_cookies": set_cookies, "hops": hops}

        # 凭据可能在这些地方：每一跳的 Set-Cookie、cookie jar、body 的 cookie 字段
        firsts = "; ".join(c.split(";", 1)[0].strip() for c in set_cookies)
        from_jar = "; ".join(f"{c.name}={c.value}" for c in self.jar
                             if c.name == "MUSIC_U" or c.name == "__csrf")
        body_cookie = res.get("cookie") or {}
        body_firsts = "; ".join(f"{k}={v}" for k, v in body_cookie.items()
                                if isinstance(v, str) and v)
        cookie = build_cookie(firsts) or build_cookie(from_jar) \
            or build_cookie(body_firsts)

        if cookie and "MUSIC_U" in cookie:
            self.cookie = cookie
            return CONFIRMED, STATUS_TEXT[CONFIRMED]

        code = int(res.get("code") or 0)
        if code == CONFIRMED:
            # 803 但一个凭据都没捞到 —— 这是最糟的情况（用户以为成了）
            raise RuntimeError(
                f"手机那边显示登录成功（803），但没收到登录凭据。"
                f"跳转链={self.hops}，Set-Cookie={set_cookies or '（一条都没有）'}，"
                f"cookie jar={[c.name for c in self.jar] or '（空）'}，"
                f"响应={str(res)[:200]}")
        known = STATUS_TEXT.get(code)
        if known:
            return code, known
        # ⚠️ 801/802/803/800 之外，网易云**确实会返回别的码**（实测见过 8821：
        #    扫码扫重复了、或者这个码已经用过的时候会这样）。
        #    这时既不能当"继续等"（会一直干等下去），也不能自己编一个说法，
        #    所以把接口原话带上 —— "接口返回 code=8821" 对排查毫无帮助。
        api_msg = str(res.get("message") or res.get("msg") or "").strip()
        return code, (f"网易云返回了异常状态 {code}"
                      + (f"：{api_msg}" if api_msg else "（接口没给说明）"))
