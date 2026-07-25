# -*- coding: utf-8 -*-
"""SQLite 连接与建库。全项目唯一的建库入口。
schema.sql(冻结,5张表)负责基础表;fundamental / news_* 等扩展表由各模块用
CREATE TABLE IF NOT EXISTS 追加(不改冻结的 schema.sql)。
"""
import sqlite3
from conf import DB_DIR, DB_PATH, SCHEMA_PATH


def get_conn(db_path=None) -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path or DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn=None):
    """执行 schema.sql 建基础表。幂等(全部 IF NOT EXISTS)。"""
    own = conn is None
    if own:
        conn = get_conn()
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()
    if own:
        conn.close()


def ensure_table(ddl: str, conn=None):
    """扩展表建表(fundamental / news_raw / news_signal)。ddl 需为 IF NOT EXISTS。"""
    own = conn is None
    if own:
        conn = get_conn()
    conn.executescript(ddl)
    conn.commit()
    if own:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("DB initialized at", DB_PATH)
