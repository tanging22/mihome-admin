/* 米家设备管家 —— 前端逻辑（原生 JS，无构建） */
"use strict";

/* ---------------- state & api ---------------- */
const state = {
  status: null,      // /api/status
  sections: [],      // 栏目
  actions: [],       // 动作
  devices: [],       // 小米设备
  editing: false,
};
const TYPE_LABEL = {
  miot_prop_set: "属性",
  miot_action: "动作",
  prop_display: "信息",
  rpc: "RPC",
  miio_lan: "局域网",
};
const $ = (sel) => document.querySelector(sel);

// fnOS 统一网关场景下 API 必须带 /app/mihome-admin 前缀：
// 页面 URL 是 /app/mihome-admin/，相对 fetch("/api/..") 会被网关
// 路由到 fnOS 自身的 API（如 /api/auth/login 是 NAS 登录）。
// TCP 直连（pathname=/）时前缀为空，行为不变。
const _MIA_BASE = location.pathname.startsWith("/app/")
  ? location.pathname.replace(/\/+$/, "")
  : "";

async function api(method, path, body) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(_MIA_BASE + path, opt);
  let d = {};
  try { d = await r.json(); } catch (e) { d = { ok: false, error: "HTTP " + r.status }; }
  // TCP 直连遇到面板密码拦截 → 弹面板登录框（登录成功自动刷新）
  if (r.status === 401 && d.need_panel_login && !path.startsWith("/api/panel/")) {
    openPanelLoginModal();
  }
  return d;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- toast & modal ---------------- */
function toast(msg, type = "", ms = 2600) {
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = msg;
  $("#toast-wrap").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, ms - 300);
  setTimeout(() => el.remove(), ms);
}

function openModal(html) {
  $("#modal-box").innerHTML = html;
  $("#modal-mask").classList.remove("hidden");
}
function closeModal() { $("#modal-mask").classList.add("hidden"); }
$("#modal-mask").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) closeModal();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

/* ---------------- 数据加载 ---------------- */
async function loadAll(silent = false) {
  const [st, sec, act] = await Promise.all([
    api("GET", "/api/status"),
    api("GET", "/api/sections"),
    api("GET", "/api/actions"),
  ]);
  state.status = st.data || st;
  state.sections = sec.data ? sec.data.sections : [];
  state.actions = act.data ? act.data.actions : [];
  if (state.status.logged_in) {
    const dv = await api("GET", "/api/devices");
    state.devices = (dv.data && dv.data.devices) || [];
  } else { state.devices = []; }
  render();
  if (!silent) toast("已刷新", "good", 1200);
}

function renderStatusLine() {
  const s = state.status || {};
  $("#status-line").textContent = s.logged_in
    ? `${s.username} · ${s.country} 区 · 设备 ${state.devices.length} 台`
    : "未登录";
  $("#btn-login").classList.toggle("hidden", !!s.logged_in);
  $("#btn-logout").classList.toggle("hidden", !s.logged_in);
}

/* ---------------- 看板渲染 ---------------- */
function render() {
  renderStatusLine();
  const board = $("#board");
  board.classList.toggle("editing", state.editing);
  const hasContent = state.sections.length > 0 || state.actions.length > 0;
  $("#empty-hint").classList.toggle("hidden", hasContent || state.editing);
  board.innerHTML = "";
  for (const sec of state.sections) board.appendChild(columnEl(sec));
  if (state.editing) {
    const add = document.createElement("div");
    add.className = "add-column";
    add.title = "添加栏目";
    add.textContent = "＋";
    add.onclick = addSection;
    board.appendChild(add);
  }
}

function columnEl(sec) {
  const col = document.createElement("section");
  col.className = "column";
  col.dataset.section = sec.id;
  const acts = state.actions.filter((a) => a.section_id === sec.id);
  const tools = state.editing ? `
    <span class="col-tools">
      <button class="icon-btn" data-op="rename" title="重命名">✎</button>
      <button class="icon-btn" data-op="del" title="删除栏目">🗑</button>
    </span>` : "";
  col.innerHTML = `<div class="column-head"><h2 title="${esc(sec.name)}">${esc(sec.name)}</h2>${tools}</div>`;
  col.querySelector('[data-op="rename"]')?.addEventListener("click", () => renameSection(sec));
  col.querySelector('[data-op="del"]')?.addEventListener("click", () => deleteSection(sec));
  const body = document.createElement("div");
  body.className = "column-body";
  for (const a of acts) body.appendChild(actionCard(a));
  if (state.editing) {
    const add = document.createElement("button");
    add.className = "add-card";
    add.textContent = "＋ 添加控件";
    add.onclick = () => openAddWizard(sec.id);
    body.appendChild(add);
  }
  if (!acts.length && !state.editing) {
    body.innerHTML = `<div class="add-card" style="cursor:default;border-style:dotted">此栏目还没有动作</div>`;
  }
  col.appendChild(body);
  return col;
}

