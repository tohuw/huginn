"use strict";

// HUGINN_TOKEN is injected server-side into index.html (same-origin bootstrap).
function apiFetch(url, opts = {}) {
  const headers = { ...(opts.headers || {}), "X-Huginn-Token": HUGINN_TOKEN };
  return fetch(url, { ...opts, headers }).then((r) => {
    if (r.status === 401) { location.reload(); throw new Error("stale token"); }
    return r;
  });
}

const sessions = new Map();   // key -> session object
const cards = new Map();      // key -> card element
const grid = document.getElementById("grid");
const tpl = document.getElementById("card-tpl");

// ---------------------------------------------------------------- rendering

function fmtAge(since) {
  const s = Math.max(0, Math.floor(Date.now() / 1000 - since));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, "0")}`;
  return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}`;
}

function fmtSubagents(subagents) {
  if (!subagents) return "";
  const total = Object.values(subagents).reduce((a, b) => a + b, 0);
  const parts = Object.entries(subagents).map(([status, n]) => `${n} ${status}`);
  return `${total} subagent${total === 1 ? "" : "s"}: ${parts.join(", ")}`;
}

const BADGES = {
  working: "working", waiting_input: "input?", waiting_permission: "permit?",
  error: "error", done: "done", idle: "idle", ended: "ended",
};
const SRC_ICON = { claude: "◆", codex: "▲", "claude-desktop": "◇" };

function upsertCard(s) {
  sessions.set(s.key, s);
  let card = cards.get(s.key);
  if (!card) {
    card = tpl.content.firstElementChild.cloneNode(true);
    card.dataset.key = s.key;
    card.querySelector(".jump").onclick = () => jump(s.key);
    card.querySelector(".peek-btn").onclick = () => peek(s.key);
    card.querySelector(".ask").onclick = () => askAbout(s.key);
    cards.set(s.key, card);
  }
  card.dataset.state = s.state;
  card.dataset.since = s.state_since;
  card.querySelector(".src").textContent = SRC_ICON[s.source] || "?";
  card.querySelector(".name").textContent = s.name;
  card.querySelector(".name").title = s.session_id;
  card.querySelector(".badge").textContent = BADGES[s.state] || s.state;
  card.querySelector(".dur").textContent = fmtAge(s.state_since);
  card.querySelector(".cwd").textContent = s.cwd ? s.cwd.replace(/^\/Users\/[^/]+/, "~") : "";
  card.querySelector(".branch").textContent = s.git_branch ? `⎇ ${s.git_branch}` : "";
  card.querySelector(".model").textContent = s.model || "";
  card.querySelector(".blurb").textContent = s.blurb || s.last_prompt || "";
  card.querySelector(".subagents").textContent = fmtSubagents(s.subagents);
  card.querySelector(".tokens").textContent = s.tokens ? `${(s.tokens / 1000).toFixed(0)}k tok` : "";
  reorder();
}

function reorder() {
  const sorted = [...sessions.values()].sort(
    (a, b) => a.rank - b.rank || a.state_since - b.state_since);
  const frag = document.createDocumentFragment();
  for (const s of sorted) frag.appendChild(cards.get(s.key));
  grid.replaceChildren(frag);
  document.getElementById("empty").hidden = sessions.size > 0;
}

function removeCard(key) {
  sessions.delete(key);
  cards.get(key)?.remove();
  cards.delete(key);
  reorder();
}

setInterval(() => {
  for (const card of cards.values()) {
    const since = Number(card.dataset.since);
    if (since) card.querySelector(".dur").textContent = fmtAge(since);
  }
}, 1000);

// ------------------------------------------------------------ attention badge

function setAttention(n) {
  const pill = document.getElementById("attention-pill");
  pill.hidden = n === 0;
  document.getElementById("attention-n").textContent = n;
  document.title = n ? `(${n}) Huginn` : "Huginn";
  const c = document.createElement("canvas");
  c.width = c.height = 32;
  const g = c.getContext("2d");
  g.fillStyle = n ? "#e0af68" : "#565f74";
  g.beginPath(); g.arc(16, 16, 14, 0, 7); g.fill();
  if (n) {
    g.fillStyle = "#14161b";
    g.font = "bold 18px sans-serif";
    g.textAlign = "center"; g.textBaseline = "middle";
    g.fillText(n > 9 ? "9+" : String(n), 16, 17);
  }
  document.getElementById("favicon").href = c.toDataURL("image/png");
}

