"""初始数据：4 条业务线、10 个用户（含 1 个已离职账号）、24 个应用。

账号覆盖四类角色与差异化授权（业务线 × 环境 两级收窄；密文查看 / 配置编辑 / 应用管理分开授予）：
- 系统管理员：平台管理员，全部范围全权；
- 张伟 / 王强 / 刘洋：业务线负责人，本业务线四环境全权；
- 李娜：应用负责人，支付结算·生产三权齐全，其他环境无应用管理权；
- 陈晨：只读观察者，仅可见「用户增长 · 生产」（看不到其他环境/业务线），无明文权；
- 赵敏：只读观察者，可见「供应链 · 生产/预发」且有密文查看权、但无编辑权（能看不能改）；
- 孙磊：应用负责人，数据平台四环境全权；
- 周婷：应用负责人，仅数据平台·生产/预发，有配置编辑权但密文查看权被收回（能改不能看明文）；
- 郭磊：已离职停用，名下应用已交接给李娜，授权全部收回（交接/停用均有留痕）。
"""
import json
import time

from .db import ENV_LABELS, ROLE_LABELS, STATUS_LABELS, execute, query_one

DAY = 86400

BUSINESS_LINES = [
    ("支付结算", "pay"),
    ("用户增长", "growth"),
    ("供应链", "supply"),
    ("数据平台", "data"),
]

# (username, 姓名, 角色, 业务线 code 或 None, 是否启用)
USERS = [
    ("admin",  "系统管理员", "admin",  None, 1),
    ("zhangwei", "张伟", "bl_owner", "pay", 1),
    ("lina",     "李娜", "app_owner", "pay", 1),
    ("guolei",   "郭磊", "app_owner", "pay", 0),       # 已离职：交接完成后停用
    ("wangqiang", "王强", "bl_owner", "growth", 1),
    ("chenchen", "陈晨", "observer", "growth", 1),     # 只读：仅 growth×prod
    ("liuyang",  "刘洋", "bl_owner", "supply", 1),
    ("zhaomin",  "赵敏", "observer", "supply", 1),     # 能看明文、不能改
    ("sunlei",   "孙磊", "app_owner", "data", 1),
    ("zhouting", "周婷", "app_owner", "data", 1),      # 能改、不能看明文
]

# (username, 业务线 code, 环境, 密文查看, 配置编辑, 应用管理)
ACCESS = [
    ("zhangwei", "pay", "dev", 1, 1, 1),
    ("zhangwei", "pay", "test", 1, 1, 1),
    ("zhangwei", "pay", "staging", 1, 1, 1),
    ("zhangwei", "pay", "prod", 1, 1, 1),
    ("lina", "pay", "dev", 1, 1, 0),
    ("lina", "pay", "test", 1, 1, 0),
    ("lina", "pay", "staging", 1, 1, 0),
    ("lina", "pay", "prod", 1, 1, 1),
    # guolei：离职交接后授权已全部收回，无任何 user_access
    ("wangqiang", "growth", "dev", 1, 1, 1),
    ("wangqiang", "growth", "test", 1, 1, 1),
    ("wangqiang", "growth", "staging", 1, 1, 1),
    ("wangqiang", "growth", "prod", 1, 1, 1),
    ("chenchen", "growth", "prod", 0, 0, 0),           # 纯只读：只见脱敏值
    ("liuyang", "supply", "dev", 1, 1, 1),
    ("liuyang", "supply", "test", 1, 1, 1),
    ("liuyang", "supply", "staging", 1, 1, 1),
    ("liuyang", "supply", "prod", 1, 1, 1),
    ("zhaomin", "supply", "staging", 1, 0, 0),         # 预发可见明文
    ("zhaomin", "supply", "prod", 1, 0, 0),            # 生产可见明文，但不能改
    ("sunlei", "data", "dev", 1, 1, 1),
    ("sunlei", "data", "test", 1, 1, 1),
    ("sunlei", "data", "staging", 1, 1, 1),
    ("sunlei", "data", "prod", 1, 1, 1),
    ("zhouting", "data", "staging", 0, 1, 0),          # 能改不能看明文
    ("zhouting", "data", "prod", 0, 1, 0),
]

