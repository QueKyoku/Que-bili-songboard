# tools/playercap —— 第三方外部媒体信息源（可选）

这个目录本来是放 [Metabox-Nexus-PlayerCap](https://github.com/Metabox-Nexus/PlayerCap)
的。**它的 exe 没有随仓库提交**（13.7 MB 的第三方二进制，不该塞进 git），
需要的话自己下：

```powershell
# 1. 从官方 release 下载
#    SHA256 校验过的是 2737a2addbed2b5a23c023517bc9df9f3f01835b3b3e1082eaf7e2fd34417443
# 2. 解压到本目录，得到 Metabox-Nexus-PlayerCap.exe
# 3. 配置 config.yml 指向本机地址
```

## 它是干嘛的

网易云客户端**不提供**"当前播放进度"，所以自动切歌没法精确判断"播完了没"。
PlayerCap 会在本机起一个 HTTP 服务（默认 `127.0.0.1:8766`），把当前播放器的
曲名 / 时长 / 位置暴露出来。本程序通过 `extapi` 配置去读它：

```jsonc
"extapi": {
  "enabled": true,
  "urls": [
    "http://127.0.0.1:8766/cloudmusicv3/song_info",   // 有 name/singer，没时长
    "http://127.0.0.1:8766/cloudmusicv3/all_lyrics"   // 有 duration/position，没歌名
  ]
}
```

两个接口各缺一半字段，所以要合并着读。

## ⚠️ 用之前必须知道的三件事

1. **需要管理员权限**才能读到播放器信息
2. **它会向外发遥测**，内容包含你的安装路径（带用户名）、公网 IP、完整配置，
   而且**没有关闭选项**
3. 它是闭源分发的二进制。介意的话就别用——不开 `extapi` 时本程序完全能跑，
   只是自动切歌的"播完判定"会退化成按时长估算（见主 README 第四节）
