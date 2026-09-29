# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> ⚠️ 依赖 B 站弹幕长连接和网易云的**非官方接口**，接口一改就可能失效。

## [0.7.1] - 2026-09-29

### 变更

- **README 第 9.1 节补上「用之前请看清楚这几点」**：cookie 等于登录态
  （别把 `config.json` 发给别人、别打包进分享的 zip）、杀毒软件可能报警
  （读浏览器 cookie 和窃密软件的行为很像，被拦是正常的）、这个功能依赖第三方库
  `rookiepy`、不放心就用手动方式 —— 并且说明**三种方式拿到的 cookie 完全等价**。
- **读不到凭据时的提示写得更明确**：会告诉你**试过哪些浏览器**，并让你
  **用平时上网的那个浏览器打开 music.163.com，从网页端登录一次**
  （登录完不用管它、也不用关浏览器），再回来点按钮。
  cookie 读到了但网易云说无效时，也是同一套引导。

## [0.7.0] - 2026-09-29

### 新增

- **控制台一键「🌐 从浏览器读取」网易云凭据。** 目前最省事也最可靠的方式：
  只要浏览器里登录过网易云，点一下就配好（会先验证属于哪个账号，有效才写入）。
  不用 F12、不用手抄、也不用碰手机。
  - 只读 `music.163.com` 这一个域名，只保留 `MUSIC_U` / `__csrf` / `NMTID`
    三个必需字段，其余一律丢掉；不做上传、不写日志。
  - 依次尝试 Edge → Chrome → Brave → Firefox 等；读不到会说清试过哪些、
    以及「先去浏览器登录一次」。
  - 依赖 `rookiepy`（可选）：没装时按钮会提示怎么装，其它功能不受影响。
  - 没登录的时候，控制台顶部那条提醒上的按钮也指向它。

### 变更

- **扫码登录降级为备选。** 实测确认网易云的风控已经把第三方客户端的扫码登录
  拦死了（返回 `8821 请切换其他登录方式或升级新版本再试`），反复扫也没用。
  所以控制台的主导操作换成「从浏览器读取」，扫码按钮保留但不推荐。

### 说明

- **「读浏览器 cookie 是死路」这个旧结论已经过时。** 之前实测确实读不出来
  （Chrome 127+ 的 App-Bound Encryption，cookie 值以 `v20` 开头）；
  `rookiepy`（Rust 实现）跟上了这个变化 —— 实测从 Edge 里成功读出了有效的
  `MUSIC_U`（834 字符，并用「我是谁」接口验证过账号）。

## [0.6.3] - 2026-09-29

### 变更

- **扫码登录被网易云风控拦掉时，控制台直接引导到手动方案。**
  实测确认：扫码会返回 `8821 请切换其他登录方式或升级新版本再试` ——
  表现就是"扫码授权之后毫无反应"。这是**第三方客户端普遍遇到的情况**
  （网易云对非官方客户端的登录风控很严，参考 HyPlayer 的登录说明），
  不是本程序的问题，反复扫也没用。所以现在扫码失败时会当场给出
  「手动粘贴 cookie」的三步指引，不再让用户一直点「重新生成」。
  手动方案不受这个风控影响：它走的是"你已经在浏览器里登录好了、
  只是把凭据复制过来"，不经过登录接口。
- **cookie 输入框现在能吃各种形态**：F12 里「Copy request headers」的整行、
  带 `Cookie:` 前缀、cURL 命令、甚至整块请求头 —— 都会自动挑出
  `MUSIC_U` / `__csrf` 等必需字段。以前是原样存下去，那个 `cookie: ` 前缀
  会被当成 cookie 的一部分发出去，结果是"明明填了却说没登录"。

### 修复

- 扫码返回**未知状态码**（801/802/803/800 之外，实测 8821）时：
  以前被当成"继续等"，界面一直显示"接口返回 code=8821"干等下去；
  现在会停下来显示**接口原话**（`8821：请切换其他登录方式或升级新版本再试`），
  并把完整响应写进日志 —— 就是靠这个才看清风控这回事。

