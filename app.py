import os
import time
import json
import threading
import traceback
import sys
from flask import Flask, request, redirect, session, jsonify, render_template_string, make_response, send_file, abort

# 确保在 Windows 控制台下输出 UTF-8，防止 GBK 终端乱码或 Emoji 导致 UnicodeEncodeError
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
from config import Config
import models
import local_cache
from feishu import (
    FeishuClient,
    process_message_content,
    extract_resource_keys,
    SizeExceededError,
)
from feishu_doc import cache_docs_for_messages, extract_doc_links

app = Flask(__name__)
app.secret_key = Config.SECRET_KEY

# 确保数据库初始化
models.init_db()

# ===== 同步进度全局状态 =====
# sync_progress[chat_id] = {"running": bool, "stage": str, "current": int, "total": int, "message": str, "result": dict, "error": str}
sync_progress = {}
sync_lock = threading.Lock()


def _set_progress(chat_id, stage, current=0, total=0, message="", result=None, error=None, running=True):
    with sync_lock:
        sync_progress[chat_id] = {
            "running": running,
            "stage": stage,
            "current": current,
            "total": total,
            "message": message,
            "result": result,
            "error": error,
        }


def _get_progress(chat_id):
    with sync_lock:
        return sync_progress.get(chat_id)

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

def timestamp_to_datetime(ts, with_seconds=True):
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
        fmt = "%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M"
        return dt.strftime(fmt)
    except (ValueError, TypeError):
        return str(ts)

# ===== 页面路由 =====

@app.route("/")
def index():
    user = get_current_user()
    if not user:
        resp = make_response(render_template_string(LOGIN_PAGE, auth_url=FeishuClient.get_authorize_url()))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    chats = models.get_chats(user["id"])
    for chat in chats:
        chat["has_cache"] = local_cache.has_cache(chat["chat_id"], chat.get("chat_name"))
    resp = make_response(render_template_string(INDEX_PAGE, user=user, chats=chats))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

# ===== OAuth 路由 =====

@app.route("/auth/callback")
def auth_callback():
    auth_code = request.args.get("auth_code")
    error = request.args.get("error")

    if error:
        return f"授权失败: {error}", 400

    if not auth_code:
        return "缺少授权码", 400

    # 用跳板下发的一次性 auth_code 换取 token
    token_data = FeishuClient.exchange_code_for_token(auth_code)
    if "access_token" not in token_data:
        return f"获取 token 失败: {json.dumps(token_data, ensure_ascii=False)}", 500

    access_token = token_data["access_token"]
    refresh_token = token_data["refresh_token"]
    expires_in = token_data["expires_in"]
    refresh_expires_in = token_data["refresh_token_expires_in"]

    # 用户信息由跳板在回调时一并获取返回；缺失时直连飞书兜底
    open_id = token_data.get("open_id", "")
    name = token_data.get("name", "")
    if not open_id:
        user_info = FeishuClient.get_user_info(access_token)
        if user_info.get("code") != 0:
            return f"获取用户信息失败: {json.dumps(user_info, ensure_ascii=False)}", 500
        open_id = user_info["data"]["open_id"]
        name = user_info["data"].get("name", open_id)
    elif not name:
        name = open_id

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
    for chat in chats:
        chat["has_cache"] = local_cache.has_cache(chat["chat_id"], chat.get("chat_name"))
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
        err_str = str(e)
        # token 失效友好提示
        if "20073" in err_str or "invalid_grant" in err_str or "Token 已过期且无法刷新" in err_str:
            return jsonify({"error": "登录已失效，请重新登录"}), 401
        return jsonify({"error": err_str}), 500

    synced = chat_config.get("record_count", 0) or 0
    last_pos = chat_config.get("last_synced_position", 0) or 0
    # 待同步 = 群里消息总数 - 已同步到的位置
    pending = max(0, total - last_pos)
    return jsonify({
        "total": total,
        "synced": synced,
        "pending": pending,
    })

@app.route("/api/chats/fetch_name", methods=["GET"])
def api_fetch_chat_name():
    """根据群聊 ID 预拉取群名称（只读，不入库）"""
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401

    chat_id = request.args.get("chat_id", "").strip()
    if not chat_id:
        return jsonify({"error": "请输入群聊 ID"}), 400

    client = get_feishu_client()
    if not client:
        return jsonify({"error": "飞书未授权或登录已失效"}), 401

    try:
        chat_info = client.get_chat_info(chat_id)
        name = (chat_info.get("name") or "").strip()
        return jsonify({"ok": True, "chat_name": name})
    except Exception as e:
        err_str = str(e)
        warning = "自动获取群名失败"
        if "232025" in err_str:
            warning = "应用未开通机器人能力，无法自动获取群名"
        return jsonify({"ok": False, "error": err_str, "warning": warning}), 200

@app.route("/api/user_chats", methods=["GET"])
def api_get_user_chats():
    """获取用户所有已缓存的群聊列表（支持关键词搜索）"""
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401

    keyword = request.args.get("q", "").strip()
    if keyword:
        chats = models.search_user_chats_cache(user["id"], keyword)
    else:
        chats = models.get_user_chats_cache(user["id"])
    meta = models.get_user_chats_cache_last_updated(user["id"])
    return jsonify({
        "ok": True,
        "chats": chats,
        "count": len(chats),
        "total_cached": meta["count"],
        "last_updated": meta["last_updated"],
    })

@app.route("/api/user_chats/sync", methods=["POST"])
def api_sync_user_chats():
    """从飞书全量拉取当前用户加入的所有群聊并刷新本地缓存"""
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401

    client = get_feishu_client()
    if not client:
        return jsonify({"error": "飞书未授权或登录已失效"}), 401

    try:
        chats_list = client.list_user_chats()
        models.save_user_chats_cache(user["id"], chats_list)

        # 刷新已配置群聊中缺失的 feishu_name
        name_map = {c["chat_id"]: (c.get("name") or "").strip() for c in chats_list if c.get("chat_id")}
        for c in models.get_chats(user["id"]):
            cid = c["chat_id"]
            if cid in name_map and name_map[cid] and name_map[cid] != c.get("feishu_name"):
                models.update_chat_feishu_name(user["id"], cid, name_map[cid])

        refreshed = models.get_user_chats_cache(user["id"])
        meta = models.get_user_chats_cache_last_updated(user["id"])
        return jsonify({
            "ok": True,
            "count": len(refreshed),
            "chats": refreshed,
            "last_updated": meta["last_updated"],
            "message": f"成功同步 {len(refreshed)} 个群聊信息",
        })
    except Exception as e:
        err_str = str(e)
        warning = "同步群聊列表失败"
        if "232025" in err_str:
            warning = "应用未开通机器人能力，无法同步群聊列表。请在飞书开发者后台开通「机器人」能力"
        elif "99991672" in err_str or "permission" in err_str.lower():
            warning = "飞书权限不足，请申请「获取群信息 (im:chat:readonly)」权限"
        return jsonify({"ok": False, "error": err_str, "warning": warning}), 200

@app.route("/api/chats", methods=["POST"])
def api_add_chat():
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401

    chat_id = request.json.get("chat_id", "").strip()
    if not chat_id:
        return jsonify({"error": "请输入群聊 ID"}), 400
    raw_input = request.json.get("chat_id", "").strip()
    if not raw_input:
        return jsonify({"error": "请输入群聊名称或 ID"}), 400

    # 用户可手动指定群名称（可选）。留空则尝试拉取，再不行退化为 chat_id
    custom_name = (request.json.get("chat_name") or "").strip()

    # 是否开启本地缓存（默认跟随全局设置）
    local_cache_opt = request.json.get("local_cache")
    if local_cache_opt is None:
        local_cache_opt = Config.DEFAULT_LOCAL_CACHE

    chat_id = None
    resolved_feishu_name = ""

    # 解析 chat_id：兼容群聊全称、部分群聊名称、chat_id (oc_xxx)
    if raw_input.startswith("oc_"):
        chat_id = raw_input
    else:
        # 尝试从用户缓存中检索
        matches = models.search_user_chats_cache(user["id"], raw_input)
        if len(matches) == 1:
            chat_id = matches[0]["chat_id"]
            resolved_feishu_name = matches[0]["chat_name"]
        elif len(matches) > 1:
            # 优先检查是否存在完全一致的群名称
            exact_matches = [m for m in matches if m["chat_name"].strip() == raw_input]
            if len(exact_matches) == 1:
                chat_id = exact_matches[0]["chat_id"]
                resolved_feishu_name = exact_matches[0]["chat_name"]
            else:
                names_preview = "、".join([f"「{m['chat_name']}」" for m in matches[:5]])
                if len(matches) > 5:
                    names_preview += f" 等 {len(matches)} 个群"
                return jsonify({
                    "error": f"匹配到多个群聊：{names_preview}，请在下拉列表中选择具体群聊"
                }), 400
        else:
            return jsonify({
                "error": f"未在缓存中找到名为“{raw_input}”的群聊。请点击输入框内的「同步群聊缓存」或直接输入以 oc_ 开头的群聊 ID"
            }), 404

    # 检查是否已存在
    existing = models.get_chat(user["id"], chat_id)
    if existing:
        return jsonify({"error": f"该群聊已存在（{existing.get('chat_name') or chat_id}）"}), 409

    # 无论是否自定义名称，都尝试拉取一次真实群名（用于列表第二行展示）
    feishu_name = resolved_feishu_name
    name_fetch_error = ""
    client = get_feishu_client()
    if client and not feishu_name:
        try:
            chat_info = client.get_chat_info(chat_id)
            feishu_name = (chat_info.get("name") or "").strip()
        except Exception as e:
            name_fetch_error = str(e)
            print(f"[add_chat] 自动获取群名失败 chat_id={chat_id}: {e}")

    # 显示名优先级：用户填写 > 真实群名 > chat_id
    chat_name = custom_name or feishu_name or chat_id

    models.add_chat(user["id"], chat_id, chat_name, local_cache=1 if local_cache_opt else 0,
                    feishu_name=feishu_name or None)
    result = {"ok": True, "chat_name": chat_name, "chat_id": chat_id, "local_cache": bool(local_cache_opt)}
    if name_fetch_error:
        # 获取失败原因可见，不再静默退化为 chat_id
        warning = "已添加，但自动获取群名失败，暂用群聊 ID 代替"
        if "232025" in name_fetch_error:
            warning += "：应用未开通机器人能力，请在飞书开发者后台「添加应用能力」中开通机器人"
        result["warning"] = warning
    return jsonify(result)

@app.route("/api/chats/<chat_id>/toggle_cache", methods=["POST"])
def api_toggle_cache(chat_id):
    """切换或设置指定群聊的本地缓存开关"""
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401

    chat_config = models.get_chat(user["id"], chat_id)
    if not chat_config:
        return jsonify({"error": "群聊未配置"}), 404

    req_data = request.get_json(silent=True) or {}
    enabled = req_data.get("enabled")
    if enabled is None:
        enabled = not bool(chat_config.get("local_cache", 0))
    else:
        enabled = bool(enabled)

    models.update_chat_local_cache(user["id"], chat_id, enabled)
    return jsonify({"ok": True, "local_cache": enabled})

@app.route("/api/chats/<chat_id>", methods=["DELETE"])
def api_delete_chat(chat_id):
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401

    # query 参数 delete_base=true 时同时删除飞书多维表格
    delete_base = request.args.get("delete_base") == "true"
    # query 参数 delete_cache=true 时同时物理删除本地缓存目录
    delete_cache = request.args.get("delete_cache") == "true"

    chat_config = models.get_chat(user["id"], chat_id)
    chat_name = chat_config.get("chat_name") if chat_config else None

    if delete_base and chat_config and chat_config.get("base_token"):
        client = get_feishu_client()
        if client:
            try:
                client.delete_bitable(chat_config["base_token"])
            except Exception as e:
                # 表格删除失败不阻断配置删除，仅返回警告
                return jsonify({"error": f"删除飞书表格失败: {e}"}), 500

    if delete_cache:
        try:
            local_cache.delete_cache(chat_id, chat_name)
        except Exception as e:
            print(f"[local_cache] 删除群聊缓存失败: {e}")

    models.delete_chat(user["id"], chat_id)
    return jsonify({"ok": True})

# ===== 本地缓存预览与资源路由 =====

@app.route("/cache/<chat_id>/view")
def cache_view(chat_id):
    """在线预览 Markdown 归档页面，支持图片灯箱和附件一键下载"""
    user = get_current_user()
    if not user:
        return redirect("/")

    chat_config = models.get_chat(user["id"], chat_id)
    if not chat_config:
        return "群聊未配置", 404

    chat_name = chat_config.get("chat_name") or chat_id
    raw_md = local_cache.get_raw_markdown(chat_id, chat_name)
    info = local_cache.get_cache_info(chat_id, chat_name)

    return render_template_string(
        VIEW_PAGE,
        chat=chat_config,
        chat_id=chat_id,
        chat_name=chat_name,
        raw_markdown=raw_md or "",
        has_cache=bool(raw_md),
        info=info,
        user=user,
    )

@app.route("/cache/<chat_id>/raw")
def cache_raw(chat_id):
    """获取/下载原始 .md 文件"""
    user = get_current_user()
    if not user:
        return redirect("/")

    chat_config = models.get_chat(user["id"], chat_id)
    if not chat_config:
        return "群聊未配置", 404

    chat_name = chat_config.get("chat_name") or chat_id
    raw_md = local_cache.get_raw_markdown(chat_id, chat_name)
    if raw_md is None:
        return "尚未生成本地缓存", 404

    safe_name = local_cache.sanitize_filename(chat_name, fallback="chat")
    from urllib.parse import quote
    encoded_name = quote(f"{safe_name}.md")
    resp = make_response(raw_md)
    resp.headers["Content-Type"] = "text/markdown; charset=utf-8"
    resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{encoded_name}"
    return resp

