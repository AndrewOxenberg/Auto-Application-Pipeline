/**
 * Feature 16 tests: the extension must click Apply and must never click Submit.
 *
 * No npm install, matching the rest of the project. The DOM stub below is
 * deliberately small - it implements only the selectors content.js actually
 * uses - and content.js is loaded through `vm` so these tests drive the code
 * that ships rather than a copy of it.
 *
 * Run: node tests/test_apply_click.mjs
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "extension", "content.js"), "utf8");

/** One element. `clicked` records what a test would have pressed. */
let nextId = 0;
function el(tag, { text = "", href = null, type = null, role = null,
                   value = "", ariaLabel = null, hidden = false,
                   disabled = false } = {}) {
  const node = {
    mark: `el${nextId++}`,
    tagName: tag.toUpperCase(),
    textContent: text,
    value,
    type,
    disabled,
    _attrs: { href, role, "aria-label": ariaLabel },
    offsetParent: hidden ? null : {},
    clicked: 0,
    getAttribute(name) {
      return this._attrs[name] ?? null;
    },
    click() {
      this.clicked += 1;
    },
  };
  if (href !== null) node.href = href;
  return node;
}

/** Matches only the selector shapes content.js passes to querySelectorAll. */
function matches(node, selector) {
  const s = selector.trim();
  if (s === "a[href]") return node.tagName === "A" && node.getAttribute("href") !== null;
  if (s === "button") return node.tagName === "BUTTON";
  if (s === '[role="button"]') return node.getAttribute("role") === "button";
  if (s === 'input[type="button"]') return node.tagName === "INPUT" && node.type === "button";
  if (s === "input, textarea, select") {
    return ["INPUT", "TEXTAREA", "SELECT"].includes(node.tagName);
  }
  return false;
}

