"use strict";
// SplitSecond web UI: plain JS, no build step. User data always goes through
// textContent (via h()), never innerHTML.

const $app = document.getElementById("app");
const $modal = document.getElementById("modal");
const $live = document.getElementById("live");
const $logout = document.getElementById("logout");

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch {} },
};

const state = {
  token: store.get("token"),
  me: null,
  group: null,        // current group, including members
  refresh: null,      // re-render function of the current view
  ws: null, wsGroup: null, wsRetry: 0,
};

// ------------------------------------------------------------------ helpers

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k in el && k !== "list") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

// Flatten nested arrays and drop null/false, so views can mix conditionals and lists.
const nodes = (...kids) => kids.flat(Infinity).filter((k) => k != null && k !== false);

function fmt(cents) {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  return `${sign}$${Math.floor(abs / 100).toLocaleString("en-CA")}.${String(abs % 100).padStart(2, "0")}`;
}

function parseCents(text) {
  const m = /^\s*\$?\s*(\d{1,9})(?:\.(\d{0,2}))?\s*$/.exec(String(text ?? ""));
  if (!m) return null;
  return Number(m[1]) * 100 + Number((m[2] || "").padEnd(2, "0"));
}

const centsToInput = (c) => (c / 100).toFixed(2);
const fmtTime = (iso) => new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });

// Idempotency keys must be UUIDs. crypto.randomUUID only exists on HTTPS/localhost.
function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
  const x = [...b].map((v) => v.toString(16).padStart(2, "0")).join("");
  return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20)}`;
}

function toast(msg, isErr = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 3200);
}

class ApiError extends Error {
  constructor(status, data) {
    const d = data.error ?? data.detail;
    super(typeof d === "string" ? d : Array.isArray(d) ? d.map((e) => e.msg).join("; ") : `HTTP ${status}`);
    this.status = status; this.data = data;
  }
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && state.token) { logout(); throw new ApiError(401, data); }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

function name(uid) {
  if (state.me && uid === state.me.id) return "You";
  return state.group?.members.find((m) => m.id === uid)?.username ?? `#${uid}`;
}

function openModal(...content) {
  document.getElementById("modal-body").replaceChildren(...nodes(content));
  if (!$modal.open) $modal.showModal();
}
const closeModal = () => $modal.open && $modal.close();
$modal.addEventListener("click", (e) => { if (e.target === $modal) closeModal(); });

function guard(fn) {
  return async (...args) => {
    try { await fn(...args); }
    catch (err) {
      toast(err.message, true);
      if (err.status === 409) { closeModal(); state.refresh?.(); }
    }
  };
}

// ------------------------------------------------------------------ realtime

