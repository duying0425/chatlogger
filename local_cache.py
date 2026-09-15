import os
import re
import io
import time
import json
import shutil
import zipfile
from datetime import datetime, timezone, timedelta
from config import Config


def sanitize_filename(name, fallback="file"):
    """移除非法字符，生成安全的文件名/目录名（兼容 Windows/Linux）"""
    if not name:
        return fallback
    # 替换 Windows/Linux 文件系统非法字符: \ / : * ? " < > | \r \n \t
    cleaned = re.sub(r'[\\/*?:"<>|\r\n\t]+', "_", str(name)).strip(" ._")
    return cleaned if cleaned else fallback


def get_chat_cache_dir(chat_id, chat_name=None):
    """获取或创建指定群聊的本地缓存根目录及 assets 目录。
    目录命名规则：cache/{safe_chat_name}_{chat_id}
    如果已存在匹配 *_{chat_id} 的目录，则复用该目录（避免改名后创建新目录）。
    """
    base_dir = Config.LOCAL_CACHE_DIR
    os.makedirs(base_dir, exist_ok=True)

    # 优先查找是否已有历史目录匹配当前 chat_id
    if os.path.exists(base_dir):
        for entry in os.listdir(base_dir):
            full_entry = os.path.join(base_dir, entry)
            if os.path.isdir(full_entry) and entry.endswith(f"_{chat_id}"):
                assets_dir = os.path.join(full_entry, "assets")
                os.makedirs(assets_dir, exist_ok=True)
                return full_entry

    safe_name = sanitize_filename(chat_name or chat_id, fallback="chat")
    dir_name = f"{safe_name}_{chat_id}"
    chat_dir = os.path.join(base_dir, dir_name)
    assets_dir = os.path.join(chat_dir, "assets")

    os.makedirs(chat_dir, exist_ok=True)
    os.makedirs(assets_dir, exist_ok=True)
    return chat_dir


def get_chat_md_path(chat_id, chat_name=None):
    """获取群聊对应的 Markdown 文件绝对路径。"""
    chat_dir = get_chat_cache_dir(chat_id, chat_name)
    # 若目录下已有 .md 文件，优先复用已有的
    for f in os.listdir(chat_dir):
        if f.endswith(".md"):
            return os.path.join(chat_dir, f)
    # 否则根据群名创建
    safe_name = sanitize_filename(chat_name or chat_id, fallback="chat")
    return os.path.join(chat_dir, f"{safe_name}.md")


def init_chat_cache(chat_id, chat_name=None):
    """如果 Markdown 文件尚不存在，初始化并写入头部元信息。"""
    md_path = get_chat_md_path(chat_id, chat_name)
    if os.path.exists(md_path) and os.path.getsize(md_path) > 0:
        return md_path

    display_name = chat_name or chat_id
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    header = (
        f"# {display_name} - 聊天记录归档\n\n"
        f"> - **群聊名称**: {display_name}\n"
        f"> - **群聊 ID**: `{chat_id}`\n"
        f"> - **初次归档时间**: {now_str}\n\n"
        f"---\n\n"
    )
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(header)
    return md_path


def ensure_chat_header(chat_id, chat_name=None):
    """确保 Markdown 文件存在头部。若已存在但原标题为 chat_id 且现在有了真实群名，则平滑更新头部标题。"""
    md_path = get_chat_md_path(chat_id, chat_name)
    if not os.path.exists(md_path) or os.path.getsize(md_path) == 0:
        return init_chat_cache(chat_id, chat_name)

    if not chat_name or chat_name == chat_id:
        return md_path

    try:
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read(2048)  # 只读前 2KB 检查头部

        old_title = f"# {chat_id} - 聊天记录归档"
        new_title = f"# {chat_name} - 聊天记录归档"
        old_meta = f"> - **群聊名称**: {chat_id}"
        new_meta = f"> - **群聊名称**: {chat_name}"

        if old_title in content or old_meta in content:
            with open(md_path, "r", encoding="utf-8") as f:
                full_content = f.read()
            full_content = full_content.replace(old_title, new_title, 1).replace(old_meta, new_meta, 1)
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(full_content)
    except Exception as e:
        print(f"[local_cache] 更新群聊归档头部失败: {e}")

    return md_path


