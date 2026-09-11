# 飞书群消息归档服务

将飞书群聊消息自动同步到多维表格的 Web 服务，并可选将消息归档为本地 Markdown。

## 功能

- 飞书 OAuth 登录（用户身份授权）
- 添加/删除群聊 ID 配置，支持自动识别群名
- **真实群名双标签**：群列表第二行并排显示「飞书真实群名 + 群聊 ID」标签；用户自定义名与真实群名解耦存储，每次同步自动刷新真实群名（飞书改群名后自动跟进），支持按任一名称搜索
- 手动一键同步群消息到飞书多维表格
- 自动创建多维表格，字段格式统一：发言人、日期、消息内容、附件
- 消息中的图片和文件自动上传为附件
- **本地缓存选项**：开启后，同步的群聊文字自动规范存为标准 Markdown（`.md`），图片与附件下载到本地 `assets/` 目录并以相对路径链接
- **云文档快照缓存**：消息中分享的飞书云文档链接（docx / wiki）自动拉取文档内容，转换为 Markdown 快照保存到 `assets/docs/`，并在消息中附加本地缓存链接（详见下文「云文档快照缓存说明」）
- **Markdown 在线预览**：点击主页群卡片直达预览页，支持图片全屏灯箱放大与附件一键直接下载
- **一键打包下载**：支持一键打包下载包含 Markdown 和全部附件的 ZIP 压缩包
- chat_id 与多维表格/本地缓存映射关系持久化存储

## 前置条件：接入 pm-assist 认证跳板

> OAuth 认证由 pm-assist（`https://pm.tmhcorps.cn/hub`）代理完成：本应用不再创建飞书自建应用、不持有 App Secret。用户在授权页看到的是 pm-assist 应用，token 也由其换发；拿到 token 后业务 API 依旧直连飞书。

### 1. 在 pm-assist 注册本应用

编辑服务器上 pm-assist 的 `.env`，在 `HUB_CLIENTS` 中加入一项（scope 按需增减）：

```json
{"id":"chatlogger","secret":"<随机密钥>","redirect_uri":"https://chatlogger.tmhcorps.cn/auth/callback","scopes":"im:message:readonly im:message.group_msg:get_as_user bitable:app im:chat:readonly offline_access docx:document:readonly wiki:wiki:readonly docs:document.media:download"}
```

然后 `systemctl --user restart pm-hub`。

### 2. 配置本应用的 .env

```
HUB_URL=https://pm.tmhcorps.cn
HUB_CLIENT_ID=chatlogger
HUB_CLIENT_SECRET=<与注册一致的随机密钥>
```

> 注：`id` / `redirect_uri` / `scopes` 以 pm-assist 注册信息为准；新增 docx / wiki / 媒体下载等 scope 后，**已授权用户需退出并重新登录授权**，user_access_token 才具备新权限，否则云文档快照会因无权限跳过。
>
> 注：pm-assist 应用需已开通下表权限并开启「刷新 user_access_token」（一次性配置）：
>
> | 权限 | 用途 |
> |------|------|
> | `im:message:readonly` | 读取群聊消息 |
> | `im:message.group_msg:get_as_user` | 以用户身份获取群组消息 |
> | `bitable:app` | 创建/编辑多维表格 |
> | `docx:document:readonly` | 读取云文档内容（云文档快照缓存） |
> | `wiki:wiki:readonly` | 解析知识库链接（云文档快照缓存） |
> | `docs:document.media:download` | 下载云文档内图片（云文档快照缓存） |
> | `offline_access` | 获取 refresh_token |

## 部署与运行

### 方式一：Docker Compose（推荐，适用于 NAS / 生产环境）

在项目根目录下准备好 `.env` 配置文件与持久化目录：

```bash
# 1. 复制环境变量模板
cp .env.example .env
# 编辑 .env 填写 HUB_CLIENT_ID, HUB_CLIENT_SECRET 等核心配置

# 2. 创建持久化数据目录
mkdir -p data cache

# 3. 启动容器
docker compose up -d

# 4. 查看日志与状态
docker compose logs -f
```

