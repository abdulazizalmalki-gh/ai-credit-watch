"use strict";

const SYMBOLS = { USD: "$", CNY: "¥", EUR: "€", GBP: "£" };
const INTERVAL_KEY = "ai-credit-watch:interval";

const el = {
  cards: document.getElementById("cards"),
  status: document.getElementById("status"),
  updated: document.getElementById("updated"),
  next: document.getElementById("next"),
  refresh: document.getElementById("refresh"),
  interval: document.getElementById("interval"),
  cooldown: document.getElementById("cooldown"),
  missing: document.getElementById("missing"),
  title: document.getElementById("app-title"),
  version: document.getElementById("version"),
  help: document.getElementById("help"),
};

let timer = null;
let nextFetchAt = 0;
let lastUpdatedAt = null;

// Abuse protection mirrors the server: after an upstream refresh the button stays
// disabled until the server would accept another one, and a 429 parks it for
// exactly as long as the server asked.
let inFlight = false;
let cooldownUntil = 0;
let refreshFloorMs = 10000;

function money(amount, currency) {
  if (amount === null || amount === undefined) return "—";
  const symbol = SYMBOLS[currency] || "";
  // A bare count (no currency, e.g. "models visible") reads better without decimals.
  // Money always shows at least cents; small balances get up to four decimals so a
  // $0.114 credit is still legible, but trailing zeros are dropped — "$6.0000" reads
  // like a bug next to "$42.18".
  const maximum = currency ? (Math.abs(amount) < 10 ? 4 : 2) : 0;
  const minimum = currency ? 2 : 0;
  const formatted = Math.abs(amount).toLocaleString(undefined, {
    minimumFractionDigits: minimum,
    maximumFractionDigits: maximum,
  });
  return `${amount < 0 ? "-" : ""}${symbol}${formatted}`;
}

function initials(name) {
  return (name || "?").replace(/[^A-Za-z0-9 ]/g, "").split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
}