function actionCard(a) {
  const el = document.createElement("div");
  el.dataset.action = a.id;
  const tools = state.editing ? `
    <div class="c-tools">
      <button class="icon-btn" data-op="del" title="删除">🗑</button>
    </div>` : "";
  // 信息控件：显示实时值（指示灯 / 功率 / 温湿度 / 电量……）
  if (a.action_type === "prop_display") {
    const isSwitch = a.params && a.params.toggle;
    el.className = "card info-card" + (isSwitch ? " as-switch" : "");
    el.innerHTML = `
      ${tools}
      <div class="c-name">${esc(a.name)}</div>
      <div class="c-value" id="iv-${a.id}">…</div>
      <div class="c-dev">${esc(a.device_name || a.device_model || "")}</div>`;
    if (isSwitch) {
      el.title = "点击切换开关";
      el.addEventListener("click", (e) => {
        if (e.target.closest(".c-tools")) return;
        toggleInfoCard(a, el);
      });
    } else {
      el.title = "点击刷新";
      el.addEventListener("click", (e) => {
        if (e.target.closest(".c-tools")) return;
        refreshInfoCards([a.id]);
      });
    }
    el.querySelector('[data-op="del"]')?.addEventListener("click", () => deleteAction(a));
    queueInfoRefresh(a.id);
    return el;
  }
  // 动作按钮卡片
  el.className = "card";
  const badges = [];
  if (a.device_name) badges.push(`<span class="badge">${esc(a.device_name)}</span>`);
  badges.push(`<span class="badge type">${TYPE_LABEL[a.action_type] || a.action_type}</span>`);
  if (a.confirm) badges.push(`<span class="badge confirm">需确认</span>`);
  if (a.require_password) badges.push(`<span class="badge pass">安全密码</span>`);
  el.innerHTML = `
    ${tools}
    <div class="c-name">${esc(a.name)}</div>
    <div class="c-dev">${esc(a.device_model || "")}</div>
    <div class="c-badges">${badges.join("")}</div>`;
  el.addEventListener("click", (e) => {
    if (e.target.closest(".c-tools")) return;
    triggerAction(a, el);
  });
  el.querySelector('[data-op="del"]')?.addEventListener("click", () => deleteAction(a));
  return el;
}

/* ---------------- 信息控件取值 ---------------- */
const _infoQueue = new Set();
function queueInfoRefresh(id) { _infoQueue.add(id); }
async function refreshInfoCards(onlyIds = null) {
  const cards = state.actions.filter(
    (a) => a.action_type === "prop_display" && (!onlyIds || onlyIds.includes(a.id)));
  if (!cards.length || !state.status?.logged_in) return;
  const items = cards.map((a) => ({
    id: a.id, did: a.device_did, siid: a.params.siid, piid: a.params.piid }));
  let vals;
  try {
    const r = await api("POST", "/api/props/read",
                        { items: items.map(({ id, ...it }) => it) });
    if (!r.ok) { markInfoFail(onlyIds || cards.map((c) => c.id)); return; }
    vals = r.data.values || [];
  } catch (e) { markInfoFail(onlyIds || cards.map((c) => c.id)); return; }
  const byKey = {};
  for (const v of vals) byKey[`${v.did}:${v.siid}:${v.piid}`] = v;
  for (const a of cards) {
    const v = byKey[`${a.device_did}:${a.params.siid}:${a.params.piid}`];
    const el = $(`#iv-${a.id}`);
    if (!el) continue;
    el.classList.remove("bad");
    if (!v || v.code !== 0) {
      el.textContent = v && v.code !== 0 ? "读取失败" : "—";
      el.classList.add("bad");
      continue;
    }
    el.innerHTML = fmtPropValue(v.value, a.params) +
      (a.params.unit ? `<span class="c-unit">${esc(a.params.unit)}</span>` : "");
    el.dataset.raw = typeof v.value === "object" ? JSON.stringify(v.value) : String(v.value);
  }
}
function markInfoFail(ids) {
  for (const id of ids || []) {
    const el = $(`#iv-${id}`);
    if (el) { el.textContent = "—"; el.classList.add("bad"); }
  }
}
function fmtPropValue(v, params) {
  if (v === true || v === 1 || v === "1") return `<span class="v-on">开</span>`;
  if (v === false || v === 0 || v === "0") return `<span class="v-off">关</span>`;
  if (typeof v === "number") {
    let t = (Math.round(v * 100) / 100).toString();
    if (params && params.unit === "℃" && v > 100) t = (v / 100).toString();
    return t;
  }
  if (typeof v === "string") return esc(v.slice(0, 20));
  if (v && typeof v === "object") {
    if (v.brightness !== undefined) return esc(String(v.brightness));
    return esc(JSON.stringify(v).slice(0, 20));
  }
  return "—";
}
async function toggleInfoCard(a, el) {
  const cur = el.querySelector(".c-value")?.dataset.raw;
  const isOn = cur === "true" || cur === "1";
  const nv = isOn ? false : true;
  const val = el.querySelector(".c-value");
  if (val) { val.innerHTML = `<span class="${nv ? "v-on" : "v-off"}">…</span>`; }
  try {
    const r = await api("POST", `/api/actions/${a.id}/run`, {});
    if (!r.ok) { toast(r.error || "切换失败", "bad", 3600); refreshInfoCards([a.id]); return; }
    if (val) {
      val.innerHTML = `<span class="${nv ? "v-on" : "v-off"}">${nv ? "开" : "关"}</span>`;
      val.dataset.raw = nv ? "true" : "false";
    }
    toast(`✓ ${a.name} 已${nv ? "打开" : "关闭"}`, "good", 1800);
  } catch (e) { toast("切换异常", "bad"); refreshInfoCards([a.id]); }
}
setInterval(() => refreshInfoCards(), 30000);


