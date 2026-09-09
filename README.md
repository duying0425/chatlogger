# 飞书群消息归档服务

将飞书群聊消息自动同步到多维表格的 Web 服务。

## 功能

- 飞书 OAuth 登录（用户身份授权）
- 添加/删除群聊 ID 配置
- 手动一键同步群消息到飞书多维表格
- 自动创建多维表格，字段格式统一：发言人、日期、消息内容、附件
- 消息中的图片和文件自动上传为附件
- **本地缓存选项**：开启后，同步的群聊文字自动规范存为标准 Markdown（`.md`），图片与附件下载到本地 `assets/` 目录并以相对路径链接
- **Markdown 在线预览**：点击主页群卡片直达预览页，支持图片全屏灯箱放大与附件一键直接下载
- **一键打包下载**：支持一键打包下载包含 Markdown 和全部附件的 ZIP 压缩包
- chat_id 与多维表格/本地缓存映射关系持久化存储

## 前置条件：创建飞书自建应用

### 1. 创建应用

前往 [飞书开放平台](https://open.feishu.cn/) → 创建企业自建应用

### 2. 开启机器人能力

应用详情 → 应用能力 → 添加机器人能力

### 3. 配置权限

应用详情 → 权限管理，申请以下权限：

| 权限 | 用途 |
|------|------|
| `im:message:readonly` | 读取群聊消息 |
| `im:message.group_msg:get_as_user` | 以用户身份获取群组消息 |
| `bitable:app` | 创建/编辑多维表格 |
| `offline_access` | 获取 refresh_token |

### 4. 配置重定向 URL

应用详情 → 开发配置 → 安全设置 → 重定向 URL

```
https://chatlogger.tmhcorps.cn/auth/callback
```

### 5. 开启 Token 刷新

安全设置 → 打开「刷新 user_access_token」开关

### 6. 发布应用

版本管理 → 创建版本 → 申请发布 → 管理员审批通过

### 7. 获取凭证

凭证和基础信息 → 记录 App ID 和 App Secret

## 部署

### 方式一：直接运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入实际配置

# 3. 运行
export $(cat .env | xargs) && python app.py
```

### 方式二：Gunicorn + Nginx（生产环境）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入实际配置

# 3. 启动 Gunicorn
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

### 方式三：Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
EXPOSE 5000
CMD ["gunicorn", "-c", "gunicorn_config.py", "app:app"]
```

```bash
docker build -t chatlogger .
docker run -d --name chatlogger \
  -p 5000:5000 \
  -e FEISHU_APP_ID=cli_xxx \
  -e FEISHU_APP_SECRET=xxx \
  -e REDIRECT_URI=https://chatlogger.tmhcorps.cn/auth/callback \
  -e SECRET_KEY=random-secret \
  -v /data/chatlogger:/app/data \
  chatlogger
```

## 使用流程

1. 访问 `https://chatlogger.tmhcorps.cn`
2. 点击「飞书登录」，授权应用
3. 在输入框中粘贴群聊 ID（格式：`oc_xxx`），可选择开启「本地缓存」，点击「添加」
4. 点击「同步」按钮，等待同步完成
5. 同步完成后：
   - 点击「查看表格」跳转到飞书多维表格
   - 开启本地缓存后，直接点击卡片或「在线预览」进入 Markdown 在线预览页面（支持图片灯箱预览与附件一键下载）
   - 点击「下载ZIP」可一键下载包含完整 Markdown 与图片附件的压缩包

## 数据存储

- 用户信息和 OAuth token 存储在 SQLite 数据库（`chatlogger.db`）
- 群聊 ID 与多维表格的映射关系也存储在同一数据库中
- 多维表格本身存储在飞书云空间中，创建者为授权用户
- **本地缓存文件**：存储在 `cache/{群名}_{chat_id}/` 目录下，包含 `{群名}.md` 与 `assets/` 附件子目录（可通过环境变量 `LOCAL_CACHE_DIR` 自定义路径）

## 注意事项

- `user_access_token` 有效期约 2 小时，服务会自动使用 `refresh_token` 刷新
- `refresh_token` 有效期约 7 天，用户授权满 365 天后需重新登录授权
- 首次同步某个群时会自动创建多维表格，后续同步为增量追加
- 附件（图片/文件）会自动下载并上传到对应记录
- 大文件（>100MB）可能下载超时
