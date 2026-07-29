import requests
import time
import re
import io
from config import Config


class FeishuClient:
    """飞书 OpenAPI 客户端，使用 user_access_token 调用"""

    def __init__(self, access_token, refresh_token, token_expires_at, refresh_expires_at, user_id):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_expires_at = token_expires_at
        self.refresh_expires_at = refresh_expires_at
        self.user_id = user_id

    def _ensure_token(self):
        """检查 token 是否快过期，如果是则自动刷新"""
        if time.time() < self.token_expires_at - 300:
            return  # 还有5分钟以上，不需要刷新
        if not self.refresh_token or time.time() > self.refresh_expires_at:
            raise Exception("Token 已过期且无法刷新，请重新授权")
        self._refresh_access_token()

    def _refresh_access_token(self):
        """刷新 user_access_token"""
        resp = requests.post(Config.TOKEN_URL, json={
            "grant_type": "refresh_token",
            "client_id": Config.FEISHU_APP_ID,
            "client_secret": Config.FEISHU_APP_SECRET,
            "refresh_token": self.refresh_token,
        })
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"刷新 token 失败: {data}")

        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        self.token_expires_at = time.time() + data["expires_in"]
        self.refresh_expires_at = time.time() + data["refresh_token_expires_in"]

        # 持久化新 token
        import models
        models.update_user_tokens(
            self.user_id,
            self.access_token,
            self.refresh_token,
            data["expires_in"],
            data["refresh_token_expires_in"],
        )

    def _headers(self):
        self._ensure_token()
        return {"Authorization": f"Bearer {self.access_token}"}

    def _api_get(self, path, params=None):
        url = f"{Config.API_BASE}{path}"
        resp = requests.get(url, headers=self._headers(), params=params)
        return resp.json()

    def _api_post(self, path, json=None):
        url = f"{Config.API_BASE}{path}"
        resp = requests.post(url, headers=self._headers(), json=json)
        return resp.json()

    # ===== OAuth 静态方法 =====

    @staticmethod
    def get_authorize_url(state=""):
        """构造 OAuth 授权链接"""
        from urllib.parse import urlencode, quote
        params = {
            "client_id": Config.FEISHU_APP_ID,
            "redirect_uri": Config.REDIRECT_URI,
            "state": state,
            "scope": Config.OAUTH_SCOPES,
        }
        return f"{Config.AUTHORIZE_URL}?{urlencode(params)}"

    @staticmethod
    def exchange_code_for_token(code):
        """用授权码换取 user_access_token"""
        resp = requests.post(Config.TOKEN_URL, json={
            "grant_type": "authorization_code",
            "client_id": Config.FEISHU_APP_ID,
            "client_secret": Config.FEISHU_APP_SECRET,
            "code": code,
            "redirect_uri": Config.REDIRECT_URI,
        })
        return resp.json()

    @staticmethod
    def get_user_info(access_token):
        """获取当前登录用户信息"""
        resp = requests.get(
            f"{Config.API_BASE}/authen/v1/user_info",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return resp.json()

    # ===== 群聊信息 =====

    def get_chat_info(self, chat_id):
        """获取群信息（群名称等）"""
        data = self._api_get(f"/im/v1/chats/{chat_id}")
        if data.get("code") != 0:
            raise Exception(f"获取群信息失败: {data}")
        return data["data"]

    # ===== 消息读取 =====

    def list_messages(self, chat_id, page_size=50, page_token=None, sort_type="ByCreateTimeAsc"):
        """获取群聊消息（一页）"""
        params = {
            "container_id": chat_id,
            "container_id_type": "chat",
            "page_size": page_size,
            "sort_type": sort_type,
        }
        if page_token:
            params["page_token"] = page_token
        data = self._api_get("/im/v1/messages", params)
        if data.get("code") != 0:
            raise Exception(f"获取消息失败: {data}")
        return data["data"]

    def get_chat_message_count(self, chat_id):
        """获取群消息总数（取按时间倒序的第一条 message_position）"""
        data = self.list_messages(chat_id, page_size=1, sort_type="ByCreateTimeDesc")
        items = data.get("items", [])
        if not items:
            return 0
        return int(items[0].get("message_position") or 0)

    def list_all_messages(self, chat_id, start_position=0):
        """获取群聊全部消息（自动分页），从指定位置开始"""
        all_messages = []
        page_token = None
        while True:
            data = self.list_messages(chat_id, page_token=page_token)
            msgs = data.get("items", [])
            # 按消息位置过滤已同步的
            new_msgs = []
            for m in msgs:
                # 飞书返回字段是 message_position（字符串），缺失时用 0
                pos = int(m.get("message_position") or 0)
                if pos > start_position:
                    new_msgs.append(m)
            all_messages.extend(new_msgs)
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break
        all_messages.sort(key=lambda m: int(m.get("message_position") or 0))
        return all_messages

    # ===== 资源下载 =====

    def download_resource(self, message_id, file_key, resource_type="image"):
        """下载消息中的图片或文件，返回字节流"""
        url = f"{Config.API_BASE}/im/v1/messages/{message_id}/resources/{file_key}"
        params = {"type": resource_type}
        resp = requests.get(url, headers=self._headers(), params=params, stream=True)
        if resp.status_code != 200:
            raise Exception(f"下载资源失败: HTTP {resp.status_code}")
        # 获取文件名
        content_disp = resp.headers.get("Content-Disposition", "")
        filename = file_key
        if content_disp:
            match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\n]+)"?', content_disp)
            if match:
                filename = match.group(1)
        return resp.content, filename

    # ===== 多维表格操作 =====

    def create_bitable(self, name, table_name="消息记录"):
        """创建多维表格，配置字段"""
        # 1. 创建 Base
        data = self._api_post("/bitable/v1/apps", json={
            "name": name,
        })
        if data.get("code") != 0:
            raise Exception(f"创建多维表格失败: {data}")
        app = data["data"]["app"]
        base_token = app["app_token"]
        base_url = app["url"]

        # 2. 获取默认表 ID
        tables_data = self._api_get(f"/bitable/v1/apps/{base_token}/tables")
        if tables_data.get("code") != 0:
            raise Exception(f"获取表格列表失败: {tables_data}")
        default_table_id = tables_data["data"]["items"][0]["table_id"]

        # 3. 更新默认表名称（飞书更新数据表用 PATCH）
        self._api_patch(f"/bitable/v1/apps/{base_token}/tables/{default_table_id}", json={
            "name": table_name,
        })

        # 4. 整理字段：首个默认字段改名为「发言人」，其余默认字段删除，再新建所需字段
        fields_data = self._api_get(f"/bitable/v1/apps/{base_token}/tables/{default_table_id}/fields")
        if fields_data.get("code") == 0:
            items = fields_data["data"]["items"]
            # 飞书不允许删除 primary 主字段，把第一个字段（主键）改名为「发言人」
            # 更新字段用 PUT（非 PATCH），且 type 必填
            if items:
                primary = items[0]
                self._api_put(
                    f"/bitable/v1/apps/{base_token}/tables/{default_table_id}/fields/{primary['field_id']}",
                    json={"field_name": "发言人", "type": primary.get("type", 1)},
                )
            # 删除其余默认字段
            for field in items[1:]:
                self._api_delete(
                    f"/bitable/v1/apps/{base_token}/tables/{default_table_id}/fields/{field['field_id']}"
                )

        # 创建字段：时间、消息内容、附件（发言人已由主字段改名而来）
        field_defs = [
            {"field_name": "时间", "type": 5, "property": {"date_formatter": "yyyy-MM-dd HH:mm"}},
            {"field_name": "消息内容", "type": 1},
            {"field_name": "附件", "type": 17},
        ]
        for fd in field_defs:
            self._api_post(
                f"/bitable/v1/apps/{base_token}/tables/{default_table_id}/fields",
                json=fd,
            )

        return base_token, default_table_id, base_url

    def _api_put(self, path, json=None):
        url = f"{Config.API_BASE}{path}"
        resp = requests.put(url, headers=self._headers(), json=json)
        return resp.json()

    def _api_patch(self, path, json=None):
        url = f"{Config.API_BASE}{path}"
        resp = requests.patch(url, headers=self._headers(), json=json)
        return resp.json()

    def _api_delete(self, path):
        url = f"{Config.API_BASE}{path}"
        resp = requests.delete(url, headers=self._headers())
        return resp.json()

    def delete_bitable(self, base_token):
        """删除多维表格（走云文档接口，type=bitable）"""
        url = f"{Config.API_BASE}/drive/v1/files/{base_token}"
        resp = requests.delete(url, headers=self._headers(), params={"type": "bitable"})
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"删除多维表格失败: {data}")
        return True

    def batch_create_records(self, base_token, table_id, records):
        """批量创建记录，每批最多 500 条"""
        all_record_ids = []
        for i in range(0, len(records), 500):
            batch = records[i:i + 500]
            data = self._api_post(
                f"/bitable/v1/apps/{base_token}/tables/{table_id}/records/batch_create",
                json={"records": [{"fields": r} for r in batch]},
            )
            if data.get("code") != 0:
                raise Exception(f"批量创建记录失败: {data}")
            for rec in data["data"]["records"]:
                all_record_ids.append(rec["record_id"])
        return all_record_ids

    def upload_file(self, base_token, file_content, filename):
        """上传文件到云空间，返回 file_token"""
        url = f"{Config.API_BASE}/drive/v1/medias/upload_all"
        # 需要用 multipart 上传
        files = {
            "file_name": (None, filename),
            "parent_type": (None, "bitable_file"),
            "parent_node": (None, base_token),
            "size": (None, str(len(file_content))),
            "file": (filename, file_content),
        }
        resp = requests.post(url, headers=self._headers(), files=files)
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"上传文件失败: {data}")
        return data["data"]["file_token"]

    def upload_attachment_to_record(self, base_token, table_id, record_id, field_id, file_token):
        """将已上传的 file_token 追加到记录的附件字段"""
        # 先读取当前附件字段值
        data = self._api_get(
            f"/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}"
        )
        existing = []
        if data.get("code") == 0:
            existing = data["data"]["record"]["fields"].get(field_id, [])

        # 追加新附件
        existing.append({"file_token": file_token})
        update_data = self._api_patch(
            f"/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}",
            json={"fields": {field_id: existing}},
        )
        return update_data.get("code") == 0

    def get_field_id(self, base_token, table_id, field_name):
        """获取指定字段名的 field_id"""
        data = self._api_get(f"/bitable/v1/apps/{base_token}/tables/{table_id}/fields")
        if data.get("code") != 0:
            raise Exception(f"获取字段列表失败: {data}")
        for field in data["data"]["items"]:
            if field["field_name"] == field_name:
                return field["field_id"]
        return None