/* ---------------- 添加控件向导（简化版） ---------------- */
async function openAddWizard(sectionId) {
  if (!state.status?.logged_in) { toast("请先登录小米账号", "bad"); return openLoginModal(); }
  if (!state.devices.length) {
    const r = await api("GET", "/api/devices?refresh=1");
    if (r.ok) state.devices = r.data.devices || [];
  }
  if (!state.devices.length) return toast("没有可用设备，请先在「设备」里刷新", "bad", 4000);
  const devOpts = [...state.devices]
    .sort((a, b) => (b.isOnline - a.isOnline))
    .map((d) => `<option value="${esc(String(d.did))}">${d.isOnline ? "🟢" : "⚪"} ${esc(d.name)}（${esc(d.model || "")}）</option>`)
    .join("");
  openModal(`
    <h3>添加控件</h3>
    <p class="m-sub">选设备 → 勾选要显示的信息或要执行的按钮 → 添加。</p>
    <div class="form-row">
      <label>选择设备</label>
      <select id="wz-dev">${devOpts}</select>
    </div>
    <div id="wz-body" class="wiz-body"><p class="hint" style="text-align:center">正在读取设备能力…</p></div>
    <div class="modal-actions">
      <button class="ghost-btn" id="wz-adv">高级模式（手动填写）</button>
      <button class="primary-btn" id="wz-go" disabled>添加</button>
    </div>`);
  $("#wz-adv").onclick = () => { closeModal(); openActionEditor(null, sectionId); };
  const loadSpec = async () => {
    const did = $("#wz-dev").value;
    const dev = state.devices.find((d) => String(d.did) === String(did));
    const body = $("#wz-body");
    body.innerHTML = `<p class="hint" style="text-align:center">正在读取设备能力…</p>`;
    $("#wz-go").disabled = true;
    const sp = await api("GET", `/api/devices/${encodeURIComponent(dev.model)}/spec`);
    if (!sp.ok || !sp.data) {
      body.innerHTML = `<p class="hint" style="text-align:center">该设备没有公开的功能描述（spec），可用「高级模式」手动添加。</p>`;
      return;
    }
    const specObj = sp.data.spec || sp.data;
    const services = specObj.services || [];
    const props = [], acts = [];
    const CN = {switch:"开关", on:"开关", power:"功率", "power-consumption":"功率",
      "electric-power":"功率", "electric-current":"电流", voltage:"电压",
      "indicator-light":"指示灯", "battery-level":"电量", battery:"电量",
      temperature:"温度", "relative-humidity":"湿度", humidity:"湿度",
      illumination:"光照度", brightness:"亮度", color:"色彩", mode:"模式",
      "fan-level":"风量", status:"状态", fault:"故障", "motor-control":"电机",
      "target-temperature":"目标温度", "target-humidity":"目标湿度",
      "alarm-switch":"告警开关", "physical-controls-locked":"童锁"};
    const label = (desc) => (desc && CN[desc.toLowerCase()]) || desc;
    for (const sv of services) {
      for (const pr of sv.properties || []) {
        const acc = pr.access || [];
        if (!acc.includes("read")) continue;
        const writable = acc.includes("write");
        const isBool = pr.format === "bool";
        props.push({ siid: sv.iid, piid: pr.iid, desc: label(pr.description) || ("属性" + pr.iid),
                     unit: pr.unit || "", writable, isBool });
      }
      for (const ac of sv.actions || []) {
        if ((ac.in || []).length) continue;      // 带参数的动作走高级模式
        acts.push({ siid: sv.iid, aiid: ac.iid, desc: label(ac.description) || ("动作" + ac.iid) });
      }
    }
    if (!props.length && !acts.length) {
      body.innerHTML = `<p class="hint" style="text-align:center">该设备没有可显示的信息或可执行的动作。</p>`;
      return;
    }
    const propRows = props.map((p, i) => `
      <label class="wiz-item">
        <input type="checkbox" class="wz-prop" data-i="${i}" ${p.isBool && p.writable ? "" : "checked"}>
        <span class="wiz-name">${esc(p.desc)}</span>
        <span class="wiz-tag">${p.writable && p.isBool ? "开关" : "信息"}${p.unit ? " · " + esc(p.unit) : ""}</span>
      </label>`).join("");
    const actRows = acts.map((a, i) => `
      <label class="wiz-item">
        <input type="checkbox" class="wz-act" data-i="${i}" checked>
        <span class="wiz-name">${esc(a.desc)}</span>
        <span class="wiz-tag">按钮</span>
      </label>`).join("");
    body.innerHTML = `
      ${props.length ? `<div class="wiz-h">显示信息（勾选后主页会显示实时数值 / 开关）</div>${propRows}` : ""}
      ${acts.length ? `<div class="wiz-h">一键按钮</div>${actRows}` : ""}`;
    $("#wz-go").disabled = false;
    $("#wz-go").textContent = `添加（勾选后点击）`;
    $("#wz-go").onclick = async () => {
      const go = $("#wz-go"); go.disabled = true;
      const picks = [];
      body.querySelectorAll(".wz-prop:checked").forEach((cb) => {
        const p = props[+cb.dataset.i];
        picks.push({
          name: p.desc, action_type: "prop_display",
          device_did: dev.did, device_name: dev.name, device_model: dev.model,
          params: { siid: p.siid, piid: p.piid, unit: p.unit,
                    toggle: (p.writable && p.isBool) ? "1" : "" } });
      });
      body.querySelectorAll(".wz-act:checked").forEach((cb) => {
        const a = acts[+cb.dataset.i];
        picks.push({
          name: a.desc, action_type: "miot_action",
          device_did: dev.did, device_name: dev.name, device_model: dev.model,
          params: { siid: a.siid, aiid: a.aiid, in: [] } });
      });
      if (!picks.length) { go.disabled = false; return toast("请先勾选至少一项", "bad"); }
      let n = 0;
      for (const pk of picks) {
        const r = await api("POST", "/api/actions", { ...pk, section_id: sectionId });
        if (r.ok) n++;
      }
      go.disabled = false;
      toast(`✓ 已添加 ${n} 个控件`, "good", 2800);
      closeModal();
      await loadAll(true);
      refreshInfoCards();
    };
  };
  $("#wz-dev").onchange = loadSpec;
  loadSpec();
}

