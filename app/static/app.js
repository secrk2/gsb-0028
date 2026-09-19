/* ============ 织云系统 前端 ============ */
"use strict";

const state = {
  token: localStorage.getItem("zy_token") || "",
  user: null,
  meta: null,
  businessLines: [],
  users: [],
  configMeta: null,
  myPerms: null,
  filters: { business_line_id: "", owner_id: "", environment: "", status: "", q: "" },
  configFilters: { business_line_id: "", environment: "" },
  auditFilters: { app_id: "", business_line_id: "", environment: "", action: "", start: "", end: "" },
};

/* ---------------- 权限 helper ---------------- */
function isAdmin() {
  return state.user && state.user.role === "admin";
}
function roleLabelOf(u) {
  return (u && (u.role_label || (state.configMeta && state.configMeta.roles || [])
    .find((r) => r.value === u.role)?.label)) || u?.role || "";
}
// 当前账号在某 业务线×环境 是否可见 / 是否持有某权限位
function accessAt(blId, env) {
  if (isAdmin()) {
    return { can_view_secret: true, can_edit_config: true, can_manage_app: true };
  }
  const rows = (state.myPerms && state.myPerms.access) || [];
  return rows.find((a) => String(a.business_line_id) === String(blId) && a.environment === env) || null;
}
function canAt(blId, env, perm) {
  if (isAdmin()) return true;
  const a = accessAt(blId, env);
  return !!(a && a[perm]);
}
function envVisible(blId, env) {
  if (isAdmin()) return true;
  return !!accessAt(blId, env);
}
function hasAnyPerm(perm) {
  if (isAdmin()) return true;
  return ((state.myPerms && state.myPerms.access) || []).some((a) => a[perm]);
}

/* ---------------- 工具 ---------------- */
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function toast(msg, type) {
  const el = document.createElement("div");
  el.className = "toast" + (type ? " " + type : "");
  el.textContent = msg;
  $("#toast-root").appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtAgo(ts) {
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 7 * 86400) return `${Math.floor(diff / 86400)} 天前`;
  return fmtTime(ts).slice(0, 10);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: {
      "Content-Type": "application/json",
      ...(state.token ? { "X-Token": state.token } : {}),
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* ignore */ }
  if (!res.ok) {
    let message = `请求失败（HTTP ${res.status}）`;
    if (data) {
      if (typeof data.detail === "string") message = data.detail;
      else if (data.detail && typeof data.detail === "object" && data.detail.message) message = data.detail.message;
    }
    if (res.status === 401) { logout(false); }
    const error = new Error(message);
    error.status = res.status;
    error.data = data;   // 结构化错误体（如 409 乐观锁冲突详情）
    throw error;
  }
  return data;
}

function statusBadge(app) {
  return `<span class="badge ${esc(app.status)}">${esc(app.status_label)}</span>`;
}

function envTag(app) {
  return `<span class="env-tag ${esc(app.environment)}">${esc(app.environment_label)}</span>`;
}

function redDotsHtml(app) {
  if (!app.red_dots || !app.red_dots.length) return "";
  return app.red_dots.map((d) => `<span class="red-dot">${esc(d.label)}</span>`).join(" ");
}

/* ---------------- 登录 ---------------- */
async function showLogin() {
  $("#topnav").hidden = true;
  $("#user-chip").hidden = true;
  $("#view").innerHTML = "";
  const overlay = $("#login-overlay");
  overlay.hidden = false;
  const box = $("#login-users");
  box.innerHTML = `<div class="empty-tip">加载账号中…</div>`;
  try {
    const users = await api("/api/public/users");
    box.innerHTML = users.map((u) => `
      <div class="login-user" data-username="${esc(u.username)}">
        <div class="u-name">${esc(u.name)}</div>
        <div class="u-meta">${esc(u.business_line_name || "平台")}</div>
        <span class="u-role ${u.role === "admin" ? "admin" : ""}">${esc(u.role_label || u.role)}</span>
      </div>`).join("");
    $$(".login-user", box).forEach((el) => {
      el.onclick = () => doLogin(el.dataset.username);
    });
  } catch (e) {
    box.innerHTML = `<div class="empty-tip">账号加载失败：${esc(e.message)}</div>`;
  }
}

async function doLogin(username) {
  try {
    const data = await api("/api/login", { method: "POST", body: { username } });
    state.token = data.token;
    state.user = data.user;
    localStorage.setItem("zy_token", data.token);
    $("#login-overlay").hidden = true;
    await bootstrap();
    toast(`欢迎，${data.user.name}`, "success");
  } catch (e) {
    toast(e.message, "error");
  }
}

function logout(showTip = true) {
  state.token = "";
  state.user = null;
  localStorage.removeItem("zy_token");
  if (showTip) toast("已退出登录");
  showLogin();
}

/* ---------------- 启动 ---------------- */
async function bootstrap() {
  try {
    if (!state.user) state.user = await api("/api/me");
    const [meta, bls, usrs, cfgMeta, myPerms] = await Promise.all([
      api("/api/meta"), api("/api/business-lines"), api("/api/users"),
      api("/api/config/meta"), api("/api/my/permissions"),
    ]);
    state.meta = meta;
    state.businessLines = bls;
    state.users = usrs;
    state.configMeta = cfgMeta;
    state.myPerms = myPerms;
  } catch (e) {
    if (e.status !== 401) toast(e.message, "error");
    return;
  }
  $("#topnav").hidden = false;
  // 平台管理员与业务线负责人可见「权限与交接」
  if (canManagePermissions()) {
    if (!$("#nav-perms")) {
      $("#topnav").insertAdjacentHTML(
        "beforeend", '<a href="#/perms" data-route="perms" id="nav-perms">权限与交接</a>');
    }
  } else {
    const a = $("#nav-perms");
    if (a) a.remove();
  }
  const chip = $("#user-chip");
  chip.hidden = false;
  const rl = state.user.role_label || roleLabelOf(state.user);
  chip.innerHTML = `
    <span class="chip-role">${esc(rl)}</span>
    <span>${esc(state.user.name)} · ${esc(state.user.business_line_name || "平台")}</span>
    <button class="logout" id="btn-logout">退出</button>`;
  $("#btn-logout").onclick = () => logout();
  route();
}

function route() {
  if (!state.user) return;
  const hash = location.hash || "#/console";
  const parts = hash.replace(/^#\//, "").split("/");
  $$("#topnav a").forEach((a) => a.classList.toggle("active", a.dataset.route === parts[0]));
  if (parts[0] === "apps" && parts[1]) renderAppDetail(parts[1]);
  else if (parts[0] === "apps") renderApps();
  else if (parts[0] === "config") {
    if (parts[1]) renderConfigProfile(parts[1], parts[2] || "");
    else renderConfigList();
  } else if (parts[0] === "audit") renderAudit();
  else if (parts[0] === "perms") renderPermsPage();
  else renderConsole();
}

window.addEventListener("hashchange", () => { if (state.user) route(); });

/* ---------------- 资产控制台 ---------------- */
async function renderConsole() {
  const view = $("#view");
  view.innerHTML = `<div class="empty-tip">加载中…</div>`;
  let data;
  try {
    data = await api("/api/console/summary");
  } catch (e) {
    view.innerHTML = errorStateHtml("加载失败", e.message);
    return;
  }
  const t = data.totals;
  const maxTotal = Math.max(1, ...data.by_business_line.map((b) => b.total));

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h2>资产控制台</h2>
        <div class="sub">${isAdmin() ? "全部业务线 · 全部环境" : esc(state.myPerms ? state.myPerms.scope_text : "授权范围")} · 应用资产概览</div>
      </div>
    </div>

    <div class="stats-grid">
      <div class="panel stat-card"><div class="num">${t.apps}</div><div class="label">应用总数</div></div>
      <div class="panel stat-card"><div class="num">${t.business_lines}</div><div class="label">业务线</div></div>
      <div class="panel stat-card"><div class="num">${t.recent_changed}</div><div class="label">近 7 天有变更</div></div>
      <div class="panel stat-card"><div class="num ${t.red_dot_apps ? "red" : ""}">${t.red_dot_apps}</div><div class="label">红点待处理应用</div></div>
    </div>

    <div class="dash-grid">
      <div class="panel panel-pad">
        <h3>各业务线应用数分布</h3>
        ${data.by_business_line.map((b) => `
          <div class="bl-row">
            <span class="bl-name" title="${esc(b.name)}">${esc(b.name)}</span>
            <div class="bl-bar-track">
              ${["developing", "online", "maintenance", "offline"].map((s) =>
                b[s] ? `<div class="bl-bar-seg ${s}" style="width:${(b[s] / maxTotal) * 100}%" title="${s}: ${b[s]}"></div>` : "").join("")}
            </div>
            <span class="bl-count">${b.total} 个应用</span>
          </div>`).join("")}
        <div class="legend">
          <span><i style="background:#6a9bff"></i>在研</span>
          <span><i style="background:#34c77b"></i>上线</span>
          <span><i style="background:#f0b429"></i>维保</span>
          <span><i style="background:#c3cad6"></i>下线</span>
        </div>
      </div>

      <div class="panel panel-pad">
        <h3>近 7 天有变更的应用</h3>
        <div class="mini-list">
          ${data.recent_changed_apps.length ? data.recent_changed_apps.map((a) => `
            <div class="mini-item" data-app-id="${a.id}">
              <span class="mini-title">${esc(a.name)}</span>
              <span class="badge ${esc(a.status)}">${esc({ developing: "在研", online: "上线", maintenance: "维保", offline: "下线" }[a.status])}</span>
              <span class="mini-meta">${esc(a.business_line_name)} · ${a.change_count} 次变更 · ${fmtAgo(a.last_changed_at)}</span>
            </div>`).join("") : `<div class="empty-tip">近 7 天暂无变更</div>`}
        </div>
      </div>

      <div class="panel panel-pad span-full">
        <h3>红点提醒（环境变量缺失 / 缺失负责人）</h3>
        <div class="red-groups">
          <div>
            <p><span class="red-dot">缺失负责人（${data.red_dots.missing_owner.length}）</span></p>
            <div class="chip-list">
              ${data.red_dots.missing_owner.length
                ? data.red_dots.missing_owner.map((a) => `<span class="app-chip" data-app-id="${a.id}">${esc(a.name)} · ${esc(a.business_line_name)}</span>`).join("")
                : `<span class="empty-tip">无</span>`}
            </div>
          </div>
          <div>
            <p><span class="red-dot">环境变量缺失（${data.red_dots.missing_env.length}）</span></p>
            <div class="chip-list">
              ${data.red_dots.missing_env.length
                ? data.red_dots.missing_env.map((a) => `<span class="app-chip" data-app-id="${a.id}">${esc(a.name)} · ${esc(a.business_line_name)}</span>`).join("")
                : `<span class="empty-tip">无</span>`}
            </div>
          </div>
        </div>
      </div>
    </div>`;

  $$("[data-app-id]", view).forEach((el) => {
    el.onclick = () => { location.hash = `#/apps/${el.dataset.appId}`; };
  });
}

