import os

class Config:
    # ===== 认证跳板配置 =====
    # OAuth 由 pm-assist 认证跳板（https://pm.tmhcorps.cn/hub）代理完成，
    # 不再直接持有飞书应用凭证；client_id/secret 在 pm-assist .env 的 HUB_CLIENTS 注册
    HUB_URL = os.environ.get("HUB_URL", "https://pm.tmhcorps.cn")
    HUB_CLIENT_ID = os.environ.get("HUB_CLIENT_ID", "")
    HUB_CLIENT_SECRET = os.environ.get("HUB_CLIENT_SECRET", "")

    # 飞书 OpenAPI 基础地址（拿到的 user_access_token 依旧直连调用）
    API_BASE = "https://open.feishu.cn/open-apis"

    # ===== 应用配置 =====
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-this-to-a-random-secret-key")
    # 数据库文件路径
    DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "chatlogger.db"))
    # 本地缓存目录路径
    LOCAL_CACHE_DIR = os.environ.get("LOCAL_CACHE_DIR", os.path.join(os.path.dirname(__file__), "cache"))
    # 默认是否开启本地缓存（添加新群时默认状态）
    DEFAULT_LOCAL_CACHE = os.environ.get("DEFAULT_LOCAL_CACHE", "true").lower() in ("true", "1", "yes")
    # 单个附件/图片大小上限（MB），超出自动在备注中记录跳过（默认 20MB）
    MAX_ATTACHMENT_SIZE_MB = int(os.environ.get("MAX_ATTACHMENT_SIZE_MB", "20"))
    # 云文档快照缓存：单个文档最多转换的 Block 数，超出截断并标注（默认 2000）
    MAX_DOC_BLOCKS = int(os.environ.get("MAX_DOC_BLOCKS", "2000"))
    # 云文档快照缓存：是否下载文档内图片到 assets/docs/
    CACHE_DOC_IMAGES = os.environ.get("CACHE_DOC_IMAGES", "true").lower() in ("true", "1", "yes")

    # 服务器配置
    HOST = os.environ.get("HOST", "0.0.0.0")
    PORT = int(os.environ.get("PORT", "5000"))
    DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
