# 架构与原理说明

*其他语言版本: [English](ARCHITECTURE.md), [中文](ARCHITECTURE_zh.md).*

本文档详细说明 iPhone Photo Manager 的技术架构、数据流、各模块设计原理以及关键实现细节。

### 1. 总体架构

```text
┌─────────────────────────────────────────────────────┐
│                    Browser (前端)                     │
│  index.html + index.css + js/ (模块化)                │
│  ─ 纯静态文件，由 FastAPI StaticFiles 托管            │
│  ─ Vanilla ES Modules，无打包构建工具依赖             │
└────────────────────┬────────────────────────────────┘
                     │ HTTP REST API
                     ▼
┌─────────────────────────────────────────────────────┐
│               FastAPI Server (后端)                   │
│  server/app.py                                       │
│  ├── API 路由层  (GET /api/photos, etc.)              │
│  ├── 后台任务引擎 (asyncio.create_task)               │
│  └── 安全中间件  (CSP, X-Frame-Options, etc.)         │
├──────────┬──────────┬──────────┬─────────────────────┤
│ scanner  │ thumbnail│ geocoder │ database            │
│ .py      │ .py      │ .py      │ .py                 │
└──────────┴──────────┴──────────┴─────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────┐
│                  数据存储层                            │
│  data/photos.db     — SQLite（异步 aiosqlite）        │
│  data/thumbnails/   — WebP 缩略图与 JPEG 高清渲染缓存 │
│  photos/            — 原始照片文件（只读）              │
└─────────────────────────────────────────────────────┘
```

**设计原则**：
1. **本地优先** — 所有处理在本机完成，照片数据不离开设备。
2. **零配置启动** — 将照片放入 `photos/` 目录即可，无需预处理。
3. **增量设计** — 只处理新增/删除的媒体、扫描器升级和发生变化的 Google Takeout sidecar，避免重复扫描未变化文件。
4. **离线能力** — 地理编码使用离线引擎，不依赖外部 API。

### 2. 启动与数据流

#### 2.1 启动流程
`app.py` 启动时初始化 SQLite schema。根据数据库记录数和环境变量 `SCAN_ON_STARTUP` 决定是进行增量全盘扫描，还是仅处理遗留的后台任务。

#### 2.2 全量扫描流程
扫描分为三个阶段，前端通过轮询实时展示进度：
1. **文件索引**：按路径对比目录树与数据库，并以存储的纳秒级 mtime 判断变更——新增文件插入、删除文件移除、mtime 变化（被替换的导出文件）则重新扫描。Takeout sidecar 也以各自的路径/mtime 状态跟踪，因此新增、替换或删除 sidecar 只刷新其对应的媒体文件。发生变更的文件会同时失效旧缩略图，并使渲染缓存键轮换（键内嵌 mtime），缓存永远不会返回过期内容。
2. **缩略图生成**：在信号量（`THUMBNAIL_WORKERS`，默认 3）控制下并发生成 WebP 缩略图（处理 EXIF 旋转方向）。
3. **地理编码**：通过离线引擎批量解析 GPS 坐标。

#### 2.3 高清原图渲染 (HEIC 处理)
当用户请求 `/api/photos/{id}/render` 时：
- 普通 JPG/PNG 格式直接串流返回。
- HEIC 格式则通过 `pillow_heif` 读取，并将其安全缩放至 4K 级别分辨率（防止爆显存，同时保持满画质视觉体验），转换并压缩为 JPEG 格式后存入本地磁盘缓存。缓存键内嵌文件 mtime，被替换的文件自动重新渲染。第二次请求时实现 0 延迟秒开。

#### 2.4 HTTP 缓存策略
媒体端点（`/thumbnail`、`/file`、`/render`）基于文件身份生成 ETag，手工处理 `If-None-Match` 返回 304，并附带 `Cache-Control: public, max-age={HTTP_CACHE_MAX_AGE}`。`index.html` 在启动时把本地资源 URL 重写为内容哈希（`?v=<sha1 前 10 位>`）：变更的 CSS/JS 获得新 URL（永久缓存，`immutable`），未变的则继续命中缓存。负责首帧前应用主题的 `boot.js` 以 `no-cache` 提供。Starlette 的 `FileResponse` 不处理 `If-None-Match`，因此需要显式实现。