@app.route("/cache/<chat_id>/download")
def cache_download(chat_id):
    """打包下载本地缓存完整 ZIP 归档（含 Markdown 和 assets 目录）"""
    user = get_current_user()
    if not user:
        return redirect("/")

    chat_config = models.get_chat(user["id"], chat_id)
    if not chat_config:
        return "群聊未配置", 404

    chat_name = chat_config.get("chat_name") or chat_id
    if not local_cache.has_cache(chat_id, chat_name):
        return "尚未生成本地缓存", 404

    memory_file, zip_filename = local_cache.build_cache_zip(chat_id, chat_name)
    from urllib.parse import quote
    encoded_name = quote(zip_filename)
    resp = send_file(
        memory_file,
        mimetype="application/zip",
        as_attachment=True,
        download_name=zip_filename,
    )
    resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{encoded_name}"
    return resp

@app.route("/cache/<chat_id>/assets/<path:filename>")
def cache_asset(chat_id, filename):
    """提供本地缓存附件访问服务：图片内嵌渲染预览，非图片/点击链接直接下载"""
    user = get_current_user()
    if not user:
        return "未登录", 401

    chat_config = models.get_chat(user["id"], chat_id)
    if not chat_config:
        return "群聊未配置", 404

    chat_name = chat_config.get("chat_name") or chat_id
    chat_dir = local_cache.get_chat_cache_dir(chat_id, chat_name)
    assets_dir = os.path.join(chat_dir, "assets")
    file_path = os.path.abspath(os.path.join(assets_dir, filename))

    # 安全检查：防止目录遍历
    if not file_path.startswith(os.path.abspath(assets_dir)):
        return "非法请求路径", 403

    if not os.path.exists(file_path):
        return "附件不存在", 404

    is_image = filename.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"))
    force_download = request.args.get("download") == "1" or not is_image

    base_name = os.path.basename(filename)
    from urllib.parse import quote
    encoded_name = quote(base_name)
    resp = send_file(
        file_path,
        as_attachment=force_download,
        download_name=base_name,
    )
    if force_download:
        resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{encoded_name}"
    return resp

@app.route("/api/sync/<chat_id>", methods=["POST"])
def api_sync(chat_id):
    """启动同步任务（后台线程执行），立即返回。"""
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401

    client = get_feishu_client()
    if not client:
        return jsonify({"error": "Token 无效，请重新登录"}), 401

    chat_config = models.get_chat(user["id"], chat_id)
    if not chat_config:
        return jsonify({"error": "群聊未配置"}), 404

    # 防止重复点击：如果已有任务在运行，拒绝
    progress = _get_progress(chat_id)
    if progress and progress.get("running"):
        return jsonify({"error": "已有同步任务进行中", "progress": progress}), 409

    # 初始化进度状态
    _set_progress(chat_id, stage="starting", current=0, total=0, message="准备开始同步...", running=True)

    # 启动后台线程
    t = threading.Thread(target=_run_sync, args=(user["id"], chat_id), daemon=True)
    t.start()

    return jsonify({"ok": True, "message": "同步任务已启动", "started": True})


@app.route("/api/sync_all", methods=["POST"])
def api_sync_all():
    """批量启动当前用户所有已配置群聊的同步任务"""
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401

    client = get_feishu_client()
    if not client:
        return jsonify({"error": "Token 无效，请重新登录"}), 401

    chats = models.get_chats(user["id"])
    if not chats:
        return jsonify({"error": "暂无可同步的群聊配置"}), 400

    started = []
    skipped = []
    for c in chats:
        cid = c["chat_id"]
        progress = _get_progress(cid)
        if progress and progress.get("running"):
            skipped.append(cid)
            continue
        _set_progress(cid, stage="starting", current=0, total=0, message="准备开始同步...", running=True)
        t = threading.Thread(target=_run_sync, args=(user["id"], cid), daemon=True)
        t.start()
        started.append(cid)

    msg = f"已启动 {len(started)} 个群聊的同步任务"
    if skipped:
        msg += f"，{len(skipped)} 个群聊已在同步中（跳过）"

    return jsonify({
        "ok": True,
        "started": started,
        "skipped": skipped,
        "total": len(chats),
        "message": msg,
    })


@app.route("/api/sync_status/<chat_id>", methods=["GET"])
def api_sync_status(chat_id):
    """查询同步进度"""
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401

    progress = _get_progress(chat_id)
    if not progress:
        return jsonify({"running": False, "stage": "idle", "message": "暂无任务"})
    return jsonify(progress)