/* ---------------- 应用台账 ---------------- */
async function renderApps() {
  const view = $("#view");
  const f = state.filters;
  const admin = isAdmin();
  // 非管理员：环境下拉只保留自己被授权过的环境
  const myEnvs = admin
    ? state.meta.environments
    : state.meta.environments.filter((e) =>
        (state.myPerms.access || []).some((a) => a.environment === e.value));
  const canCreate = admin || hasAnyPerm("can_manage_app");

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h2>应用台账</h2>
        <div class="sub">${admin ? "全部业务线 · 全部环境" : esc(state.myPerms.scope_text)} · 支持业务线 / 负责人 / 环境 / 状态组合筛选</div>
      </div>
      ${canCreate ? '<button class="btn primary" id="btn-new-app">+ 新建应用</button>'
                  : '<span class="perm-hint">只读账号无新建权，如需开通请联系平台管理员</span>'}
    </div>
    <div class="panel filter-bar">
      <select id="f-bl" ${admin ? "" : "disabled"}>
        <option value="">全部业务线</option>
        ${state.businessLines.map((b) => `<option value="${b.id}" ${String(b.id) === String(f.business_line_id) ? "selected" : ""}>${esc(b.name)}</option>`).join("")}
      </select>
      <select id="f-owner">
        <option value="">全部负责人</option>
        ${state.users.map((u) => `<option value="${u.id}" ${String(u.id) === String(f.owner_id) ? "selected" : ""}>${esc(u.name)}（${esc(u.business_line_name || "平台")}）</option>`).join("")}
      </select>
      <select id="f-env">
        <option value="">全部环境</option>
        ${myEnvs.map((e) => `<option value="${e.value}" ${f.environment === e.value ? "selected" : ""}>${esc(e.label)}</option>`).join("")}
      </select>
      <select id="f-status">
        <option value="">全部状态</option>
        ${state.meta.statuses.map((s) => `<option value="${s.value}" ${f.status === s.value ? "selected" : ""}>${esc(s.label)}</option>`).join("")}
      </select>
      <input id="f-q" class="grow" placeholder="搜索应用名…" value="${esc(f.q)}">
      <button class="btn" id="btn-reset">重置</button>
    </div>
    <div id="apps-result"><div class="empty-tip">加载中…</div></div>`;

  $("#btn-new-app").onclick = () => openAppModal();
  $("#btn-reset").onclick = () => {
    state.filters = { business_line_id: "", owner_id: "", environment: "", status: "", q: "" };
    renderApps();
  };
  const reload = () => {
    state.filters = {
      business_line_id: $("#f-bl").value,
      owner_id: $("#f-owner").value,
      environment: $("#f-env").value,
      status: $("#f-status").value,
      q: $("#f-q").value.trim(),
    };
    loadAppList();
  };
  ["#f-bl", "#f-owner", "#f-env", "#f-status"].forEach((sel) => { $(sel).onchange = reload; });
  let timer = null;
  $("#f-q").oninput = () => { clearTimeout(timer); timer = setTimeout(reload, 300); };
  $("#f-q").onkeydown = (e) => { if (e.key === "Enter") reload(); };

  await loadAppList();
}

async function loadAppList() {
  const box = $("#apps-result");
  const p = new URLSearchParams();
  const f = state.filters;
  if (f.business_line_id) p.set("business_line_id", f.business_line_id);
  if (f.owner_id) p.set("owner_id", f.owner_id);
  if (f.environment) p.set("environment", f.environment);
  if (f.status) p.set("status", f.status);
  if (f.q) p.set("q", f.q);
  let apps;
  try {
    apps = await api(`/api/apps?${p.toString()}`);
  } catch (e) {
    box.innerHTML = errorStateHtml(e.status === 403 ? "403 无权访问" : "加载失败", e.message);
    return;
  }
  if (!apps.length) {
    box.innerHTML = `<div class="panel panel-pad empty-tip">没有符合条件的应用</div>`;
    return;
  }

  box.innerHTML = `
    <div class="panel table-wrap">
      <table class="app-table">
        <thead><tr>
          <th>应用</th><th>业务线</th><th>负责人</th><th>集群</th><th>环境</th><th>状态</th><th>风险提示</th><th>更新时间</th>
        </tr></thead>
        <tbody>
          ${apps.map((a) => `
            <tr data-app-id="${a.id}">
              <td class="app-name-cell">${esc(a.name)}<div class="app-desc">${esc(a.description || "")}</div></td>
              <td>${esc(a.business_line_name)}</td>
              <td>${a.owner_name ? esc(a.owner_name) : '<span class="red-dot">未设置</span>'}</td>
              <td>${esc(a.cluster)}</td>
              <td>${envTag(a)}</td>
              <td>${statusBadge(a)}</td>
              <td>${redDotsHtml(a) || '<span style="color:var(--ink-3)">—</span>'}</td>
              <td title="${fmtTime(a.updated_at)}">${fmtAgo(a.updated_at)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>
    <div class="card-list">
      ${apps.map((a) => `
        <div class="panel app-card" data-app-id="${a.id}">
          <div class="card-head">
            <span class="name">${esc(a.name)}</span>
            ${envTag(a)}
            ${statusBadge(a)}
          </div>
          <div class="card-fields">
            <div class="cf"><span class="k">业务线</span>${esc(a.business_line_name)}</div>
            <div class="cf"><span class="k">负责人</span>${a.owner_name ? esc(a.owner_name) : "未设置"}</div>
            <div class="cf"><span class="k">集群</span>${esc(a.cluster)}</div>
            <div class="cf"><span class="k">更新</span>${fmtAgo(a.updated_at)}</div>
          </div>
          ${a.red_dots.length ? `<div class="card-red">${redDotsHtml(a)}</div>` : ""}
        </div>`).join("")}
    </div>`;

  $$("[data-app-id]", box).forEach((el) => {
    el.onclick = () => { location.hash = `#/apps/${el.dataset.appId}`; };
  });
}

/* ---------------- 应用详情 ---------------- */
const STATUS_FLOW = ["developing", "online", "maintenance", "offline"];

async function renderAppDetail(appId) {
  const view = $("#view");
  view.innerHTML = `<div class="empty-tip">加载中…</div>`;
  let app;
  try {
    app = await api(`/api/apps/${appId}`);
  } catch (e) {
    // 越权 / 不存在：明确错误态，而不是空白页
    view.innerHTML = errorStateHtml(
      e.status === 403 ? "403 无权访问" : e.status === 404 ? "404 应用不存在" : "加载失败",
      e.message
    );
    return;
  }
  const isOffline = app.status === "offline";
  const curIdx = STATUS_FLOW.indexOf(app.status);
  const nextStatuses = STATUS_FLOW.slice(curIdx + 1);
  const perms = app.permissions || { can_manage_app: false };
  const canManage = !!perms.can_manage_app;

  view.innerHTML = `
    <div class="page-head">
      <div>
        <h2>${esc(app.name)} ${statusBadge(app)}</h2>
        <div class="sub">${esc(app.business_line_name)} · ${esc(app.cluster)} · ${esc(app.environment_label)}环境</div>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn" id="btn-back">← 返回台账</button>
        <button class="btn" id="btn-config">配置档案</button>
        ${canManage && !isOffline ? '<button class="btn" id="btn-edit">编辑信息</button>' : ""}
        ${canManage ? '<button class="btn warn" id="btn-handover">应用交接</button>' : ""}
      </div>
    </div>

    ${!canManage ? `<div class="panel perm-banner">
      <span>🔒 当前账号在「${esc(app.business_line_name)} · ${esc(app.environment_label)}」没有<b>应用管理权</b>：
      可查看本页信息，但不能编辑、流转状态、改环境变量或发起交接。</span>
    </div>` : ""}

    ${app.red_dots.length ? `<div class="panel panel-pad" style="margin-bottom:16px">
      <span style="margin-right:10px;color:var(--ink-2)">风险提示：</span>${redDotsHtml(app)}
    </div>` : ""}

    <div class="detail-grid">
      <div>
        <div class="panel panel-pad" style="margin-bottom:16px">
          <h3>生命周期</h3>
          <div class="lifecycle">
            ${STATUS_FLOW.map((s, i) => `
              ${i > 0 ? `<div class="lc-line ${i <= curIdx ? "done" : ""}"></div>` : ""}
              <div class="lc-step ${i < curIdx ? "done" : ""} ${i === curIdx ? "current" : ""}">
                <span class="lc-dot">${i + 1}</span>
                <span class="lc-label">${esc(state.meta.statuses[i].label)}</span>
              </div>`).join("")}
          </div>
          ${isOffline
            ? `<p style="color:var(--ink-3);font-size:13px">应用已下线（终态），所有信息只读，不能再变更。</p>`
            : !canManage
              ? `<p style="color:var(--ink-3);font-size:13px">当前账号无应用管理权，状态流转需负责人或管理员操作。</p>`
              : `<div class="status-actions">
                <span style="color:var(--ink-2);font-size:13px;align-self:center">流转到：</span>
                ${nextStatuses.map((s) => {
                  const label = state.meta.statuses.find((x) => x.value === s).label;
                  return `<button class="btn small ${s === "offline" ? "danger" : "primary"}" data-to-status="${s}">${esc(label)}</button>`;
                }).join("")}
              </div>`}
        </div>

        <div class="panel panel-pad" style="margin-bottom:16px">
          <h3>基本信息</h3>
          <div class="kv-grid">
            <div class="kv"><div class="k">应用 ID</div><div class="v">#${app.id}</div></div>
            <div class="kv"><div class="k">所属业务线</div><div class="v">${esc(app.business_line_name)}</div></div>
            <div class="kv"><div class="k">负责人</div><div class="v">${app.owner_name ? esc(app.owner_name) : '<span class="red-dot">缺失负责人</span>'}</div></div>
            <div class="kv"><div class="k">所属集群</div><div class="v">${esc(app.cluster)}</div></div>
            <div class="kv"><div class="k">环境</div><div class="v">${envTag(app)}</div></div>
            <div class="kv"><div class="k">创建时间</div><div class="v">${fmtTime(app.created_at)}</div></div>
            <div class="kv" style="grid-column:1/-1"><div class="k">描述</div><div class="v">${esc(app.description || "—")}</div></div>
          </div>
        </div>

        <div class="panel panel-pad">
          <h3>环境变量 ${app.env_vars.length === 0 ? '<span class="red-dot">环境变量缺失</span>' : `<span style="color:var(--ink-3);font-weight:400;font-size:12px">（${app.env_vars.length} 项）</span>`}</h3>
          <div id="env-editor"></div>
        </div>
      </div>

      <div>
        <div class="panel panel-pad" id="handover-panel" style="margin-bottom:16px">
          <h3>交接留痕 <span style="font-weight:400;font-size:12px;color:var(--ink-3)">应用归属与权限的交接记录</span></h3>
          <div id="handover-list"><div class="empty-tip">加载中…</div></div>
        </div>

        <div class="panel panel-pad">
          <h3>变更记录</h3>
          <div class="log-list">
            ${app.change_logs.length ? app.change_logs.map((l) => `
              <div class="log-item">
                <div><b>${esc(l.action)}</b> · ${esc(l.detail)}</div>
                <div class="log-meta">${esc(l.user_name || "系统")} · ${fmtTime(l.created_at)}</div>
              </div>`).join("") : `<div class="empty-tip">暂无变更记录</div>`}
          </div>
        </div>
      </div>
    </div>`;

  $("#btn-back").onclick = () => { location.hash = "#/apps"; };
  $("#btn-config").onclick = () => { location.hash = `#/config/${app.id}/${app.environment}`; };
  const editBtn = $("#btn-edit");
  if (editBtn) editBtn.onclick = () => openAppModal(app);
  const hoBtn = $("#btn-handover");
  if (hoBtn) hoBtn.onclick = () => openHandoverModal(app);
  $$("[data-to-status]", view).forEach((btn) => {
    btn.onclick = () => transitionStatus(app.id, btn.dataset.toStatus);
  });
  renderEnvEditor(app);
  loadHandovers(app.id);
}

async function loadHandovers(appId) {
  const box = $("#handover-list");
  if (!box) return;
  try {
    const rows = await api(`/api/apps/${appId}/handovers`);
    if (!rows.length) {
      box.innerHTML = `<div class="empty-tip">暂无交接记录</div>`;
      return;
    }
    box.innerHTML = rows.map((h) => `
      <div class="handover-item">
        <div><b>${esc(h.from_name)}</b> → <b>${esc(h.to_name)}</b>
          <span class="meta-tag">${fmtTime(h.effective_at)} 生效</span></div>
        <div class="log-meta">交接原因：${esc(h.reason)} · 操作人：${esc(h.operator_name)}</div>
        <div class="ho-note">${esc(h.permissions_note)}</div>
      </div>`).join("");
  } catch (e) {
    box.innerHTML = `<div class="empty-tip">交接记录加载失败：${esc(e.message)}</div>`;
  }
}

function renderEnvEditor(app) {
  const box = $("#env-editor");
  const noPerm = !(app.permissions && app.permissions.can_manage_app);
  const readOnly = app.status === "offline" || noPerm;
  const rows = app.env_vars.map((v) => ({ ...v }));
  if (!rows.length) rows.push({ key: "", value: "" });

  box.innerHTML = `
    <table class="env-table">
      <thead><tr><th style="width:38%">KEY</th><th>VALUE</th>${readOnly ? "" : '<th style="width:52px"></th>'}</tr></thead>
      <tbody id="env-tbody"></tbody>
    </table>
    ${app.status === "offline"
      ? `<p style="color:var(--ink-3);font-size:13px">应用已下线（终态），环境变量只读。</p>`
      : noPerm
        ? `<p style="color:var(--ink-3);font-size:13px">当前账号在该业务线×环境无应用管理权，环境变量只读；需要修改请联系负责人或平台管理员。</p>`
        : `<div style="display:flex;gap:8px;margin-top:10px">
          <button class="btn small" id="env-add">+ 添加一行</button>
          <button class="btn primary small" id="env-save">保存环境变量</button>
        </div>`}`;

  const tbody = $("#env-tbody", box);
  function paint() {
    tbody.innerHTML = rows.map((r, i) => `
      <tr>
        <td><input data-i="${i}" data-f="key" placeholder="如 DB_HOST" value="${esc(r.key)}" ${readOnly ? "disabled" : ""}></td>
        <td><input data-i="${i}" data-f="value" placeholder="值" value="${esc(r.value)}" ${readOnly ? "disabled" : ""}></td>
        ${readOnly ? "" : `<td><button class="btn small danger" data-del="${i}">删</button></td>`}
      </tr>`).join("");
    $$("input", tbody).forEach((inp) => {
      inp.oninput = () => { rows[+inp.dataset.i][inp.dataset.f] = inp.value; };
    });
    $$("[data-del]", tbody).forEach((btn) => {
      btn.onclick = () => { rows.splice(+btn.dataset.del, 1); if (!rows.length) rows.push({ key: "", value: "" }); paint(); };
    });
  }
  paint();

  if (!readOnly) {
    $("#env-add", box).onclick = () => { rows.push({ key: "", value: "" }); paint(); };
    $("#env-save", box).onclick = async () => {
      const vars = rows.filter((r) => r.key.trim()).map((r) => ({ key: r.key.trim(), value: r.value }));
      try {
        await api(`/api/apps/${app.id}/env-vars`, { method: "PUT", body: { vars } });
        toast("环境变量已保存", "success");
        renderAppDetail(app.id);
      } catch (e) { toast(e.message, "error"); }
    };
  }
}

async function transitionStatus(appId, toStatus) {
  const label = state.meta.statuses.find((s) => s.value === toStatus).label;
  if (toStatus === "offline" && !confirm(`确认将应用流转到「${label}」？下线为终态，不可再变更。`)) return;
  try {
    await api(`/api/apps/${appId}/status`, { method: "POST", body: { status: toStatus } });
    toast(`已流转到「${label}」`, "success");
  } catch (e) {
    // 非法状态回退等：展示后端给出的具体原因
    toast(e.message, "error");
  }
  renderAppDetail(appId);
}

/* ---------------- 新建 / 编辑弹窗 ---------------- */
function openAppModal(app) {
  const isEdit = !!app;
  const admin = isAdmin();
  const root = $("#modal-root");
  const ownerOptions = (blId) => state.users
    .filter((u) => !blId || String(u.business_line_id) === String(blId))
    .map((u) => `<option value="${u.id}" ${app && app.owner_id === u.id ? "selected" : ""}>${esc(u.name)}</option>`).join("");
  // 非管理员只能在自己持"应用管理权"的 业务线×环境 上新建
  const manageableBLs = admin
    ? state.businessLines.map((b) => b.id)
    : [...new Set((state.myPerms.access || []).filter((a) => a.can_manage_app).map((a) => a.business_line_id))];
  const blChoices = state.businessLines.filter((b) => manageableBLs.includes(b.id));
  const envsForBL = (blId) => admin
    ? state.meta.environments
    : state.meta.environments.filter((e) => canAt(blId, e.value, "can_manage_app"));

  const initBL = isEdit ? app.business_line_id
    : (blChoices.find((b) => String(b.id) === String(state.filters.business_line_id))?.id
       || blChoices[0]?.id);
  const initEnv = isEdit ? app.environment
    : (envsForBL(initBL).find((e) => e.value === state.filters.environment)?.value
       || envsForBL(initBL)[0]?.value);

  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal">
        <h3>${isEdit ? `编辑应用 #${app.id}` : "新建应用"}</h3>
        <div class="form-grid">
          <div>
            <label>应用名称 *</label>
            <input id="m-name" maxlength="64" value="${isEdit ? esc(app.name) : ""}" placeholder="同一业务线下不可重名">
          </div>
          <div>
            <label>所属业务线 *</label>
            <select id="m-bl" ${isEdit ? "disabled" : ""}>
              ${blChoices.map((b) => `<option value="${b.id}" ${String(b.id) === String(initBL) ? "selected" : ""}>${esc(b.name)}</option>`).join("")}
            </select>
          </div>
          <div>
            <label>负责人</label>
            <select id="m-owner"><option value="">（暂不指定）</option>${ownerOptions(initBL)}</select>
          </div>
          <div>
            <label>所属集群 *</label>
            <select id="m-cluster">
              ${state.meta.clusters.map((c) => `<option ${isEdit && app.cluster === c ? "selected" : ""}>${esc(c)}</option>`).join("")}
            </select>
          </div>
          <div>
            <label>环境 *${isEdit ? "（改挂环境需在目标环境也有应用管理权）" : ""}</label>
            <select id="m-env">
              ${(isEdit ? state.meta.environments : envsForBL(initBL)).map((e) =>
                `<option value="${e.value}" ${initEnv === e.value ? "selected" : ""}>${esc(e.label)}</option>`).join("")}
            </select>
          </div>
          <div class="full">
            <label>描述</label>
            <textarea id="m-desc" rows="2" placeholder="应用用途简述">${isEdit ? esc(app.description || "") : ""}</textarea>
          </div>
        </div>
        <div class="form-error" id="m-error"></div>
        <div class="form-actions">
          <button class="btn" id="m-cancel">取消</button>
          <button class="btn primary" id="m-submit">${isEdit ? "保存" : "创建"}</button>
        </div>
      </div>
    </div>`;

  const close = () => { root.innerHTML = ""; };
  $("#m-cancel").onclick = close;
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) close(); };

  const blSel = $("#m-bl");
  const envSel = $("#m-env");
  if (!isEdit) {
    blSel.onchange = () => {
      $("#m-owner").innerHTML = `<option value="">（暂不指定）</option>` + ownerOptions(blSel.value);
      envSel.innerHTML = envsForBL(blSel.value).map((e) => `<option value="${e.value}">${esc(e.label)}</option>`).join("");
    };
  }

  $("#m-submit").onclick = async () => {
    const errBox = $("#m-error");
    errBox.classList.remove("show");
    const name = $("#m-name").value.trim();
    if (!name) { errBox.textContent = "应用名称不能为空"; errBox.classList.add("show"); return; }
    try {
      if (isEdit) {
        await api(`/api/apps/${app.id}`, {
          method: "PATCH",
          body: {
            name,
            set_owner: true,
            owner_id: $("#m-owner").value ? +$("#m-owner").value : null,
            cluster: $("#m-cluster").value,
            environment: $("#m-env").value,
            description: $("#m-desc").value.trim(),
          },
        });
        toast("应用信息已更新", "success");
        close();
        renderAppDetail(app.id);
      } else {
        await api("/api/apps", {
          method: "POST",
          body: {
            name,
            business_line_id: +blSel.value,
            owner_id: $("#m-owner").value ? +$("#m-owner").value : null,
            cluster: $("#m-cluster").value,
            environment: $("#m-env").value,
            description: $("#m-desc").value.trim(),
          },
        });
        toast(`应用「${name}」创建成功（初始状态：在研）`, "success");
        close();
        renderApps();
      }
    } catch (e) {
      // 重名 409 / 校验失败等：表单内展示原因
      errBox.textContent = e.message;
      errBox.classList.add("show");
    }
  };
}

/* ---------------- 应用交接（归属 + 权限，留痕） ---------------- */
function openHandoverModal(app) {
  const root = $("#modal-root");
  // 候选新负责人：同业务线、启用中、非观察者、排除现任
  const candidates = state.users.filter(
    (u) => String(u.business_line_id) === String(app.business_line_id)
      && u.active !== false && u.role !== "observer" && u.id !== app.owner_id);
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:600px">
        <h3>应用交接 · ${esc(app.name)}</h3>
        <p style="color:var(--ink-2);font-size:13px">
          交接的是<b>应用归属与权限</b>：配置项随应用一并移交（配置档案/版本/留痕归属不变，无需搬运）；
          新负责人会获得原负责人在该业务线各环境的权限，并可选择是否收回原负责人的权限。
          交接前后谁负责、权限如何处置都会留痕。
        </p>
        <div class="form-grid" style="grid-template-columns:1fr">
          <div>
            <label>当前负责人</label>
            <input value="${esc(app.owner_name || "（未设置）")}" disabled>
          </div>
          <div>
            <label>移交给 *</label>
            <select id="ho-to">
              <option value="">请选择新负责人（须同业务线）</option>
              ${candidates.map((u) => `<option value="${u.id}">${esc(u.name)}（${esc(u.role_label || u.role)}）</option>`).join("")}
            </select>
          </div>
          <div>
            <label class="check-line">
              <input type="checkbox" id="ho-revoke" checked>
              交接后收回原负责人在「${esc(app.business_line_name)}」的全部权限（离职/转岗场景勾选；并行协助可取消）
            </label>
          </div>
          <div>
            <label>交接原因 *（不少于 5 字）</label>
            <textarea id="ho-reason" rows="3" placeholder="如：负责人离职转岗，应用移交新团队承接，工单号 HR-2026-091"></textarea>
          </div>
        </div>
        <div class="form-error" id="ho-error"></div>
        <div class="form-actions">
          <button class="btn" id="ho-cancel">取消</button>
          <button class="btn warn primary" id="ho-submit">确认交接并留痕</button>
        </div>
      </div>
    </div>`;
  const close = () => { root.innerHTML = ""; };
  $("#ho-cancel").onclick = close;
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) close(); };
  $("#ho-submit").onclick = async () => {
    const errBox = $("#ho-error");
    errBox.classList.remove("show");
    const toUserId = $("#ho-to").value;
    const reason = $("#ho-reason").value.trim();
    if (!toUserId) { errBox.textContent = "请选择新负责人"; errBox.classList.add("show"); return; }
    if (reason.length < 5) { errBox.textContent = "交接原因不少于 5 个字"; errBox.classList.add("show"); return; }
    if (!confirm("确认交接？应用归属与权限将立即转移并写入交接留痕。")) return;
    try {
      const res = await api(`/api/apps/${app.id}/handover`, {
        method: "POST",
        body: { to_user_id: +toUserId, reason, revoke_from: $("#ho-revoke").checked },
      });
      close();
      toast("交接完成，权限处置与交接记录已留痕", "success");
      renderAppDetail(app.id);
    } catch (e) {
      errBox.textContent = e.message;
      errBox.classList.add("show");
    }
  };
}

