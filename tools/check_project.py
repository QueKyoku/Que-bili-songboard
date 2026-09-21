"""项目体检：找「自检跑过了、但项目整体仍有毛病」的那类问题。

    python tools/check_project.py

selftest.py 管的是"功能对不对"；这个管的是"项目有没有烂"：
  1. 所有源码能不能编译（含 .pyw / tools/ 下的脚本）
  2. 每个脚本依赖的第三方模块是否装得上
  3. README 里提到的文件是不是真的存在
  4. 有没有临时文件、构建产物、凭据残留
  5. .gitignore 有没有挡住该挡的
  6. 版本号三处是否一致
  7. git 工作区是否干净、与远端是否同步

它抓到的都是"测试全绿但项目有病"的情况，比如：
  · 移动文件后 README 里的路径没跟着改
  · 某个脚本每次运行都报 SyntaxWarning（无效转义）
  · 查废弃功能的过时脚本还留在根目录
  · 打包白名单过时，上传时会静默漏文件
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOM_PS1 = b"\xef\xbb\xbf"      # UTF-8 BOM：PowerShell 5.1 认它
problems: list[str] = []
notes: list[str] = []


def bad(msg: str) -> None:
    problems.append(msg)
    print(f"  [问题] {msg}")


def ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def has_module(name: str) -> bool:
    return subprocess.run([sys.executable, "-c", f"import {name}"],
                          capture_output=True).returncode == 0


# ─────────────── 1. 编译 ───────────────
print("\n=== 1. 所有源码编译 ===")
sources: list[Path] = []
for pat in ("*.py", "*.pyw", "tools/*.py", "tools/*.pyw", "songboard/*.py"):
    sources += list(ROOT.glob(pat))
sources = sorted(set(sources))
fails = []
for f in sources:
    try:
        # 顺手把 SyntaxWarning 也当问题抓出来（无效转义就是这种）
        import warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            compile(f.read_text(encoding="utf-8"), str(f), "exec")
        for w in caught:
            if issubclass(w.category, SyntaxWarning):
                fails.append(f"{f.name}:{w.lineno} {w.message}")
    except SyntaxError as e:
        fails.append(f"{f.name}:{e.lineno} {e.msg}")
    except UnicodeDecodeError as e:
        fails.append(f"{f.name}: 不是 UTF-8（{e}）")
if fails:
    bad("编译有问题：" + "; ".join(fails[:5]))
else:
    ok(f"{len(sources)} 个文件编译通过，没有 SyntaxWarning")


# ─────────────── 2. 第三方依赖 ───────────────
print("\n=== 2. 脚本依赖的第三方模块 ===")
LOCAL = {p.stem for p in (ROOT / "tools").glob("*.py")} | \
        {p.stem for p in ROOT.glob("*.py")} | {"songboard"}
unresolved = []
for f in sources:
    tree = ast.parse(f.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):          # 函数里的 import 也看（打包时容易漏）
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    for m in sorted(mods):
        # 只挑第三方：项目自己的模块、同级脚本、标准库都跳过
        if m in LOCAL or m in sys.stdlib_module_names or m.startswith("_"):
            continue
        if (ROOT / f"{m}.py").exists() or (ROOT / "tools" / f"{m}.py").exists():
            continue
        if not has_module(m):
            unresolved.append(f"{f.name} → {m}")
if unresolved:
    bad("依赖装不上：" + "; ".join(unresolved))
else:
    ok("第三方依赖都能 import")


# ─────────────── 3. README 引用的文件 ───────────────
print("\n=== 3. README 提到的文件 ===")
text = (ROOT / "README.md").read_text(encoding="utf-8")
cand = set(re.findall(
    r"`([A-Za-z0-9_./\\\u4e00-\u9fff-]+\.(?:py|pyw|bat|ps1|json|md|cpp|html))`", text))
missing = []
for c in sorted(cand):
    if "*" in c:                       # 通配写法（如 demo_*.py）跳过
        continue
    if (ROOT / c.replace("\\", "/")).exists():
        continue
    # 项目结构图里是按层级简写的（web/ 下面只写 overlay.html），也放过
    if any((ROOT / d / c.replace("\\", "/")).exists()
           for d in ("web", "tools", "songboard", "native")):
        continue
    missing.append(c)
if missing:
    bad(f"README 提到但找不到：{missing}")
else:
    ok(f"README 里的 {len(cand)} 个文件名都能对上")

root_scripts = {f.name for f in ROOT.glob("*.py")} | {f.name for f in ROOT.glob("*.pyw")}
unmentioned = sorted(n for n in root_scripts
                     if n not in text and not n.startswith("_")
                     # README 用 demo_*.py 这样的通配提到了一批
                     and not re.search(rf"\b{re.escape(n.split('_')[0])}_\*\.py", text))
if unmentioned:
    notes.append(f"根目录这些脚本 README 没提：{unmentioned}")
    print(f"  [提示] README 没提到：{unmentioned}")
else:
    ok("根目录的脚本都在 README 里出现过")


# ─────────────── 4. 残留 / 产物 / 凭据 ───────────────
print("\n=== 4. 临时文件、产物、凭据 ===")
# __selftest_config.json 是 selftest.py 的工作文件（每次跑都会生成），
# 已经被 .gitignore 挡住，不算"残留垃圾"。
JUNK_OK = {"__selftest_config.json"}
junk = sorted([p.name for p in ROOT.glob("_*")
               if p.is_file() and p.name not in JUNK_OK] +
              [p.name for p in ROOT.glob("*.log")])
if junk:
    bad(f"根目录有残留：{junk}")
else:
    ok("没有临时文件残留")
for name in ("dist", "build"):
    if (ROOT / name).exists():
        bad(f"构建产物还在：{name}/")
for spec in ROOT.glob("*.spec"):
    bad(f"PyInstaller 的 spec 还在：{spec.name}")
if not any((ROOT / n).exists() for n in ("dist", "build")) and not list(ROOT.glob("*.spec")):
    ok("没有构建产物残留")

tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8").stdout.splitlines()
if "config.json" in tracked:
    bad("config.json 被 git 跟踪了（里面有真 cookie）")
elif subprocess.run(["git", "check-ignore", "config.json"], cwd=ROOT,
                    capture_output=True).returncode == 0:
    ok("config.json 没被跟踪，且被 .gitignore 挡住")
else:
    bad("config.json 没被 .gitignore 挡住！")


# ─────────────── 5. 版本号 ───────────────
print("\n=== 5. 版本号一致性 ===")
ver = re.search(r'__version__\s*=\s*"([^"]+)"',
                (ROOT / "songboard/__init__.py").read_text(encoding="utf-8")).group(1)
cl = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
cl_ver = re.search(r"^##\s*\[?(\d+\.\d+\.\d+)\]?", cl, re.M).group(1)
rm_ver = re.search(r"当前版本\s*\*\*v(\d+\.\d+\.\d+)\*\*", text).group(1)
if ver == cl_ver == rm_ver:
    ok(f"三处一致：{ver}")
else:
    bad(f"版本号不一致：代码={ver} 日志={cl_ver} README={rm_ver}")


# ─────────────── 5.5 PowerShell 脚本 ───────────────
print("\n=== 5.5 PowerShell 脚本编码 ===")
# 两个都是实测踩过的：
#   · 没 BOM → PowerShell 5.1 按 GBK 读，带中文的脚本整段乱码、语法都不过
#   · 每次用编辑器改完 .ps1，BOM 都会丢，所以必须有人盯着
ps1_bad = []
for p in sorted((ROOT / "tools").glob("*.ps1")):
    raw = p.read_bytes()
    txt = raw.decode("utf-8", "replace")
    has_cn = any("\u4e00" <= c <= "\u9fff" for c in txt)
    if has_cn and not raw.startswith(BOM_PS1):
        ps1_bad.append(f"{p.name}(缺 BOM)")
if ps1_bad:
    bad("这些 .ps1 有问题：" + ", ".join(ps1_bad))
else:
    ok("tools/*.ps1 的 BOM 都对")


# ─────────────── 6. 硬编码路径 ───────────────
print("\n=== 6. 硬编码的用户路径 ===")
susp = []
for f in sources:
    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        if re.search(r"[A-Za-z]:[\\/]{1,2}(Users|Documents|Desktop)", line) \
                and "expandvars" not in line:
            susp.append(f"{f.name}:{i}")
if susp:
    bad(f"疑似硬编码路径：{susp[:6]}")
else:
    ok("没有硬编码的用户目录")


# ─────────────── 7. git ───────────────
print("\n=== 7. git 状态 ===")
st = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                    capture_output=True, text=True, encoding="utf-8").stdout.strip()
if st:
    bad(f"工作区不干净：\n{st}")
else:
    ok("工作区干净")
br = subprocess.run(["git", "status", "-sb"], cwd=ROOT, capture_output=True,
                    text=True, encoding="utf-8").stdout.splitlines()[0]
if "ahead" in br or "behind" in br:
    bad(f"与远端不同步：{br}")
else:
    ok(f"与远端同步（{br}）")


print("\n" + "=" * 68)
if problems:
    print(f"发现 {len(problems)} 个问题：")
    for p in problems:
        print(f"  · {p}")
else:
    print("体检通过，没发现问题")
if notes:
    print(f"\n{len(notes)} 条提示（不一定是问题）：")
    for n in notes:
        print(f"  · {n}")
sys.exit(1 if problems else 0)
