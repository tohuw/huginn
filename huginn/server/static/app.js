"use strict";

// Auth: the daemon hands us a token once, via a URL fragment (#t=...) that
// browsers never send over the network -- see issue #23. bootstrapSession()
// trades it for an HttpOnly cookie the browser then attaches automatically
// to every same-origin request (including EventSource, which can't set
// custom headers), so the token never needs to touch a URL or JS-readable
// storage again.
let refreshInFlight = null;
const DEMO_MODE = new URLSearchParams(location.search).get("demo") === "1";
const DEMO_TOUR = DEMO_MODE && new URLSearchParams(location.search).get("tour") === "1";

async function bootstrapSession() {
  const params = new URLSearchParams(location.hash.slice(1));
  const token = params.get("t");
  if (!token) return;
  history.replaceState(null, "", location.pathname + location.search);
  await fetch("/api/session", { method: "POST", headers: { "X-Huginn-Token": token } });
}

async function refreshSession() {
  if (!refreshInFlight) {
    refreshInFlight = fetch("/api/session/refresh", { method: "POST" })
      .then((r) => r.ok)
      .finally(() => { refreshInFlight = null; });
  }
  return refreshInFlight;
}

async function apiFetch(url, opts = {}) {
  let r = await fetch(url, opts);
  if (r.status === 401 && await refreshSession()) r = await fetch(url, opts);
  if (r.status === 401) throw new Error("unauthorized");
  return r;
}

const sessions = new Map();   // key -> session object
const cards = new Map();      // key -> card element
const grid = document.getElementById("grid");
const appGrid = document.getElementById("app-grid");
const appTiles = document.getElementById("app-tiles");
const tpl = document.getElementById("card-tpl");
let llmEnabled = true;
let desktopVisible = true;
// Startup and daemon restarts are indeterminate until both the roster and the
// cheap activity probe answer.  Begin with the honest state instead of briefly
// claiming there are no sessions.
let rosterLoading = true;
const PROVIDER_KEY = "huginn.provider";
const providerSelect = document.getElementById("provider");
function getRememberedProvider() {
  if (DEMO_MODE) return null;
  try { return localStorage.getItem(PROVIDER_KEY); }
  catch (_) { return null; }
}
function rememberProvider(value) {
  if (DEMO_MODE) return;
  try {
    if (value) localStorage.setItem(PROVIDER_KEY, value);
    else localStorage.removeItem(PROVIDER_KEY);
  } catch (_) { /* server config remains the fallback */ }
}
const rememberedProvider = getRememberedProvider();
if ([...providerSelect.options].some((o) => o.value === rememberedProvider)) {
  providerSelect.value = rememberedProvider;
}

function demoSessions() {
  const now = Date.now() / 1000;
  const base = {
    attention: false, git_branch: "main", shells: 0, subagents: null,
    title_origin: "guessed", transcript_path: null, version: "",
  };
  return [
    { ...base, key: "demo:claude-atlas", source: "claude", session_id: "atlas",
      cwd: "/Users/demo/Projects/atlas", name: "atlas-7c2a", entrypoint: "cli",
      state: "working", rank: 4, state_since: now - 263, last_activity: now - 4,
      model: "claude-opus-4-8", title: "Refactor Search Pipeline",
      blurb: "Splitting the indexer into resumable stages and running regression tests.",
      tokens: 184000, shells: 2, subagents: { running: 2, done: 1 } },
    { ...base, key: "demo:codex-lantern", source: "codex", session_id: "lantern",
      cwd: "/Users/demo/Projects/lantern", name: "lantern-b19f", entrypoint: "cli",
      state: "waiting_permission", rank: 0, attention: true,
      state_since: now - 38, last_activity: now - 38, model: "gpt-5.6-codex",
      title: "Ship Database Migration", blurb: "Ready to apply the migration after approval.",
      tokens: 92000 },
    { ...base, key: "demo:claude-orbit", source: "claude", session_id: "orbit",
      cwd: "/Users/demo/Projects/orbit", name: "orbit-42e1", entrypoint: "cli",
      state: "waiting_input", rank: 1, attention: true,
      state_since: now - 74, last_activity: now - 74, model: "claude-sonnet-4-6",
      title: "Design Import Workflow", blurb: "Needs a choice between streaming and batch imports.",
      tokens: 61000, subagents: { done: 3 } },
    { ...base, key: "demo:codex-canopy", source: "codex", session_id: "canopy",
      cwd: "/Users/demo/Projects/canopy", name: "canopy-91ad", entrypoint: "cli",
      state: "working", rank: 4, state_since: now - 511, last_activity: now - 2,
      model: "gpt-5.6-codex", git_branch: "feature/maps", title: "Build Offline Maps",
      blurb: "Implementing tile caching and testing interrupted downloads.",
      tokens: 237000, shells: 1, subagents: { running: 1, done: 4 } },
    { ...base, key: "demo:claude-ember", source: "claude", session_id: "ember",
      cwd: "/Users/demo/Projects/ember", name: "ember-d033", entrypoint: "cli",
      state: "error", rank: 2, attention: true, state_since: now - 129,
      last_activity: now - 129, model: "claude-opus-4-8", git_branch: "fix/ci",
      title: "Repair Release Build", blurb: "Build failed in the Windows packaging job.",
      tokens: 128000 },
    { ...base, key: "demo:codex-sonata", source: "codex", session_id: "sonata",
      cwd: "/Users/demo/Projects/sonata", name: "sonata-e510", entrypoint: "cli",
      state: "done", rank: 3, state_since: now - 92, last_activity: now - 92,
      model: "gpt-5.6-codex", title: "Polish Audio Timeline",
      blurb: "Timeline snapping and keyboard navigation are complete.", tokens: 74000 },
    { ...base, key: "demo:claude-harbor", source: "claude", session_id: "harbor",
      cwd: "/Users/demo/Projects/harbor", name: "harbor-66be", entrypoint: "cli",
      state: "idle", rank: 5, state_since: now - 846, last_activity: now - 846,
      model: "claude-sonnet-4-6", title: "Audit API Contracts",
      blurb: "Waiting after documenting compatibility gaps.", tokens: 43000 },
    { ...base, key: "demo:claude-app", source: "claude-desktop", session_id: "desktop",
      cwd: "", name: "Claude.app", entrypoint: "desktop", state: "active", rank: 7,
      state_since: now - 19, last_activity: now - 19, model: "",
      title: "", title_origin: null, blurb: "" },
    { ...base, key: "demo:chatgpt-app", source: "chatgpt-desktop", session_id: "desktop",
      cwd: "", name: "ChatGPT.app", entrypoint: "desktop", state: "idle", rank: 5,
      state_since: now - 307, last_activity: now - 307, model: "",
      title: "", title_origin: null, blurb: "" },
  ];
}