/* ---------------- 配置档案 · 列表 ---------------- */
function cfgTypeLabel(v) {
  const t = state.configMeta.types.find((x) => x.value === v);
  return t ? t.label : v;
}
function cfgScopeLabel(v) {
  const t = state.configMeta.scopes.find((x) => x.value === v);
  return t ? t.label : v;
}
function actionLabel(a) {
  const t = state.configMeta.actions.find((x) => x.value === a);
  return t ? t.label : a;
}

async function renderConfigList() {
  const view = $("#view");
  const f = state.configFilters;
  const admin = isAdmin();
  const myEnvs = admin
    ? state.meta.environments
    : state.meta.environments.filter((e) =>
        (state.myPerms.access || []).some((a) => a.environment === e.value));
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h2>配置档案</h2>
        <div class="sub">${admin ? "全部业务线 · 全部环境" : esc(state.myPerms.scope_text)} · 密文默认脱敏，查看明文与编辑权限分开授予</div>
      </div>
    </div>
    <div class="panel filter-bar">
      <select id="cf-bl" ${admin ? "" : "disabled"}>
        <option value="">全部业务线</option>
        ${state.businessLines.map((b) => `<option value="${b.id}" ${String(b.id) === String(f.business_line_id) ? "selected" : ""}>${esc(b.name)}</option>`).join("")}
      </select>
      <select id="cf-env">
        <option value="">全部环境</option>
        ${myEnvs.map((e) => `<option value="${e.value}" ${f.environment === e.value ? "selected" : ""}>${esc(e.label)}</option>`).join("")}
      </select>
      <button class="btn" id="cf-reset">重置</button>
    </div>
    <div id="cfg-result"><div class="empty-tip">加载中…</div></div>`;

  const load = async () => {
    const p = new URLSearchParams();
    if (admin && $("#cf-bl").value) p.set("business_line_id", $("#cf-bl").value);
    if ($("#cf-env").value) p.set("environment", $("#cf-env").value);
    try {
      const rows = await api(`/api/config/profiles?${p.toString()}`);
      const box = $("#cfg-result");
      if (!rows.length) {
        box.innerHTML = `<div class="panel panel-pad empty-tip">还没有配置档案，进入应用详情对应的环境后可新增配置</div>`;
        return;
      }
      box.innerHTML = `
        <div class="panel table-wrap">
          <table class="app-table">
            <thead><tr><th>应用</th><th>业务线</th><th>环境</th><th>配置项</th><th>密文</th><th>最新版本</th><th>最近更新</th></tr></thead>
            <tbody>
              ${rows.map((r) => `
                <tr data-app="${r.app_id}" data-env="${r.environment}">
                  <td class="app-name-cell">${esc(r.app_name)}
                    ${r.app_status === "offline" ? '<span class="badge offline">已下线</span>' : ""}</td>
                  <td>${esc(r.business_line_name)}</td>
                  <td><span class="env-tag ${esc(r.environment)}">${esc(state.meta.environments.find((e) => e.value === r.environment).label)}</span></td>
                  <td>${r.item_count} 项</td>
                  <td>${r.secret_count ? `<span class="secret-chip">🔒 ${r.secret_count}</span>` : '<span style="color:var(--ink-3)">—</span>'}</td>
                  <td><b>v${r.latest_version}</b>${r.latest_note ? `<div class="app-desc">${esc(r.latest_note)}</div>` : ""}</td>
                  <td title="${fmtTime(r.latest_at)}">${esc(r.created_by_name || "系统")} · ${fmtAgo(r.latest_at)}</td>
                </tr>`).join("")}
            </tbody>
          </table>
        </div>`;
      $$("[data-app]", box).forEach((tr) => {
        tr.onclick = () => { location.hash = `#/config/${tr.dataset.app}/${tr.dataset.env}`; };
      });
    } catch (e) {
      $("#cfg-result").innerHTML = errorStateHtml("加载失败", e.message);
    }
  };
  $("#cf-bl").onchange = load;
  $("#cf-env").onchange = load;
  $("#cf-reset").onclick = () => {
    state.configFilters = { business_line_id: "", environment: "" };
    renderConfigList();
  };
  await load();
}