function connectGroup(gid) {
  if (state.wsGroup === gid && state.ws && state.ws.readyState <= 1) return;
  disconnect();
  state.wsGroup = gid;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/groups/${gid}?token=${encodeURIComponent(state.token)}`);
  state.ws = ws;
  ws.onopen = () => { state.wsRetry = 0; setLive(true); };
  ws.onmessage = (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "ping") { ws.send("pong"); return; }
    scheduleRefresh(msg);  // "hello" = (re)connected: we may have missed events, so reload too
  };
  ws.onclose = () => {
    // Intentional disconnect() clears state.ws first; don't flash "reconnecting".
    if (state.ws !== ws || state.wsGroup !== gid) return;
    setLive(false);
    const delay = Math.min(1000 * 2 ** state.wsRetry++, 15000);
    setTimeout(() => { if (state.wsGroup === gid && state.ws === ws) { state.ws = null; connectGroup(gid); } }, delay);
  };
}

function disconnect() {
  const ws = state.ws;
  state.ws = null; state.wsGroup = null;
  if (ws) ws.close();
  setLive(false, true);
}

function setLive(on, hide = false) {
  $live.hidden = hide;
  $live.className = "live" + (on ? "" : " off");
  $live.textContent = on ? "live" : "reconnecting";
}

let refreshTimer = null, pendingEvents = [];
function scheduleRefresh(msg) {
  pendingEvents.push(msg);
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    const events = pendingEvents; pendingEvents = [];
    state.refresh?.(events);
  }, 120);
}

// ------------------------------------------------------------------ routing

function logout() {
  state.token = null; state.me = null; store.set("token", null);
  disconnect();
  location.hash = "#/";
  render();
}
$logout.onclick = logout;

async function ensureGroup(gid) {
  if (!state.group || state.group.id !== gid) state.group = await api("GET", `/api/groups/${gid}`);
  connectGroup(gid);
}

async function render() {
  state.refresh = null;
  closeModal();
  $logout.hidden = !state.token;
  if (!state.token) { disconnect(); return renderAuth(); }
  try {
    if (!state.me) state.me = await api("GET", "/api/me");
    const parts = location.hash.replace(/^#\/?/, "").split("/");
    if (parts[0] === "g" && parts[1]) {
      await ensureGroup(Number(parts[1]));
      return renderGroup(parts[2] || "expenses");
    }
    if (parts[0] === "b" && parts[1]) return renderBill(Number(parts[1]));
    disconnect();
    state.group = null;
    return renderGroups();
  } catch (err) {
    if (err.status === 404) { toast("Not found"); location.hash = "#/"; return; }
    $app.replaceChildren(h("div", { class: "empty" }, "Couldn't load: ", err.message,
      h("div", {}, h("button", { class: "secondary", onclick: render }, "Retry"))));
  }
}
window.addEventListener("hashchange", render);

// ------------------------------------------------------------------ auth

function renderAuth(mode = "login") {
  const isSignup = mode === "signup";
  const form = h("form", {
    onsubmit: guard(async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      const res = await api("POST", isSignup ? "/api/auth/signup" : "/api/auth/login",
        { username: f.get("username"), password: f.get("password") });
      state.token = res.token; state.me = res.user; store.set("token", res.token);
      render();
    }) },
    h("label", {}, "Username"),
    h("input", { name: "username", required: true, minLength: 3, maxLength: 40, autocomplete: "username",
      autocapitalize: "none", pattern: "[A-Za-z0-9_.\\-]+", title: "letters, numbers, _ . -" }),
    h("label", {}, "Password"),
    h("input", { name: "password", type: "password", required: true, minLength: isSignup ? 8 : 1,
      autocomplete: isSignup ? "new-password" : "current-password" }),
    h("button", { type: "submit" }, isSignup ? "Create account" : "Log in"));
  const mark = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  mark.setAttribute("viewBox", "0 0 32 32");
  mark.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  for (const [k, v] of Object.entries({ d: "M7 24L25 8M7 9h8M17 23h8", stroke: "white", "stroke-width": "3.2",
    "stroke-linecap": "round", fill: "none" })) path.setAttribute(k, v);
  mark.append(path);
  $app.replaceChildren(h("div", { class: "auth" },
    h("div", { class: "auth-mark" }, mark),
    h("p", { class: "brand-hero" }, "Split", h("span", {}, "Second")),
    h("h1", {}, isSignup ? "Create your account" : "Welcome back"),
    h("p", { class: "lede" }, "Split bills fairly, claim your own items, and keep a history nobody can lose."),
    form,
    h("p", { class: "muted small switch" }, isSignup ? "Already have an account? " : "New here? ",
      h("button", { class: "link", onclick: () => renderAuth(isSignup ? "login" : "signup") }, isSignup ? "Log in" : "Sign up"))));
}

// ------------------------------------------------------------------ groups

async function renderGroups() {
  const groups = await api("GET", "/api/groups");
  const list = groups.length ? h("div", { class: "list" }, groups.map((g) =>
    h("div", { class: "card row", onclick: () => (location.hash = `#/g/${g.id}`) },
      h("div", { class: "grow" }, h("strong", {}, g.name),
        h("div", { class: "muted small" }, `${g.member_count} member${g.member_count === 1 ? "" : "s"}`)),
      netLabel(g.my_net_cents))))
    : h("div", { class: "empty" }, "No groups yet. Create one, then add your friends by username.");

  const create = h("form", { class: "card", onsubmit: guard(async (e) => {
    e.preventDefault();
    const g = await api("POST", "/api/groups", { name: e.target.gname.value });
    location.hash = `#/g/${g.id}`;
  }) }, h("label", {}, "New group"), h("div", { class: "row" },
    h("input", { name: "gname", placeholder: "e.g. Flat 4B, Montréal trip", required: true, maxLength: 100 }),
    h("button", { type: "submit" }, "Create")));

  $app.replaceChildren(h("h1", {}, `Hi, ${state.me.username}`), list, create,
    h("p", { class: "muted small" }, `Your username is “${state.me.username}”. Share it so friends can add you to their groups.`));
  state.refresh = renderGroups;
}