const DEMO_TRANSCRIPTS = {
  "demo:claude-atlas": [
    "user: Split the indexer so interrupted runs can resume safely.",
    "assistant: I’ll map the current checkpoint boundaries first.",
    "assistant to=functions.exec_command: rg -n checkpoint src tests",
    "tool: Process exited with code 0",
    "assistant: The write phase is already idempotent; parsing needs a durable cursor.",
    "assistant to=functions.exec_command: pytest -q tests/test_indexer.py",
    "tool: 28 passed in 1.14s",
    "assistant: Delegating crash-recovery coverage while I extract the stages.",
    "assistant: Two subagents running; refactor in progress.",
  ],
  "demo:codex-lantern": [
    "user: Prepare the production database migration.",
    "assistant: Migration is generated and the rollback path is verified.",
    "assistant to=functions.exec_command: uv run alembic upgrade head",
    "tool: You have 1,284 weighted tokens left",
    "assistant: This command changes the shared database and requires approval.",
    "assistant: Waiting for permission to apply revision 9f31_add_delivery_state.",
  ],
  "demo:claude-orbit": [
    "user: Design the customer import workflow.",
    "assistant: Both approaches preserve validation errors and resumability.",
    "assistant: Streaming minimizes memory; batch mode makes review and rollback simpler.",
    "assistant to=request_user_input: Choose streaming or batch imports",
    "tool: Waiting for user response",
  ],
  "demo:codex-canopy": [
    "user: Make map downloads work reliably offline.",
    "assistant: I’m adding a content-addressed tile cache and resumable manifests.",
    "assistant to=functions.exec_command: cargo test tile_cache --all-features",
    "tool: running 41 tests",
    "assistant: One worker is fuzzing interrupted downloads; four earlier tasks are done.",
    "assistant to=functions.exec_command: cargo test interrupted_download",
    "tool: test interrupted_download ... ok",
    "assistant: Integrating eviction metrics now.",
  ],
  "demo:claude-ember": [
    "user: Find out why the release build fails on Windows.",
    "assistant: The wheel builds locally; inspecting the CI-only packaging step.",
    "assistant to=functions.exec_command: gh run view 8841 --log-failed",
    "tool: error MSB3073: command exited with code 1",
    "assistant: The generated path exceeds the legacy Windows path limit.",
    "assistant: Release build stopped; needs a shorter staging directory.",
  ],
  "demo:codex-sonata": [
    "user: Polish snapping and keyboard navigation in the audio timeline.",
    "assistant: Added beat, marker, and clip-edge snapping with visible guides.",
    "assistant to=functions.exec_command: npm test -- timeline",
    "tool: 67 passed, 0 failed",
    "assistant: Keyboard navigation and screen-reader announcements are covered.",
    "assistant: Done. No remaining changes.",
  ],
  "demo:claude-harbor": [
    "user: Audit our public API contracts before the partner launch.",
    "assistant: Compared OpenAPI output against the three supported SDKs.",
    "assistant: Found two nullable-field mismatches and documented both.",
    "assistant to=functions.exec_command: uv run pytest -q tests/contracts",
    "tool: 112 passed in 3.82s",
    "assistant: Audit is complete; waiting for the next task.",
  ],
};