## [0.6.2] - 2026-09-29

### 修复

- **扫码之后什么都不发生**（状态永远停在 801）。网易云的扫码登录是**绑会话**的：
  unikey 发给那个会话，之后轮询必须带着同一份 cookie（NMTID）回去，
  否则服务端不认识这个 key —— 手机上明明扫了、也点了确认，程序这边却一直
  「等待扫码…」，而且日志里一切正常（请求全成功、全返回 801），特别难查。
  现在整个扫码流程共用一个 cookie jar。
  附带修掉一个把会话保持**彻底废掉**的写法：请求头里写死了一个空的
  `Cookie: `，把 cookie jar 自动带上的 cookie 整个覆盖了（自检里加了一条盯它）。
- 修 0.6.1 里判错的方向：那个接口实测**没有 302 跳转**（`hops=[200]`），
  单次轮询只要 0.1 秒，所以"凭据在跳转那一跳上"的假设不成立。
  手动跟跳转的逻辑保留（无害，且万一以后真有跳转能兜住），
  但真正的病根是会话没保持。
- 扫码的诊断信息补全：控制台上会显示**查询次数和状态码**
  （`等待扫码…（第 12 次 · code=801）`），服务端在状态变化时写一条
  `📱 扫码状态：801 …`。这样"没反应"能一眼分成两种情况：
  一条日志都没有 = 前端没在轮询；一直 801 = 轮询在跑但服务端不认这个码。

## [0.6.1] - 2026-09-29

### 修复

- **控制台扫码成功却拿不到 cookie**（0.6.0 引入）。凭据是 `Set-Cookie` 下发的，
  而它挂在**跳转那一跳**（302）上 —— `urlopen` 默认会自动跟跳转，中间那个响应头
  一丢，最后拿到的响应里只有 `NMTID`，看起来像"扫码成功了但没给凭据"。
  对照实测：老代码在那个接口上收到的 `Set-Cookie` 里没有 `MUSIC_U`，
  而手动跟跳转能收到。现在改成手动跟跳转、把每一跳的 `Set-Cookie` 都收下来；
  判成功也改成**看有没有真的拿到 `MUSIC_U`**，而不是只看 body 里的 code。
  万一还是没捞到，报错会带上跳转链、收到的 Set-Cookie 和接口原样返回 ——
  803 那一步没法离线自测，只能靠这份诊断。
  回归测试用本地小服务器复现了"凭据在跳转链上"，不依赖真接口。

## [0.6.0] - 2026-09-29

### 新增

- **控制台里就能扫码登录网易云。** 不用退出程序、也不用双击 bat 开窗口 ——
  「网易云（播放队列）」卡片里有「📱 扫码登录」，点了当场出二维码
  （前端 Canvas 画的，不需要图片接口、也不需要 Pillow），手机扫一下、点确认，
  cookie 自动填好并**立刻生效**，不用重启点歌板。
- **没登录会在控制台提醒。** 没填 cookie / cookie 过期 / 网易云功能没启用时，
  控制台顶部弹一条提醒，写清后果（搜不到歌 → 拿不到歌曲 id →
  歌插不进播放队列）并带「扫码登录」按钮，就地修好。

### 变更

- **扫码不再从 bat 走。** `启动.bat` 的 `[4] 扫码登录网易云` 去掉，
  菜单里改成提示"启动后打开控制台扫码"。`扫码登录.bat`（独立窗口版）
  保留作为备选 —— 万一控制台打不开还能用它。

## [0.5.0] - 2026-09-20

### 变更

- **演示弹幕和文档示例不再用周杰伦的歌**（网易云没有版权）。
  演示歌单里「稻香」换成「大鱼」，示例统一改用《起风了》；
  另外演示歌单里那首「晴天花」实测会搜出《烟花易冷》——
  点名和实际放的不是一首，演示时最容易穿帮，换成了「打上花火」。
  演示歌单逐首用真 cookie 搜过一遍，21 首全部对得上。