/* ---------------- 动作执行 ---------------- */
function triggerAction(a, el) {
  if (a.require_password) return openPasswordModal(a, el);
  if (a.confirm) return openConfirmModal(a, el);
  doRun(a, null, el);
}
function openConfirmModal(a, el) {
  openModal(`
    <h3>确认执行「${esc(a.name)}」？</h3>
    <p class="m-sub">目标设备：${esc(a.device_name || a.device_did || "—")}</p>
    <div class="modal-actions">
      <button class="ghost-btn" id="m-cancel">取消</button>
      <button class="primary-btn" id="m-go">执行</button>
    </div>`);
  $("#m-cancel").onclick = closeModal;
  $("#m-go").onclick = () => { closeModal(); doRun(a, null, el); };
}
function openPasswordModal(a, el) {
  openModal(`
    <h3>🔒 安全密码</h3>
    <p class="m-sub">「${esc(a.name)}」需要验证小米账号密码。</p>
    <div class="form-row">
      <label>小米账号密码</label>
      <input type="password" id="m-pw" autocomplete="off">
    </div>
    <div class="modal-actions">
      <button class="ghost-btn" id="m-cancel">取消</button>
      <button class="primary-btn" id="m-go">验证并执行</button>
    </div>`);
  $("#m-pw").focus();
  const go = () => {
    const pw = $("#m-pw").value;
    if (!pw) return toast("请输入密码", "bad");
    closeModal(); doRun(a, pw, el);
  };
  $("#m-go").onclick = go;
  $("#m-pw").onkeydown = (e) => { if (e.key === "Enter") go(); };
  $("#m-cancel").onclick = closeModal;
}
async function doRun(a, password, el) {
  el?.classList.add("running");
  const r = await api("POST", `/api/actions/${a.id}/run`, password ? { password } : {});
  el?.classList.remove("running");
  if (r.ok) {
    const res = r.result;
    const brief = res && res.result && res.result[0] !== undefined
      ? JSON.stringify(res.result[0]) : "";
    toast(`✓ ${a.name} 执行成功${brief ? " · " + brief.slice(0, 80) : ""}`, "good", 3200);
  } else if (r.need_password) {
    openPasswordModal(a, el);
  } else {
    toast(`✗ ${r.error || "执行失败"}`, "bad", 4200);
  }
}

/* ---------------- 登录 ---------------- */
function openLoginModal() {
  openModal(`
    <h3>登录小米账号</h3>
    <p class="m-sub">账号仅保存在本机 NAS，用于获取设备列表与执行动作。</p>
    <button class="primary-btn" id="lg-oauth" style="width:100%">打开小米官方登录页</button>
    <p class="hint" style="text-align:center;margin:6px 0 0">验证码 / 短信等安全验证都在小米官方页面完成。</p>
    <div id="lg-oauth-box" class="notice hidden">
      <b>① 在官方页面完成登录</b>。<br>
      登录后浏览器会跳到一个<b>打不开的页面</b>，这是正常的。<br>
      <b>② 复制浏览器地址栏里的完整网址</b>，粘贴到下面：
      <div class="form-row" style="margin-top:8px">
        <input type="text" id="lg-paste-url" placeholder="http://homeassistant.local:8123/?code=...&state=...">
      </div>
      <div class="modal-actions" style="margin-bottom:0">
        <button class="primary-btn" id="lg-paste-go">完成登录</button>
      </div>
    </div>
    <div class="modal-actions">
      <button class="ghost-btn" id="lg-cancel">取消</button>
    </div>`);
  $("#lg-cancel").onclick = closeModal;
  $("#lg-oauth").onclick = async () => {
    const btn = $("#lg-oauth");
    btn.disabled = true; btn.textContent = "正在跳转小米官方页面…";
    const r = await api("POST", "/api/auth/oauth/start", {});
    if (!r.ok) {
      btn.disabled = false; btn.textContent = "打开小米官方登录页";
      return toast(r.error || "发起官方登录失败", "bad", 4000);
    }
    window.open(r.data.url, "_blank");
    $("#lg-oauth-box").classList.remove("hidden");
    btn.textContent = "官方页面已打开";
    $("#lg-paste-go").onclick = async () => {
      const url = $("#lg-paste-url").value.trim();
      if (!url) return toast("请粘贴登录后浏览器地址栏的完整网址", "bad");
      const go = $("#lg-paste-go"); go.disabled = true; go.textContent = "验证中…";
      const er = await api("POST", "/api/auth/oauth/exchange", { callback_url: url });
      go.disabled = false; go.textContent = "完成登录";
      if (er.ok) {
        toast(`✓ 登录成功${er.data.username ? "（" + er.data.username + "）" : ""}，发现 ${er.data.devices} 台设备`, "good", 3600);
        closeModal();
        await loadAll(true);
      } else {
        toast(er.error || "验证失败", "bad", 5000);
      }
    };
  };
  $("#lg-paste-url").addEventListener("paste", () => setTimeout(() => $("#lg-paste-go")?.click(), 200));
}

