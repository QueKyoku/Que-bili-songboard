"""打包一份「只含该上传文件」的干净副本，供 GitHub 上传用。

做法：**白名单**复制 —— 只复制明确该上传的文件，而不是复制全部再删。
白名单比黑名单安全：漏掉一个排除项就会泄露凭据，
而漏掉一个包含项只会少个文件（容易发现）。

同时生成 zip，方便网页上传。
"""

import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path('.').resolve()
OUT = ROOT.parent / 'bili-songboard-upload'
ZIP = ROOT.parent / 'bili-songboard-upload.zip'

# ── 白名单：整个目录都要的
DIRS = [
    'songboard',
    'web',
    'tools',
    'cef-4472',
    'native',
]

# ── 白名单：单个文件
FILES = [
    '.gitignore',
    'LICENSE',
    'README.md',
    'THIRD_PARTY_NOTICES.md',
    'config.example.json',
    'selftest.py',
    'proc_check.py',
    'e2e_check.py',
    'check_integration.py',
    'check_state.py',
    'diag_room.py',
    'set_cookie.py',
    'demo_full.py',
    'demo_service.py',
    'demo_flow.py',
    'demo_five.py',
    'demo_manual.py',
    '__netwatch.py',
    '__selftest_config.json',
    '启动.bat',
    '启动外部媒体源.bat',
]

# ── 永远排除（双保险，即使白名单里带了目录）
NEVER = {
    'config.json',
    'bridge/AwooNcmCefBridge.dll',
    'tools/playercap/Metabox-Nexus-PlayerCap.exe',
}
NEVER_SUFFIX = {'.obj', '.pdb', '.lib', '.exp', '.ilk'}
NEVER_DIRS = {'__pycache__', 'build-obj', 'bridge', '.git', 'tmp', 'data'}


def wanted(rel: str) -> bool:
    if rel in NEVER:
        return False
    parts = rel.split('/')
    if any(p in NEVER_DIRS for p in parts):
        return False
    if Path(rel).suffix.lower() in NEVER_SUFFIX:
        return False
    return True


if OUT.exists():
    shutil.rmtree(OUT, ignore_errors=True)
OUT.mkdir(parents=True)

copied: list[tuple[str, int]] = []


def take(src: Path, rel: str) -> None:
    if not wanted(rel):
        return
    dst = OUT / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    copied.append((rel, dst.stat().st_size))


for d in DIRS:
    base = ROOT / d
    if not base.exists():
        print(f"  !! 目录不存在，跳过: {d}")
        continue
    for p in sorted(base.rglob('*')):
        if p.is_file():
            take(p, p.relative_to(ROOT).as_posix())

for f in FILES:
    p = ROOT / f
    if not p.exists():
        print(f"  !! 文件不存在，跳过: {f}")
        continue
    take(p, f)

print(f"复制了 {len(copied)} 个文件到 {OUT.name}/")
print()

# 安全复核：干净副本里绝不能有凭据
# ⚠️ 必须用「长度 + 字符集」判断，不能只看到 MUSIC_U= 就报警：
#    selftest.py 里有测试用的假值 `MUSIC_U=fake`，光看前缀会误报，
#    而误报多了就会让人忽略真正的报警。
COOKIE_RE = [
    (r'MUSIC_U=[A-Za-z0-9%]{20,}', 'MUSIC_U'),
    (r'__csrf=[0-9a-f]{16,}', '__csrf'),
    (r'SESSDATA=[A-Za-z0-9%]{20,}', 'SESSDATA'),
    (r'bili_jct=[0-9a-f]{16,}', 'bili_jct'),
]
leak = []
for rel, _s in copied:
    if rel == 'config.json':
        leak.append(f"{rel} (整个配置文件)")
        continue
    p = OUT / rel
    if p.suffix.lower() in {'.dll', '.exe', '.png', '.jpg'}:
        continue
    try:
        t = p.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        continue
    for pat, name in COOKIE_RE:
        m = re.search(pat, t)
        if m:
            leak.append(f"{rel} ({name}: {m.group(0)[:30]}…)")

print("=" * 58)
if leak:
    print("!! 干净副本里发现凭据，不要上传：")
    for x in leak:
        print("   ", x)
else:
    print("干净副本复核通过：无凭据 ✅")
print("=" * 58)

# 打 zip
if ZIP.exists():
    ZIP.unlink()
with zipfile.ZipFile(ZIP, 'w', zipfile.ZIP_DEFLATED) as z:
    for p in sorted(OUT.rglob('*')):
        if p.is_file():
            z.write(p, p.relative_to(OUT.parent).as_posix())

print()
print(f"zip 已生成: {ZIP}")
print(f"  大小: {ZIP.stat().st_size/1024:.1f} KB")
print(f"  含 {len(copied)} 个文件")
print()
print("目录结构预览：")
dirs = sorted({str(Path(r).parent).replace('.', '(根目录)') for r, _ in copied})
for d in sorted(set(dirs)):
    print(f"   {d}/")