function demoMention(question) {
  const match = question.match(/@([^\s]+)/);
  if (!match) return null;
  const needle = match[1].toLowerCase();
  return [...sessions.values()].find((s) => s.name.toLowerCase() === needle
    || s.name.toLowerCase().startsWith(needle)) || null;
}

function demoAnswer(question) {
  const q = question.toLowerCase();
  const mentioned = demoMention(question);
  if (/hide.*(apps|desktop)|(apps|desktop).*off/.test(q)) {
    desktopVisible = false; document.getElementById("desktop-toggle").checked = false; reorder();
    return "Desktop presence is hidden for this demo.";
  }
  if (/show.*(apps|desktop)|(apps|desktop).*on/.test(q)) {
    desktopVisible = true; document.getElementById("desktop-toggle").checked = true; reorder();
    return "Desktop presence is visible for this demo.";
  }
  const titleMatch = question.match(/(?:set|change)\s+@([^\s]+).*?title\s+(?:to\s+)?[“\"]?(.+?)[”\"]?$/i);
  if (titleMatch) {
    const target = demoMention(`@${titleMatch[1]}`);
    if (!target) return `I can’t find @${titleMatch[1]} in the demo roster.`;
    target.title = titleMatch[2].trim(); target.title_origin = "manual"; upsertCard(target);
    return `Set @${target.name}’s title to “${target.title}”.`;
  }
  if (mentioned) {
    const work = fmtWork(mentioned);
    return `@${mentioned.name} is ${mentioned.state.replaceAll("_", " ")}. `
      + `${mentioned.blurb || mentioned.last_prompt || "No recent summary."}${work ? ` ${work}.` : ""}`;
  }
  if (/need|attention|waiting|block/.test(q)) {
    const names = [...sessions.values()].filter((s) => s.attention).map((s) => `@${s.name}`);
    return `${names.length} sessions need you: ${names.join(", ")}. One needs permission, one needs input, and one hit an error.`;
  }
  if (/working|busy|running/.test(q)) {
    const active = [...sessions.values()].filter((s) => s.state === "working");
    return `${active.length} agents are working: ${active.map((s) => `@${s.name} — ${s.title}`).join("; ")}.`;
  }
  return "This fictional roster has 7 terminal agents: 2 working, 3 needing attention, 1 done, and 1 idle. Try asking “what needs me?”, mentioning @canopy-91ad, or telling me to hide desktop apps.";
}

function startDemoTour() {
  openChat(true);
  const log = document.getElementById("chat-log");
  log.replaceChildren();
  const steps = [
    "Welcome to Huginn. This is a completely fictional roster, so it’s safe to explore.",
    "Each card is a live coding-agent session. State and color show what is working, finished, or needs you; titles and blurbs explain the task at a glance.",
    "Try Peek on any terminal card for its recent transcript, Jump for focus feedback, or the pencil to rename it. The view, sort, blurbs, and apps controls are live too.",
    "I can reason over the whole roster. Ask “what needs me?”, “who is working?”, mention @canopy-91ad, or tell me to hide desktop apps.",
  ];
  steps.forEach((message, index) => {
    setTimeout(() => addMsg("a", message), index * 450);
  });
  setTimeout(() => document.getElementById("chat-input").focus(), steps.length * 450);
}

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

function fmtWork(s) {
  const parts = [];
  const subagents = fmtSubagents(s.subagents);
  if (subagents) parts.push(subagents);
  if (s.shells) parts.push(`${s.shells} shell${s.shells === 1 ? "" : "s"} running`);
  return parts.join(" · ");
}

function compactPath(path) {
  return path.replace(/^\/Users\/[^/]+/, "~").replace(/^[A-Z]:\\Users\\[^\\]+/i, "~");
}

const BADGES = {
  active: "active",
  working: "working", waiting_input: "input?", waiting_permission: "permit?",
  error: "error", done: "done", idle: "idle", ended: "ended",
};
const SOURCE_META = {
  claude: { label: "claude", family: "claude" },
  "claude-desktop": { label: "claude app", family: "claude" },
  codex: { label: "codex", family: "openai" },
  "chatgpt-desktop": { label: "chatgpt", family: "openai" },
};

function upsertCard(s) {
  rosterLoading = false;
  sessions.set(s.key, s);
  let card = cards.get(s.key);
  if (!card) {
    card = tpl.content.firstElementChild.cloneNode(true);
    card.dataset.key = s.key;
    card.querySelector(".jump").onclick = () => jump(s.key);
    card.querySelector(".peek-btn").onclick = () => peek(s.key);
    card.querySelector(".ask").onclick = () => askAbout(s.key);
    card.querySelector(".edit-title").onclick = () => editTitle(s.key);
    cards.set(s.key, card);
  }
  card.dataset.state = s.state;
  card.dataset.appTile = String(s.source.endsWith("-desktop"));
  card.dataset.since = s.state_since;
  const source = SOURCE_META[s.source] || { label: s.source || "unknown", family: "other" };
  card.dataset.sourceFamily = source.family;
  card.querySelector(".src").textContent = source.label;
  card.querySelector(".name").textContent = s.name;
  card.querySelector(".name").title = s.session_id;
  card.querySelector(".badge").textContent = BADGES[s.state] || s.state;
  card.querySelector(".dur").textContent = fmtAge(s.state_since);
  card.querySelector(".cwd").textContent = s.cwd ? compactPath(s.cwd) : "";
  card.querySelector(".branch").textContent = s.git_branch ? `⎇ ${s.git_branch}` : "";
  card.querySelector(".model").textContent = s.model || "";
  const title = card.querySelector(".card-title");
  title.textContent = s.title || "";
  title.dataset.origin = s.title_origin || "";
  const summary = card.querySelector(".blurb");
  const usingBlurb = llmEnabled && Boolean(s.blurb);
  summary.textContent = usingBlurb ? s.blurb : (s.last_prompt || "");
  summary.dataset.kind = usingBlurb ? "blurb" : (s.last_prompt ? "prompt" : "");
  card.querySelector(".subagents").textContent = fmtWork(s);
  card.querySelector(".tokens").textContent = s.tokens ? `${(s.tokens / 1000).toFixed(0)}k tok` : "";
  reorder();
}

async function editTitle(key) {
  const s = sessions.get(key);
  const card = cards.get(key);
  const row = card.querySelector(".card-title-row");
  if (row.querySelector("input")) return;
  const label = row.querySelector(".card-title");
  const button = row.querySelector(".edit-title");
  const input = document.createElement("input");
  input.className = "title-input";
  input.maxLength = 60;
  input.placeholder = "short title";
  input.value = s?.title || "";
  label.hidden = true; button.hidden = true;
  row.prepend(input); input.focus(); input.select();
  let finished = false;
  const finish = async (save) => {
    if (finished) return;
    finished = true;
    input.remove(); label.hidden = false; button.hidden = false;
    if (!save) return;
    if (DEMO_MODE) {
      s.title = input.value.trim() || null;
      s.title_origin = s.title ? "manual" : null;
      upsertCard(s);
      return;
    }
    const r = await apiFetch(`/api/sessions/${encodeURIComponent(key)}/title`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: input.value }),
    });
    if (r.ok) upsertCard(await r.json());
  };
  input.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
  };
  input.onblur = () => finish(true);
}