# (应用名, 业务线, 负责人 username 或 None, 集群, 环境, 状态, 描述, 环境变量数, 距今天数)
APPS = [
    # 支付结算
    ("支付网关",        "pay",    "zhangwei", "华东1集群", "prod",    "online",      "统一收单与路由网关", 5, 1),
    ("清结算中心",      "pay",    "lina",     "华东1集群", "prod",    "maintenance", "T+1 清分结算批处理（郭磊离职后交接给李娜）", 4, 3),
    ("风控实时引擎",    "pay",    "zhangwei", "华北2集群", "prod",    "online",      "实时交易风控决策", 6, 6),
    ("对账平台",        "pay",    None,       "华南1集群", "test",    "developing",  "渠道对账与差错处理", 0, 2),
    ("收银台 H5",       "pay",    "lina",     "华东1集群", "staging", "online",      "移动端收银台", 3, 20),
    ("代付通道服务",    "pay",    None,       "华北2集群", "prod",    "offline",     "已迁移至新代付平台", 2, 40),
    # 用户增长
    ("会员中心",        "growth", "wangqiang", "华东1集群", "prod",   "online",      "会员等级与权益", 5, 4),
    ("裂变活动平台",    "growth", "wangqiang", "华南1集群", "staging", "developing",  "老带新裂变活动配置", 0, 0),
    ("消息推送中心",    "growth", "wangqiang", "华北2集群", "prod",   "maintenance", "Push/短信/站内信", 4, 5),
    ("积分商城",        "growth", None,        "华东1集群", "test",   "developing",  "积分兑换商城", 3, 12),
    ("增长实验平台",    "growth", "wangqiang", "华北2集群", "dev",     "developing",  "AB 实验与分流", 0, 1),
    ("老客召回系统",    "growth", "wangqiang", "华南1集群", "prod",   "offline",     "已被消息推送中心替代", 1, 60),
    # 供应链
    ("订单履约中心",    "supply", "liuyang",  "华东1集群", "prod",    "online",      "订单寻源与履约调度", 6, 2),
    ("仓储管理 WMS",    "supply", "liuyang",  "华北2集群", "prod",    "maintenance", "仓内作业管理", 5, 8),
    ("运输调度 TMS",    "supply", "liuyang",  "华南1集群", "staging", "online",      "干线与城配调度", 4, 15),
    ("供应商门户",      "supply", None,       "华东1集群", "test",    "developing",  "供应商协同门户", 0, 3),
    ("库存中台",        "supply", "liuyang",  "华北2集群", "prod",    "online",      "全渠道库存共享", 5, 6),
    ("旧采购系统",      "supply", "liuyang",  "西南灾备集群", "prod", "offline",     "采购 1.0，已下线", 2, 90),
    # 数据平台
    ("实时数仓",        "data",   "sunlei",   "华北2集群", "prod",    "online",      "Flink 实时数仓", 6, 1),
    ("离线调度平台",    "data",   "zhouting", "华北2集群", "prod",    "maintenance", "离线任务调度", 4, 9),
    ("BI 报表平台",     "data",   "sunlei",   "华东1集群", "prod",    "online",      "经营分析报表", 3, 25),
    ("数据质量中心",    "data",   None,       "华南1集群", "dev",     "developing",  "数据质量规则引擎", 0, 0),
    ("标签画像平台",    "data",   "zhouting", "华东1集群", "staging", "online",      "用户标签与画像", 5, 4),
    ("日志采集 Agent",  "data",   "sunlei",   "西南灾备集群", "prod", "offline",     "已被 Filebeat 方案替代", 2, 120),
]

ENV_VAR_POOL = [
    ("DB_HOST", "mysql.internal"),
    ("DB_PASSWORD", "****"),
    ("REDIS_URL", "redis://redis.internal:6379/0"),
    ("MQ_BROKER", "kafka://kafka.internal:9092"),
    ("LOG_LEVEL", "INFO"),
    ("OSS_BUCKET", "app-assets"),
]