def process_message_content(msg):
    """处理消息内容，返回适合写入多维表格的文本"""
    msg_type = msg.get("msg_type", "")
    body = msg.get("body", {})
    content_str = body.get("content", "")

    if msg_type == "text":
        try:
            import json
            c = json.loads(content_str)
            return c.get("text", content_str)
        except (json.JSONDecodeError, TypeError):
            return content_str
    elif msg_type == "image":
        return "[图片]"
    elif msg_type == "post":
        try:
            import json
            c = json.loads(content_str)
            # 提取富文本中的文字
            text_parts = []
            locale = c.get("zh_cn") or c.get("en_us") or {}
            for block in locale.get("content", []):
                for elem in block:
                    if elem.get("tag") == "text":
                        text_parts.append(elem.get("text", ""))
                    elif elem.get("tag") == "img":
                        text_parts.append("[图片]")
                    elif elem.get("tag") == "a":
                        text_parts.append(elem.get("text", elem.get("href", "")))
            return "\n".join(text_parts) if text_parts else "[富文本消息]"
        except (json.JSONDecodeError, TypeError):
            return "[富文本消息]"
    elif msg_type == "file":
        try:
            import json
            c = json.loads(content_str)
            return f"[文件] {c.get('file_name', '未知文件')}"
        except (json.JSONDecodeError, TypeError):
            return "[文件]"
    elif msg_type == "video_chat":
        return "[视频通话]"
    elif msg_type == "system":
        return content_str
    else:
        return content_str if content_str else f"[{msg_type}]"


