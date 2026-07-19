"use strict";

// Auth: the daemon hands us a token once, via a URL fragment (#t=...) that
// browsers never send over the network -- see issue #23. bootstrapSession()
// trades it for an HttpOnly cookie the browser then attaches automatically
// to every same-origin request (including EventSource, which can't set
// custom headers), so the token never needs to touch a URL or JS-readable
// storage again.
let authExpired = false;

async function bootstrapSession() {
  const params = new URLSearchParams(location.hash.slice(1));
  const token = params.get("t");
  if (!token) return;
  history.replaceState(null, "", location.pathname + location.search);
  await fetch("/api/session", { method: "POST", headers: { "X-Huginn-Token": token } });
}

function apiFetch(url, opts = {}) {
  return fetch(url, opts).then((r) => {
    if (r.status === 401) {
      if (!authExpired) {
        authExpired = true;
        document.querySelector(".brand").textContent =
          "Huginn — session expired, run `huginn open`";
      }
      throw new Error("unauthorized");
    }
    return r;
  });
}

const sessions = new Map();   // key -> session object
const cards = new Map();      // key -> card element
const grid = document.getElementById("grid");
const tpl = document.getElementById("card-tpl");
let llmEnabled = true;

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
  const summary = card.querySelector(".blurb");
  const usingBlurb = llmEnabled && Boolean(s.blurb);
  summary.textContent = usingBlurb ? s.blurb : (s.last_prompt || "");
  summary.dataset.kind = usingBlurb ? "blurb" : (s.last_prompt ? "prompt" : "");
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
  g.fillStyle = n ? "#b7a0da" : "#9b87d1";
  const bird = new Path2D("M343.313 22.22c-57.33 0-61.26 36.153-91.125 54.874C154.782 42.52 133.115 221.496 169.844 330c-15.396 31.924-30.736 75.9-43.813 134.906 56.828 30.66 119.124 38.655 182.22 9.906-6.2-37.715-14.18-68.858-21.97-95.375 25.025-12.63 59.594-14.573 86.5 14.407.24-28.626-19.022-40.956-40.53-42.25l-22.03-47.313c42.606-45.056 74.38-100.18 57.905-157.06-10.303-38.45 58.203-62.225 122.344-53.75-24.523-21.164-55.99-30.482-85.845-33.876-8.843-21.763-32.616-37.375-61.313-37.375zm10.968 21.936c9.808 0 17.783 7.944 17.783 17.75 0 9.807-7.974 17.75-17.782 17.75-9.807 0-17.75-7.943-17.75-17.75 0-9.806 7.945-17.75 17.75-17.75zm-58.092 274.25 16.28 34.938c-11.62 2.698-22.325 8.217-29.312 15.687-3.298-10.84-6.498-20.903-9.47-30.28a499.965 499.965 0 0 0 22.502-20.344z");
  g.save(); g.scale(0.058, 0.058); g.translate(18, 18); g.fill(bird); g.restore();
  if (n) {
    g.fillStyle = "#100e16";
    g.beginPath(); g.arc(24, 9, 8, 0, Math.PI * 2); g.fill();
    g.fillStyle = "#b7a0da"; g.font = "bold 9px sans-serif";
    g.textAlign = "center"; g.textBaseline = "middle";
    g.fillText(n > 9 ? "9+" : String(n), 24, 9.5);
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

let chatOpen = true;
function openChat(open) {
  chatOpen = open === undefined ? !chatOpen : open;
  document.getElementById("chat").hidden = !chatOpen;
  document.getElementById("chat-toggle").textContent = chatOpen ? "ask ▾" : "ask ▸";
}
document.getElementById("chat-toggle").onclick = () => openChat();

// Chat concurrency is per-daemon (one subprocess in flight at a time), but
// every open tab shares the same SSE stream -- request_id is how a tab
// tells "my answer" apart from another tab's, and drops anything else
// (issue #17). Only one currentRequestId lives per tab, so a late/stale
// event for an older question can't append to a newer one either.
let currentAnswer = null;
let currentRequestId = null;
document.getElementById("chat-form").onsubmit = async (e) => {
  e.preventDefault();
  const input = document.getElementById("chat-input");
  const q = input.value.trim();
  if (!q) return;
  input.value = "";
  addMsg("q", q);
  currentAnswer = addMsg("a", "");
  currentRequestId = null;
  const provider = document.getElementById("provider").value;
  const r = await apiFetch("/api/chat", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question: q, provider }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    if (currentAnswer) {
      currentAnswer.classList.add("err");
      currentAnswer.textContent = body.detail || `chat unavailable (${r.status})`;
    }
    currentAnswer = null;
  } else {
    currentRequestId = body.request_id || null;
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

// ------------------------------------------------------- @name autocomplete

const chatInput = document.getElementById("chat-input");
const mentionMenu = document.getElementById("mention-menu");
let mentionMatches = [];
let mentionIndex = 0;
let mentionRange = null;

function updateMentions() {
  const before = chatInput.value.slice(0, chatInput.selectionStart);
  const match = before.match(/(^|\s)@([^\s@]*)$/);
  if (!match) { closeMentions(); return; }
  const query = match[2].toLowerCase();
  mentionRange = [before.length - query.length - 1, before.length];
  mentionMatches = [...sessions.values()]
    .filter((s) => s.name.toLowerCase().includes(query))
    .sort((a, b) => a.name.localeCompare(b.name))
    .slice(0, 8);
  if (!mentionMatches.length) { closeMentions(); return; }
  mentionIndex = Math.min(mentionIndex, mentionMatches.length - 1);
  mentionMenu.replaceChildren(...mentionMatches.map((s, i) => {
    const option = document.createElement("button");
    option.type = "button";
    option.setAttribute("role", "option");
    option.className = i === mentionIndex ? "selected" : "";
    option.innerHTML = `<span>@${escapeHtml(s.name)}</span><small>${escapeHtml(s.state)}</small>`;
    option.onmousedown = (e) => { e.preventDefault(); chooseMention(i); };
    return option;
  }));
  mentionMenu.hidden = false;
}

function escapeHtml(text) {
  const node = document.createElement("span");
  node.textContent = text;
  return node.innerHTML;
}

function closeMentions() {
  mentionMenu.hidden = true;
  mentionMatches = [];
  mentionRange = null;
  mentionIndex = 0;
}

function chooseMention(index = mentionIndex) {
  if (!mentionRange || !mentionMatches[index]) return;
  const [start, end] = mentionRange;
  const insertion = `@${mentionMatches[index].name} `;
  chatInput.setRangeText(insertion, start, end, "end");
  closeMentions();
  chatInput.focus();
}

chatInput.addEventListener("input", updateMentions);
chatInput.addEventListener("click", updateMentions);
chatInput.addEventListener("keydown", (e) => {
  if (!mentionMenu.hidden) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      mentionIndex = (mentionIndex + (e.key === "ArrowDown" ? 1 : -1) + mentionMatches.length)
        % mentionMatches.length;
      updateMentions();
      return;
    }
    if (e.key === "Tab" || e.key === "Enter") {
      e.preventDefault(); chooseMention(); return;
    }
    if (e.key === "Escape") { e.preventDefault(); closeMentions(); return; }
  }
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    document.getElementById("chat-form").requestSubmit();
  }
});