function netLabel(cents, me = true) {
  if (cents === 0) return h("span", { class: "muted small" }, "settled up");
  const owed = cents > 0;
  return h("div", { style: "text-align:right" },
    h("div", { class: "small muted" }, me ? (owed ? "you're owed" : "you owe") : (owed ? "is owed" : "owes")),
    h("div", { class: `amount ${owed ? "pos" : "neg"}` }, fmt(Math.abs(cents))));
}

// ------------------------------------------------------------------ group

const TABS = [["expenses", "Expenses"], ["balances", "Balances"], ["bills", "Live bills"], ["history", "History"]];

async function renderGroup(tab) {
  const g = state.group;
  const header = h("div", {},
    h("div", { class: "row" },
      h("h1", { class: "grow" }, g.name),
      h("button", { class: "secondary small", onclick: () => openModal(membersBox(g)) }, "Members")),
    h("nav", { class: "tabs" }, TABS.map(([k, label]) =>
      h("a", { href: `#/g/${g.id}/${k}`, class: k === tab ? "on" : "" }, label))));
  const body = h("div", {}, h("div", { class: "empty" }, "Loading…"));
  $app.replaceChildren(header, body);
  const views = { expenses: expensesTab, balances: balancesTab, bills: billsTab, history: historyTab };
  const view = views[tab] || expensesTab;
  const refresh = async (events) => {
    if (events?.some((e) => e.type === "members_changed")) state.group = await api("GET", `/api/groups/${g.id}`);
    body.replaceChildren(...nodes(await view(state.group)));
  };
  state.refresh = guard(refresh);
  await refresh();
}

function membersBox(g) {
  return h("div", {}, h("h2", {}, "Members"),
    g.members.map((m) => h("div", { class: "chip" }, m.username)),
    h("form", { onsubmit: guard(async (e) => {
      e.preventDefault();
      const u = await api("POST", `/api/groups/${g.id}/members`, { username: e.target.uname.value });
      state.group = await api("GET", `/api/groups/${g.id}`);
      toast(`Added ${u.username}`);
      openModal(membersBox(state.group));
    }) },
      h("label", {}, "Add someone by username (they need to sign up first)"),
      h("div", { class: "row" },
        h("input", { name: "uname", required: true, autocapitalize: "none", placeholder: "username" }),
        h("button", { type: "submit" }, "Add"))),
    h("div", { class: "actions" }, h("button", { class: "secondary", onclick: closeModal }, "Done")));
}

async function expensesTab(g) {
  const expenses = await api("GET", `/api/groups/${g.id}/expenses`);
  const fab = h("button", { class: "fab", onclick: () => expenseForm(g) }, "+ Add expense");
  if (!expenses.length) return [h("div", { class: "empty" }, "No expenses yet."), fab];
  return [h("div", { class: "list" }, expenses.map((e) => {
    const mine = e.shares.find((s) => s.user_id === state.me.id)?.amount_cents ?? 0;
    return h("div", { class: "card row", onclick: () => expenseDetail(g, e.expense_id) },
      h("div", { class: "grow" },
        h("strong", {}, e.description || "Expense", e.bill_id ? " 🧾" : ""),
        h("div", { class: "muted small" }, `${name(e.payer_id)} paid ${fmt(e.total_cents)} · ${fmtTime(e.first_created_at)}`
          + (e.version > 1 ? " · changed" : ""))),
      h("div", { style: "text-align:right" }, h("div", { class: "small muted" }, "your share"),
        h("div", { class: "amount" }, fmt(mine))));
  })), fab];
}