/* ---------------- 配置档案 · 详情（单应用单环境） ---------------- */
async function renderConfigProfile(appId, environment) {
  const view = $("#view");
  if (!environment) {
    // 从菜单直接进 #/config/12：先取应用，默认落 prod（或应用所属环境）
    try {
      const a = await api(`/api/apps/${appId}`);
      location.hash = `#/config/${appId}/${a.environment}`;
    } catch (e) {
      view.innerHTML = errorStateHtml(e.status === 403 ? "403 无权访问" : "加载失败", e.message);
    }
    return;
  }
  await paintConfigProfile(appId, environment);
}

async function paintConfigProfile(appId, environment) {
  const view = $("#view");
  view.innerHTML = `<div class="empty-tip">加载中…</div>`;
  let data;
  try {
    data = await api(`/api/apps/${appId}/config?environment=${encodeURIComponent(environment)}`);
  } catch (e) {
    view.innerHTML = errorStateHtml(e.status === 403 ? "403 无权访问" : e.status === 404 ? "404 不存在" : "加载失败", e.message);
    return;
  }
  const envTabs = state.meta.environments;
  const perms = data.permissions || {};
  const canEdit = !!perms.can_edit_config;
  const canReveal = !!perms.can_view_secret;
  const canManageApp = !!perms.can_manage_app;
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h2>${esc(data.app_name)} · 配置档案</h2>
        <div class="sub">密文默认脱敏展示；查看明文需填写理由，服务端二次校验并留痕</div>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn" id="cp-back">← 档案列表</button>
        <button class="btn" id="cp-app">查看应用</button>
      </div>
    </div>
    ${!canEdit ? `<div class="panel perm-banner">
      🔒 当前账号在该应用此环境没有<b>配置编辑权</b>，配置只读${canReveal ? "；你持有密文查看权，可申请查看明文" : "，也没有密文查看权"}。
      能看明文 ≠ 能改，权限分开授予；如需编辑请联系平台管理员或业务线负责人。
    </div>` : ""}
    <div class="env-tabs" id="cp-tabs">
      ${envTabs.map((e) => {
        const allowed = envVisible(data.business_line_id, e.value);
        const cls = `env-tab ${e.value === environment ? "active" : ""} ${e.value === "prod" ? "prod" : ""} ${allowed ? "" : "locked"}`;
        return allowed
          ? `<a class="${cls}" href="#/config/${appId}/${e.value}">${esc(e.label)}</a>`
          : `<span class="${cls}" title="该环境不在你的可见范围内">${esc(e.label)} 🔒</span>`;
      }).join("")}
    </div>
    <div class="detail-grid cfg-grid">
      <div>
        <div class="panel panel-pad" style="margin-bottom:16px">
          <div class="cfg-toolbar">
            <h3 style="margin:0">配置项（${data.items.length}）</h3>
            <div style="display:flex;gap:8px">
              <button class="btn small" id="cp-diff-btn">环境对比</button>
              ${data.read_only ? "" : canEdit
                ? '<button class="btn primary small" id="cp-edit-btn">编辑配置</button>'
                : '<button class="btn small" disabled title="缺少配置编辑权：当前为只读">编辑配置（无权限）</button>'}
            </div>
          </div>
          ${data.read_only ? '<p style="color:var(--ink-3);font-size:13px;margin:8px 0 0">应用已下线（终态），配置档案只读。</p>' : ""}
          <div id="cp-items" style="margin-top:12px"></div>
        </div>
        <div class="panel panel-pad" id="cp-diff-panel" hidden></div>
      </div>
      <div class="panel panel-pad">
        <h3>版本历史</h3>
        <div id="cp-versions" class="version-list"></div>
      </div>
    </div>`;

  $("#cp-back").onclick = () => { location.hash = "#/config"; };
  $("#cp-app").onclick = () => { location.hash = `#/apps/${appId}`; };
  const diffBtn = $("#cp-diff-btn");
  const visibleEnvCount = isAdmin()
    ? state.meta.environments.length
    : state.meta.environments.filter((e) => envVisible(data.business_line_id, e.value)).length;
  if (visibleEnvCount < 2) {
    diffBtn.disabled = true;
    diffBtn.textContent = "环境对比（仅可见 1 个环境）";
    diffBtn.title = "你的可见范围内没有第二个环境，无法对比";
  } else {
    diffBtn.onclick = () => openEnvDiffModal(appId, environment, data.business_line_id);
  }
  const editBtn = $("#cp-edit-btn");
  if (editBtn) editBtn.onclick = () => openConfigEditor(appId, environment, data);

  paintConfigItems(data.items, { canReveal });
  paintVersions(appId, environment, data.versions, data.read_only || !canEdit);
}

