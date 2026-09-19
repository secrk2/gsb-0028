"""织云系统 - 认证、业务线数据范围、应用取数等公共依赖。"""
from fastapi import Depends, Header, HTTPException

from .db import query_one


def err(status_code: int, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


def current_user(x_token: str = Header(default="")) -> dict:
    if not x_token:
        raise err(401, "未登录：缺少访问令牌")
    row = query_one(
        """SELECT u.id, u.username, u.name, u.role, u.business_line_id,
                  b.name AS business_line_name
           FROM users u LEFT JOIN business_lines b ON b.id = u.business_line_id
           WHERE u.token = ?""",
        (x_token,),
    )
    if not row:
        raise err(401, "登录已失效，请重新登录")
    return dict(row)


User = Depends(current_user)


def is_admin(user: dict) -> bool:
    return user["role"] == "admin"


def check_bl_scope(user: dict, business_line_id: int) -> None:
    """普通成员只能操作本业务线的数据，越权直接 403。"""
    if is_admin(user):
        return
    if user["business_line_id"] != business_line_id:
        bl = query_one("SELECT name FROM business_lines WHERE id = ?", (business_line_id,))
        name = bl["name"] if bl else f"#{business_line_id}"
        raise err(403, f"无权访问其他业务线（{name}）的数据，仅可操作本业务线（{user['business_line_name']}）")


def get_app_or_404(app_id: int) -> dict:
    row = query_one("SELECT * FROM applications WHERE id = ?", (app_id,))
    if not row:
        raise err(404, f"应用 #{app_id} 不存在或已被删除")
    return dict(row)


def get_app_checked(user: dict, app_id: int) -> dict:
    app_row = get_app_or_404(app_id)
    check_bl_scope(user, app_row["business_line_id"])
    return app_row
