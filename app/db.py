"""织云系统 - SQLite 数据层。

生命周期状态机（只能向前流转，下线为终态）：
    在研 developing -> 上线 online -> 维保 maintenance -> 下线 offline(终态)
"""
import os
import sqlite3
import threading

DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "zhiyun.db")
)

ENVIRONMENTS = ["dev", "test", "staging", "prod"]
ENV_LABELS = {"dev": "开发", "test": "测试", "staging": "预发", "prod": "生产"}

STATUSES = ["developing", "online", "maintenance", "offline"]
STATUS_LABELS = {
    "developing": "在研",
    "online": "上线",
    "maintenance": "维保",
    "offline": "下线",
}
STATUS_ORDER = {s: i for i, s in enumerate(STATUSES)}
TERMINAL_STATUS = "offline"

CLUSTERS = ["华东1集群", "华北2集群", "华南1集群", "西南灾备集群"]

# 配置档案（按 应用 + 环境 管理）
CONFIG_TYPES = ["string", "number", "boolean", "json"]
CONFIG_TYPE_LABELS = {"string": "字符串", "number": "数字", "boolean": "布尔", "json": "JSON"}
# 生效范围
CONFIG_SCOPES = ["global", "cluster", "canary"]
CONFIG_SCOPE_LABELS = {"global": "全局", "cluster": "集群", "canary": "灰度"}
# 布尔值归一化后的存储形态
BOOL_TRUE = {"true", "1", "yes", "on", "是", "开"}

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS business_lines (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    role             TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member')),
    business_line_id INTEGER REFERENCES business_lines(id),
    token            TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS applications (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    business_line_id INTEGER NOT NULL REFERENCES business_lines(id),
    owner_id         INTEGER REFERENCES users(id),
    cluster          TEXT NOT NULL,
    environment      TEXT NOT NULL CHECK (environment IN ('dev','test','staging','prod')),
    status           TEXT NOT NULL DEFAULT 'developing'
                     CHECK (status IN ('developing','online','maintenance','offline')),
    description      TEXT NOT NULL DEFAULT '',
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    UNIQUE (business_line_id, name)
);

CREATE TABLE IF NOT EXISTS env_vars (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    key    TEXT NOT NULL,
    value  TEXT NOT NULL DEFAULT '',
    UNIQUE (app_id, key)
);

CREATE TABLE IF NOT EXISTS change_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id     INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    user_id    INTEGER REFERENCES users(id),
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

-- 配置档案：配置项按 应用 + 环境 管理（键、值、类型、生效范围、是否密文）
CREATE TABLE IF NOT EXISTS config_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    environment TEXT NOT NULL CHECK (environment IN ('dev','test','staging','prod')),
    key         TEXT NOT NULL,
    value       TEXT NOT NULL DEFAULT '',   -- 密文同样落库（内部系统演示，无外部 KMS），接口默认不回传明文
    value_type  TEXT NOT NULL DEFAULT 'string'
                CHECK (value_type IN ('string','number','boolean','json')),
    scope       TEXT NOT NULL DEFAULT 'global'
                CHECK (scope IN ('global','cluster','canary')),
    is_secret   INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0,1)),
    updated_by  INTEGER REFERENCES users(id),
    updated_at  INTEGER NOT NULL,
    UNIQUE (app_id, environment, key)
);

-- 配置版本：每次保存产生一个全量快照；回滚 = 追加新版本，绝不改写历史版本
CREATE TABLE IF NOT EXISTS config_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    environment TEXT NOT NULL,
    version     INTEGER NOT NULL,          -- 该 应用+环境 内自增
    snapshot    TEXT NOT NULL,             -- JSON 全量快照（密文存明文，仅回滚/版本对比内部使用）
    change_note TEXT NOT NULL DEFAULT '',
    created_by  INTEGER REFERENCES users(id),
    created_at  INTEGER NOT NULL,
    UNIQUE (app_id, environment, version)
);

-- 配置留痕：逐键流水（改前/改后/操作人/理由），只追加，不更新不删除
CREATE TABLE IF NOT EXISTS config_audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    environment TEXT NOT NULL,
    version_id  INTEGER REFERENCES config_versions(id) ON DELETE SET NULL,
    user_id     INTEGER REFERENCES users(id),
    action      TEXT NOT NULL,             -- add/update/remove/rollback/reveal
    config_key  TEXT NOT NULL DEFAULT '',
    old_value   TEXT,
    new_value   TEXT,
    is_secret   INTEGER NOT NULL DEFAULT 0,
    reason      TEXT NOT NULL DEFAULT '',  -- reveal 强制填写；rollback 记录目标版本
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_apps_bl ON applications(business_line_id);
CREATE INDEX IF NOT EXISTS idx_apps_owner ON applications(owner_id);
CREATE INDEX IF NOT EXISTS idx_logs_app ON change_logs(app_id);
CREATE INDEX IF NOT EXISTS idx_logs_time ON change_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_cfg_app_env ON config_items(app_id, environment);
CREATE INDEX IF NOT EXISTS idx_ver_app_env ON config_versions(app_id, environment);
CREATE INDEX IF NOT EXISTS idx_audit_app ON config_audit_logs(app_id);
CREATE INDEX IF NOT EXISTS idx_audit_time ON config_audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON config_audit_logs(action);
"""

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _local.conn = conn
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()


def query(sql: str, params: tuple = ()) -> list:
    return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()):
    return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    cur = get_conn().execute(sql, params)
    get_conn().commit()
    return cur