- **播放状态完全以播放器为准；点歌队列只做「插入下一首」，不主动切歌、不主动起播。**
  - 去掉**切歌**：弹幕「切歌」、控制台「⏭ 下一首」、命令行 `n`。它们本来就只改
    点歌板自己的队列、不碰播放器，下一轮对齐就弹回去了。
  - 去掉**自动下一首**（含控制台那个开关）：那套逻辑会调 `play_now` 替主播决定
    现在放什么，还会出现「程序以为播完了、播放器其实还在放」的错位。
  - 去掉**空闲时主动起播**：播放器没在放歌时，新点的歌只排队等着。
  - 去掉**手动「✅ 这首播完了」**：播放器还在放的话，下一轮对齐就纠正回来了。
  - 新增：播放器在放**队列外**的歌（主播自己的歌单）时，叠加层和控制台
    **跟着显示那首**并标注「不在点歌队列」，而不是挂着上一首点歌。
  - 相关配置项一并删除：`playback.authority`、`playback.fallback_to_board`、
    `queue_only.play_if_idle`、`ncm_bridge.play_if_idle`、`media.auto_next`、
    `media.trust_browser_progress`、`danmaku.skip_keywords` 等。

### 新增

- 启动时检查有无新版本，控制台顶部提醒（含「看更新日志」和「知道了」）；
  连接状态栏显示当前版本号。新增配置 `update_check`（默认开，每 6 小时复查一次，
  断网静默跳过）。
- `tools/setup_env.ps1`：自动配置环境。多镜像下载 Python（华为云 → npmmirror →
  阿里云 → 官方），校验文件大小、PE 头、数字签名后静默安装到用户目录
  （不需要管理员权限），依赖同样多镜像安装。
- `扫码登录.bat` / `启动.bat` 在没装 Python 时改为询问是否自动安装，
  拒绝或失败时回落到手动指引。
- **控制台里能一键重新注入播放队列桥。** 桥断了时顶部弹红色横幅，
  上面有「重新注入桥」按钮 —— 以前只能去命令行跑 `tools/inject_bridge.py`，
  主播开播前手忙脚乱根本不记得。注入是后台任务（要等十几秒等管道握手），
  接口立刻返回、进度写在横幅上，修好后不用重启点歌板。
- **控制台常驻显示桥状态**（`已连接 · 已插 N 首` / `未连接`）——
  桥断了是静默降级，不显示的话主播根本没处看。
- 自检新增 `test_version_check`（41 项）、`test_ps1_files`。

### 修复

- `App()` 在本线程跑过 `asyncio.run()` 之后构造会抛
  `RuntimeError: There is no current event loop`，改用 `get_running_loop()`。
- `self.log()` 的 `print` 缺少 `flush`，stdout 接管道时日志要等下一次输入才显示。
- `tools/gsm_probe.ps1` 含中文却没有 UTF-8 BOM，PowerShell 5.1 下中文乱码。
- `tools/setup_env.ps1`：两处 PowerShell 7 专有写法（PS 5.1 语法错误）、
  误用自动变量 `$args`、LF 换行。
- **桥断线时的日志在骗人**：写的是「播放队列桥不可用，只写歌单（需手动点播放）」，
  可 `queue_only` 模式下**根本不写歌单** —— 歌只留在点歌板自己的队列里，
  网易云里什么都没有。主播照那句话去歌单里找，发现也是空的。
  现在改成说清后果 + 给出修复命令，并且同一状态只提醒一次。
- **日文歌名里的浊音认不出是同一首**：`normalize()` 缺 Unicode 规范化，
  「すずめ」的预组合写法（U+305A）和网易云返回的组合写法（U+3059 U+3099）
  看着一模一样但字节不同，相似度只有 0.33（正常应 0.85）。
  含浊音的日文歌（が/ざ/ず/だ/ば/ぱ 行）在播放对齐时会认不出是同一首；
  中文歌不受影响，所以一直没暴露。

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

[0.7.1]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.6.3...v0.7.0
[0.6.3]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.6.2...v0.6.3
[0.6.2]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/QueKyoku/Que-bili-songboard/compare/v0.5.0...v0.6.0
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