/* ---------------- 栏目编辑 ---------------- */
function addSection() {
  openModal(`
    <h3>添加栏目</h3>
    <div class="form-row"><label>栏目名称（如：客厅 / 卧室 / 场景）</label>
      <input type="text" id="s-name" maxlength="20"></div>
    <div class="modal-actions">
      <button class="ghost-btn" id="s-cancel">取消</button>
      <button class="primary-btn" id="s-go">添加</button>
    </div>`);
  $("#s-name").focus();
  const go = async () => {
    const name = $("#s-name").value.trim();
    if (!name) return toast("名称不能为空", "bad");
    const r = await api("POST", "/api/sections", { name });
    closeModal(); if (r.ok) { await loadAll(true); toast("栏目已添加", "good"); }
  };
  $("#s-go").onclick = go;
  $("#s-name").onkeydown = (e) => { if (e.key === "Enter") go(); };
  $("#s-cancel").onclick = closeModal;
}
function renameSection(sec) {
  openModal(`
    <h3>重命名栏目</h3>
    <div class="form-row"><label>新名称</label><input type="text" id="s-name" value="${esc(sec.name)}" maxlength="20"></div>
    <div class="modal-actions">
      <button class="ghost-btn" id="s-cancel">取消</button>
      <button class="primary-btn" id="s-go">保存</button>
    </div>`);
  $("#s-name").focus(); $("#s-name").select();
  const go = async () => {
    const name = $("#s-name").value.trim();
    if (!name) return toast("名称不能为空", "bad");
    const r = await api("PUT", `/api/sections/${sec.id}`, { name });
    closeModal(); if (r.ok) { await loadAll(true); toast("已保存", "good"); }
  };
  $("#s-go").onclick = go;
  $("#s-name").onkeydown = (e) => { if (e.key === "Enter") go(); };
  $("#s-cancel").onclick = closeModal;
}
async function deleteSection(sec) {
  const acts = state.actions.filter((a) => a.section_id === sec.id).length;
  openModal(`
    <h3>删除栏目「${esc(sec.name)}」？</h3>
    <p class="m-sub">${acts ? `栏目内 ${acts} 个动作将一并删除。` : "该栏目为空。"}</p>
    <div class="modal-actions">
      <button class="ghost-btn" id="s-cancel">取消</button>
      <button class="danger-btn" id="s-go">删除</button>
    </div>`);
  $("#s-cancel").onclick = closeModal;
  $("#s-go").onclick = async () => {
    const r = await api("DELETE", `/api/sections/${sec.id}`);
    closeModal(); if (r.ok) { await loadAll(true); toast("已删除", "good"); }
  };
}
async function deleteAction(a) {
  openModal(`
    <h3>删除动作「${esc(a.name)}」？</h3>
    <div class="modal-actions">
      <button class="ghost-btn" id="s-cancel">取消</button>
      <button class="danger-btn" id="s-go">删除</button>
    </div>`);
  $("#s-cancel").onclick = closeModal;
  $("#s-go").onclick = async () => {
    const r = await api("DELETE", `/api/actions/${a.id}`);
    closeModal(); if (r.ok) { await loadAll(true); toast("已删除", "good"); }
  };
}

