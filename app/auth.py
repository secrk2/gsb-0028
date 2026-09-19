"""织云系统 - 认证与权限。

四类角色：
    admin     平台管理员：全部业务线、全部环境，可见 + 密文查看 + 配置编辑 + 应用管理
    bl_owner  业务线负责人：本业务线内可见范围与授权按 user_access 收窄；可管理本业务线人员授权
    app_owner 应用负责人：按 user_access 授权（通常带配置编辑/应用管理位）
    observer  只读观察者：按 user_access 只能有可见/密文查看位，不能有任何编辑/管理权

可见范围按 业务线 × 环境 两级收窄（user_access 一行 = 一个 业务线×环境 的授权）。
密文查看权 can_view_secret 与配置编辑权 can_edit_config 分开授予：
能看明文不代表能改，能改也不代表能看明文。
所有越权一律抛 403 并带具体原因（角色、目标范围、当前范围、缺什么权限），不返回空白页。
"""
from fastapi import Depends, Header, HTTPException

from .db import ENV_LABELS, ROLE_LABELS, query, query_one


def err(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


def _access_of(user_id: int) -> list[dict]:
    rows = query(
        """SELECT ua.business_line_id, b.name AS business_line_name, ua.environment,
                  ua.can_view_secret, ua.can_edit_config, ua.can_manage_app
           FROM user_access ua
           JOIN business_lines b ON b.id = ua.business_line_id
           WHERE ua.user_id = ?
           ORDER BY ua.business_line_id, ua.environment""",
        (user_id,),
    )
    return [dict(r) for r in rows]


def current_user(x_token: str = Header(default="")) -> dict:
    if not x_token:
        raise err(401, "未登录：缺少访问令牌")
    row = query_one(
        """SELECT u.id, u.username, u.name, u.role, u.business_line_id, u.active,
                  b.name AS business_line_name
           FROM users u LEFT JOIN business_lines b ON b.id = u.business_line_id
           WHERE u.token = ?""",
        (x_token,),
    )
    if not row:
        raise err(401, "登录已失效，请重新登录")
    user = dict(row)
    if not user["active"]:
        # 离职账号令牌即使还在也一律拦下，并说清原因
        raise err(403, f"账号 {user['name']}（{user['username']}）已停用，通常由于离职交接完成；"
                       "如有疑问请联系平台管理员")
    user["access"] = [] if user["role"] == "admin" else _access_of(user["id"])
    return user


User = Depends(current_user)


# ---------------------------------------------------------------- 角色与范围

def is_admin(user: dict) -> bool:
    return user["role"] == "admin"


def role_label(user: dict) -> str:
    return ROLE_LABELS.get(user["role"], user["role"])


def _access_map(user: dict) -> dict[tuple[int, str], dict]:
    return {(a["business_line_id"], a["environment"]): a for a in user.get("access", [])}


def scope_text(user: dict) -> str:
    """当前账号可见范围的自然语言描述，用于 403 文案。"""
    if is_admin(user):
        return "全部业务线 · 全部环境"
    access = user.get("access", [])
    if not access:
        return "（当前未授予任何业务线/环境的可见范围）"
    bls: dict[int, list[str]] = {}
    for a in access:
        bls.setdefault(a["business_line_id"], []).append(a["environment"])
    parts = []
    for bl_id, envs in bls.items():
        name = next((a["business_line_name"] for a in access if a["business_line_id"] == bl_id), f"#{bl_id}")
        parts.append(f"「{name} · {'/'.join(ENV_LABELS[e] for e in envs)}」")
    return "、".join(parts)


def _bl_name(business_line_id: int) -> str:
    bl = query_one("SELECT name FROM business_lines WHERE id = ?", (business_line_id,))
    return bl["name"] if bl else f"#{business_line_id}"


def can_see(user: dict, business_line_id: int, environment: str | None = None) -> bool:
    """是否可见某业务线（或其某环境）。"""
    if is_admin(user):
        return True
    amap = _access_map(user)
    if environment is None:
        return any(bl == business_line_id for bl, _env in amap)
    return (business_line_id, environment) in amap


def can(user: dict, perm: str, business_line_id: int, environment: str) -> bool:
    """三项独立授权位判定；admin 恒为真。"""
    if is_admin(user):
        return True
    a = _access_map(user).get((business_line_id, environment))
    return bool(a and a[perm])


def can_manage_bl(user: dict, business_line_id: int) -> bool:
    """业务线级管理权：admin，或在该业务线任一环境持有 can_manage_app 的业务线负责人。"""
    if is_admin(user):
        return True
    if user["role"] != "bl_owner":
        return False
    return any(a["business_line_id"] == business_line_id and a["can_manage_app"]
               for a in user.get("access", []))


# ---------------------------------------------------------------- 强制校验（403 带原因）

def _deny(user: dict, target: str, missing: str | None = None) -> None:
    prefix = f"无权访问 {target}：账号 {user['name']} 的角色是「{role_label(user)}」"
    if missing:
        raise err(403, f"{prefix}，{missing}。当前可见/授权范围：{scope_text(user)}。"
                       "如需开通请联系平台管理员或本业务线负责人")
    raise err(403, f"{prefix}，不在你的可见范围内。当前可见范围：{scope_text(user)}。"
                   "如需访问请联系平台管理员或本业务线负责人开通授权")


def require_visibility(user: dict, business_line_id: int, environment: str | None = None) -> None:
    if can_see(user, business_line_id, environment):
        return
    target = f"业务线「{_bl_name(business_line_id)}」"
    if environment:
        target += f"的「{ENV_LABELS.get(environment, environment)}」环境"
    if not is_admin(user) and can_see(user, business_line_id):
        # 业务线本身可见、只是环境没授权：错误文案点到环境这一级
        raise err(403,
                  f"无权访问「{_bl_name(business_line_id)}」的「{ENV_LABELS.get(environment, environment)}」"
                  f"环境：账号 {user['name']}（{role_label(user)}）在该业务线被授予的环境不包含它。"
                  f"当前可见环境：{scope_text(user)}。如需开通请联系平台管理员或业务线负责人")
    _deny(user, target)


def require_perm(user: dict, perm: str, business_line_id: int, environment: str) -> None:
    """密文查看 / 配置编辑 等点位校验；不可见与"可见但没权限"给出不同原因。"""
    require_visibility(user, business_line_id, environment)
    if can(user, perm, business_line_id, environment):
        return
    perm_name = {"can_view_secret": "密文查看权", "can_edit_config": "配置编辑权",
                 "can_manage_app": "应用管理权"}[perm]
    bl_name = _bl_name(business_line_id)
    env_label = ENV_LABELS.get(environment, environment)
    if perm == "can_view_secret":
        hint = ("能看到配置项（脱敏值）不等于能查看明文；密文查看权与配置编辑权分开授予，"
                "每次查看明文还需填写业务理由并逐条留痕")
    elif perm == "can_edit_config":
        hint = "你在该范围当前为只读；配置编辑权需单独授予，与密文查看权互不包含"
    else:
        hint = "应用归属与管理权需经交接或管理员授权"
    raise err(403,
              f"缺少「{bl_name} · {env_label}」的{perm_name}：账号 {user['name']}"
              f"（{role_label(user)}）没有该权限位。{hint}。")


def require_app_manage(user: dict, app_row: dict) -> None:
    """应用台账写操作（信息修改/状态流转/环境变量/交接发起）。"""
    bl_id, env = app_row["business_line_id"], app_row["environment"]
    require_perm(user, "can_manage_app", bl_id, env)


def check_bl_scope(user: dict, business_line_id: int) -> None:
    """旧接口兼容：业务线级可见性校验（显式请求其他业务线 → 403 而非静默收窄）。"""
    require_visibility(user, business_line_id)


def get_app_or_404(app_id: int) -> dict:
    row = query_one("SELECT * FROM applications WHERE id = ?", (app_id,))
    if not row:
        raise err(404, f"应用 #{app_id} 不存在或已被删除")
    return dict(row)


def get_app_checked(user: dict, app_id: int, environment: str | None = None) -> dict:
    """取应用并校验可见性：默认按应用所属环境收窄；可指定其他环境（配置档案场景）。"""
    app_row = get_app_or_404(app_id)
    require_visibility(user, app_row["business_line_id"], environment or app_row["environment"])
    return app_row


# ---------------------------------------------------------------- 列表过滤片段

def visibility_sql(user: dict, bl_col: str, env_col: str) -> tuple[str, list]:
    """给列表 SQL 追加 业务线×环境 可见范围过滤。返回 (sql片段, 参数)。

    bl_col/env_col 需带表别名，如 'a.business_line_id'、'x.environment'。
    显式越权由调用方先用 require_visibility 拦截，这里只做静默收窄兜底。
    """
    if is_admin(user):
        return "", []
    return (f" AND EXISTS (SELECT 1 FROM user_access ua "
            f"WHERE ua.user_id = ? AND ua.business_line_id = {bl_col} "
            f"AND ua.environment = {env_col})", [user["id"]])
