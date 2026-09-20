# 第三方代码署名 / Third-Party Notices

本仓库包含一份**第三方源代码**。在此明确标注其来源与授权状态。

---

## 1. `native/AwooNcmCefBridge.cpp`

| 项 | 内容 |
|---|---|
| **原作者** | Enkianssus |
| **来源仓库** | https://github.com/Enkianssus/awoo-connectors |
| **具体路径** | `native/Netease/AwooNcmCefBridge.cpp` |
| **所属项目** | [Awoo MusicBot](https://github.com/Enkianssus/AwooMusicBot)（嗷呜点歌机） |
| **获取方式** | 从上游仓库 `main` 分支原样拷贝，**未作任何修改** |
| **文件大小** | 44,223 字节 |

### ⚠️ 授权状态：上游没有 LICENSE 文件

**这个文件不是 MIT、GPL 或任何已知开源许可证授权的。**
上游仓库（`awoo-connectors` 与 `AwooMusicBot`）**都没有 LICENSE 文件**。
按照著作权法默认规则，**没有许可证 = 版权归原作者所有（All rights reserved）**，
未经授权不得再分发。

本仓库收录它是为了**互操作性**（让用户能在自己的机器上编译出桥接 DLL），
并在此完整标注作者与来源。**但这不等于已获得原作者授权。**

### 如果你要使用本项目

- 请自行判断这一风险。稳妥做法是**直接去上游仓库 clone 源码**，
  不要依赖本仓库里的这份拷贝。
- 本项目的编译脚本允许你指定任意路径的源码（见下），
  所以完全可以不用仓库里这份。

### 如果你是原作者

若你不希望这份拷贝留在本仓库，请提 issue，我会立即移除。
若你愿意给它加个许可证，也欢迎告知，我会同步更新本文件。

### 这个文件是干什么的

它把 `AwooNcmCefBridge.dll` 注入网易云音乐客户端（`cloudmusic.exe`），
通过 CEF 的 DevTools 通道在页面 JS 里执行内部命令，从而把歌曲插入**播放队列**：

```js
{cmd:'playingList', type:'addToNext', value:'<歌曲id>'}
{cmd:'play', type:'song', id:'<歌曲id>'}
```

本项目的 Python 侧（`songboard/ncmbridge.py`、`tools/inject_bridge.py`）
**全部是自研代码**，只通过命名管道和它通信，不含上游代码。

---

## 2. 关于 CEF 头文件 `cef-4472/include/*.h`

这四个头文件（`cef_base.h` / `cef_browser.h` / `cef_registration.h` /
`cef_devtools_message_observer.h`）是**本项目自写的极简 shim**，
不是 CEF 官方头文件，也不是上游项目的文件。

它们只提供让 `AwooNcmCefBridge.cpp` 能编译通过所必需的少量类型声明
（正规做法需要下载约 1 GB 的 CEF 官方发布包）。

其中的类层次与虚函数顺序参照了 [chromiumembedded/cef](https://github.com/chromiumembedded/cef)
的公开接口头文件（BSD 3-Clause），以确保与 `libcef.dll` 的 vtable 布局一致。

---

## 3. `tools/playercap/`

[Metabox-Nexus-PlayerCap](https://github.com/Metabox-Nexus/PlayerCap) 是第三方工具，
**其可执行文件未随本仓库提交**（已在 `.gitignore` 中排除）。
详见 `tools/playercap/README.md`。
