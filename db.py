"""数据库连接 — MySQL 连接管理，session 和 skills 共享"""

import json
import os
from pathlib import Path

import pymysql

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
    cfg = dict(DB_CONFIG_DEFAULTS)
    cfg["host"] = os.environ.get("MYSQL_HOST", cfg["host"])
    cfg["port"] = int(os.environ.get("MYSQL_PORT", cfg["port"]))
    cfg["user"] = os.environ.get("MYSQL_USER", cfg["user"])
    cfg["password"] = os.environ.get("MYSQL_PASSWORD", cfg["password"])
    cfg["database"] = os.environ.get("MYSQL_DATABASE", cfg["database"])

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


def get_connection():
    """获取 MySQL 连接，自动创建数据库"""
    config = _load_db_config()
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
    return conn


def ensure_table(name: str, ddl: str):
    """创建表（如果不存在）"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
    finally:
        conn.close()


def try_connection() -> bool:
    """测试 MySQL 是否可用"""
    try:
        conn = get_connection()
        conn.close()
        return True
    except Exception:
        return False