async function expenseDetail(g, expenseId) {
  let e;
  try { e = await api("GET", `/api/expenses/${expenseId}`); } catch (err) { toast(err.message, true); return; }
  const live = e.live;
  if (!live) { toast("This expense was deleted. Restore it from History."); return; }
  openModal(
    h("h2", {}, live.description || "Expense"),
    h("p", {}, h("span", { class: "amount" }, fmt(live.total_cents)), ` paid by ${name(live.payer_id)}`,
      live.bill_id ? h("span", { class: "badge", style: "margin-left:8px" }, "live bill") : null),
    live.shares.map((s) => h("div", { class: "row" }, h("span", { class: "grow" }, name(s.user_id)),
      h("span", { class: "amount" }, fmt(s.amount_cents)))),
    h("h2", {}, "Versions"),
    e.versions.slice().reverse().map((v) => h("div", { class: "small muted" },
      `v${v.version} · ${fmtTime(v.created_at)} · ${fmt(v.total_cents)}`,
      v.voided_at ? ` · ${v.void_reason}` : " (current)")),
    h("div", { class: "actions" },
      h("button", { class: "danger", onclick: guard(async () => {
        await api("DELETE", `/api/expenses/${expenseId}?expected_version=${live.version}`);
        closeModal(); toast("Deleted. You can restore it from History."); state.refresh?.();
      }) }, "Delete"),
      !live.bill_id && h("button", { class: "secondary", onclick: () => editForm(g, live) }, "Edit"),
      h("button", { onclick: closeModal }, "Close")));
}

function participantChecks(members, checked) {
  return h("div", { class: "split-grid" }, members.map((m) => h("div", { class: "row" },
    h("span", { class: "grow" }, m.username),
    h("input", { type: "checkbox", name: `p${m.id}`, checked: checked(m.id) }))));
}

function expenseForm(g) {
  const members = g.members;
  const key = uuid(); // one key per form: a retry or double tap can't create two expenses
  let mode = "equal";
  const grid = h("div", { class: "split-grid" });
  const drawGrid = () => {
    if (mode === "equal") { grid.replaceChildren(participantChecks(members, () => true)); return; }
    grid.replaceChildren(...members.map((m) => h("div", { class: "row" }, h("span", { class: "grow" }, m.username),
      mode === "exact"
        ? h("input", { type: "text", inputMode: "decimal", name: `p${m.id}`, placeholder: "0.00" })
        : h("input", { type: "number", min: 0, step: 1, name: `p${m.id}`, placeholder: "0", value: 1 }))));
  };
  drawGrid();

  openModal(h("form", { onsubmit: guard(async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    const total = parseCents(f.amount.value);
    if (!total) throw new Error("Enter an amount like 42.50");
    const body = { description: f.description.value, total_cents: total, payer_id: Number(f.payer.value),
      split_mode: mode, idempotency_key: key };
    if (mode === "equal") {
      body.participants = members.filter((m) => f[`p${m.id}`].checked).map((m) => m.id);
      if (!body.participants.length) throw new Error("Pick at least one person");
    } else if (mode === "exact") {
      body.amounts = {};
      for (const m of members) {
        const v = f[`p${m.id}`].value.trim();
        if (!v) continue;
        const c = parseCents(v);
        if (c == null) throw new Error(`Bad amount for ${m.username}`);
        body.amounts[m.id] = c;
      }
      const sum = Object.values(body.amounts).reduce((a, b) => a + b, 0);
      if (sum !== total) throw new Error(`Amounts add up to ${fmt(sum)}, not ${fmt(total)}`);
    } else {
      body.shares = {};
      for (const m of members) { const w = parseInt(f[`p${m.id}`].value, 10); if (w > 0) body.shares[m.id] = w; }
    }
    await api("POST", `/api/groups/${g.id}/expenses`, body);
    closeModal(); toast("Added"); state.refresh?.();
  }) },
    h("h2", {}, "Add expense"),
    h("label", {}, "Description"), h("input", { name: "description", required: true, maxLength: 200 }),
    h("label", {}, "Amount"), h("input", { name: "amount", inputMode: "decimal", required: true, placeholder: "0.00" }),
    h("label", {}, "Paid by"), h("select", { name: "payer" }, members.map((m) =>
      h("option", { value: m.id, selected: m.id === state.me.id }, m.username))),
    h("label", {}, "Split"),
    h("select", { name: "mode", onchange: (e) => { mode = e.target.value; drawGrid(); } },
      [["equal", "Equally"], ["exact", "Exact amounts"], ["shares", "By shares"]].map(([v, l]) => h("option", { value: v }, l))),
    grid,
    h("div", { class: "actions" }, h("button", { type: "button", class: "secondary", onclick: closeModal }, "Cancel"),
      h("button", { type: "submit" }, "Add"))));
}

