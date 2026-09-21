# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> ⚠️ 依赖 B 站弹幕长连接和网易云的**非官方接口**，接口一改就可能失效。

## [0.5.0] - 2026-09-20

### 新增

- 启动时检查有无新版本，控制台顶部提醒（含「看更新日志」和「知道了」）；
  连接状态栏显示当前版本号。新增配置 `update_check`（默认开，每 6 小时复查一次，
  断网静默跳过）。
- `tools/setup_env.ps1`：自动配置环境。多镜像下载 Python（华为云 → npmmirror →
  阿里云 → 官方），校验文件大小、PE 头、数字签名后静默安装到用户目录
  （不需要管理员权限），依赖同样多镜像安装。
- `扫码登录.bat` / `启动.bat` 在没装 Python 时改为询问是否自动安装，
  拒绝或失败时回落到手动指引。
- 自检新增 `test_version_check`（41 项）、`test_ps1_files`。

### 修复

- `App()` 在本线程跑过 `asyncio.run()` 之后构造会抛
  `RuntimeError: There is no current event loop`，改用 `get_running_loop()`。
- `self.log()` 的 `print` 缺少 `flush`，stdout 接管道时日志要等下一次输入才显示。
- `tools/gsm_probe.ps1` 含中文却没有 UTF-8 BOM，PowerShell 5.1 下中文乱码。
- `tools/setup_env.ps1`：两处 PowerShell 7 专有写法（PS 5.1 语法错误）、
  误用自动变量 `$args`、LF 换行。

## [0.4.4] - 2026-09-20

### 修复

- 打包版点「自动安装 qrcode」时把 exe 自己当成 `python.exe` 去跑 pip，
  装不上还可能把程序再启动一遍；现在直接拦住并提示改用 `扫码登录.bat`。

### 新增

- `tools/check_gui.py` 补两条分支：没装 `qrcode` 时的界面状态、
  打包版点自动安装时不会真去跑 pip。

## [0.4.3] - 2026-09-20

### 修复

- `tools/login_qrcode.py` docstring 里的 `\l` 是无效转义，每次运行报 `SyntaxWarning`。
- 删除 `check_state.py`（查歌单用的，项目早就不写歌单了）。
- `__netwatch.py` 移到 `tools/netwatch.py`。

### 新增

- `tools/check_project.py`：项目体检。检查编译告警、README 引用的文件是否存在、
  临时文件/构建产物/凭据残留、版本号三处一致性、硬编码路径、git 工作区状态。
- `tools/make_upload.py` 增加白名单漏网检查。

### 变更

- `tools/make_upload.py` 白名单增删；README 补三个工具、修两处只写文件名没写目录的引用。

## [0.4.2] - 2026-09-20

### 变更

- 扫码登录相关文件移入 `tools/`，根目录只保留 `扫码登录.bat` 一个入口：
  `tools/扫码登录.pyw`（图形界面本体）、`tools/login_qrcode.py`（命令行版）。
- 同步所有引用：`扫码登录.bat` 启动路径、`tools/build_gui.ps1` 打包目标、
  `tools/check_gui.py` 加载路径、注释与 README。
- 清掉本地构建产物（`dist/`、`build/`、`*.spec`、根目录 exe）。

## [0.4.1] - 2026-09-20

### 修复

- 补上「用户电脑没装 Python」的前置检测。`.pyw` 在没装 Python 时双击不会被执行，
  因此新增 `扫码登录.bat` 作为入口，检查 Python 版本、`tkinter`、`qrcode`
  （缺则自动 `pip install`），五种情况都给明确提示。

### 变更

- `启动.bat` 主菜单新增 `[4] 扫码登录网易云`。
- README 9.1 的入口改为 `扫码登录.bat`。

## [0.4.0] - 2026-09-20

### 新增

