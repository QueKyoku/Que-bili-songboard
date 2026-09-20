"""进程级测试：确保没有交互终端时，服务不会跟着 stdin 关闭一起退出。

用法：python proc_check.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"


def http_ok(path: str, timeout: float = 3, limit: int = 80) -> tuple[bool, str]:
    """GET 一个路径，返回 (是否 200, 响应文本)。

    limit 是给阅读用的截断长度——需要解析 JSON 时必须传大值，
    否则截断后的文本不是合法 JSON。
    """
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return r.status == 200, body[:limit] if limit else body
    except Exception as exc:
        return False, repr(exc)


def http_json(path: str, timeout: float = 3) -> dict:
    ok, raw = http_ok(path, timeout, limit=0)
    if not ok:
        raise RuntimeError(f"GET {path} 失败: {raw}")
    return json.loads(raw)


def main() -> int:
    print(f"== 进程级检查：无终端启动，端口 {PORT} ==")
    # 用独立的临时配置，绝不改动用户自己的 config.json
    import shutil
    import tempfile

    tmpdir = Path(tempfile.mkdtemp(prefix="songboard_proc_"))
    tmp_cfg = tmpdir / "config.json"
    src_cfg = ROOT / "config.json"
    if src_cfg.exists():
        shutil.copy2(src_cfg, tmp_cfg)
    # ⚠️ 复制来的配置里 netease.auto_add 很可能是 true —— 那样这个测试实例
    # 会把"进程级测试曲"（一个搜不到的曲名）写进**你真实的网易云歌单**，
    # 而且反复失败刷日志。测试只该验证进程行为，绝不该碰真歌单。
    try:
        cfg_data = json.loads(tmp_cfg.read_text(encoding="utf-8"))
    except Exception:
        cfg_data = {}
    ne = cfg_data.setdefault("netease", {})
    ne["auto_add"] = False          # 只读，不写歌单
    ne["enabled"] = False           # 连读都关掉，彻底不碰网易云
    cfg_data.setdefault("ncm_bridge", {})["enabled"] = False
    tmp_cfg.write_text(json.dumps(cfg_data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    print(f"  临时配置已关闭网易云写入（auto_add=false, enabled=false）")
    proc = subprocess.Popen(
        [sys.executable, "-m", "songboard", "--mode", "demo",
         "--port", str(PORT), "--no-persist", "--config", str(tmp_cfg)],
        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    results: list[tuple[str, bool, str]] = []
    try:
        # 等服务起来
        alive_at = None
        for _ in range(30):
            time.sleep(0.5)
            ok, detail = http_ok("/api/state")
            if ok:
                alive_at = time.time()
                break
        results.append(("服务在无终端环境下成功启动", alive_at is not None,
                        "启动成功" if alive_at else "3 秒内没起来"))
        if alive_at is None:
            raise SystemExit(1)

        # 关键：等 5 秒，确认进程没有因为 stdin=DEVNULL 的 EOF 自杀
        print("  等待 5 秒，观察进程是否因 stdin 关闭退出…")
        for i in range(10):
            time.sleep(0.5)
            if proc.poll() is not None:
                results.append(("进程存活（未因 EOF 退出）", False,
                                f"第 {i * 0.5 + 0.5:.1f}s 就退出了，exit={proc.returncode}"))
                break
        else:
            results.append(("进程存活（未因 EOF 退出）", True, "5 秒内仍在运行"))

        ok, html = http_ok("/overlay")
        results.append(("叠加层可访问", ok and "点歌板" in html, html[:60]))
        ok, _ = http_ok("/control")
        results.append(("控制台可访问", ok, ""))
        ok, raw = http_ok("/api/state", limit=0)
        # /api/state 返回的是快照本身（current/queue/pending/counts）。
        # 演示模式的弹幕是随机间隔生成的，这里轮询等它出内容。
        snap: dict = {}
        last_err = ""
        for _ in range(24):          # 最多等 12 秒
            try:
                snap = json.loads(raw) if ok else {}
            except json.JSONDecodeError as exc:
                snap = {}
                last_err = f"JSON 解析失败 {exc!r}"
            if snap.get("current") or snap.get("queue") or snap.get("pending"):
                break
            if not ok:
                last_err = raw
            time.sleep(0.5)
            ok, raw = http_ok("/api/state", limit=0)
        demo_working = bool(snap.get("current") or snap.get("queue") or snap.get("pending"))
        detail = (f"current={(snap.get('current') or {}).get('song')} counts={snap.get('counts')}"
                  if demo_working else f"没等到内容；最后一次响应 ok={ok} err={last_err[:120]!r}")
        results.append(("演示模式自动生成点歌", demo_working, detail))

        # 通过 HTTP 加歌，确认服务真的在干活
        req = urllib.request.Request(
            BASE + "/api/add",
            data=json.dumps({"song": "进程级测试曲", "user": "proc", "force": True}).encode(),
            method="POST", headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                body = json.loads(r.read().decode())
            results.append(("REST 加歌生效", bool(body.get("ok")), json.dumps(body, ensure_ascii=False)[:80]))
        except Exception as exc:
            results.append(("REST 加歌生效", False, repr(exc)))
    finally:
        out = ""
        try:
            proc.terminate()
            out = proc.communicate(timeout=10)[0] or ""
        except Exception:
            proc.kill()
        shutil.rmtree(tmpdir, ignore_errors=True)

    if "Traceback" in out:
        results.append(("进程无未捕获异常", False, out.strip().splitlines()[-1][:120]))
    else:
        results.append(("进程无未捕获异常", True, ""))

    failed = [r for r in results if not r[1]]
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("\n--- 进程输出 ---")
        print(out[-2000:])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