// ------------------------------------------------------------------ settings

async function loadSettings() {
  const [settingsResponse, providersResponse] = await Promise.all([
    apiFetch("/api/settings"), apiFetch("/api/providers"),
  ]);
  const cfg = await settingsResponse.json();
  const availability = (await providersResponse.json()).providers;
  llmEnabled = cfg.llm.enabled;
  document.getElementById("llm-toggle").checked = llmEnabled;
  const providerSelect = document.getElementById("provider");
  providerSelect.value = cfg.llm.provider;
  for (const option of providerSelect.options) {
    const status = availability[option.value];
    option.disabled = status && !status.available;
    option.title = status?.reason || "installed";
  }
  const selectedStatus = availability[providerSelect.value];
  providerSelect.title = selectedStatus?.reason || "Q&A provider";
}
document.getElementById("llm-toggle").onchange = async (e) => {
  llmEnabled = e.target.checked;
  for (const s of sessions.values()) upsertCard(s);
  const r = await apiFetch("/api/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ llm: { enabled: e.target.checked } }),
  });
  if (!r.ok) {
    llmEnabled = !llmEnabled;
    e.target.checked = llmEnabled;
    for (const s of sessions.values()) upsertCard(s);
  }
};
document.getElementById("provider").onchange = (e) => {
  e.target.title = "Q&A provider";
  return apiFetch("/api/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ llm: { provider: e.target.value } }),
  });
};

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
  // Same-origin EventSource requests send cookies automatically -- no query
  // param needed (a bearer token must never ride in a URL).
  const es = new EventSource("/api/events");
  es.addEventListener("session.upsert", (e) => upsertCard(JSON.parse(e.data)));
  es.addEventListener("session.remove", (e) => removeCard(JSON.parse(e.data).key));
  es.addEventListener("attention.count", (e) => setAttention(JSON.parse(e.data).count));
  es.addEventListener("chat.delta", (e) => {
    const data = JSON.parse(e.data);
    if (currentAnswer && data.request_id === currentRequestId) {
      currentAnswer.textContent += data.text;
      const log = document.getElementById("chat-log");
      log.scrollTop = log.scrollHeight;
    }
  });
  es.addEventListener("chat.done", (e) => {
    if (JSON.parse(e.data).request_id === currentRequestId) currentAnswer = null;
  });
  es.addEventListener("chat.error", (e) => {
    const data = JSON.parse(e.data);
    if (currentAnswer && data.request_id === currentRequestId) {
      currentAnswer.classList.add("err");
      currentAnswer.textContent += `\n[${data.error}]`;
      currentAnswer = null;
    }
  });
  es.onopen = snapshot;   // resync after every (re)connect
}

bootstrapSession().then(() => {
  loadSettings();
  connect();
  setAttention(0);
});
