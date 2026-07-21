import sqlite3
import os
from config import Config

def get_db():
    conn = sqlite3.connect(Config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

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
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            UNIQUE(user_id, chat_id)
        )
    """)

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
        "SELECT * FROM chats WHERE user_id = ? ORDER BY created_at", (user_id,)
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

def add_chat(user_id, chat_id, chat_name=None):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO chats (user_id, chat_id, chat_name) VALUES (?, ?, ?)",
            (user_id, chat_id, chat_name)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass  # 已存在则忽略
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