function relTime(iso) {
  if (!iso) return "";
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function cooldownLeft() {
  return Math.max(0, cooldownUntil - Date.now());
}

function startCooldown(seconds) {
  const ms = Math.max(0, Number(seconds) || 0) * 1000;
  if (ms > 0) cooldownUntil = Math.max(cooldownUntil, Date.now() + ms);
  syncControls();
}

// Keeps the button and the countdown in step with the cooldown, immediately after
// a response as well as on every tick (so the user never sees a stale button).
function syncControls() {
  const left = cooldownLeft();
  el.refresh.disabled = inFlight || left > 0;
  if (el.cooldown) {
    el.cooldown.textContent = left > 0 ? `retry in ${Math.ceil(left / 1000)}s` : "";
  }
}

function primaryBalance(provider) {
  const balances = provider.balances || [];
  return balances.find((b) => b.primary) || balances.find((b) => b.kind === "balance") || null;
}

function line(label, value, className = "") {
  const li = document.createElement("li");
  const k = document.createElement("span");
  k.className = "k" + (className ? " " + className : "");
  k.textContent = label;
  const v = document.createElement("span");
  v.className = "val";
  v.textContent = value;
  li.append(k, v);
  return li;
}

// The Alibaba console-link flow: start returns a console-login URL that must be
// opened on the machine running this server (the console delivers the token to
// 127.0.0.1 there). We poll /api/link/status until it reports linked.
let linkPoll = null;

function stopLinkPoll() {
  if (linkPoll) clearTimeout(linkPoll);
  linkPoll = null;
}

async function linkStatus() {
  try {
    const response = await fetch("/api/link/status", { cache: "no-store" });
    return response.ok ? await response.json() : { status: "idle" };
  } catch (error) {
    return { status: "idle" };
  }
}

function linkControl(provider) {
  const wrap = document.createElement("div");
  wrap.className = "link-box";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "link-btn";
  const hint = document.createElement("span");
  hint.className = "muted link-hint";
  const alt = document.createElement("button");
  alt.type = "button";
  alt.className = "link-alt";
  alt.textContent = "Browser on another device?";

  if (provider.console_linked) {
    button.textContent = "Reconnect console session";
    hint.textContent = "linked";
  } else {
    button.textContent = "Connect console session";
    hint.textContent = "for Credits usage";
  }

  const startPoll = () => {
    stopLinkPoll();
    const tickLink = async () => {
      const status = await linkStatus();
      if (status.status === "linked") {
        stopLinkPoll();
        load(true);
        return;
      }
      if (status.status !== "waiting") {
        hint.textContent = "link window closed — try again";
        button.disabled = false;
        alt.disabled = false;
        return;
      }
      linkPoll = setTimeout(tickLink, 2000);
    };
    linkPoll = setTimeout(tickLink, 2000);
  };

  button.addEventListener("click", async () => {
    button.disabled = true;
    alt.disabled = true;
    hint.textContent = "starting…";
    try {
      const response = await fetch("/api/link/start", { method: "POST" });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || "HTTP " + response.status);
      hint.textContent = "complete the sign-in in the tab we opened, on THIS machine";
      window.open(body.url, "_blank", "noopener");
      startPoll();
    } catch (error) {
      hint.textContent = String(error.message || error);
      button.disabled = false;
      alt.disabled = false;
    }
  });

  alt.addEventListener("click", async () => {
    button.disabled = true;
    alt.disabled = true;
    hint.textContent = "arming relay link…";
    try {
      const response = await fetch("/api/link/start?relay=true", { method: "POST" });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || "HTTP " + response.status);
      const origin = window.location.origin;
      // Windows: python3 is a Store alias stub and bare `curl` is a
      // PowerShell alias — the working spellings are py/python + curl.exe.
      const win = /Windows/i.test(navigator.userAgent);
      const fetchCmd = win ? "curl.exe" : "curl -s";
      const runCmd = win ? "python" : "python3";
      const command = win
        ? `${fetchCmd} ${origin}/static/link-relay.py -o link-relay.py; ${runCmd} link-relay.py --server ${origin} --code ${body.code}`
        : `${fetchCmd} ${origin}/static/link-relay.py -o link-relay.py && ${runCmd} link-relay.py --server ${origin} --code ${body.code}`;
      hint.innerHTML = "";
      const pre = document.createElement("div");
      pre.className = "link-cmd";
      const code = document.createElement("code");
      code.textContent = command;
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "link-copy";
      copy.textContent = "copy";
      copy.addEventListener("click", () => {
        navigator.clipboard.writeText(command).then(() => { copy.textContent = "copied"; });
      });
      pre.append(code, copy);
      wrap.append(pre);
      hint.textContent = "run that on the computer with your browser — it opens the Qwen sign-in there and finishes automatically";
      startPoll();
    } catch (error) {
      hint.textContent = String(error.message || error);
      button.disabled = false;
      alt.disabled = false;
    }
  });

  wrap.append(button, alt, hint);
  return wrap;
}