// ------------------------------------------------------------------- actions

async function jump(key) {
  const r = await apiFetch(`/api/sessions/${encodeURIComponent(key)}/focus`, { method: "POST" });
  if (!r.ok) console.warn("focus failed", await r.text());
}

async function peek(key) {
  const card = cards.get(key);
  const pre = card.querySelector(".peek");
  if (!pre.hidden) { pre.hidden = true; return; }
  const r = await apiFetch(`/api/sessions/${encodeURIComponent(key)}/tail?n=15`);
  const data = await r.json();
  pre.textContent = (data.lines || []).join("\n") || "(no transcript yet)";
  pre.hidden = false;
}

function askAbout(key) {
  const s = sessions.get(key);
  openChat(true);
  const input = document.getElementById("chat-input");
  input.value = `@${s.name} `;
  input.focus();
}

// ---------------------------------------------------------------------- chat

let chatOpen = false;
function openChat(open) {
  chatOpen = open === undefined ? !chatOpen : open;
  document.getElementById("chat").hidden = !chatOpen;
  document.getElementById("chat-toggle").textContent = chatOpen ? "ask ▾" : "ask ▸";
}
document.getElementById("chat-toggle").onclick = () => openChat();

let currentAnswer = null;
document.getElementById("chat-form").onsubmit = async (e) => {
  e.preventDefault();
  const input = document.getElementById("chat-input");
  const q = input.value.trim();
  if (!q) return;
  input.value = "";
  addMsg("q", q);
  currentAnswer = addMsg("a", "");
  const provider = document.getElementById("provider").value;
  const r = await apiFetch("/api/chat", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question: q, provider }),
  });
  if (!r.ok) {
    currentAnswer.classList.add("err");
    currentAnswer.textContent = `chat unavailable (${r.status})`;
    currentAnswer = null;
  }
};

function addMsg(kind, text) {
  const div = document.createElement("div");
  div.className = `msg ${kind}`;
  div.textContent = text;
  const log = document.getElementById("chat-log");
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

document.getElementById("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    document.getElementById("chat-form").requestSubmit();
  }
});

// ------------------------------------------------------------------ settings

async function loadSettings() {
  const r = await apiFetch("/api/settings");
  const cfg = await r.json();
  document.getElementById("llm-toggle").checked = cfg.llm.enabled;
  document.getElementById("provider").value = cfg.llm.provider;
}
document.getElementById("llm-toggle").onchange = (e) =>
  apiFetch("/api/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ llm: { enabled: e.target.checked } }),
  });
document.getElementById("provider").onchange = (e) =>
  apiFetch("/api/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ llm: { provider: e.target.value } }),
  });

// -------------------------------------------------------------------- wiring

async function snapshot() {
  const r = await apiFetch("/api/sessions");
  const data = await r.json();
  const seen = new Set();
  for (const s of data.sessions) { seen.add(s.key); upsertCard(s); }
  for (const key of [...sessions.keys()]) if (!seen.has(key)) removeCard(key);
  setAttention(data.attention);
}

function connect() {
  // EventSource can't set custom headers; the token rides as a query param.
  const es = new EventSource(`/api/events?token=${encodeURIComponent(HUGINN_TOKEN)}`);
  es.addEventListener("session.upsert", (e) => upsertCard(JSON.parse(e.data)));
  es.addEventListener("session.remove", (e) => removeCard(JSON.parse(e.data).key));
  es.addEventListener("attention.count", (e) => setAttention(JSON.parse(e.data).count));
  es.addEventListener("chat.delta", (e) => {
    if (currentAnswer) {
      currentAnswer.textContent += JSON.parse(e.data).text;
      const log = document.getElementById("chat-log");
      log.scrollTop = log.scrollHeight;
    }
  });
  es.addEventListener("chat.done", () => { currentAnswer = null; });
  es.addEventListener("chat.error", (e) => {
    if (currentAnswer) {
      currentAnswer.classList.add("err");
      currentAnswer.textContent += `\n[${JSON.parse(e.data).error}]`;
      currentAnswer = null;
    }
  });
  es.onopen = snapshot;   // resync after every (re)connect
}

loadSettings();
connect();
setAttention(0);