- 图形界面版扫码登录 `扫码登录.pyw`：窗口内显示二维码，手机扫码确认后自动写入
  cookie；缺 `qrcode` 时界面提供自动安装按钮；崩溃信息写入 `扫码登录_错误日志.txt`
  并弹框告知路径。
- `tools/build_gui.ps1`：打包成单个 exe（约 23 MB），供没装 Python 的机器使用。
- `tools/check_gui.py`：检查窗口能否创建、二维码是否画出、三种状态能否正确切换。
- `songboard/qrlogin.py`：扫码逻辑抽成共用模块，命令行版与图形界面版共用。

### 修复

- `tools/build_gui.ps1`：`$ErrorActionPreference = "Stop"` 会让脚本在检查
  PyInstaller 时误退出；`--collect-subdirs` 参数不存在（应为 `--collect-submodules`）。

### 变更

- `.gitignore` 加上打包产物与错误日志。

## [0.3.0] - 2026-09-20

### 新增

- 扫码登录命令行版 `login_qrcode.py`：终端里画二维码，扫码确认后写入 cookie
  并当场验证。需要可选依赖 `qrcode`。
- `songboard/cookies.py`：cookie 提取逻辑抽成共用模块，手动复制与扫码登录共用；
  新增从 `Set-Cookie` 响应头收凭据。
- README 第九章补对应小节。

### 说明

- 「自动读浏览器里的 cookie」不可行：Chrome / Edge 127+ 给 cookie 换了
  App-Bound Encryption（值以 `v20` 开头），不解密注入浏览器进程就无法解开。
- 803（扫码成功）需要真人手机扫码，未自测；响应头里没有 `MUSIC_U` 时会打印
  全部 `Set-Cookie` 便于定位。

## [0.2.8] - 2026-09-20

### 新增

- README 新增第九章「快捷获取 Cookie」。
- `set_cookie.py` 重写：支持纯 cookie、`Cookie:` 前缀、整块请求头、
  `Copy as cURL`（bash / cmd）、JSON 等形态（改为按字段名正则取值）；
  写入后立刻调账号接口验证并打印昵称；删掉已废弃的歌单逻辑。

## [0.2.7] - 2026-09-20

### 新增

- 控制台显示所连房间（`直播间 26259546《测试》`），状态行含房间名、分区、开播状态。
- `status()` 增加 `room` 字段。

### 修复

- `diag_room.py` 用短号连接弹幕服务器会得出错误结论，现在先换成真实房间号再连。
- `diag_room.py` 不再以「历史弹幕为空」判断房间没弹幕。
- 未开播的房间明确提示「没开播就没有弹幕流，连上了也收不到」。

## [0.2.6] - 2026-09-20

### 修复

- cookie 过期被误报成「搜不到这首歌」：搜索接口返回 `code=50000005` 时明确提示
  「登录态无效」；新增 `NeteaseAuthError` 区分登录态问题与真搜不到。
- 控制台的网易云状态标签从未更新过（HTML 写死「未启用」）。
- `driver.test()` 不再依赖已废弃的歌单，改为调 `/w/nuser/account/get` 拿昵称。
- 启动时自动验证 cookie 并写入日志。

### 变更

- 网易云卡片显示四种状态；`status()` 增加 `account` 字段。
- README 补一节「能不能不用 cookie」（结论：不能，匿名接口会限流）。

## [0.2.5] - 2026-09-20

### 修复

- 控制台显示完整的叠加层地址（不再是相对路径 `/overlay`），并加「复制」「预览」按钮。
- `tools/check_overlay_dom.py` 增加对应检查。

## [0.2.4] - 2026-09-20

### 修复

- `启动.bat` 自检 Python 与依赖：缺 `websockets` 自动安装，没装 Python 或版本过旧
  给出明确提示。
- README 环境一节标明 `websockets` 必需、`cryptography` 可选。

## [0.2.3] - 2026-09-20

### 修复

- `proc_check.py` 输出被子进程污染（子进程的 `input()` 提示符直接写到当前终端），
  改用 `CREATE_NO_WINDOW` 让子进程脱离当前控制台。