function card(provider) {
  const node = document.createElement("article");
  node.className = "card" + (provider.configured ? "" : " unconfigured");

  const head = document.createElement("div");
  head.className = "card-head";
  const left = document.createElement("div");
  left.className = "provider";
  const logo = document.createElement("div");
  logo.className = "logo";
  logo.textContent = initials(provider.name);
  const names = document.createElement("div");
  const pname = document.createElement("div");
  pname.className = "pname";
  pname.textContent = provider.name;
  names.append(pname);
  if (provider.key_hint) {
    const hint = document.createElement("div");
    hint.className = "keyhint";
    hint.textContent = provider.key_hint;
    names.append(hint);
  }
  left.append(logo, names);

  const badge = document.createElement("span");
  if (!provider.configured) {
    badge.className = "badge";
    badge.textContent = "not configured";
  } else if (provider.ok) {
    badge.className = "badge ok";
    badge.textContent = "live";
  } else {
    badge.className = "badge err";
    badge.textContent = "error";
  }
  head.append(left, badge);
  node.append(head);

  if (provider.configured && provider.ok) {
    const primary = primaryBalance(provider);
    if (primary) {
      const amount = document.createElement("div");
      amount.className = "amount";
      amount.textContent = money(primary.amount, primary.currency);
      if (primary.currency) {
        const cur = document.createElement("span");
        cur.className = "cur";
        cur.textContent = primary.currency;
        amount.append(cur);
      }
      const label = document.createElement("div");
      label.className = "amount-label";
      label.textContent = primary.label;
      node.append(amount, label);
    }

    const others = (provider.balances || []).filter((b) => b !== primary);
    if (others.length) {
      const list = document.createElement("ul");
      list.className = "lines";
      others.forEach((b) => list.append(line(b.label, money(b.amount, b.currency))));
      node.append(list);
    }

    const meta = provider.meta || {};
    if (meta.is_free_tier === true) {
      const note = document.createElement("div");
      note.className = "note";
      note.textContent = "Free tier account — no credits purchased yet.";
      node.append(note);
    }
    if (meta.usage_daily !== null && meta.usage_daily !== undefined) {
      const list = document.createElement("ul");
      list.className = "lines";
      list.append(
        line("Usage today", money(meta.usage_daily, "USD")),
        line("This week", money(meta.usage_weekly, "USD")),
        line("This month", money(meta.usage_monthly, "USD"))
      );
      node.append(list);
    }
  }

  if (provider.configured && !provider.ok && provider.error) {
    const error = document.createElement("div");
    error.className = "error";
    error.textContent = provider.error;
    node.append(error);
  }

  if (provider.note) {
    const note = document.createElement("div");
    note.className = "note";
    note.textContent = provider.note;
    node.append(note);
  }

  if (!provider.configured && provider.key_env) {
    const note = document.createElement("div");
    note.className = "note";
    note.append(document.createTextNode("Set "));
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = provider.key_env;
    note.append(chip);
    node.append(note);
  }

  if (provider.linkable) node.append(linkControl(provider));

  const foot = document.createElement("div");
  foot.className = "card-foot";
  const links = document.createElement("span");
  if (!provider.configured && provider.keys_url) {
    const a = document.createElement("a");
    a.href = provider.keys_url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "Get a key ↗";
    links.append(a);
  } else if (provider.signup_url) {
    const a = document.createElement("a");
    a.href = provider.signup_url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "Top up ↗";
    links.append(a);
  }
  const fetched = document.createElement("span");
  fetched.className = "muted";
  fetched.dataset.fetchedAt = provider.fetched_at || "";
  fetched.textContent = provider.fetched_at ? relTime(provider.fetched_at) : "";
  foot.append(links, fetched);
  node.append(foot);

  return node;
}

function setStatus(text, kind) {
  el.status.textContent = text;
  el.status.className = "pill" + (kind ? " " + kind : "");
}

function render(payload) {
  const providers = payload.providers || [];
  const configured = providers.filter((p) => p.configured);
  const missing = providers.filter((p) => !p.configured);

  // Unconfigured providers are hidden unless the server was told to show placeholders.
  const visible = payload.show_unconfigured ? providers : configured;
  el.cards.replaceChildren(...visible.map(card));

  const failing = configured.filter((p) => !p.ok);
  if (!configured.length) {
    setStatus("no keys configured", "warn");
  } else if (failing.length) {
    setStatus(`${failing.length}/${configured.length} providers failing`, "warn");
  } else {
    setStatus(`all ${configured.length} providers ok`, "ok");
  }

  if (missing.length) {
    el.missing.replaceChildren(document.createTextNode("Not configured: "));
    missing.forEach((provider, index) => {
      if (index) el.missing.append(document.createTextNode(" · "));
      el.missing.append(document.createTextNode(`${provider.name} — set `));
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = provider.key_env;
      el.missing.append(chip);
    });
  } else {
    el.missing.replaceChildren();
  }

  const bits = [];
  bits.push(`updated ${relTime(payload.updated_at)}`);
  if (payload.cached) bits.push("cached");
  if (missing.length) bits.push(`${missing.length} not configured`);
  bits.push(`${payload.duration_ms} ms`);
  el.updated.textContent = bits.join(" · ");

  lastUpdatedAt = payload.updated_at;
  el.help.hidden = !(missing.length && !configured.length);
  if (payload.title) {
    el.title.textContent = payload.title;
    document.title = payload.title;
  }
  if (payload.version) el.version.textContent = "v" + payload.version;
}