function load(nodes) {
  const document = {
    body: {},
    readyState: "loading",
    querySelectorAll(selector) {
      const parts = selector.split(",").map((p) => p.trim());
      // "input, textarea, select" is passed as one string by formIsPresent.
      if (parts.length === 3 && parts[0] === "input") {
        return nodes.filter((n) => matches(n, "input, textarea, select"));
      }
      return nodes.filter((n) => parts.some((p) => matches(n, p)));
    },
    getElementById: () => null,
    createElement: () => ({ style: {}, addEventListener() {}, remove() {} }),
    addEventListener() {},
  };
  const window = { addEventListener() {} };
  const sandbox = {
    document,
    window,
    location: { href: "https://boards.greenhouse.io/acme/jobs/123", pathname: "/acme/jobs/123" },
    URL,
    setTimeout,
    clearTimeout,
    MutationObserver: class {
      observe() {}
      disconnect() {}
    },
    chrome: { runtime: { sendMessage: async () => null } },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return window.__jobpipe_apply;
}

/** vm hands back foreign-realm arrays; Array.from makes them native so
 *  assert.deepEqual compares values rather than prototypes. */
const marks = (api) => Array.from(api.findApplyControls(), (e) => e.mark);

let passed = 0;
let failed = 0;
function test(name, fn) {
  try {
    fn();
    passed += 1;
  } catch (e) {
    failed += 1;
    console.error(`FAIL  ${name}\n      ${e.message}`);
  }
}

// -- the safety rule ------------------------------------------------------

test("never offers a Submit Application button", () => {
  const submit = el("button", { text: "Submit Application" });
  const api = load([submit]);
  assert.equal(api.findApplyControls().length, 0);
});

test("never offers Submit even when the label also says apply", () => {
  // The exact label on a filled Greenhouse form.
  const api = load([el("button", { text: "Submit application" }),
                    el("button", { text: "Send Application" }),
                    el("button", { text: "Complete your application" })]);
  assert.equal(api.findApplyControls().length, 0);
});

test("SUBMIT_WORDS covers the destructive verbs", () => {
  const api = load([]);
  for (const word of ["submit", "Send", "finish", "Complete", "confirm"]) {
    assert.ok(api.SUBMIT_WORDS.test(word), `${word} should be excluded`);
  }
});

// -- finding the real control ---------------------------------------------

test("finds Lever's apply link by href", () => {
  const link = el("a", { text: "Apply for this job",
                         href: "https://jobs.lever.co/acme/abc-123/apply" });
  const api = load([link]);
  assert.deepEqual(marks(api), [link.mark]);
});

test("finds Ashby's application route by href", () => {
  const link = el("a", { text: "Apply for this Job",
                         href: "https://jobs.ashbyhq.com/acme/abc/application" });
  const api = load([link]);
  assert.equal(api.findApplyControls().length, 1);
});

test("finds Greenhouse's in-page anchor", () => {
  const link = el("a", { text: "Apply", href: "#app" });
  const api = load([link]);
  assert.equal(api.findApplyControls().length, 1);
});

test("finds a plain Apply button by text", () => {
  const button = el("button", { text: "Apply" });
  const api = load([button]);
  assert.deepEqual(marks(api), [button.mark]);
});

test("href beats text, so the route is tried first", () => {
  const text = el("button", { text: "Apply" });
  const href = el("a", { text: "Continue", href: "/acme/job/apply" });
  const api = load([text, href]);
  assert.deepEqual(marks(api), [href.mark, text.mark]);
});

test("ignores unrelated links", () => {
  const api = load([el("a", { text: "See all jobs", href: "/jobs" }),
                    el("a", { text: "Privacy Policy", href: "/privacy" }),
                    el("button", { text: "Share" })]);
  assert.equal(api.findApplyControls().length, 0);
});

test("ignores a disabled control", () => {
  const api = load([el("button", { text: "Apply", disabled: true })]);
  assert.equal(api.findApplyControls().length, 0);
});

test("ignores a hidden button", () => {
  const api = load([el("button", { text: "Apply", hidden: true })]);
  assert.equal(api.findApplyControls().length, 0);
});

test("tries at most three candidates", () => {
  const api = load(Array.from({ length: 9 }, () => el("button", { text: "Apply" })));
  assert.equal(api.findApplyControls().length, 3);
});

test("does not match Apply inside a longer sentence", () => {
  // "Apply by December 1st" is prose, not a control.
  const api = load([el("a", { text: "Apply by December 1st to be considered",
                              href: "/info" })]);
  assert.equal(api.findApplyControls().length, 0);
});

// -- formIsPresent, the guard that makes the click safe --------------------

test("a description page has no form", () => {
  const api = load([el("input", { type: "email" })]);
  assert.equal(api.formIsPresent(), false);
});

test("an application form is detected", () => {
  const nodes = ["text", "email", "tel", "text", "text"].map((t) => el("input", { type: t }));
  const api = load(nodes);
  assert.equal(api.formIsPresent(), true);
});

test("a file input plus two fields counts as a form", () => {
  const api = load([el("input", { type: "file" }), el("input", { type: "text" }),
                    el("input", { type: "email" })]);
  assert.equal(api.formIsPresent(), true);
});

// -- sameJob, unchanged but load-bearing for the click ---------------------

test("matches a description page against its apply route", () => {
  const api = load([]);
  assert.ok(api.sameJob("https://jobs.lever.co/acme/abc-123",
                        "https://jobs.lever.co/acme/abc-123/apply"));
});

test("matches across the greenhouse host split", () => {
  const api = load([]);
  assert.ok(api.sameJob("https://boards.greenhouse.io/acme/jobs/123",
                        "https://job-boards.greenhouse.io/acme/jobs/123"));
});

test("does not match a different job", () => {
  const api = load([]);
  assert.equal(api.sameJob("https://jobs.lever.co/acme/abc-123",
                           "https://jobs.lever.co/acme/zzz-999"), false);
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