def extract_resource_keys(msg):
    """从消息中提取需要下载的资源 key 列表"""
    msg_type = msg.get("msg_type", "")
    body = msg.get("body", {})
    content_str = body.get("content", "")
    message_id = msg.get("message_id", "")
    resources = []

    import json

    if msg_type == "image":
        try:
            c = json.loads(content_str)
            key = c.get("image_key")
            if key:
                resources.append({"message_id": message_id, "file_key": key, "type": "image"})
        except (json.JSONDecodeError, TypeError):
            pass

    elif msg_type == "file":
        try:
            c = json.loads(content_str)
            key = c.get("file_key")
            name = c.get("file_name", "file")
            if key:
                resources.append({"message_id": message_id, "file_key": key, "type": "file", "file_name": name})
        except (json.JSONDecodeError, TypeError):
            pass

    elif msg_type == "post":
        try:
            c = json.loads(content_str)
            locale = c.get("zh_cn") or c.get("en_us") or {}
            for block in locale.get("content", []):
                for elem in block:
                    if elem.get("tag") == "img":
                        key = elem.get("image_key")
                        if key:
                            resources.append({"message_id": message_id, "file_key": key, "type": "image"})
                    elif elem.get("tag") == "media":
                        key = elem.get("file_key")
                        if key:
                            resources.append({"message_id": message_id, "file_key": key, "type": "file", "file_name": "media"})
        except (json.JSONDecodeError, TypeError):
            pass

    return resources


def get_sender_name(msg):
    """从消息中提取发送者名称"""
    sender = msg.get("sender", {})
    id_val = sender.get("id", "")
    id_type = sender.get("id_type", "")
    sender_type = sender.get("sender_type", "")

    if sender_type == "user":
        return id_val  # open_id，后续可以再查名字
    return "系统"