function reorder() {
  const mode = document.getElementById("sort").value || "state";
  const byName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
  const byClass = (a, b) => Number(a.source.endsWith("-desktop")) - Number(b.source.endsWith("-desktop"));
  const compare = {
    state: (a, b) => byClass(a, b) || a.rank - b.rank || a.state_since - b.state_since || byName(a, b),
    alpha: (a, b) => byClass(a, b) || byName(a, b),
    newest: (a, b) => byClass(a, b) || b.last_activity - a.last_activity || byName(a, b),
    oldest: (a, b) => byClass(a, b) || a.last_activity - b.last_activity || byName(a, b),
  }[mode];
  const sorted = [...sessions.values()].sort(compare);
  const sessionFrag = document.createDocumentFragment();
  const appFrag = document.createDocumentFragment();
  let appCount = 0;
  for (const s of sorted) {
    if (s.source.endsWith("-desktop")) {
      appFrag.appendChild(cards.get(s.key)); appCount += 1;
    } else {
      sessionFrag.appendChild(cards.get(s.key));
    }
  }
  grid.replaceChildren(sessionFrag);
  appGrid.replaceChildren(appFrag);
  appTiles.hidden = !desktopVisible || appCount === 0;
  renderEmpty();
}

function renderEmpty() {
  const empty = document.getElementById("empty");
  empty.hidden = sessions.size > 0;
  document.getElementById("roster-throbber").hidden = !rosterLoading;
  document.getElementById("empty-label").textContent = rosterLoading ? "finding agents" : "no sessions";
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

function setTriage(triage) {
  const banner = document.getElementById("triage");
  const contentions = triage?.contentions || [];
  banner.replaceChildren();
  banner.hidden = contentions.length === 0;
  if (!contentions.length) return;
  const head = document.createElement("div");
  head.className = "triage-head";
  head.textContent = triage.verdict?.headline || "Competing sessions detected";
  banner.appendChild(head);
  for (const item of contentions) {
    const row = document.createElement("div");
    row.className = "triage-item";
    const root = document.createElement("code");
    root.textContent = item.worktree;
    const names = (item.sessions || []).map((session) => `@${session.name}`).join(", ");
    row.append(root, document.createTextNode(` — ${names}`));
    banner.appendChild(row);
  }
}

// ------------------------------------------------------------------- actions

async function jump(key) {
  if (DEMO_MODE) {
    const card = cards.get(key);
    const button = card.querySelector(".jump");
    card.classList.remove("demo-focused");
    void card.offsetWidth;
    card.classList.add("demo-focused");
    button.textContent = "focused";
    setTimeout(() => { button.textContent = "jump"; card.classList.remove("demo-focused"); }, 1200);
    return;
  }
  const r = await apiFetch(`/api/sessions/${encodeURIComponent(key)}/focus`, { method: "POST" });
  if (r.status === 404) {
    // The browser can miss a removal event while EventSource reconnects.
    // A rejected focus is definitive: discard that local card immediately.
    removeCard(key);
    return;
  }
  if (!r.ok) console.warn("focus failed", await r.text());
}

async function peek(key) {
  const card = cards.get(key);
  const pre = card.querySelector(".peek");
  if (!pre.hidden) { pre.hidden = true; return; }
  if (DEMO_MODE) {
    pre.textContent = (DEMO_TRANSCRIPTS[key] || ["(desktop presence has no transcript)"]).join("\n");
    pre.hidden = false;
    return;
  }
  const r = await apiFetch(`/api/sessions/${encodeURIComponent(key)}/tail?n=15`);
  const data = await r.json();
  pre.textContent = (data.lines || []).join("\n") || "(no transcript yet)";
  pre.hidden = false;
}

// Ask-driven equivalents of the jump/peek buttons above -- "jump @name" and
// "peek @name" perform the same action a click would, not just describe it.
function flashFocused(key) {
  const card = cards.get(key);
  if (!card) return;
  card.classList.remove("demo-focused");
  void card.offsetWidth;
  card.classList.add("demo-focused");
  setTimeout(() => card.classList.remove("demo-focused"), 1200);
}

function showPeek({ key, lines }) {
  const card = cards.get(key);
  if (!card) return;
  const pre = card.querySelector(".peek");
  pre.textContent = (lines || []).join("\n") || "(no transcript yet)";
  pre.hidden = false;
}

function askAbout(key) {
  const s = sessions.get(key);
  openChat(true, true);
  const input = document.getElementById("chat-input");
  input.value = `@${s.name} `;
  input.focus();
}

// ---------------------------------------------------------------------- chat

let chatOpen = true;
let chatSpan = "vertical";
const chatPanel = document.getElementById("chat");
const chatSpanButton = document.getElementById("chat-span");
const CHAT_SIZE_KEYS = {
  vertical: "huginn.chat.width",
  horizontal: "huginn.chat.height",
};
async function saveSettings(body) {
  if (DEMO_MODE) return { ok: true };
  return apiFetch("/api/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
function openChat(open, persist = false) {
  chatOpen = open === undefined ? !chatOpen : open;
  document.getElementById("chat").hidden = !chatOpen;
  document.getElementById("chat-toggle").textContent = chatOpen ? "ask ▾" : "ask ▸";
  if (persist) saveSettings({ ui: { chat_open: chatOpen } });
}
document.getElementById("chat-toggle").onclick = () => openChat(undefined, true);
document.getElementById("help").onclick = () => {
  if (DEMO_MODE) {
    startDemoTour();
  } else {
    window.open("/?demo=1&tour=1", "huginn-demo");
  }
};

function storedChatSize(span) {
  if (DEMO_MODE) return null;
  try { return Number.parseInt(localStorage.getItem(CHAT_SIZE_KEYS[span]), 10) || null; }
  catch (_) { return null; }
}
function applyChatSpan(span) {
  chatSpan = span === "horizontal" ? "horizontal" : "vertical";
  document.body.dataset.chatSpan = chatSpan;
  const horizontal = chatSpan === "horizontal";
  const label = horizontal ? "Dock Ask on the right" : "Span Ask across the bottom";
  chatSpanButton.title = label;
  chatSpanButton.setAttribute("aria-label", label);
  const size = storedChatSize(chatSpan);
  if (size) chatPanel.style.setProperty(horizontal ? "--chat-height" : "--chat-width", `${size}px`);
}
chatSpanButton.onclick = async () => {
  const previous = chatSpan;
  const next = chatSpan === "vertical" ? "horizontal" : "vertical";
  applyChatSpan(next);
  const r = await saveSettings({ ui: { chat_span: next } });
  if (!r.ok) applyChatSpan(previous);
};

document.getElementById("chat-resize").addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  e.preventDefault();
  const horizontal = chatSpan === "horizontal";
  const start = horizontal ? e.clientY : e.clientX;
  const initial = horizontal ? chatPanel.getBoundingClientRect().height : chatPanel.getBoundingClientRect().width;
  const handle = e.currentTarget;
  handle.setPointerCapture(e.pointerId);
  document.body.classList.add("chat-resizing");
  const move = (event) => {
    const delta = horizontal ? start - event.clientY : start - event.clientX;
    const max = horizontal ? window.innerHeight * .75 : window.innerWidth * .75;
    const size = Math.round(Math.max(horizontal ? 160 : 260, Math.min(max, initial + delta)));
    chatPanel.style.setProperty(horizontal ? "--chat-height" : "--chat-width", `${size}px`);
  };
  const end = () => {
    handle.removeEventListener("pointermove", move);
    handle.removeEventListener("pointerup", end);
    handle.removeEventListener("pointercancel", end);
    document.body.classList.remove("chat-resizing");
    const size = Math.round(horizontal ? chatPanel.getBoundingClientRect().height : chatPanel.getBoundingClientRect().width);
    if (!DEMO_MODE) {
      try { localStorage.setItem(CHAT_SIZE_KEYS[chatSpan], String(size)); } catch (_) { /* optional */ }
    }
  };
  handle.addEventListener("pointermove", move);
  handle.addEventListener("pointerup", end);
  handle.addEventListener("pointercancel", end);
});

// Chat concurrency is per-daemon (one subprocess in flight at a time), but
// every open tab shares the same SSE stream -- request_id is how a tab
// tells "my answer" apart from another tab's, and drops anything else
// (issue #17). Only one currentRequestId lives per tab, so a late/stale
// event for an older question can't append to a newer one either.
let currentAnswer = null;
let currentRequestId = null;
let chatBusy = false;
function setChatBusy(busy) {
  chatBusy = busy;
  document.querySelector("#chat-form button[type=submit]").disabled = busy;
  document.getElementById("chat-log").setAttribute("aria-busy", String(busy));
  if (!busy) currentAnswer?.classList.remove("thinking");
}
document.getElementById("chat-form").onsubmit = async (e) => {
  e.preventDefault();
  if (chatBusy) return;
  const input = document.getElementById("chat-input");
  const q = input.value.trim();
  if (!q) return;
  input.value = "";
  addMsg("q", q);
  currentAnswer = addMsg("a", "");
  currentAnswer.classList.add("thinking");
  setChatBusy(true);
  currentRequestId = null;
  if (DEMO_MODE) {
    await new Promise((resolve) => setTimeout(resolve, 350));
    setMarkdownText(currentAnswer, demoAnswer(q));
    setChatBusy(false);
    currentAnswer = null;
    return;
  }
  const provider = document.getElementById("provider").value;
  let r;
  try {
    r = await apiFetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, provider }),
    });
  } catch (err) {
    currentAnswer.classList.add("err");
    currentAnswer.textContent = `chat unavailable (${err.message})`;
    setChatBusy(false);
    currentAnswer = null;
    return;
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    if (currentAnswer) {
      currentAnswer.classList.add("err");
      currentAnswer.textContent = body.detail || `chat unavailable (${r.status})`;
    }
    currentAnswer = null;
    setChatBusy(false);
  } else {
    currentRequestId = body.request_id || null;
    if (body.settings) applySettings(body.settings);
  }
};

