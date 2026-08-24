import os
import time
import json
import threading
import traceback
from flask import Flask, request, redirect, session, jsonify, render_template_string, make_response
from config import Config
import models
from feishu import (
    FeishuClient,
    process_message_content,
    extract_resource_keys,
    SizeExceededError,
)

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
        resp = make_response(render_template_string(LOGIN_PAGE, auth_url=FeishuClient.get_authorize_url()))
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp

    chats = models.get_chats(user["id"])
    resp = make_response(render_template_string(INDEX_PAGE, user=user, chats=chats))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

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

@app.route("/api/chats", methods=["POST"])
def api_add_chat():
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401

    chat_id = request.json.get("chat_id", "").strip()
    if not chat_id:
        return jsonify({"error": "请输入群聊 ID"}), 400

    # 用户可手动指定群名称（可选）。留空则尝试拉取，再不行退化为 chat_id
    custom_name = (request.json.get("chat_name") or "").strip()

    # 检查是否已存在
    existing = models.get_chat(user["id"], chat_id)
    if existing:
        return jsonify({"error": "该群聊已存在"}), 409

    # 名称优先级：用户填写 > API 拉取 > chat_id
    chat_name = custom_name
    name_fetch_error = ""
    if not chat_name:
        client = get_feishu_client()
        if client:
            try:
                chat_info = client.get_chat_info(chat_id)
                chat_name = chat_info.get("name", "") or ""
            except Exception as e:
                chat_name = ""
                name_fetch_error = str(e)
                print(f"[add_chat] 自动获取群名失败 chat_id={chat_id}: {e}")
    if not chat_name:
        chat_name = chat_id

    models.add_chat(user["id"], chat_id, chat_name)
    result = {"ok": True, "chat_name": chat_name}
    if name_fetch_error:
        # 获取失败原因可见，不再静默退化为 chat_id
        warning = "已添加，但自动获取群名失败，暂用群聊 ID 代替"
        if "232025" in name_fetch_error:
            warning += "：应用未开通机器人能力，请在飞书开发者后台「添加应用能力」中开通机器人"
        result["warning"] = warning
    return jsonify(result)

