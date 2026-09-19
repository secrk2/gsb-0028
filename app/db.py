"""织云系统 - SQLite 数据层。

生命周期状态机（只能向前流转，下线为终态）：
    在研 developing -> 上线 online -> 维保 maintenance -> 下线 offline(终态)

权限模型（业务线 × 环境 两级收窄）：
    角色 role：admin 平台管理员 / bl_owner 业务线负责人 / app_owner 应用负责人 / observer 只读观察者
    user_access：某用户在「业务线 × 环境」上的可见与授权记录；无任何记录 = 不可见；
                 can_view_secret（密文查看权）与 can_edit_config（配置编辑权）分开授予，
                 can_manage_app（应用归属/台账管理权）决定应用交接等操作。
"""
import os
import sqlite3
import threading

DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "zhiyun.db")
)

ENVIRONMENTS = ["dev", "test", "staging", "prod"]
ENV_LABELS = {"dev": "开发", "test": "测试", "staging": "预发", "prod": "生产"}
# 环境在发布链上的次序：授予某环境时，默认同时可见其上游（非生产）环境
ENV_ORDER = {e: i for i, e in enumerate(ENVIRONMENTS)}

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

# 四类角色
ROLES = ["admin", "bl_owner", "app_owner", "observer"]
ROLE_LABELS = {
    "admin": "平台管理员",
    "bl_owner": "业务线负责人",
    "app_owner": "应用负责人",
    "observer": "只读观察者",
}
# 老库 member 角色迁移映射
LEGACY_ROLE_MAP = {"member": "app_owner"}

# 三项独立授权位（密文查看权 ≠ 配置编辑权）
PERM_LABELS = {
    "can_view_secret": "密文查看",
    "can_edit_config": "配置编辑",
    "can_manage_app": "应用管理",
}

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
    role             TEXT NOT NULL DEFAULT 'app_owner'
                     CHECK (role IN ('admin','bl_owner','app_owner','observer')),
    business_line_id INTEGER REFERENCES business_lines(id),
    active           INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),  -- 离职停用
    token            TEXT NOT NULL UNIQUE
);