def find_existing_asset(chat_id, chat_name, filename, file_key=""):
    """在 assets/ 目录中查找是否已下载过该附件/图片。
    如果存在且文件大小 > 0，直接返回相对路径 'assets/{filename}'，避免重复下载。
    """
    chat_dir = get_chat_cache_dir(chat_id, chat_name)
    assets_dir = os.path.join(chat_dir, "assets")
    if not os.path.exists(assets_dir):
        return None

    safe_name = sanitize_filename(filename, fallback="")
    candidates = []
    if safe_name:
        candidates.append(safe_name)
    if file_key:
        safe_key = sanitize_filename(file_key)
        candidates.append(safe_key)
        ext = os.path.splitext(filename)[1] if filename else ""
        if ext:
            candidates.append(f"{safe_key}{ext}")

    try:
        existing_files = set(os.listdir(assets_dir))
    except Exception:
        return None

    for c in candidates:
        if c in existing_files:
            target_path = os.path.join(assets_dir, c)
            if os.path.getsize(target_path) > 0:
                return f"assets/{c}"

    # 按 file_key 子串查找（兼容 img_v3_...jpg 等情况）
    if file_key:
        for f in existing_files:
            if file_key in f:
                target_path = os.path.join(assets_dir, f)
                if os.path.getsize(target_path) > 0:
                    return f"assets/{f}"

    return None


def save_asset(chat_id, chat_name, file_content, filename, file_key=""):
    """保存图片或附件到 cache/{chat}/assets/ 目录。
    返回在 Markdown 中使用的相对路径，如: assets/unique_file.jpg
    """
    chat_dir = get_chat_cache_dir(chat_id, chat_name)
    assets_dir = os.path.join(chat_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)

    safe_name = sanitize_filename(filename, fallback=file_key or "attachment")
    # 避免重名覆盖：如果文件已存在但内容不同，加上 file_key 前缀
    target_path = os.path.join(assets_dir, safe_name)
    if os.path.exists(target_path):
        # 检查大小，如果大小相同则视为同一文件直接复用
        if os.path.getsize(target_path) == len(file_content):
            return f"assets/{safe_name}"
        # 否则加唯一标识
        prefix = sanitize_filename(file_key[:8]) if file_key else str(int(time.time()))
        safe_name = f"{prefix}_{safe_name}"
        target_path = os.path.join(assets_dir, safe_name)

    with open(target_path, "wb") as f:
        f.write(file_content)

    return f"assets/{safe_name}"


# ===== 云文档快照缓存 =====

def get_doc_cache_dir(chat_id, chat_name=None):
    """云文档快照目录：cache/{chat}/assets/docs/"""
    docs_dir = os.path.join(get_chat_cache_dir(chat_id, chat_name), "assets", "docs")
    os.makedirs(docs_dir, exist_ok=True)
    return docs_dir


def find_cached_doc(chat_id, chat_name, doc_token):
    """按 doc_token 查找已缓存的文档快照，返回相对路径（assets/docs/xxx.md）或 None。"""
    docs_dir = get_doc_cache_dir(chat_id, chat_name)
    suffix = f"_{doc_token}.md"
    for f in os.listdir(docs_dir):
        if f.endswith(suffix):
            return f"assets/docs/{f}"
    return None


def save_doc(chat_id, chat_name, doc_token, title, md_content):
    """保存云文档快照 Markdown。同一文档（doc_token）已缓存则直接复用（快照语义，不追更新）。"""
    existing = find_cached_doc(chat_id, chat_name, doc_token)
    if existing:
        return existing
    docs_dir = get_doc_cache_dir(chat_id, chat_name)
    safe_title = sanitize_filename(title, fallback="doc")
    fname = f"{safe_title}_{doc_token}.md"
    with open(os.path.join(docs_dir, fname), "w", encoding="utf-8") as f:
        f.write(md_content)
    return f"assets/docs/{fname}"