function editForm(g, live) {
  // The ledger's update keeps the payer and splits equally.
  const inSplit = new Set(live.shares.map((s) => s.user_id));
  openModal(h("form", { onsubmit: guard(async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    const total = parseCents(f.amount.value);
    if (!total) throw new Error("Enter an amount like 42.50");
    const participants = g.members.filter((m) => f[`p${m.id}`].checked).map((m) => m.id);
    if (!participants.length) throw new Error("Pick at least one person");
    await api("PUT", `/api/expenses/${live.expense_id}`, { expected_version: live.version,
      description: f.description.value, total_cents: total, participants });
    closeModal(); toast("Saved"); state.refresh?.();
  }) },
    h("h2", {}, "Edit expense"),
    h("p", { class: "muted small" }, `Paid by ${name(live.payer_id)}. Edits split the amount equally.`),
    h("label", {}, "Description"), h("input", { name: "description", required: true, maxLength: 200, value: live.description || "" }),
    h("label", {}, "Amount"), h("input", { name: "amount", inputMode: "decimal", required: true, value: centsToInput(live.total_cents) }),
    h("label", {}, "Split equally between"), participantChecks(g.members, (id) => inSplit.has(id)),
    h("div", { class: "actions" }, h("button", { type: "button", class: "secondary", onclick: closeModal }, "Cancel"),
      h("button", { type: "submit" }, "Save"))));
}

async function balancesTab(g, at = null) {
  const q = at ? `?at=${encodeURIComponent(at)}` : "";
  const [bal, plan, pays] = await Promise.all([
    api("GET", `/api/groups/${g.id}/balances${q}`),
    at ? null : api("GET", `/api/groups/${g.id}/settle-up`),
    at ? null : api("GET", `/api/groups/${g.id}/settlements`),
  ]);
  const container = h("div", {});
  const dateInput = h("input", { type: "date", style: "max-width:170px", value: at ? at.slice(0, 10) : "",
    max: new Date().toISOString().slice(0, 10),
    onchange: guard(async (e) => {
      const v = e.target.value;
      const iso = v ? new Date(`${v}T23:59:59`).toISOString() : null;
      container.replaceWith(...nodes(await balancesTab(g, iso)));
    }) });
  container.append(...nodes(
    h("div", { class: "card row" }, h("span", { class: "grow small muted" }, at ? "Balances at end of" : "Balances now. Past date:"),
      dateInput, at && h("button", { class: "link", onclick: () => state.refresh?.() }, "Now")),
    bal.balances.map((b) => h("div", { class: "card row" },
      h("strong", { class: "grow" }, name(b.user_id)),
      netLabel(b.net_cents, b.user_id === state.me.id)))));
  if (at) return container;

  container.append(...nodes(h("h2", {}, "Settle up"),
    plan.length ? h("p", { class: "muted small" }, `${plan.length} payment${plan.length > 1 ? "s" : ""} settles everyone.`)
      : h("p", { class: "muted" }, "Everyone is settled up. 🎉"),
    plan.map((p) => h("div", { class: "card row" },
      h("div", { class: "grow" }, `${name(p.from_user_id)} → ${name(p.to_user_id)}`),
      h("span", { class: "amount" }, fmt(p.amount_cents)),
      h("button", { class: "secondary", onclick: guard(async (ev) => {
        ev.target.disabled = true;
        await api("POST", `/api/groups/${g.id}/settlements`, { from_user_id: p.from_user_id, to_user_id: p.to_user_id,
          amount_cents: p.amount_cents, idempotency_key: uuid() });
        toast("Payment recorded"); state.refresh?.();
      }) }, "Record"))),
    h("div", { class: "actions" }, h("button", { class: "secondary", onclick: () => paymentForm(g) }, "Record another payment"))));

  if (pays.length) container.append(...nodes(h("h2", {}, "Payments"), pays.map((p) => h("div", { class: "card row" },
    h("div", { class: "grow" }, `${name(p.from_user_id)} paid ${name(p.to_user_id)}`, h("div", { class: "muted small" }, fmtTime(p.created_at))),
    h("span", { class: "amount" }, fmt(p.amount_cents))))));
  return container;
}

