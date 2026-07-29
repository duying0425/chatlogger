import os

class Config:
    # ===== 飞书应用配置 =====
    # 在飞书开发者后台创建自建应用后，从「凭证和基础信息」获取
    FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
    FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

    # OAuth 重定向地址，部署时改为实际域名
    # 本地开发: http://localhost:5000/auth/callback
    # 生产环境: https://chatlogger.tmhcorps.cn/auth/callback
    REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://localhost:5000/auth/callback")

    # OAuth 授权页地址
    AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
    # Token 端点
    TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
    # 飞书 OpenAPI 基础地址
    API_BASE = "https://open.feishu.cn/open-apis"

    # 需要申请的 OAuth scope（空格分隔）
    # im:chat.members:read - 读取群成员（直接返回 name，无需通讯录权限）
    # im:chat:readonly - 读取群信息（群名称等）
    OAUTH_SCOPES = "im:message:readonly im:message.group_msg:get_as_user im:chat.members:read im:chat:readonly bitable:app offline_access"

    # ===== 应用配置 =====
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-this-to-a-random-secret-key")
    # 数据库文件路径
    DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "chatlogger.db"))

    # 服务器配置
    HOST = os.environ.get("HOST", "0.0.0.0")
    PORT = int(os.environ.get("PORT", "5000"))
    DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