async function loadConfig() {
  try {
    const response = await fetch("/api/providers", { cache: "no-store" });
    if (!response.ok) return;
    const config = await response.json();
    const seconds = Number(config.refresh_min_interval_seconds);
    if (Number.isFinite(seconds) && seconds > 0) refreshFloorMs = seconds * 1000;
  } catch (error) {
    /* the button still works; the server enforces the real limit */
  }
}

async function load(force) {
  if (inFlight) return; // a second click must not queue another upstream refresh
  if (force && cooldownLeft() > 0) {
    setStatus(`rate limited · retry in ${Math.ceil(cooldownLeft() / 1000)}s`, "warn");
    return;
  }

  inFlight = true;
  el.refresh.disabled = true;
  setStatus(force ? "refreshing…" : "loading…", "");
  try {
    const response = await fetch(`/api/balances${force ? "?refresh=true" : ""}`, { cache: "no-store" });
    if (response.status === 429) {
      const body = await response.json().catch(() => ({}));
      const retryAfter = Number(body.retry_after);
      startCooldown(Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : 15);
      setStatus(`rate limited · retry in ${Math.ceil(cooldownLeft() / 1000)}s`, "warn");
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
    if (force) startCooldown(refreshFloorMs / 1000);
  } catch (error) {
    console.error("ai-credit-watch: refresh failed", error);
    setStatus("request failed", "err");
    el.updated.textContent = String(error.message || error);
  } finally {
    inFlight = false;
    syncControls();
    schedule();
  }
}

function intervalSeconds() {
  return Number(el.interval.value) || 0;
}

function schedule() {
  clearTimeout(timer);
  const seconds = intervalSeconds();
  if (!seconds) {
    nextFetchAt = 0;
    el.next.textContent = "";
    return;
  }
  // Never schedule an auto refresh before the server would accept one.
  const waitMs = Math.max(seconds * 1000, cooldownLeft() + 500);
  nextFetchAt = Date.now() + waitMs;
  timer = setTimeout(() => load(true), waitMs);
}

function tick() {
  const left = cooldownLeft();
  syncControls();

  if (!el.updated.textContent) return;
  const bits = [];
  if (lastUpdatedAt) bits.push(`updated ${relTime(lastUpdatedAt)}`);
  if (nextFetchAt && !left) bits.push(`next in ${Math.max(0, Math.round((nextFetchAt - Date.now()) / 1000))}s`);
  el.updated.textContent = bits.join(" · ");

  document.querySelectorAll("[data-fetched-at]").forEach((span) => {
    if (span.dataset.fetchedAt) span.textContent = relTime(span.dataset.fetchedAt);
  });
}

el.refresh.addEventListener("click", () => load(true));
el.interval.addEventListener("change", () => {
  localStorage.setItem(INTERVAL_KEY, el.interval.value);
  schedule();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "r" && !event.metaKey && !event.ctrlKey && !event.altKey) load(true);
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && intervalSeconds()) load(false);
});

const stored = localStorage.getItem(INTERVAL_KEY);
if (stored !== null) el.interval.value = stored;

setInterval(tick, 1000);
loadConfig().then(() => load(false));