function paymentForm(g) {
  const key = uuid();
  const sel = (n, def) => h("select", { name: n }, g.members.map((m) => h("option", { value: m.id, selected: m.id === def }, m.username)));
  openModal(h("form", { onsubmit: guard(async (e) => {
    e.preventDefault();
    const f = e.target, amount = parseCents(f.amount.value);
    if (!amount) throw new Error("Enter an amount");
    await api("POST", `/api/groups/${g.id}/settlements`, { from_user_id: Number(f.from.value),
      to_user_id: Number(f.to.value), amount_cents: amount, idempotency_key: key });
    closeModal(); toast("Payment recorded"); state.refresh?.();
  }) }, h("h2", {}, "Record a payment"),
    h("label", {}, "From"), sel("from", state.me.id),
    h("label", {}, "To"), sel("to", g.members.find((m) => m.id !== state.me.id)?.id),
    h("label", {}, "Amount"), h("input", { name: "amount", inputMode: "decimal", required: true, placeholder: "0.00" }),
    h("div", { class: "actions" }, h("button", { type: "button", class: "secondary", onclick: closeModal }, "Cancel"),
      h("button", { type: "submit" }, "Record"))));
}

async function billsTab(g) {
  const list = await api("GET", `/api/groups/${g.id}/bills`);
  const fab = h("button", { class: "fab", onclick: () => billForm(g) }, "+ New live bill");
  if (!list.length) return [h("div", { class: "empty" }, "Enter a receipt once, and everyone claims their own items from their phone."), fab];
  return [h("div", { class: "list" }, list.map((b) => h("div", { class: "card row", onclick: () => (location.hash = `#/b/${b.id}`) },
    h("div", { class: "grow" }, h("strong", {}, `Bill #${b.id}`),
      h("div", { class: "muted small" }, `${b.item_count} items · paid by ${name(b.payer_id)} · ${fmtTime(b.created_at)}`)),
    h("div", { style: "text-align:right" }, h("div", { class: `badge ${b.status === "open" ? "open" : ""}` }, b.status),
      h("div", { class: "amount" }, fmt(b.total_cents)))))), fab];
}

function billForm(g) {
  const rowsBox = h("div", {});
  const addRow = () => rowsBox.append(h("div", { class: "row", style: "margin:6px 0" },
    h("input", { name: "iname", placeholder: "Item", maxLength: 200 }),
    h("input", { name: "iprice", placeholder: "0.00", inputMode: "decimal", style: "max-width:110px" })));
  for (let i = 0; i < 3; i++) addRow();
  openModal(h("form", { onsubmit: guard(async (e) => {
    e.preventDefault();
    const f = e.target;
    const names = [...f.querySelectorAll("[name=iname]")], prices = [...f.querySelectorAll("[name=iprice]")];
    const items = [];
    names.forEach((n, i) => {
      if (!n.value.trim() && !prices[i].value.trim()) return;
      const c = parseCents(prices[i].value);
      if (!c || !n.value.trim()) throw new Error(`Check item ${i + 1}`);
      items.push({ name: n.value.trim(), price_cents: c });
    });
    const tax = parseCents(f.tax.value || "0"), tip = parseCents(f.tip.value || "0");
    if (tax == null || tip == null) throw new Error("Check tax and tip");
    const bill = await api("POST", `/api/groups/${g.id}/bills`, { payer_id: Number(f.payer.value), tax_cents: tax, tip_cents: tip, items });
    closeModal(); location.hash = `#/b/${bill.id}`;
  }) }, h("h2", {}, "New live bill"),
    h("label", {}, "Paid by"), h("select", { name: "payer" }, g.members.map((m) => h("option", { value: m.id, selected: m.id === state.me.id }, m.username))),
    h("label", {}, "Receipt items"), rowsBox,
    h("button", { type: "button", class: "link", onclick: addRow }, "+ another item"),
    h("div", { class: "row" },
      h("div", { class: "grow" }, h("label", {}, "Tax"), h("input", { name: "tax", inputMode: "decimal", placeholder: "0.00" })),
      h("div", { class: "grow" }, h("label", {}, "Tip"), h("input", { name: "tip", inputMode: "decimal", placeholder: "0.00" }))),
    h("div", { class: "actions" }, h("button", { type: "button", class: "secondary", onclick: closeModal }, "Cancel"),
      h("button", { type: "submit" }, "Start claiming"))));
}