def save_doc_image(chat_id, chat_name, image_token, file_content, ext="png"):
    """保存文档内图片到 assets/docs/。返回相对文档快照 md 的路径（同目录文件名）。"""
    docs_dir = get_doc_cache_dir(chat_id, chat_name)
    fname = f"img_{sanitize_filename(image_token, fallback='img')}.{ext}"
    path = os.path.join(docs_dir, fname)
    if not os.path.exists(path):  # 同 token 图片复用
        with open(path, "wb") as f:
            f.write(file_content)
    return fname


def annotate_doc_links(content_md, doc_map):
    """在消息 Markdown 中的飞书云文档链接后附加本地快照链接。
    doc_map: {token -> rel_path}，token 为消息 URL 中出现的 docx/wiki token。
    两种形态分别处理：
    - Markdown 链接 [文本](URL) → [文本](URL) [📄缓存](rel)
    - 裸 URL → URL [📄缓存](rel)
    """
    if not doc_map or not content_md:
        return content_md
    for token, rel in doc_map.items():
        et = re.escape(token)
        # 1) Markdown 链接形式：整个 [x](URL) 之后追加
        link_re = re.compile(
            rf"\[([^\]]*)\]\((https?://[^\s)]*feishu\.cn/(?:docx|wiki|docs)/{et}[^\s)]*)\)",
            re.IGNORECASE)
        content_md = link_re.sub(lambda m: f"{m.group(0)} [📄缓存]({rel})", content_md)
        # 2) 裸 URL（不在 ]( 内）：URL 后追加。查询参数遇空白/中文即止，防止吞掉后续文字
        bare_re = re.compile(
            rf"(?<!\]\()(https?://[A-Za-z0-9.-]*feishu\.cn/(?:docx|wiki|docs)/{et}"
            rf"(?:\?[^\s\u4e00-\u9fff\uff0c\u3002\uff1b\uff1a\uff01\uff09]*)?)",
            re.IGNORECASE)
        content_md = bare_re.sub(lambda m: f"{m.group(1)} [📄缓存]({rel})", content_md)
    return content_md


def _format_post_content(locale_dict, asset_map=None):
    """格式化飞书富文本（post）为标准 Markdown 文本，并内嵌替换图片与多媒体相对路径。"""
    if not isinstance(locale_dict, dict):
        return ""

    title = locale_dict.get("title", "")
    lines = []
    if title:
        lines.append(f"### {title}\n")

    blocks = locale_dict.get("content", [])
    if not isinstance(blocks, list):
        return "\n".join(lines)

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
                styles = elem.get("style", []) or []
                if "bold" in styles:
                    t = f"**{t}**"
                if "italic" in styles:
                    t = f"*{t}*"
                if "lineThrough" in styles:
                    t = f"~~{t}~~"
                if "underline" in styles:
                    t = f"<u>{t}</u>"
                line_parts.append(t)
            elif tag == "a":
                text = elem.get("text") or elem.get("href") or "链接"
                href = elem.get("href") or ""
                line_parts.append(f"[{text}]({href})" if href else text)
            elif tag == "at":
                name = elem.get("user_name") or elem.get("user_id") or "成员"
                line_parts.append(f"@{name}")
            elif tag == "img":
                key = elem.get("image_key", "")
                rel_path = asset_map.get(key) if asset_map else None
                if rel_path:
                    line_parts.append(f"\n\n![图片]({rel_path})\n\n")
                else:
                    line_parts.append("[图片]")
            elif tag == "media":
                key = elem.get("file_key", "")
                name = elem.get("file_name") or "视频"
                rel_path = asset_map.get(key) if asset_map else None
                if rel_path:
                    line_parts.append(f"\n\n[🎬 {name}]({rel_path})\n\n")
                else:
                    line_parts.append(f"[视频: {name}]")
            elif tag == "code_block":
                code = elem.get("text", "")
                lang = elem.get("language", "")
                line_parts.append(f"\n\n```{lang}\n{code}\n```\n\n")
            elif tag in ("emoji", "emotion"):
                emoji_name = elem.get("name") or elem.get("text") or ""
                line_parts.append(f":{emoji_name}:" if emoji_name else "")
            else:
                t = elem.get("text", "")
                if t:
                    line_parts.append(t)
        if line_parts:
            lines.append("".join(line_parts))

    return "\n\n".join(lines).strip()


