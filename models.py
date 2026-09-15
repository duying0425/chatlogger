import sqlite3
import os
from config import Config

def get_db():
    conn = sqlite3.connect(Config.DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")

    # 用户表：存储 OAuth token
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            open_id TEXT UNIQUE NOT NULL,
            name TEXT,
            access_token TEXT,
            refresh_token TEXT,
            token_expires_at REAL,
            refresh_expires_at REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 群聊配置表：chat_id ↔ 多维表格映射
    c.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL,
            chat_name TEXT,
            base_token TEXT,
            table_id TEXT,
            base_url TEXT,
            last_synced_position INTEGER DEFAULT 0,
            record_count INTEGER DEFAULT 0,
            local_cache INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            UNIQUE(user_id, chat_id)
        )
    """)

    # 迁移：检查 chats 表是否已有 local_cache 字段
    try:
        c.execute("ALTER TABLE chats ADD COLUMN local_cache INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # 迁移：飞书真实群名（与用户自定义名 chat_name 分开存储）
    try:
        c.execute("ALTER TABLE chats ADD COLUMN feishu_name TEXT")
        # 列刚创建：存量行的 chat_name 多为自动拉取的真实群名，回填以立即展示
        c.execute("UPDATE chats SET feishu_name = chat_name "
                  "WHERE chat_name IS NOT NULL AND chat_name != chat_id")
    except sqlite3.OperationalError:
        pass

    # 迁移：本地缓存已归档的最大消息位置
    try:
        c.execute("ALTER TABLE chats ADD COLUMN last_cached_position INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # 迁移：群聊最新一条消息的发送时间戳（毫秒）
    try:
        c.execute("ALTER TABLE chats ADD COLUMN latest_message_time INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # 用户群聊信息缓存表：缓存该用户在飞书加入的所有群聊
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_chats_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL,
            chat_name TEXT,
            avatar TEXT,
            description TEXT,
            chat_status TEXT DEFAULT 'normal',
            owner_id TEXT,
            is_owner INTEGER DEFAULT 0,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            UNIQUE(user_id, chat_id)
        )
    """)
    # 迁移：检查 user_chats_cache 表是否已有 owner_id / is_owner 字段
    try:
        c.execute("ALTER TABLE user_chats_cache ADD COLUMN owner_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE user_chats_cache ADD COLUMN is_owner INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    c.execute("CREATE INDEX IF NOT EXISTS idx_user_chats_cache_user ON user_chats_cache(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_chats_cache_user_name ON user_chats_cache(user_id, chat_name)")

    conn.commit()
    conn.close()

# ===== User 操作 =====

def get_or_create_user(open_id, name):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE open_id = ?", (open_id,))
    user = c.fetchone()
    if user:
        return dict(user)
    c.execute("INSERT INTO users (open_id, name) VALUES (?, ?)", (open_id, name))
    conn.commit()
    user_id = c.lastrowid
    conn.close()
    return {"id": user_id, "open_id": open_id, "name": name}

def update_user_tokens(user_id, access_token, refresh_token, expires_in, refresh_expires_in):
    import time
    conn = get_db()
    conn.execute("""
        UPDATE users SET
            access_token = ?,
            refresh_token = ?,
            token_expires_at = ?,
            refresh_expires_at = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (
        access_token,
        refresh_token,
        time.time() + expires_in,
        time.time() + refresh_expires_in,
        user_id
    ))
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(user) if user else None

def update_user_name(user_id, name):
    conn = get_db()
    conn.execute("UPDATE users SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (name, user_id))
    conn.commit()
    conn.close()

# ===== Chat 配置操作 =====

def get_chats(user_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM chats WHERE user_id = ? ORDER BY COALESCE(latest_message_time, 0) DESC, updated_at DESC, id DESC", (user_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_chat(user_id, chat_id):
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM chats WHERE user_id = ? AND chat_id = ?", (user_id, chat_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def add_chat(user_id, chat_id, chat_name=None, local_cache=0, feishu_name=None):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO chats (user_id, chat_id, chat_name, local_cache, feishu_name) VALUES (?, ?, ?, ?, ?)",
            (user_id, chat_id, chat_name, 1 if local_cache else 0, feishu_name)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # 已存在则忽略
    conn.close()

def update_chat_feishu_name(user_id, chat_id, feishu_name):
    """刷新飞书真实群名（不触碰 chat_name / updated_at，避免影响列表排序）"""
    if not feishu_name:
        return
    conn = get_db()
    conn.execute(
        "UPDATE chats SET feishu_name = ? WHERE user_id = ? AND chat_id = ?",
        (feishu_name, user_id, chat_id)
    )
    conn.commit()
    conn.close()

def update_chat_names(user_id, chat_id, chat_name=None, feishu_name=None):
    """更新群聊自定义名称与/或飞书真实群名，并级联更新用户群聊搜索缓存表"""
    conn = get_db()
    sets = []
    params = []
    if chat_name is not None:
        sets.append("chat_name = ?")
        params.append(chat_name)
    if feishu_name is not None:
        sets.append("feishu_name = ?")
        params.append(feishu_name)
    if not sets:
        conn.close()
        return

    params.extend([user_id, chat_id])
    conn.execute(f"UPDATE chats SET {', '.join(sets)} WHERE user_id = ? AND chat_id = ?", params)

    # 联动更新 user_chats_cache 表中的群名
    cache_name_to_sync = feishu_name or chat_name
    if cache_name_to_sync:
        conn.execute("""
            UPDATE user_chats_cache 
            SET chat_name = ?, updated_at = CURRENT_TIMESTAMP 
            WHERE user_id = ? AND chat_id = ?
        """, (cache_name_to_sync, user_id, chat_id))

    conn.commit()
    conn.close()


def update_chat_local_cache(user_id, chat_id, local_cache):
    conn = get_db()
    conn.execute(
        "UPDATE chats SET local_cache = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND chat_id = ?",
        (1 if local_cache else 0, user_id, chat_id)
    )
    conn.commit()
    conn.close()

def delete_chat(user_id, chat_id):
    conn = get_db()
    conn.execute("DELETE FROM chats WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
    conn.commit()
    conn.close()

def update_chat_table_info(user_id, chat_id, base_token, table_id, base_url, chat_name=None):
    conn = get_db()
    fields = "base_token = ?, table_id = ?, base_url = ?, updated_at = CURRENT_TIMESTAMP"
    params = [base_token, table_id, base_url]
    if chat_name:
        fields += ", chat_name = ?"
        params.append(chat_name)
    params.extend([user_id, chat_id])
    conn.execute(
        f"UPDATE chats SET {fields} WHERE user_id = ? AND chat_id = ?",
        params
    )
    conn.commit()
    conn.close()

def update_chat_sync_status(user_id, chat_id, last_position, record_count):
    conn = get_db()
    conn.execute(
        "UPDATE chats SET last_synced_position = ?, record_count = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND chat_id = ?",
        (last_position, record_count, user_id, chat_id)
    )
    conn.commit()
    conn.close()

def update_chat_last_cached_position(user_id, chat_id, last_cached_position):
    conn = get_db()
    conn.execute(
        "UPDATE chats SET last_cached_position = ? WHERE user_id = ? AND chat_id = ?",
        (last_cached_position, user_id, chat_id)
    )
    conn.commit()
    conn.close()

def update_chat_latest_message_time(user_id, chat_id, latest_message_time):
    if not latest_message_time:
        return
    conn = get_db()
    conn.execute(
        "UPDATE chats SET latest_message_time = ? WHERE user_id = ? AND chat_id = ? AND (latest_message_time IS NULL OR latest_message_time < ?)",
        (latest_message_time, user_id, chat_id, latest_message_time)
    )
    conn.commit()
    conn.close()

# ===== 用户群聊列表缓存操作 =====

def save_user_chats_cache(user_id, chats_list):
    """保存/刷新当前用户的群聊缓存列表"""
    conn = get_db()
    try:
        user_row = conn.execute("SELECT open_id FROM users WHERE id = ?", (user_id,)).fetchone()
        user_open_id = user_row["open_id"] if user_row else ""

        # 使用事务：先清除当前用户的旧缓存，再批量写入最新群聊
        conn.execute("DELETE FROM user_chats_cache WHERE user_id = ?", (user_id,))
        for c in chats_list:
            chat_id = c.get("chat_id")
            if not chat_id:
                continue
            chat_name = (c.get("name") or c.get("chat_name") or "").strip()
            avatar = c.get("avatar") or ""
            description = c.get("description") or ""
            chat_status = c.get("chat_status") or "normal"
            owner_id = c.get("owner_id") or ""
            is_owner = 1 if (owner_id and user_open_id and owner_id == user_open_id) else 0
            conn.execute("""
                INSERT INTO user_chats_cache (user_id, chat_id, chat_name, avatar, description, chat_status, owner_id, is_owner, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (user_id, chat_id, chat_name, avatar, description, chat_status, owner_id, is_owner))
        conn.commit()
    finally:
        conn.close()

def get_user_chats_cache(user_id):
    """获取当前用户的所有已缓存群聊，按创建时间降序（最新创建排前），并标记已配置与群主状态"""
    conn = get_db()
    added_rows = conn.execute("SELECT chat_id FROM chats WHERE user_id = ?", (user_id,)).fetchall()
    added_ids = {r["chat_id"] for r in added_rows}

    rows = conn.execute("""
        SELECT chat_id, chat_name, avatar, description, chat_status, owner_id, is_owner, updated_at
        FROM user_chats_cache
        WHERE user_id = ?
        ORDER BY id DESC
    """, (user_id,)).fetchall()
    conn.close()

    result = []
    for r in rows:
        d = dict(r)
        d["name"] = d.get("chat_name") or ""
        d["is_added"] = d["chat_id"] in added_ids
        d["is_owner"] = bool(d.get("is_owner"))
        result.append(d)
    return result

def search_user_chats_cache(user_id, keyword):
    """根据群名称、部分群名称或 chat_id 检索已缓存的群聊，按相关度与创建时间降序排序"""
    if not keyword:
        return get_user_chats_cache(user_id)
    keyword = keyword.strip()
    conn = get_db()
    added_rows = conn.execute("SELECT chat_id FROM chats WHERE user_id = ?", (user_id,)).fetchall()
    added_ids = {r["chat_id"] for r in added_rows}

    kw_like = f"%{keyword}%"
    rows = conn.execute("""
        SELECT chat_id, chat_name, avatar, description, chat_status, owner_id, is_owner, updated_at
        FROM user_chats_cache
        WHERE user_id = ? AND (
            chat_name LIKE ? OR chat_id LIKE ?
        )
        ORDER BY 
            CASE WHEN chat_id = ? THEN 1
                 WHEN LOWER(chat_name) = LOWER(?) THEN 2
                 WHEN chat_name LIKE ? THEN 3
                 ELSE 4 END,
            id DESC
    """, (user_id, kw_like, kw_like, keyword, keyword, f"{keyword}%")).fetchall()
    conn.close()

    result = []
    for r in rows:
        d = dict(r)
        d["name"] = d.get("chat_name") or ""
        d["is_added"] = d["chat_id"] in added_ids
        d["is_owner"] = bool(d.get("is_owner"))
        result.append(d)
    return result

def get_user_chats_cache_last_updated(user_id):
    """获取当前用户群聊缓存的最新更新时间及群聊总数"""
    conn = get_db()
    row = conn.execute("""
        SELECT MAX(updated_at) as last_updated, COUNT(*) as count 
        FROM user_chats_cache 
        WHERE user_id = ?
    """, (user_id,)).fetchone()
    conn.close()
    if row:
        return {"last_updated": row["last_updated"], "count": row["count"]}
    return {"last_updated": None, "count": 0}
