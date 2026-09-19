"""织云系统 - 配置档案领域服务。

设计要点：
- 配置项按 (应用, 环境) 管理，键在同一应用+环境内唯一；
- 每次保存生成一个全量快照版本（config_versions），回滚 = 用旧快照"追加"一个新版本，
  历史版本与留痕永不被改写或删除；
- 密文（is_secret=1）在所有列表/对比/版本/导出/审计接口中一律脱敏；
  查看明文只能走专门的 reveal 动作，理由由服务端强制校验并逐条留痕。
"""
import csv
import io
import json
import re
import time

from .db import (
    BOOL_TRUE, CONFIG_SCOPES, CONFIG_TYPES, ENV_LABELS, get_conn, query, query_one,
)

MASK = "••••••••"
KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.\-]{0,127}$")


class NoChangeError(ValueError):
    """配置与当前值完全一致，不产生空版本。"""


class ConflictError(Exception):
    """乐观锁冲突：编辑基于的版本已不是最新版本（典型：两个浏览器同时改同一条配置）。

    payload 携带 base→current 的逐键差异与对方操作信息，供前端给出"刷新合并 / 显式覆盖"取舍，
    服务端绝不允许后来者静默盖掉前一个改动。
    """

    def __init__(self, payload: dict):
        super().__init__(payload["message"])
        self.payload = payload


ACTION_LABELS = {
    "add": "新增",
    "update": "修改",
    "remove": "删除",
    "rollback": "回滚",
    "reveal": "查看明文",
}


# ---------------------------------------------------------------- 基础工具

def mask_value(value, is_secret):
    return MASK if is_secret else value


def validate_environment(environment: str):
    if environment not in ENV_LABELS:
        raise ValueError(f"非法环境：{environment}，可选：{'/'.join(ENV_LABELS)}")


def normalize_item(raw: dict) -> dict:
    """校验并归一化单个配置项，非法输入抛 ValueError（路由层转 400）。"""
    key = str(raw.get("key", "")).strip()
    if not KEY_PATTERN.match(key):
        raise ValueError(f"非法配置键：「{key}」，需以字母开头，仅含字母、数字、_-.，长度 ≤128")
    value_type = str(raw.get("value_type", "string"))
    if value_type not in CONFIG_TYPES:
        raise ValueError(f"配置项 {key} 的类型非法：{value_type}")
    scope = str(raw.get("scope", "global"))
    if scope not in CONFIG_SCOPES:
        raise ValueError(f"配置项 {key} 的生效范围非法：{scope}")
    is_secret = 1 if raw.get("is_secret") else 0
    keep_value = bool(raw.get("keep_value"))
    raw_value = raw.get("value", "")
    if raw_value is None:
        raw_value = ""
    value = str(raw_value)

    if keep_value:
        # 编辑表单里密文留空 = 沿用库里旧值，占位空串不参与类型校验
        value = ""
    elif value_type == "number":
        try:
            num = float(value)
        except ValueError:
            raise ValueError(f"配置项 {key} 声明为数字类型，但值无法解析：{value}")
        value = str(int(num)) if num.is_integer() else str(num)
    elif value_type == "boolean":
        if value.strip().lower() not in BOOL_TRUE and value.strip().lower() not in {"false", "0", "no", "off", "否", "关"}:
            raise ValueError(f"配置项 {key} 声明为布尔类型，值需为 true/false：{value}")
        value = "true" if value.strip().lower() in BOOL_TRUE else "false"
    elif value_type == "json":
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            raise ValueError(f"配置项 {key} 声明为 JSON 类型，但值不是合法 JSON：{value}")
        value = json.dumps(parsed, ensure_ascii=False)

    return {"key": key, "value": value, "value_type": value_type,
            "scope": scope, "is_secret": is_secret,
            "keep_value": bool(raw.get("keep_value"))}


def validate_items(raw_items) -> list[dict]:
    seen: set[str] = set()
    cleaned = []
    for raw in raw_items or []:
        item = normalize_item(raw)
        if item["key"] in seen:
            raise ValueError(f"配置键重复：{item['key']}")
        seen.add(item["key"])
        cleaned.append(item)
    return cleaned


def current_items(app_id: int, environment: str) -> list[dict]:
    rows = query(
        "SELECT * FROM config_items WHERE app_id = ? AND environment = ? ORDER BY key",
        (app_id, environment),
    )
    return [dict(r) for r in rows]