def format_message_to_markdown(msg, speaker_name, date_str, asset_map=None, skipped_notes=None, doc_map=None):
    """将一条飞书消息对象格式化为标准 Markdown 消息块。
    msg: 飞书消息 dict
    speaker_name: 解析后的发言人姓名（如 "张三"、"系统"、"机器人"）
    date_str: 格式化时间字符串 "YYYY-MM-DD HH:mm:ss"
    asset_map: {file_key: rel_path} 映射表
    skipped_notes: 该条消息被跳过的附件提示（如超大文件）
    doc_map: {doc_token: rel_path} 云文档快照映射，命中消息中的云文档链接时附加本地缓存链接
    """
    if asset_map is None:
        asset_map = {}

    msg_id = msg.get("message_id", "")
    msg_type = msg.get("msg_type", "")
    body = msg.get("body", {})
    content_raw = body.get("content", "")

    # 解析 content
    content_dict = None
    if isinstance(content_raw, dict):
        content_dict = content_raw
    elif isinstance(content_raw, str):
        try:
            content_dict = json.loads(content_raw)
        except Exception:
            content_dict = None

    content_md = ""
    used_asset_keys = set()

    if msg_type == "text":
        text = content_dict.get("text", content_raw) if content_dict else str(content_raw)
        # 兼容 @_user_N 替换
        from feishu import _resolve_mentions
        text = _resolve_mentions(text, msg)
        content_md = text

    elif msg_type == "post":
        if content_dict:
            locale = content_dict.get("zh_cn") or content_dict.get("en_us") or content_dict
            content_md = _format_post_content(locale, asset_map)
            # 记录已在 post 中内嵌渲染的 asset
            for k in asset_map:
                if k in str(content_dict):
                    used_asset_keys.add(k)
        else:
            content_md = "[富文本消息]"

    elif msg_type == "image":
        key = content_dict.get("image_key", "") if content_dict else ""
        rel_path = asset_map.get(key)
        if rel_path:
            content_md = f"![图片]({rel_path})"
            used_asset_keys.add(key)
        else:
            content_md = "![图片]([图片下载失败或已跳过])"

    elif msg_type == "file":
        key = content_dict.get("file_key", "") if content_dict else ""
        fname = content_dict.get("file_name", "文件") if content_dict else "文件"
        rel_path = asset_map.get(key)
        if rel_path:
            content_md = f"[📎 {fname}]({rel_path})"
            used_asset_keys.add(key)
        else:
            content_md = f"[📎 {fname} (未下载或超出限制)]"

    elif msg_type == "system":
        from feishu import process_message_content
        content_md = f"*{process_message_content(msg)}*"

    elif msg_type == "audio":
        content_md = "[🎵 语音消息]"

    elif msg_type == "media":
        content_md = "[🎬 视频消息]"

    else:
        from feishu import process_message_content
        content_md = process_message_content(msg)

    # 云文档快照链接标注（text / post 中的飞书云文档链接）
    if doc_map:
        content_md = annotate_doc_links(content_md, doc_map)

    # 检查是否有未在正文中渲染的关联资源（如普通消息的附件），追加到消息底部
    extra_assets = []
    for k, rel_path in asset_map.items():
        if k not in used_asset_keys:
            if rel_path.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                extra_assets.append(f"![图片]({rel_path})")
            else:
                base_fname = os.path.basename(rel_path)
                extra_assets.append(f"[📎 {base_fname}]({rel_path})")

    if extra_assets:
        content_md = content_md + "\n\n" + "\n\n".join(extra_assets)

    # 追加跳过说明（若有）
    if skipped_notes:
        notes_str = " ".join(skipped_notes)
        content_md += f"\n\n> ⚠️ *{notes_str}*"

    # 规范组合单条消息块，并在块首内嵌位置与 ID 注释（HTML 注释在渲染时完全隐藏，但可用于断点续传/自愈）
    header_speaker = f"**{speaker_name}**" if speaker_name else "**未知发言人**"
    pos = int(msg.get("message_position") or 0)
    mid = msg.get("message_id") or ""
    pos_meta = f"<!-- msg_pos:{pos} msg_id:{mid} -->\n" if pos else ""

    if msg_type == "system":
        block = f"{pos_meta}*系统消息 · {date_str}*\n\n{content_md}\n\n---\n"
    else:
        block = f"{pos_meta}{header_speaker} &nbsp; `{date_str}`\n\n{content_md}\n\n---\n"

    return block


