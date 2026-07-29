"""会话持久化 — MySQL 存储"""

import hashlib
import json
import time
from pathlib import Path

import pymysql

# ─── 数据库连接 ──────────────────────────────────────────

DB_CONFIG_DEFAULTS = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "",
    "database": "mini_agent",
    "charset": "utf8mb4",
}


def _load_db_config() -> dict:
    """从 ~/.mini-agent/config.json 加载数据库配置，环境变量优先"""
    import os
    cfg = dict(DB_CONFIG_DEFAULTS)
    cfg["host"] = os.environ.get("MYSQL_HOST", cfg["host"])
    cfg["port"] = int(os.environ.get("MYSQL_PORT", cfg["port"]))
    cfg["user"] = os.environ.get("MYSQL_USER", cfg["user"])
    cfg["password"] = os.environ.get("MYSQL_PASSWORD", cfg["password"])
    cfg["database"] = os.environ.get("MYSQL_DATABASE", cfg["database"])

    # 配置文件
    config_file = Path.home() / ".mini-agent" / "config.json"
    if config_file.is_file():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
            db = data.get("mysql", {})
            if isinstance(db, dict):
                for k in ("host", "user", "password", "database"):
                    if db.get(k):
                        cfg[k] = db[k]
                if db.get("port"):
                    cfg["port"] = int(db["port"])
        except Exception:
            pass
    return cfg


def _get_connection():
    """获取 MySQL 连接，自动创建数据库和表"""
    config = _load_db_config()
    # 先连接不指定 database，创建数据库
    db_name = config.pop("database")
    try:
        conn = pymysql.connect(**config,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
        )
    except Exception as e:
        raise RuntimeError(f"MySQL 连接失败 ({config['host']}:{config['port']}): {e}")

    with conn.cursor() as cur:
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
            f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    conn.select_db(db_name)

    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                project_hash VARCHAR(32) NOT NULL,
                model VARCHAR(100) DEFAULT '',
                messages JSON,
                stats JSON,
                saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_project (project_hash),
                INDEX idx_name (name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
    conn.commit()
    return conn


def _project_hash() -> str:
    return hashlib.sha256(str(Path.cwd()).encode()).hexdigest()[:16]


# ─── 会话 CRUD ──────────────────────────────────────────

def save_session(name: str, messages: list[dict], stats: dict | None = None,
                 model: str = "") -> int:
    """
    保存会话到 MySQL。返回会话 ID。
    同名会话会更新（UPSERT）。
    """
    conn = _get_connection()
    try:
        ph = _project_hash()
        msgs_json = json.dumps(messages, ensure_ascii=False)
        stats_json = json.dumps(stats or {}, ensure_ascii=False)

        with conn.cursor() as cur:
            # 检查是否存在同名+同项目
            cur.execute(
                "SELECT id FROM sessions WHERE name = %s AND project_hash = %s",
                (name, ph),
            )
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    "UPDATE sessions SET messages = %s, stats = %s, model = %s, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (msgs_json, stats_json, model, existing["id"]),
                )
                session_id = existing["id"]
            else:
                cur.execute(
                    "INSERT INTO sessions (name, project_hash, model, messages, stats) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (name, ph, model, msgs_json, stats_json),
                )
                session_id = cur.lastrowid
        conn.commit()
        return session_id
    finally:
        conn.close()


def load_session(name_or_id) -> dict | None:
    """
    加载会话。返回 {"id": ..., "name": ..., "messages": ..., "stats": ..., ...} 或 None。
    可按名称或 ID 查找。
    """
    conn = _get_connection()
    try:
        ph = _project_hash()
        with conn.cursor() as cur:
            # 尝试按 ID
            if isinstance(name_or_id, int):
                cur.execute(
                    "SELECT * FROM sessions WHERE id = %s AND project_hash = %s",
                    (name_or_id, ph),
                )
            else:
                cur.execute(
                    "SELECT * FROM sessions WHERE name = %s AND project_hash = %s",
                    (name_or_id, ph),
                )
            row = cur.fetchone()
            if not row:
                return None
            row["messages"] = json.loads(row.get("messages", "[]")) if isinstance(row.get("messages"), str) else row.get("messages", [])
            row["stats"] = json.loads(row.get("stats", "{}")) if isinstance(row.get("stats"), str) else row.get("stats", {})
            # 转换时间
            if row.get("saved_at"):
                row["saved_at"] = row["saved_at"].timestamp()
            return row
    finally:
        conn.close()


def list_sessions() -> list[dict]:
    """列出当前项目的所有会话（按更新时间倒序）"""
    conn = _get_connection()
    try:
        ph = _project_hash()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, model, saved_at, updated_at, "
                "JSON_LENGTH(messages) AS message_count "
                "FROM sessions WHERE project_hash = %s "
                "ORDER BY updated_at DESC LIMIT 50",
                (ph,),
            )
            rows = cur.fetchall()
            result = []
            for r in rows:
                result.append({
                    "id": r["id"],
                    "name": r["name"],
                    "model": r.get("model", ""),
                    "saved_at": r["saved_at"].timestamp() if r.get("saved_at") else 0,
                    "message_count": r.get("message_count", 0),
                })
            return result
    finally:
        conn.close()


def delete_session(name_or_id) -> bool:
    """删除一个会话"""
    conn = _get_connection()
    try:
        ph = _project_hash()
        with conn.cursor() as cur:
            if isinstance(name_or_id, int):
                cur.execute(
                    "DELETE FROM sessions WHERE id = %s AND project_hash = %s",
                    (name_or_id, ph),
                )
            else:
                cur.execute(
                    "DELETE FROM sessions WHERE name = %s AND project_hash = %s",
                    (name_or_id, ph),
                )
            conn.commit()
            return cur.rowcount > 0
    finally:
        conn.close()
