"""织云系统 - 权限与可见范围、应用归属交接、权限变更留痕 API。

规则要点：
- 角色四类：平台管理员 / 业务线负责人 / 应用负责人 / 只读观察者；
- user_access 按 业务线×环境 授权，密文查看、配置编辑、应用管理三个权限位彼此独立；
- 应用交接移交的是"归属 + 权限"，配置项挂在应用上随应用一并移交，
  交接前后负责人、权限如何处置、原因、生效时间全部写入 handover_records 与 permission_logs；
- 权限的每一次变更（角色/授权位/收回/停用/交接）都强制带理由并进留痕。
"""
import time

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..auth import (
    User, can_manage_bl, err, get_app_or_404, is_admin,
    require_perm, require_visibility, role_label,
)
from ..db import (
    ENVIRONMENTS, ENV_LABELS, ROLES, ROLE_LABELS, execute, get_conn, query, query_one,
)

router = APIRouter()

REASON_MIN = 5


# ---------------------------------------------------------------- 请求模型

class AccessItemIn(BaseModel):
    business_line_id: int
    environment: str
    can_view_secret: bool = False
    can_edit_config: bool = False
    can_manage_app: bool = False


class PermissionsIn(BaseModel):
    reason: str = Field(min_length=REASON_MIN, max_length=200)
    role: str | None = None
    access: list[AccessItemIn] = []


class HandoverIn(BaseModel):
    to_user_id: int
    reason: str = Field(min_length=REASON_MIN, max_length=200)
    revoke_from: bool = True   # 转交后是否收回原负责人在该业务线的权限（离职/转岗场景）


class DeactivateIn(BaseModel):
    reason: str = Field(min_length=REASON_MIN, max_length=200)


# ---------------------------------------------------------------- 工具

def _bl_name(bl_id: int) -> str:
    row = query_one("SELECT name FROM business_lines WHERE id = ?", (bl_id,))
    return row["name"] if row else f"#{bl_id}"