def get_last_cached_position_from_file(chat_id, chat_name=None):
    """从本地 Markdown 文件末尾快速提取已归档的最后一条消息 position。
    文件不存在或未找到匹配注释时返回 None。
    """
    md_path = get_chat_md_path(chat_id, chat_name)
    if not os.path.exists(md_path) or os.path.getsize(md_path) == 0:
        return None

    try:
        size = os.path.getsize(md_path)
        read_size = min(size, 32768)  # 读末尾 32KB
        with open(md_path, "rb") as f:
            if size > read_size:
                f.seek(size - read_size)
            chunk = f.read().decode("utf-8", errors="ignore")
        matches = re.findall(r"<!--\s*msg_pos:(\d+)", chunk)
        if matches:
            return int(matches[-1])
    except Exception as e:
        print(f"[local_cache] 从文件读取最后 cached position 失败: {e}")
    return None


def append_messages_to_cache(chat_id, chat_name, formatted_blocks):
    """将一组格式化好的 Markdown 消息块批量追加到本地缓存文件中。"""
    if not formatted_blocks:
        return

    md_path = init_chat_cache(chat_id, chat_name)
    with open(md_path, "a", encoding="utf-8") as f:
        for b in formatted_blocks:
            f.write(b + "\n")


def has_cache(chat_id, chat_name=None):
    """检查群聊是否已有本地缓存（MD 文件存在且非空）。"""
    chat_dir = get_chat_cache_dir(chat_id, chat_name)
    if not os.path.exists(chat_dir):
        return False
    for f in os.listdir(chat_dir):
        if f.endswith(".md"):
            p = os.path.join(chat_dir, f)
            if os.path.getsize(p) > 0:
                return True
    return False


