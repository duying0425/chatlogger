import os
import time
import json
from flask import Flask, request, redirect, session, jsonify, render_template_string
from config import Config
import models
from feishu import (
    FeishuClient,
    process_message_content,
    extract_resource_keys,
    get_sender_name,
)

app = Flask(__name__)
app.secret_key = Config.SECRET_KEY

# 确保数据库初始化
models.init_db()

# ===== 辅助函数 =====

def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return models.get_user(user_id)

def get_feishu_client():
    user = get_current_user()
    if not user or not user.get("access_token"):
        return None
    return FeishuClient(
        access_token=user["access_token"],
        refresh_token=user["refresh_token"],
        token_expires_at=user["token_expires_at"],
        refresh_expires_at=user["refresh_expires_at"],
        user_id=user["id"],
    )

def timestamp_to_datetime(ts):
    """飞书消息的 create_time 是毫秒时间戳，转为 datetime 字符串"""
    if not ts:
        return ""
    try:
        ts_int = int(ts)
        # 飞书的时间戳是毫秒
        if ts_int > 1e12:
            ts_int = ts_int // 1000
        from datetime import datetime, timezone, timedelta
        dt = datetime.fromtimestamp(ts_int, tz=timezone(timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(ts)

# ===== 页面路由 =====

@app.route("/")
def index():
    user = get_current_user()
    if not user:
        return render_template_string(LOGIN_PAGE, auth_url=FeishuClient.get_authorize_url())

    chats = models.get_chats(user["id"])
    return render_template_string(INDEX_PAGE, user=user, chats=chats)

# ===== OAuth 路由 =====

@app.route("/auth/callback")
def auth_callback():
    code = request.args.get("code")
    error = request.args.get("error")

    if error:
        return f"授权失败: {error}", 400

    if not code:
        return "缺少授权码", 400

    # 用 code 换取 token
    token_data = FeishuClient.exchange_code_for_token(code)
    if token_data.get("code") != 0:
        return f"获取 token 失败: {json.dumps(token_data, ensure_ascii=False)}", 500

    access_token = token_data["access_token"]
    refresh_token = token_data["refresh_token"]
    expires_in = token_data["expires_in"]
    refresh_expires_in = token_data["refresh_token_expires_in"]

    # 获取用户信息
    user_info = FeishuClient.get_user_info(access_token)
    if user_info.get("code") != 0:
        return f"获取用户信息失败: {json.dumps(user_info, ensure_ascii=False)}", 500

    open_id = user_info["data"]["open_id"]
    name = user_info["data"].get("name", open_id)

    # 存入数据库
    user = models.get_or_create_user(open_id, name)
    models.update_user_tokens(user["id"], access_token, refresh_token, expires_in, refresh_expires_in)
    models.update_user_name(user["id"], name)

    session["user_id"] = user["id"]
    return redirect("/")

@app.route("/auth/logout")
def logout():
    session.clear()
    return redirect("/")

# ===== API 路由 =====

@app.route("/api/chats", methods=["GET"])
def api_get_chats():
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401
    chats = models.get_chats(user["id"])
    return jsonify({"chats": chats})

@app.route("/api/chat_stats/<chat_id>", methods=["GET"])
def api_chat_stats(chat_id):
    """实时查询群消息总数，返回已同步/待同步条数"""
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401
    client = get_feishu_client()
    if not client:
        return jsonify({"error": "Token 无效"}), 401

    chat_config = models.get_chat(user["id"], chat_id)
    if not chat_config:
        return jsonify({"error": "群聊未配置"}), 404

    try:
        total = client.get_chat_message_count(chat_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    synced = chat_config.get("record_count", 0) or 0
    last_pos = chat_config.get("last_synced_position", 0) or 0
    # 待同步 = 群里消息总数 - 已同步到的位置
    pending = max(0, total - last_pos)
    return jsonify({
        "total": total,
        "synced": synced,
        "pending": pending,
    })

@app.route("/api/chats", methods=["POST"])
def api_add_chat():
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401

    chat_id = request.json.get("chat_id", "").strip()
    if not chat_id:
        return jsonify({"error": "请输入群聊 ID"}), 400

    # 检查是否已存在
    existing = models.get_chat(user["id"], chat_id)
    if existing:
        return jsonify({"error": "该群聊已存在"}), 409

    # 尝试获取群名称
    client = get_feishu_client()
    chat_name = chat_id
    if client:
        try:
            chat_info = client.get_chat_info(chat_id)
            chat_name = chat_info.get("name", chat_id)
        except Exception:
            pass

    models.add_chat(user["id"], chat_id, chat_name)
    return jsonify({"ok": True, "chat_name": chat_name})

@app.route("/api/chats/<chat_id>", methods=["DELETE"])
def api_delete_chat(chat_id):
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401
    models.delete_chat(user["id"], chat_id)
    return jsonify({"ok": True})

@app.route("/api/sync/<chat_id>", methods=["POST"])
def api_sync(chat_id):
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401

    client = get_feishu_client()
    if not client:
        return jsonify({"error": "Token 无效，请重新登录"}), 401

    chat_config = models.get_chat(user["id"], chat_id)
    if not chat_config:
        return jsonify({"error": "群聊未配置"}), 404

    try:
        # 1. 获取群名称（如果还没有）
        chat_name = chat_config.get("chat_name") or chat_id
        try:
            chat_info = client.get_chat_info(chat_id)
            chat_name = chat_info.get("name", chat_name)
        except Exception:
            pass

        # 2. 获取新消息
        last_position = chat_config.get("last_synced_position", 0) or 0
        messages = client.list_all_messages(chat_id, start_position=last_position)

        if not messages:
            return jsonify({"ok": True, "message": "没有新消息", "new_count": 0})

        # 3. 确保多维表格存在
        base_token = chat_config.get("base_token")
        table_id = chat_config.get("table_id")
        base_url = chat_config.get("base_url")

        if not base_token:
            # 首次同步，创建多维表格
            base_token, table_id, base_url = client.create_bitable(
                name=f"{chat_name}-群消息记录",
                table_name="消息记录"
            )
            models.update_chat_table_info(user["id"], chat_id, base_token, table_id, base_url, chat_name)

        # 4. 获取附件字段 ID
        attach_field_id = client.get_field_id(base_token, table_id, "附件")

        # 5. 准备记录数据
        records = []
        msg_resources = {}  # message_id -> [resource_info]
        for m in messages:
            sender = get_sender_name(m)
            # 飞书多维表格日期字段要求毫秒时间戳
            create_time = m.get("create_time")
            try:
                date_value = int(create_time)
            except (TypeError, ValueError):
                date_value = None
            content = process_message_content(m)

            record = {
                "发言人": sender,
                "日期": date_value,
                "消息内容": content,
            }
            records.append(record)

            # 提取资源
            resources = extract_resource_keys(m)
            if resources:
                msg_resources[m["message_id"]] = resources

        # 6. 批量写入记录
        record_ids = client.batch_create_records(base_token, table_id, records)

        # 7. 下载并上传附件
        attach_count = 0
        for i, m in enumerate(messages):
            msg_id = m["message_id"]
            resources = msg_resources.get(msg_id, [])
            if not resources:
                continue
            record_id = record_ids[i] if i < len(record_ids) else None
            if not record_id:
                continue

            for r in resources:
                try:
                    file_content, filename = client.download_resource(
                        r["message_id"], r["file_key"], r["type"]
                    )
                    file_token = client.upload_file(base_token, file_content, filename)
                    client.upload_attachment_to_record(
                        base_token, table_id, record_id, attach_field_id, file_token
                    )
                    attach_count += 1
                except Exception as e:
                    # 附件上传失败不中断整体流程
                    print(f"附件上传失败: {e}")

        # 8. 更新同步状态
        new_last_position = int(messages[-1].get("message_position") or 0)
        new_record_count = (chat_config.get("record_count", 0) or 0) + len(messages)
        models.update_chat_sync_status(user["id"], chat_id, new_last_position, new_record_count)

        # 更新群名称（如果之前没有）
        if not chat_config.get("chat_name"):
            models.update_chat_table_info(user["id"], chat_id, base_token, table_id, base_url, chat_name)

        return jsonify({
            "ok": True,
            "new_count": len(messages),
            "attach_count": attach_count,
            "total_records": new_record_count,
            "base_url": base_url,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ===== 页面模板 =====

LOGIN_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>飞书群消息归档</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f6f8; display: flex; justify-content: center; align-items: center; min-height: 100vh; }
        .login-card { background: white; border-radius: 16px; padding: 48px 40px; text-align: center; box-shadow: 0 2px 12px rgba(0,0,0,0.08); max-width: 400px; width: 90%; }
        .login-card h1 { font-size: 24px; color: #1f2329; margin-bottom: 12px; }
        .login-card p { color: #646a73; font-size: 14px; margin-bottom: 32px; line-height: 1.6; }
        .login-btn { display: inline-block; background: #3370ff; color: white; text-decoration: none; padding: 12px 40px; border-radius: 8px; font-size: 15px; font-weight: 500; transition: background 0.2s; }
        .login-btn:hover { background: #2860e1; }
    </style>
</head>
<body>
    <div class="login-card">
        <h1>飞书群消息归档</h1>
        <p>登录后可配置群聊 ID，将群消息自动同步到飞书多维表格</p>
        <a href="{{ auth_url }}" class="login-btn">飞书登录</a>
    </div>
</body>
</html>
"""

INDEX_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>飞书群消息归档</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f6f8; color: #1f2329; }
        .header { background: white; border-bottom: 1px solid #e5e6eb; padding: 16px 32px; display: flex; justify-content: space-between; align-items: center; }
        .header h1 { font-size: 18px; font-weight: 600; }
        .header .user { font-size: 14px; color: #646a73; }
        .header .user a { color: #3370ff; text-decoration: none; margin-left: 16px; }
        .container { max-width: 900px; margin: 32px auto; padding: 0 20px; }
        .add-chat { background: white; border-radius: 12px; padding: 24px; margin-bottom: 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.05); }
        .add-chat h2 { font-size: 16px; margin-bottom: 16px; }
        .add-chat input { width: 70%; padding: 10px 14px; border: 1px solid #dee0e3; border-radius: 8px; font-size: 14px; outline: none; }
        .add-chat input:focus { border-color: #3370ff; }
        .add-chat button { padding: 10px 24px; background: #3370ff; color: white; border: none; border-radius: 8px; font-size: 14px; cursor: pointer; margin-left: 8px; }
        .add-chat button:hover { background: #2860e1; }
        .chat-list { background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.05); }
        .chat-item { padding: 20px 24px; border-bottom: 1px solid #f0f1f5; display: flex; justify-content: space-between; align-items: center; }
        .chat-item:last-child { border-bottom: none; }
        .chat-info .name { font-size: 15px; font-weight: 500; }
        .chat-info .meta { font-size: 12px; color: #8f959e; margin-top: 4px; }
        .chat-info .meta a { color: #3370ff; text-decoration: none; }
        .chat-actions { display: flex; gap: 8px; }
        .btn-sync { padding: 8px 20px; background: #00b42a; color: white; border: none; border-radius: 6px; font-size: 13px; cursor: pointer; }
        .btn-sync:hover { background: #009a25; }
        .btn-sync:disabled { background: #c9cdd4; cursor: not-allowed; }
        .btn-delete { padding: 8px 16px; background: #f53f3f; color: white; border: none; border-radius: 6px; font-size: 13px; cursor: pointer; }
        .btn-delete:hover { background: #d93636; }
        .empty { text-align: center; padding: 48px; color: #8f959e; }
        .toast { position: fixed; top: 20px; right: 20px; padding: 12px 24px; border-radius: 8px; color: white; font-size: 14px; z-index: 999; opacity: 0; transition: opacity 0.3s; }
        .toast.success { background: #00b42a; }
        .toast.error { background: #f53f3f; }
        .toast.show { opacity: 1; }
    </style>
</head>
<body>
    <div class="header">
        <h1>飞书群消息归档</h1>
        <div class="user">
            {{ user.name }}
            <a href="/auth/logout">退出</a>
        </div>
    </div>
    <div class="container">
        <div class="add-chat">
            <h2>添加群聊</h2>
            <input type="text" id="chatIdInput" placeholder="输入群聊 ID (oc_xxx)" />
            <button onclick="addChat()">添加</button>
        </div>
        <div class="chat-list" id="chatList">
            {% if chats %}
                {% for chat in chats %}
                <div class="chat-item" id="chat-{{ chat.chat_id }}">
                    <div class="chat-info">
                        <div class="name">{{ chat.chat_name or chat.chat_id }}</div>
                        <div class="meta">
                            ID: {{ chat.chat_id }}
                            {% if chat.record_count %} | 已同步 {{ chat.record_count }} 条{% endif %}
                            {% if chat.base_url %} | <a href="{{ chat.base_url }}" target="_blank">查看表格</a>{% endif %}
                        </div>
                    </div>
                    <div class="chat-actions">
                        <button class="btn-sync" onclick="syncChat('{{ chat.chat_id }}', this)">同步</button>
                        <button class="btn-delete" onclick="deleteChat('{{ chat.chat_id }}')">删除</button>
                    </div>
                </div>
                {% endfor %}
            {% else %}
                <div class="empty">还没有添加群聊，请在上方输入群聊 ID</div>
            {% endif %}
        </div>
    </div>
    <div class="toast" id="toast"></div>
    <script>
        function showToast(msg, type) {
            const toast = document.getElementById('toast');
            toast.textContent = msg;
            toast.className = 'toast ' + type + ' show';
            setTimeout(() => toast.className = 'toast ' + type, 3000);
        }

        async function addChat() {
            const input = document.getElementById('chatIdInput');
            const chatId = input.value.trim();
            if (!chatId) return;
            const resp = await fetch('/api/chats', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chat_id: chatId }),
            });
            const data = await resp.json();
            if (data.ok) { location.reload(); }
            else { showToast(data.error || '添加失败', 'error'); }
        }

        async function deleteChat(chatId) {
            if (!confirm('确认删除该群聊配置？多维表格中的数据不会被删除。')) return;
            const resp = await fetch('/api/chats/' + chatId, { method: 'DELETE' });
            const data = await resp.json();
            if (data.ok) { location.reload(); }
            else { showToast(data.error || '删除失败', 'error'); }
        }

        async function syncChat(chatId, btn) {
            btn.disabled = true;
            btn.textContent = '同步中...';
            try {
                const resp = await fetch('/api/sync/' + chatId, { method: 'POST' });
                const data = await resp.json();
                if (data.ok) {
                    const msg = '同步成功：新增 ' + data.new_count + ' 条消息' +
                        (data.attach_count > 0 ? '，附件 ' + data.attach_count + ' 个' : '');
                    showToast(msg, 'success');
                    setTimeout(() => location.reload(), 2000);
                } else {
                    showToast(data.error || '同步失败', 'error');
                    btn.disabled = false;
                    btn.textContent = '同步';
                }
            } catch (e) {
                showToast('网络错误', 'error');
                btn.disabled = false;
                btn.textContent = '同步';
            }
        }

        // 页面加载后实时查询每个群的待同步条数
        document.addEventListener('DOMContentLoaded', () => {
            document.querySelectorAll('.stats').forEach(async (el) => {
                const chatId = el.dataset.chatId;
                if (!chatId) return;
                try {
                    const resp = await fetch('/api/chat_stats/' + chatId);
                    const data = await resp.json();
                    if (data.error) {
                        el.textContent = '| ' + data.error;
                        return;
                    }
                    const total = data.total || 0;
                    const synced = data.synced || 0;
                    const pending = data.pending || 0;
                    const parts = ['| 已同步 ' + synced + ' / ' + total + ' 条'];
                    if (pending > 0) parts.push('待同步 ' + pending + ' 条');
                    el.textContent = parts.join(' ');
                } catch (e) {
                    // 查询失败保持原状
                }
            });
        });

        document.getElementById('chatIdInput').addEventListener('keydown', function(e) {
            if (e.key === 'Enter') addChat();
        });
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG)