def item_to_dict(row: dict) -> dict:
    is_secret = bool(row["is_secret"])
    return {
        "id": row["id"],
        "key": row["key"],
        "value": mask_value(row["value"], is_secret),
        "masked": is_secret,
        "value_type": row["value_type"],
        "scope": row["scope"],
        "is_secret": is_secret,
        "updated_at": row["updated_at"],
        "updated_by_name": None,  # 由调用方补用户名
    }


def latest_version_no(conn, app_id: int, environment: str) -> int:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM config_versions WHERE app_id = ? AND environment = ?",
        (app_id, environment),
    ).fetchone()
    return row["v"] or 0


def write_app_change_log(conn, app_id: int, user_id: int, text: str) -> None:
    conn.execute(
        "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) VALUES (?,?,?,?,?)",
        (app_id, user_id, "配置变更", text, int(time.time())),
    )


# ---------------------------------------------------------------- 保存（产生版本 + 留痕）

def conflict_detail(conn, app_id: int, environment: str, base_version: int, latest: int) -> dict:
    """组装乐观锁冲突详情：base→current 之间的版本与逐键差异（密文脱敏）。"""
    ver_rows = conn.execute(
        """SELECT v.version, v.change_note, v.created_at, u.name AS created_by_name
           FROM config_versions v LEFT JOIN users u ON u.id = v.created_by
           WHERE v.app_id = ? AND v.environment = ? AND v.version > ?
           ORDER BY v.version""",
        (app_id, environment, base_version),
    ).fetchall()
    base = conn.execute(
        "SELECT snapshot FROM config_versions WHERE app_id = ? AND environment = ? AND version = ?",
        (app_id, environment, base_version),
    ).fetchone()
    base_items = json.loads(base["snapshot"]) if base else []
    now_rows = conn.execute(
        "SELECT * FROM config_items WHERE app_id = ? AND environment = ?",
        (app_id, environment),
    ).fetchall()
    entries = diff_item_lists([dict(r) for r in base_items], [dict(r) for r in now_rows])
    changed = [e for e in entries if e["status"] != "same"]
    versions = [{
        "version": r["version"],
        "change_note": r["change_note"],
        "created_by_name": r["created_by_name"] or "系统",
        "created_at": r["created_at"],
    } for r in ver_rows]
    others = "、".join(sorted({v["created_by_name"] for v in versions}))
    return {
        "code": "config_version_conflict",
        "message": (
            f"配置已被他人更新：你打开时基于 v{base_version}，当前已是 v{latest}"
            + (f"（{others} 在你之后保存过）" if others else "")
            + "。请刷新查看对方改动后再决定：合并后重新保存，或在确认风险后显式覆盖；"
              "系统不会让后来者静默盖掉前一个改动。"
        ),
        "base_version": base_version,
        "current_version": latest,
        "intervening_versions": versions,
        "entries": changed,
        "summary": summarize_diff(entries),
    }