async function historyTab(g) {
  const hist = await api("GET", `/api/groups/${g.id}/history`);
  const restore = (eid) => guard(async () => {
    await api("POST", `/api/expenses/${eid}/restore`);
    toast("Restored"); state.refresh?.();
  });
  const out = [];
  if (hist.deleted_expenses.length) {
    out.push(h("h2", {}, "Deleted expenses"), hist.deleted_expenses.map((d) => h("div", { class: "card row" },
      h("div", { class: "grow" }, h("strong", {}, d.description || "Expense"),
        h("div", { class: "muted small" }, `${fmt(d.total_cents)} · deleted ${fmtTime(d.voided_at)}`)),
      h("button", { class: "secondary", onclick: restore(d.expense_id) }, "Restore"))));
  }
  out.push(h("h2", {}, "Every change"));
  if (!hist.events.length) out.push(h("div", { class: "empty" }, "Nothing has happened yet."));
  const css = { created: "", edited: "edit", restored: "restore", deleted: "delete", payment: "" };
  for (const ev of hist.events) {
    const title = ev.kind === "payment"
      ? `${name(ev.from_user_id)} paid ${name(ev.to_user_id)} ${fmt(ev.amount_cents)}`
      : `${ev.description || "Expense"} · ${fmt(ev.total_cents)} · v${ev.version}`;
    out.push(h("div", { class: `event ${css[ev.kind]}` },
      h("div", {}, h("strong", {}, ev.kind === "payment" ? "Payment recorded" : ev.kind[0].toUpperCase() + ev.kind.slice(1))),
      h("div", { class: "small" }, title), h("div", { class: "muted small" }, fmtTime(ev.at))));
  }
  return out;
}

// ------------------------------------------------------------------ live bill