def get_cache_info(chat_id, chat_name=None):
    """获取群聊本地缓存统计信息。"""
    chat_dir = get_chat_cache_dir(chat_id, chat_name)
    if not os.path.exists(chat_dir):
        return {"exists": False}

    md_path = None
    md_size = 0
    md_name = ""
    updated_at = ""

    for f in os.listdir(chat_dir):
        if f.endswith(".md"):
            md_path = os.path.join(chat_dir, f)
            md_name = f
            md_size = os.path.getsize(md_path)
            mtime = os.path.getmtime(md_path)
            updated_at = datetime.fromtimestamp(mtime, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
            break

    if not md_path:
        return {"exists": False}

    assets_dir = os.path.join(chat_dir, "assets")
    asset_count = 0
    assets_size = 0
    if os.path.exists(assets_dir):
        for root, _, files in os.walk(assets_dir):
            for file in files:
                asset_count += 1
                assets_size += os.path.getsize(os.path.join(root, file))

    total_size = md_size + assets_size
    return {
        "exists": True,
        "chat_dir": chat_dir,
        "md_path": md_path,
        "md_name": md_name,
        "md_size": md_size,
        "asset_count": asset_count,
        "total_size": total_size,
        "updated_at": updated_at,
    }


def get_raw_markdown(chat_id, chat_name=None):
    """读取并返回群聊的原始 Markdown 内容。"""
    md_path = get_chat_md_path(chat_id, chat_name)
    if not os.path.exists(md_path):
        return None
    with open(md_path, "r", encoding="utf-8") as f:
        return f.read()


def build_cache_zip(chat_id, chat_name=None):
    """将群聊的本地缓存目录（包括 .md 和 assets 目录）打包为 ZIP 文件流。
    返回 (zip_bytes_io, zip_filename)
    """
    chat_dir = get_chat_cache_dir(chat_id, chat_name)
    safe_name = sanitize_filename(chat_name or chat_id, fallback="chat")
    zip_filename = f"{safe_name}_archive.zip"

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(chat_dir):
            for file in files:
                abs_path = os.path.join(root, file)
                rel_path = os.path.relpath(abs_path, chat_dir)
                zf.write(abs_path, arcname=rel_path)

    memory_file.seek(0)
    return memory_file, zip_filename


def delete_cache(chat_id, chat_name=None):
    """删除指定群聊的本地缓存目录及全部文件。
    安全防范：严格限制在 Config.LOCAL_CACHE_DIR 目录下删除，匹配 _{chat_id} 或 {chat_id}。
    """
    base_dir = os.path.abspath(Config.LOCAL_CACHE_DIR)
    if not os.path.exists(base_dir):
        return False

    deleted = False
    for entry in os.listdir(base_dir):
        full_entry = os.path.abspath(os.path.join(base_dir, entry))
        if not full_entry.startswith(base_dir):
            continue
        if os.path.isdir(full_entry) and (entry.endswith(f"_{chat_id}") or entry == chat_id):
            try:
                shutil.rmtree(full_entry)
                deleted = True
            except Exception as e:
                print(f"[local_cache] 删除缓存目录 {full_entry} 失败: {e}")

    return deleted


def rename_chat_cache(chat_id, new_chat_name, old_chat_name=None):
    """当群聊名称发生变更时，自动联动重命名本地缓存目录、Markdown 文件，并更新 Markdown 头部标题。"""
    base_dir = os.path.abspath(Config.LOCAL_CACHE_DIR)
    if not os.path.exists(base_dir):
        return {"renamed": False, "reason": "cache_dir_not_found"}

    # 1. 查找现有缓存目录（匹配 _{chat_id} 或直接为 {chat_id}）
    old_dir = None
    for entry in os.listdir(base_dir):
        full_entry = os.path.join(base_dir, entry)
        if os.path.isdir(full_entry) and (entry.endswith(f"_{chat_id}") or entry == chat_id):
            old_dir = full_entry
            break

    if not old_dir:
        return {"renamed": False, "reason": "no_existing_cache"}

    safe_new_name = sanitize_filename(new_chat_name or chat_id, fallback="chat")
    new_dir_name = f"{safe_new_name}_{chat_id}"
    new_dir = os.path.join(base_dir, new_dir_name)

    # 2. 如果目录名称不同，重命名目录
    current_dir = old_dir
    if os.path.abspath(old_dir) != os.path.abspath(new_dir):
        try:
            if not os.path.exists(new_dir):
                os.rename(old_dir, new_dir)
                current_dir = new_dir
        except Exception as e:
            print(f"[local_cache] 重命名群聊缓存目录失败: {e}")
            current_dir = old_dir

    # 3. 重命名 .md 文件
    old_md_path = None
    for f in os.listdir(current_dir):
        if f.endswith(".md"):
            old_md_path = os.path.join(current_dir, f)
            break

    new_md_name = f"{safe_new_name}.md"
    new_md_path = os.path.join(current_dir, new_md_name)

    if old_md_path and os.path.exists(old_md_path):
        if os.path.abspath(old_md_path) != os.path.abspath(new_md_path):
            try:
                os.rename(old_md_path, new_md_path)
            except Exception as e:
                print(f"[local_cache] 重命名 Markdown 文件失败: {e}")
                new_md_path = old_md_path

        # 4. 更新 Markdown 头部标题与元信息
        try:
            with open(new_md_path, "r", encoding="utf-8") as f:
                content = f.read()

            display_name = new_chat_name or chat_id
            # 替换正文第一级大标题 # ... - 聊天记录归档
            content = re.sub(
                r"^#\s+[^\n]+?\s+-\s+聊天记录归档",
                f"# {display_name} - 聊天记录归档",
                content,
                count=1,
                flags=re.MULTILINE
            )
            # 替换元数据行 > - **群聊名称**: ...
            content = re.sub(
                r"> - \*\*群聊名称\*\*:\s*[^\n]+",
                f"> - **群聊名称**: {display_name}",
                content,
                count=1
            )
            with open(new_md_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            print(f"[local_cache] 更新 Markdown 头部标题失败: {e}")

    return {"renamed": True, "dir": current_dir, "md": new_md_path}