@app.route("/api/debug/messages/<chat_id>", methods=["GET"])
def api_debug_messages(chat_id):
    """调试端点：按时间范围拉取飞书原始消息 JSON（未经任何加工），用于核对本地缓存是否忠实。

    查询参数：
      start_time / end_time: "YYYY-MM-DD HH:mm:ss"（北京时间，可只给一边）
      limit: 最多返回条数（默认 50，防止响应过大）
    """
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401

    client = get_feishu_client()
    if not client:
        return jsonify({"error": "Token 无效，请重新登录"}), 401

    chat_config = models.get_chat(user["id"], chat_id)
    if not chat_config:
        return jsonify({"error": "群聊未配置"}), 404

    def parse_ts(s, end=False):
        """把 "YYYY-MM-DD HH:mm[:ss]" 转为毫秒时间戳；end=True 时秒缺省取 59"""
        if not s:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                from datetime import datetime, timezone, timedelta
                dt = datetime.strptime(s.strip(), fmt)
                if end and fmt != "%Y-%m-%d %H:%M:%S":
                    dt = dt.replace(hour=23, minute=59, second=59)
                return int(dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
            except ValueError:
                continue
        return jsonify({"error": f"时间格式无效: {s}（应为 YYYY-MM-DD HH:mm:ss）"}), 400

    try:
        start_ts = parse_ts(request.args.get("start_time"))
        end_ts = parse_ts(request.args.get("end_time"), end=True)
        if isinstance(start_ts, tuple):
            return start_ts
        if isinstance(end_ts, tuple):
            return end_ts
    except Exception as e:
        return jsonify({"error": f"时间参数解析失败: {e}"}), 400

    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except ValueError:
        limit = 50

    # 拉取消息（复用现有分页逻辑，不做增量过滤）
    try:
        messages = client.list_all_messages(chat_id, start_position=0)
    except Exception as e:
        return jsonify({"error": f"拉取消息失败: {e}"}), 502

    def in_range(m):
        ts = int(m.get("create_time") or 0)
        if start_ts and ts < start_ts:
            return False
        if end_ts and ts > end_ts:
            return False
        return True

    filtered = [m for m in messages if in_range(m)]
    filtered = filtered[:limit]

    # 附上可读时间便于对照
    result = []
    for m in filtered:
        result.append({
            "create_time": m.get("create_time"),
            "time_readable": timestamp_to_datetime(m.get("create_time")),
            "sender": m.get("sender"),
            "msg_type": m.get("msg_type"),
            "message_id": m.get("message_id"),
            "raw": m,
        })

    return jsonify({
        "chat_id": chat_id,
        "count": len(result),
        "messages": result,
    })


def _run_sync(user_id, chat_id):
    """在后台线程中执行同步主流程，实时更新进度状态。"""
    # 在子线程中重新获取用户和 client（Flask session 在子线程不可用）
    user = models.get_user(user_id)
    if not user or not user.get("access_token"):
        _set_progress(chat_id, stage="error", running=False, error="Token 无效，请重新登录")
        return

    client = FeishuClient(
        access_token=user["access_token"],
        refresh_token=user["refresh_token"],
        token_expires_at=user["token_expires_at"],
        refresh_expires_at=user["refresh_expires_at"],
        user_id=user["id"],
    )

    try:
        chat_config = models.get_chat(user["id"], chat_id)
        if not chat_config:
            _set_progress(chat_id, stage="error", running=False, error="群聊未配置")
            return

        is_local_cache_enabled = bool(chat_config.get("local_cache", 0))

        # 1. 获取群信息：刷新真实群名（群名可能已变）；chat_name 仅在缺失时补齐，避免覆盖自定义名称
        _set_progress(chat_id, stage="fetching_chat_info", message="获取群信息...")
        chat_name = chat_config.get("chat_name") or ""
        try:
            chat_info = client.get_chat_info(chat_id)
            fetched = (chat_info.get("name") or "").strip()
            if fetched:
                models.update_chat_feishu_name(user["id"], chat_id, fetched)
                if not chat_name or chat_name == chat_id:
                    chat_name = fetched
        except Exception as e:
            print(f"[sync] 自动获取群名失败 chat_id={chat_id}: {e}")
        if not chat_name:
            chat_name = chat_id

        # 2. 获取新消息
        _set_progress(chat_id, stage="fetching_messages", message="拉取群消息...")
        last_position = chat_config.get("last_synced_position", 0) or 0
        messages = client.list_all_messages(chat_id, start_position=last_position)

        if not messages:
            _set_progress(chat_id, stage="done", running=False, message="没有新消息",
                          result={"ok": True, "new_count": 0, "message": "没有新消息"})
            return

        total = len(messages)
        _set_progress(chat_id, stage="messages_fetched", current=0, total=total,
                      message=f"已拉取 {total} 条新消息")

        # 解析发言人姓名（用于本地缓存 Markdown 归档）
        speaker_names = {}
        if user.get("open_id") and user.get("name"):
            speaker_names[user["open_id"]] = user["name"]
        try:
            members_map = client.get_chat_members_safe(chat_id)
            if members_map:
                speaker_names.update(members_map)
        except Exception:
            pass
        for m in messages:
            for item in (m.get("mentions") or []):
                if isinstance(item, dict):
                    oid = item.get("id", {}).get("open_id") if isinstance(item.get("id"), dict) else None
                    name = item.get("name")
                    if oid and name:
                        speaker_names[oid] = name

        # 3. 确保多维表格存在
        base_token = chat_config.get("base_token")
        table_id = chat_config.get("table_id")
        base_url = chat_config.get("base_url")

        if not base_token:
            _set_progress(chat_id, stage="creating_bitable", current=0, total=total,
                          message="首次同步，创建多维表格...")
            base_token, table_id, base_url = client.create_bitable(
                name=f"{chat_name}-群消息记录",
                table_name="消息记录"
            )
            models.update_chat_table_info(user["id"], chat_id, base_token, table_id, base_url, chat_name)

        # 4. 准备记录数据
        # 发言人用人员字段：直接传 open_id，飞书自动解析为姓名+头像，无需我们调接口
        # 自动补建缺失字段（兼容旧表）
        try:
            client.ensure_fields(base_token, table_id, [
                {"name": "序号", "type": 1},
                {"name": "发言人", "type": 11},
                {"name": "时间", "type": 5, "property": {"date_formatter": "yyyy-MM-dd HH:mm"}},
                {"name": "消息内容", "type": 1},
                {"name": "附件", "type": 17},
                {"name": "备注", "type": 1},
            ])
        except Exception as e:
            print(f"[ensure_fields] 补建字段失败（继续尝试）: {e}")
        # 序号从已有记录数 + 1 开始
        start_seq = (chat_config.get("record_count", 0) or 0) + 1
        _set_progress(chat_id, stage="preparing_records", current=0, total=total,
                      message=f"准备记录数据 (0/{total})...")
        records = []
        msg_resources = {}
        for idx, m in enumerate(messages):
            sender_id = m.get("sender", {}).get("id", "")
            msg_type = m.get("msg_type", "")
            create_time = m.get("create_time")
            try:
                date_value = int(create_time)
            except (TypeError, ValueError):
                date_value = None
            content = process_message_content(m)

            # 发言人字段处理：
            # - 用户消息（sender.id 以 ou_ 开头）：写入人员字段，飞书自动显示姓名
            # - 系统消息（msg_type=system）：发言人留空（消息内容本身已是描述）
            # - 机器人消息（sender.id 以 cli_ 开头，非 system）：发言人留空，消息内容前加[机器人名]
            if sender_id and sender_id.startswith("ou_"):
                speaker_field = [{"id": sender_id}]
            elif msg_type == "system":
                speaker_field = []
                # 系统消息不加前缀，内容本身已是描述（如「张三邀请李四加入群组」）
            elif sender_id and sender_id.startswith("cli_"):
                # 机器人消息：加 [机器人名] 前缀
                bot_name = m.get("sender", {}).get("name", "") or "机器人"
                content = f"[{bot_name}] {content}" if content else f"[{bot_name}]"
                speaker_field = []
            else:
                speaker_field = []

            record = {
                "序号": str(start_seq + idx),
                "发言人": speaker_field,
                "时间": date_value,
                "消息内容": content,
            }
            records.append(record)

            resources = extract_resource_keys(m)
            if resources:
                msg_resources[m["message_id"]] = resources

            # 每处理 50 条更新一次进度，避免过于频繁
            if (idx + 1) % 50 == 0 or idx + 1 == total:
                _set_progress(chat_id, stage="preparing_records", current=idx + 1, total=total,
                              message=f"准备记录数据 ({idx + 1}/{total})...")

        # 6. 批量写入记录（每批成功后即时保存进度，防止后续失败导致重复写入）
        _set_progress(chat_id, stage="writing_records", current=0, total=total,
                      message=f"写入多维表格 (0/{total})...")
        print(f"[DEBUG] 准备写入 {len(records)} 条记录")
        # 记录已写入数量与最后一条消息的 position，每批成功后立即落库
        written_count = [0]  # 用 list 闭包可变
        written_last_pos = [last_position]
        # 消息在 records 中按原 messages 顺序对应，每批 batch_size=500
        batch_size = 500

        def _on_batch_done(batch_idx, batch_records, batch_ids):
            # batch_records 对应 messages[start_idx : start_idx+len(batch_records)]
            start_idx = batch_idx * batch_size
            end_idx = start_idx + len(batch_records)
            # 飞书 message_position 可能缺失，兜底用 start_seq+end_idx-1
            last_msg = messages[end_idx - 1] if end_idx - 1 < total else messages[-1]
            last_pos = int(last_msg.get("message_position") or 0)
            new_count = (chat_config.get("record_count", 0) or 0) + end_idx
            try:
                models.update_chat_sync_status(user["id"], chat_id, last_pos, new_count)
            except Exception as e:
                print(f"[WARN] 即时保存进度失败（不阻断同步）: {e}")
            written_count[0] = end_idx
            written_last_pos[0] = last_pos
            _set_progress(chat_id, stage="writing_records", current=end_idx, total=total,
                          message=f"写入多维表格 ({end_idx}/{total})...")

        record_ids = client.batch_create_records(
            base_token, table_id, records, on_batch_done=_on_batch_done
        )
        print(f"[DEBUG] 写入完成，返回 {len(record_ids)} 个 record_id")
        _set_progress(chat_id, stage="records_written", current=total, total=total,
                      message=f"记录写入完成 ({len(record_ids)} 条)")

        # 7. 下载并上传附件
        attach_count = 0
        skipped_count = 0
        # 记录每条消息被跳过的附件说明，后续批量追加到「消息内容」字段
        # 结构：record_id -> [说明1, 说明2, ...]
        skipped_notes = {}
        # 计算附件总数（用于进度展示）
        total_attach_tasks = sum(len(msg_resources.get(m["message_id"], [])) for m in messages)
        if total_attach_tasks > 0:
            _set_progress(chat_id, stage="uploading_attachments", current=0, total=total_attach_tasks,
                          message=f"上传附件 (0/{total_attach_tasks})...")
        else:
            _set_progress(chat_id, stage="uploading_attachments", current=0, total=0,
                          message="无附件")

        attach_done = 0
        downloaded_assets = {}  # file_key -> rel_path

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
                    # 单个附件失败重试 1 次（瞬时网络抖动）
                    # SizeExceededError 不重试（业务错误，重试也必失败）
                    last_err = None
                    for attempt in range(2):  # 0=首次, 1=重试
                        try:
                            file_content, filename = client.download_resource(
                                r["message_id"], r["file_key"], r["type"],
                                max_size_mb=Config.MAX_ATTACHMENT_SIZE_MB,
                                original_filename=r.get("file_name", "")
                            )
                            # 若开启本地缓存，保存文件至 cache/{chat}/assets/
                            if is_local_cache_enabled:
                                try:
                                    rel_path = local_cache.save_asset(
                                        chat_id, chat_name, file_content, filename, r["file_key"]
                                    )
                                    downloaded_assets[r["file_key"]] = rel_path
                                except Exception as ce:
                                    print(f"[local_cache] 保存附件失败: {ce}")

                            file_token = client.upload_file(base_token, file_content, filename)
                            client.upload_attachment_to_record(
                                base_token, table_id, record_id, "附件", file_token
                            )
                            attach_count += 1
                            last_err = None
                            break
                        except SizeExceededError:
                            raise  # 直接走外层 except
                        except Exception as e:
                            last_err = e
                            if attempt == 0:
                                time.sleep(0.5)
                    if last_err:
                        raise last_err
                except SizeExceededError as e:
                    skipped_count += 1
                    # 记录跳过说明：包含文件名和原因
                    fname = r.get("file_name") or r.get("file_key", "")
                    ftype = "图片" if r.get("type") == "image" else "文件"
                    note = f"[跳过{ftype}：{fname}（{e}）]"
                    skipped_notes.setdefault(record_id, []).append(note)
                    print(f"[跳过大附件] {e}")
                except Exception as e:
                    print(f"附件上传失败: {e}")
                finally:
                    attach_done += 1
                    _set_progress(chat_id, stage="uploading_attachments",
                                 current=attach_done, total=total_attach_tasks,
                                 message=f"上传附件 ({attach_done}/{total_attach_tasks})...")

        # 7.5 把跳过说明写入「备注」字段（不污染消息内容或附件列）
        if skipped_notes:
            for rid, notes in skipped_notes.items():
                try:
                    client.update_record_field(
                        base_token, table_id, rid, "备注", " ".join(notes)
                    )
                except Exception as e:
                    print(f"[写入跳过说明失败] {e}")

        # 7.6 若开启本地缓存，格式化所有消息并追加写入 Markdown 文件
        doc_map = {}
        doc_fail_map = {}
        if is_local_cache_enabled:
            try:
                _set_progress(chat_id, stage="saving_local_cache", current=0, total=total,
                              message="正在写入本地缓存 Markdown...")

                # 7.65 云文档快照：扫描本轮消息中的飞书云文档链接并缓存内容
                try:
                    def _save_doc(doc_token, title, content):
                        return local_cache.save_doc(chat_id, chat_name, doc_token, title, content)

                    def _save_doc_image(image_token):
                        content, ext = client.download_drive_media(image_token)
                        return local_cache.save_doc_image(
                            chat_id, chat_name, image_token, content, ext)

                    doc_map, doc_failures = cache_docs_for_messages(
                        client, messages, _save_doc, save_doc_image=_save_doc_image,
                        on_progress=lambda d, t, msg: _set_progress(
                            chat_id, stage="caching_docs", current=d, total=t, message=msg))
                    doc_fail_map = {token: err for token, err in doc_failures}
                except Exception as de:
                    print(f"[doc_cache] 云文档缓存失败（不阻断同步）: {de}")

                if doc_map or doc_fail_map:
                    _set_progress(chat_id, stage="saving_local_cache", current=0, total=total,
                                  message="正在写入本地缓存 Markdown...")

                md_blocks = []
                for i, m in enumerate(messages):
                    sid = m.get("sender", {}).get("id", "")
                    m_type = m.get("msg_type", "")
                    c_time = m.get("create_time")
                    d_str = timestamp_to_datetime(c_time, with_seconds=True)

                    if sid.startswith("cli_"):
                        bot_name = m.get("sender", {}).get("name", "") or "机器人"
                        s_name = f"🤖 {bot_name}"
                    elif m_type == "system":
                        s_name = "系统"
                    else:
                        s_name = speaker_names.get(sid, sid or "成员")

                    # 提取该消息关联的 asset_map: file_key -> rel_path
                    m_assets = {}
                    for r in extract_resource_keys(m):
                        fk = r.get("file_key")
                        if fk and fk in downloaded_assets:
                            m_assets[fk] = downloaded_assets[fk]

                    record_id = record_ids[i] if i < len(record_ids) else None
                    m_skipped = list(skipped_notes.get(record_id)) if record_id and skipped_notes.get(record_id) else None
                    # 该消息中的云文档快照失败提示（无权限/类型不支持等）
                    if doc_fail_map:
                        for link in extract_doc_links(m):
                            ferr = doc_fail_map.get(link.get("token"))
                            if ferr:
                                m_skipped = (m_skipped or []) + [f"[云文档快照失败: {ferr}]"]
                    if m_skipped is not None and not m_skipped:
                        m_skipped = None

                    block = local_cache.format_message_to_markdown(
                        m, s_name, d_str, asset_map=m_assets, skipped_notes=m_skipped,
                        doc_map=doc_map
                    )
                    md_blocks.append(block)

                local_cache.append_messages_to_cache(chat_id, chat_name, md_blocks)
            except Exception as ce:
                print(f"[local_cache] 写入 Markdown 失败: {ce}")

        # 8. 更新同步状态
        new_last_position = int(messages[-1].get("message_position") or 0)
        new_record_count = (chat_config.get("record_count", 0) or 0) + len(messages)
        models.update_chat_sync_status(user["id"], chat_id, new_last_position, new_record_count)

        if not chat_config.get("chat_name"):
            models.update_chat_table_info(user["id"], chat_id, base_token, table_id, base_url, chat_name)

        has_cache_now = local_cache.has_cache(chat_id, chat_name)
        result = {
            "ok": True,
            "new_count": len(messages),
            "attach_count": attach_count,
            "skipped_count": skipped_count,
            "doc_count": len(doc_map),
            "total_records": new_record_count,
            "base_url": base_url,
            "local_cache": is_local_cache_enabled,
            "has_cache": has_cache_now,
        }
        cache_suffix = "（已保存至本地 Markdown 缓存）" if is_local_cache_enabled else ""
        _set_progress(chat_id, stage="done", running=False,
                      current=total, total=total,
                      message=f"同步完成：新增 {len(messages)} 条消息" +
                              (f"，附件 {attach_count} 个" if attach_count > 0 else "") +
                              (f"，跳过大附件 {skipped_count} 个" if skipped_count > 0 else "") +
                              (f"，云文档快照 {len(doc_map)} 篇" if doc_map else "") +
                              cache_suffix,
                      result=result)

    except Exception as e:
        print(f"[SYNC ERROR] {traceback.format_exc()}")
        err_str = str(e)
        # token 失效友好提示：refresh_token 一次性被用掉 / 已过期
        if "20073" in err_str or "invalid_grant" in err_str:
            friendly = "登录已失效，请重新登录后再同步"
        elif "Token 已过期且无法刷新" in err_str:
            friendly = "登录已过期，请重新登录后再同步"
        else:
            friendly = f"同步失败: {e}"
        _set_progress(chat_id, stage="error", running=False, error=friendly,
                      message=friendly)


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

VIEW_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ chat_name }} - 本地 Markdown 预览</title>
    <!-- 优先加载本地托管的 Marked 引擎（无需外网，毫秒级响应且 100% 稳定可靠） -->
    <script src="/static/marked.min.js?v=15.0.12"></script>
    <script>
        // 本地静态资源降级备选（国内 BootCDN）
        if (typeof marked === 'undefined') {
            document.write('<scr' + 'ipt src="https://cdn.bootcdn.net/ajax/libs/marked/15.0.12/marked.min.js"></scr' + 'ipt>');
        }
    </script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', sans-serif; background: #f5f6f8; color: #1f2329; }
        .top-nav { background: white; border-bottom: 1px solid #e5e6eb; padding: 12px 32px; display: flex; justify-content: space-between; align-items: center; position: sticky; top: 0; z-index: 50; box-shadow: 0 1px 4px rgba(0,0,0,0.03); }
        .nav-left { display: flex; align-items: center; gap: 14px; }
        .btn-back { display: inline-flex; align-items: center; gap: 6px; color: #4e5969; text-decoration: none; font-size: 13px; padding: 6px 12px; border-radius: 6px; background: #f2f3f5; font-weight: 500; transition: all 0.2s; }
        .btn-back:hover { background: #e5e6eb; color: #1f2329; }
        .chat-title { font-size: 16px; font-weight: 600; color: #1f2329; }
        .nav-right { display: flex; align-items: center; gap: 10px; }
        .nav-btn { display: inline-flex; align-items: center; gap: 6px; padding: 7px 15px; border-radius: 6px; font-size: 13px; font-weight: 500; text-decoration: none; cursor: pointer; transition: all 0.2s; border: none; }
        .btn-raw { background: #f2f3f5; color: #1f2329; }
        .btn-raw:hover { background: #e5e6eb; }
        .btn-zip { background: #3370ff; color: white; }
        .btn-zip:hover { background: #2860e1; }
        .container { max-width: 920px; margin: 24px auto; padding: 0 20px; }
        .meta-card { background: white; border-radius: 12px; padding: 16px 22px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
        .meta-items { display: flex; gap: 18px; font-size: 13px; color: #646a73; flex-wrap: wrap; }
        .meta-item { display: flex; align-items: center; gap: 6px; }
        .meta-item b { color: #1f2329; font-weight: 600; }
        .md-card { background: white; border-radius: 12px; padding: 36px 40px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); line-height: 1.75; font-size: 14.5px; }
        
        /* Markdown 样式 */
        .md-card h1 { font-size: 22px; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid #e5e6eb; color: #1f2329; }
        .md-card h2 { font-size: 18px; margin: 24px 0 14px; color: #1f2329; }
        .md-card h3 { font-size: 15px; margin: 18px 0 10px; color: #1f2329; }
        .md-card hr { border: none; height: 1px; background: #e5e6eb; margin: 24px 0; }
        .md-card p { margin-bottom: 12px; word-break: break-word; }
        .md-card blockquote { border-left: 4px solid #3370ff; padding: 10px 16px; background: #f7f9fd; color: #4e5969; border-radius: 0 6px 6px 0; margin-bottom: 14px; }
        .md-card code { font-family: 'SF Mono', Consolas, Monaco, monospace; background: #f2f3f5; padding: 2px 6px; border-radius: 4px; font-size: 12.5px; color: #1f2329; }
        .md-card pre { background: #1e1e1e; color: #d4d4d4; padding: 14px 18px; border-radius: 8px; overflow-x: auto; margin: 14px 0; font-family: 'SF Mono', Consolas, Monaco, monospace; font-size: 13px; line-height: 1.5; }
        .md-card pre code { background: transparent; color: inherit; padding: 0; }
        .md-card img { max-width: 100%; max-height: 480px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.08); margin: 10px 0; cursor: zoom-in; transition: transform 0.2s, box-shadow 0.2s; display: inline-block; vertical-align: middle; }
        .md-card img:hover { transform: scale(1.01); box-shadow: 0 4px 16px rgba(0,0,0,0.12); }
        .md-card a { color: #3370ff; text-decoration: none; word-break: break-all; }
        .md-card a:hover { text-decoration: underline; }
        
        /* 附件下载卡片样式 */
        .attachment-card { display: inline-flex; align-items: center; gap: 10px; background: #f7f8fa; border: 1px solid #dee0e3; border-radius: 8px; padding: 10px 16px; text-decoration: none !important; color: #1f2329 !important; font-size: 13.5px; margin: 8px 0; font-weight: 500; transition: all 0.2s; box-shadow: 0 1px 2px rgba(0,0,0,0.02); }
        .attachment-card:hover { background: #e8f3ff; border-color: #3370ff; color: #3370ff !important; transform: translateY(-1px); box-shadow: 0 3px 8px rgba(51,112,255,0.12); }
        .attachment-card .icon { font-size: 18px; line-height: 1; }
        .attachment-card .action-tag { font-size: 12px; color: #3370ff; background: #e8f3ff; padding: 2px 8px; border-radius: 4px; margin-left: 6px; }
        
        /* Lightbox 灯箱大图预览 */
        .lightbox-modal { position: fixed; inset: 0; background: rgba(0,0,0,0.85); display: none; justify-content: center; align-items: center; z-index: 1000; padding: 24px; backdrop-filter: blur(4px); }
        .lightbox-modal.show { display: flex; }
        .lightbox-modal img { max-width: 92vw; max-height: 90vh; border-radius: 8px; box-shadow: 0 4px 24px rgba(0,0,0,0.5); object-fit: contain; }
        .lightbox-actions { position: fixed; top: 20px; right: 24px; display: flex; gap: 12px; z-index: 1001; }
        .lightbox-btn { background: rgba(255,255,255,0.2); color: white; border: none; border-radius: 6px; padding: 8px 16px; font-size: 13px; cursor: pointer; text-decoration: none; transition: background 0.2s; backdrop-filter: blur(8px); }
        .lightbox-btn:hover { background: rgba(255,255,255,0.35); }
        .empty-tip { text-align: center; padding: 60px; color: #86909c; }
    </style>
</head>
<body>
    <div class="top-nav">
        <div class="nav-left">
            <a href="/" class="btn-back">← 返回控制台</a>
            <span class="chat-title">{{ chat_name }}</span>
        </div>
        <div class="nav-right">
            <a href="/cache/{{ chat_id }}/raw" class="nav-btn btn-raw" target="_blank" title="在新窗口查看/下载原始 Markdown">📄 下载 Markdown</a>
            <a href="/cache/{{ chat_id }}/download" class="nav-btn btn-zip" title="打包下载包含 Markdown 和附件的完整压缩包">📦 打包下载 (ZIP)</a>
        </div>
    </div>

    <div class="container">
        {% if has_cache %}
        <div class="meta-card">
            <div class="meta-items">
                <div class="meta-item"><span>群聊 ID:</span> <code>{{ chat_id }}</code></div>
                {% if info.updated_at %}<div class="meta-item"><span>更新时间:</span> <b>{{ info.updated_at }}</b></div>{% endif %}
                {% if info.asset_count is defined %}<div class="meta-item"><span>已缓存附件:</span> <b>{{ info.asset_count }} 个</b></div>{% endif %}
                {% if info.total_size %}<div class="meta-item"><span>总大小:</span> <b>{{ (info.total_size / 1024 / 1024)|round(2) if info.total_size > 1048576 else (info.total_size / 1024)|round(1) }} {{ 'MB' if info.total_size > 1048576 else 'KB' }}</b></div>{% endif %}
            </div>
        </div>
        <div class="md-card" id="mdContent">
            <!-- Markdown 渲染目标容器 -->
        </div>
        {% else %}
        <div class="md-card empty-tip">
            <h2>暂无本地缓存数据</h2>
            <p style="margin-top:12px; font-size:14px; color:#646a73;">该群聊尚未开启本地缓存或尚未进行同步。</p>
            <div style="margin-top:24px;"><a href="/" class="nav-btn btn-zip">返回开启并同步</a></div>
        </div>
        {% endif %}
    </div>

    <!-- Lightbox 图片灯箱模态框 -->
    <div class="lightbox-modal" id="lightboxModal" onclick="closeLightbox()">
        <div class="lightbox-actions" onclick="event.stopPropagation()">
            <a href="#" id="lightboxExtBtn" target="_blank" class="lightbox-btn">在新标签页打开</a>
            <button class="lightbox-btn" onclick="closeLightbox()">关闭 (Esc)</button>
        </div>
        <img id="lightboxImg" src="" alt="大图预览" onclick="event.stopPropagation()">
    </div>

    <script>
        function renderContent() {
            const rawMarkdown = {{ raw_markdown | tojson }};
            const chatId = {{ chat_id | tojson }};
            const mdContainer = document.getElementById('mdContent');
            if (!mdContainer) return;

            if (!rawMarkdown) {
                return;
            }

            if (typeof marked !== 'undefined') {
                try {
                    const renderer = new marked.Renderer();

                    // 自定义图片渲染：相对路径转换为 /cache/<chat_id>/assets/，点击打开灯箱
                    renderer.image = function(token) {
                        var href = (typeof token === 'object' && token) ? (token.href || '') : String(token || '');
                        var text = (typeof token === 'object' && token) ? (token.text || '图片') : (arguments[2] || '图片');
                        var src = href;
                        if (src.startsWith('./assets/')) {
                            src = '/cache/' + encodeURIComponent(chatId) + '/' + src.substring(2);
                        } else if (src.startsWith('assets/')) {
                            src = '/cache/' + encodeURIComponent(chatId) + '/' + src;
                        }
                        return '<img src="' + src + '" alt="' + text + '" onclick="openLightbox(this.src)" title="点击放大查看大图" loading="lazy">';
                    };

                    // 自定义链接渲染：检测附件链接，美化为可直接下载的卡片
                    renderer.link = function(token) {
                        var href = (typeof token === 'object' && token) ? (token.href || '') : String(token || '');
                        var text = (typeof token === 'object' && token) ? (token.text || href) : (arguments[2] || href);
                        var url = href;
                        if (url.startsWith('./assets/')) {
                            url = '/cache/' + encodeURIComponent(chatId) + '/' + url.substring(2);
                            return '<a href="' + url + '?download=1" download class="attachment-card" title="点击直接下载文件"><span class="icon">📎</span><span>' + text + '</span><span class="action-tag">点击下载</span></a>';
                        } else if (url.startsWith('assets/')) {
                            url = '/cache/' + encodeURIComponent(chatId) + '/' + url;
                            return '<a href="' + url + '?download=1" download class="attachment-card" title="点击直接下载文件"><span class="icon">📎</span><span>' + text + '</span><span class="action-tag">点击下载</span></a>';
                        }
                        return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + text + '</a>';
                    };

                    if (typeof marked.setOptions === 'function') {
                        marked.setOptions({
                            renderer: renderer,
                            breaks: true,
                            gfm: true
                        });
                    }

                    var parseFn = typeof marked.parse === 'function' ? marked.parse : marked;
                    mdContainer.innerHTML = parseFn(rawMarkdown);
                    return;
                } catch (err) {
                    console.error('Marked render failed, falling back to text:', err);
                }
            }

            // 降级兜底渲染：格式化纯文本
            renderFallback(rawMarkdown, mdContainer);
        }

        function renderFallback(rawText, container) {
            var warningBanner = '<div style="background:#fffbe6; border:1px solid #ffe58f; padding:10px 16px; border-radius:8px; margin-bottom:18px; font-size:13px; color:#d48806;">⚠️ Markdown 渲染脚本暂未就绪，已切换为结构化纯文本预览模式。</div>';
            var escaped = rawText
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;');
            container.innerHTML = warningBanner + '<pre style="white-space: pre-wrap; word-break: break-word; font-family: inherit; font-size: 14.5px; line-height: 1.75; background: transparent; color: inherit; padding: 0;">' + escaped + '</pre>';
        }

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', renderContent);
        } else {
            renderContent();
        }

        function openLightbox(src) {
            const modal = document.getElementById('lightboxModal');
            const img = document.getElementById('lightboxImg');
            const extBtn = document.getElementById('lightboxExtBtn');
            img.src = src;
            extBtn.href = src;
            modal.classList.add('show');
            document.body.style.overflow = 'hidden';
        }

        function closeLightbox() {
            const modal = document.getElementById('lightboxModal');
            modal.classList.remove('show');
            document.body.style.overflow = '';
        }

        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') closeLightbox();
        });
    </script>
</body>
</html>
"""

INDEX_PAGE = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <title>飞书群消息归档</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', sans-serif; background: #f5f6f8; color: #1f2329; }
        .header { background: white; border-bottom: 1px solid #e5e6eb; padding: 14px 32px; display: flex; justify-content: space-between; align-items: center; position: sticky; top: 0; z-index: 10; }
        .header h1 { font-size: 17px; font-weight: 600; color: #1f2329; }
        .header .user { font-size: 14px; color: #646a73; display: flex; align-items: center; gap: 12px; }
        .header .user .avatar { width: 28px; height: 28px; border-radius: 50%; background: #3370ff; color: white; display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 500; }
        .header .user a { color: #646a73; text-decoration: none; font-size: 13px; }
        .header .user a:hover { color: #f53f3f; }
        .container { max-width: 960px; margin: 28px auto; padding: 0 20px; }
        .section-title { font-size: 13px; color: #86909c; font-weight: 500; margin-bottom: 12px; padding-left: 4px; letter-spacing: 0.3px; }
        .add-chat { background: white; border-radius: 12px; padding: 20px 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
        .add-chat h2 { font-size: 15px; margin-bottom: 14px; font-weight: 600; }
        .add-chat .input-row { display: flex; gap: 8px; }
        .add-chat input { flex: 1; padding: 10px 14px; border: 1px solid #dee0e3; border-radius: 8px; font-size: 14px; outline: none; transition: border-color 0.2s; }
        .add-chat input:focus { border-color: #3370ff; }
        .add-chat button { padding: 10px 22px; background: #3370ff; color: white; border: none; border-radius: 8px; font-size: 14px; cursor: pointer; transition: background 0.2s; }
        .add-chat button:hover { background: #2860e1; }
        .chat-list { display: flex; flex-direction: column; gap: 12px; }
        .chat-item { background: white; border-radius: 12px; padding: 18px 22px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; transition: box-shadow 0.2s; border-left: 4px solid transparent; }
        .chat-item:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.06); }
        .chat-item.status-pending { border-left-color: #ff7d00; }
        .chat-item.status-synced { border-left-color: #00b42a; }
        .chat-item.status-syncing { border-left-color: #3370ff; }
        .chat-item.status-error { border-left-color: #f53f3f; }
        .chat-info { flex: 1; min-width: 0; }
        .chat-info .name { font-size: 15px; font-weight: 600; color: #1f2329; margin-bottom: 6px; }
        .chat-info .meta { font-size: 12px; color: #86909c; margin-bottom: 8px; display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
        .chat-info .meta .id { font-family: 'SF Mono', Consolas, monospace; background: #f2f3f5; padding: 2px 6px; border-radius: 4px; font-size: 11px; }
        .chat-info .meta .feishu-tag { background: #e8f3ff; color: #3370ff; padding: 2px 8px; border-radius: 4px; font-size: 11px; max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .chat-info .meta a { color: #3370ff; text-decoration: none; font-size: 12px; }
        .chat-info .meta a:hover { text-decoration: underline; }
        .chat-info .meta .dot { color: #c9cdd4; }
        .stats-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 8px; }
        .stats { font-size: 12px; color: #86909c; }
        .badge { display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 12px; font-size: 12px; font-weight: 500; }
        .badge-pending { background: #fff7e8; color: #ff7d00; }
        .badge-synced { background: #e8ffea; color: #00b42a; }
        .badge-syncing { background: #e8f3ff; color: #3370ff; }
        .badge-error { background: #ffece8; color: #f53f3f; }
        .badge .dot-icon { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
        .badge-syncing .dot-icon { animation: pulse 1.2s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
        .chat-actions { display: flex; gap: 8px; align-items: center; flex-shrink: 0; }
        .btn-sync { padding: 8px 18px; background: #00b42a; color: white; border: none; border-radius: 8px; font-size: 13px; cursor: pointer; transition: all 0.2s; font-weight: 500; }
        .btn-sync:hover { background: #009a25; }
        .btn-sync:disabled { background: #c9cdd4; cursor: not-allowed; }
        .btn-sync.ready-pending { background: #ff7d00; }
        .btn-sync.ready-pending:hover { background: #e66e00; }
        .btn-delete { padding: 8px 14px; background: white; color: #f53f3f; border: 1px solid #ffd6d0; border-radius: 8px; font-size: 13px; cursor: pointer; transition: all 0.2s; }
        .btn-delete:hover { background: #ffece8; }
        .empty { text-align: center; padding: 56px; color: #86909c; background: white; border-radius: 12px; }
        .toast { position: fixed; top: 20px; right: 20px; padding: 12px 22px; border-radius: 8px; color: white; font-size: 14px; z-index: 999; opacity: 0; transition: opacity 0.3s; pointer-events: none; max-width: 400px; }
        .toast.success { background: #00b42a; }
        .toast.error { background: #f53f3f; }
        .toast.warn { background: #ff7d00; }
        .toast.show { opacity: 1; pointer-events: auto; }
        .modal-mask { position: fixed; inset: 0; background: rgba(0,0,0,0.4); display: none; justify-content: center; align-items: center; z-index: 1000; pointer-events: none; }
        .modal-mask.show { display: flex; pointer-events: auto; }
        .modal { background: white; border-radius: 12px; padding: 28px 32px; max-width: 440px; width: 90%; box-shadow: 0 4px 20px rgba(0,0,0,0.12); }
        .modal h3 { font-size: 17px; margin-bottom: 8px; }
        .modal p { font-size: 13px; color: #646a73; margin-bottom: 20px; line-height: 1.6; }
        .modal-btns { display: flex; gap: 8px; justify-content: flex-end; }
        .modal-btns button { padding: 8px 18px; border: none; border-radius: 8px; font-size: 13px; cursor: pointer; transition: background 0.2s; }
        .modal-btns .btn-cancel { background: #f2f3f5; color: #4e5969; }
        .modal-btns .btn-cancel:hover { background: #e5e6eb; }
        .modal-btns .btn-only-config { background: #3370ff; color: white; }
        .modal-btns .btn-only-config:hover { background: #2860e1; }
        .modal-btns .btn-delete-all { background: #f53f3f; color: white; }
        .modal-btns .btn-delete-all:hover { background: #d93636; }
        .delete-cache-box { margin: 16px 0 20px; padding: 12px 14px; background: #f7f8fa; border: 1px solid #dee0e3; border-radius: 8px; text-align: left; }
        .delete-cache-label { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #1f2329; cursor: pointer; user-select: none; }
        .delete-cache-label input[type="checkbox"] { width: 16px; height: 16px; cursor: pointer; accent-color: #f53f3f; }
        .delete-cache-label.disabled { color: #8f959e; cursor: not-allowed; }
        .delete-cache-label.disabled input[type="checkbox"] { cursor: not-allowed; }
        .delete-cache-tip { font-size: 12px; color: #86909c; margin-top: 5px; padding-left: 24px; line-height: 1.4; }
        /* 同步进度条 */
        .sync-progress { margin-top: 10px; display: none; }
        .sync-progress .stage { font-size: 12px; color: #4e5969; margin-bottom: 6px; line-height: 1.4; }
        .sync-progress .bar-wrap { width: 100%; height: 6px; background: #f2f3f5; border-radius: 3px; overflow: hidden; }
        .sync-progress .bar { height: 100%; background: linear-gradient(90deg, #3370ff, #00b42a); border-radius: 3px; width: 0%; transition: width 0.3s; }
        .sync-progress.error .bar { background: #f53f3f; }
        .sync-progress.done .bar { background: #00b42a; }
        /* 本地缓存增强样式 */
        .chat-item.is-clickable { cursor: pointer; }
        .chat-item.is-clickable:hover { border-color: #b3ccff; box-shadow: 0 4px 16px rgba(51,112,255,0.08); }
        .name-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
        .preview-tag { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 4px; font-size: 11px; background: #e8f3ff; color: #3370ff; font-weight: 500; text-decoration: none; cursor: pointer; transition: all 0.2s; }
        .preview-tag:hover { background: #3370ff; color: white; }
        .link-cache { color: #3370ff; text-decoration: none; font-size: 12px; font-weight: 500; }
        .link-cache:hover { text-decoration: underline; }
        .cache-toggle-wrap { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: #4e5969; cursor: pointer; user-select: none; background: #f2f3f5; padding: 3px 9px; border-radius: 6px; transition: background 0.2s; }
        .cache-toggle-wrap:hover { background: #e5e6eb; }
        .cache-toggle-wrap input { cursor: pointer; margin: 0; }
        .checkbox-row { margin-top: 12px; display: flex; align-items: center; gap: 8px; font-size: 13px; color: #4e5969; }
        .checkbox-row input { cursor: pointer; width: 15px; height: 15px; }
        /* 列表头部与搜索框 */
        .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; flex-wrap: wrap; gap: 12px; }
        .section-title { font-size: 16px; font-weight: 600; color: #1f2329; }
        .chat-count { font-size: 13px; color: #8f959e; font-weight: normal; margin-left: 6px; }
        .section-actions { display: flex; align-items: center; gap: 12px; }
        .btn-sync-all { display: inline-flex; align-items: center; gap: 6px; padding: 7px 15px; background: #00b42a; color: white; border: none; border-radius: 8px; font-size: 13px; font-weight: 500; cursor: pointer; transition: all 0.2s; box-shadow: 0 1px 3px rgba(0, 180, 42, 0.2); }
        .btn-sync-all:hover { background: #009a25; }
        .btn-sync-all:disabled { background: #c9cdd4; cursor: not-allowed; box-shadow: none; }
        .btn-sync-all.syncing .sync-icon { display: inline-block; animation: spin 0.8s linear infinite; }
        .search-box { position: relative; }
        .search-box input { padding: 7px 14px 7px 32px; border: 1px solid #dee0e3; border-radius: 8px; font-size: 13px; outline: none; width: 220px; transition: all 0.2s; background: white; }
        .search-box input:focus { border-color: #3370ff; box-shadow: 0 0 0 2px rgba(51,112,255,0.15); width: 250px; }
        .search-icon { position: absolute; left: 10px; top: 50%; transform: translateY(-50%); font-size: 13px; color: #8f959e; pointer-events: none; }
        /* 预获取群名称提示与动画 */
        .name-input-wrapper { position: relative; flex: 1; min-width: 160px; display: flex; align-items: center; }
        .name-input-wrapper input { width: 100%; padding-right: 32px; }
        .input-spinner { position: absolute; right: 12px; width: 14px; height: 14px; border: 2px solid #dee0e3; border-top-color: #3370ff; border-radius: 50%; animation: spin 0.8s linear infinite; pointer-events: none; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        .name-fetch-tip { font-size: 12px; margin-top: 6px; padding-left: 2px; line-height: 1.4; }
        .name-fetch-tip.info { color: #00b42a; }
        .name-fetch-tip.warn { color: #ff7d00; }
        .name-fetch-tip.custom { color: #86909c; }

        /* 群聊输入与下拉选择组件样式 */
        .chat-input-wrapper { position: relative; flex: 1.3; min-width: 260px; }
        .chat-input-wrapper input { width: 100%; padding-right: 50px; }
        .chat-input-actions { position: absolute; right: 8px; top: 50%; transform: translateY(-50%); display: flex; align-items: center; gap: 4px; }
        .chat-input-btn { cursor: pointer; color: #8f959e; font-size: 12px; width: 20px; height: 20px; display: inline-flex; align-items: center; justify-content: center; border-radius: 50%; transition: all 0.2s; user-select: none; }
        .chat-input-btn:hover { background: #e5e6eb; color: #1f2329; }
        .chat-dropdown-arrow { font-size: 13px; transition: transform 0.2s; }
        .chat-dropdown-arrow.open { transform: rotate(180deg); }

        /* 下拉面板 */
        .chat-dropdown-panel { position: absolute; top: calc(100% + 6px); left: 0; right: 0; background: white; border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,0.12); border: 1px solid #dee0e3; z-index: 100; overflow: hidden; display: none; flex-direction: column; max-height: 380px; }
        .chat-dropdown-panel.show { display: flex; }
        
        .dropdown-header { display: flex; justify-content: space-between; align-items: center; padding: 10px 14px; background: #f7f8fa; border-bottom: 1px solid #ebeef5; font-size: 12px; color: #646a73; }
        .dropdown-header .dropdown-tip { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .btn-sync-cache { display: inline-flex; align-items: center; gap: 4px; padding: 4px 10px; background: white; border: 1px solid #dee0e3; border-radius: 6px; font-size: 12px; color: #3370ff; cursor: pointer; font-weight: 500; transition: all 0.2s; flex-shrink: 0; }
        .btn-sync-cache:hover { background: #e8f3ff; border-color: #3370ff; }
        .btn-sync-cache.syncing { pointer-events: none; opacity: 0.7; }

        .dropdown-list { overflow-y: auto; max-height: 280px; padding: 4px 0; overscroll-behavior: contain; }
        .dropdown-item { display: flex; align-items: center; gap: 10px; padding: 9px 14px; cursor: pointer; transition: background 0.15s; border-bottom: 1px solid #f9fafb; text-align: left; }
        .dropdown-item:last-child { border-bottom: none; }
        .dropdown-item:hover, .dropdown-item.active { background: #f2f6ff; }
        .item-avatar { width: 32px; height: 32px; border-radius: 50%; object-fit: cover; flex-shrink: 0; background: #e8f3ff; color: #3370ff; display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 600; }
        .item-content { flex: 1; min-width: 0; }
        .item-title-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
        .item-name { font-size: 13.5px; font-weight: 500; color: #1f2329; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .item-name mark { background: #ffe066; color: inherit; padding: 0 1px; border-radius: 2px; }
        .item-tag-added { font-size: 11px; color: #86909c; background: #f2f3f5; padding: 2px 6px; border-radius: 4px; flex-shrink: 0; font-weight: normal; }
        .item-tag-dissolved { font-size: 11px; color: #d46b08; background: #fff7e6; border: 1px solid #ffd591; padding: 1px 5px; border-radius: 4px; flex-shrink: 0; font-weight: normal; }
        .item-sub-row { display: flex; align-items: center; gap: 8px; margin-top: 2px; font-size: 11.5px; color: #8f959e; }
        .item-id { font-family: 'SF Mono', Consolas, monospace; background: #f7f8fa; padding: 1px 4px; border-radius: 3px; font-size: 11px; }
        .item-id mark { background: #ffe066; color: inherit; padding: 0 1px; border-radius: 2px; }

        /* 搜索无结果 / 空状态 */
        .dropdown-empty { padding: 24px 16px; text-align: center; color: #646a73; }
        .dropdown-empty-icon { font-size: 26px; margin-bottom: 8px; }
        .dropdown-empty-text { font-size: 13px; color: #1f2329; font-weight: 600; margin-bottom: 4px; }
        .dropdown-empty-desc { font-size: 12px; color: #8f959e; margin-bottom: 14px; line-height: 1.5; padding: 0 10px; }
        .btn-empty-sync { display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 8px 18px; background: #3370ff; color: white; border: none; border-radius: 6px; font-size: 13px; font-weight: 500; cursor: pointer; transition: all 0.2s; box-shadow: 0 2px 6px rgba(51,112,255,0.25); }
        .btn-empty-sync:hover { background: #2860e1; }
        .btn-empty-sync:disabled { background: #c9cdd4; cursor: not-allowed; }
        .sync-spin { display: inline-block; animation: spin 0.8s linear infinite; }
    </style>
</head>
<body>
    <div class="header">
        <h1>飞书群消息归档</h1>
        <div class="user">
            <div class="avatar">{{ user.name[:1] if user.name else 'U' }}</div>
            <span>{{ user.name }}</span>
            <a href="/auth/logout">退出</a>
        </div>
    </div>
    <div class="container">
        <div class="add-chat">
            <h2>添加群聊</h2>
            <div class="input-row">
                <div class="chat-input-wrapper" id="chatInputWrapper">
                    <input type="text" id="chatIdInput" placeholder="选择或搜索群聊名称 / 关键词 / 群聊 ID" autocomplete="off" />
                    <div class="chat-input-actions">
                        <span id="chatInputClear" class="chat-input-btn" title="清空输入" style="display:none;" onclick="clearChatInput(event)">✕</span>
                        <span id="chatDropdownArrow" class="chat-input-btn chat-dropdown-arrow" title="展开/收起群聊列表" onclick="toggleDropdown(event)">▾</span>
                    </div>
                    <div class="chat-dropdown-panel" id="chatDropdownPanel">
                        <div class="dropdown-header">
                            <span id="dropdownCountTip" class="dropdown-tip">读取群聊缓存中...</span>
                            <button type="button" class="btn-sync-cache" id="btnSyncCache" onclick="syncUserChats(event)" title="从飞书全量拉取已加入的群聊并更新缓存">
                                <span class="sync-icon">🔄</span> <span class="sync-text">同步群聊缓存</span>
                            </button>
                        </div>
                        <div class="dropdown-list" id="dropdownList"></div>
                        <div class="dropdown-empty" id="dropdownEmpty" style="display:none;">
                            <div class="dropdown-empty-icon">🔍</div>
                            <div class="dropdown-empty-text" id="dropdownEmptyTitle">未找到匹配的群聊</div>
                            <div class="dropdown-empty-desc" id="dropdownEmptyDesc">若群聊刚创建或刚加入，请点击下方同步；也可直接输入群聊 ID (oc_xxx) 添加</div>
                            <button type="button" class="btn-empty-sync" id="btnEmptySync" onclick="syncUserChats(event)">
                                <span class="sync-icon">🔄</span> <span class="sync-text">立即同步飞书所有群聊缓存</span>
                            </button>
                        </div>
                    </div>
                </div>
                <div class="name-input-wrapper">
                    <input type="text" id="chatNameInput" placeholder="自定义名称（可选，留空用真实群名）" />
                    <span id="nameFetchSpinner" class="input-spinner" style="display:none;"></span>
                </div>
                <button onclick="addChat()">添加</button>
            </div>
            <div id="nameFetchTip" class="name-fetch-tip" style="display:none;"></div>
            <div class="checkbox-row">
                <label style="display:flex; align-items:center; gap:6px; cursor:pointer;">
                    <input type="checkbox" id="localCacheCheckbox" checked>
                    <span>开启本地缓存（同步时自动将文字存为 Markdown 并下载图片和附件）</span>
                </label>
            </div>
        </div>
        <div class="section-header">
            <div class="section-title">
                已配置群聊
                {% if chats %}<span class="chat-count">({{ chats|length }})</span>{% endif %}
            </div>
            {% if chats %}
            <div class="section-actions">
                <button type="button" class="btn-sync-all" id="btnSyncAll" onclick="syncAllChats()" title="一键启动所有已配置群聊的消息同步">
                    <span class="sync-icon">⚡</span> <span class="sync-text">全部开始同步</span>
                </button>
                <div class="search-box">
                    <span class="search-icon">🔍</span>
                    <input type="text" id="chatSearchInput" placeholder="搜索群聊名称或 ID..." oninput="filterChats()" />
                </div>
            </div>
            {% endif %}
        </div>
        <div class="chat-list" id="chatList">
            {% if chats %}
                {% for chat in chats %}
                <div class="chat-item {% if chat.has_cache %}is-clickable{% endif %}" id="chat-{{ chat.chat_id }}" data-chat-id="{{ chat.chat_id }}">
                    <div class="chat-info" {% if chat.has_cache %}onclick="openCacheView('{{ chat.chat_id }}', event)" title="点击进入 Markdown 预览"{% endif %}>
                        <div class="name-row">
                            <div class="name">{{ chat.chat_name or chat.chat_id }}</div>
                            {% if chat.has_cache %}
                            <span class="preview-tag" onclick="openCacheView('{{ chat.chat_id }}', event)" title="点击进入 Markdown 预览">📑 预览归档</span>
                            {% endif %}
                        </div>
                        <div class="meta" id="meta-{{ chat.chat_id }}">
                            {% if chat.feishu_name and chat.feishu_name != chat.chat_name %}<span class="feishu-tag" title="飞书真实群名">{{ chat.feishu_name }}</span>{% endif %}
                            <span class="id">{{ chat.chat_id }}</span>
                            {% if chat.record_count %}<span class="dot">·</span><span>已同步 <span class="record-count">{{ chat.record_count }}</span> 条</span>{% endif %}
                            {% if chat.base_url %}<span class="dot">·</span><a href="{{ chat.base_url }}" target="_blank" onclick="event.stopPropagation()">查看表格</a>{% endif %}
                            {% if chat.has_cache %}
                            <span class="dot">·</span><a href="/cache/{{ chat.chat_id }}/view" target="_blank" onclick="event.stopPropagation()" class="link-cache">在线预览</a>
                            <span class="dot">·</span><a href="/cache/{{ chat.chat_id }}/download" onclick="event.stopPropagation()" class="link-cache">下载ZIP</a>
                            {% endif %}
                        </div>
                        <div class="stats-row">
                            <div class="stats" data-chat-id="{{ chat.chat_id }}">查询中...</div>
                            <span class="badge badge-syncing" id="badge-{{ chat.chat_id }}" style="display:none;"><span class="dot-icon"></span><span class="badge-text">同步中</span></span>
                            <label class="cache-toggle-wrap" onclick="event.stopPropagation()" title="开启后，同步时自动保存 Markdown 记录并下载图片和附件">
                                <input type="checkbox" id="toggle-{{ chat.chat_id }}" onchange="toggleLocalCache('{{ chat.chat_id }}', this.checked)" {% if chat.local_cache %}checked{% endif %}>
                                <span>本地缓存</span>
                            </label>
                        </div>
                        <div class="sync-progress" id="progress-{{ chat.chat_id }}">
                            <div class="stage">准备中...</div>
                            <div class="bar-wrap"><div class="bar"></div></div>
                        </div>
                    </div>
                    <div class="chat-actions" onclick="event.stopPropagation()">
                        <button class="btn-sync" onclick="syncChat('{{ chat.chat_id }}', this)">同步</button>
                        <button class="btn-delete" onclick="deleteChat('{{ chat.chat_id }}', {{ 'true' if chat.has_cache else 'false' }})">删除</button>
                    </div>
                </div>
                {% endfor %}
            {% else %}
                <div class="empty">还没有添加群聊，请在上方输入群聊 ID</div>
            {% endif %}
        </div>
    </div>
    <div class="toast" id="toast"></div>
    <div class="modal-mask" id="deleteModal">
        <div class="modal">
            <h3>删除群聊配置</h3>
            <p>请选择删除方式：<br>「仅删配置」：飞书多维表格中的数据会保留。<br>「同时删表格」：将一并删除已同步的飞书多维表格，此操作不可恢复。</p>
            <div class="delete-cache-box">
                <label id="deleteCacheLabel" class="delete-cache-label">
                    <input type="checkbox" id="deleteCacheCheck">
                    <span>同时删除本地缓存文件（Markdown 与附件）</span>
                </label>
                <div class="delete-cache-tip" id="deleteCacheTip"></div>
            </div>
            <div class="modal-btns">
                <button class="btn-cancel" onclick="closeDeleteModal()">取消</button>
                <button class="btn-only-config" onclick="doDelete(false)">仅删配置</button>
                <button class="btn-delete-all" onclick="doDelete(true)">同时删表格</button>
            </div>
        </div>
    </div>
    <script>
        // 显示上次添加群聊返回的警告（如自动获取群名失败）
        window.addEventListener('load', function() {
            const w = sessionStorage.getItem('addChatWarning');
            if (w) {
                sessionStorage.removeItem('addChatWarning');
                showToast(w, 'warn');
            }
        });

        function showToast(msg, type) {
            const toast = document.getElementById('toast');
            toast.textContent = msg;
            toast.className = 'toast ' + type + ' show';
            setTimeout(() => toast.className = 'toast ' + type, 3000);
        }

        function openCacheView(chatId, e) {
            if (e) e.stopPropagation();
            window.location.href = '/cache/' + chatId + '/view';
        }

        function filterChats() {
            const input = document.getElementById('chatSearchInput');
            const query = (input ? input.value : '').trim().toLowerCase();
            const items = document.querySelectorAll('#chatList .chat-item');
            let visibleCount = 0;
            items.forEach(item => {
                const name = item.querySelector('.name')?.textContent.toLowerCase() || '';
                const feishu = item.querySelector('.feishu-tag')?.textContent.toLowerCase() || '';
                const id = item.getAttribute('data-chat-id')?.toLowerCase() || '';
                if (!query || name.includes(query) || feishu.includes(query) || id.includes(query)) {
                    item.style.display = '';
                    visibleCount++;
                } else {
                    item.style.display = 'none';
                }
            });
            let emptyTip = document.getElementById('searchNoResult');
            if (visibleCount === 0 && items.length > 0) {
                if (!emptyTip) {
                    emptyTip = document.createElement('div');
                    emptyTip.id = 'searchNoResult';
                    emptyTip.className = 'empty';
                    emptyTip.textContent = '未搜索到匹配的群聊';
                    document.getElementById('chatList').appendChild(emptyTip);
                }
                emptyTip.style.display = 'block';
            } else if (emptyTip) {
                emptyTip.style.display = 'none';
            }
        }

        async function toggleLocalCache(chatId, enabled) {
            try {
                const resp = await fetch('/api/chats/' + chatId + '/toggle_cache', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: enabled })
                });
                const data = await resp.json();
                if (data.ok) {
                    showToast(enabled ? '已开启该群本地缓存' : '已关闭该群本地缓存', 'success');
                } else {
                    showToast(data.error || '切换失败', 'error');
                }
            } catch (err) {
                showToast('网络请求异常', 'error');
            }
        }

        async function addChat() {
            const input = document.getElementById('chatIdInput');
            const nameInput = document.getElementById('chatNameInput');
            const localCacheBox = document.getElementById('localCacheCheckbox');
            const rawVal = input ? input.value.trim() : '';
            if (!rawVal) return;

            // 优先使用已选中的 chat_id，若无选中则使用输入文本（可能是群名或 oc_xxx）
            const chatIdToSend = (typeof selectedChatId !== 'undefined' && selectedChatId) ? selectedChatId : rawVal;
            const chatName = nameInput ? nameInput.value.trim() : '';
            const localCache = localCacheBox ? localCacheBox.checked : true;

            try {
                const resp = await fetch('/api/chats', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        chat_id: chatIdToSend,
                        chat_name: chatName || undefined,
                        local_cache: localCache
                    }),
                });
                const data = await resp.json();
                if (data.ok) {
                    if (data.warning) { sessionStorage.setItem('addChatWarning', data.warning); }
                    location.reload();
                } else {
                    showToast(data.error || '添加失败', 'error');
                }
            } catch (err) {
                showToast('网络请求异常', 'error');
            }
        }

        let pendingDeleteChatId = null;
        function deleteChat(chatId, hasCache) {
            pendingDeleteChatId = chatId;
            const check = document.getElementById('deleteCacheCheck');
            const label = document.getElementById('deleteCacheLabel');
            const tip = document.getElementById('deleteCacheTip');

            if (hasCache) {
                check.checked = true;
                check.disabled = false;
                label.classList.remove('disabled');
                label.title = '';
                tip.textContent = '检测到该群存在本地 Markdown 与附件缓存，默认同步清理。';
            } else {
                check.checked = false;
                check.disabled = true;
                label.classList.add('disabled');
                label.title = '该群聊暂无本地缓存文件';
                tip.textContent = '该群聊暂无本地缓存文件，无需清理。';
            }

            document.getElementById('deleteModal').classList.add('show');
        }

        function closeDeleteModal() {
            pendingDeleteChatId = null;
            document.getElementById('deleteModal').classList.remove('show');
        }

        async function doDelete(deleteBase) {
            const chatId = pendingDeleteChatId;
            if (!chatId) return;
            const deleteCache = document.getElementById('deleteCacheCheck').checked && !document.getElementById('deleteCacheCheck').disabled;
            const url = '/api/chats/' + chatId + '?delete_base=' + (deleteBase ? 'true' : 'false') + '&delete_cache=' + (deleteCache ? 'true' : 'false');
            try {
                const resp = await fetch(url, { method: 'DELETE' });
                const data = await resp.json();
                if (data.ok) { location.reload(); }
                else { showToast(data.error || '删除失败', 'error'); }
            } catch (e) {
                showToast('网络错误', 'error');
            } finally {
                closeDeleteModal();
            }
        }

        // 阶段中文映射
        const STAGE_LABEL = {
            starting: '准备开始',
            fetching_chat_info: '获取群信息',
            fetching_messages: '拉取群消息',
            messages_fetched: '消息拉取完成',
            creating_bitable: '创建多维表格',
            fetching_members: '获取群成员姓名',
            preparing_records: '准备记录数据',
            writing_records: '写入多维表格',
            records_written: '记录写入完成',
            uploading_attachments: '上传附件',
            saving_local_cache: '写入本地缓存',
            caching_docs: '缓存云文档',
            done: '同步完成',
            error: '同步失败',
            idle: '空闲'
        };

        function updateProgressUI(chatId, p) {
            const wrap = document.getElementById('progress-' + chatId);
            if (!wrap) return;
            const stageEl = wrap.querySelector('.stage');
            const barEl = wrap.querySelector('.bar');
            const stageText = STAGE_LABEL[p.stage] || p.stage;
            let percent = 0;
            if (p.total > 0) percent = Math.min(100, Math.round((p.current / p.total) * 100));
            else if (p.stage === 'done') percent = 100;
            stageEl.textContent = (p.message || stageText) + (p.total > 0 ? ' (' + p.current + '/' + p.total + ')' : '');
            barEl.style.width = percent + '%';
            wrap.classList.remove('error', 'done');
            if (p.stage === 'done') wrap.classList.add('done');
            if (p.stage === 'error') wrap.classList.add('error');
            wrap.style.display = 'block';
        }

        const syncTimers = {};
        function stopPolling(chatId) {
            if (syncTimers[chatId]) {
                clearInterval(syncTimers[chatId]);
                syncTimers[chatId] = null;
            }
        }
        function startPolling(chatId, btn) {
            stopPolling(chatId);
            syncTimers[chatId] = setInterval(async () => {
                try {
                    const resp = await fetch('/api/sync_status/' + chatId);
                    const p = await resp.json();
                    updateProgressUI(chatId, p);
                    if (!p.running) {
                        stopPolling(chatId);
                        if (btn) { btn.disabled = false; btn.textContent = '同步'; }
                        if (p.stage === 'done') {
                            const r = p.result || {};
                            const msg = '同步成功：新增 ' + (r.new_count || 0) + ' 条消息' +
                                (r.attach_count > 0 ? '，附件 ' + r.attach_count + ' 个' : '') +
                                (r.skipped_count > 0 ? '，跳过大附件 ' + r.skipped_count + ' 个' : '');
                            showToast(msg, 'success');
                            // 直接更新 meta 行，无需刷新页面
                            updateMetaAfterSync(chatId, r);
                            // 更新卡片状态为已同步满
                            updateChatStatus(chatId, 'synced', '已同步满');
                        } else if (p.stage === 'error') {
                            showToast(p.error || p.message || '同步失败', 'error');
                            updateChatStatus(chatId, 'error', '同步失败');
                        }
                    } else {
                        // 同步中：更新状态徽章
                        updateChatStatus(chatId, 'syncing', '同步中');
                    }
                } catch (e) {
                    // 网络错误不停止轮询，等下一轮重试
                }
            }, 800);
        }

        // 同步完成后直接更新 meta 行：已同步条数 + 飞书表格链接
        function updateMetaAfterSync(chatId, result) {
            const meta = document.getElementById('meta-' + chatId);
            if (!meta) return;
            // 更新已同步条数
            let countSpan = meta.querySelector('.record-count');
            if (countSpan) {
                countSpan.textContent = result.total_records || 0;
            } else if (result.total_records) {
                // 之前没有 record_count，追加（新格式：· 已同步 N 条）
                const cnt = document.createElement('span');
                cnt.innerHTML = '<span class="dot">·</span><span>已同步 <span class="record-count">' + result.total_records + '</span> 条</span>';
                meta.appendChild(cnt);
            }
            // 追加查看表格链接（如不存在）
            if (result.base_url && !meta.querySelector('a[href="' + result.base_url + '"]')) {
                const link = document.createElement('span');
                link.innerHTML = '<span class="dot">·</span><a href="' + result.base_url + '" target="_blank">查看表格</a>';
                meta.appendChild(link);
            }
            // 同时刷新 stats 里的待同步条数（待同步应变为 0）
            const statsEl = document.querySelector('.stats[data-chat-id="' + chatId + '"]');
            if (statsEl) {
                const total = result.total_records || 0;
                const m = statsEl.textContent.match(/(\d+)\s*\/\s*(\d+)/);
                const totalInStats = m ? parseInt(m[2]) : total;
                statsEl.textContent = '已同步 ' + total + ' / ' + totalInStats + ' 条';
            }

            // 若已有本地缓存，实时给卡片赋予预览交互及快捷链接
            if (result.has_cache) {
                const item = document.getElementById('chat-' + chatId);
                if (item) {
                    if (!item.classList.contains('is-clickable')) {
                        item.classList.add('is-clickable');
                        const infoEl = item.querySelector('.chat-info');
                        if (infoEl) {
                            infoEl.setAttribute('onclick', "openCacheView('" + chatId + "', event)");
                            infoEl.title = '点击进入 Markdown 预览';
                        }
                    }
                    const nameRow = item.querySelector('.name-row');
                    if (nameRow && !nameRow.querySelector('.preview-tag')) {
                        const tag = document.createElement('span');
                        tag.className = 'preview-tag';
                        tag.textContent = '📑 预览归档';
                        tag.title = '点击进入 Markdown 预览';
                        tag.onclick = (e) => openCacheView(chatId, e);
                        nameRow.appendChild(tag);
                    }
                    if (!meta.querySelector('.link-cache')) {
                        const cLink = document.createElement('span');
                        cLink.innerHTML = '<span class="dot">·</span><a href="/cache/' + chatId + '/view" target="_blank" class="link-cache" onclick="event.stopPropagation()">在线预览</a>' +
                                          '<span class="dot">·</span><a href="/cache/' + chatId + '/download" class="link-cache" onclick="event.stopPropagation()">下载ZIP</a>';
                        meta.appendChild(cLink);
                    }
                }
            }
        }

        async function syncChat(chatId, btn) {
            btn.disabled = true;
            btn.textContent = '同步中...';
            // 立即显示进度条占位 + 同步中状态
            updateProgressUI(chatId, { stage: 'starting', current: 0, total: 0, message: '准备开始同步...' });
            updateChatStatus(chatId, 'syncing', '同步中');
            try {
                const resp = await fetch('/api/sync/' + chatId, { method: 'POST' });
                const data = await resp.json();
                if (data.ok || data.started) {
                    startPolling(chatId, btn);
                } else if (data.error && data.error.indexOf('进行中') >= 0) {
                    // 已有任务在运行，直接开始轮询
                    startPolling(chatId, btn);
                } else {
                    showToast(data.error || '同步失败', 'error');
                    btn.disabled = false;
                    btn.textContent = '同步';
                    updateChatStatus(chatId, 'error', '同步失败');
                }
            } catch (e) {
                showToast('网络错误', 'error');
                btn.disabled = false;
                btn.textContent = '同步';
                updateChatStatus(chatId, 'error', '同步失败');
            }
        }

        // 更新卡片状态：边框色 + 徽章 + 按钮颜色
        function updateChatStatus(chatId, status, badgeText) {
            const item = document.getElementById('chat-' + chatId);
            if (!item) return;
            // 清除旧状态类
            item.classList.remove('status-pending', 'status-synced', 'status-syncing', 'status-error');
            item.classList.add('status-' + status);
            // 更新徽章
            const badge = document.getElementById('badge-' + chatId);
            if (badge) {
                badge.className = 'badge badge-' + status;
                const textEl = badge.querySelector('.badge-text');
                if (textEl) textEl.textContent = badgeText || '';
                badge.style.display = badgeText ? 'inline-flex' : 'none';
            }
            // 更新同步按钮颜色
            const btn = item.querySelector('.btn-sync');
            if (btn && !btn.disabled) {
                btn.classList.toggle('ready-pending', status === 'pending');
            }
        }

        let allSyncMonitorTimer = null;

        function setSyncAllButtonState(isSyncing) {
            const btn = document.getElementById('btnSyncAll');
            if (!btn) return;
            const icon = btn.querySelector('.sync-icon');
            const text = btn.querySelector('.sync-text');
            if (isSyncing) {
                btn.disabled = true;
                btn.classList.add('syncing');
                if (icon) icon.textContent = '🔄';
                if (text) text.textContent = '全部同步中...';
            } else {
                btn.disabled = false;
                btn.classList.remove('syncing');
                if (icon) icon.textContent = '⚡';
                if (text) text.textContent = '全部开始同步';
            }
        }

        function monitorAllSyncProgress() {
            if (allSyncMonitorTimer) return;
            setSyncAllButtonState(true);
            allSyncMonitorTimer = setInterval(() => {
                const syncingItems = document.querySelectorAll('.chat-item.status-syncing');
                if (syncingItems.length === 0) {
                    clearInterval(allSyncMonitorTimer);
                    allSyncMonitorTimer = null;
                    setSyncAllButtonState(false);
                }
            }, 1000);
        }

        async function syncAllChats() {
            const btn = document.getElementById('btnSyncAll');
            if (btn && btn.disabled) return;
            setSyncAllButtonState(true);

            try {
                const resp = await fetch('/api/sync_all', { method: 'POST' });
                const data = await resp.json();
                if (data.ok) {
                    showToast(data.message || '已启动全部群聊同步', 'success');
                    const startedSet = new Set(data.started || []);
                    document.querySelectorAll('.chat-item').forEach(item => {
                        const cid = item.getAttribute('data-chat-id');
                        if (cid && startedSet.has(cid)) {
                            const chatBtn = item.querySelector('.btn-sync');
                            if (chatBtn) {
                                chatBtn.disabled = true;
                                chatBtn.textContent = '同步中...';
                            }
                            updateProgressUI(cid, { stage: 'starting', current: 0, total: 0, message: '准备开始同步...' });
                            updateChatStatus(cid, 'syncing', '同步中');
                            startPolling(cid, chatBtn);
                        }
                    });
                    monitorAllSyncProgress();
                } else {
                    showToast(data.error || '全部同步启动失败', 'error');
                    setSyncAllButtonState(false);
                }
            } catch (e) {
                showToast('网络请求异常', 'error');
                setSyncAllButtonState(false);
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
                        el.textContent = data.error;
                        updateChatStatus(chatId, 'error', '查询失败');
                        return;
                    }
                    const total = data.total || 0;
                    const synced = data.synced || 0;
                    const pending = data.pending || 0;
                    el.textContent = '已同步 ' + synced + ' / ' + total + ' 条' + (pending > 0 ? ' · 待同步 ' + pending + ' 条' : '');
                    if (pending > 0) {
                        updateChatStatus(chatId, 'pending', '待同步 ' + pending);
                    } else {
                        updateChatStatus(chatId, 'synced', '已同步满');
                    }
                } catch (e) {
                    // 查询失败保持原状
                }
                // 顺便检查是否有正在运行的后台同步任务（支持刷新页面后恢复进度显示）
                try {
                    const sr = await fetch('/api/sync_status/' + chatId);
                    const sp = await sr.json();
                    if (sp && sp.running) {
                        const btn = document.querySelector('#chat-' + chatId + ' .btn-sync');
                        if (btn) { btn.disabled = true; btn.textContent = '同步中...'; }
                        updateProgressUI(chatId, sp);
                        updateChatStatus(chatId, 'syncing', '同步中');
                        startPolling(chatId, btn);
                        monitorAllSyncProgress();
                    }
                } catch (e) {}
            });
        });
        // ===== 群名称自动获取与防覆写逻辑 =====
        let userHasCustomizedName = false;
        let lastAutoFilledName = '';
        let lastFetchedChatId = '';
        let fetchRequestId = 0;
        let fetchDebounceTimer = null;

        async function tryFetchChatName(force = false) {
            const chatIdInput = document.getElementById('chatIdInput');
            const chatNameInput = document.getElementById('chatNameInput');
            const spinner = document.getElementById('nameFetchSpinner');
            const tip = document.getElementById('nameFetchTip');
            if (!chatIdInput || !chatNameInput) return;

            const chatId = chatIdInput.value.trim();

            if (!chatId) {
                if (fetchDebounceTimer) clearTimeout(fetchDebounceTimer);
                if (spinner) spinner.style.display = 'none';
                if (tip) tip.style.display = 'none';
                if (chatNameInput.value === lastAutoFilledName) {
                    chatNameInput.value = '';
                    lastAutoFilledName = '';
                }
                lastFetchedChatId = '';
                return;
            }

            // 群 ID 小于 5 位不触发拉取
            if (chatId.length < 5) return;

            if (chatId === lastFetchedChatId && !force) return;
            lastFetchedChatId = chatId;

            const reqId = ++fetchRequestId;
            if (spinner) spinner.style.display = 'inline-block';
            if (tip) tip.style.display = 'none';

            try {
                const resp = await fetch('/api/chats/fetch_name?chat_id=' + encodeURIComponent(chatId));
                const data = await resp.json();

                // 异步请求校验：丢弃过期请求与已被修改的输入
                if (reqId !== fetchRequestId) return;
                if (chatIdInput.value.trim() !== chatId) return;

                if (spinner) spinner.style.display = 'none';

                if (data.ok && data.chat_name) {
                    const fetchedName = data.chat_name;
                    const isFocusingName = (document.activeElement === chatNameInput);

                    // 允许自动填入的条件：
                    // 1. 用户从未手动输入群名；
                    // 2. 或者当前输入框内容为空；
                    // 3. 或者当前输入框的内容正好是上一次自动填入的值（未被改动）。
                    const currentVal = chatNameInput.value.trim();
                    const shouldAutoFill = !userHasCustomizedName || currentVal === '' || currentVal === lastAutoFilledName;

                    if (shouldAutoFill && !isFocusingName) {
                        chatNameInput.value = fetchedName;
                        lastAutoFilledName = fetchedName;
                        if (tip) {
                            tip.className = 'name-fetch-tip info';
                            tip.textContent = '✓ 已自动识别群名：' + fetchedName;
                            tip.style.display = 'block';
                        }
                    } else if (isFocusingName && shouldAutoFill) {
                        // 用户正在群名框编辑，不强行覆写输入框，显示提示并在 blur 时视情况填入
                        if (tip) {
                            tip.className = 'name-fetch-tip info';
                            tip.textContent = '已识别群名：“' + fetchedName + '”（离开焦点生效）';
                            tip.style.display = 'block';
                        }
                        const onBlurFill = function() {
                            if (!userHasCustomizedName || chatNameInput.value.trim() === '' || chatNameInput.value.trim() === lastAutoFilledName) {
                                chatNameInput.value = fetchedName;
                                lastAutoFilledName = fetchedName;
                                if (tip) {
                                    tip.className = 'name-fetch-tip info';
                                    tip.textContent = '✓ 已自动识别群名：' + fetchedName;
                                }
                            }
                            chatNameInput.removeEventListener('blur', onBlurFill);
                        };
                        chatNameInput.addEventListener('blur', onBlurFill);
                    } else {
                        // 用户已经输入了自定义名称，绝对不覆盖！
                        if (tip) {
                            tip.className = 'name-fetch-tip custom';
                            tip.textContent = '已识别飞书群名：“' + fetchedName + '”（保留您的自定义名称）';
                            tip.style.display = 'block';
                        }
                    }
                } else if (data.ok && !data.chat_name) {
                    if (tip) {
                        tip.className = 'name-fetch-tip warn';
                        tip.textContent = '⚠️ 未能获取到群名称（群可能未命名），可手动输入';
                        tip.style.display = 'block';
                    }
                } else {
                    if (tip) {
                        tip.className = 'name-fetch-tip warn';
                        tip.textContent = '⚠️ ' + (data.warning || data.error || '获取群名失败，可手动填写');
                        tip.style.display = 'block';
                    }
                }
            } catch (err) {
                if (reqId !== fetchRequestId) return;
                if (spinner) spinner.style.display = 'none';
                if (tip) {
                    tip.className = 'name-fetch-tip warn';
                    tip.textContent = '⚠️ 获取群名网络异常，可手动输入';
                    tip.style.display = 'block';
                }
            }
        }

        // ===== 群聊缓存与智能下拉选择逻辑 =====
        let cachedUserChats = [];
        let hasLoadedCache = false;
        let selectedChatId = '';
        let selectedChatName = '';
        let activeDropdownIndex = -1;
        let currentFilteredItems = [];

        function escapeHtml(str) {
            if (!str) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }

        function highlightMatch(text, query) {
            if (!text) return '';
            const safeText = escapeHtml(text);
            if (!query) return safeText;
            const safeQuery = escapeHtml(query);
            try {
                const regex = new RegExp('(' + safeQuery.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
                return safeText.replace(regex, '<mark>$1</mark>');
            } catch (e) {
                return safeText;
            }
        }

        async function loadUserChatsCache(force = false) {
            if (hasLoadedCache && !force) return;
            const tipEl = document.getElementById('dropdownCountTip');
            if (tipEl) tipEl.textContent = '正在读取群聊缓存...';
            try {
                const resp = await fetch('/api/user_chats');
                const data = await resp.json();
                if (data.ok) {
                    cachedUserChats = data.chats || [];
                    hasLoadedCache = true;
                    updateDropdownHeaderTip(cachedUserChats.length, data.last_updated);
                } else {
                    if (tipEl) tipEl.textContent = '获取群聊缓存失败';
                }
            } catch (e) {
                if (tipEl) tipEl.textContent = '读取群聊缓存异常';
            }
        }

        function updateDropdownHeaderTip(count, lastUpdated) {
            const tipEl = document.getElementById('dropdownCountTip');
            if (!tipEl) return;
            if (count === 0) {
                tipEl.textContent = '暂无已缓存群聊';
            } else {
                let timeStr = '';
                if (lastUpdated) {
                    try {
                        const parts = lastUpdated.split(/[- :]/);
                        if (parts.length >= 5) {
                            timeStr = ' · ' + parseInt(parts[1]) + '月' + parseInt(parts[2]) + '日 ' + parts[3] + ':' + parts[4];
                        }
                    } catch(e) {}
                }
                tipEl.textContent = '已缓存 ' + count + ' 个群聊' + timeStr;
            }
        }

        function openDropdown() {
            const panel = document.getElementById('chatDropdownPanel');
            const arrow = document.getElementById('chatDropdownArrow');
            if (!panel) return;
            panel.classList.add('show');
            if (arrow) arrow.classList.add('open');
            activeDropdownIndex = -1;

            if (!hasLoadedCache) {
                loadUserChatsCache().then(() => {
                    const input = document.getElementById('chatIdInput');
                    filterAndRenderDropdown(input ? input.value.trim() : '');
                });
            } else {
                const input = document.getElementById('chatIdInput');
                filterAndRenderDropdown(input ? input.value.trim() : '');
            }
        }

        function closeDropdown() {
            const panel = document.getElementById('chatDropdownPanel');
            const arrow = document.getElementById('chatDropdownArrow');
            if (!panel) return;
            panel.classList.remove('show');
            if (arrow) arrow.classList.remove('open');
            activeDropdownIndex = -1;
        }

        function toggleDropdown(e) {
            if (e) { e.preventDefault(); e.stopPropagation(); }
            const panel = document.getElementById('chatDropdownPanel');
            if (panel && panel.classList.contains('show')) {
                closeDropdown();
            } else {
                const input = document.getElementById('chatIdInput');
                if (input) input.focus();
                openDropdown();
            }
        }

        function clearChatInput(e) {
            if (e) { e.preventDefault(); e.stopPropagation(); }
            const input = document.getElementById('chatIdInput');
            const clearBtn = document.getElementById('chatInputClear');
            const tip = document.getElementById('nameFetchTip');
            if (input) {
                input.value = '';
                input.focus();
            }
            selectedChatId = '';
            selectedChatName = '';
            if (clearBtn) clearBtn.style.display = 'none';
            if (tip) tip.style.display = 'none';
            filterAndRenderDropdown('');
        }

        function selectChat(chat) {
            const input = document.getElementById('chatIdInput');
            const nameInput = document.getElementById('chatNameInput');
            const clearBtn = document.getElementById('chatInputClear');
            const tip = document.getElementById('nameFetchTip');

            selectedChatId = chat.chat_id;
            selectedChatName = chat.chat_name || chat.chat_id;

            if (input) {
                input.value = selectedChatName;
            }
            if (clearBtn) clearBtn.style.display = 'inline-flex';

            // 自动填充自定义群名框（若未被用户手动填写）
            if (nameInput) {
                if (!userHasCustomizedName || nameInput.value.trim() === '' || nameInput.value.trim() === lastAutoFilledName) {
                    nameInput.value = chat.chat_name || '';
                    lastAutoFilledName = chat.chat_name || '';
                }
            }

            // 展示选中反馈
            if (tip) {
                tip.className = 'name-fetch-tip info';
                tip.innerHTML = '✓ 已选择飞书群聊：<b>' + escapeHtml(selectedChatName) + '</b> <span style="font-family:monospace; font-size:11px; color:#4e5969; margin-left:4px;">(' + chat.chat_id + ')</span>';
                const dissolvedNotice = (chat.chat_status === 'dissolved_save') ? ' <span style="color:#d46b08; font-size:11px; font-weight:500;">[已解散·保留历史]</span>' : '';
                tip.innerHTML = '✓ 已选择飞书群聊：<b>' + escapeHtml(selectedChatName) + '</b>' + dissolvedNotice + ' <span style="font-family:monospace; font-size:11px; color:#4e5969; margin-left:4px;">(' + chat.chat_id + ')</span>';
                tip.style.display = 'block';
            }

            closeDropdown();
        }

        function filterAndRenderDropdown(query) {
            const listEl = document.getElementById('dropdownList');
            const emptyEl = document.getElementById('dropdownEmpty');
            const emptyTitle = document.getElementById('dropdownEmptyTitle');
            const emptyDesc = document.getElementById('dropdownEmptyDesc');
            if (!listEl || !emptyEl) return;

            const q = (query || '').trim().toLowerCase();

            // 若用户输入与之前选中的不一致，重置 selectedChatId
            if (selectedChatName && query.trim() !== selectedChatName) {
                selectedChatId = '';
                selectedChatName = '';
            }

            // 过滤
            if (!q) {
                currentFilteredItems = cachedUserChats.slice();
            } else {
                currentFilteredItems = cachedUserChats.filter(c => {
                    const name = (c.chat_name || '').toLowerCase();
                    const id = (c.chat_id || '').toLowerCase();
                    return name.includes(q) || id.includes(q);
                });
            }

            // 渲染
            if (currentFilteredItems.length > 0) {
                listEl.style.display = 'block';
                emptyEl.style.display = 'none';
                let html = '';
                for (let idx = 0; idx < currentFilteredItems.length; idx++) {
                    const c = currentFilteredItems[idx];
                    const avatarContent = c.avatar 
                        ? '<img src="' + escapeHtml(c.avatar) + '" class="item-avatar" alt="">' 
                        : '<div class="item-avatar">' + escapeHtml((c.chat_name || '群').charAt(0).toUpperCase()) + '</div>';
                    const nameHtml = highlightMatch(c.chat_name || '未命名群聊', q);
                    const idHtml = highlightMatch(c.chat_id, q);
                    const addedHtml = c.is_added ? '<span class="item-tag-added">已在列表</span>' : '';
                    const dissolvedHtml = (c.chat_status === 'dissolved_save') ? '<span class="item-tag-dissolved" title="该群已解散，但飞书保留了历史消息，仍可归档">已解散(保留历史)</span>' : '';
                    const tagHtml = addedHtml + dissolvedHtml;
                    const descHtml = c.description ? '<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:180px;" title="' + escapeHtml(c.description) + '">' + escapeHtml(c.description) + '</span>' : '';
                    html += '<div class="dropdown-item' + (idx === activeDropdownIndex ? ' active' : '') + '" data-index="' + idx + '" onclick="handleItemClick(' + idx + ', event)">' +
                        avatarContent +
                        '<div class="item-content">' +
                            '<div class="item-title-row">' +
                                '<span class="item-name">' + nameHtml + '</span>' +
                                tagHtml +
                            '</div>' +
                            '<div class="item-sub-row">' +
                                '<span class="item-id">' + idHtml + '</span>' +
                                (descHtml ? '<span>·</span>' + descHtml : '') +
                            '</div>' +
                        '</div>' +
                    '</div>';
                }
                listEl.innerHTML = html;
            } else {
                listEl.style.display = 'none';
                emptyEl.style.display = 'block';
                if (cachedUserChats.length === 0) {
                    emptyTitle.textContent = '暂无已缓存群聊';
                    emptyDesc.textContent = '您尚未同步飞书群聊信息，点击下方按钮从飞书全量拉取您的群聊列表。';
                } else {
                    emptyTitle.textContent = '未找到与 “' + escapeHtml(query) + '” 匹配的群聊';
                    emptyDesc.textContent = '若该群刚创建或刚加入，请点击下方同步最新群聊；也可直接输入以 oc_ 开头的群聊 ID 添加。';
                }
            }
        }

        function handleItemClick(index, event) {
            if (event) { event.preventDefault(); event.stopPropagation(); }
            if (currentFilteredItems[index]) {
                selectChat(currentFilteredItems[index]);
            }
        }

        function updateActiveDropdownItem() {
            const items = document.querySelectorAll('#dropdownList .dropdown-item');
            items.forEach((item, idx) => {
                if (idx === activeDropdownIndex) {
                    item.classList.add('active');
                    item.scrollIntoView({ block: 'nearest' });
                } else {
                    item.classList.remove('active');
                }
            });
        }

        async function syncUserChats(e) {
            if (e) { e.preventDefault(); e.stopPropagation(); }
            const btn1 = document.getElementById('btnSyncCache');
            const btn2 = document.getElementById('btnEmptySync');

            const setSyncing = (syncing) => {
                if (btn1) {
                    btn1.classList.toggle('syncing', syncing);
                    const icon = btn1.querySelector('.sync-icon');
                    if (icon) icon.className = syncing ? 'sync-icon sync-spin' : 'sync-icon';
                    const txt = btn1.querySelector('.sync-text');
                    if (txt) txt.textContent = syncing ? '正在同步...' : '同步群聊缓存';
                }
                if (btn2) {
                    btn2.disabled = syncing;
                    const icon = btn2.querySelector('.sync-icon');
                    if (icon) icon.className = syncing ? 'sync-icon sync-spin' : 'sync-icon';
                    const txt = btn2.querySelector('.sync-text');
                    if (txt) txt.textContent = syncing ? '正在拉取飞书所有群聊...' : '立即同步飞书所有群聊缓存';
                }
            };

            setSyncing(true);
            try {
                const resp = await fetch('/api/user_chats/sync', { method: 'POST' });
                const data = await resp.json();
                if (data.ok) {
                    cachedUserChats = data.chats || [];
                    hasLoadedCache = true;
                    updateDropdownHeaderTip(cachedUserChats.length, data.last_updated);
                    showToast(data.message || ('同步成功，已缓存 ' + cachedUserChats.length + ' 个群聊'), 'success');
                    const input = document.getElementById('chatIdInput');
                    filterAndRenderDropdown(input ? input.value.trim() : '');
                } else {
                    showToast(data.warning || data.error || '同步群聊失败', 'error');
                }
            } catch (err) {
                showToast('同步群聊网络异常', 'error');
            } finally {
                setSyncing(false);
            }
        }

        // 点击外部区域自动关闭群聊下拉面板
        document.addEventListener('click', function(e) {
            const wrapper = document.getElementById('chatInputWrapper');
            if (wrapper && !wrapper.contains(e.target)) {
                closeDropdown();
            }
        });

        const chatIdEl = document.getElementById('chatIdInput');
        const chatNameEl = document.getElementById('chatNameInput');

        if (chatIdEl) {
            chatIdEl.addEventListener('focus', function() {
                openDropdown();
            });
            chatIdEl.addEventListener('click', function() {
                openDropdown();
            });
            chatIdEl.addEventListener('input', function() {
                if (fetchDebounceTimer) clearTimeout(fetchDebounceTimer);
                fetchDebounceTimer = setTimeout(() => tryFetchChatName(), 600);
                const val = chatIdEl.value.trim();
                const clearBtn = document.getElementById('chatInputClear');
                if (clearBtn) clearBtn.style.display = val ? 'inline-flex' : 'none';

                openDropdown();
                filterAndRenderDropdown(val);

                // 若输入以 oc_ 开头，触发飞书预拉取名称
                if (val.startsWith('oc_')) {
                    if (fetchDebounceTimer) clearTimeout(fetchDebounceTimer);
                    fetchDebounceTimer = setTimeout(() => tryFetchChatName(), 600);
                }
            });
            chatIdEl.addEventListener('blur', function() {
                if (fetchDebounceTimer) clearTimeout(fetchDebounceTimer);
                tryFetchChatName();
            });
            chatIdEl.addEventListener('paste', function() {
                setTimeout(() => {
                    if (fetchDebounceTimer) clearTimeout(fetchDebounceTimer);
                    tryFetchChatName();
                    const val = chatIdEl.value.trim();
                    const clearBtn = document.getElementById('chatInputClear');
                    if (clearBtn) clearBtn.style.display = val ? 'inline-flex' : 'none';
                    openDropdown();
                    filterAndRenderDropdown(val);
                    if (val.startsWith('oc_')) {
                        if (fetchDebounceTimer) clearTimeout(fetchDebounceTimer);
                        tryFetchChatName();
                    }
                }, 50);
            });
            chatIdEl.addEventListener('keydown', function(e) {
                if (e.key === 'Enter') addChat();
                const panel = document.getElementById('chatDropdownPanel');
                const isDropdownOpen = panel && panel.classList.contains('show');

                if (e.key === 'ArrowDown') {
                    if (!isDropdownOpen) {
                        openDropdown();
                    } else if (currentFilteredItems.length > 0) {
                        e.preventDefault();
                        activeDropdownIndex = (activeDropdownIndex + 1) % currentFilteredItems.length;
                        updateActiveDropdownItem();
                    }
                } else if (e.key === 'ArrowUp') {
                    if (isDropdownOpen && currentFilteredItems.length > 0) {
                        e.preventDefault();
                        activeDropdownIndex = (activeDropdownIndex - 1 + currentFilteredItems.length) % currentFilteredItems.length;
                        updateActiveDropdownItem();
                    }
                } else if (e.key === 'Enter') {
                    if (isDropdownOpen && activeDropdownIndex >= 0 && currentFilteredItems[activeDropdownIndex]) {
                        e.preventDefault();
                        selectChat(currentFilteredItems[activeDropdownIndex]);
                    } else {
                        closeDropdown();
                        addChat();
                    }
                } else if (e.key === 'Escape') {
                    closeDropdown();
                }
            });
        }

        if (chatNameEl) {
            chatNameEl.addEventListener('input', function() {
                const val = chatNameEl.value.trim();
                if (val && val !== lastAutoFilledName) {
                    userHasCustomizedName = true;
                    const tip = document.getElementById('nameFetchTip');
                    if (tip && tip.classList.contains('info')) tip.style.display = 'none';
                } else if (!val) {
                    userHasCustomizedName = false;
                }
            });
            chatNameEl.addEventListener('keydown', function(e) {
                if (e.key === 'Enter') addChat();
            });
        }
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG)