function addMsg(kind, text) {
  const div = document.createElement("div");
  div.className = `msg ${kind}`;
  if (kind === "a") setMarkdownText(div, text);
  else div.textContent = text;
  const log = document.getElementById("chat-log");
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

// --------------------------------------------------------- safe Markdown DOM

function appendInlineMarkdown(parent, text) {
  let cursor = 0;
  const markers = ["`", "**", "*"];
  while (cursor < text.length) {
    let marker = null;
    let start = text.length;
    for (const candidate of markers) {
      const found = text.indexOf(candidate, cursor);
      if (found !== -1 && found < start) {
        marker = candidate;
        start = found;
      }
    }
    if (!marker) {
      parent.appendChild(document.createTextNode(text.slice(cursor)));
      break;
    }
    if (start > cursor) parent.appendChild(document.createTextNode(text.slice(cursor, start)));
    const end = text.indexOf(marker, start + marker.length);
    if (end === -1) {
      parent.appendChild(document.createTextNode(text.slice(start)));
      break;
    }
    const tag = marker === "`" ? "code" : marker === "**" ? "strong" : "em";
    const element = document.createElement(tag);
    element.textContent = text.slice(start + marker.length, end);
    parent.appendChild(element);
    cursor = end + marker.length;
  }
}

function renderMarkdown(target, raw) {
  target.replaceChildren();
  const lines = String(raw).split("\n");
  let paragraph = [];
  let list = null;
  let codeLines = null;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    const element = document.createElement("p");
    appendInlineMarkdown(element, paragraph.join(" "));
    target.appendChild(element);
    paragraph = [];
  };
  const closeList = () => { list = null; };
  const appendCode = () => {
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    code.textContent = codeLines.join("\n");
    pre.appendChild(code);
    target.appendChild(pre);
    codeLines = null;
  };

  for (const line of lines) {
    if (line.trimStart().startsWith("```")) {
      flushParagraph();
      closeList();
      if (codeLines === null) codeLines = [];
      else appendCode();
      continue;
    }
    if (codeLines !== null) {
      codeLines.push(line);
      continue;
    }
    const trimmed = line.trim();
    if (!trimmed) {
      flushParagraph();
      closeList();
      continue;
    }
    const heading = /^(#{1,3})\s+(.+)$/.exec(trimmed);
    if (heading) {
      flushParagraph();
      closeList();
      const element = document.createElement(`h${heading[1].length}`);
      appendInlineMarkdown(element, heading[2]);
      target.appendChild(element);
      continue;
    }
    const bullet = /^[-*]\s+(.+)$/.exec(trimmed);
    const numbered = /^\d+\.\s+(.+)$/.exec(trimmed);
    if (bullet || numbered) {
      flushParagraph();
      const kind = bullet ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== kind) {
        list = document.createElement(kind);
        target.appendChild(list);
      }
      const item = document.createElement("li");
      appendInlineMarkdown(item, (bullet || numbered)[1]);
      list.appendChild(item);
      continue;
    }
    closeList();
    paragraph.push(trimmed);
  }
  flushParagraph();
  if (codeLines !== null) appendCode();
}