### 3. 模块详解

#### 3.1 `scanner.py` — 文件扫描与元数据提取
从 HEIC/HEIF/JPG/PNG/WebP/AVIF 提取 EXIF 数据。不依赖 ffmpeg，直接解析 MOV/MP4/3GP 的 ISO 媒体原子结构提取时长与定位。自动在同级目录寻找同名 `.MOV` 以识别 Live Photo，并解析 Android Motion Photo 1.0 XMP、旧版 MicroVideoOffset 和三星 MotionPhoto_Data 标记来定位内嵌视频。

Google Photos Takeout JSON 会按目录建立索引，并通过标准 sidecar 文件名或 JSON 内的 `title` 字段匹配媒体（后者用于兼容 Takeout 截断长 sidecar 文件名）。有效 sidecar 会优先提供拍摄时间和 GPS，并补充描述及收藏状态。数据库保存 sidecar 的相对路径和纳秒级修改时间，因此增量扫描也能识别 sidecar 的新增、替换和删除。

Motion Photo 只在 SQLite 中保存内嵌视频的偏移、长度和 MIME 类型。`/api/photos/{id}/motion-video` 支持 HTTP Range，从原照片中按需流式读取视频，不产生额外媒体副本。

#### 3.2 `thumbnail.py` — 缩略图生成
在缓存目录中镜像原始照片的层级结构，使用 Pillow 生成高质量、高压缩率的 WebP 缩略图。

#### 3.3 `geocoder.py` — 离线地理编码
基于 KD-Tree 的离线反向地理编码，将 GPS 坐标四舍五入到 2 位小数（约 1.1km 精度）后聚类去重，实现极速查询。中国境内坐标回退到 BigDataCloud 获取中文名称。离线引擎对部分地点返回本地语拼写（如德语 "Zuerich"）；`EXONYM_MAP` 在组装时将其归一为英文标准名，数据库、搜索、翻译缓存与界面因此始终使用同一拼写。

#### 3.4 `database.py` — 数据库层
单表 `photos` 覆盖文件信息、拍摄参数、GPS、Takeout 来源、标签和状态。使用 `aiosqlite` 进行异步访问，并在启动时通过追加式迁移兼容旧数据库。数据库以 WAL 模式运行，每连接设置 `busy_timeout`/`synchronous=NORMAL`。迁移分两类：`MIGRATIONS` 幂等地补列与索引；`DATA_MIGRATIONS` 由 `PRAGMA user_version` 门控执行一次性内容重写（如归一化历史遗留的城市名拼写）。列表查询裁剪重列（Takeout sidecar JSON 只在详情视图读取）。

#### 3.5 前端架构 (`frontend/js/`)
基于原生 ES Modules 的模块化单页面应用。
- **模块化设计**：拆分为 `main.js` (入口), `boot.js` (首帧前应用主题，防 FOUC), `api.js` (网络), `state.js` (状态), `constants.js` (共享常量), `favorites.js` (收藏切换 + `favorite-changed` 事件), `gallery.js` (照片墙), `modal.js` (大图), `sidebar.js` (侧边栏), `autoplay.js` (幻灯片自动播放), `i18n.js` (多语言), `utils.js` (工具)。
- **卡片池化 (Card Pooling)**：远离屏幕（3000px 缓冲之外）的照片卡片被回收进池并替换为固定尺寸占位符，数万张照片的库中 DOM 数量始终与视口相当。另一个 `IntersectionObserver` 在更靠近视口边缘处卸载卡片*内容*，暂停并移除 `<img>`/`<video>` 节点（连同悬停定时器），内存占用保持极低。
- **无限滚动**：懒加载分页数据以实现无缝滚动体验。
- **Live Photo 交互**：弹窗中动态创建 `<video>` 标签进行实时预览回放。
- **层级筛选**：地点支持国家→城市层级点击，时间支持月份→具体日期筛选。