/* ---------------- 动作编辑器 ---------------- */
function openActionEditor(action = null, presetSection = null) {
  if (!state.status?.logged_in) { toast("请先登录小米账号", "bad"); return openLoginModal(); }
  const isEdit = !!action;
  const a = action || {
    section_id: presetSection ?? (state.sections[0] ? state.sections[0].id : null),
    name: "", device_did: "", device_name: "", device_model: "",
    action_type: "miot_prop_set", params: {}, confirm: false, require_password: false,
  };
  const secOpts = state.sections.map((s) =>
    `<option value="${s.id}" ${s.id === a.section_id ? "selected" : ""}>${esc(s.name)}</option>`).join("");
  if (!secOpts) { toast("请先添加栏目", "bad"); return addSection(); }
  const devOpts = ['<option value="">— 选择设备 —</option>'].concat(state.devices.map((d) =>
    `<option value="${esc(d.did)}" ${String(d.did) === String(a.device_did) ? "selected" : ""}>
      ${esc(d.name)}（${esc(d.model || d.ip || d.did)}）</option>`)).join("");
  const typeOpts = Object.entries(TYPE_LABEL).map(([k, v]) =>
    `<option value="${k}" ${k === a.action_type ? "selected" : ""}>${v}</option>`).join("");
  openModal(`
    <h3>${isEdit ? "编辑动作" : "添加动作"}</h3>
    <div class="form-row"><label>动作名称（按钮上显示的）</label>
      <input type="text" id="a-name" maxlength="30" value="${esc(a.name)}" placeholder="如：客厅灯 · 关"></div>
    <div class="form-grid2">
      <div class="form-row"><label>所属栏目</label><select id="a-section">${secOpts}</select></div>
      <div class="form-row"><label>目标设备</label><select id="a-device">${devOpts}</select></div>
    </div>
    <div class="form-row"><label>动作类型</label><select id="a-type">${typeOpts}</select></div>
    <div id="a-params"></div>
    <div class="switch-row">
      <label>二次确认<span class="hint">点击卡片后先弹出确认框</span></label>
      <label class="switch"><input type="checkbox" id="a-confirm" ${a.confirm ? "checked" : ""}><span class="slider"></span></label>
    </div>
    <div class="switch-row">
      <label>需要小米安全密码<span class="hint">执行前必须输入小米账号密码验证</span></label>
      <label class="switch"><input type="checkbox" id="a-pass" ${a.require_password ? "checked" : ""}><span class="slider"></span></label>
    </div>
    <div class="modal-actions">
      <button class="ghost-btn" id="a-cancel">取消</button>
      <button class="primary-btn" id="a-go">${isEdit ? "保存" : "添加"}</button>
    </div>`);
  $("#a-cancel").onclick = closeModal;
  const renderParams = () => {
    const t = $("#a-type").value;
    const box = $("#a-params");
    const p = a.params || {};
    if (t === "miot_prop_set") {
      box.innerHTML = `
        <div class="notice">云端 MIoT 属性设置。点「从设备规格选取」可按设备型号列出全部可写属性。</div>
        <div class="form-grid3">
          <div class="form-row"><label>siid（服务）</label><input type="number" id="p-siid" value="${esc(p.siid ?? "")}"></div>
          <div class="form-row"><label>piid（属性）</label><input type="number" id="p-piid" value="${esc(p.piid ?? "")}"></div>
          <div class="form-row"><label>value 类型</label>
            <select id="p-vtype">
              <option value="num" ${p.__vtype === "num" ? "selected" : ""}>数字</option>
              <option value="bool" ${p.__vtype === "bool" ? "selected" : ""}>布尔</option>
              <option value="str" ${p.__vtype === "str" ? "selected" : ""}>文本</option>
            </select></div>
        </div>
        <div class="form-row"><label>value（按类型自动转换）</label>
          <input type="text" id="p-value" value="${esc(p.__vtype === "bool" ? (p.value ? "true" : "false") : (p.value ?? ""))}"></div>
        <button class="mini-btn" id="p-spec">从设备规格选取</button>`;
    } else if (t === "miot_action") {
      box.innerHTML = `
        <div class="notice">云端 MIoT 动作调用（如扫地、开机）。可从设备规格选取。</div>
        <div class="form-grid2">
          <div class="form-row"><label>siid</label><input type="number" id="p-siid" value="${esc(p.siid ?? "")}"></div>
          <div class="form-row"><label>aiid</label><input type="number" id="p-aiid" value="${esc(p.aiid ?? "")}"></div>
        </div>
        <div class="form-row"><label>入参 in（JSON 数组，可为 []）</label>
          <textarea id="p-in">${esc(JSON.stringify(p.in || []))}</textarea></div>
        <button class="mini-btn" id="p-spec">从设备规格选取</button>`;
    } else if (t === "rpc") {
      box.innerHTML = `
        <div class="notice">旧版通用 RPC（部分老设备用它）。method 如 set_power / get_prop。</div>
        <div class="form-row"><label>method</label><input type="text" id="p-method" value="${esc(p.method ?? "")}"></div>
        <div class="form-row"><label>params（JSON 数组）</label>
          <textarea id="p-params">${esc(JSON.stringify(p.params || []))}</textarea></div>`;
    } else if (t === "miio_lan") {
      box.innerHTML = `
        <div class="notice">局域网直连（UDP 54321）。需要设备 token 与本机可达设备 IP。</div>
        <div class="form-grid2">
          <div class="form-row"><label>IP</label><input type="text" id="p-ip" value="${esc(p.ip ?? "")}"></div>
          <div class="form-row"><label>token（32 位）</label><input type="text" id="p-token" value="${esc(p.token ?? "")}"></div>
        </div>
        <div class="form-row"><label>method</label><input type="text" id="p-method" value="${esc(p.method ?? "")}"></div>
        <div class="form-row"><label>params（JSON 数组）</label>
          <textarea id="p-params">${esc(JSON.stringify(p.params || []))}</textarea></div>`;
    }
    box.querySelector("#p-spec")?.addEventListener("click", openSpecPicker);
  };
  renderParams();
  $("#a-type").onchange = renderParams;
  $("#a-device").onchange = () => { /* 选中后记录 model 名 */ };
  $("#a-go").onclick = async () => {
    const t = $("#a-type").value;
    const dev = state.devices.find((d) => String(d.did) === $("#a-device").value) || {};
    let params = {};
    try {
      if (t === "miot_prop_set") {
        const vtype = $("#p-vtype").value;
        let value = $("#p-value").value;
        if (vtype === "num") value = Number(value);
        else if (vtype === "bool") value = String(value).trim() === "true" || value === "1" || value === "开";
        params = { siid: Number($("#p-siid").value), piid: Number($("#p-piid").value), value, __vtype: vtype };
      } else if (t === "miot_action") {
        params = { siid: Number($("#p-siid").value), aiid: Number($("#p-aiid").value), in: JSON.parse($("#p-in").value || "[]") };
      } else if (t === "rpc") {
        params = { method: $("#p-method").value.trim(), params: JSON.parse($("#p-params").value || "[]") };
      } else if (t === "miio_lan") {
        params = { ip: $("#p-ip").value.trim(), token: $("#p-token").value.trim(),
                   method: $("#p-method").value.trim(), params: JSON.parse($("#p-params").value || "[]") };
      }
    } catch (e) { return toast("参数格式错误：" + e.message, "bad"); }
    const name = $("#a-name").value.trim();
    if (!name) return toast("请填写动作名称", "bad");
    const payload = {
      section_id: Number($("#a-section").value), name,
      device_did: String(dev.did ?? $("#a-device").value ?? ""),
      device_name: dev.name || "", device_model: dev.model || "",
      action_type: t, params,
      confirm: $("#a-confirm").checked, require_password: $("#a-pass").checked,
    };
    const r = isEdit ? await api("PUT", `/api/actions/${a.id}`, payload)
                     : await api("POST", "/api/actions", payload);
    if (r.ok) { closeModal(); await loadAll(true); toast(isEdit ? "已保存" : "动作已添加", "good"); }
    else toast(r.error || "保存失败", "bad");
  };
}