function paintConfigItems(items, opts = {}) {
  const box = $("#cp-items");
  const canReveal = opts.canReveal !== false;
  if (!items.length) {
    box.innerHTML = `<div class="empty-tip">该环境暂无配置项${canReveal ? "，点「编辑配置」新增" : ""}</div>`;
    return;
  }
  const appId = appIdFromHash();
  box.innerHTML = `
    <table class="env-table cfg-table">
      <thead><tr><th>键</th><th>值</th><th>类型</th><th>范围</th><th style="width:96px"></th></tr></thead>
      <tbody>
        ${items.map((it) => `
          <tr>
            <td class="mono k">${esc(it.key)}</td>
            <td class="mono v">${it.is_secret
              ? `<span class="secret-masked">${esc(it.value)}</span>`
              : esc(it.value)}</td>
            <td><span class="meta-tag">${esc(cfgTypeLabel(it.value_type))}</span></td>
            <td><span class="meta-tag">${esc(cfgScopeLabel(it.scope))}</span></td>
            <td>${it.is_secret ? (canReveal
                ? `<button class="btn small" data-reveal="${it.id}" data-key="${esc(it.key)}">看明文</button>`
                : `<button class="btn small" disabled title="缺少密文查看权：能看到脱敏值不代表能看明文">看明文🔒</button>`)
              : ""}</td>
          </tr>`).join("")}
      </tbody>
    </table>`;
  $$("[data-reveal]", box).forEach((btn) => {
    btn.onclick = () => openRevealModal(appId, btn.dataset.reveal, btn.dataset.key);
  });
}

function appIdFromHash() {
  return (location.hash.split("/")[2] || "").split("?")[0];
}

async function paintVersions(appId, environment, versions, readOnly) {
  const box = $("#cp-versions");
  if (!versions.length) {
    box.innerHTML = `<div class="empty-tip">还没有版本，首次保存后生成 v1</div>`;
    return;
  }
  box.innerHTML = versions.map((v) => `
    <div class="version-item ${v.is_current ? "current" : ""}">
      <div class="ver-head">
        <b>v${v.version}</b>${v.is_current ? '<span class="ver-cur">当前版本</span>' : ""}
        <span class="ver-meta">${v.item_count} 项 · ${esc(v.created_by_name)} · ${fmtAgo(v.created_at)}</span>
      </div>
      ${v.change_note ? `<div class="ver-note">${esc(v.change_note)}</div>` : ""}
      <div class="ver-actions">
        <button class="btn small" data-view="${v.version}">查看快照</button>
        ${readOnly || v.is_current ? "" : `<button class="btn small danger" data-rb="${v.version}">回滚到此版</button>`}
      </div>
    </div>`).join("");
  $$("[data-view]", box).forEach((b) => {
    b.onclick = () => openVersionSnapshot(appId, environment, +b.dataset.view);
  });
  $$("[data-rb]", box).forEach((b) => {
    b.onclick = () => openRollbackModal(appId, environment, +b.dataset.rb);
  });
}

/* ---------------- 密文查看（服务端强制理由） ---------------- */
function openRevealModal(appId, itemId, key) {
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:480px">
        <h3>查看密文明文 · <span class="mono">${esc(key)}</span></h3>
        <p style="color:var(--ink-2);font-size:13px">
          本次查看将由<b>服务端强制记录</b>：操作人、时间、键名与查看理由。请如实填写业务理由（不少于 5 个字）。
        </p>
        <div class="form-grid" style="grid-template-columns:1fr">
          <div>
            <label>查看理由 *</label>
            <textarea id="rv-reason" rows="3" placeholder="如：线上工单 INC-xxx 排查数据库连接问题"></textarea>
          </div>
        </div>
        <div class="form-error" id="rv-error"></div>
        <div class="form-actions">
          <button class="btn" id="rv-cancel">取消</button>
          <button class="btn primary" id="rv-submit">确认查看并留痕</button>
        </div>
      </div>
    </div>`;
  const close = () => { root.innerHTML = ""; };
  $("#rv-cancel").onclick = close;
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) close(); };
  $("#rv-submit").onclick = async () => {
    const reason = $("#rv-reason").value.trim();
    const errBox = $("#rv-error");
    errBox.classList.remove("show");
    try {
      const res = await api(`/api/apps/${appId}/config/reveal`, {
        method: "POST", body: { item_id: +itemId, reason },
      });
      close();
      showRevealedValue(key, res.value);
    } catch (e) {
      errBox.textContent = e.message;
      errBox.classList.add("show");
    }
  };
}

function showRevealedValue(key, value) {
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:520px">
        <h3>明文（仅本次展示）· <span class="mono">${esc(key)}</span></h3>
        <div class="secret-plain mono">${esc(value)}</div>
        <p style="color:var(--red);font-size:12px">本次查看已记录到变更留痕，请勿截屏外发。</p>
        <div class="form-actions">
          <button class="btn primary" id="rv-done">我知道了</button>
        </div>
      </div>
    </div>`;
  $("#rv-done").onclick = () => { root.innerHTML = ""; };
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) root.innerHTML = ""; };
}