## [0.2.2] - 2026-09-20

### 修复

- `启动.bat` 中文被吃掉：文件是 UTF-8 无 BOM 却写了 `chcp 65001`，导致整行消失。
  两个 `.bat` 统一改成 GBK + `chcp 936`。
- `启动.bat` 的 `[1]` 文案改为「演示模式（离线看效果；会自己造模拟弹幕，
  正式开播别用）」。

### 新增

- 自检新增批处理编码检查：`.bat` 必须是 GBK / CRLF / 无 BOM / 不含 `chcp 65001`。

## [0.2.1] - 2026-09-20

### 修复

- 叠加层完全不刷新：`web/overlay.html` 里 `let lastKey` 重复声明，
  整段内联脚本被浏览器丢弃。
- `proc_check.py` 的「叠加层可访问」断言取响应前 80 字符，改为断言结构性标记
  （`id="currentSong"` / `id="queue"`）。

### 新增

- `tools/check_overlay_dom.py`：用无头 Edge / Chrome 实跑叠加层与控制台。
- 自检新增网页内联 JS 语法检查（有 node 则 `node --check` 真编译）。
- 命令行 `--version`。

### 变更

- `web/overlay.html` 换行统一为 LF。

## [0.2.0] - 2026-09-20

### 新增

- 点歌门槛：可设为「送过礼物才能点歌」。四种判定方式 `min_total` / `any_paid` /
  `per_send` / `guard_only`；只认 `paid=true` 的礼物；事件取自 `SEND_GIFT` /
  `COMBO_SEND` / `GUARD_BUY` / `SUPER_CHAT_MESSAGE`。
- 控制台新增「点歌门槛」卡片（开关、方式、金额、时效、舰长放行、醒目留言放行、
  清空贡献记录），命令行新增 `gift` 系列指令。
- 被拦下的弹幕写入日志和控制台「最近被拦」，并说明还差多少。
- `demo_gift.py`：模拟送礼 / 上舰 / 醒目留言。
- `GiftLedger.qualified()`：只读判定，不弄脏统计与拦截记录。
- 新增门槛判定、连击计费、时效、免费礼物、脏数据容错等自检用例。

### 修复

- `reload_gift_gate()` 改门槛会清空所有人的送礼记录。
- `config.json` 带 BOM 会导致启动失败，改用 `utf-8-sig` 读取，
  JSON 出错时给出行号列号与人话说明。
- 自检等后台任务存在竞态，统一为 `_drain_tasks()`。

## [0.1.0] - 2026-09-20

首个公开版本。

### 新增

- 弹幕长连接：握手、WBI 签名鉴权、心跳、解压（brotli / zlib）、解析。
  关闭 websockets 库的协议层保活，改用 B 站自己的心跳包。
- 弹幕指令：`点歌` / `!点歌` / `#点歌` / `求歌`（前缀可配）、取消点歌、查询、切歌。
- 点歌队列：队列上限、每人限点、冷却、去重、取消、备选池、状态持久化。
- 播放队列桥：注入 `AwooNcmCefBridge.dll`，经 CEF DevTools 把歌插进网易云的
  **播放队列**（不是歌单），一次只插一首。
- 网页叠加层（供直播姬当浏览器源）与网页控制台。
- 播放状态对齐：读网易云真实播放状态，可选接入外部媒体信息源拿精确进度。
- 诊断与自检：`selftest.py`、`proc_check.py`、`check_integration.py`、
  `e2e_check.py`、`diag_room.py`、`tools/` 下的注入与编译脚本。

### 说明

- 仅支持 Windows（播放队列桥依赖进程注入与命名管道）。
- 桥需要每次重新注入，网易云一重启就失效。

[0.5.0]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.4.4...v0.5.0
[0.4.4]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.2.8...v0.3.0
[0.2.8]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/QueKyoku/Que-bili-songboard/releases/tag/v0.1.0
