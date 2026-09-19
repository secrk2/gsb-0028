"""织云系统 - 配置档案 & 变更留痕 API。

所有接口都走 current_user 鉴权与业务线数据范围校验；
密文相关规则在服务端强制执行（仅前端拦截不算数）。
"""
import time
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import config_service as svc
from ..auth import User, check_bl_scope, err, get_app_checked
from ..db import (
    CONFIG_SCOPE_LABELS, CONFIG_SCOPES, CONFIG_TYPE_LABELS, CONFIG_TYPES,
    ENVIRONMENTS, TERMINAL_STATUS, query, query_one,
)

router = APIRouter()

REVEAL_REASON_MIN = 5


# ---------------------------------------------------------------- 请求模型

class ConfigItemIn(BaseModel):
    key: str
    value: str | None = ""
    value_type: str = "string"
    scope: str = "global"
    is_secret: bool = False
    keep_value: bool = False  # 密文编辑时留空：沿用库里旧值，避免明文回填


class SaveConfigIn(BaseModel):
    items: list[ConfigItemIn]
    change_note: str = Field(default="", max_length=200)


class RevealIn(BaseModel):
    item_id: int
    reason: str = Field(default="", max_length=200)


class RollbackIn(BaseModel):
    environment: str
    version: int


# ---------------------------------------------------------------- 辅助

def env_checked(environment: str) -> str:
    if environment not in ENVIRONMENTS:
        raise err(400, f"非法环境：{environment}，可选：{'/'.join(ENVIRONMENTS)}")
    return environment


def updater_names(item_rows: list[dict]) -> dict[int, str]:
    ids = {r["updated_by"] for r in item_rows if r["updated_by"]}
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = query(f"SELECT id, name FROM users WHERE id IN ({placeholders})", tuple(ids))
    return {r["id"]: r["name"] for r in rows}


# ---------------------------------------------------------------- 元数据

@router.get("/api/config/meta")
def config_meta(user: dict = User):
    return {
        "types": [{"value": t, "label": CONFIG_TYPE_LABELS[t]} for t in CONFIG_TYPES],
        "scopes": [{"value": s, "label": CONFIG_SCOPE_LABELS[s]} for s in CONFIG_SCOPES],
        "actions": [{"value": k, "label": v} for k, v in svc.ACTION_LABELS.items()],
    }


# ---------------------------------------------------------------- 配置档案

@router.get("/api/config/profiles")
def list_profiles(user: dict = User,
                  business_line_id: int | None = None,
                  environment: str | None = None):
    """有配置档案（至少一个版本）的 应用×环境 列表。"""
    sql = """SELECT a.id AS app_id, a.name AS app_name, a.status AS app_status,
                    b.id AS business_line_id, b.name AS business_line_name,
                    x.environment, x.version AS latest_version,
                    x.change_note AS latest_note, x.created_at AS latest_at,
                    u.name AS created_by_name,
                    (SELECT COUNT(*) FROM config_items ci
                       WHERE ci.app_id = a.id AND ci.environment = x.environment) AS item_count,
                    (SELECT COUNT(*) FROM config_items ci
                       WHERE ci.app_id = a.id AND ci.environment = x.environment AND ci.is_secret = 1) AS secret_count
             FROM config_versions x
             JOIN applications a ON a.id = x.app_id
             JOIN business_lines b ON b.id = a.business_line_id
             LEFT JOIN users u ON u.id = x.created_by
             WHERE x.version = (
                 SELECT MAX(y.version) FROM config_versions y
                 WHERE y.app_id = x.app_id AND y.environment = x.environment)"""
    params: list = []
    if user["role"] != "admin":
        # 成员显式按其他业务线筛选属于越权，返回 403 而非静默收窄
        if business_line_id and business_line_id != user["business_line_id"]:
            check_bl_scope(user, business_line_id)
        sql += " AND a.business_line_id = ?"
        params.append(user["business_line_id"])
    elif business_line_id:
        sql += " AND a.business_line_id = ?"
        params.append(business_line_id)
    if environment:
        env_checked(environment)
        sql += " AND x.environment = ?"
        params.append(environment)
    sql += " ORDER BY x.created_at DESC, a.id"
    return [dict(r) for r in query(sql, tuple(params))]


@router.get("/api/apps/{app_id}/config")
def get_config(app_id: int, environment: str, user: dict = User):
    app_row = get_app_checked(user, app_id)
    env_checked(environment)
    rows = svc.current_items(app_id, environment)
    names = updater_names(rows)
    items = []
    for r in rows:
        d = svc.item_to_dict(r)
        d["updated_by_name"] = names.get(r["updated_by"])
        items.append(d)
    return {
        "app_id": app_id,
        "app_name": app_row["name"],
        "environment": environment,
        "read_only": app_row["status"] == TERMINAL_STATUS,
        "items": items,
        "versions": svc.list_versions(app_id, environment),
    }


@router.put("/api/apps/{app_id}/config")
def save_config(app_id: int, environment: str, body: SaveConfigIn, user: dict = User):
    app_row = get_app_checked(user, app_id)
    env_checked(environment)
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），配置档案只读，禁止修改")
    try:
        items = svc.validate_items([it.model_dump() for it in body.items])
        result = svc.save_profile(app_id, environment, items, user, change_note=body.change_note)
    except ValueError as e:
        raise err(400, str(e))
    return {"ok": True, **result}