/* ---------------- 配置编辑 ---------------- */
function openConfigEditor(appId, environment, data) {
  const root = $("#modal-root");
  const rows = data.items.map((it) => ({
    key: it.key,
    value: it.is_secret ? "" : it.value,   // 密文不回填明文
    value_type: it.value_type,
    scope: it.scope,
    is_secret: it.is_secret,
    keep_value: it.is_secret,              // 留空 = 不改密文
    existed: true,
  }));
  if (!rows.length) rows.push(blankRow());

  function blankRow() {
    return { key: "", value: "", value_type: "string", scope: "global", is_secret: false, keep_value: false, existed: false };
  }

  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:880px">
        <h3>编辑配置 · ${esc(data.app_name)} · ${esc(state.meta.environments.find((e) => e.value === environment).label)}环境</h3>
        <p style="color:var(--ink-3);font-size:12px;margin:0 0 10px">整体保存后生成一个新版本；密文值留空表示「不修改原密文」。</p>
        <table class="env-table cfg-edit-table">
          <thead><tr>
            <th style="width:24%">键</th><th style="width:30%">值</th><th style="width:13%">类型</th>
            <th style="width:13%">范围</th><th style="width:9%">密文</th><th style="width:48px"></th>
          </tr></thead>
          <tbody id="ce-tbody"></tbody>
        </table>
        <button class="btn small" id="ce-add" style="margin-top:8px">+ 添加一行</button>
        <div class="form-grid" style="grid-template-columns:1fr;margin-top:12px">
          <div>
            <label>变更备注（可选）</label>
            <input id="ce-note" maxlength="200" placeholder="如：缩短支付超时 / 轮换数据库口令">
          </div>
        </div>
        <div class="form-error" id="ce-error"></div>
        <div class="form-actions">
          <button class="btn" id="ce-cancel">取消</button>
          <button class="btn primary" id="ce-submit">保存为新版本</button>
        </div>
      </div>
    </div>`;
  const close = () => { root.innerHTML = ""; };
  $("#ce-cancel").onclick = close;
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) close(); };
  $("#ce-add").onclick = () => { rows.push(blankRow()); paint(); };

  const tbody = $("#ce-tbody");
  function paint() {
    tbody.innerHTML = rows.map((r, i) => `
      <tr>
        <td><input data-i="${i}" data-f="key" placeholder="如 DB_HOST" value="${esc(r.key)}"></td>
        <td>
          <input data-i="${i}" data-f="value" autocomplete="new-password"
            placeholder="${r.is_secret ? "密文已设置，留空不改" : "值"}" value="${esc(r.value)}">
        </td>
        <td>
          <select data-i="${i}" data-f="value_type">
            ${state.configMeta.types.map((t) => `<option value="${t.value}" ${r.value_type === t.value ? "selected" : ""}>${esc(t.label)}</option>`).join("")}
          </select>
        </td>
        <td>
          <select data-i="${i}" data-f="scope">
            ${state.configMeta.scopes.map((t) => `<option value="${t.value}" ${r.scope === t.value ? "selected" : ""}>${esc(t.label)}</option>`).join("")}
          </select>
        </td>
        <td style="text-align:center"><input type="checkbox" data-i="${i}" data-f="is_secret" ${r.is_secret ? "checked" : ""}></td>
        <td><button class="btn small danger" data-del="${i}">删</button></td>
      </tr>`).join("");
    $$("input,select", tbody).forEach((el) => {
      el.oninput = el.onchange = () => {
        const i = +el.dataset.i;
        if (el.dataset.f === "is_secret") {
          rows[i].is_secret = el.checked;
          if (el.checked && rows[i].existed) rows[i].keep_value = true;
          if (!el.checked) rows[i].keep_value = false;
          paint();
          return;
        }
        if (el.dataset.f === "value") {
          // 一旦手动输入新值，就不再沿用旧密文
          rows[i].keep_value = false;
        }
        rows[i][el.dataset.f] = el.value;
      };
    });
    $$("[data-del]", tbody).forEach((b) => {
      b.onclick = () => { rows.splice(+b.dataset.del, 1); if (!rows.length) rows.push(blankRow()); paint(); };
    });
  }
  paint();

  const submit = async (force) => {
    const errBox = $("#ce-error");
    if (errBox) errBox.classList.remove("show");
    const items = rows.filter((r) => r.key.trim()).map((r) => ({
      key: r.key.trim(),
      value: r.value,
      value_type: r.value_type,
      scope: r.scope,
      is_secret: r.is_secret,
      keep_value: r.is_secret && r.existed && r.value === "",
    }));
    if (!items.length) {
      errBox.textContent = "至少保留一个配置键"; errBox.classList.add("show"); return;
    }
    const emptyNewSecret = items.find((it) => it.is_secret && !it.keep_value && it.value === "");
    if (emptyNewSecret) {
      errBox.textContent = `新增密文项 ${emptyNewSecret.key} 的值不能为空（已有密文可留空表示不修改）`;
      errBox.classList.add("show");
      return;
    }
    const submitBtn = $("#ce-submit");
    if (submitBtn) submitBtn.disabled = true;
    try {
      const res = await api(`/api/apps/${appId}/config?environment=${encodeURIComponent(environment)}`, {
        method: "PUT",
        body: {
          items, change_note: $("#ce-note").value.trim(),
          base_version: data.current_version || 0, force: !!force,
        },
      });
      close();
      toast(`已生成 v${res.version}，${res.changes} 个键发生变化${force ? "（冲突后显式覆盖，已留痕）" : ""}`, "success");
      paintConfigProfile(appId, environment);
    } catch (e) {
      if (submitBtn) submitBtn.disabled = false;
      if (e.status === 409 && e.data && e.data.detail && e.data.detail.code === "config_version_conflict") {
        // 不许静默盖掉：先展示对方改了什么，由人做取舍
        openConflictModal(e.data.detail, () => submit(true));
        return;
      }
      if (errBox) {
        errBox.textContent = e.message;
        errBox.classList.add("show");
      } else {
        toast(e.message, "error");
      }
    }
  };
  $("#ce-submit").onclick = () => submit(false);
}

/* ---------------- 并发冲突取舍（两个浏览器同时改同一条配置） ---------------- */
function openConflictModal(cf, onForce) {
  const envLabel = (v) => state.meta.environments.find((e) => e.value === v)?.label || v;
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:860px">
        <h3>⚠️ 配置已被他人更新（v${cf.base_version} → v${cf.current_version}）</h3>
        <p style="color:var(--red);font-size:13px;margin:4px 0 10px">${esc(cf.message)}</p>
        <div id="cf-versions" style="margin-bottom:10px">
          ${(cf.intervening_versions || []).map((v) => `
            <div class="cf-ver">
              <b>v${v.version}</b> · ${esc(v.created_by_name)} · ${fmtAgo(v.created_at)}
              ${v.change_note ? ` · ${esc(v.change_note)}` : ""}
            </div>`).join("")}
        </div>
        <table class="env-table diff-table">
          <thead><tr><th>键</th><th>你打开时 v${cf.base_version}</th><th></th><th>对方保存后 v${cf.current_version}</th></tr></thead>
          <tbody>
            ${(cf.entries || []).map((e) => `
              <tr class="diff-${e.status}">
                <td class="mono">${esc(e.key)}</td>
                <td class="mono">${e.a ? sideValue(e.a) : '<span class="diff-gone">（无）</span>'}</td>
                <td>${diffArrow(e.status)}</td>
                <td class="mono">${e.b ? sideValue(e.b) : '<span class="diff-gone">（无）</span>'}</td>
              </tr>`).join("")}
          </tbody>
        </table>
        <p style="color:var(--ink-3);font-size:12px;margin:10px 0 0">
          「刷新后重新编辑」会放弃你当前表单、按对方最新版本重做；「显式覆盖」会用你的内容生成新版本，
          对方改动被覆盖一事将逐键写入留痕。
        </p>
        <div class="form-actions">
          <button class="btn primary" id="cf-refresh">放弃我的改动，刷新后重新编辑</button>
          <button class="btn danger" id="cf-force">我已确认，显式覆盖（留痕）</button>
        </div>
      </div>
    </div>`;
  $("#cf-refresh").onclick = () => {
    root.innerHTML = "";
    const parts = location.hash.split("/");
    paintConfigProfile(parts[2], parts[3] || "");
  };
  $("#cf-force").onclick = () => {
    root.innerHTML = "";
    if (!confirm("确认用你的内容覆盖对方刚保存的版本？该取舍会写入配置留痕。")) return;
    onForce();
  };
}

/* ---------------- 版本快照 ---------------- */
async function openVersionSnapshot(appId, environment, version) {
  let v;
  try {
    v = await api(`/api/apps/${appId}/config/versions/${version}?environment=${encodeURIComponent(environment)}`);
  } catch (e) { toast(e.message, "error"); return; }
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:720px">
        <h3>版本快照 v${v.version} ${v.is_current ? '<span class="ver-cur">当前版本</span>' : ""}</h3>
        ${v.change_note ? `<p style="color:var(--ink-2);font-size:13px;margin:0 0 8px">${esc(v.change_note)}</p>` : ""}
        <table class="env-table cfg-table">
          <thead><tr><th>键</th><th>值</th><th>类型</th><th>范围</th></tr></thead>
          <tbody>
            ${v.items.map((it) => `
              <tr>
                <td class="mono">${esc(it.key)}</td>
                <td class="mono">${it.is_secret ? `<span class="secret-masked">${esc(it.value)}</span>` : esc(it.value)}</td>
                <td>${esc(cfgTypeLabel(it.value_type))}</td>
                <td>${esc(cfgScopeLabel(it.scope))}</td>
              </tr>`).join("")}
          </tbody>
        </table>
        <div class="form-actions"><button class="btn primary" id="vs-done">关闭</button></div>
      </div>
    </div>`;
  $("#vs-done").onclick = () => { root.innerHTML = ""; };
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) root.innerHTML = ""; };
}

/* ---------------- 回滚（追加新版本，不覆盖留痕） ---------------- */
async function openRollbackModal(appId, environment, targetVersion) {
  let preview;
  try {
    preview = await api(`/api/apps/${appId}/config/rollback-preview?environment=${encodeURIComponent(environment)}&version=${targetVersion}`);
  } catch (e) { toast(e.message, "error"); return; }
  const root = $("#modal-root");
  const diffRows = preview.entries.filter((e) => e.status !== "same");
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:820px">
        <h3>回滚：v${preview.from_version} → v${targetVersion}</h3>
        <p style="color:var(--ink-2);font-size:13px;margin:0 0 8px">
          回滚会<b>追加一个新版本</b>（内容同 v${targetVersion}），v${preview.from_version} 及其全部留痕<b>原样保留、不会被覆盖</b>。
        </p>
        ${diffRows.length ? `
        <table class="env-table diff-table">
          <thead><tr><th>键</th><th>当前 v${preview.from_version}</th><th></th><th>回滚后 v${targetVersion}</th></tr></thead>
          <tbody>
            ${diffRows.map((e) => `
              <tr class="diff-${e.status}">
                <td class="mono">${esc(e.key)}</td>
                <td class="mono">${e.a ? sideValue(e.a) : '<span class="diff-gone">（不存在）</span>'}</td>
                <td>${diffArrow(e.status)}</td>
                <td class="mono">${e.b ? sideValue(e.b) : '<span class="diff-gone">（将删除）</span>'}</td>
              </tr>`).join("")}
          </tbody>
        </table>` : `<p>两个版本内容一致，无需回滚。</p>`}
        <div class="form-error" id="rb-error"></div>
        <div class="form-actions">
          <button class="btn" id="rb-cancel">取消</button>
          <button class="btn danger" id="rb-submit" ${diffRows.length ? "" : "disabled"}>确认回滚（生成新版本）</button>
        </div>
      </div>
    </div>`;
  const close = () => { root.innerHTML = ""; };
  $("#rb-cancel").onclick = close;
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) close(); };
  const submitRollback = async (force) => {
    try {
      const res = await api(`/api/apps/${appId}/config/rollback`, {
        method: "POST",
        body: { environment, version: targetVersion,
                base_version: preview.from_version, force: !!force },
      });
      close();
      toast(`已回滚：生成 v${res.version}，${res.changes} 个键变化；历史留痕已保留`, "success");
      paintConfigProfile(appId, environment);
    } catch (e) {
      if (e.status === 409 && e.data && e.data.detail && e.data.detail.code === "config_version_conflict") {
        openConflictModal(e.data.detail, () => submitRollback(true));
        return;
      }
      const box = $("#rb-error");
      if (box) { box.textContent = e.message; box.classList.add("show"); }
      else toast(e.message, "error");
    }
  };
  $("#rb-submit").onclick = () => submitRollback(false);
}