`docker-compose.yml` 默认挂载：
- `./data:/app/data`：持久化存储 SQLite 数据库（`chatlogger.db`）
- `./cache:/app/cache`：持久化存储本地 Markdown 归档文件、`assets/` 附件与 `assets/docs/` 云文档快照
- `restart: unless-stopped`：NAS 或服务器重启后自动恢复服务

### 方式二：直接使用 Python 运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入实际配置

# 3. 启动（开发）
export $(cat .env | xargs) && python app.py

# 或启动 Gunicorn（生产）
export $(cat .env | xargs) && gunicorn -c gunicorn_config.py app:app
```

### Nginx 配置示例

```nginx
server {
    listen 80;
    server_name chatlogger.tmhcorps.cn;

    # HTTPS 重定向
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name chatlogger.tmhcorps.cn;

    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## 环境变量配置说明

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `HUB_URL` | `https://pm.tmhcorps.cn` | 认证跳板地址（pm-assist） |
| `HUB_CLIENT_ID` | 无 (必填) | 跳板注册的 client_id（见 pm-assist `.env` 的 HUB_CLIENTS） |
| `HUB_CLIENT_SECRET` | 无 (必填) | 跳板注册的 client_secret，需与 pm-assist 一致 |
| `SECRET_KEY` | `change-this-to-a-random-secret-key` | Flask Session 加密密钥（建议生成长随机字符） |
| `DB_PATH` | `./chatlogger.db`（容器内推荐 `/app/data/chatlogger.db`） | SQLite 数据库文件存储路径 |
| `LOCAL_CACHE_DIR` | `./cache`（容器内推荐 `/app/cache`） | 本地 Markdown 归档与附件缓存存储目录 |
| `DEFAULT_LOCAL_CACHE` | `true` | 添加新群聊时是否默认开启「本地缓存」选项 |
| `MAX_ATTACHMENT_SIZE_MB` | `20` | 单个附件/图片大小上限（MB），超出自动在备注中记录跳过 |
| `MAX_DOC_BLOCKS` | `2000` | 单篇云文档快照最多转换的内容块数，超出自动截断并标注 |
| `CACHE_DOC_IMAGES` | `true` | 是否下载云文档内的图片到本地 `assets/docs/` |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `5000` | 监听端口 |

### 附件大小上限（`MAX_ATTACHMENT_SIZE_MB`）机制说明

- **为什么默认是 20MB？**
  飞书多维表格单文件上传接口（`upload_all`）的官方硬性上限是 20MB。系统在下载时以此阈值进行防御性拦截，防止大文件导致上传接口报错。
- **如何修改与动态备注**：
  在 `.env` 中设置 `MAX_ATTACHMENT_SIZE_MB=50` 即可将上限调整为 50MB。当消息中存在超出限制的图片或附件时，系统会自动在多维表格「备注」列以及 Markdown 对应位置生成动态跳过提示：
  `[跳过文件：视频.mp4（附件 65MB 超过最大允许 50MB 限制）]`，提示中的数值会随环境变量实时动态更新。

## 云文档快照缓存说明

- 同步时自动识别消息中的飞书云文档链接（`/docx/`、`/wiki/`），通过 docx Blocks API 拉取文档内容并转换为 Markdown（标题、列表、代码块、表格、图片、待办、引用等）
- 快照保存至 `cache/{群名}_{chat_id}/assets/docs/{文档标题}_{doc_token}.md`，头部含原文链接与缓存时间；文档内图片下载到同目录
- 消息 Markdown 中的云文档链接后自动附加 `📄缓存` 本地链接，原链接保留不变
- 快照语义：保存同步时刻的内容存档，文档后续修改不自动更新；同一文档（doc_token）只缓存一份，多条消息引用时复用
- 单篇文档超过 `MAX_DOC_BLOCKS` 个内容块自动截断并在末尾标注
- 无权限访问、非 docx 类型（如 sheet / bitable）或旧版 `/docs/` 链接：保留原链接，消息尾部附 `[云文档快照失败: ...]` 说明，不影响整体同步
- 单篇文档缓存失败不阻断同步；ZIP 打包下载自动包含全部文档快照

## 从阿里云迁移到 NAS 操作指南

如果当前服务部署在阿里云，希望无缝迁移至群晖、威联通、Unraid 等 NAS 的 Docker 环境中：

### 步骤 1：从阿里云备份数据

登录阿里云服务器，将当前运行的数据与配置文件打包：

```bash
cd /home/duyingfang/chatlogger

# 打包数据库、本地缓存和环境配置
tar -czvf chatlogger_backup.tar.gz chatlogger.db cache/ .env
```

### 步骤 2：传输到 NAS

通过 `scp` 或 NAS 的 File Station 将 `chatlogger_backup.tar.gz` 传输到 NAS 的 Docker 项目目录（例如 `/volume1/docker/chatlogger`）：

```bash
# 在 NAS 终端解压
mkdir -p /volume1/docker/chatlogger/data
cd /volume1/docker/chatlogger
tar -xzvf chatlogger_backup.tar.gz

# 将数据库移入 data 目录
mv chatlogger.db data/
```

### 步骤 3：拉取项目代码或配置文件

将本仓库的 `docker-compose.yml` 放入 `/volume1/docker/chatlogger/`。

目录结构检查：
```text
/volume1/docker/chatlogger/
├── docker-compose.yml
├── .env
├── data/
│   └── chatlogger.db
└── cache/
    └── {群名}_{chat_id}/
        ├── {群名}.md
        └── assets/
            ├── ...（图片与附件）
            └── docs/（云文档快照）
```

### 步骤 4：调整 `.env` 与认证跳板回调地址

1. **若保留原域名**（推荐）：
   通过 NAS 的反向代理、Cloudflare Tunnel 或 DDNS 将 `chatlogger.tmhcorps.cn` 指向 NAS 的 `5000` 端口，则 `.env` 与 pm-assist 注册信息完全无需修改！
2. **若更换新域名/IP**：
   - 修改 NAS 上的 `.env`（如有相关配置）；
   - 登录服务器修改 pm-assist `.env` 中 `HUB_CLIENTS` 的 `redirect_uri` 为 `https://<新域名或IP:端口>/auth/callback`，然后 `systemctl --user restart pm-hub`。飞书开放平台无需任何改动。

### 步骤 5：启动 NAS 容器

在 NAS 对应目录下执行：
```bash
docker compose up -d
```
或在群晖「Container Manager / Docker」套件中通过「项目（Compose）」直接导入并启动。访问服务并登录验证历史群聊与本地缓存。

## 使用与管理说明

1. **登录授权**：访问部署地址，点击「飞书登录」授权应用；
2. **群聊管理**：
   - 输入群聊 ID 添加配置，可自定义群名或勾选「本地缓存」；
   - **群聊删除联动清理**：点击「删除」按钮时，弹窗提供「同时删除本地缓存文件（Markdown 与附件）」选项。若该群已存在本地缓存，复选框默认勾选并联动清理物理磁盘空间；若从未缓存过，复选框自动置灰禁用。
3. **数据同步**：
   - 点击「同步」按钮执行增量归档；
   - 同步完成后可直达飞书多维表格，或点击群卡片进入 Markdown 在线预览页面（支持图片灯箱预览与附件直接下载）；
   - 开启本地缓存时，消息中的飞书云文档链接会自动生成 Markdown 快照（需 docx / wiki / 媒体下载权限）。

## 数据存储

- 用户信息和 OAuth token 存储在 SQLite 数据库（`chatlogger.db`）
- 群聊 ID 与多维表格的映射关系也存储在同一数据库中
- 多维表格本身存储在飞书云空间中，创建者为授权用户
- **本地缓存文件**：存储在 `cache/{群名}_{chat_id}/` 目录下，包含 `{群名}.md`、`assets/` 附件子目录与 `assets/docs/` 云文档快照子目录（可通过环境变量 `LOCAL_CACHE_DIR` 自定义路径）

## 注意事项

- `user_access_token` 有效期约 2 小时，服务会自动使用 `refresh_token` 刷新
- `refresh_token` 有效期约 7 天，用户授权满 365 天后需重新登录授权
- 首次同步某个群时会自动创建多维表格，后续同步为增量追加
- 附件（图片/文件）会自动下载并上传到对应记录
- 大文件（>100MB）可能下载超时
- 云文档快照依赖 docx / wiki / 媒体下载权限；在飞书后台新增权限后，已登录用户需重新授权才能生效

## 运行测试

```bash
python -m pytest tests/ -v
```
