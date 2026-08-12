import requests
import time
import re
import io
import threading
from config import Config


class SizeExceededError(Exception):
    """附件超过大小限制"""
    pass


# 飞书 API 错误码：限流 / 服务过载（可重试）
RETRYABLE_API_CODES = {99991663, 99991664}
# HTTP 状态码：可重试（限流 + 服务端错误）
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}

# 全局 token 刷新锁：飞书 refresh_token 一次性，
# 并发刷新会导致后续请求报 20073（refresh token has been used）
_token_refresh_lock = threading.Lock()


def _request_with_retry(request_fn, parse_json=True, max_retries=3):
    """执行 requests 调用并对瞬时错误做指数退避重试。

    可重试：网络错误、HTTP 429/5xx、飞书业务码 99991663/99991664
    不重试：业务错误、4xx（除 429）、其他业务码

    request_fn: 无参数 callable，返回 requests.Response
    parse_json: 是否解析响应为 json 并检查飞书业务码（stream 下载设为 False）
    """
    last_resp = None
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            resp = request_fn()
            last_resp = resp
            # HTTP 层判断
            if resp.status_code in RETRYABLE_HTTP_STATUS:
                last_exc = Exception(f"HTTP {resp.status_code}")
                if attempt < max_retries:
                    time.sleep(0.5 * (2 ** attempt))  # 0.5s, 1s, 2s
                    continue
                return resp  # 重试耗尽，返回错误响应
            # 业务层判断（仅 json 响应）
            if parse_json:
                ctype = resp.headers.get('content-type', '')
                if ctype.startswith('application/json'):
                    try:
                        data = resp.json()
                        code = data.get("code")
                        if code in RETRYABLE_API_CODES:
                            last_exc = Exception(f"飞书限流 code={code}")
                            if attempt < max_retries:
                                time.sleep(0.5 * (2 ** attempt))
                                continue
                    except ValueError:
                        pass  # 解析失败，交给调用方处理
            return resp
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            if attempt < max_retries:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise
    # 重试耗尽
    if last_exc:
        raise last_exc
    return last_resp


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
        """刷新 user_access_token。
        飞书 refresh_token 一次性，并发刷新会互相覆盖导致 20073 错误，
        用全局锁串行化。同一进程内多个线程同时触发刷新时，
        第一个完成后，后续线程检测 token 已更新就跳过。"""
        # 双重检查：拿到锁之前可能已有线程刷新成功
        if time.time() < self.token_expires_at - 300:
            return
        with _token_refresh_lock:
            # 再次检查：拿到锁后可能已被其他线程刷新
            if time.time() < self.token_expires_at - 300:
                return
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
        resp = _request_with_retry(lambda: requests.get(url, headers=self._headers(), params=params))
        return resp.json()

    def _api_post(self, path, json=None):
        url = f"{Config.API_BASE}{path}"
        resp = _request_with_retry(lambda: requests.post(url, headers=self._headers(), json=json))
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

    def download_resource(self, message_id, file_key, resource_type="image", max_size_mb=20, original_filename=""):
        """下载消息中的图片或文件，返回字节流。
        超过 max_size_mb 则抛出 SizeExceededError（飞书 upload_all 限制 20MB）。
        文件名优先级：original_filename > Content-Disposition > file_key + 扩展名"""
        url = f"{Config.API_BASE}/im/v1/messages/{message_id}/resources/{file_key}"
        params = {"type": resource_type}
        # stream 下载：parse_json=False 让重试逻辑只判断 HTTP 状态码
        resp = _request_with_retry(
            lambda: requests.get(url, headers=self._headers(), params=params, stream=True),
            parse_json=False,
        )
        if resp.status_code != 200:
            raise Exception(f"下载资源失败: HTTP {resp.status_code}")
        content_length = int(resp.headers.get("Content-Length", 0))
        if content_length > max_size_mb * 1024 * 1024:
            raise SizeExceededError(f"附件 {content_length // 1024 // 1024}MB 超过 {max_size_mb}MB 限制")
        # 获取文件名：优先用原始文件名，其次从 Content-Disposition 解析
        filename = ""
        if original_filename:
            filename = original_filename
        else:
            content_disp = resp.headers.get("Content-Disposition", "")
            if content_disp:
                match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\n]+)"?', content_disp)
                if match:
                    filename = match.group(1)
        # 兜底：用 file_key 作文件名
        if not filename:
            filename = file_key
        # 图片类型必须补 .jpg 扩展名（飞书消息图片都是 jpeg）
        # 否则多维表格无法识别图片格式进行预览
        if resource_type == "image" and "." not in filename.rsplit("/", 1)[-1]:
            filename = filename + ".jpg"
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

        # 4. 整理字段：首个默认字段（主键，不可删除）改名为「序号」（文本类型，存行号），
        #    其余默认字段删除后新建：发言人（人员）、时间、消息内容、附件
        #    注意：飞书主字段必须是文本类型，不能改成人员或自动编号
        fields_data = self._api_get(f"/bitable/v1/apps/{base_token}/tables/{default_table_id}/fields")
        if fields_data.get("code") == 0:
            items = fields_data["data"]["items"]
            # 主字段改名为「序号」
            if items:
                primary = items[0]
                self._api_put(
                    f"/bitable/v1/apps/{base_token}/tables/{default_table_id}/fields/{primary['field_id']}",
                    json={"field_name": "序号", "type": primary.get("type", 1)},
                )
            # 删除其余默认字段
            for field in items[1:]:
                self._api_delete(
                    f"/bitable/v1/apps/{base_token}/tables/{default_table_id}/fields/{field['field_id']}"
                )

        # 创建字段：发言人（人员，飞书自动解析 open_id 显示姓名+头像）、时间、消息内容、附件、消息ID、备注
        # 注意：人员字段 type=11 不能传 property，否则报 UserFieldPropertiesError
        field_defs = [
            {"field_name": "发言人", "type": 11},
            {"field_name": "时间", "type": 5, "property": {"date_formatter": "yyyy-MM-dd HH:mm"}},
            {"field_name": "消息内容", "type": 1},
            {"field_name": "附件", "type": 17},
            {"field_name": "消息ID", "type": 1},
            {"field_name": "备注", "type": 1},
        ]
        for fd in field_defs:
            self._api_post(
                f"/bitable/v1/apps/{base_token}/tables/{default_table_id}/fields",
                json=fd,
            )

        # 5. 删除飞书自动创建的默认空记录
        rec_data = self._api_get(f"/bitable/v1/apps/{base_token}/tables/{default_table_id}/records?page_size=500")
        if rec_data.get("code") == 0:
            default_record_ids = [r["record_id"] for r in rec_data["data"].get("items", []) if not r.get("fields")]
            if default_record_ids:
                self._api_post(
                    f"/bitable/v1/apps/{base_token}/tables/{default_table_id}/records/batch_delete",
                    json={"records": default_record_ids},
                )

        return base_token, default_table_id, base_url

    def _api_put(self, path, json=None):
        url = f"{Config.API_BASE}{path}"
        resp = _request_with_retry(lambda: requests.put(url, headers=self._headers(), json=json))
        return resp.json()

    def _api_patch(self, path, json=None):
        url = f"{Config.API_BASE}{path}"
        resp = _request_with_retry(lambda: requests.patch(url, headers=self._headers(), json=json))
        return resp.json()

    def _api_delete(self, path):
        url = f"{Config.API_BASE}{path}"
        resp = _request_with_retry(lambda: requests.delete(url, headers=self._headers()))
        return resp.json()

    def delete_bitable(self, base_token):
        """删除多维表格（走云文档接口，type=bitable）"""
        url = f"{Config.API_BASE}/drive/v1/files/{base_token}"
        resp = _request_with_retry(
            lambda: requests.delete(url, headers=self._headers(), params={"type": "bitable"})
        )
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"删除多维表格失败: {data}")
        return True

    def batch_create_records(self, base_token, table_id, records, on_batch_done=None):
        """批量创建记录，每批最多 500 条。

        on_batch_done(batch_index, batch_records, record_ids):
            每批成功后的回调，用于即时保存进度到数据库，
            避免后续批次失败导致已写入的记录下次同步重复写入。
        """
        all_record_ids = []
        for i in range(0, len(records), 500):
            batch_idx = i // 500
            batch = records[i:i + 500]
            data = self._api_post(
                f"/bitable/v1/apps/{base_token}/tables/{table_id}/records/batch_create",
                json={"records": [{"fields": r} for r in batch]},
            )
            if data.get("code") != 0:
                raise Exception(f"批量创建记录失败（第 {batch_idx + 1} 批，{len(batch)} 条）: {data}")
            batch_ids = [rec["record_id"] for rec in data["data"]["records"]]
            all_record_ids.extend(batch_ids)
            # 即时保存进度，避免后续失败导致重复写入
            if on_batch_done:
                on_batch_done(batch_idx, batch, batch_ids)
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
        resp = _request_with_retry(lambda: requests.post(url, headers=self._headers(), files=files))
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"上传文件失败: {data}")
        return data["data"]["file_token"]

    def upload_attachment_to_record(self, base_token, table_id, record_id, field_name, file_token):
        """将已上传的 file_token 追加到记录的附件字段。
        注意：飞书记录 fields 用 field_name 作 key（非 field_id）；
        单条更新记录用 PUT（会覆盖所有字段，需先读全部字段再合并附件）。"""
        # 读取当前记录的全部字段
        data = self._api_get(
            f"/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}"
        )
        if data.get("code") != 0:
            return False
        all_fields = data["data"]["record"]["fields"]

        # 追加新附件到指定字段
        existing = all_fields.get(field_name, []) or []
        existing.append({"file_token": file_token})
        all_fields[field_name] = existing

        # PUT 更新记录（会覆盖所有字段，所以传全部字段）
        update_data = self._api_put(
            f"/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}",
            json={"fields": all_fields},
        )
        return update_data.get("code") == 0

    def update_record_field(self, base_token, table_id, record_id, field_name, value):
        """更新单条记录的指定字段（不影响其他字段）。
        飞书 PUT 会覆盖所有字段，所以需先读全部字段再合并目标字段值。"""
        data = self._api_get(
            f"/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}"
        )
        if data.get("code") != 0:
            return False
        all_fields = data["data"]["record"]["fields"]
        all_fields[field_name] = value
        update_data = self._api_put(
            f"/bitable/v1/apps/{base_token}/tables/{table_id}/records/{record_id}",
            json={"fields": all_fields},
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

    def ensure_fields(self, base_token, table_id, field_specs):
        """检查并补建缺失的字段。
        field_specs: [{"name": "发言人", "type": 11, "property": {...}}, ...]
        返回新增的字段数。"""
        data = self._api_get(f"/bitable/v1/apps/{base_token}/tables/{table_id}/fields")
        if data.get("code") != 0:
            raise Exception(f"获取字段列表失败: {data}")
        existing = {f["field_name"] for f in data["data"]["items"]}
        created = 0
        for spec in field_specs:
            name = spec["name"]
            if name in existing:
                continue
            payload = {"field_name": name, "type": spec["type"]}
            if "property" in spec:
                payload["property"] = spec["property"]
            r = self._api_post(
                f"/bitable/v1/apps/{base_token}/tables/{table_id}/fields",
                json=payload,
            )
            if r.get("code") == 0:
                created += 1
                existing.add(name)
            else:
                print(f"[ensure_fields] 创建字段 {name} 失败: {r}")
        return created

    def record_fields_to_ids(self, base_token, table_id, record):
        """把 record.fields 的 key 从 field_name 转成 field_id（飞书 API 要求 field_id）。
        返回转换后的 fields dict，找不到的 field_name 会被跳过。"""
        field_map = {}  # field_name -> field_id
        data = self._api_get(f"/bitable/v1/apps/{base_token}/tables/{table_id}/fields")
        if data.get("code") != 0:
            return record
        for f in data["data"]["items"]:
            field_map[f["field_name"]] = f["field_id"]
        result = {}
        for name, val in record.items():
            fid = field_map.get(name)
            if fid:
                result[fid] = val
            else:
                print(f"[record_fields_to_ids] 字段 {name} 不存在，跳过")
        return result


def _parse_content(content_str):
    """解析消息 content 字段。
    飞书 API 返回的 body.content 通常是 JSON 字符串，
    但部分场景下可能是 dict / None / 空。统一返回 dict 或 None。
    """
    import json
    if not content_str:
        return None
    if isinstance(content_str, dict):
        return content_str
    if isinstance(content_str, str):
        try:
            return json.loads(content_str)
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _resolve_mentions(content_str, msg):
    """把飞书消息 content 中的 @_user_N 占位符替换为真实用户名。
    飞书 @人信息保存在消息顶层的 mentions 数组中，结构：
      [{"key":"@_user_1","id":{"open_id":"ou_xxx","user_id":"u_xxx"},"name":"张三"}, ...]
    content 中以 @_user_N 占位符形式出现（text / post 均可能出现）。
    """
    mentions = msg.get("mentions") or []
    if not mentions:
        return content_str
    for m in mentions:
        key = m.get("key") if isinstance(m, dict) else None
        if not key:
            continue
        # 名称优先级：name > id.user_id > id.open_id
        name = m.get("name") or ""
        if not name and isinstance(m.get("id"), dict):
            name = m["id"].get("user_id") or m["id"].get("open_id") or ""
        if name:
            content_str = content_str.replace(key, f"@{name}")
    return content_str


def process_message_content(msg):
    """处理消息内容，返回适合写入多维表格的文本"""
    msg_type = msg.get("msg_type", "")
    body = msg.get("body", {})
    content_str = body.get("content", "")
    # 统一替换 @人 占位符（text / post 都可能出现 @_user_N）
    content_str = _resolve_mentions(content_str, msg)

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
        # 飞书富文本（post）消息：body.content 是 JSON 字符串
        # 结构: {"zh_cn":{"title":"...","content":[[{"tag":"text","text":"..."},{"tag":"img","image_key":"..."}]]}}
        c = _parse_content(content_str)
        if c is None:
            return content_str or "[富文本消息]"
        # 兼容 locale 在不同层级的情况
        locale = c.get("zh_cn") or c.get("en_us") or c
        # title 是富文本标题（可选）
        title = locale.get("title") if isinstance(locale, dict) else None
        text_parts = []
        blocks = locale.get("content", []) if isinstance(locale, dict) else []
        for block in blocks:
            if not isinstance(block, list):
                continue
            line_parts = []
            for elem in block:
                if not isinstance(elem, dict):
                    continue
                tag = elem.get("tag")
                if tag == "text":
                    t = elem.get("text", "")
                    if t:
                        line_parts.append(t)
                elif tag == "a":
                    t = elem.get("text") or elem.get("href") or ""
                    if t:
                        line_parts.append(t)
                elif tag == "at":
                    # @人：显示用户名，没有则显示 user_id
                    t = elem.get("user_name") or elem.get("user_id") or ""
                    if t:
                        line_parts.append(f"@{t}")
                elif tag == "img":
                    line_parts.append("[图片]")
                elif tag == "media":
                    line_parts.append("[视频]" if elem.get("file_key", "").endswith("mp4")
                                     else "[文件]")
                elif tag == "emoji":
                    # 表情：飞书用 :text: 表示
                    t = elem.get("text") or elem.get("name") or ""
                    if t:
                        line_parts.append(f":{t}:" if not t.startswith(":") else t)
                elif tag == "code_block":
                    t = elem.get("text", "")
                    if t:
                        line_parts.append(t)
                else:
                    # 兜底：尝试取 text 字段，没有就跳过
                    t = elem.get("text", "")
                    if t:
                        line_parts.append(t)
            if line_parts:
                text_parts.append("".join(line_parts))
        # 标题作为第一行
        if title and title not in text_parts:
            text_parts.insert(0, title)
        return "\n".join(text_parts) if text_parts else "[富文本消息]"
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
        # 系统消息 content 形如 {"template":"{from_user} invited {to_chatters}...","from_user":["张三"],...}
        try:
            import json
            c = json.loads(content_str)
            template = c.get("template", "")
            # 替换 {from_user}/{to_chatters}/{to_user} 等占位符为实际值
            for key in ("from_user", "to_chatters", "to_user"):
                val = c.get(key)
                if val and isinstance(val, list):
                    template = template.replace("{" + key + "}", "、".join(val))
            return template if template else content_str
        except (json.JSONDecodeError, TypeError):
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
                # 飞书图片下载接口不返回文件名，用 image_key + .jpg
                resources.append({"message_id": message_id, "file_key": key, "type": "image",
                                  "file_name": f"{key}.jpg"})
        except (json.JSONDecodeError, TypeError):
            pass

    elif msg_type == "file":
        try:
            c = json.loads(content_str)
            key = c.get("file_key")
            name = c.get("file_name") or key or "file"
            if key:
                resources.append({"message_id": message_id, "file_key": key, "type": "file",
                                  "file_name": name})
        except (json.JSONDecodeError, TypeError):
            pass

    elif msg_type == "post":
        c = _parse_content(content_str)
        if isinstance(c, dict):
            locale = c.get("zh_cn") or c.get("en_us") or c
            if isinstance(locale, dict):
                for block in locale.get("content", []):
                    if not isinstance(block, list):
                        continue
                    for elem in block:
                        if not isinstance(elem, dict):
                            continue
                        tag = elem.get("tag")
                        if tag == "img":
                            key = elem.get("image_key")
                            if key:
                                resources.append({"message_id": message_id, "file_key": key, "type": "image",
                                                  "file_name": f"{key}.jpg"})
                        elif tag == "media":
                            key = elem.get("file_key")
                            name = elem.get("file_name") or "media"
                            if key:
                                resources.append({"message_id": message_id, "file_key": key, "type": "file",
                                                  "file_name": name})

    return resources
