# iPhone Photo Manager

*其他语言版本: [English](README.md), [中文](README_zh.md).*

这是一个轻量级、响应式的网页应用，用于管理、组织和探索从 iPhone 与 Android 导出的照片及视频。它能通过时间线进行展示，并利用内嵌的 EXIF 数据自动通过地理位置对媒体进行归类和分组。

## 演示 (Demo)
<p align="center">
  <img src="assets/demo1.png" width="80%" style="margin-bottom: 20px" />
  <br />
  <img src="assets/demo2.png" width="80%" />
</p>
*演示展示了时间线滚动、地点分类筛选以及中英文切换功能。*

### ✨ 核心功能

一款轻量级、本地优先的手机照片网页画廊。支持 HEIC/HEIF、JPEG、PNG、WebP、AVIF、MOV、MP4、3GP、iPhone 实况照片和 Android Motion Photo。Motion Photo 内嵌视频直接从原文件流式读取，不会生成重复视频副本。

- **⭐ 交互式照片收藏**：相册卡片右上角与详情灯箱中一键添加/取消收藏，主页与灯箱双向秒级同步；顶部专属“⭐ 收藏”分类视图，取消收藏时带有平滑优雅的淡出动画。
- **智能语义分类与精细过滤**：“全部”、“照片”、“视频”、“截图”（智能匹配 iOS/Android 屏幕尺寸及 EXIF/XMP 标记）、“⭐ 收藏”5 组语义分类。顶部搜索框支持中英文全字段模糊搜索——文件名、描述、地点均可命中（如直接输入“苏黎世”或“Zurich”、“北京”等）；每张照片附带下载按钮，原始文件名一键保存。
- **流畅全屏照片详情灯箱**：磨砂玻璃悬浮切图控制、键盘左右键、鼠标滚轮、**手机端触控滑动手势**全支持；纯键盘即可完成完整操作（Tab/Enter/空格/Esc），手机端同样功能齐全——收藏、下载、查看原图收纳在悬浮操作栏，点击 ⓘ 从底部抽屉查看 EXIF 详情。切换到当前已加载末尾时**自动异步拉取下一页**，支持跨页无缝连续浏览与循环。
- **沉浸式幻灯片播放：** 高度可配置的自动播放功能，解放双手。可按日期、国家、媒体类型筛选，调整播放速度，并支持等待实况照片和视频播放完毕再自动切换下一张。
- **Google Photos Takeout 元数据：** 从媒体旁的 Takeout JSON 恢复拍摄时间、GPS、海拔、描述和收藏状态，兼容 supplemental-metadata 及长文件名截断的 sidecar。
- **玻璃拟态（Glassmorphism）UI：** 现代化、高质感的界面设计，拥有丝滑的微动画和精美的深/浅色双主题。

使用方法非常简单：只需将 iPhone 或 Android 相册导出并复制到你指定的 `PHOTOS_DIR` 目录下即可。例如放在当前项目目录的 `./photos` 文件夹下（此路径可在 `.env` 文件中配置）。工具在启动时会自动扫描该目录。

**预期的目录结构：**

```text
iphone-photo-manager/
├── photos/                  # 你的 PHOTOS_DIR 照片目录（在 .env 中配置）
│   ├── 202601/              # （可选）按年月划分子文件夹
│   │   ├── IMG_0001.HEIC
│   │   ├── IMG_0001.MOV     # 对应的实况照片视频
│   │   └── IMG_0002.JPG
│   └── ...
├── server/                  # Python 后端
│   ├── app.py               # FastAPI 主入口，API 路由，后台任务
│   ├── database.py          # SQLite 数据库 schema 与查询
│   ├── scanner.py           # 文件扫描与 EXIF/MOV 元数据提取
│   ├── thumbnail.py         # WebP 缩略图生成（small / medium）
│   └── geocoder.py          # 离线反向地理编码（reverse_geocoder + pycountry）
├── frontend/                # 纯 HTML/CSS/JS 前端（无框架）
│   ├── index.html           # 页面结构
│   ├── index.css            # 样式（深色/浅色主题、移动端媒体查询）
│   └── js/                  # 模块化的 JS 逻辑（相册、灯箱、时间线、虚拟滚动等）
├── data/                    # 运行时数据（自动生成，已 gitignore）
│   ├── photos.db            # SQLite 数据库
│   ├── thumbnails/          # 缩略图及高清渲染缓存
│   └── translation_cache.json # 地点多语言翻译缓存
├── scripts/                 # 服务启停运维脚本
│   ├── start.sh / start.ps1 # 启动服务脚本（Linux/macOS 及 Windows）
│   └── stop.sh / stop.ps1   # 停止服务脚本（Linux/macOS 及 Windows）
├── tests/                   # 自动化单元与集成测试套件
├── .env                     # 环境变量配置（不提交）
├── .env.template            # 环境变量模板
├── requirements.txt         # Python 依赖
└── ARCHITECTURE_zh.md       # 项目架构与原理说明
```

### 🚀 快速开始

#### 1. 环境准备
需要 Python 3.10+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

```bash
# 克隆项目
git clone <repo-url>
cd iphone-photo-manager

# 创建虚拟环境并安装依赖
uv venv --python 3.10
uv pip install -r requirements.txt
```

#### 2. 配置

```bash
# macOS/Linux
cp .env.template .env

# Windows (CMD/PowerShell)
copy .env.template .env
```

