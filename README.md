# 🎵 XiaoAi × YouTube Music (`xiaoai-ytmusic`)

<p align="left">
  <img src="https://img.shields.io/badge/Docker-Alpine%20~31MB-2496ED?logo=docker&logoColor=white" alt="Docker Size" />
  <img src="https://img.shields.io/badge/Arch-amd64%20%7C%20arm64-success" alt="Architecture" />
  <img src="https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/License-MIT-green" alt="License" />
</p>

**`xiaoai-ytmusic`** 是一个专为 NAS 与家庭服务器打造的超轻量级 **小爱音箱 × YouTube Music 一体化音源替换与投屏中枢**。

本项目深度融合了 [`songloft-org/songloft`](https://github.com/songloft-org/songloft)、[`Xeltra233/youtube-music-api`](https://github.com/Xeltra233/youtube-music-api) 与 [`deerwan/miair-next`](https://github.com/deerwan/miair-next) 三个开源项目的核心协议栈，剔除了无头浏览器、FFmpeg 转码、重型前端框架等全部冗余依赖，将原本近 **1.9 GB 的三容器架构浓缩为仅 ~31 MB 的单容器镜像**。

---

## ✨ 核心特性

- **🗣️ 原生语音点歌劫持**：对音箱说 *“小爱同学，播放周杰伦的晴天”* 或 *“播放歌单我喜欢的音乐”*，自动打断小爱原厂的 VIP 付费提示，实时检索 YouTube Music 并推送到音箱播放。
- **📚 登录后自动同步歌单**：导入 YouTube Music 登录凭证后，自动拉取并保存账号下的 **「我喜欢的音乐 (`LM`)」** 及全部自建/收藏播放列表，支持语音或网页一键整单播放。
- **⚡ 原生 M4A 零转码直推 + 下一首预缓存**：直接提取 YouTube 原生 `itag=140` (`.m4a` AAC) 音频流供小爱音箱硬解，免除 `ffmpeg` CPU 转码开销；播放当前曲目时后台静默预缓存下一首，切歌零延迟。
- **🔐 多格式 YouTube 登录与快照防擦除**：支持一键粘贴浏览器 `F12 -> Copy as cURL`、拖拽 `cookies.txt` 或 `Cookie-Editor` JSON 登录；内置 `SAPISIDHASH` 认证签名与 `yt-dlp` 临时快照隔离机制，彻底杜绝登录态被覆写失效。
- **📱 米家 App 扫码登录 + 三级凭证自愈**：支持米家 App 扫码免密绑定音箱，内置 `serviceToken 缓存复用 ➔ passToken 换发 ➔ 密码重登` 三级自愈链。
- **📡 内置 DLNA 局域网投屏接收器**：复用主 HTTP 端口提供标准 UPnP / DLNA `MediaRenderer` 服务，支持手机/电脑音乐 App 局域网无线投送。
- **🖥️ 零构建内嵌 Web 控制台**：单文件响应式管理后台，集点歌搜索、歌单管理、实时队列控制、语音拦截日志、连通性一键诊断于一体。

---

## 🏗️ 工作原理架构

```text
[用户语音: "小爱同学，播放周杰伦的晴天 / 播放我喜欢的音乐"]
                          │
                          ▼
               [小米云端对话记录生成]
                          │
                          ▼ (服务端时间戳去重基线轮询)
        ┌───────────────────────────────────┐
        │     NAS: xiaoai-ytmusic 容器      │
        │                                   │
        │  1. VoiceEngine 精确/跳字模糊匹配 │
        │  2. 立即下发 pause+stop 切断原声  │
        │  3. 检索本地已同步歌单或          │
        │     三级容灾检索 YouTube Music    │
        │  4. 提取原生 M4A 音频流并缓存     │
        │     (后台自动预取队列下一首)      │
        └─────────────────┬─────────────────┘
                          │
                          ▼ 5. MIoT / MiNA 推送内网直链
             http://<NAS_IP>:8080/stream/<id>.m4a
                          │
                          ▼
                     [小爱音箱]
        (音箱在局域网内通过 HTTP Range 拉流硬解，音箱本身无需翻墙)
```

---

## 📊 架构瘦身对比（~1.9 GB ➔ ~31 MB）

| 来源项目 | 保留并强化的核心模块 | 剔除的冗余模块 |
| :--- | :--- | :--- |
| **`Xeltra233/youtube-music-api`** | • InnerTube `WEB_REMIX` 检索与 `SAPISIDHASH` 签名<br>• NFKC 归一化 + CJK 分词 + Levenshtein 匹配算法<br>• `QuickJS` + `yt-dlp-ejs` 签名解密与 Cookie 快照保护<br>• Singleflight 并发合并 + LRU 磁盘缓存 + Range 流服务 | • 剔除 **Chromium 无头浏览器与中文字体**（省 ~700 MB）<br>• 剔除 **Deno 运行时**（改用 1MB `QuickJS`，省 ~120 MB）<br>• 剔除 **FFmpeg 强制 MP3/MP4 转码**（省 ~150 MB，改走小爱原生支持的 `.m4a` 直推） |
| **`songloft-org/songloft`**<br>+ `songloft-plugin-miot` | • 服务端时间戳去重基线 (`primed`) 对话监听器<br>• 跨优先级最长关键词 + 有界跳字模糊子序列口令引擎<br>• 自动排队切歌 + 非音乐问答打断后智能续播 (`Smart Resume`) | • 剔除本地全盘音视频扫描器、WebDAV、Subsonic 服务<br>• 剔除 **Flutter Web 前端**（省 ~50 MB）与 QuickJS 插件沙箱<br>• 剔除 AI 向量记忆库、OpenCC 繁简字典、节假日日历表 |
| **`deerwan/miair-next`** | • 米家 App 扫码登录 (`QRLogin`) 与三级凭证自愈链<br>• 触屏音箱歌词封面 `audioID` 匹配 (`player_play_music`)<br>• 共享端口的轻量级 UPnP / DLNA 投屏渲染器 | • 剔除 **Node.js + Vue 3 + Naive UI** 构建层<br>• 剔除 **PyAV (`av`) + `libportaudio2` + `ffmpeg`** 重采样依赖 |

---

## 🚀 快速部署指南

### 方式一：使用 `docker-compose.yml` 部署（推荐）

1. 下载本项目或创建 `docker-compose.yml`：

```yaml
services:
  xiaoai-ytmusic:
    image: xiaoai-ytmusic:latest
    build: .
    container_name: xiaoai-ytmusic
    restart: unless-stopped
    network_mode: host
    environment:
      - TZ=Asia/Shanghai
      - PORT=8080
      # 填入 NAS 的真实局域网 IP（如 192.168.1.100），留空则自动探测
      - LAN_IP=192.168.1.100
      # 访问 YouTube Music 的代理（若 NAS 已走旁路由/透明网关请留空）
      - YTMUSIC_PROXY=
      # 可选：管理后台访问密码
      - ADMIN_PASSWORD=
      # 音频缓存上限 (MB) 与过期时间 (小时)
      - MAX_CACHE_MB=1024
      - CACHE_TTL_HOURS=72
    volumes:
      - ./data:/app/data
```

2. 启动容器：

```bash
docker compose up -d --build
```

### 方式二：跨平台构建导出离线镜像包（供 ARM64 / AMD64 NAS 导入）

```bash
# 构建 ARM64 镜像并导出离线包
docker build --platform linux/arm64 -t xiaoai-ytmusic:latest .
docker save -o xiaoai-ytmusic-arm64.tar xiaoai-ytmusic:latest

# 在 NAS 上导入并启动
docker load -i xiaoai-ytmusic-arm64.tar
docker compose up -d
```

---

## 🛠️ 配置与使用说明

容器启动后，浏览器访问 **`http://<NAS_IP>:8080`** 进入管理控制台：

### 1. 绑定小米账号与小爱音箱
1. 切换到 **「🔊 小米账号与音箱」** 标签页。
2. 点击 **「获取登录二维码」**，使用手机 **米家 App** 扫码确认。
3. 在右侧设备列表中勾选需要开启语音拦截的小爱音箱，并点击 **「设为默认」**。

### 2. 登录 YouTube Music 并自动同步歌单
1. 切换到 **「🎬 YouTube Music 登录」** 标签页。
2. **推荐免插件登录法（15 秒完成）**：
   - 点击页面上的 **「🔗 1. 打开 YouTube Music 网页版登录」** 在浏览器登录你的 Google 账号。
   - 按 `F12` 打开开发者工具 ➔ 切换到 **Network (网络)** ➔ 右键任意一行请求 ➔ 选择 **`Copy -> Copy as cURL (bash)`**（或 `Copy as cURL (cmd)`）。
   - 粘贴到管理后台输入框中，点击 **「🔐 一键解析并登录 YouTube Music」**。
   - *备选方式*：也支持直接拖拽上传 `Get cookies.txt LOCALLY` 导出的 `.txt` 文件或 `Cookie-Editor` 导出的 `.json` 文件。
3. 登录成功后，系统会在后台**自动拉取并保存你的「我喜欢的音乐」及所有自建/收藏歌单**，可在首页直接查看与整单点播。
4. 点击 **「⚡ 一键诊断搜索与播放下载」** 可实时检验搜索连通性与音频流提取状态。

### 3. 支持的语音口令清单

直接唤醒小爱音箱说出以下口令即可触发拦截接管（支持跳字模糊匹配，如 *“我今天想听晴天”*）：

| 动作类型 | 触发关键词示例 | 示例语音口令 |
| :--- | :--- | :--- |
| **播放指定歌曲** | `播放歌曲`、`播放音乐`、`放一首`、`来一首`、`我想听`、`点播`、`播放` | *“小爱同学，播放周杰伦的晴天”*<br>*“小爱同学，我想听陈奕迅的十年”* |
| **播放已存歌单** | `播放歌单`、`放歌单`、`播放列表`、`播放我喜欢的音乐` | *“小爱同学，播放我喜欢的音乐”*<br>*“小爱同学，播放歌单华语精选”* |
| **播放指定歌手** | `播放歌手`、`放歌手`、`听歌手` | *“小爱同学，播放歌手林俊杰”* |
| **切歌控制** | `下一首`、`切歌`、`换一首`、`上一首` | *“小爱同学，下一首”* |
| **暂停 / 停止 / 恢复** | `暂停播放`、`停止播放`、`别播了`、`继续播放`、`接着播` | *“小爱同学，停止播放”* / *“继续播放”* |
| **切换播放模式** | `单曲循环`、`随机播放`、`列表循环`、`顺序播放` | *“小爱同学，单曲循环”* |

---

## ⚙️ 环境变量说明

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `PORT` | `8080` | Web 管理后台、音频流服务与 DLNA 描述共用端口 |
| `LAN_IP` | 自动探测 | NAS 局域网 IPv4 地址（小爱音箱通过该地址拉取音频流，切勿填 `127.0.0.1`） |
| `YTMUSIC_PROXY` | 空 | 访问 YouTube 的 HTTP/HTTPS 代理（如 `http://192.168.1.2:7890`；旁路由透明网关环境请留空） |
| `DATA_DIR` | `/app/data` | 配置文件、歌单库、YouTube Cookie 与音频缓存持久化目录 |
| `ADMIN_PASSWORD` | 空 | Web 管理后台密码（留空则局域网免密访问） |
| `API_KEY` | 空 | 外部 Bot 调用 `/search` 与 `/download` 接口的 `X-API-Key`（留空不校验） |
| `MAX_CACHE_MB` | `1024` | 磁盘音频缓存最大容量（MB），超出后按 LRU 自动淘汰 |
| `CACHE_TTL_HOURS` | `72` | 音频缓存文件保留时长（小时） |

---

## 📂 项目结构

```text
xiaomi_ytb/
├── Dockerfile                # 基于 python:3.12-alpine + quickjs 的极简镜像定义
├── docker-compose.yml        # Docker Compose 编排文件
├── requirements.txt          # Python 依赖清单 (aiohttp, yt-dlp[default])
├── .env.example              # 环境变量示例文件
├── run.py                    # 服务启动入口
├── app/
│   ├── config.py             # 配置持久化与局域网 IP 自动探测
│   ├── ytmusic/
│   │   ├── client.py         # InnerTube 搜索、歌单拉取与直链解析客户端
│   │   ├── cookies.py        # 多格式 Cookie 解析、SAPISIDHASH 签名与快照保护
│   │   ├── downloader.py     # Singleflight 去重、QuickJS 解密下载与 LRU 磁盘缓存
│   │   ├── matching.py       # NFKC 归一化、CJK 单字分词与 Levenshtein 匹配算法
│   │   └── playlists.py      # 播放列表自动同步、导入与本地持久化管理
│   ├── xiaomi/
│   │   ├── const.py          # 小爱音箱型号白名单与 API 常量
│   │   ├── auth.py           # 米家扫码登录、三步密码认证与三级凭证自愈链
│   │   └── client.py         # 小爱设备控制、触屏歌词匹配与对话记录拉取
│   ├── engine/
│   │   ├── player.py         # 音箱播放队列状态机、自动切歌与后台预缓存
│   │   ├── voice.py          # 服务端时间戳去重对话监听与语音口令拦截引擎
│   │   └── dlna.py           # UPnP / DLNA MediaRenderer 投屏接收器
│   └── web/
│       ├── server.py         # 统一 HTTP 服务 (Web API + Range 音频流 + DLNA 路由)
│       └── static/index.html # 零构建单文件可视化管理控制台
└── tests/
    └── test_all.py           # 单元测试与端到端集成测试套件
```

---

## 🙏 致谢

本项目站在以下优秀开源项目的肩膀上构建，特此致谢：

- [songloft-org/songloft](https://github.com/songloft-org/songloft) & [songloft-plugin-miot](https://github.com/songloft-org/songloft-plugin-miot)（小爱音箱 MIoT 对话监听与语音口令匹配机制）
- [Xeltra233/youtube-music-api](https://github.com/Xeltra233/youtube-music-api)（YouTube Music InnerTube 搜索解析、相似度打分与 Cookie 快照保护思想）
- [deerwan/miair-next](https://github.com/deerwan/miair-next)（米家扫码登录、三级凭证自愈链与 DLNA 渲染器设计）
- [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp)

---

<p align="center">
  Vibe coding with ❤️ · Powered by <strong>Gemini 4 Argon</strong>
</p>