function setMarkdownText(target, text) {
  target.dataset.raw = String(text);
  renderMarkdown(target, target.dataset.raw);
}

function appendMarkdownText(target, text) {
  target.dataset.raw = (target.dataset.raw || "") + String(text);
  renderMarkdown(target, target.dataset.raw);
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
    const name = document.createElement("span");
    name.textContent = `@${s.name}`;
    const state = document.createElement("small");
    state.textContent = s.state;
    option.append(name, state);
    option.onmousedown = (e) => { e.preventDefault(); chooseMention(i); };
    return option;
  }));
  mentionMenu.hidden = false;
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
  for (const [name, status] of Object.entries(availability)) {
    if (![...providerSelect.options].some((option) => option.value === name)) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = status.label || name;
      providerSelect.appendChild(option);
    }
  }
  applySettings(cfg);
  for (const option of providerSelect.options) {
    const status = availability[option.value];
    option.disabled = status && !status.available;
    option.title = status?.reason || "installed";
  }
  const selectedStatus = availability[providerSelect.value];
  providerSelect.title = selectedStatus?.reason || "Q&A provider";
}
function applySettings(cfg) {
  llmEnabled = cfg.llm.enabled;
  document.getElementById("llm-toggle").checked = llmEnabled;
  desktopVisible = cfg.ui.show_desktop !== false;
  document.getElementById("desktop-toggle").checked = desktopVisible;
  providerSelect.value = cfg.llm.provider;
  rememberProvider(cfg.llm.provider);
  const view = cfg.ui.view || "cards";
  document.getElementById("view").value = view;
  grid.dataset.view = view;
  appGrid.dataset.view = view;
  const sort = cfg.ui.sort || "state";
  document.getElementById("sort").value = sort;
  document.getElementById("sort").dataset.saved = sort;
  applyChatSpan(cfg.ui.chat_span || "vertical");
  openChat(cfg.ui.chat_open !== false);
  for (const s of sessions.values()) upsertCard(s);
}
document.getElementById("llm-toggle").onchange = async (e) => {
  llmEnabled = e.target.checked;
  for (const s of sessions.values()) upsertCard(s);
  const r = await saveSettings({ llm: { enabled: e.target.checked } });
  if (!r.ok) {
    llmEnabled = !llmEnabled;
    e.target.checked = llmEnabled;
    for (const s of sessions.values()) upsertCard(s);
  }
};
document.getElementById("desktop-toggle").onchange = async (e) => {
  const previous = desktopVisible;
  desktopVisible = e.target.checked;
  reorder();
  const r = await saveSettings({ ui: { show_desktop: desktopVisible } });
  if (!r.ok) {
    desktopVisible = previous;
    e.target.checked = previous;
    reorder();
  }
};
providerSelect.onchange = async (e) => {
  const previous = getRememberedProvider();
  rememberProvider(e.target.value);
  e.target.title = "Q&A provider";
  const r = await saveSettings({ llm: { provider: e.target.value } });
  if (!r.ok) {
    rememberProvider(previous);
    e.target.value = previous || "claude";
  }
};
document.getElementById("view").onchange = async (e) => {
  const previous = grid.dataset.view || "cards";
  grid.dataset.view = e.target.value;
  appGrid.dataset.view = e.target.value;
  const r = await saveSettings({ ui: { view: e.target.value } });
  if (!r.ok) {
    grid.dataset.view = previous;
    appGrid.dataset.view = previous;
    e.target.value = previous;
  }
};
document.getElementById("sort").onchange = async (e) => {
  const previous = e.target.dataset.saved || "state";
  reorder();
  const r = await saveSettings({ ui: { sort: e.target.value } });
  if (r.ok) {
    e.target.dataset.saved = e.target.value;
  } else {
    e.target.value = previous;
    reorder();
  }
};