/* ---------------- spec 选取器 ---------------- */
async function openSpecPicker() {
  const dev = state.devices.find((d) => String(d.did) === $("#a-device").value);
  if (!dev || !dev.model) return toast("请先选择目标设备", "bad");
  const t = $("#a-type").value;
  toast("正在拉取设备规格…", "", 1600);
  const r = await api("GET", `/api/devices/${encodeURIComponent(dev.model)}/spec`);
  if (!r.ok) return toast(r.error || "规格拉取失败", "bad", 4000);
  const spec = r.data.spec || {};
  const services = spec.services || [];
  if (t === "miot_prop_set") {
    const writable = [];
    for (const sv of services) {
      for (const pr of sv.properties || []) {
        if ((pr.access || []).includes("write")) writable.push({ s: sv, p: pr });
      }
    }
    const opts = writable.map((w) =>
      `<option value="${w.s.siid}:${w.p.piid}" data-format="${esc(w.p.format || "")}">
        ${esc(w.s.siid === 1 ? "[设备]" : "")} ${esc(w.p.description || w.p.name)}（siid ${w.s.siid} / piid ${w.p.piid}，${esc(w.p.format || "")}）</option>`).join("");
    if (!opts) return toast("该设备没有可写属性", "bad");
    openModal(`
      <h3>选择属性（${esc(dev.model)}）</h3>
      <p class="m-sub">共 ${writable.length} 个可写属性</p>
      <div class="form-row"><select id="sp-pick" size="10" style="height:auto">${opts}</select></div>
      <div class="modal-actions"><button class="ghost-btn" id="sp-cancel">取消</button>
        <button class="primary-btn" id="sp-go">填入</button></div>`);
    $("#sp-cancel").onclick = closeModal;
    $("#sp-go").onclick = () => {
      const [siid, piid] = $("#sp-pick").value.split(":").map(Number);
      const w = writable.find((x) => x.s.siid === siid && x.p.piid === piid);
      $("#p-siid").value = siid; $("#p-piid").value = piid;
      const fmt = w ? (w.p.format || "") : "";
      if (fmt === "bool") $("#p-vtype").value = "bool";
      else if (["int", "uint8", "uint16", "uint32", "float"].includes(fmt)) $("#p-vtype").value = "num";
      else $("#p-vtype").value = "str";
      closeModal(); toast("已填入", "good", 1200);
    };
  } else if (t === "miot_action") {
    const acts = [];
    for (const sv of services) for (const ac of sv.actions || []) acts.push({ s: sv, a: ac });
    const opts = acts.map((x) =>
      `<option value="${x.s.siid}:${x.a.aiid}">
        ${esc(x.s.siid === 1 ? "[设备]" : "")} ${esc(x.a.description || x.a.name)}（siid ${x.s.siid} / aiid ${x.a.aiid}）</option>`).join("");
    if (!opts) return toast("该设备没有可用动作", "bad");
    openModal(`
      <h3>选择动作（${esc(dev.model)}）</h3>
      <div class="form-row"><select id="sp-pick" size="10" style="height:auto">${opts}</select></div>
      <div class="modal-actions"><button class="ghost-btn" id="sp-cancel">取消</button>
        <button class="primary-btn" id="sp-go">填入</button></div>`);
    $("#sp-cancel").onclick = closeModal;
    $("#sp-go").onclick = () => {
      const [siid, aiid] = $("#sp-pick").value.split(":").map(Number);
      const x = acts.find((y) => y.s.siid === siid && y.a.aiid === aiid);
      $("#p-siid").value = siid; $("#p-aiid").value = aiid;
      $("#p-in").value = JSON.stringify(x && x.a.in ? x.a.in : []);
      closeModal(); toast("已填入", "good", 1200);
    };
  }
}

/* ---------------- 设备 & 调试页 ---------------- */
function openDevicesModal() {
  if (!state.status?.logged_in) { toast("请先登录小米账号", "bad"); return openLoginModal(); }
  const list = state.devices.map((d) => {
    const online = d.is_online !== undefined ? d.is_online : d.isOnline;
    return `
    <div class="dev-item" data-did="${esc(d.did)}">
      <span class="dot ${online ? "online" : ""}"></span>
      <div class="meta">
        <b>${esc(d.name)}</b>
        <small>${esc(d.model || "")} · ${esc(d.ip || "")} · did ${esc(d.did)}${online ? "" : " · 离线"}</small>
      </div>
      <div class="acts">
        <button class="mini-btn" data-op="token">复制 token</button>
        <button class="mini-btn" data-op="miio">miIO 测试</button>
      </div>
    </div>`;
  }).join("") || `<p style="color:var(--sub)">没有设备。试试刷新。</p>`;
  openModal(`
    <h3>设备（${state.devices.length}）</h3>
    <div class="modal-actions" style="margin:0 0 10px">
      <button class="ghost-btn left" id="dv-refresh">⟳ 刷新设备列表</button>
      <button class="mini-btn" id="dv-miio-console">局域网 miIO 调试台</button>
    </div>
    <div class="dev-list">${list}</div>
    <div class="modal-actions"><button class="ghost-btn" id="dv-close">关闭</button></div>`);
  $("#dv-close").onclick = closeModal;
  $("#dv-refresh").onclick = async () => {
    toast("刷新中…", "", 3000);
    const r = await api("GET", "/api/devices?refresh=1");
    if (r.ok) { state.devices = r.data.devices; await loadAll(true); closeModal(); openDevicesModal(); toast(`共 ${state.devices.length} 台`, "good"); }
    else toast(r.error || "刷新失败", "bad", 4200);
  };
  $("#dv-miio-console").onclick = () => openMiioConsole();
  $("#modal-box").querySelectorAll('.dev-item').forEach((item) => {
    const did = item.dataset.did;
    const dev = state.devices.find((d) => String(d.did) === String(did));
    item.querySelector('[data-op="token"]').onclick = () => {
      navigator.clipboard?.writeText(dev.token || "").then(
        () => toast("token 已复制", "good"), () => toast(dev.token || "（无 token）", "", 6000));
    };
    item.querySelector('[data-op="miio"]').onclick = () => openMiioConsole(dev);
  });
}

function openMiioConsole(dev = null) {
  openModal(`
    <h3>局域网 miIO 调试台</h3>
    <p class="m-sub">直连设备（UDP 54321）。需与本机同一网段。常用：miIO.info / get_prop / set_power。</p>
    <div class="form-grid3">
      <div class="form-row"><label>IP</label><input type="text" id="mc-ip" value="${esc(dev?.ip || "")}"></div>
      <div class="form-row"><label>token</label><input type="text" id="mc-token" value="${esc(dev?.token || "")}"></div>
      <div class="form-row"><label>method</label><input type="text" id="mc-method" value="miIO.info"></div>
    </div>
    <div class="form-row"><label>params（JSON 数组）</label><textarea id="mc-params">[]</textarea></div>
    <div class="modal-actions">
      <button class="ghost-btn" id="mc-cancel">关闭</button>
      <button class="primary-btn" id="mc-go">发送</button>
    </div>
    <div id="mc-out" class="result-box hidden"></div>`);
  $("#mc-cancel").onclick = closeModal;
  $("#mc-go").onclick = async () => {
    const out = $("#mc-out");
    out.classList.remove("hidden"); out.textContent = "发送中…";
    let params; try { params = JSON.parse($("#mc-params").value || "[]"); }
    catch (e) { out.textContent = "params JSON 错误：" + e.message; return; }
    const r = await api("POST", "/api/miio/send", {
      ip: $("#mc-ip").value.trim(), token: $("#mc-token").value.trim(),
      method: $("#mc-method").value.trim(), params });
    out.textContent = JSON.stringify(r.ok ? r.result : r.error, null, 2);
  };
}