主要配置项：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `APP_LANGUAGE` | `zh` | 界面语言：`zh`（中文）或 `en`（英文） |
| `APP_THEME` | `light` | 主题：`light` 或 `dark` |
| `PHOTOS_DIR` | `photos` | 照片目录路径（相对于项目根目录，或绝对路径） |
| `SERVER_HOST` | `127.0.0.1` | 服务绑定地址（设为 `0.0.0.0` 可允许局域网访问） |
| `SERVER_PORT` | `8000` | 服务端口 |
| `SCAN_ON_STARTUP` | `True` | 启动时是否自动执行增量扫描 |
| `LOAD_ORIGINAL_ON_CLICK`| `False` | 打开单张图片时是否直接加载高清原图 |
| `DB_PATH` | `data/photos.db` | SQLite 数据库文件路径 |
| `THUMBNAIL_DIR` | `data/thumbnails` | 缩略图缓存目录 |
| `THUMBNAIL_WORKERS` | `3` | 缩略图/HEIC 渲染并发数（每个并发需解码整张图，内存紧张时可调低） |
| `HTTP_CACHE_MAX_AGE` | `86400` | 照片、缩略图与渲染结果的浏览器缓存时长（秒） |
| `TRANSLATION_FLUSH_DEBOUNCE` | `5` | 翻译缓存落盘防抖间隔（秒） |
| `TRANSLATE_MAX_ITEMS` | `300` | 地点翻译条数超过该值时 `/api/locations` 直接返回原文 |

#### 3. 导入照片
将 iPhone 照片按年月分子文件夹放入 `photos/` 目录。（支持用 AirDrop 或 USB 直接导出，系统会自动配对 Live Photo）。

导入 Google Photos Takeout 时，请把所有压缩分卷解压到同一目录树，并将 `PHOTOS_DIR` 指向解压后的 `Takeout/Google Photos`（也可以把该目录复制到 `photos/` 下）。请保留媒体文件与对应 `.json` 或 `.supplemental-metadata.json` 文件的相邻结构。JSON 不会出现在相册中，其中的拍摄时间、GPS、海拔、描述和收藏状态会自动应用；新增、替换或删除 sidecar 后，下次增量扫描会刷新对应媒体。

#### 4. 启动服务
请确保你当前位于项目根目录（例如 `iPhone-Photo-Manager`）下，然后执行：
```bash
# 方式一：直接运行
uv run --no-project python -m server.app

# 方式二：使用便捷脚本
# Linux / macOS:
./scripts/start.sh
# Windows PowerShell:
.\scripts\start.ps1
```
启动完成后在浏览器打开：**http://127.0.0.1:8000**

#### 5. 停止服务
在运行服务的终端中按下 `Ctrl+C` 即可停止服务。

如果是后台运行：
```bash
# Linux / macOS:
./scripts/stop.sh

# Windows PowerShell:
.\scripts\stop.ps1
```

### 📖 使用指南
- **时间筛选**：左侧时间线点击月份快速跳转，点击箭头展开可精确到天。
- **地点筛选**：点击左侧国家名即可筛选该国所有照片，展开后可精确到城市。
- **搜索**：顶部搜索框可按文件名、描述、地点全字段模糊搜索，中英文均可。输入搜索会自动清除地点筛选（反之亦然），保证当前过滤条件始终清晰。
- **查看大图**：点击缩略图进入大图模式。支持屏幕按钮、键盘左右键、鼠标滚轮、**移动端触控滑动**切换。到达末尾自动追加加载下一批。如果 `LOAD_ORIGINAL_ON_CLICK` 为 false，可以在大图预览界面底部点击“查看原图”，系统会自动渲染并缓存一张满画质（4K）的高清大图。
- **照片收藏**：点击缩略图右上角或大图界面的五角星即可收藏，在顶部点击“⭐ 收藏”即可查看个人专属精选集。
- **幻灯片播放**：点击顶部导航栏的“幻灯片”按钮即可调出播放设置；在大图浏览界面中，也可以直接开启内联的自动播放开关，立刻开始重温记忆。

### ⚙️ 幕后工作原理

- **基于变更检测的增量扫描**：每个文件的纳秒级 mtime 都存储在数据库中。每次扫描只重新读取新增、删除或 mtime 发生变化的文件（被替换的文件会重新提取元数据、重建缩略图并使渲染缓存失效）。Google Takeout sidecar 采用同样的跟踪方式，新增或修改 sidecar 只会影响其对应的媒体文件。
- **WAL 模式**：SQLite 以预写日志模式运行，扫描分批提交时浏览依然流畅。这会产生 `photos.db-wal` / `photos.db-shm` 附属文件，属正常现象。备份数据库时请使用 `sqlite3 photos.db ".backup backup.db"`，避免在写入中途直接复制文件。
- **一次性数据迁移**：内容级重写（如归一化地理编码返回的城市名拼写）由 SQLite 的 `PRAGMA user_version` 门控，仅在升级后的首次启动时执行一次。
- **浏览器缓存**：照片、缩略图与渲染结果均带 ETag 支持条件请求（304），HTML 中的 CSS/JS 引用被替换为内容哈希——未变化的资源命中缓存，变化的资源获得全新 URL。升级部署后的首次页面加载会重新拉取一次静态资源，属预期行为。