function sideValue(side) {
  return side.is_secret ? `<span class="secret-masked">${esc(side.value)}</span>` : esc(side.value);
}
function diffArrow(status) {
  return { only_a: "→ 删除", only_b: "← 恢复", changed: "→" }[status] || "";
}

/* ---------------- 环境对比 ---------------- */
function openEnvDiffModal(appId, curEnv, blId) {
  const root = $("#modal-root");
  // 环境可见集合按授权范围过滤（blId 显式传入，避免跨应用串味）
  const visibleEnvs = isAdmin()
    ? state.meta.environments
    : state.meta.environments.filter((e) => envVisible(blId, e.value));
  const otherEnvs = visibleEnvs.filter((e) => e.value !== curEnv);
  if (otherEnvs.length === 0) {
    toast("你的可见范围内没有第二个环境可对比", "error");
    return;
  }
  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:860px">
        <h3>环境配置差异对比</h3>
        <div class="filter-bar" style="padding:0 0 12px;box-shadow:none;border:0">
          <span style="font-size:13px;color:var(--ink-2)">基准环境</span>
          <select id="df-a">
            ${state.meta.environments.map((e) => `<option value="${e.value}" ${e.value === curEnv ? "selected" : ""}>${esc(e.label)}</option>`).join("")}
          </select>
          <span style="font-size:13px;color:var(--ink-2)">对比环境</span>
          <select id="df-b">
            ${otherEnvs.map((e) => `<option value="${e.value}" ${e.value === "prod" && curEnv !== "prod" ? "selected" : ""}>${esc(e.label)}</option>`).join("")}
          </select>
          <button class="btn primary small" id="df-run">对比</button>
        </div>
        <div id="df-result"><div class="empty-tip">选择两个环境后点「对比」</div></div>
        <div class="form-actions"><button class="btn" id="df-done">关闭</button></div>
      </div>
    </div>`;
  const close = () => { root.innerHTML = ""; };
  $("#df-done").onclick = close;
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) close(); };
  const run = async () => {
    const a = $("#df-a").value, b = $("#df-b").value;
    if (a === b) { $("#df-result").innerHTML = `<div class="empty-tip">请选择两个不同的环境</div>`; return; }
    $("#df-result").innerHTML = `<div class="empty-tip">对比中…</div>`;
    try {
      const d = await api(`/api/apps/${appId}/config/diff?env_a=${a}&env_b=${b}`);
      paintDiffResult(d);
    } catch (e) { $("#df-result").innerHTML = `<div class="empty-tip">${esc(e.message)}</div>`; }
  };
  $("#df-run").onclick = run;
  run();
}

function paintDiffResult(d) {
  const envLabel = (v) => state.meta.environments.find((e) => e.value === v).label;
  const s = d.summary;
  const interesting = d.entries.filter((e) => e.status !== "same");
  const box = $("#df-result");
  box.innerHTML = `
    <div class="diff-summary">
      <span class="ds changed">值不同 ${s.changed}</span>
      <span class="ds onlya">仅${envLabel(d.env_a)}有 ${s.only_a}</span>
      <span class="ds onlyb">仅${envLabel(d.env_b)}有 ${s.only_b}</span>
      <span class="ds same">一致 ${s.same}</span>
    </div>
    ${interesting.length ? `
    <table class="env-table diff-table">
      <thead><tr><th>键</th><th>${esc(envLabel(d.env_a))}</th><th></th><th>${esc(envLabel(d.env_b))}</th></tr></thead>
      <tbody>
        ${interesting.map((e) => `
          <tr class="diff-${e.status}">
            <td class="mono">${esc(e.key)}</td>
            <td class="mono">${e.a ? sideValue(e.a) : '<span class="diff-gone">（无）</span>'}</td>
            <td>${diffArrow(e.status)}</td>
            <td class="mono">${e.b ? sideValue(e.b) : '<span class="diff-gone">（无）</span>'}</td>
          </tr>`).join("")}
      </tbody>
    </table>` : `<div class="empty-tip">两个环境配置完全一致 🎉</div>`}`;
}

/* ---------------- 变更留痕 ---------------- */
async function renderAudit() {
  const view = $("#view");
  const f = state.auditFilters;
  const admin = isAdmin();
  const myEnvs = admin
    ? state.meta.environments
    : state.meta.environments.filter((e) =>
        (state.myPerms.access || []).some((a) => a.environment === e.value));
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h2>变更留痕</h2>
        <div class="sub">${admin ? "全部业务线 · 全部环境" : esc(state.myPerms.scope_text)} · 配置每次改动都有逐键流水，密文同样脱敏</div>
      </div>
      <button class="btn primary" id="au-export">导出差异清单 CSV</button>
    </div>
    <div class="panel filter-bar">
      <select id="au-bl" ${admin ? "" : "disabled"}>
        <option value="">全部业务线</option>
        ${state.businessLines.map((b) => `<option value="${b.id}" ${String(b.id) === String(f.business_line_id) ? "selected" : ""}>${esc(b.name)}</option>`).join("")}
      </select>
      <input id="au-app" placeholder="应用 ID（可留空）" value="${esc(f.app_id)}" style="width:130px">
      <select id="au-env">
        <option value="">全部环境</option>
        ${myEnvs.map((e) => `<option value="${e.value}" ${f.environment === e.value ? "selected" : ""}>${esc(e.label)}</option>`).join("")}
      </select>
      <select id="au-action">
        <option value="">全部动作</option>
        ${state.configMeta.actions.map((a) => `<option value="${a.value}" ${f.action === a.value ? "selected" : ""}>${esc(a.label)}</option>`).join("")}
      </select>
      <input type="date" id="au-start" value="${esc(f.start)}" title="开始日期">
      <span style="color:var(--ink-3)">至</span>
      <input type="date" id="au-end" value="${esc(f.end)}" title="结束日期">
      <button class="btn primary" id="au-run">查询</button>
      <button class="btn" id="au-reset">重置</button>
    </div>
    <div id="au-result"><div class="empty-tip">加载中…</div></div>`;

  const buildParams = () => {
    const p = new URLSearchParams();
    if (admin && $("#au-bl").value) p.set("business_line_id", $("#au-bl").value);
    if ($("#au-app").value.trim()) p.set("app_id", $("#au-app").value.trim());
    if ($("#au-env").value) p.set("environment", $("#au-env").value);
    if ($("#au-action").value) p.set("action", $("#au-action").value);
    if ($("#au-start").value) p.set("start", $("#au-start").value);
    if ($("#au-end").value) p.set("end", $("#au-end").value);
    return p;
  };
  const load = async () => {
    try {
      const rows = await api(`/api/config/audit?${buildParams().toString()}`);
      paintAuditRows(rows);
    } catch (e) {
      $("#au-result").innerHTML = errorStateHtml("查询失败", e.message);
    }
  };
  $("#au-run").onclick = () => {
    state.auditFilters = {
      app_id: $("#au-app").value.trim(), business_line_id: $("#au-bl").value,
      environment: $("#au-env").value, action: $("#au-action").value,
      start: $("#au-start").value, end: $("#au-end").value,
    };
    load();
  };
  $("#au-reset").onclick = () => {
    state.auditFilters = { app_id: "", business_line_id: "", environment: "", action: "", start: "", end: "" };
    renderAudit();
  };
  $("#au-export").onclick = () => {
    // 带当前筛选条件与令牌下载（令牌在 header 里，用 fetch 转 blob）
    fetch(`/api/config/audit/export.csv?${buildParams().toString()}`, {
      headers: { "X-Token": state.token },
    }).then(async (res) => {
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || `导出失败（HTTP ${res.status}）`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `config-changelog-${Date.now()}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    }).catch((e) => toast(e.message, "error"));
  };
  await load();
}

function paintAuditRows(rows) {
  const box = $("#au-result");
  if (!rows.length) {
    box.innerHTML = `<div class="panel panel-pad empty-tip">该时间窗内没有配置变更流水</div>`;
    return;
  }
  box.innerHTML = `
    <div class="panel table-wrap">
      <table class="app-table audit-table">
        <thead><tr>
          <th>时间</th><th>应用</th><th>环境</th><th>动作</th><th>配置键</th>
          <th>改前</th><th>改后</th><th>操作人</th><th>理由/备注</th>
        </tr></thead>
        <tbody>
          ${rows.map((r) => `
            <tr>
              <td class="nowrap">${fmtTime(r.created_at)}</td>
              <td class="app-name-cell">${esc(r.app_name)}<div class="app-desc">${esc(r.business_line_name)}</div></td>
              <td><span class="env-tag ${esc(r.environment)}">${esc(r.environment_label)}</span></td>
              <td><span class="action-tag ${esc(r.action)}">${esc(r.action_label)}</span>${r.is_secret ? ' <span title="涉及密文">🔒</span>' : ""}</td>
              <td class="mono">${esc(r.config_key || "—")}</td>
              <td class="mono audit-val">${r.old_value === null ? '<span class="diff-gone">—</span>' : esc(r.old_value)}</td>
              <td class="mono audit-val">${r.new_value === null ? '<span class="diff-gone">—</span>' : esc(r.new_value)}</td>
              <td class="nowrap">${esc(r.user_name)}</td>
              <td class="reason-cell">${esc(r.reason || "")}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </div>
    <p style="color:var(--ink-3);font-size:12px;margin:8px 2px">最多返回最近 500 条；如需完整流水请用「导出差异清单 CSV」。</p>`;
}

/* ---------------- 权限与交接（平台管理员 / 业务线负责人） ---------------- */
const PERM_META = [
  { key: "can_view_secret", label: "密文查看" },
  { key: "can_edit_config", label: "配置编辑" },
  { key: "can_manage_app", label: "应用管理" },
];

function canManagePermissions() {
  return isAdmin() || state.user.role === "bl_owner";
}

async function renderPermsPage() {
  const view = $("#view");
  if (!canManagePermissions()) {
    view.innerHTML = errorStateHtml("403 无权访问", "仅平台管理员或业务线负责人可管理人员权限");
    return;
  }
  view.innerHTML = `<div class="empty-tip">加载中…</div>`;
  let users, logs;
  try {
    [users, logs] = await Promise.all([api("/api/users"), api("/api/permission-logs")]);
  } catch (e) {
    view.innerHTML = errorStateHtml("加载失败", e.message);
    return;
  }
  view.innerHTML = `
    <div class="page-head">
      <div>
        <h2>权限与交接</h2>
        <div class="sub">角色四类；可见范围按 业务线 × 环境 收窄；密文查看 / 配置编辑 / 应用管理分开授予，变更全部留痕</div>
      </div>
    </div>
    <div class="detail-grid">
      <div class="panel panel-pad">
        <h3>人员与授权（${users.length}）</h3>
        <div class="perm-user-list">
          ${users.map((u) => `
            <div class="perm-user" data-uid="${u.id}">
              <div class="pu-head">
                <b>${esc(u.name)}</b>
                <span class="u-role ${u.role === "admin" ? "admin" : ""}">${esc(u.role_label || u.role)}</span>
                ${u.active === false ? '<span class="red-dot">已停用</span>' : ""}
              </div>
              <div class="pu-meta">${esc(u.username)} · ${esc(u.business_line_name || "平台")}</div>
            </div>`).join("")}
        </div>
      </div>
      <div class="panel panel-pad">
        <h3>权限变更流水</h3>
        <div id="perm-logs" class="log-list">
          ${logs.length ? logs.map((l) => `
            <div class="log-item">
              <div><span class="action-tag">${esc(l.action_label)}</span>
                <b>${esc(l.target_name)}</b>${l.business_line_name ? ` · ${esc(l.business_line_name)}${l.environment_label ? " · " + esc(l.environment_label) : ""}` : ""}</div>
              <div class="log-detail">${esc(l.detail)}</div>
              <div class="log-meta">${esc(l.actor_name)} · ${fmtTime(l.created_at)} · 理由：${esc(l.reason)}</div>
            </div>`).join("") : '<div class="empty-tip">暂无权限变更记录</div>'}
        </div>
      </div>
    </div>`;
  $$("[data-uid]", view).forEach((el) => {
    el.onclick = () => openPermEditor(+el.dataset.uid);
  });
}

async function openPermEditor(userId) {
  let p;
  try {
    p = await api(`/api/users/${userId}/permissions`);
  } catch (e) { toast(e.message, "error"); return; }
  const root = $("#modal-root");
  const isMeAdmin = isAdmin();
  const rows = p.access.map((a) => ({
    business_line_id: a.business_line_id, environment: a.environment,
    can_view_secret: a.can_view_secret, can_edit_config: a.can_edit_config,
    can_manage_app: a.can_manage_app,
  }));

  const blChoices = () => isMeAdmin
    ? state.businessLines
    : state.businessLines.filter((b) =>
        (state.myPerms.access || []).some((a) => a.business_line_id === b.id && a.can_manage_app));

  root.innerHTML = `
    <div class="modal-mask">
      <div class="modal" style="width:860px">
        <h3>权限设置 · ${esc(p.name)} <span class="u-role ${p.role === "admin" ? "admin" : ""}">${esc(p.role_label)}</span>
          ${p.active ? "" : '<span class="red-dot">账号已停用</span>'}</h3>
        <p style="color:var(--ink-3);font-size:12px;margin:2px 0 10px">${esc(p.username)} · ${esc(p.business_line_name || "平台")}</p>
        <div class="form-grid" style="grid-template-columns:1fr">
          <div>
            <label>角色</label>
            <select id="pe-role" ${isMeAdmin ? "" : "disabled"}>
              ${(state.configMeta.roles || []).map((r) =>
                `<option value="${r.value}" ${p.role === r.value ? "selected" : ""}>${esc(r.label)}</option>`).join("")}
            </select>
            ${isMeAdmin ? "" : '<p class="pe-note">业务线负责人只能调整本业务线内的环境授权，角色变更需平台管理员。</p>'}
          </div>
        </div>
        <h4 style="margin:12px 0 6px">可见范围与授权（业务线 × 环境）</h4>
        <table class="env-table perm-table" id="pe-table">
          <thead><tr><th>业务线</th><th>环境</th><th>密文查看</th><th>配置编辑</th><th>应用管理</th><th style="width:48px"></th></tr></thead>
          <tbody id="pe-tbody"></tbody>
        </table>
        <button class="btn small" id="pe-add" style="margin-top:8px">+ 添加一行授权</button>
        <p class="pe-note" style="margin-top:8px">
          没有任何授权行 = 完全不可见；只读观察者不允许勾选「配置编辑 / 应用管理」；密文查看与配置编辑互不包含。
        </p>
        <div class="form-grid" style="grid-template-columns:1fr;margin-top:6px">
          <div>
            <label>变更理由 *（不少于 5 字）</label>
            <input id="pe-reason" maxlength="200" placeholder="如：最小权限整改，收回生产密文查看权，工单 SEC-2026-003">
          </div>
        </div>
        <div class="form-error" id="pe-error"></div>
        <div class="form-actions">
          ${isMeAdmin && p.active && p.role !== "admin" ? '<button class="btn danger" id="pe-deactivate">停用账号（离职）</button>' : ""}
          <span style="flex:1"></span>
          <button class="btn" id="pe-cancel">取消</button>
          <button class="btn primary" id="pe-submit">保存权限并留痕</button>
        </div>
      </div>
    </div>`;
  const close = () => { root.innerHTML = ""; };
  $("#pe-cancel").onclick = close;
  $(".modal-mask", root).onclick = (e) => { if (e.target.classList.contains("modal-mask")) close(); };

  const tbody = $("#pe-tbody");
  function paint() {
    tbody.innerHTML = rows.map((r, i) => `
      <tr>
        <td>
          <select data-i="${i}" data-f="business_line_id" ${isMeAdmin ? "" : "disabled"}>
            ${blChoices().map((b) => `<option value="${b.id}" ${String(b.id) === String(r.business_line_id) ? "selected" : ""}>${esc(b.name)}</option>`).join("")}
          </select>
        </td>
        <td>
          <select data-i="${i}" data-f="environment">
            ${state.meta.environments.map((e) => `<option value="${e.value}" ${r.environment === e.value ? "selected" : ""}>${esc(e.label)}</option>`).join("")}
          </select>
        </td>
        ${PERM_META.map((m) => `
          <td style="text-align:center">
            <input type="checkbox" data-i="${i}" data-f="${m.key}" ${r[m.key] ? "checked" : ""}>
          </td>`).join("")}
        <td><button class="btn small danger" data-del="${i}">删</button></td>
      </tr>`).join("");
    $$("select,input", tbody).forEach((el) => {
      el.onchange = () => {
        const i = +el.dataset.i;
        if (el.type === "checkbox") rows[i][el.dataset.f] = el.checked;
        else rows[i][el.dataset.f] = isNaN(+el.value) ? el.value : +el.value;
      };
    });
    $$("[data-del]", tbody).forEach((b) => {
      b.onclick = () => { rows.splice(+b.dataset.del, 1); paint(); };
    });
  }
  paint();
  $("#pe-add").onclick = () => {
    const bl0 = blChoices()[0];
    rows.push({ business_line_id: bl0 ? bl0.id : null, environment: "prod",
                can_view_secret: false, can_edit_config: false, can_manage_app: false });
    paint();
  };

  $("#pe-submit").onclick = async () => {
    const errBox = $("#pe-error");
    errBox.classList.remove("show");
    const reason = $("#pe-reason").value.trim();
    if (reason.length < 5) { errBox.textContent = "变更理由不少于 5 个字，权限变更必须带理由留痕"; errBox.classList.add("show"); return; }
    // 前端先按观察者约束拦一道（服务端也强制）
    const role = $("#pe-role").value;
    if (role === "observer" && rows.some((r) => r.can_edit_config || r.can_manage_app)) {
      errBox.textContent = "只读观察者不能持有配置编辑/应用管理权（密文查看可单独授予）";
      errBox.classList.add("show"); return;
    }
    const seen = new Set();
    for (const r of rows) {
      const k = `${r.business_line_id}@${r.environment}`;
      if (seen.has(k)) { errBox.textContent = "授权行重复：同一业务线 × 环境只能有一行"; errBox.classList.add("show"); return; }
      seen.add(k);
    }
    try {
      await api(`/api/users/${userId}/permissions`, {
        method: "PUT",
        body: {
          reason,
          role: isMeAdmin ? role : null,
          access: rows.map((r) => ({
            business_line_id: r.business_line_id, environment: r.environment,
            can_view_secret: !!r.can_view_secret, can_edit_config: !!r.can_edit_config,
            can_manage_app: !!r.can_manage_app,
          })),
        },
      });
      close();
      toast("权限已更新，变更内容写入权限流水", "success");
      renderPermsPage();
    } catch (e) {
      errBox.textContent = e.message;
      errBox.classList.add("show");
    }
  };

  const deactBtn = $("#pe-deactivate");
  if (deactBtn) {
    deactBtn.onclick = async () => {
      const reason = prompt("停用原因（不少于 5 字）。注意：名下仍有应用时必须先完成应用交接，否则会被拦下。");
      if (reason === null) return;
      if (reason.trim().length < 5) { toast("停用原因不少于 5 个字", "error"); return; }
      try {
        await api(`/api/users/${userId}/deactivate`, { method: "POST", body: { reason: reason.trim() } });
        close();
        toast("账号已停用，授权全部收回并留痕", "success");
        renderPermsPage();
      } catch (e) {
        // 名下仍有应用时服务端 409 列出未交接清单：原样展示，不允许糊里糊涂停用
        alert(e.message);
      }
    };
  }
}

/* ---------------- 错误态 ---------------- */
function errorStateHtml(title, message) {
  return `
    <div class="panel error-state">
      <div class="code">${esc(title)}</div>
      <div class="msg">${esc(message)}</div>
      <button class="btn" onclick="location.hash='#/apps'">返回应用台账</button>
      <button class="btn" onclick="location.hash='#/console'" style="margin-left:8px">回到控制台</button>
    </div>`;
}

/* ---------------- 入口 ---------------- */
(async function init() {
  if (state.token) {
    try {
      state.user = await api("/api/me");
      await bootstrap();
      return;
    } catch (e) { /* token 失效，走登录 */ }
  }
  showLogin();
})();