@router.post("/api/apps/{app_id}/config/rollback", status_code=201)
def rollback_config(app_id: int, body: RollbackIn, user: dict = User):
    app_row = get_app_checked(user, app_id)
    env_checked(body.environment)
    if app_row["status"] == TERMINAL_STATUS:
        raise err(400, "应用已下线（终态），配置档案只读，禁止回滚")
    try:
        result = svc.rollback(app_id, body.environment, body.version, user)
    except LookupError as e:
        raise err(404, str(e))
    except ValueError as e:
        raise err(400, str(e))
    # 回滚产生的是新版本；被回滚版本及之前的全部留痕原样保留
    return {"ok": True, **result}


# ---------------------------------------------------------------- 密文查看（服务端强制二次确认）

@router.post("/api/apps/{app_id}/config/reveal")
def reveal_secret(app_id: int, body: RevealIn, user: dict = User):
    get_app_checked(user, app_id)
    reason = body.reason.strip()
    # 服务端强制：理由为空或过短直接拒绝，前端有没有拦都一样
    if len(reason) < REVEAL_REASON_MIN:
        raise err(400, f"查看密文明文必须填写不少于 {REVEAL_REASON_MIN} 个字的理由，服务端将记录本次查看")
    item = query_one(
        "SELECT * FROM config_items WHERE id = ? AND app_id = ?",
        (body.item_id, app_id),
    )
    if not item:
        raise err(404, f"配置项 #{body.item_id} 不存在")
    if not item["is_secret"]:
        raise err(400, "该配置项不是密文，无需查看明文")
    svc.log_reveal(app_id, item["environment"], body.item_id, item["key"], user, reason)
    return {
        "item_id": item["id"],
        "key": item["key"],
        "value": item["value"],  # 仅在此接口、带理由时返回一次明文
        "revealed_at": int(time.time()),
    }


# ---------------------------------------------------------------- 环境对比

@router.get("/api/apps/{app_id}/config/diff")
def diff_config(app_id: int, env_a: str, env_b: str, user: dict = User):
    get_app_checked(user, app_id)
    env_checked(env_a)
    env_checked(env_b)
    if env_a == env_b:
        raise err(400, "环境对比必须选择两个不同的环境")
    return svc.diff_environments(app_id, env_a, env_b)


# ---------------------------------------------------------------- 版本

@router.get("/api/apps/{app_id}/config/versions")
def get_versions(app_id: int, environment: str, user: dict = User):
    get_app_checked(user, app_id)
    env_checked(environment)
    return svc.list_versions(app_id, environment)


@router.get("/api/apps/{app_id}/config/versions/{version_no}")
def get_version(app_id: int, version_no: int, environment: str, user: dict = User):
    get_app_checked(user, app_id)
    env_checked(environment)
    try:
        return svc.version_detail(app_id, environment, version_no)
    except LookupError as e:
        raise err(404, str(e))


@router.get("/api/apps/{app_id}/config/rollback-preview")
def rollback_preview(app_id: int, environment: str, version: int, user: dict = User):
    get_app_checked(user, app_id)
    env_checked(environment)
    try:
        return svc.rollback_preview(app_id, environment, version)
    except LookupError as e:
        raise err(404, str(e))


# ---------------------------------------------------------------- 变更留痕

def _audit_rows(user: dict, app_id, business_line_id, environment, action, start, end):
    if action and action not in svc.ACTION_LABELS:
        raise err(400, f"非法动作类型：{action}")
    if environment:
        env_checked(environment)
    if app_id:
        # 按应用查询同样要过业务线范围校验，越权返回 403 而非空列表
        app_row = query_one("SELECT id, business_line_id FROM applications WHERE id = ?", (app_id,))
        if not app_row:
            raise err(404, f"应用 #{app_id} 不存在")
        check_bl_scope(user, app_row["business_line_id"])
    elif business_line_id:
        # 成员显式按其他业务线筛选属于越权
        check_bl_scope(user, business_line_id)
    try:
        sql, params = svc.query_audit(
            user, app_id=app_id, business_line_id=business_line_id,
            environment=environment, action=action, start=start, end=end,
        )
    except ValueError as e:
        raise err(400, str(e))
    return query(sql, tuple(params))


@router.get("/api/config/audit")
def list_audit(user: dict = User,
               app_id: int | None = None,
               business_line_id: int | None = None,
               environment: str | None = None,
               action: str | None = None,
               start: str | None = None,
               end: str | None = None):
    rows = _audit_rows(user, app_id, business_line_id, environment, action, start, end)
    return [svc.audit_row_to_dict(r) for r in rows]


@router.get("/api/config/audit/export.csv")
def export_audit(user: dict = User,
                 app_id: int | None = None,
                 business_line_id: int | None = None,
                 environment: str | None = None,
                 action: str | None = None,
                 start: str | None = None,
                 end: str | None = None):
    rows = _audit_rows(user, app_id, business_line_id, environment, action, start, end)
    csv_text = svc.export_csv(rows)
    filename = f"config-changelog-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )
