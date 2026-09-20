#!/usr/bin/env node
// Optional frontend smoke check: runs app/static/app.js against a minimal DOM
// stub and asserts what the cards would display. No browser, no dependencies.
//
//   node dev/render-check.js                  # live API on 127.0.0.1:8760 / :8000
//   node dev/render-check.js payload.json     # or a saved /api/balances response
//   node dev/render-check.js --rate-limited   # asserts the abuse blocker in the UI
//
"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const STATIC = path.join(ROOT, "app", "static");
const ARGS = process.argv.slice(2);
const RATE_LIMITED = ARGS.includes("--rate-limited");
const NONE_CONFIGURED = ARGS.includes("--none-configured");
const URL_FLAG = ARGS.indexOf("--url");
const BASE_URL = URL_FLAG >= 0 ? ARGS[URL_FLAG + 1] : null;
// Skip only the value belonging to --url; a bare positional arg is the payload file.
const CONSUMED = URL_FLAG >= 0 ? [URL_FLAG + 1] : [];
const PAYLOAD_ARG = ARGS.find((arg, index) => !arg.startsWith("--") && !CONSUMED.includes(index));

class TextNode {
  constructor(text) { this.tagName = "#text"; this.className = ""; this.children = []; this._t = String(text); }
  get textContent() { return this._t; }
  set textContent(value) { this._t = String(value); }
}

class Node {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.className = "";
    this.dataset = {};
    this.hidden = false;
    this.disabled = false;
    this.value = "";
  }
  // Same semantics as the DOM: assigning textContent replaces children with one
  // text node, so a later append() lands after that text.
  set textContent(value) { this.children = String(value) === "" ? [] : [new TextNode(value)]; }
  get textContent() { return this.children.map((child) => child.textContent).join(""); }
  append(...nodes) { nodes.forEach((n) => this.children.push(typeof n === "string" ? new TextNode(n) : n)); }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener() {}
  querySelector() { return null; }
}

function dump(node, out = [], depth = 0) {
  if (!node || !node.tagName) return out;
  const cls = node.className ? "." + node.className.split(" ").join(".") : "";
  const text = node.children.length ? "" : node.textContent;
  out.push("  ".repeat(depth) + node.tagName + cls + (text ? " = " + text : ""));
  node.children.forEach((child) => dump(child, out, depth + 1));
  return out;
}

async function loadPayload() {
  if (PAYLOAD_ARG) return JSON.parse(fs.readFileSync(PAYLOAD_ARG, "utf8"));
  const targets = BASE_URL
    ? [BASE_URL]
    : [`http://127.0.0.1:${process.env.PORT || 8760}`, "http://127.0.0.1:8000"];
  const errors = [];
  for (const base of targets) {
    try {
      const response = await fetch(`${base.replace(/\/$/, "")}/api/balances`);
      if (response.ok) return await response.json();
      errors.push(`${base}: HTTP ${response.status}`);
    } catch (error) {
      errors.push(`${base}: ${error.message}`);
    }
  }
  throw new Error(`no running app found (${errors.join(", ")}) — start it, or pass --url <base> / payload.json`);
}

function buildDom() {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  const byId = {};
  for (const match of html.matchAll(/\bid="([^"]+)"/g)) {
    byId[match[1]] = new Node("div");
    byId[match[1]].id = match[1];
  }
  byId.interval.value = "300";

  const document = {
    title: "",
    hidden: false,
    getElementById: (id) => byId[id] || null,
    createElement: (tag) => new Node(tag),
    createTextNode: (text) => new TextNode(text),
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener: () => {},
  };

  const window = {
    document,
    localStorage: { getItem: () => null, setItem: () => {} },
  };

  return { byId, document, window };
}

function makeFetch(payload, config) {
  return async (url) => {
    const target = String(url);
    if (target.includes("/api/providers")) {
      return { ok: true, status: 200, json: async () => config };
    }
    if (RATE_LIMITED) {
      return {
        ok: false,
        status: 429,
        json: async () => ({ detail: "Refresh rate limit reached — try again shortly.", retry_after: 12 }),
      };
    }
    return { ok: true, status: 200, json: async () => payload };
  };
}

function runPage(document, window, fetchStub) {
  const source = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
  const factory = new Function(
    "document", "window", "fetch", "localStorage", "setTimeout", "clearTimeout", "setInterval", "console",
    `${source}\n return { load, render, cooldownLeft };`
  );
  return factory(
    document,
    window,
    fetchStub,
    window.localStorage,
    () => 0, () => {}, () => 0, // timers are inert; rendering is driven manually
    console
  );
}

(async () => {
  let payload = RATE_LIMITED ? { providers: [] } : await loadPayload();
  if (NONE_CONFIGURED) {
    payload = {
      ...payload,
      show_unconfigured: false,
      providers: payload.providers.map((provider) => ({
        ...provider,
        configured: false,
        key_hint: null,
        status: "unconfigured",
        ok: false,
        error: null,
        note: `Not configured — set ${provider.key_env} to include this provider.`,
        balances: [],
        meta: {},
        fetched_at: null,
      })),
    };
  }
  const config = { refresh_min_interval_seconds: 10, refresh_max_per_minute: 20, enabled: true };
  const { byId, document, window } = buildDom();
  const api = runPage(document, window, makeFetch(payload, config));
  await api.load(false);
  await new Promise((resolve) => setTimeout(resolve, 50));

  const modeName = RATE_LIMITED ? "rate limited (429 from the server)" : NONE_CONFIGURED ? "no provider configured" : "normal";
  console.log(`=== mode: ${modeName} ===`);
  console.log("status pill:", byId.status.textContent, "|", byId.status.className);
  console.log("cooldown text:", JSON.stringify(byId.cooldown.textContent));
  console.log("refresh button disabled:", byId.refresh.disabled);
  if (!RATE_LIMITED) {
    console.log("=== rendered cards ===");
    console.log(dump(byId.cards).join("\n"));
  }

  const rendered = byId.cards.textContent;
  const configured = payload.providers.filter((provider) => provider.configured);
  const primary = payload.providers.flatMap((provider) => provider.balances || []).find((b) => b.primary);

  const checks = RATE_LIMITED
    ? [
        ["429 is surfaced to the user", /rate limited/i.test(byId.status.textContent)],
        ["status pill is a warning, not an error", byId.status.className.includes("pill") && byId.status.className.includes("warn")],
        ["refresh button stays disabled during the cooldown", byId.refresh.disabled === true],
        ["a countdown is shown next to the button", /retry in \d+s/.test(byId.cooldown.textContent)],
        ["cooldown matches the server's retry_after", api.cooldownLeft() > 9000],
        ["no cards rendered from a rejected response", byId.cards.children.length === 0],
      ]
    : NONE_CONFIGURED
    ? [
        ["no cards at all", byId.cards.children.length === 0],
        ["status pill says no keys configured", byId.status.textContent === "no keys configured"],
        ["the setup help is shown", byId.help.hidden === false],
        ["every provider's env var is named", payload.providers
          .every((provider) => byId.missing.textContent.includes(provider.key_env))],
        ["the missing line is populated", /Not configured:/.test(byId.missing.textContent)],
        ["the updated line counts them", /\d+ not configured/.test(byId.updated.textContent)],
      ]
    : [
        ["a card per configured provider only", byId.cards.children.length === configured.length],
        ["unconfigured providers get no card (default)",
          payload.show_unconfigured === true || !rendered.includes("not configured")],
        ["primary amount formatted", !primary || rendered.includes(primary.amount.toFixed(2))],
        // Money keeps at least cents and drops padding beyond them: "$6.0000" next to
        // "$42.18" reads like a formatting bug, while a real $0.114 stays visible.
        ["amounts never pad past cents with zeros", !/[0-9]\.[0-9]{2}0{2}/.test(rendered)],
        ["key shown masked only", payload.providers
          .filter((provider) => provider.key_hint)
          .every((provider) => rendered.includes(provider.key_hint))],
        ["no key bytes leaked into the page", !JSON.stringify(payload).match(/\b(?:sk|sk-or)-[A-Za-z0-9]{8,}/)],
        ["the missing line names each unconfigured provider's env var",
          payload.providers
            .filter((provider) => !provider.configured)
            .every((provider) => byId.missing.textContent.includes(provider.key_env))],
        ["status pill reflects health",
          configured.length === 0
            ? byId.status.textContent === "no keys configured"
            : byId.status.textContent.includes(`${configured.length}`) && !byId.status.className.includes("err")],
        ["timestamp line rendered", /updated/.test(byId.updated.textContent)],
        ["button is usable again after a successful load", byId.refresh.disabled === false],
      ];

  let failed = 0;
  console.log("=== assertions ===");
  checks.forEach(([name, ok]) => { console.log(`${ok ? "PASS" : "FAIL"}  ${name}`); if (!ok) failed += 1; });
  if (failed) {
    console.error(`\n${failed} check(s) failed`);
    process.exit(1);
  }
  console.log(`\nall ${checks.length} checks passed`);
})().catch((error) => {
  console.error(String(error.message || error));
  process.exit(1);
});