async function renderBill(billId) {
  let bill = await api("GET", `/api/bills/${billId}`);
  await ensureGroup(bill.group_id);
  const g = state.group;
  let claimAs = state.me.id;
  let prevClaims = new Map();

  const draw = () => {
    const open = bill.status === "open";
    const mineFor = (item) => item.claimed_by.includes(claimAs);
    const items = bill.items.map((item) => {
      const changed = prevClaims.has(item.id) && prevClaims.get(item.id) !== item.claimed_by.join(",");
      return h("div", { class: `item ${mineFor(item) ? "mine" : ""} ${item.claimed_by.length ? "" : "unclaimed"} ${changed ? "flash" : ""}`,
        onclick: open ? guard(async () => {
          const path = `/api/bills/${bill.id}/items/${item.id}/claim`;
          bill = mineFor(item)
            ? await api("DELETE", `${path}${claimAs !== state.me.id ? `?for_user=${claimAs}` : ""}`)
            : await api("PUT", path, claimAs !== state.me.id ? { for_user: claimAs } : undefined);
          prevClaims = new Map(); draw();
        }) : null },
        h("div", { class: "check" }, mineFor(item) ? "✓" : ""),
        h("div", { class: "grow" }, h("div", {}, item.name),
          h("div", {}, item.claimed_by.length
            ? item.claimed_by.map((u) => h("span", { class: `chip ${u === state.me.id ? "me" : ""}` }, name(u)))
            : h("span", { class: "small muted" }, "unclaimed"))),
        h("div", { class: "amount" }, fmt(item.price_cents)),
        open && h("button", { class: "link", title: "Remove item", onclick: guard(async (e) => {
          e.stopPropagation();
          if (!confirm(`Remove “${item.name}” from the bill?`)) return;
          bill = await api("DELETE", `/api/bills/${bill.id}/items/${item.id}`); draw();
        }) }, "✕"));
    });
    prevClaims = new Map(bill.items.map((i) => [i.id, i.claimed_by.join(",")]));

    const addForm = open && h("form", { class: "row", style: "margin:8px 0 4px", onsubmit: guard(async (e) => {
      e.preventDefault();
      const c = parseCents(e.target.price.value);
      if (!c) throw new Error("Enter a price");
      bill = await api("POST", `/api/bills/${bill.id}/items`, { name: e.target.iname.value, price_cents: c });
      draw();
    }) }, h("input", { name: "iname", placeholder: "Add an item", required: true, maxLength: 200 }),
      h("input", { name: "price", placeholder: "0.00", inputMode: "decimal", style: "max-width:100px" }),
      h("button", { type: "submit", class: "secondary" }, "Add"));

    const unclaimed = bill.unclaimed_item_ids.length;
    const preview = h("div", { class: "card bill-summary" }, h("strong", {}, open ? "So far" : "Final split"),
      bill.preview.length ? bill.preview.map((p) => h("div", { class: "row small", style: "margin-top:8px" },
        h("div", { class: "grow" }, h("div", { style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }, name(p.user_id)),
          h("div", { class: "muted" }, `${fmt(p.subtotal_cents)} + tax ${fmt(p.tax_cents)} + tip ${fmt(p.tip_cents)}`)),
        h("span", { class: "amount" }, fmt(p.total_cents))))
        : h("div", { class: "muted small" }, "Nobody has claimed anything yet."),
      h("div", { class: "row small", style: "margin-top:10px;border-top:1px solid var(--line);padding-top:8px" },
        h("span", { class: "grow" }, `Subtotal ${fmt(bill.subtotal_cents)} · tax ${fmt(bill.tax_cents)} · tip ${fmt(bill.tip_cents)}`),
        h("strong", { class: "amount" }, fmt(bill.total_cents))));

    $app.replaceChildren(...nodes(
      h("a", { href: `#/g/${g.id}/bills`, class: "small" }, `← ${g.name}`),
      h("div", { class: "row" }, h("h1", { class: "grow" }, `Bill #${bill.id}`),
        h("span", { class: `badge ${open ? "open" : ""}` }, bill.status)),
      h("p", { class: "muted small" }, `${name(bill.payer_id)} paid. `,
        open ? "Tap the items you had. Shared something? Everyone who had it taps it." : ""),
      open && h("div", { class: "card row" }, h("span", { class: "grow small muted" }, "Claiming as"),
        h("select", { style: "max-width:200px", onchange: (e) => { claimAs = Number(e.target.value); draw(); } },
          g.members.map((m) => h("option", { value: m.id, selected: m.id === claimAs }, m.id === state.me.id ? `${m.username} (me)` : m.username)))),
      items, addForm, preview,
      open && h("div", { class: "actions bill-cta" },
        h("button", { disabled: unclaimed > 0 || !bill.items.length, onclick: guard(async () => {
          bill = await api("POST", `/api/bills/${bill.id}/finalize`);
          toast("Added to the ledger"); draw();
        }) }, unclaimed ? `${unclaimed} item${unclaimed > 1 ? "s" : ""} unclaimed` : "Finalize")),
      bill.status === "finalized" && h("p", {}, h("a", { href: `#/g/${g.id}/balances` }, "See balances →"))));
  };

  draw();
  state.refresh = guard(async (events) => {
    if (events && !events.some((e) => e.type !== "bill_updated" || e.bill_id === bill.id)) return;
    if (events?.some((e) => e.type === "members_changed")) state.group = await api("GET", `/api/groups/${g.id}`);
    bill = await api("GET", `/api/bills/${bill.id}`);
    draw();
  });
}

render();