// -------------------------------------------------------------------- wiring

let rosterPollTimer = null;
let snapshotInFlight = false;
function pollRosterSoon() {
  clearTimeout(rosterPollTimer);
  rosterPollTimer = setTimeout(snapshot, 750);
}
async function snapshot() {
  if (snapshotInFlight) return;
  snapshotInFlight = true;
  try {
    const r = await apiFetch("/api/sessions");
    const data = await r.json();
    // Snapshots are additive reconciliation. During daemon startup they may be
    // temporarily incomplete, so absence is never evidence that a terminal
    // session ended. Removal requires the source/reducer's explicit SSE event
    // (confirmed dead/missing) or a definitive 404 when the card is used.
    for (const s of data.sessions) upsertCard(s);
    setAttention(data.attention);
    setTriage(data.triage);
    if (!data.sessions.length) {
      const activity = await (await apiFetch("/api/activity")).json();
      rosterLoading = activity.agents_running;
      renderEmpty();
      if (rosterLoading) pollRosterSoon();
    } else {
      rosterLoading = false;
      clearTimeout(rosterPollTimer);
      renderEmpty();
    }
  } catch (error) {
    // The daemon commonly disappears for a moment during a restart. Keep the
    // empty roster visibly alive and retry without requiring a page refresh.
    if (!sessions.size) {
      rosterLoading = true;
      renderEmpty();
    }
    pollRosterSoon();
    console.debug("roster snapshot unavailable; retrying", error);
  } finally {
    snapshotInFlight = false;
  }
}