def save_profile(app_id: int, environment: str, items: list[dict], user: dict,
                 change_note: str = "", rollback_from: int | None = None,
                 base_version: int | None = None, force: bool = False) -> dict:
    """整体保存某应用某环境的配置档案。

    与旧值逐键 diff 写留痕；无任何变化时拒绝产生空版本。
    rollback_from 不为 None 时，留痕动作记为 rollback，且这是一次"追加"而非覆盖。
    base_version 为编辑所依据的版本号：与当前最新版本不一致时抛 ConflictError（409）；
    force=True 表示用户已看过冲突、显式选择覆盖（留痕中注明）。
    """
    note = change_note.strip() if change_note else ""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        latest = latest_version_no(conn, app_id, environment)
        if base_version is not None and base_version != latest:
            if not force:
                raise ConflictError(conflict_detail(conn, app_id, environment, base_version, latest))
            # 显式覆盖：把取舍写进备注与逐条留痕，绝不允许"静默"盖掉
            force_tag = f"冲突后显式覆盖（基于 v{base_version}，覆盖 v{latest} 的改动）"
            note = f"{note}；{force_tag}" if note else force_tag
        old_rows = conn.execute(
            "SELECT * FROM config_items WHERE app_id = ? AND environment = ?",
            (app_id, environment),
        ).fetchall()
        old_map = {r["key"]: dict(r) for r in old_rows}

        resolved = []
        for it in items:
            keep = it.pop("keep_value", False)
            if keep:
                old = old_map.get(it["key"])
                if old is None:
                    raise ValueError(f"配置项 {it['key']} 是新增键，密文值不能为空")
                it["value"] = old["value"]
            resolved.append(it)
        items = resolved
        new_map = {it["key"]: it for it in items}

        audit_rows = []
        now = int(time.time())
        for key in sorted(set(old_map) | set(new_map)):
            old = old_map.get(key)
            new = new_map.get(key)
            if old is None:
                action, reason = ("rollback", f"回滚到 v{rollback_from} 时恢复该键") if rollback_from else ("add", "")
                audit_rows.append((action, key, None, new["value"], new["is_secret"], reason))
            elif new is None:
                action, reason = ("rollback", f"回滚到 v{rollback_from} 时移除该键") if rollback_from else ("remove", "")
                audit_rows.append((action, key, old["value"], None, old["is_secret"], reason))
            else:
                meta_changes = []
                if old["value_type"] != new["value_type"]:
                    meta_changes.append(f"类型 {old['value_type']}→{new['value_type']}")
                if old["scope"] != new["scope"]:
                    meta_changes.append(f"范围 {old['scope']}→{new['scope']}")
                if bool(old["is_secret"]) != bool(new["is_secret"]):
                    meta_changes.append("密文标记变更")
                value_changed = old["value"] != new["value"] or bool(old["is_secret"]) != bool(new["is_secret"])
                if not value_changed and not meta_changes:
                    continue
                reason = "；".join(meta_changes)
                if rollback_from:
                    reason = f"回滚到 v{rollback_from}" + (f"（{reason}）" if reason else "")
                    action = "rollback"
                else:
                    action = "update"
                audit_rows.append((action, key, old["value"], new["value"],
                                   new["is_secret"] or old["is_secret"], reason))

        if not audit_rows:
            raise NoChangeError("配置内容没有任何变化，未生成新版本")

        if base_version is not None and base_version != latest and force:
            # 显式覆盖时逐键留痕也带上取舍说明
            tag = f"冲突后显式覆盖 v{latest}（编辑基于 v{base_version}）"
            audit_rows = [(a, k, o, n, s, (f"{r}；{tag}" if r else tag))
                          for (a, k, o, n, s, r) in audit_rows]

        version_no = latest_version_no(conn, app_id, environment) + 1
        snapshot = json.dumps(items, ensure_ascii=False)
        cur = conn.execute(
            """INSERT INTO config_versions
               (app_id, environment, version, snapshot, change_note, created_by, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (app_id, environment, version_no, snapshot,
             (f"回滚到 v{rollback_from}" if rollback_from else note),
             user["id"], now),
        )
        version_id = cur.lastrowid

        conn.execute("DELETE FROM config_items WHERE app_id = ? AND environment = ?",
                     (app_id, environment))
        conn.executemany(
            """INSERT INTO config_items
               (app_id, environment, key, value, value_type, scope, is_secret, updated_by, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(app_id, environment, it["key"], it["value"], it["value_type"],
              it["scope"], it["is_secret"], user["id"], now) for it in items],
        )
        conn.executemany(
            """INSERT INTO config_audit_logs
               (app_id, environment, version_id, user_id, action, config_key,
                old_value, new_value, is_secret, reason, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [(app_id, environment, version_id, user["id"], action, key,
              old_v, new_v, is_secret, reason, now)
             for action, key, old_v, new_v, is_secret, reason in audit_rows],
        )
        summary = f"{'回滚配置档案到 v' + str(rollback_from) if rollback_from else '更新配置档案'}：v{version_no}，" \
                  f"{len(audit_rows)} 个键发生变化" + (f"（备注：{note}）" if note and not rollback_from else "")
        write_app_change_log(conn, app_id, user["id"], summary)
        conn.commit()
        return {"version": version_no, "version_id": version_id, "changes": len(audit_rows)}
    except Exception:
        conn.execute("ROLLBACK")
        raise


def rollback(app_id: int, environment: str, version_no: int, user: dict,
             base_version: int | None = None, force: bool = False) -> dict:
    target = query_one(
        "SELECT * FROM config_versions WHERE app_id = ? AND environment = ? AND version = ?",
        (app_id, environment, version_no),
    )
    if not target:
        raise LookupError(f"v{version_no} 不存在")
    current = latest_version_no(get_conn(), app_id, environment)
    if version_no == current:
        raise ValueError(f"v{version_no} 就是当前版本，无需回滚")
    if base_version is not None and base_version != current and not force:
        conn = get_conn()
        raise ConflictError(conflict_detail(conn, app_id, environment, base_version, current))
    items = validate_items(json.loads(target["snapshot"]))
    return save_profile(app_id, environment, items, user,
                        change_note=target["change_note"], rollback_from=version_no,
                        base_version=base_version, force=force)


# ---------------------------------------------------------------- 环境对比

def _side(row):
    if row is None:
        return None
    return {
        "key": row["key"],
        "value": mask_value(row["value"], bool(row["is_secret"])),
        "masked": bool(row["is_secret"]),
        "value_type": row["value_type"],
        "scope": row["scope"],
        "is_secret": bool(row["is_secret"]),
    }


def diff_item_lists(a_rows: list[dict], b_rows: list[dict]) -> list[dict]:
    """两组配置项按键 diff，值按库里真实值比较，但两侧输出都脱敏。"""
    a_map = {r["key"]: r for r in a_rows}
    b_map = {r["key"]: r for r in b_rows}
    entries = []
    for key in sorted(set(a_map) | set(b_map)):
        a, b = a_map.get(key), b_map.get(key)
        if a and not b:
            status = "only_a"
        elif b and not a:
            status = "only_b"
        elif a["value"] != b["value"] or a["value_type"] != b["value_type"] \
                or a["scope"] != b["scope"] or bool(a["is_secret"]) != bool(b["is_secret"]):
            status = "changed"
        else:
            status = "same"
        entries.append({"key": key, "status": status, "a": _side(a), "b": _side(b)})
    return entries


def summarize_diff(entries: list[dict]) -> dict:
    return {
        "only_a": sum(1 for e in entries if e["status"] == "only_a"),
        "only_b": sum(1 for e in entries if e["status"] == "only_b"),
        "changed": sum(1 for e in entries if e["status"] == "changed"),
        "same": sum(1 for e in entries if e["status"] == "same"),
        "total": len(entries),
    }


def diff_environments(app_id: int, env_a: str, env_b: str) -> dict:
    a_rows = current_items(app_id, env_a)
    b_rows = current_items(app_id, env_b)
    entries = diff_item_lists(a_rows, b_rows)
    return {"env_a": env_a, "env_b": env_b, "entries": entries,
            "summary": summarize_diff(entries)}


def rollback_preview(app_id: int, environment: str, version_no: int) -> dict:
    """对比"当前版本 → 目标版本"的差异（回滚后会变成什么样）。"""
    target = query_one(
        "SELECT * FROM config_versions WHERE app_id = ? AND environment = ? AND version = ?",
        (app_id, environment, version_no),
    )
    if not target:
        raise LookupError(f"v{version_no} 不存在")
    target_items = json.loads(target["snapshot"])
    now_rows = current_items(app_id, environment)
    # 回滚方向：a=当前，b=目标版本
    entries = diff_item_lists(now_rows, target_items)
    return {
        "environment": environment,
        "from_version": latest_version_no(get_conn(), app_id, environment),
        "target_version": version_no,
        "entries": entries,
        "summary": summarize_diff(entries),
    }


# ---------------------------------------------------------------- 版本

def list_versions(app_id: int, environment: str) -> list[dict]:
    rows = query(
        """SELECT v.id, v.version, v.change_note, v.created_at, v.snapshot,
                  u.name AS created_by_name
           FROM config_versions v LEFT JOIN users u ON u.id = v.created_by
           WHERE v.app_id = ? AND v.environment = ?
           ORDER BY v.version DESC""",
        (app_id, environment),
    )
    current = latest_version_no(get_conn(), app_id, environment)
    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "version": r["version"],
            "change_note": r["change_note"],
            "created_by_name": r["created_by_name"] or "系统",
            "created_at": r["created_at"],
            "item_count": len(json.loads(r["snapshot"])),
            "is_current": r["version"] == current,
        })
    return result


def version_detail(app_id: int, environment: str, version_no: int) -> dict:
    row = query_one(
        "SELECT * FROM config_versions WHERE app_id = ? AND environment = ? AND version = ?",
        (app_id, environment, version_no),
    )
    if not row:
        raise LookupError(f"v{version_no} 不存在")
    current = latest_version_no(get_conn(), app_id, environment)
    items = []
    for it in json.loads(row["snapshot"]):
        items.append({
            "key": it["key"],
            "value": mask_value(it["value"], bool(it["is_secret"])),
            "masked": bool(it["is_secret"]),
            "value_type": it["value_type"],
            "scope": it["scope"],
            "is_secret": bool(it["is_secret"]),
        })
    return {
        "version": row["version"],
        "change_note": row["change_note"],
        "created_at": row["created_at"],
        "is_current": row["version"] == current,
        "items": items,
    }


# ---------------------------------------------------------------- 密文查看

def log_reveal(app_id: int, environment: str, item_id: int, key: str, user: dict, reason: str) -> None:
    get_conn().execute(
        """INSERT INTO config_audit_logs
           (app_id, environment, version_id, user_id, action, config_key,
            old_value, new_value, is_secret, reason, created_at)
           VALUES (?,?,NULL,?, 'reveal', ?, NULL, NULL, 1, ?, ?)""",
        (app_id, environment, user["id"], key, reason.strip(), int(time.time())),
    )
    get_conn().commit()


# ---------------------------------------------------------------- 留痕查询

def _date_to_epoch(value: str | None, end: bool = False) -> int | None:
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        t = time.strptime(value + (" 23:59:59" if end else " 00:00:00"), "%Y-%m-%d %H:%M:%S")
        return int(time.mktime(t))
    if re.fullmatch(r"\d{10}", value):
        return int(value)
    raise ValueError("时间格式应为 YYYY-MM-DD")


def query_audit(user: dict, *, app_id: int | None = None, business_line_id: int | None = None,
                environment: str | None = None, action: str | None = None,
                start: str | None = None, end: str | None = None) -> tuple[str, list]:
    """返回 (sql, params)，已强制 业务线×环境 可见范围（无授权记录的组合查不到流水）。"""
    sql = """SELECT l.id, l.app_id, l.environment, l.version_id, l.action, l.config_key,
                    l.old_value, l.new_value, l.is_secret, l.reason, l.created_at,
                    a.name AS app_name, b.id AS business_line_id, b.name AS business_line_name,
                    u.name AS user_name
             FROM config_audit_logs l
             JOIN applications a ON a.id = l.app_id
             JOIN business_lines b ON b.id = a.business_line_id
             LEFT JOIN users u ON u.id = l.user_id
             WHERE 1=1"""
    params: list = []
    if user["role"] != "admin":
        # 业务线×环境 两级收窄：只返回 user_access 明确覆盖的组合
        sql += (" AND EXISTS (SELECT 1 FROM user_access ua"
                " WHERE ua.user_id = ? AND ua.business_line_id = a.business_line_id"
                " AND ua.environment = l.environment)")
        params.append(user["id"])
        if business_line_id:
            sql += " AND a.business_line_id = ?"
            params.append(business_line_id)
    elif business_line_id:
        sql += " AND a.business_line_id = ?"
        params.append(business_line_id)
    if app_id:
        sql += " AND l.app_id = ?"
        params.append(app_id)
    if environment:
        sql += " AND l.environment = ?"
        params.append(environment)
    if action:
        sql += " AND l.action = ?"
        params.append(action)
    start_ts = _date_to_epoch(start)
    end_ts = _date_to_epoch(end, end=True)
    if start_ts is not None:
        sql += " AND l.created_at >= ?"
        params.append(start_ts)
    if end_ts is not None:
        sql += " AND l.created_at <= ?"
        params.append(end_ts)
    sql += " ORDER BY l.created_at DESC, l.id DESC LIMIT 500"
    return sql, params


def audit_row_to_dict(r) -> dict:
    is_secret = bool(r["is_secret"])
    return {
        "id": r["id"],
        "version_id": r["version_id"],
        "app_id": r["app_id"],
        "app_name": r["app_name"],
        "business_line_name": r["business_line_name"],
        "environment": r["environment"],
        "environment_label": ENV_LABELS[r["environment"]],
        "action": r["action"],
        "action_label": ACTION_LABELS.get(r["action"], r["action"]),
        "config_key": r["config_key"],
        "old_value": mask_value(r["old_value"], is_secret) if r["old_value"] is not None else None,
        "new_value": mask_value(r["new_value"], is_secret) if r["new_value"] is not None else None,
        "is_secret": is_secret,
        "reason": r["reason"],
        "user_name": r["user_name"] or "系统",
        "created_at": r["created_at"],
    }


# ---------------------------------------------------------------- CSV 导出

CSV_HEADER = ["时间", "业务线", "应用", "环境", "操作人", "动作", "配置键", "改前", "改后", "是否密文", "理由/备注"]


def export_csv(rows) -> str:
    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM，Excel 直接打开不乱码
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for r in rows:
        d = audit_row_to_dict(r)
        writer.writerow([
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["created_at"])),
            d["business_line_name"],
            d["app_name"],
            d["environment_label"],
            d["user_name"],
            d["action_label"],
            d["config_key"],
            "" if d["old_value"] is None else d["old_value"],
            "" if d["new_value"] is None else d["new_value"],
            "是" if d["is_secret"] else "否",
            d["reason"],
        ])
    return buf.getvalue()