@app.route("/api/chats/<chat_id>", methods=["DELETE"])
def api_delete_chat(chat_id):
    user = get_current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401

    # query 参数 delete_base=true 时同时删除飞书多维表格
    delete_base = request.args.get("delete_base") == "true"

    chat_config = models.get_chat(user["id"], chat_id)
    if delete_base and chat_config and chat_config.get("base_token"):
        client = get_feishu_client()
        if client:
            try:
                client.delete_bitable(chat_config["base_token"])
            except Exception as e:
                # 表格删除失败不阻断配置删除，仅返回警告
                return jsonify({"error": f"删除飞书表格失败: {e}"}), 500

    models.delete_chat(user["id"], chat_id)
    return jsonify({"ok": True})

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

        # 1. 获取群名称：仅在尚未设置（或退化为 chat_id）时尝试拉取，避免覆盖用户自定义名称
        _set_progress(chat_id, stage="fetching_chat_info", message="获取群信息...")
        chat_name = chat_config.get("chat_name") or ""
        if not chat_name or chat_name == chat_id:
            try:
                chat_info = client.get_chat_info(chat_id)
                fetched = chat_info.get("name", "") or ""
                if fetched:
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
                                original_filename=r.get("file_name", "")
                            )
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

        # 8. 更新同步状态
        new_last_position = int(messages[-1].get("message_position") or 0)
        new_record_count = (chat_config.get("record_count", 0) or 0) + len(messages)
        models.update_chat_sync_status(user["id"], chat_id, new_last_position, new_record_count)

        if not chat_config.get("chat_name"):
            models.update_chat_table_info(user["id"], chat_id, base_token, table_id, base_url, chat_name)

        result = {
            "ok": True,
            "new_count": len(messages),
            "attach_count": attach_count,
            "skipped_count": skipped_count,
            "total_records": new_record_count,
            "base_url": base_url,
        }
        _set_progress(chat_id, stage="done", running=False,
                      current=total, total=total,
                      message=f"同步完成：新增 {len(messages)} 条消息" +
                              (f"，附件 {attach_count} 个" if attach_count > 0 else "") +
                              (f"，跳过大附件 {skipped_count} 个" if skipped_count > 0 else ""),
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

INDEX_PAGE = """
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
        /* 同步进度条 */
        .sync-progress { margin-top: 10px; display: none; }
        .sync-progress .stage { font-size: 12px; color: #4e5969; margin-bottom: 6px; line-height: 1.4; }
        .sync-progress .bar-wrap { width: 100%; height: 6px; background: #f2f3f5; border-radius: 3px; overflow: hidden; }
        .sync-progress .bar { height: 100%; background: linear-gradient(90deg, #3370ff, #00b42a); border-radius: 3px; width: 0%; transition: width 0.3s; }
        .sync-progress.error .bar { background: #f53f3f; }
        .sync-progress.done .bar { background: #00b42a; }
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
                <input type="text" id="chatIdInput" placeholder="输入群聊 ID (oc_xxx)" />
                <input type="text" id="chatNameInput" placeholder="群聊名称（可选，留空自动获取）" style="flex:1; min-width:160px;" />
                <button onclick="addChat()">添加</button>
            </div>
        </div>
        <div class="section-title">已配置群聊</div>
        <div class="chat-list" id="chatList">
            {% if chats %}
                {% for chat in chats %}
                <div class="chat-item" id="chat-{{ chat.chat_id }}" data-chat-id="{{ chat.chat_id }}">
                    <div class="chat-info">
                        <div class="name">{{ chat.chat_name or chat.chat_id }}</div>
                        <div class="meta" id="meta-{{ chat.chat_id }}">
                            <span class="id">{{ chat.chat_id }}</span>
                            {% if chat.record_count %}<span class="dot">·</span><span>已同步 <span class="record-count">{{ chat.record_count }}</span> 条</span>{% endif %}
                            {% if chat.base_url %}<span class="dot">·</span><a href="{{ chat.base_url }}" target="_blank">查看表格</a>{% endif %}
                        </div>
                        <div class="stats-row">
                            <div class="stats" data-chat-id="{{ chat.chat_id }}">查询中...</div>
                            <span class="badge badge-syncing" id="badge-{{ chat.chat_id }}" style="display:none;"><span class="dot-icon"></span><span class="badge-text">同步中</span></span>
                        </div>
                        <div class="sync-progress" id="progress-{{ chat.chat_id }}">
                            <div class="stage">准备中...</div>
                            <div class="bar-wrap"><div class="bar"></div></div>
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
    <div class="modal-mask" id="deleteModal">
        <div class="modal">
            <h3>删除群聊配置</h3>
            <p>请选择删除方式：<br>「仅删配置」：飞书多维表格中的数据会保留。<br>「同时删表格」：将一并删除已同步的飞书多维表格，此操作不可恢复。</p>
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

        async function addChat() {
            const input = document.getElementById('chatIdInput');
            const nameInput = document.getElementById('chatNameInput');
            const chatId = input.value.trim();
            if (!chatId) return;
            const chatName = nameInput.value.trim();
            const resp = await fetch('/api/chats', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chat_id: chatId, chat_name: chatName || undefined }),
            });
            const data = await resp.json();
            if (data.ok) {
                if (data.warning) { sessionStorage.setItem('addChatWarning', data.warning); }
                location.reload();
            }
            else { showToast(data.error || '添加失败', 'error'); }
        }

        let pendingDeleteChatId = null;
        function deleteChat(chatId) {
            pendingDeleteChatId = chatId;
            document.getElementById('deleteModal').classList.add('show');
        }
        function closeDeleteModal() {
            pendingDeleteChatId = null;
            document.getElementById('deleteModal').classList.remove('show');
        }
        async function doDelete(deleteBase) {
            const chatId = pendingDeleteChatId;
            if (!chatId) return;
            const url = '/api/chats/' + chatId + (deleteBase ? '?delete_base=true' : '');
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
                    }
                } catch (e) {}
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
