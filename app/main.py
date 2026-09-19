"""织云系统 - 应用台账 & 资产控制台 API。"""
import os
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import (
    User, can, err, get_app_checked, get_app_or_404, is_admin, require_perm,
    require_visibility, role_label, visibility_sql,
)
from .db import (
    CLUSTERS, ENV_LABELS, ENVIRONMENTS, ROLE_LABELS, STATUS_LABELS, STATUS_ORDER,
    STATUSES, TERMINAL_STATUS, execute, get_conn, init_db, query, query_one,
)
from .routers import access as access_router
from .routers import config as config_router
from .seed import seed_if_empty

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="织云系统", docs_url=None, redoc_url=None)
app.include_router(config_router.router)
app.include_router(access_router.router)


@app.on_event("startup")
def startup() -> None:
    init_db()
    seed_if_empty()


# ---------------------------------------------------------------- 基础工具

def log_change(app_id: int, user_id: int, action: str, detail: str) -> None:
    execute(
        "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) VALUES (?,?,?,?,?)",
        (app_id, user_id, action, detail, int(time.time())),
    )


def touch(app_id: int) -> None:
    execute("UPDATE applications SET updated_at = ? WHERE id = ?", (int(time.time()), app_id))


def app_to_dict(row, with_env: bool = False) -> dict:
    app_id = row["id"]
    owner = query_one("SELECT id, name FROM users WHERE id = ?", (row["owner_id"],)) if row["owner_id"] else None
    bl = query_one("SELECT id, name FROM business_lines WHERE id = ?", (row["business_line_id"],))
    env_vars = query("SELECT key, value FROM env_vars WHERE app_id = ? ORDER BY key", (app_id,))
    missing_owner = row["owner_id"] is None
    missing_env = len(env_vars) == 0
    data = {
        "id": app_id,
        "name": row["name"],
        "business_line_id": row["business_line_id"],
        "business_line_name": bl["name"] if bl else "",
        "owner_id": row["owner_id"],
        "owner_name": owner["name"] if owner else None,
        "cluster": row["cluster"],
        "environment": row["environment"],
        "environment_label": ENV_LABELS[row["environment"]],
        "status": row["status"],
        "status_label": STATUS_LABELS[row["status"]],
        "description": row["description"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "env_var_count": len(env_vars),
        "red_dots": ([{"type": "missing_owner", "label": "缺失负责人"}] if missing_owner else [])
                    + ([{"type": "missing_env", "label": "环境变量缺失"}] if missing_env else []),
    }
    if with_env:
        data["env_vars"] = [dict(v) for v in env_vars]
    return data


# ---------------------------------------------------------------- 请求模型

class LoginIn(BaseModel):
    username: str


class AppCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    business_line_id: int
    owner_id: int | None = None
    cluster: str
    environment: str
    description: str = ""


class AppUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    owner_id: int | None = None
    set_owner: bool = False          # 显式区分"不修改"与"清空负责人"
    cluster: str | None = None
    environment: str | None = None
    description: str | None = None


class StatusIn(BaseModel):
    status: str


class EnvVarsIn(BaseModel):
    vars: list[dict]


# ---------------------------------------------------------------- 认证

@app.post("/api/login")
def login(body: LoginIn):
    row = query_one(
        """SELECT u.id, u.username, u.name, u.role, u.token, u.business_line_id, u.active,
                  b.name AS business_line_name
           FROM users u LEFT JOIN business_lines b ON b.id = u.business_line_id
           WHERE u.username = ?""",
        (body.username.strip(),),
    )
    if not row:
        raise err(401, "用户不存在")
    if not row["active"]:
        raise err(403, f"账号 {row['name']} 已停用（离职/转岗交接完成），请联系平台管理员")
    row = dict(row)
    row.pop("active")
    row["role_label"] = ROLE_LABELS.get(row["role"], row["role"])
    return {"token": row.pop("token"), "user": row}


@app.get("/api/public/users")
def public_users():
    """登录页可选账号列表（内部系统演示，不暴露令牌；停用账号不展示）。"""
    rows = query(
        """SELECT u.id, u.username, u.name, u.role, b.name AS business_line_name
           FROM users u LEFT JOIN business_lines b ON b.id = u.business_line_id
           WHERE u.active = 1
           ORDER BY CASE u.role WHEN 'admin' THEN 0 WHEN 'bl_owner' THEN 1
                                WHEN 'app_owner' THEN 2 ELSE 3 END, u.id"""
    )
    return [dict(r) | {"role_label": ROLE_LABELS.get(r["role"], r["role"])} for r in rows]


@app.get("/api/me")
def me(user: dict = User):
    user["role_label"] = role_label(user)
    return user


# ---------------------------------------------------------------- 元数据

@app.get("/api/meta")
def meta(user: dict = User):
    return {
        "environments": [{"value": e, "label": ENV_LABELS[e]} for e in ENVIRONMENTS],
        "statuses": [{"value": s, "label": STATUS_LABELS[s]} for s in STATUSES],
        "clusters": CLUSTERS,
        "roles": [{"value": k, "label": v} for k, v in ROLE_LABELS.items()],
    }


@app.get("/api/business-lines")
def business_lines(user: dict = User):
    if is_admin(user):
        rows = query("SELECT id, name, code FROM business_lines ORDER BY id")
    else:
        # 只列出授权范围实际覆盖到的业务线（至少一个环境可见）
        visible = {a["business_line_id"] for a in user.get("access", [])}
        if visible:
            placeholders = ",".join("?" * len(visible))
            rows = query(
                f"SELECT id, name, code FROM business_lines WHERE id IN ({placeholders}) ORDER BY id",
                tuple(sorted(visible)),
            )
        else:
            rows = []
    return [dict(r) for r in rows]


@app.get("/api/users")
def users(user: dict = User, business_line_id: int | None = None):
    sql = """SELECT u.id, u.name, u.username, u.role, u.active, u.business_line_id,
                    b.name AS business_line_name
             FROM users u LEFT JOIN business_lines b ON b.id = u.business_line_id"""
    params: list = []
    if not is_admin(user):
        # 非管理员只能看自己业务线的人（负责人下拉/交接选人够用）
        sql += " WHERE u.business_line_id = ?"
        params.append(user["business_line_id"])
    elif business_line_id:
        sql += " WHERE u.business_line_id = ?"
        params.append(business_line_id)
    sql += " ORDER BY u.id"
    out = []
    for r in query(sql, tuple(params)):
        d = dict(r)
        d["role_label"] = ROLE_LABELS.get(r["role"], r["role"])
        out.append(d)
    return out


# ---------------------------------------------------------------- 应用台账

@app.get("/api/apps")
def list_apps(user: dict = User,
              business_line_id: int | None = None,
              owner_id: int | None = None,
              environment: str | None = None,
              status: str | None = None,
              q: str | None = None):
    sql = "SELECT * FROM applications WHERE 1=1"
    params: list = []
    if environment and environment not in ENVIRONMENTS:
        raise err(400, f"非法环境：{environment}，可选：{'/'.join(ENVIRONMENTS)}")
    if is_admin(user):
        if business_line_id:
            sql += " AND business_line_id = ?"
            params.append(business_line_id)
    else:
        # 显式按未授权业务线/环境筛选属于越权：403 说明原因，而非静默给空列表
        if business_line_id:
            require_visibility(user, business_line_id, environment)
        elif environment and not any(a["environment"] == environment for a in user.get("access", [])):
            raise err(403,
                      f"无权查看任何业务线的「{ENV_LABELS[environment]}」环境："
                      f"账号 {user['name']} 的授权范围不包含该环境")
        # 业务线×环境 两级收窄（列名必须带表别名，否则相关子查询会绑定到 user_access 的同名列而恒真）
        frag, fparams = visibility_sql(user, "applications.business_line_id", "applications.environment")
        sql += frag
        params.extend(fparams)
    if owner_id:
        sql += " AND owner_id = ?"
        params.append(owner_id)
    if environment:
        sql += " AND environment = ?"
        params.append(environment)
    if status:
        if status not in STATUSES:
            raise err(400, f"非法状态：{status}，可选：{'/'.join(STATUSES)}")
        sql += " AND status = ?"
        params.append(status)
    if q:
        sql += " AND name LIKE ?"
        params.append(f"%{q.strip()}%")
    sql += " ORDER BY updated_at DESC, id DESC"
    return [app_to_dict(r) for r in query(sql, tuple(params))]


@app.post("/api/apps", status_code=201)
def create_app(body: AppCreateIn, user: dict = User):
    # 新建应用需要目标 业务线×环境 的应用管理权
    require_perm(user, "can_manage_app", body.business_line_id, body.environment)
    if body.environment not in ENVIRONMENTS:
        raise err(400, f"非法环境：{body.environment}")
    if body.cluster not in CLUSTERS:
        raise err(400, f"非法集群：{body.cluster}，可选：{'、'.join(CLUSTERS)}")
    if body.owner_id is not None:
        owner = query_one("SELECT id, business_line_id, active FROM users WHERE id = ?", (body.owner_id,))
        if not owner:
            raise err(400, "负责人不存在")
        if not owner["active"]:
            raise err(400, "负责人账号已停用，不能被指定为应用负责人")
        if owner["business_line_id"] != body.business_line_id:
            raise err(400, "负责人必须属于应用所在业务线")
    dup = query_one("SELECT id FROM applications WHERE business_line_id = ? AND name = ?",
                    (body.business_line_id, body.name.strip()))
    if dup:
        raise err(409, f"同一业务线下应用名不能重复：「{body.name.strip()}」已存在（应用 #{dup['id']}）")
    now = int(time.time())
    cur = execute(
        """INSERT INTO applications
           (name, business_line_id, owner_id, cluster, environment, status,
            description, created_at, updated_at)
           VALUES (?,?,?,?,?,'developing',?,?,?)""",
        (body.name.strip(), body.business_line_id, body.owner_id, body.cluster,
         body.environment, body.description.strip(), now, now),
    )
    log_change(cur.lastrowid, user["id"], "创建应用", f"应用「{body.name.strip()}」创建，初始状态：在研")
    return app_to_dict(get_app_or_404(cur.lastrowid), with_env=True)


@app.get("/api/apps/{app_id}")
def app_detail(app_id: int, user: dict = User):
    app_row = get_app_checked(user, app_id)
    data = app_to_dict(app_row, with_env=True)
    data["permissions"] = {
        "can_view_secret": can(user, "can_view_secret", app_row["business_line_id"], app_row["environment"]),
        "can_edit_config": can(user, "can_edit_config", app_row["business_line_id"], app_row["environment"]),
        "can_manage_app": can(user, "can_manage_app", app_row["business_line_id"], app_row["environment"]),
    }
    logs = query(
        """SELECT l.action, l.detail, l.created_at, u.name AS user_name
           FROM change_logs l LEFT JOIN users u ON u.id = l.user_id
           WHERE l.app_id = ? ORDER BY l.created_at DESC, l.id DESC LIMIT 50""",
        (app_id,),
    )
    data["change_logs"] = [dict(r) for r in logs]
    return data


@app.patch("/api/apps/{app_id}")
def update_app(app_id: int, body: AppUpdateIn, user: dict = User):
    app_row = get_app_checked(user, app_id)
    require_perm(user, "can_manage_app", app_row["business_line_id"], app_row["environment"])
    # 改挂到另一个环境：目标环境同样要有应用管理权
    if body.environment is not None and body.environment != app_row["environment"]:
        require_perm(user, "can_manage_app", app_row["business_line_id"], body.environment)
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），所有信息只读，禁止修改")
    changes = []
    if body.name is not None and body.name.strip() != app_row["name"]:
        dup = query_one("SELECT id FROM applications WHERE business_line_id = ? AND name = ? AND id != ?",
                        (app_row["business_line_id"], body.name.strip(), app_id))
        if dup:
            raise err(409, f"同一业务线下应用名不能重复：「{body.name.strip()}」已存在（应用 #{dup['id']}）")
        changes.append(("name", body.name.strip(), f"应用更名：{app_row['name']} → {body.name.strip()}"))
    if body.set_owner:
        new_owner = body.owner_id
        if new_owner is not None:
            owner = query_one("SELECT id, name, business_line_id, active FROM users WHERE id = ?", (new_owner,))
            if not owner:
                raise err(400, "负责人不存在")
            if not owner["active"]:
                raise err(400, "该账号已停用，不能设为负责人；如为离职交接请使用「应用交接」功能")
            if owner["business_line_id"] != app_row["business_line_id"]:
                raise err(400, "负责人必须属于应用所在业务线；跨人员移交请走「应用交接」并留痕")
        if new_owner != app_row["owner_id"]:
            old = query_one("SELECT name FROM users WHERE id = ?", (app_row["owner_id"],)) if app_row["owner_id"] else None
            new = query_one("SELECT name FROM users WHERE id = ?", (new_owner,)) if new_owner else None
            changes.append(("owner_id", new_owner,
                            f"负责人变更（未走交接流程）：{old['name'] if old else '（空）'} → {new['name'] if new else '（空）'}"))
    if body.cluster is not None and body.cluster != app_row["cluster"]:
        if body.cluster not in CLUSTERS:
            raise err(400, f"非法集群：{body.cluster}")
        changes.append(("cluster", body.cluster, f"集群变更：{app_row['cluster']} → {body.cluster}"))
    if body.environment is not None and body.environment != app_row["environment"]:
        if body.environment not in ENVIRONMENTS:
            raise err(400, f"非法环境：{body.environment}")
        changes.append(("environment", body.environment,
                        f"环境变更：{ENV_LABELS[app_row['environment']]} → {ENV_LABELS[body.environment]}"))
    if body.description is not None and body.description.strip() != app_row["description"]:
        changes.append(("description", body.description.strip(), "更新应用描述"))
    for field, value, _log in changes:
        execute(f"UPDATE applications SET {field} = ? WHERE id = ?", (value, app_id))
    for _field, _value, log_text in changes:
        log_change(app_id, user["id"], "信息变更", log_text)
    if changes:
        touch(app_id)
    return app_to_dict(get_app_or_404(app_id), with_env=True)


@app.post("/api/apps/{app_id}/status")
def change_status(app_id: int, body: StatusIn, user: dict = User):
    app_row = get_app_checked(user, app_id)
    require_perm(user, "can_manage_app", app_row["business_line_id"], app_row["environment"])
    old, new = app_row["status"], body.status
    if new not in STATUSES:
        raise err(400, f"非法状态：{new}，可选：{'/'.join(STATUSES)}")
    if old == TERMINAL_STATUS:
        raise err(400, "应用已下线，「下线」为生命周期终态，不能再做任何状态变更")
    if new == old:
        raise err(400, f"应用已处于「{STATUS_LABELS[old]}」状态，无需变更")
    if STATUS_ORDER[new] < STATUS_ORDER[old]:
        raise err(400,
                  f"非法状态回退：不允许从「{STATUS_LABELS[old]}」回退到「{STATUS_LABELS[new]}」。"
                  f"生命周期只能向前流转：在研 → 上线 → 维保 → 下线")
    execute("UPDATE applications SET status = ? WHERE id = ?", (new, app_id))
    touch(app_id)
    log_change(app_id, user["id"], "状态变更",
               f"{STATUS_LABELS[old]} → {STATUS_LABELS[new]}")
    return app_to_dict(get_app_or_404(app_id), with_env=True)


@app.put("/api/apps/{app_id}/env-vars")
def put_env_vars(app_id: int, body: EnvVarsIn, user: dict = User):
    app_row = get_app_checked(user, app_id)
    require_perm(user, "can_manage_app", app_row["business_line_id"], app_row["environment"])
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），环境变量只读，禁止修改")
    seen: set = set()
    cleaned = []
    for item in body.vars:
        key = str(item.get("key", "")).strip()
        if not key:
            continue
        if key in seen:
            raise err(400, f"环境变量 key 重复：{key}")
        seen.add(key)
        cleaned.append((key, str(item.get("value", ""))))
    conn = get_conn()
    conn.execute("DELETE FROM env_vars WHERE app_id = ?", (app_id,))
    conn.executemany("INSERT INTO env_vars (app_id, key, value) VALUES (?,?,?)",
                     [(app_id, k, v) for k, v in cleaned])
    conn.commit()
    touch(app_id)
    log_change(app_id, user["id"], "环境变量变更", f"环境变量更新为 {len(cleaned)} 项")
    return app_to_dict(get_app_or_404(app_id), with_env=True)


# ---------------------------------------------------------------- 资产控制台

@app.get("/api/console/summary")
def console_summary(user: dict = User):
    # 业务线×环境 可见范围片段（控制台所有统计都以此收窄）
    scope_frag, scope_params = ("", []) if is_admin(user) else visibility_sql(
        user, "a.business_line_id", "a.environment")

    bl_stats_sql = """SELECT b.id, b.name,
                   COUNT(a.id) AS total,
                   SUM(CASE WHEN a.status = 'developing'   THEN 1 ELSE 0 END) AS developing,
                   SUM(CASE WHEN a.status = 'online'       THEN 1 ELSE 0 END) AS online,
                   SUM(CASE WHEN a.status = 'maintenance'  THEN 1 ELSE 0 END) AS maintenance,
                   SUM(CASE WHEN a.status = 'offline'      THEN 1 ELSE 0 END) AS offline
            FROM business_lines b
            LEFT JOIN applications a ON a.business_line_id = b.id {scope}
            GROUP BY b.id ORDER BY total DESC, b.id"""
    if is_admin(user):
        by_bl = query(bl_stats_sql.format(scope=""))
    else:
        by_bl = query(bl_stats_sql.format(scope="WHERE 1=1" + scope_frag),
                      tuple(scope_params))

    week_ago = int(time.time()) - 7 * 86400
    recent = query(
        f"""SELECT a.id, a.name, b.name AS business_line_name, a.status,
                   MAX(l.created_at) AS last_changed_at, COUNT(l.id) AS change_count
            FROM change_logs l
            JOIN applications a ON a.id = l.app_id
            JOIN business_lines b ON b.id = a.business_line_id
            WHERE l.created_at >= ? {scope_frag}
            GROUP BY a.id ORDER BY last_changed_at DESC LIMIT 20""",
        (week_ago, *scope_params),
    )

    apps = query(f"SELECT a.* FROM applications a WHERE 1=1 {scope_frag}", tuple(scope_params))
    missing_owner, missing_env = [], []
    for row in apps:
        if row["owner_id"] is None:
            missing_owner.append(app_to_dict(row))
        if not query_one("SELECT id FROM env_vars WHERE app_id = ? LIMIT 1", (row["id"],)):
            missing_env.append(app_to_dict(row))

    visible_bl_count = len({r["id"] for r in by_bl if r["total"] > 0}) if not is_admin(user) else len(by_bl)
    return {
        "by_business_line": [dict(r) for r in by_bl],
        "recent_changed_apps": [dict(r) for r in recent],
        "red_dots": {
            "missing_owner": missing_owner,
            "missing_env": missing_env,
        },
        "totals": {
            "apps": len(apps),
            "business_lines": len(by_bl) if is_admin(user) else visible_bl_count,
            "recent_changed": len(recent),
            "red_dot_apps": len({a["id"] for a in missing_owner} | {a["id"] for a in missing_env}),
        },
    }


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 静态页面

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