def access_rows(user_id: int) -> list[dict]:
    rows = query(
        """SELECT ua.business_line_id, b.name AS business_line_name, ua.environment,
                  ua.can_view_secret, ua.can_edit_config, ua.can_manage_app
           FROM user_access ua JOIN business_lines b ON b.id = ua.business_line_id
           WHERE ua.user_id = ?
           ORDER BY ua.business_line_id, ua.environment""",
        (user_id,),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["environment_label"] = ENV_LABELS[r["environment"]]
        d["can_view_secret"] = bool(r["can_view_secret"])
        d["can_edit_config"] = bool(r["can_edit_config"])
        d["can_manage_app"] = bool(r["can_manage_app"])
        out.append(d)
    return out


def permission_profile(user_row: dict) -> dict:
    """组装某用户的角色 + 业务线×环境授权（含自然语言范围说明）。"""
    if user_row["role"] == "admin":
        access = []
        scope_text = "全部业务线 · 全部环境（含密文查看、配置编辑、应用管理）"
    else:
        access = access_rows(user_row["id"])
        groups: dict[int, list[str]] = {}
        for a in access:
            groups.setdefault(a["business_line_id"], []).append(
                ENV_LABELS[a["environment"]])
        scope_text = "；".join(
            f"{_bl_name(bl)}：{'/'.join(envs)}" for bl, envs in sorted(groups.items())
        ) or "（未授予任何可见范围）"
    bl = query_one("SELECT name FROM business_lines WHERE id = ?",
                   (user_row["business_line_id"],)) if user_row["business_line_id"] else None
    return {
        "user_id": user_row["id"],
        "username": user_row["username"],
        "name": user_row["name"],
        "role": user_row["role"],
        "role_label": ROLE_LABELS.get(user_row["role"], user_row["role"]),
        "active": bool(user_row["active"]),
        "business_line_id": user_row["business_line_id"],
        "business_line_name": bl["name"] if bl else None,
        "access": access,
        "scope_text": scope_text,
    }


def log_permission(actor_id: int | None, target_id: int | None, bl_id: int | None,
                   env: str | None, action: str, detail: str, reason: str) -> None:
    execute(
        """INSERT INTO permission_logs
           (actor_id, target_user_id, business_line_id, environment, action, detail, reason, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (actor_id, target_id, bl_id, env, action, detail, reason, int(time.time())),
    )


def administered_bl_ids(user: dict) -> set[int]:
    """该操作者可以管理授权的业务线集合（admin=全部；bl_owner=持有应用管理权的本业务线）。"""
    if is_admin(user):
        return {r["id"] for r in query("SELECT id FROM business_lines")}
    if user["role"] == "bl_owner":
        return {a["business_line_id"] for a in user.get("access", []) if a["can_manage_app"]}
    return set()


# ---------------------------------------------------------------- 我的权限

@router.get("/api/my/permissions")
def my_permissions(user: dict = User):
    row = query_one("SELECT * FROM users WHERE id = ?", (user["id"],))
    return permission_profile(dict(row))


# ---------------------------------------------------------------- 人员权限管理

@router.get("/api/users/{user_id}/permissions")
def get_user_permissions(user_id: int, user: dict = User):
    target = query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not target:
        raise err(404, f"用户 #{user_id} 不存在")
    target = dict(target)
    if not (is_admin(user) or user["id"] == user_id
            or (user["role"] == "bl_owner"
                and target["business_line_id"] in administered_bl_ids(user))):
        raise err(403, f"无权查看 {target['name']} 的权限明细：仅本人、本业务线负责人或平台管理员可查")
    profile = permission_profile(target)
    profile["editable"] = is_admin(user) or (
        user["role"] == "bl_owner"
        and target["business_line_id"] in administered_bl_ids(user)
        and target["role"] != "admin"
    )
    profile["can_change_role"] = is_admin(user)
    return profile


def _flags_text(a: dict) -> str:
    flags = []
    if a["can_view_secret"]:
        flags.append("密文查看")
    if a["can_edit_config"]:
        flags.append("配置编辑")
    if a["can_manage_app"]:
        flags.append("应用管理")
    return "、".join(flags) if flags else "仅可见（只读）"


@router.put("/api/users/{user_id}/permissions")
def update_user_permissions(user_id: int, body: PermissionsIn, user: dict = User):
    target = query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not target:
        raise err(404, f"用户 #{user_id} 不存在")
    target = dict(target)
    if target["role"] == "admin" and not is_admin(user):
        raise err(403, "无权修改平台管理员的权限")
    if not is_admin(user):
        # 业务线负责人：不能改角色，且授权行只能落在自己持管理权且属于对方所属的业务线
        if body.role is not None and body.role != target["role"]:
            raise err(403, "角色调整仅平台管理员可操作；业务线负责人只能调整本业务线内的环境授权")
        # 必须实际管辖对方所属业务线：应用负责人 / 观察者（或管别的业务线的负责人）一律 403，
        # 即使提交的是空授权（no-op）也不返回 200，避免"改不动还显示成功"
        if target["business_line_id"] not in administered_bl_ids(user):
            raise err(403,
                      f"无权调整 {target['name']} 的权限：账号 {user['name']}（{role_label(user)}）"
                      "不持有其所属业务线的管理权；请联系平台管理员")
    if body.role is not None and body.role not in ROLES:
        raise err(400, f"非法角色：{body.role}，可选：{'/'.join(ROLES)}")

    # 入参合法性 + 操作者管辖范围
    wanted = {}
    managed = administered_bl_ids(user)
    target_home_bl = target["business_line_id"]
    for item in body.access:
        if item.environment not in ENVIRONMENTS:
            raise err(400, f"非法环境：{item.environment}")
        bl = query_one("SELECT id, name FROM business_lines WHERE id = ?", (item.business_line_id,))
        if not bl:
            raise err(400, f"业务线 #{item.business_line_id} 不存在")
        if not is_admin(user):
            if item.business_line_id not in managed:
                raise err(403, f"无权管理业务线「{bl['name']}」的授权")
            if target_home_bl is not None and item.business_line_id != target_home_bl:
                raise err(400,
                          f"用户 {target['name']} 属于其他业务线，不能跨业务线授予「{bl['name']}」的权限")
        key = (item.business_line_id, item.environment)
        if key in wanted:
            raise err(400, f"授权重复：业务线 #{item.business_line_id} × {item.environment}")
        wanted[key] = item

    new_role = body.role or target["role"]
    # 只读观察者：硬性约束，不能携带任何编辑/管理位（密文查看可单独授予）
    if new_role == "observer":
        illegal = [it for it in body.access
                   if it.can_edit_config or it.can_manage_app]
        if illegal:
            raise err(400,
                      "只读观察者不能持有「配置编辑」或「应用管理」权限位（密文查看可单独授予）；"
                      "如需编辑能力请先把角色调整为应用负责人/业务线负责人")

    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = int(time.time())

        # 1) 角色变更
        if body.role is not None and body.role != target["role"]:
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (body.role, user_id))
            log_permission(user["id"], user_id, None, None, "role_grant",
                           f"角色调整：{ROLE_LABELS[target['role']]} → {ROLE_LABELS[body.role]}",
                           body.reason)

        # 2) 授权行 diff（管理员整体替换；业务线负责人只替换其管辖业务线的行，其余原样保留）
        old_rows = conn.execute(
            "SELECT * FROM user_access WHERE user_id = ?", (user_id,)).fetchall()
        old_map = {(r["business_line_id"], r["environment"]): dict(r) for r in old_rows}

        for key, item in wanted.items():
            old = old_map.get(key)
            flags = (int(item.can_view_secret), int(item.can_edit_config), int(item.can_manage_app))
            scope_desc = f"{_bl_name(key[0])} · {ENV_LABELS[key[1]]}"
            new_flags_desc = _flags_text({
                "can_view_secret": item.can_view_secret,
                "can_edit_config": item.can_edit_config,
                "can_manage_app": item.can_manage_app,
            })
            if old is None:
                conn.execute(
                    """INSERT INTO user_access
                       (user_id, business_line_id, environment,
                        can_view_secret, can_edit_config, can_manage_app, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (user_id, key[0], key[1], *flags, now, now),
                )
                log_permission(user["id"], user_id, key[0], key[1], "access_grant",
                               f"新增授权「{scope_desc}」：{new_flags_desc}", body.reason)
            else:
                changes = []
                for col, label in (("can_view_secret", "密文查看"),
                                   ("can_edit_config", "配置编辑"),
                                   ("can_manage_app", "应用管理")):
                    new_v = int(getattr(item, col))
                    if int(old[col]) != new_v:
                        changes.append(f"{label}{'授予' if new_v else '收回'}")
                if changes:
                    conn.execute(
                        """UPDATE user_access SET can_view_secret=?, can_edit_config=?,
                           can_manage_app=?, updated_at=? WHERE id=?""",
                        (*flags, now, old["id"]),
                    )
                    log_permission(user["id"], user_id, key[0], key[1], "access_update",
                                   f"调整授权「{_bl_name(key[0])} · {ENV_LABELS[key[1]]}」：{'、'.join(changes)}",
                                   body.reason)

        # 删除：管理员可整体收走；业务线负责人只能收走自己管辖 BL 的行
        for key, old in old_map.items():
            if key in wanted:
                continue
            if not is_admin(user) and key[0] not in managed:
                continue
            conn.execute("DELETE FROM user_access WHERE id = ?", (old["id"],))
            old_flags_desc = _flags_text({
                "can_view_secret": old["can_view_secret"],
                "can_edit_config": old["can_edit_config"],
                "can_manage_app": old["can_manage_app"],
            })
            log_permission(user["id"], user_id, key[0], key[1], "access_revoke",
                           f"收回授权「{_bl_name(key[0])} · {ENV_LABELS[key[1]]}」"
                           f"（原权限：{old_flags_desc}）",
                           body.reason)
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK")
        raise

    row = query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    return {"ok": True, "profile": permission_profile(dict(row))}


@router.post("/api/users/{user_id}/deactivate")
def deactivate_user(user_id: int, body: DeactivateIn, user: dict = User):
    if not is_admin(user):
        raise err(403, "账号停用（离职）仅平台管理员可操作；请先完成应用交接后再停用")
    target = query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if not target:
        raise err(404, f"用户 #{user_id} 不存在")
    if not target["active"]:
        raise err(400, f"账号 {target['name']} 已是停用状态")
    owns = query("SELECT id, name FROM applications WHERE owner_id = ?", (user_id,))
    if owns:
        names = "、".join(f"「{r['name']}」" for r in owns)
        raise err(409,
                  f"{target['name']} 仍是 {len(owns)} 个应用的负责人（{names}）；"
                  "请先逐一完成应用归属交接，再停用账号")
    execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
    execute("DELETE FROM user_access WHERE user_id = ?", (user_id,))
    log_permission(user["id"], user_id, None, None, "deactivate",
                   f"账号停用（离职/转岗）：{target['name']}（{target['username']}），授权范围已全部收回",
                   body.reason)
    return {"ok": True}


# ---------------------------------------------------------------- 应用交接

@router.get("/api/apps/{app_id}/handovers")
def list_handovers(app_id: int, user: dict = User):
    app_row = get_app_or_404(app_id)
    require_visibility(user, app_row["business_line_id"], app_row["environment"])
    rows = query(
        """SELECT h.*, fu.name AS from_name, tu.name AS to_name, op.name AS operator_name
           FROM handover_records h
           LEFT JOIN users fu ON fu.id = h.from_user_id
           LEFT JOIN users tu ON tu.id = h.to_user_id
           LEFT JOIN users op ON op.id = h.operator_id
           WHERE h.app_id = ? ORDER BY h.created_at DESC, h.id DESC""",
        (app_id,),
    )
    return [{
        "id": r["id"],
        "app_id": r["app_id"],
        "from_user_id": r["from_user_id"],
        "from_name": r["from_name"] or "（未设置负责人）",
        "to_user_id": r["to_user_id"],
        "to_name": r["to_name"] or "（空）",
        "operator_name": r["operator_name"] or "系统",
        "reason": r["reason"],
        "permissions_note": r["permissions_note"],
        "effective_at": r["effective_at"],
        "created_at": r["created_at"],
    } for r in rows]


@router.post("/api/apps/{app_id}/handover", status_code=201)
def handover_app(app_id: int, body: HandoverIn, user: dict = User):
    """交接应用归属与权限：

    - 配置项挂在应用上，归属转移后配置档案/版本/留痕自然随应用一并移交，不产生数据搬运；
    - 新负责人获得原负责人在该业务线各环境的授权（并集，不降低其已有权限）；
    - revoke_from=true 时收回原负责人在该业务线的全部授权（离职/转岗彻底移交）；
    - 交接前后谁负责、权限如何处置、原因与生效时刻逐条留痕。
    """
    app_row = get_app_or_404(app_id)
    # 应用管理权按应用登记的环境校验
    require_perm(user, "can_manage_app", app_row["business_line_id"], app_row["environment"])
    target = query_one("SELECT * FROM users WHERE id = ?", (body.to_user_id,))
    if not target or not target["active"]:
        raise err(400, "交接目标账号不存在或已停用，不能作为新负责人")
    if target["role"] == "observer":
        raise err(400,
                  f"只读观察者 {target['name']} 不能承接应用归属；"
                  "请先由平台管理员调整其角色与授权")
    if target["business_line_id"] != app_row["business_line_id"]:
        raise err(400,
                  f"新负责人 {target['name']} 不属于该应用所在业务线「{_bl_name(app_row['business_line_id'])}」，"
                  "不能跨业务线交接；如需转线请先由平台管理员调整人员归属")
    if app_row["owner_id"] == body.to_user_id:
        raise err(400, f"应用当前负责人已经是 {target['name']}，无需交接")

    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        from_id = app_row["owner_id"]
        from_name = "（未设置负责人）"
        if from_id:
            fr = conn.execute("SELECT name FROM users WHERE id = ?", (from_id,)).fetchone()
            from_name = fr["name"] if fr else f"#{from_id}"

        # 1) 复制原负责人在该业务线的授权给新负责人（按位取并集）
        granted_envs = []
        if from_id:
            src_rows = conn.execute(
                "SELECT * FROM user_access WHERE user_id = ? AND business_line_id = ?",
                (from_id, app_row["business_line_id"]),
            ).fetchall()
            for s in src_rows:
                dst = conn.execute(
                    "SELECT * FROM user_access WHERE user_id = ? AND business_line_id = ? AND environment = ?",
                    (body.to_user_id, app_row["business_line_id"], s["environment"]),
                ).fetchone()
                if dst is None:
                    conn.execute(
                        """INSERT INTO user_access
                           (user_id, business_line_id, environment,
                            can_view_secret, can_edit_config, can_manage_app, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (body.to_user_id, app_row["business_line_id"], s["environment"],
                         s["can_view_secret"], s["can_edit_config"], s["can_manage_app"], now, now),
                    )
                else:
                    conn.execute(
                        """UPDATE user_access SET can_view_secret=MAX(can_view_secret,?),
                           can_edit_config=MAX(can_edit_config,?), can_manage_app=MAX(can_manage_app,?),
                           updated_at=? WHERE id=?""",
                        (s["can_view_secret"], s["can_edit_config"], s["can_manage_app"], now, dst["id"]),
                    )
                granted_envs.append(ENV_LABELS[s["environment"]])
        # 至少保证新负责人在应用所属环境持有应用管理权
        base = conn.execute(
            "SELECT * FROM user_access WHERE user_id = ? AND business_line_id = ? AND environment = ?",
            (body.to_user_id, app_row["business_line_id"], app_row["environment"]),
        ).fetchone()
        if base is None:
            conn.execute(
                """INSERT INTO user_access
                   (user_id, business_line_id, environment,
                    can_view_secret, can_edit_config, can_manage_app, created_at, updated_at)
                   VALUES (?,?,?,0,0,1,?,?)""",
                (body.to_user_id, app_row["business_line_id"], app_row["environment"], now, now),
            )
            granted_envs.append(ENV_LABELS[app_row["environment"]])
        elif not base["can_manage_app"]:
            conn.execute(
                "UPDATE user_access SET can_manage_app = 1, updated_at = ? WHERE id = ?",
                (now, base["id"]),
            )

        # 2) 收回原负责人在该业务线的授权（可选）
        revoked_note = ""
        if body.revoke_from and from_id:
            rev_rows = conn.execute(
                "SELECT environment, can_view_secret, can_edit_config, can_manage_app FROM user_access"
                " WHERE user_id = ? AND business_line_id = ?",
                (from_id, app_row["business_line_id"]),
            ).fetchall()
            conn.execute(
                "DELETE FROM user_access WHERE user_id = ? AND business_line_id = ?",
                (from_id, app_row["business_line_id"]),
            )
            revoked_note = (f"原负责人 {from_name} 在该业务线的授权已全部收回"
                            f"（涉及环境：{'/'.join(ENV_LABELS[r['environment']] for r in rev_rows) or '无'}）")
        elif from_id:
            revoked_note = f"原负责人 {from_name} 的授权保留未动（可并行协助，后续可单独收回）"

        # 3) 应用归属移交
        conn.execute("UPDATE applications SET owner_id = ?, updated_at = ? WHERE id = ?",
                     (body.to_user_id, now, app_id))
        perm_note = (
            f"配置项随应用一并移交（配置档案/版本历史/逐键留痕归属不变，无需搬运）；"
            f"已将原负责人在「{_bl_name(app_row['business_line_id'])}」"
            f"{'/'.join(sorted(set(granted_envs)))} 等环境的权限同步授予新负责人 {target['name']}；{revoked_note}"
        )
        conn.execute(
            """INSERT INTO handover_records
               (app_id, from_user_id, to_user_id, operator_id, reason, permissions_note,
                effective_at, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (app_id, from_id, body.to_user_id, user["id"], body.reason, perm_note, now, now),
        )
        conn.execute(
            """INSERT INTO change_logs (app_id, user_id, action, detail, created_at)
               VALUES (?,?,?,?,?)""",
            (app_id, user["id"], "应用交接",
             f"应用负责人交接：{from_name} → {target['name']}，原因：{body.reason}", now),
        )
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK")
        raise

    log_permission(
        user["id"], body.to_user_id, app_row["business_line_id"], None, "handover",
        f"应用「{app_row['name']}」交接：{from_name} → {target['name']}；{revoked_note}",
        body.reason,
    )
    if app_row["owner_id"]:
        log_permission(
            user["id"], app_row["owner_id"], app_row["business_line_id"], None, "handover",
            f"应用「{app_row['name']}」交出：{from_name} → {target['name']}", body.reason,
        )

    app_now = get_app_or_404(app_id)
    return {
        "ok": True,
        "owner_id": app_now["owner_id"],
        "owner_name": target["name"],
        "permissions_note": perm_note,
        "effective_at": now,
    }


# ---------------------------------------------------------------- 权限变更留痕

@router.get("/api/permission-logs")
def list_permission_logs(user: dict = User,
                         target_user_id: int | None = None,
                         business_line_id: int | None = None):
    sql = """SELECT l.*, au.name AS actor_name, tu.name AS target_name,
                    b.name AS business_line_name
             FROM permission_logs l
             LEFT JOIN users au ON au.id = l.actor_id
             LEFT JOIN users tu ON tu.id = l.target_user_id
             LEFT JOIN business_lines b ON b.id = l.business_line_id
             WHERE 1=1"""
    params: list = []
    if not is_admin(user):
        if user["role"] == "bl_owner":
            managed = administered_bl_ids(user)
            if not managed:
                raise err(403, "你当前不持有任何业务线的管理权，无权查看权限变更留痕")
            placeholders = ",".join("?" * len(managed))
            sql += (f" AND (l.business_line_id IN ({placeholders})"
                    " OR l.target_user_id = ? OR l.actor_id = ?)")
            params.extend(sorted(managed))
            params.extend([user["id"], user["id"]])
        else:
            # 应用负责人/观察者只能看与自己相关的权限流水
            if target_user_id and target_user_id != user["id"]:
                raise err(403, "只能查看与本人相关的权限变更留痕")
            target_user_id = user["id"]
            sql += " AND (l.target_user_id = ? OR l.actor_id = ?)"
            params.extend([user["id"], user["id"]])
    if target_user_id:
        sql += " AND l.target_user_id = ?"
        params.append(target_user_id)
    if business_line_id:
        if not (is_admin(user) or can_manage_bl(user, business_line_id)):
            raise err(403, "无权按该业务线查询权限变更留痕")
        sql += " AND l.business_line_id = ?"
        params.append(business_line_id)
    sql += " ORDER BY l.created_at DESC, l.id DESC LIMIT 300"
    rows = query(sql, tuple(params))
    action_labels = {
        "role_grant": "角色调整", "access_grant": "授予权限",
        "access_update": "调整权限", "access_revoke": "收回权限",
        "deactivate": "账号停用", "handover": "应用交接",
    }
    return [{
        "id": r["id"],
        "actor_name": r["actor_name"] or "系统",
        "target_name": r["target_name"] or "系统",
        "business_line_name": r["business_line_name"],
        "environment": r["environment"],
        "environment_label": ENV_LABELS[r["environment"]] if r["environment"] else None,
        "action": r["action"],
        "action_label": action_labels.get(r["action"], r["action"]),
        "detail": r["detail"],
        "reason": r["reason"],
        "created_at": r["created_at"],
    } for r in rows]
