# 哔哩哔哩点歌板

> 弹幕点歌 → 自动排队 → **自动插进网易云播放队列** → 直播画面上实时显示点歌板

给 B 站主播用的点歌工具。观众发一条 `点歌 稻香`，程序就会把这首歌加到网易云的
**播放队列**里，同时把点歌板作为网页叠加层显示在直播画面上。

**不需要给直播姬装插件** —— 直播姬/OBS 都不支持第三方插件，所以这是一个独立的小程序：
一边读你直播间的弹幕，一边把点歌板作为网页叠加层输出。

- ✅ 网易云播放队列自动排队（真正的"点了就能听到"）
- ✅ **绝不改动你的网易云歌单** —— 一个字节都不动
- ✅ 网页叠加层，直播姬 / OBS 通用
- ✅ 演示模式，不连直播间也能先看效果
- ⚠️ 播放队列功能需要往网易云注入一个 DLL，**请先读第三章再决定用不用**

---

## 目录

- [一、它能做什么](#一它能做什么)
- [二、快速开始](#二快速开始)
- [三、播放队列桥（核心功能，含风险说明）](#三播放队列桥核心功能含风险说明)
- [四、直播姬 / OBS 里怎么加](#四直播姬--obs-里怎么加)
- [五、弹幕指令](#五弹幕指令)
- [六、播放状态与自动切歌](#六播放状态与自动切歌)
- [七、配置项](#七配置项)
- [八、自检与诊断工具](#八自检与诊断工具)
- [九、常见问题](#九常见问题)
- [十、项目结构](#十项目结构)
- [十一、致谢与第三方代码](#十一致谢与第三方代码)
- [十二、许可证](#十二许可证)

---

## 一、它能做什么

| 功能 | 说明 |
|---|---|
| 读弹幕 | 连 B 站弹幕长连接，解析 `DANMU_MSG`，识别点歌指令 |
| 队列管理 | 每人限点、冷却、队列上限、去重、取消点歌、备选池 |
| **插进播放队列** | 把点中的歌插到网易云的"下一首"，**不动你的歌单** |
| 网页点歌板 | 叠加层（给直播画面用）+ 控制台（给你自己用） |
| 播放状态对齐 | 读网易云真实播放状态，点歌板显示的就是实际在放的 |
| 时长/进度 | 可选接入外部媒体源，拿到精确进度用于自动切歌 |

**没做的**：自动回复弹幕、点歌付费/礼物、多平台（QQ音乐/酷狗）。
未来要接别的播放器，只需在 `songboard/netease.py` 里加一个 `MusicDriver` 子类。

---

## 二、快速开始

### 环境

- Windows（播放队列桥依赖 Windows API）
- Python 3.10+（开发环境用的是 3.12）
- 依赖：`websockets`、`cryptography`（装不上 cryptography 也能跑，只是网易云网页接口会失效）

```bash
pip install websockets cryptography
```

### 跑起来

```bash
# 1) 复制配置模板（config.json 含凭据，不在 git 里）
copy config.example.json config.json

# 2) 先看效果：演示模式，本地随机生成弹幕，不连网络
python -m songboard --mode demo --open

# 3) 连自己的直播间
python -m songboard --room <你的房间号> --open
```

Windows 上也可以直接双击 **`启动.bat`**。

### 命令行参数

```
python -m songboard [选项]

  --room ROOM         直播间号（注意：是直播间号，不是 UID）
  --port PORT         本地网页端口，默认 8765
  --mode {demo,live}  演示模式 / 直播间模式
  --config CONFIG     配置文件路径
  --open              启动后打开控制台
  --no-persist        不把队列状态存到磁盘
```

### 两个页面

| 地址 | 用途 |
|---|---|
| `http://127.0.0.1:8765/control` | **控制台**：看队列、切歌、加歌、改房间号、改模式 |
| `http://127.0.0.1:8765/overlay` | **叠加层**：加进直播姬的浏览器源 |
| `http://127.0.0.1:8765/overlay?bg=1` | 叠加层预览（带背景，方便先调样式） |

叠加层可以拼参数调外观：

```
/overlay?w=560&fs=30&theme=light&accent=%23ff6b9d&show=10
        w=面板宽度  fs=字号  theme=dark|light  accent=主色  show=显示几条
```

### 控制台快捷键（命令行里）

程序启动后会在终端进一个简易 REPL：

```
回车            查看状态
n / next        下一首（切歌）
a 歌名          手动加入队列        例：a 稻香
r 序号          移除队列里第 N 首
top 序号        把第 N 首置顶
clear           清空队列
mode demo|live  切换演示/直播间模式
room 房间号     设置直播间号并重连
ne              查看网易云驱动状态
h / help        显示帮助
q / quit        退出
```

没有终端（后台启动、管道）时会自动转成常驻模式，不会因为 EOF 退出。

---

## 三、播放队列桥（核心功能，含风险说明）

这是本项目最有价值、也最需要谨慎对待的部分。**用之前请读完本节。**

### 3.1 为什么需要它（以及为什么放弃了"写歌单"）

先把两条实测结论摆出来，它们是整个设计的基础：

**结论一：往网易云歌单加歌，不会改变播放队列。**

歌单是"库"，播放队列是"正在放 / 接下来放"，两者**完全独立**。
往歌单加一首歌，播放队列纹丝不动。

**结论二：网易云加歌永远插在歌单的第 1 位。**

没有"追加到末尾"的接口。要追加只能"全部删除 + 倒序重加"，
是最重的操作，还很容易出错。

所以早期版本做的"写歌单"方案有两个致命问题：
达不到"自动播放"的目的，而且**在动你自己的歌单**。
现在这条路**默认关闭**（`netease.write_playlist = false`），
代码保留在 `songboard/netease.py` 里仅作存档。

### 3.2 它是怎么把歌插进播放队列的

网易云的播放器页面里存在这样的内部命令：

```js
{cmd:'playingList', type:'addToNext', value:'<歌曲id>'}   // 插到"下一首"
{cmd:'play',        type:'song', id:'<歌曲id>'}           // 立刻播放
```

但这条命令**只能从页面自己的 JS 上下文里发出**。

历史上有人用共享内存 `orpheus_ipc_{pid}_{tick}` +
`SendMessageTimeout(hwnd, 0x8001, ...)` 投递。**这条路在新版客户端上已经不通了。**
我们实测过：把非法命令投进去，返回值与合法命令**完全一样**
（都返回 `receiver=0`）—— 说明根本没有接收方在解析，投递是石沉大海。

现在的做法：**注入 `AwooNcmCefBridge.dll` 进 `cloudmusic.exe`**，
让它用 CEF 的 DevTools 通道在页面里执行那段 JS，
再通过**命名管道**和外面的 Python 通信。

```
songboard/ncmbridge.py  ──命名管道──▶  AwooNcmCefBridge.dll（在 cloudmusic.exe 内）
   (本项目，Python)                      (第三方，C++)  ──CEF DevTools──▶  页面 JS
```

### 3.3 ⚠️ 风险说明（认真读）

**1. 这是进程注入。** 桥 DLL 会被加载进网易云音乐的进程。
它做的是只读操作（读 CEF 版本、通过 DevTools 发命令），
但**进程注入本身是有风险的操作**，可能让客户端不稳定。
介意的话请不要用这一章，程序其余部分照常工作。

**2. 桥的源码不是本项目原创。** 见[第十一章](#十一致谢与第三方代码)。

**3. 网易云一重启，桥就没了。** DLL 是内存注入，不落盘、不改客户端文件，
所以**每次重开网易云都要重新注入一次**。

**4. 网易云换 CEF 内核会导致失效。** 桥会做硬校验（见 3.5）。

### 3.4 安装和使用

需要 **Visual Studio 2022 的 C++ x64 生成工具**（用来编译桥 DLL）。

```powershell
# 1) 编译桥 DLL（产物：bridge\AwooNcmCefBridge.dll）
powershell -File tools\build_bridge.ps1

# 2) 先干跑，看看目标进程找得对不对（不写入任何东西）
python tools\inject_bridge.py --dry-run

# 3) 注入（需要管理员权限）
python tools\inject_bridge.py

# 4) 不想用了就卸载
python tools\inject_bridge.py --eject
```

注入成功后应该看到：

```
OK READY cef=91.2.2+4472.169 validation=exact route=internal-devtools events=ready
```

然后在 `config.json` 里打开：

```jsonc
"ncm_bridge": {
  "enabled": true
}
```

**关于卸载的实测结论（和直觉不同）：**
`FreeLibrary` 会返回成功，但模块**不会真的从进程里消失** ——
桥的 `DllMain` 启动了常驻 worker 线程，进程里对它有额外引用。
`--eject` 会如实告诉你"模块仍然驻留"，这是正常的，**别反复重试**
（重复 `FreeLibrary` 可能破坏引用计数）。
**要彻底移除桥，正常关闭并重新打开网易云即可。**

**另一个坑：换路径重新注入会产生第二份副本。**
Windows 的 `LoadLibrary` 是**按路径**计引用的，从 A 路径注入过、再从 B 路径注入，
进程里就会有**两个**同名模块（各自独立的管道服务器）。
实测功能不受影响，但不够干净。所以固定用一条路径。

### 3.5 为什么它在你的机器上能过校验

桥 DLL 会调 `libcef.dll` 的 `cef_version_info` / `cef_api_hash` 做**硬校验**。
实测（网易云 3.1.40.205461）：

| 项 | 上游期望 | 实测 | |
|---|---|---|---|
| CEF 版本 | 91.2.2 / commit 2376 / chromium 91.0.4472.169 | 完全一致 | ✅ |
| `cef_api_hash(0)` universal | `37d5f9f068cf9b5ecfb6d039fc3c5c56be3864ba` | 一致 | ✅ |
| `cef_api_hash(1)` platform | `306fdfb40c5dbdc34992b9a5669c199a64749d5c` | 一致 | ✅ |
| `cloudmusic.exe` | 3.1.38.205386 | 3.1.40.205461 | ⚠️ 仅"精确匹配"标记 |

也就是说：**网易云外壳升级了，但 CEF 内核没换**，所以桥能直接跑。

哪天网易云换了 CEF 内核，注入后会看到 `REFUSED` 开头的错误，
需要重新对齐版本号和 API 哈希。

### 3.6 插队顺序：一次只插一首

`addToNext` 的语义是**"把这首歌插入到当前播放曲目之后"**——
原有队列完整保留，不会挤掉任何歌。对照实验：

```
ADD_NEXT(稻香, 原位置[0])  →  稻香移动到 [6]（当前曲目在 [5]）
曲目集合完全没变
```

**但后插的会排在前面。** 当前是 A，依次插 B、C，结果是 `A, C, B`。
所以一次插多首必然得到反序。

因此程序的做法（也是主播要求的流程）是**一次只插一首**：

```
A 在播时，把 B 插到"下一首"
   ↓ 等 B 自然播起来
B 在播时，把 C 插到"下一首"
   ↓ 以此类推
```

判定"B 有没有开始播"用的是**网易云报告的当前曲目**，
不能用点歌板自己的状态 —— 那个会被对齐逻辑改来改去，会误判。

### 3.7 桥不可用时会怎样

**永远不影响点歌板。** 没注入 / 没编译 / 网易云没开 / 版本不匹配，
都只记一条日志：

```
ℹ️ 播放队列桥不可用，只写歌单（需手动点播放）
```

此时程序退化成"纯点歌板"：弹幕照收、队列照管、叠加层照显示，
只是不会自动往播放队列里放歌。

---

## 四、直播姬 / OBS 里怎么加

两边都是"加一个浏览器源 / 浏览器"：

1. 程序启动后，拿到叠加层地址：`http://127.0.0.1:8765/overlay`
2. 在直播姬里添加**浏览器源**（OBS 里叫"浏览器"）
3. 地址填上面那个，宽高按你的需求（面板默认自适应，常见的 560×400 左右）
4. **勾上"透明"**，否则会是一块白底或黑底

调试建议：先用 `http://127.0.0.1:8765/overlay?bg=1`（带背景）调样式和字号，
调好后再换成不带 `bg=1` 的正式地址。

> ⚠️ 叠加层用的是 WebSocket 实时推送，**不需要刷新**。
> 如果你改了 `web/overlay.html`，要先重启程序再刷新一次。

---

## 五、弹幕指令

解析逻辑在 `songboard/command.py`，规则如下（**按这个顺序匹配**）：

| 指令 | 触发 | 说明 |
|---|---|---|
| 取消点歌 | `取消点歌` / `撤销点歌` | 优先级最高 |
| 查询 | `查询点歌` / `我的点歌` / `点歌查询` | |
| 点歌 | `点歌 歌名` / `!点歌 歌名` / `#点歌 歌名` / `求歌 歌名` | 前缀可配 |
| 切歌 | `点歌 切歌` 或单独发 `切歌` / `下一首` / `跳过` | 关键词可配 |

细节：

- **前缀按长度倒序匹配**，所以 `!点歌` 不会被 `点歌` 抢先吃掉
- 歌名开头的空白和 `:：,，、-—` 会被自动去掉
- **把某个关键词列表留空 = 关闭该指令**。比如
  `"skip_keywords": []` 就彻底关掉切歌（推荐这么做，避免观众乱切）
- 同一个用户：默认最多同时点 2 首、两次之间至少隔 30 秒（都可配）
- 歌名重复会被拦（日志里记 `duplicate`）

---

## 六、播放状态与自动切歌

程序需要知道"现在在放什么、放了多久"，才能：
把点歌板的"正在播放"对齐到实际、以及判断一首歌是不是播完了。

### 用哪个信号读"现在在放什么"

按可靠性排序（`config.json` 的 `media.prefer_apps`）：

1. **网易云客户端** —— 有窗口标题（`歌名 - 艺人`），最可靠
2. Spotify / PotPlayer —— 有系统媒体会话
3. **浏览器**（Edge/Chrome） —— ⚠️ 最不可靠，见下

### 浏览器为什么不可靠

浏览器的媒体会话**常常没有标题**，分不清是"网易云网页版在放歌"还是
"你在看 B 站视频"。直接采信会导致看视频时把点歌队列切乱。

所以默认**不采信浏览器的进度**，除非：

- 配置里显式打开 `media.trust_browser_progress`，或
- 该会话自带标题、且与当前歌名相似度达标

**想稳，就用网易云客户端。**

### 想要精确进度：接一个外部媒体信息源

网易云客户端**不提供**"当前播放进度"，所以自动切歌没法精确判断"播完了没"。
可以接入本机的第三方服务（如 Metabox-Nexus-PlayerCap）来拿到进度：

```jsonc
"extapi": {
  "enabled": true,
  "urls": [
    "http://127.0.0.1:8766/cloudmusicv3/song_info",   // 有 name/singer，没时长
    "http://127.0.0.1:8766/cloudmusicv3/all_lyrics"   // 有 duration/position，没歌名
  ]
}
```

两个接口各缺一半字段，所以要**配两个、合并着读**。
详见 `tools/playercap/README.md`（含"它会向外发遥测"的说明）。

### 怎么判"播完了"

- 有精确进度 → 剩余时间小于 `media.almost_done_seconds`（默认 3 秒）就算播完
- 没有进度 → 按 `media.duration_fallback`（默认 300 秒）估算
- `media.grace_seconds`（默认 9 秒）：歌名变化后等几秒再判定，过滤切歌抖动

### 关于"自动切歌"的一个重要说明

**在 `playback.authority = "netease"`（默认）模式下，程序不会自动切歌。**

既然播放状态以网易云为准，那就该由**你来控制播放**，
程序只在每一轮轮询时把点歌板的显示对齐。
否则会出现"程序以为播完了、把队列推进了，但网易云其实还在放"的错位。

对播放队列桥来说这是**正确**的：程序只负责把下一首排上去，
切歌交给网易云自然播放或你手动操作。

---

## 七、配置项

完整默认值见 `config.example.json`。下面只讲需要动的。

### 常用

```jsonc
{
  "room_id": 12345,              // 直播间号（不是 UID）
  "mode": "live",                // demo=演示模式  live=连直播间
  "http_port": 8765,

  "netease": {
    "enabled": true,             // 开启才能搜索歌曲、查时长（只读）
    "cookie": "",                // 网易云登录态，见下
    "write_playlist": false      // ⚠️ 保持 false，别动你的歌单
  },

  "queue_only": {
    "enabled": true              // 只插播放队列模式（默认开）
  },

  "ncm_bridge": {
    "enabled": true              // 播放队列桥，需要先注入 DLL
  }
}
```

### Cookie 怎么填

登录网易云网页版 → F12 开发者工具 → 从请求头里复制 `Cookie`，
至少要有 `MUSIC_U` 和 `__csrf`。也可以用辅助脚本：

```bash
python set_cookie.py --cookie "MUSIC_U=...; __csrf=..."
```

搜索和查时长**必须**有 cookie；没有的话日志会提示"没有网易云登录态"。

> ⚠️ `config.json` 里是**真实的登录凭据**，已被 `.gitignore` 排除。
> 千万别提交、别截图、别发给别人。

### 队列规则

```jsonc
"queue": {
  "max_size": 30,          // 队列上限，满了进备选池
  "per_user_limit": 2,     // 每人同时最多几首
  "cooldown_seconds": 30,  // 同一人两次点歌最小间隔
  "max_song_name_len": 40
}
```

### 点歌板外观

```jsonc
"board": {
  "title": "点歌板",
  "subtitle": "弹幕发送「点歌 歌名」",
  "show_size": 8,          // 叠加层显示几条
  "theme": "dark"          // dark | light
}
```

### 播放队列桥

```jsonc
"ncm_bridge": {
  "enabled": true,
  "insert_next": true,     // 插到"下一首"
  "play_if_idle": true,    // 队列空着时直接开始播第一首
  "timeout": 3.0,          // 管道响应超时（秒）
  "probe_seconds": 30      // 桥可用性缓存秒数
},
"queue_only": {
  "enabled": true,         // true = 只插播放队列，绝不碰歌单
  "play_if_idle": true
}
```

> **`queue_only.enabled = true` 时，程序启动会把 `netease.auto_add`
> 和 `netease.write_playlist` 强制改回 `false` 并写盘。**
> 这是刻意的保护 —— 少写一处判断就可能漏掉一条写歌单的路径。

### 已废弃的歌单相关配置

`netease.auto_add` / `netease.playlist_id` / `netease.reorder` /
`netease.append_last` / `netease.max_tracks` / `netease.prune_played` /
`netease.order_playlist` 都属于**已废弃的"写歌单"方案**，默认全关。
代码保留在 `songboard/netease.py` 里仅作存档。
控制台上也已经没有对应开关了，`/api/netease/enable` 也**不接受**
`auto_add` / `playlist_id`。

---

## 八、自检与诊断工具

### 跑测试

```bash
python selftest.py         # 离线自检：指令/队列/协议/清理/插队逻辑（225 项）
python proc_check.py       # 进程级：无终端启动不会退出（7 项）
python check_integration.py  # 集成：点歌→插队列接线（9 项，用真实桥）
```

```bash
# 真连测试（只连你自己这个房间，不会发任何东西）
python selftest.py --live <你的房间号>
```

> `selftest.py` 里的"只插播放队列""清理策略"等用例会刻意忽略你 `config.json`
> 里的相关设置，自己显式设好 —— 否则改一下配置测试就会假失败。

### 端到端检查（⚠️ 有副作用）

```bash
python e2e_check.py http://127.0.0.1:8765
```

它会往**正在运行的那个服务**加歌、切歌、清空队列。
如果那个服务开着 `netease.auto_add`，还会连带写你的真实歌单。
所以脚本会先检查，发现风险就**拒绝运行**；确实要跑就设
`DSH_E2E_ALLOW_NETEASE_WRITE=1`。

### 诊断工具

| 工具 | 用途 |
|---|---|
| `python diag_room.py <房间号> [秒数]` | 直连弹幕服务器，打印收到的每一个原始帧 |
| `python tools/bridge_client.py` | 查看桥的状态、找主进程 |
| `python tools/list_netease_windows.py` | 列出网易云各窗口，确认要注入哪个进程 |
| `python tools/inject_bridge.py --dry-run` | 注入前检查（不写入） |
| `tools/gsm_probe.ps1` | 列出系统媒体会话（看有哪些播放器能被读到） |

### 手动跑一遍完整流程

```bash
python demo_full.py      # 往点歌板点 5 首，观察是否按顺序进播放队列
python demo_service.py "点歌 稻香" 观众A   # 对运行中的服务点一首歌
python demo_flow.py      # 不连直播间，走完整业务逻辑
```

> `demo_full.py` **不会**用 `PLAY` 强切歌 —— 它只负责点歌和观察，
> 切歌由你手动操作或等它自然播完。早期版本为了跑完测试强行切歌，
> 那等于替你按了"下一首"，是错的。

### 验证顺序的正确方法

**必须看播放队列的实际落点，不能只看播放标题。**
标题采样每 2 秒一次，快速连续切歌会漏记/重记，得出错误结论。

```bash
# 正确：读播放队列文件里的 displayOrder
python -c "import json,os;p=os.path.join(os.environ['LOCALAPPDATA'],'Netease','CloudMusic','WebData','file','playingList');items=sorted(json.load(open(p,encoding='utf-8'))['list'],key=lambda x:x.get('displayOrder',0));print([(it.get('track') or {}).get('name') for it in items[:20]])"
```

另外，**测试用的歌必须是队列里原本没有的**，
否则旧条目恰好补上位置，会让"顺序对了"变成巧合（我们踩过这个坑）。

---

## 九、常见问题

**自己发的弹幕抓不到？**

先分清是谁的问题。用 `python diag_room.py <房间号> 45`，它会打印收到的每一个原始帧：

- **一个 `DANMU_MSG` 都没有** → 你的弹幕没进这个房间的流。
  用隐身窗口打开自己直播间，看右侧弹幕列表里有没有你刚发的那条：
  - 也没有 → B 站没发出去（重复内容被拦 / 发送失败 / 房间号不对）
  - 有 → 是程序的问题
- **有 `DANMU_MSG`** → 那是解析或指令前缀的问题

**为什么连接老是断？**

B 站弹幕服务器**不响应 websocket 协议层 ping**，所以库自带的保活会每隔约 40 秒
以 `keepalive ping timeout` 硬断一次（正好把弹幕丢在重连窗口里）。
现在关掉了库的协议保活，改用 B 站自己的心跳包（op=2 → 服务器回 op=3）判断存活，
再配一个 95 秒的兜底重连。

**没开播能收弹幕吗？**

不能。B 站只有开播时才有弹幕流；未开播时连接能建立、认证能通过，但收不到消息。
想先调界面就用 `python -m songboard --mode demo`。

**`getDanmuInfo` 报 -352 / 签名错误？**

程序已内置 WBI 签名（自动取 `img_key` / `sub_key`）。若仍失败，多半是 B 站改了接口，
把日志里的错误码发出来。

**点歌板会隔一小段时间闪一下？**

已修。原因是叠加层每次都 `innerHTML = ''` 整个重建列表，
而每个条目带 `animation: grow`。服务每 2 秒广播一次状态，
于是每 2 秒所有条目重放一次入场动画。
现在改成**内容指纹比对 + 按 id 复用 DOM**，只有真正新增的歌才播动画。

**点了两首歌，但只加进去一首？**

已修。`addToNext` 是"插入到当前曲目之后"而不是"追加"，
连插两首会得到反序（当前=A，插 B 再插 C → `A, C, B`）。
现在一次只插一首，等它开始播才插下一首。

**队列里明明是空的，点歌却一直不插？**

已修。闸门原先只看内存标志（"我插过谁"），
队列里本来就有别的歌占着"下一首"时，程序会误以为"已经排好了"，于是永远不再插。
现在改成看播放队列的**实际状态**。

**叠加层在直播姬里是白底/黑底？**

透明背景没生效。检查浏览器源的"透明"开关；
另外确认地址是 `/overlay` 而不是 `/overlay?bg=1`（后者是故意加背景的预览用）。

**端口被占用？**

`python -m songboard --port 8800`，然后直播姬里的地址跟着改。

**想让弹幕收到"已加入队列"的回复？**

程序已经在 `reply_queue` 里攒好了回复文案（`songboard/main.py`），
但**没有发送弹幕** —— 那需要你的登录态，涉及风控，属于独立讨论的部分。

---

## 十、项目结构

```
songboard/                 主程序（Python）
  __main__.py              命令行入口
  main.py                  App 编排：轮询、对齐、队列推进、插队
  bilibili.py              弹幕长连接（握手/认证/心跳/解压/解析）
  wbi.py                   WBI 签名
  command.py               弹幕指令解析
  store.py                 点歌队列（上限/限流/去重/持久化）
  media.py                 读"正在放什么"（窗口标题 + 系统媒体会话）
  extapi.py                外部媒体信息源适配
  netease.py               网易云歌单驱动（**已废弃，仅存档**）
  ncmbridge.py             ★ 播放队列桥客户端（命名管道）
  webui.py                 HTTP + WebSocket 服务
  config.py                配置读写

web/
  overlay.html             叠加层（给直播画面）
  control.html             控制台（给你自己）

native/
  AwooNcmCefBridge.cpp     ⚠️ 第三方源码，见第十一章

cef-4472/include/          CEF 头文件 shim（自写，替代约 1GB 官方包）

tools/
  build_bridge.ps1         编译桥 DLL
  inject_bridge.py         注入 / 卸载（支持 --dry-run / --eject）
  bridge_client.py         桥的管道客户端
  list_netease_windows.py  找网易云主进程
  gsm_probe.ps1            列出系统媒体会话
  playercap/               可选的外部媒体信息源（exe 未入库）

bridge/                    编译产物（DLL，已 gitignore）
build-obj/                 编译中间文件（已 gitignore）

selftest.py                离线自检（225 项）
proc_check.py              进程级检查（7 项）
e2e_check.py               端到端检查（⚠️ 有副作用）
check_integration.py       集成检查（9 项）
demo_*.py                  手动演练脚本
diag_room.py               弹幕直连诊断
set_cookie.py              填网易云 cookie
启动.bat / 启动外部媒体源.bat
```

---

## 十一、致谢与第三方代码

### 播放队列桥的源码不是本项目原创

`native/AwooNcmCefBridge.cpp` 来自 **Enkianssus** 的项目
[**awoo-connectors**](https://github.com/Enkianssus/awoo-connectors)
（属于 [Awoo MusicBot 嗷呜点歌机](https://github.com/Enkianssus/AwooMusicBot)）。

**「注入网易云、用 CEF DevTools 把歌插进播放队列」这个思路和实现都是他们的。**
没有这份工作，本项目不可能做到"点歌自动进播放队列"。

⚠️ **请注意授权状态：上游仓库没有 LICENSE 文件**，
因此该文件版权归原作者所有，**不受本项目的 MIT 许可证覆盖**，
严格讲属于未经许可的分发。详细说明见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

如果你要长期使用，稳妥做法是**直接去上游仓库 clone 源码**：

```powershell
git clone https://github.com/Enkianssus/awoo-connectors
powershell -File tools\build_bridge.ps1 `
  -BridgeSource ..\awoo-connectors\native\Netease\AwooNcmCefBridge.cpp
```

`build_bridge.ps1` 支持 `-BridgeSource` 指定任意路径，不必用仓库里这份拷贝。

### 本项目原创的部分

- `songboard/` 全部 Python 代码（弹幕连接、WBI 签名、指令解析、队列规则、
  播放队列推进策略、Web 控制台与叠加层）
- `web/` 控制台与叠加层页面
- `cef-4472/include/*.h` —— 让上游那个 `.cpp` 能编译的极简 CEF 头文件 shim
  （替代约 1 GB 的官方发布包）
- `tools/` 下的注入器、编译脚本、诊断工具

### 其他第三方

- [Metabox-Nexus-PlayerCap](https://github.com/Metabox-Nexus/PlayerCap) ——
  可选的外部媒体信息源，exe 未随仓库提交，见 `tools/playercap/README.md`
- CEF 官方接口头文件（BSD 3-Clause）—— shim 的类层次与虚函数顺序参照了它，
  以确保与 `libcef.dll` 的 vtable 布局一致

---

## 十二、许可证

本项目原创代码采用 **MIT 许可证**，见 [`LICENSE`](LICENSE)。

第三方代码不在此许可范围内，见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

---

## 最后：已知限制

诚实列一下目前做不到的事：

- **桥需要每次重新注入。** 网易云一重启就失效。
- **网易云换 CEF 内核会失效。** 需要重新对齐版本号和 API 哈希。
- **一次只插一首。** 受 `addToNext` 的语义限制，无法提前把整排待播放进队列。
- **播放队列清不干净。** 桥只能插入、不能删除，队列是历史累积的，
  要清得你自己在网易云里操作。
- **依赖非官方接口。** 网易云没有官方第三方 API，用的是网页端接口和内部命令，
  **可能随官方改动失效**。
- **只有 Windows。** 播放队列桥依赖 Windows 的进程注入与命名管道。