/* ---------------- 顶部按钮 ---------------- */
$("#btn-login").onclick = () => openLoginModal();
$("#btn-logout").onclick = async () => {
  openModal(`
    <h3>退出登录？</h3>
    <p class="m-sub">将清除本机保存的账号、密码与登录态（栏目和动作保留）。</p>
    <div class="modal-actions">
      <button class="ghost-btn" id="s-cancel">取消</button>
      <button class="danger-btn" id="s-go">退出</button>
    </div>`);
  $("#s-cancel").onclick = closeModal;
  $("#s-go").onclick = async () => { await api("POST", "/api/auth/logout", {}); closeModal(); await loadAll(true); toast("已退出", "good"); };
};
$("#btn-edit").onclick = () => {
  state.editing = !state.editing;
  $("#btn-edit").classList.toggle("active", state.editing);
  $("#btn-edit").textContent = state.editing ? "完成" : "编辑";
  render();
};
$("#btn-devices").onclick = () => openDevicesModal();
$("#btn-settings").onclick = () => openSettingsModal();

/* ---------------- 面板密码登录框 ---------------- */
function openPanelLoginModal() {
  openModal(`
    <h3>面板访问验证</h3>
    <p class="m-sub">当前通过局域网直连访问，需要输入面板密码。
       从 fnOS 桌面打开（统一网关）则无需此验证。</p>
    <div class="form-row">
      <label>面板密码</label>
      <input type="password" id="pl-pass" autocomplete="off">
    </div>
    <div class="modal-actions">
      <button class="ghost-btn" id="pl-cancel">取消</button>
      <button class="primary-btn" id="pl-go">解锁</button>
    </div>`);
  $("#pl-pass").focus();
  const go = async () => {
    const password = $("#pl-pass").value;
    if (!password) return toast("请输入面板密码", "bad");
    const r = await api("POST", "/api/panel/login", { password });
    if (r.ok) { toast("✓ 已解锁", "good"); location.reload(); }
    else toast(r.error || "密码不正确", "bad", 3600);
  };
  $("#pl-go").onclick = go;
  $("#pl-pass").onkeydown = (e) => { if (e.key === "Enter") go(); };
}

/* ---------------- 面板设置弹窗 ---------------- */
async function openSettingsModal() {
  const cfg = await api("GET", "/api/panel/config");
  const c = cfg.data || {};
  const pwSet = !!c.password_set;
  openModal(`
    <h3>面板设置</h3>
    <p class="m-sub">从 fnOS 桌面打开（统一网关 /app/mihome-admin）时，
       访问跟随 fnOS 登录状态，无需面板密码。</p>
    <h4 class="set-h">局域网监听（${esc(String(c.port || ""))} 端口直连）</h4>
    <div class="form-row switch-line">
      <label class="switch"><input type="checkbox" id="st-listen" ${c.listen_enabled ? "checked" : ""}><span class="slider"></span></label>
      <span>允许局域网设备直连访问</span>
    </div>
    <div class="form-row">
      <label>绑定地址</label>
      <input type="text" id="st-addr" value="${esc(c.listen_addr || "0.0.0.0")}"
             placeholder="0.0.0.0 全部网卡 / 127.0.0.1 仅本机">
      <span class="hint">0.0.0.0=局域网可访问；127.0.0.1=仅本机</span>
    </div>
    <h4 class="set-h">面板密码（保护局域网直连）</h4>
    ${pwSet ? `
    <div class="form-row">
      <label>旧密码</label>
      <input type="password" id="st-old" autocomplete="off">
    </div>` : ""}
    <div class="form-row">
      <label>${pwSet ? "新密码" : "设置密码（至少 4 位）"}</label>
      <input type="password" id="st-new" autocomplete="off">
    </div>
    ${pwSet ? `
    <div class="form-row">
      <button class="danger-btn" id="st-clear">清除密码（恢复免验证）</button>
    </div>` : ""}
    <div class="modal-actions">
      <button class="ghost-btn" id="st-cancel">关闭</button>
      <button class="primary-btn" id="st-save">保存</button>
    </div>`);
  $("#st-cancel").onclick = closeModal;
  $("#st-save").onclick = async () => {
    // 1) 监听配置
    const lr = await api("POST", "/api/panel/listen", {
      enabled: $("#st-listen").checked,
      addr: $("#st-addr").value.trim() || "0.0.0.0",
    });
    if (!lr.ok) return toast(lr.error || "监听设置失败", "bad", 3600);
    // 2) 密码（填了才改）
    const nw = $("#st-new").value;
    const oldV = pwSet ? ($("#st-old")?.value || "") : "";
    if (nw) {
      if (nw.length < 4) return toast("新密码至少 4 位", "bad");
      const pr = await api("POST", "/api/panel/password", { old: oldV, new: nw });
      if (!pr.ok) return toast(pr.error || "密码设置失败", "bad", 3600);
      toast("✓ 密码已更新", "good");
    }
    toast("✓ 设置已保存", "good", 2000);
    closeModal();
  };
  const clearBtn = $("#st-clear");
  if (clearBtn) clearBtn.onclick = async () => {
    const oldV = $("#st-old")?.value || "";
    const r = await api("POST", "/api/panel/password/clear", { old: oldV });
    if (r.ok) { toast("✓ 密码已清除", "good"); closeModal(); }
    else toast(r.error || "清除失败", "bad", 3600);
  };
}

/* ---------------- 启动 ---------------- */
loadAll(true);