function connect() {
  // Same-origin EventSource requests send cookies automatically -- no query
  // param needed (a bearer token must never ride in a URL).
  const es = new EventSource("/api/events");
  es.addEventListener("session.upsert", (e) => upsertCard(JSON.parse(e.data)));
  es.addEventListener("session.remove", (e) => removeCard(JSON.parse(e.data).key));
  es.addEventListener("attention.count", (e) => setAttention(JSON.parse(e.data).count));
  es.addEventListener("triage.changed", (e) => setTriage(JSON.parse(e.data)));
  es.addEventListener("settings.changed", (e) => applySettings(JSON.parse(e.data)));
  es.addEventListener("session.focused", (e) => flashFocused(JSON.parse(e.data).key));
  es.addEventListener("session.peek", (e) => showPeek(JSON.parse(e.data)));
  es.addEventListener("chat.delta", (e) => {
    const data = JSON.parse(e.data);
    if (currentAnswer && data.request_id === currentRequestId) {
      appendMarkdownText(currentAnswer, data.text);
      const log = document.getElementById("chat-log");
      log.scrollTop = log.scrollHeight;
    }
  });
  es.addEventListener("chat.done", (e) => {
    if (JSON.parse(e.data).request_id === currentRequestId) {
      setChatBusy(false);
      currentAnswer = null;
    }
  });
  es.addEventListener("chat.error", (e) => {
    const data = JSON.parse(e.data);
    if (currentAnswer && data.request_id === currentRequestId) {
      currentAnswer.classList.add("err");
      currentAnswer.textContent += `\n[${data.error}]`;
      setChatBusy(false);
      currentAnswer = null;
    }
  });
  es.onopen = snapshot;   // resync after every (re)connect
  es.onerror = () => {
    if (!sessions.size) {
      rosterLoading = true;
      renderEmpty();
    }
    pollRosterSoon();
  };
}

if (DEMO_MODE) {
  rosterLoading = false;
  applySettings({
    llm: { enabled: true, provider: "codex" },
    ui: { show_desktop: true, view: "cards", sort: "state", chat_span: "vertical", chat_open: true },
  });
  for (const session of demoSessions()) upsertCard(session);
  if (DEMO_TOUR) startDemoTour();
  else addMsg("a", "Demo roster loaded. Try Peek, Jump, editing a title, or ask what needs your attention.");
  setAttention(3);
} else bootstrapSession().then(() => {
  renderEmpty();
  loadSettings();
  connect();
  // Do not wait for EventSource's first open before attempting the initial
  // roster fetch; this also covers browsers delaying SSE reconnection.
  snapshot();
  // SSE is the fast path; periodic additive reconciliation recovers session
  // upserts that landed while the browser or daemon was reconnecting.
  setInterval(snapshot, 5000);
  setAttention(0);
});