-- 可见范围与授权：业务线 × 环境 一条；同一业务线可多条覆盖不同环境
CREATE TABLE IF NOT EXISTS user_access (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    business_line_id INTEGER NOT NULL REFERENCES business_lines(id),
    environment      TEXT NOT NULL CHECK (environment IN ('dev','test','staging','prod')),
    can_view_secret  INTEGER NOT NULL DEFAULT 0 CHECK (can_view_secret IN (0,1)),
    can_edit_config  INTEGER NOT NULL DEFAULT 0 CHECK (can_edit_config IN (0,1)),
    can_manage_app   INTEGER NOT NULL DEFAULT 0 CHECK (can_manage_app IN (0,1)),
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    UNIQUE (user_id, business_line_id, environment)
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

-- 交接留痕：应用归属（owner）与管理权随应用一并移交；配置项挂在应用上自然随之移交
CREATE TABLE IF NOT EXISTS handover_records (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id            INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    from_user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    to_user_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    operator_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    reason            TEXT NOT NULL DEFAULT '',
    permissions_note  TEXT NOT NULL DEFAULT '',  -- 交接时权限范围如何处理的文字说明
    effective_at      INTEGER NOT NULL,
    created_at        INTEGER NOT NULL
);

-- 权限变更留痕：角色调整、授权范围增删改、离职停用/交接，全部只追加
CREATE TABLE IF NOT EXISTS permission_logs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id          INTEGER REFERENCES users(id) ON DELETE SET NULL,
    target_user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    business_line_id  INTEGER REFERENCES business_lines(id) ON DELETE SET NULL,
    environment       TEXT,
    action            TEXT NOT NULL,       -- role_grant/access_grant/access_update/access_revoke/deactivate/handover
    detail            TEXT NOT NULL DEFAULT '',
    reason            TEXT NOT NULL DEFAULT '',
    created_at        INTEGER NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_access_user ON user_access(user_id);
CREATE INDEX IF NOT EXISTS idx_handover_app ON handover_records(app_id);
CREATE INDEX IF NOT EXISTS idx_permlog_target ON permission_logs(target_user_id);
CREATE INDEX IF NOT EXISTS idx_permlog_time ON permission_logs(created_at);
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
    _migrate(conn)
    # _migrate 可能已重连（writable_schema 改写 users 后旧连接缓存失效），
    # 统一取当前线程连接提交；随后关闭并丢弃，让首次业务请求以最新 schema 重新连接
    conn = get_conn()
    conn.commit()
    conn.close()
    try:
        del _local.conn
    except AttributeError:
        pass


def _migrate(conn: sqlite3.Connection) -> None:
    """老库（admin/member 两角色、无授权表）平滑迁移到四类角色 + 授权范围模型。"""
    import time as _t
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "active" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    # 旧 CHECK 约束只允许 admin/member。SQLite 无法直接 ALTER 约束：
    # 用 writable_schema 重写 users 的建表 SQL（表名不变，applications 等外键引用不受影响），
    # 避免"重命名重建表"导致外键引用悬空。
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    need_rewrite = row and "app_owner" not in (row["sql"] or "")
    if need_rewrite:
        # 列顺序必须与 ALTER ADD COLUMN 后的物理布局一致（active 追加在 token 之后）
        new_sql = (
            "CREATE TABLE users ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "username TEXT NOT NULL UNIQUE, "
            "name TEXT NOT NULL, "
            "role TEXT NOT NULL DEFAULT 'app_owner' "
            "CHECK (role IN ('admin','bl_owner','app_owner','observer')), "
            "business_line_id INTEGER REFERENCES business_lines(id), "
            "token TEXT NOT NULL UNIQUE, "
            "active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)))"
        )
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("UPDATE sqlite_master SET sql = ? WHERE type='table' AND name='users'",
                     (new_sql,))
        conn.execute("PRAGMA writable_schema=OFF")
        conn.commit()
        conn.close()
        try:
            del _local.conn
        except AttributeError:
            pass
        # 新连接会按改写后的 CREATE TABLE 解析 CHECK 约束
        conn = get_conn()
        # 老 member 统一归入"应用负责人"角色
        conn.execute("UPDATE users SET role = 'app_owner' WHERE role NOT IN ('admin')")

    # 老库成员：按所属业务线补齐四个环境的可见 + 全部授权位，
    # 保持升级前"本业务线全权"的行为不回退；细粒度收口由管理员之后调整。
    legacy = conn.execute(
        "SELECT DISTINCT u.id, u.business_line_id FROM users u "
        "WHERE u.role = 'app_owner' AND u.business_line_id IS NOT NULL"
    ).fetchall()
    migrated = False
    now = int(_t.time())
    for u in legacy:
        for env in ENVIRONMENTS:
            cur = conn.execute(
                """INSERT OR IGNORE INTO user_access
                   (user_id, business_line_id, environment,
                    can_view_secret, can_edit_config, can_manage_app, created_at, updated_at)
                   VALUES (?,?,?,1,1,1,?,?)""",
                (u["id"], u["business_line_id"], env, now, now),
            )
            migrated = migrated or cur.rowcount > 0
    if migrated:
        conn.execute(
            """INSERT INTO permission_logs
               (actor_id, target_user_id, business_line_id, environment, action, detail, reason, created_at)
               SELECT NULL, u.id, u.business_line_id, NULL, 'access_grant',
                      '系统迁移：老版本成员默认获得本业务线四环境可见与密文查看/配置编辑/应用管理权',
                      '版本升级自动迁移', ?
               FROM users u WHERE u.role = 'app_owner'""",
            (now,),
        )
    conn.commit()


def query(sql: str, params: tuple = ()) -> list:
    return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()):
    return get_conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    cur = get_conn().execute(sql, params)
    get_conn().commit()
    return cur