def seed_if_empty() -> bool:
    """数据库为空时写入初始数据。返回是否执行了种子写入。"""
    if query_one("SELECT id FROM business_lines LIMIT 1"):
        return False

    now = int(time.time())

    bl_ids = {}
    for name, code in BUSINESS_LINES:
        cur = execute("INSERT INTO business_lines (name, code) VALUES (?, ?)", (name, code))
        bl_ids[code] = cur.lastrowid

    user_ids = {}
    for username, name, role, bl_code, active in USERS:
        cur = execute(
            "INSERT INTO users (username, name, role, business_line_id, active, token) VALUES (?,?,?,?,?,?)",
            (username, name, role, bl_ids.get(bl_code), active, f"tok-{username}-zhiyun"),
        )
        user_ids[username] = cur.lastrowid

    # 授权范围：业务线 × 环境 + 三个独立权限位
    for username, bl_code, env, view_secret, edit, manage in ACCESS:
        execute(
            """INSERT INTO user_access
               (user_id, business_line_id, environment,
                can_view_secret, can_edit_config, can_manage_app, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (user_ids[username], bl_ids[bl_code], env, view_secret, edit, manage, now, now),
        )

    app_ids: dict[str, int] = {}
    for (app_name, bl_code, owner, cluster, env, status, desc,
         env_count, days_ago) in APPS:
        created = now - days_ago * DAY - 3600
        cur = execute(
            """INSERT INTO applications
               (name, business_line_id, owner_id, cluster, environment, status,
                description, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (app_name, bl_ids[bl_code], user_ids.get(owner), cluster, env, status,
             desc, created, now - days_ago * DAY),
        )
        app_id = cur.lastrowid
        app_ids[app_name] = app_id
        for key, value in ENV_VAR_POOL[:env_count]:
            execute("INSERT INTO env_vars (app_id, key, value) VALUES (?,?,?)",
                    (app_id, key, value))
        execute(
            "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) VALUES (?,?,?,?,?)",
            (app_id, user_ids.get(owner), "创建应用", f"应用「{app_name}」创建，初始状态：在研",
             created),
        )
        # 近 7 天内有变更的应用：补一条状态流转日志，让控制台开箱有数据
        if days_ago <= 6 and status != "developing":
            execute(
                "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) VALUES (?,?,?,?,?)",
                (app_id, user_ids.get(owner) or user_ids["admin"], "状态变更",
                 f"状态流转至「{STATUS_LABELS[status]}」", now - days_ago * DAY),
            )

    seed_config_profiles(app_ids, user_ids, now)
    seed_handovers_and_perm_logs(app_ids, user_ids, bl_ids, now)
    return True


# ---------------------------------------------------------------- 配置档案种子

def _cfg(key, value, value_type="string", scope="global", is_secret=0):
    return {"key": key, "value": value, "value_type": value_type,
            "scope": scope, "is_secret": is_secret}


# 每个应用的配置档案按时间顺序给出多个版本；相邻版本自动产生逐键留痕。
# (应用名, 环境, [(操作人 username, 备注, 距今天数, [配置项...]) ...])
CONFIG_PROFILES = [
    ("支付网关", "prod", [
        ("zhangwei", "支付网关生产环境初始配置", 12, [
            _cfg("DB_HOST", "mysql-pay.prod.internal"),
            _cfg("DB_PORT", "3306", "number"),
            _cfg("DB_PASSWORD", "Pa$$w0rd-Init-2026", is_secret=1),
            _cfg("REDIS_URL", "redis://redis-prod:6379/0"),
            _cfg("REDIS_PASSWORD", "redis-init-secret", is_secret=1),
            _cfg("MQ_BROKER", "kafka://kafka-prod:9092"),
            _cfg("PAY_TIMEOUT_MS", "3000", "number"),
            _cfg("ENABLE_PROFIT_SHARING", "false", "boolean"),
            _cfg("LOG_LEVEL", "INFO"),
        ]),
        ("zhangwei", "缩短支付超时，关闭详细日志", 6, [
            _cfg("DB_HOST", "mysql-pay.prod.internal"),
            _cfg("DB_PORT", "3306", "number"),
            _cfg("DB_PASSWORD", "Pa$$w0rd-Init-2026", is_secret=1),
            _cfg("REDIS_URL", "redis://redis-prod:6379/0"),
            _cfg("REDIS_PASSWORD", "redis-init-secret", is_secret=1),
            _cfg("MQ_BROKER", "kafka://kafka-prod:9092"),
            _cfg("PAY_TIMEOUT_MS", "2000", "number"),
            _cfg("ENABLE_PROFIT_SHARING", "false", "boolean"),
            _cfg("LOG_LEVEL", "WARN"),
        ]),
        ("lina", "开启分账灰度并轮换数据库口令", 1, [
            _cfg("DB_HOST", "mysql-pay.prod.internal"),
            _cfg("DB_PORT", "3306", "number"),
            _cfg("DB_PASSWORD", "Pa$$w0rd-Rot-0918", is_secret=1),
            _cfg("REDIS_URL", "redis://redis-prod:6379/0"),
            _cfg("REDIS_PASSWORD", "redis-init-secret", is_secret=1),
            _cfg("MQ_BROKER", "kafka://kafka-prod-2:9092"),
            _cfg("PAY_TIMEOUT_MS", "2000", "number"),
            _cfg("ENABLE_PROFIT_SHARING", "true", "boolean", "canary"),
            _cfg("LOG_LEVEL", "WARN"),
            _cfg("RATE_LIMIT_QPS", "500", "number"),
        ]),
    ]),
    ("支付网关", "dev", [
        ("zhangwei", "支付网关开发环境配置", 9, [
            _cfg("DB_HOST", "mysql-pay.dev.internal"),
            _cfg("DB_PORT", "3306", "number"),
            _cfg("DB_PASSWORD", "dev-db-password", is_secret=1),
            _cfg("REDIS_URL", "redis://redis-dev:6379/0"),
            _cfg("MQ_BROKER", "kafka://kafka-dev:9092"),
            _cfg("PAY_TIMEOUT_MS", "5000", "number"),
            _cfg("ENABLE_PROFIT_SHARING", "true", "boolean"),
            _cfg("LOG_LEVEL", "DEBUG"),
            _cfg("MOCK_CHANNEL", "true", "boolean"),
        ]),
    ]),
    ("会员中心", "prod", [
        ("wangqiang", "会员中心生产环境初始配置", 8, [
            _cfg("DB_HOST", "mysql-growth.prod.internal"),
            _cfg("DB_PASSWORD", "member-db-secret", is_secret=1),
            _cfg("REDIS_URL", "redis://redis-growth:6379/1"),
            _cfg("POINT_EXPIRE_DAYS", "365", "number"),
            _cfg("LEVEL_RULES", '{"silver":1000,"gold":10000}', "json"),
            _cfg("LOG_LEVEL", "INFO"),
        ]),
        ("wangqiang", "积分有效期调整为 730 天", 2, [
            _cfg("DB_HOST", "mysql-growth.prod.internal"),
            _cfg("DB_PASSWORD", "member-db-secret", is_secret=1),
            _cfg("REDIS_URL", "redis://redis-growth:16379/1"),
            _cfg("POINT_EXPIRE_DAYS", "730", "number"),
            _cfg("LEVEL_RULES", '{"silver":1000,"gold":10000,"diamond":50000}', "json"),
            _cfg("LOG_LEVEL", "INFO"),
            _cfg("PUSH_ENABLED", "true", "boolean", "cluster"),
        ]),
    ]),
    ("实时数仓", "prod", [
        ("sunlei", "实时数仓生产配置", 4, [
            _cfg("FLINK_JOBMANAGER", "flink-jm.data.internal:8081"),
            _cfg("CHECKPOINT_INTERVAL_MS", "60000", "number"),
            _cfg("KAFKA_SASL_PASSWORD", "flink-kafka-secret", is_secret=1),
            _cfg("PARALLELISM", "8", "number"),
            _cfg("LOG_LEVEL", "INFO"),
        ]),
    ]),
]


def seed_config_profiles(app_ids: dict, user_ids: dict, now: int) -> None:
    """写入配置档案：每个版本一条快照 + 当前值表 + 逐键审计流水。"""
    for app_name, environment, versions in CONFIG_PROFILES:
        app_id = app_ids.get(app_name)
        if app_id is None:
            continue
        prev_map: dict[str, dict] = {}
        for idx, (username, note, days_ago, items) in enumerate(versions, start=1):
            uid = user_ids.get(username) or user_ids["admin"]
            ts = now - days_ago * DAY - 3600
            execute(
                """INSERT INTO config_versions
                   (app_id, environment, version, snapshot, change_note, created_by, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (app_id, environment, idx, json.dumps(items, ensure_ascii=False), note, uid, ts),
            )
            version_id = query_one(
                "SELECT id FROM config_versions WHERE app_id=? AND environment=? AND version=?",
                (app_id, environment, idx),
            )["id"]
            audit = []
            new_map = {it["key"]: it for it in items}
            for key in sorted(set(prev_map) | set(new_map)):
                old, new = prev_map.get(key), new_map.get(key)
                if old is None and new is not None:
                    audit.append(("add", key, None, new["value"], new["is_secret"], ""))
                elif new is None and old is not None:
                    audit.append(("remove", key, old["value"], None, old["is_secret"], ""))
                elif old is not None and new is not None:
                    meta = []
                    if old["value_type"] != new["value_type"]:
                        meta.append(f"类型 {old['value_type']}→{new['value_type']}")
                    if old["scope"] != new["scope"]:
                        meta.append(f"范围 {old['scope']}→{new['scope']}")
                    if old["is_secret"] != new["is_secret"]:
                        meta.append("密文标记变更")
                    audit.append(("update", key, old["value"], new["value"],
                                  old["is_secret"] or new["is_secret"], "；".join(meta)))
            for action, key, old_v, new_v, is_secret, reason in audit:
                execute(
                    """INSERT INTO config_audit_logs
                       (app_id, environment, version_id, user_id, action, config_key,
                        old_value, new_value, is_secret, reason, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (app_id, environment, version_id, uid, action, key,
                     old_v, new_v, is_secret, reason, ts),
                )
            prev_map = new_map

        # 当前值表落到最后一个版本
        latest = items
        latest_ts = ts
        execute("DELETE FROM config_items WHERE app_id = ? AND environment = ?",
                (app_id, environment))
        for it in latest:
            execute(
                """INSERT INTO config_items
                   (app_id, environment, key, value, value_type, scope, is_secret, updated_by, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (app_id, environment, it["key"], it["value"], it["value_type"],
                 it["scope"], it["is_secret"], uid, latest_ts),
            )

    # 一条"查看明文"留痕样例（昨天，李娜排查支付问题时申请查看库口令）
    pay = app_ids.get("支付网关")
    if pay:
        pwd = query_one(
            "SELECT id, key FROM config_items WHERE app_id=? AND environment='prod' AND is_secret=1 AND key='DB_PASSWORD'",
            (pay,),
        )
        if pwd:
            execute(
                """INSERT INTO config_audit_logs
                   (app_id, environment, version_id, user_id, action, config_key,
                    old_value, new_value, is_secret, reason, created_at)
                   VALUES (?,?,NULL,?, 'reveal', ?, NULL, NULL, 1, ?, ?)""",
                (pay, "prod", user_ids["lina"], pwd["key"],
                 "线上支付失败率升高，排查数据库连接鉴权问题，工单 INC-20260918-07",
                 now - DAY),
            )


# ---------------------------------------------------------------- 交接与权限变更留痕种子

def seed_handovers_and_perm_logs(app_ids: dict, user_ids: dict, bl_ids: dict, now: int) -> None:
    """离职交接样例 + 权限调整留痕样例。"""
    admin = user_ids["admin"]

    def plog(actor, target, bl_code, env, action, detail, reason, ts):
        execute(
            """INSERT INTO permission_logs
               (actor_id, target_user_id, business_line_id, environment, action, detail, reason, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (actor, target, bl_ids.get(bl_code) if bl_code else None, env,
             action, detail, reason, ts),
        )

    # 1) 陈晨转岗为生产值班：角色调整为只读观察者，仅保留 用户增长×生产 的可见范围
    t = now - 20 * DAY
    plog(admin, user_ids["chenchen"], "growth", None, "role_grant",
         f"角色调整：应用负责人 → {ROLE_LABELS['observer']}",
         "转岗为生产值班，只需只读巡检", t)
    plog(admin, user_ids["chenchen"], "growth", "prod", "access_grant",
         "新增授权「用户增长 · 生产」：仅可见（只读）",
         "转岗为生产值班，只需只读巡检", t)
    for env in ("dev", "test", "staging"):
        plog(admin, user_ids["chenchen"], "growth", env, "access_revoke",
             f"收回授权「用户增长 · {ENV_LABELS[env]}」（原权限：密文查看、配置编辑、应用管理）",
             "转岗为生产值班，非生产环境不再可见", t)

    # 2) 赵敏：供应链审计需要，授予生产/预发密文查看权（只读，不含编辑）
    t = now - 15 * DAY
    plog(admin, user_ids["zhaomin"], "supply", "prod", "access_grant",
         "新增授权「供应链 · 生产」：密文查看",
         "安全审计需要核对生产密文配置，明确不授予编辑权", t)
    plog(admin, user_ids["zhaomin"], "supply", "staging", "access_grant",
         "新增授权「供应链 · 预发」：密文查看",
         "预发验收需要核对密文，只读", t)

    # 3) 周婷：最小权限整改，收回密文查看权，保留生产/预发配置编辑权
    t = now - 7 * DAY
    plog(admin, user_ids["zhouting"], "data", "prod", "access_update",
         "调整授权「数据平台 · 生产」：密文查看收回",
         "最小权限整改：配置执行人不再需要查看明文，改密文走工单由负责人操作", t)
    plog(admin, user_ids["zhouting"], "data", "staging", "access_update",
         "调整授权「数据平台 · 预发」：密文查看收回",
         "最小权限整改：配置执行人不再需要查看明文", t)
    plog(admin, user_ids["zhouting"], "data", "dev", "access_revoke",
         "收回授权「数据平台 · 开发」（原权限：密文查看、配置编辑、应用管理）",
         "只负责生产与预发配置，开发环境移交孙磊", t)
    plog(admin, user_ids["zhouting"], "data", "test", "access_revoke",
         "收回授权「数据平台 · 测试」（原权限：密文查看、配置编辑、应用管理）",
         "只负责生产与预发配置，测试环境移交孙磊", t)

    # 4) 郭磊离职：清结算中心交接给李娜（4 天前），随后停用账号
    t = now - 4 * DAY
    settle_id = app_ids.get("清结算中心")
    guolei, lina = user_ids["guolei"], user_ids["lina"]
    perm_note = ("配置项随应用一并移交（配置档案/版本历史/逐键留痕归属不变，无需搬运）；"
                 "已将原负责人在「支付结算 · 开发/测试/预发/生产」等环境的权限同步授予新负责人 李娜；"
                 "原负责人 郭磊 在该业务线的授权已全部收回（涉及环境：开发/测试/预发/生产）")
    execute(
        """INSERT INTO handover_records
           (app_id, from_user_id, to_user_id, operator_id, reason, permissions_note,
            effective_at, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (settle_id, guolei, lina, admin,
         "郭磊离职转岗，清结算中心工作移交李娜承接",
         perm_note, t, t),
    )
    execute(
        "INSERT INTO change_logs (app_id, user_id, action, detail, created_at) VALUES (?,?,?,?,?)",
        (settle_id, admin, "应用交接",
         "应用负责人交接：郭磊 → 李娜，原因：郭磊离职转岗，清结算中心工作移交李娜承接", t),
    )
    plog(admin, lina, "pay", None, "handover",
         "应用「清结算中心」交接：郭磊 → 李娜；原负责人 郭磊 在该业务线的授权已全部收回",
         "郭磊离职转岗，清结算中心工作移交李娜承接", t)
    plog(admin, guolei, "pay", None, "handover",
         "应用「清结算中心」交出：郭磊 → 李娜",
         "郭磊离职转岗，清结算中心工作移交李娜承接", t)

    # 交接完成次日停用离职账号
    t2 = now - 3 * DAY
    execute("UPDATE users SET active = 0 WHERE id = ?", (guolei,))
    plog(admin, guolei, "pay", None, "deactivate",
         "账号停用（离职/转岗）：郭磊（guolei），授权范围已全部收回",
         "离职交接全部完成，停用账号", t2)
