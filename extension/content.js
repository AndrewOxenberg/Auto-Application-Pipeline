// Handshake, waiting, and the banner. All the filling lives in fill-engine.js
// so the debug harness can exercise exactly the code that ships.
//
// It NEVER submits.

(() => {
  "use strict";

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /**
   * Does this page belong to the job we parked?
   *
   * Most links land on the job description, not the form: Lever's form is at
   * /apply, Ashby's at /application, and Greenhouse renders it further down the
   * same page. Comparing full URLs meant the extension gave up on every one of
   * those. Compare the job's identity instead — host plus the id in the path.
   */
  function sameJob(a, b) {
    try {
      const ua = new URL(a);
      const ub = new URL(b);
      if (ua.host.replace(/^job-boards\./, "boards.") !==
          ub.host.replace(/^job-boards\./, "boards.")) return false;

      const strip = (p) =>
        p.replace(/\/(apply|application)\/?$/i, "").replace(/\/+$/, "");
      if (strip(ua.pathname) === strip(ub.pathname)) return true;

      // Greenhouse also identifies the posting with gh_jid in the query.
      const idOf = (u) => {
        const q = u.searchParams.get("gh_jid");
        if (q) return q;
        const m = strip(u.pathname).match(/([0-9a-f-]{6,})$/i);
        return m ? m[1] : null;
      };
      const ia = idOf(ua);
      const ib = idOf(ub);
      return Boolean(ia && ib && ia === ib);
    } catch {
      return false;
    }
  }

  /** Real, fillable inputs — not the newsletter box in the footer. */
  function formIsPresent() {
    const inputs = Array.from(
      document.querySelectorAll("input, textarea, select")
    ).filter(
      (el) =>
        !["hidden", "submit", "button", "image", "reset"].includes(el.type) &&
        !/recaptcha|captcha/i.test(`${el.name || ""} ${el.id || ""}`) &&
        (el.type === "file" || el.offsetParent !== null)
    );
    const hasFile = inputs.some((el) => el.type === "file");
    return inputs.length >= 5 || (hasFile && inputs.length >= 3);
  }

  // Feature 16. An Apply control and a Submit control are both "the primary
  // button on the page", and this tool's one absolute rule is that it never
  // submits. Anything naming a submit action is excluded by name even when it
  // also says apply: "Submit application" IS the submit button on a Greenhouse
  // form, and a text match alone would happily press it.
  const SUBMIT_WORDS = /submit|send|finish|complete|confirm|save and continue/i;
  const APPLY_TEXT = /^\s*(apply|apply now|apply for this job|apply to this job|apply here|start( your)? application)\s*$/i;

  /**
   * The Apply control on a job-description page, or null.
   *
   * Href shape beats text. A link whose path ends in /apply or /application is
   * unambiguous; text matching is the fallback for Greenhouse, which renders a
   * button and an in-page anchor rather than a route.
   */
  function findApplyControls() {
    const clickable = Array.from(
      document.querySelectorAll('a[href], button, [role="button"], input[type="button"]')
    ).filter((el) => {
      if (el.disabled) return false;
      if (el.offsetParent === null && el.tagName !== "A") return false;
      const label = `${el.textContent || ""} ${el.value || ""} ${el.getAttribute("aria-label") || ""}`;
      return !SUBMIT_WORDS.test(label);
    });

    const byHref = [];
    const byText = [];
    for (const el of clickable) {
      const href = el.getAttribute("href") || "";
      const label = (el.textContent || el.value || el.getAttribute("aria-label") || "").trim();
      if (/\/(apply|application)\/?(\?|#|$)/i.test(href) || href === "#app") {
        byHref.push(el);
      } else if (APPLY_TEXT.test(label)) {
        byText.push(el);
      }
    }
    return [...byHref, ...byText].slice(0, 3);
  }

  /**
   * Click toward the form. Returns true if a form appeared.
   *
   * Guarded by formIsPresent(): if the application is already on the page there
   * is nothing to click toward, and this returns without touching anything.
   * That guard is what makes it structurally unable to press Submit.
   */
  async function clickApply() {
    if (formIsPresent()) return true;
    for (const el of findApplyControls()) {
      el.click();
      for (let waited = 0; waited < 2500; waited += 250) {
        await sleep(250);
        if (formIsPresent()) return true;
      }
      // A route change may still be rendering; the observer below catches it.
      if (location.pathname !== new URL(el.href || location.href, location.href).pathname) {
        return false;
      }
    }
    return false;
  }

  /**
   * Wait for the application form to exist.
   *
   * The old version ran once on load and stopped. On a job-description page
   * that meant nothing ever happened, even after the user clicked Apply and
   * the form appeared. Watch instead, for a bounded time.
   */
  function waitForForm(timeoutMs = 90000) {
    return new Promise((resolve) => {
      if (formIsPresent()) return resolve(true);

      let settled = false;
      const finish = (ok) => {
        if (settled) return;
        settled = true;
        observer.disconnect();
        clearTimeout(timer);
        resolve(ok);
      };

      const observer = new MutationObserver(() => {
        if (formIsPresent()) {
          // Let the rest of the form finish rendering before reading it.
          setTimeout(() => finish(true), 600);
        }
      });
      observer.observe(document.body, { childList: true, subtree: true });
      const timer = setTimeout(() => finish(false), timeoutMs);
    });
  }

  function banner(packet, report) {
    document.getElementById("jobpipe-banner")?.remove();
    const el = document.createElement("div");
    el.id = "jobpipe-banner";

    const blanks = packet.fields.filter((f) => f.required && f.status !== "filled");
    const notPlaced = report.missed.filter((m) => m.includes("no control"));

    el.innerHTML = `
      <div class="jp-row">
        <strong>Filled ${report.filled.length} field${
          report.filled.length === 1 ? "" : "s"}${report.resume ? ", resume attached" : ""}.</strong>
        <span>Nothing was submitted.</span>
        <button id="jp-close" aria-label="Dismiss">×</button>
      </div>
      ${!report.resume ? `<div class="jp-note">Resume not attached — attach it
        yourself before submitting.</div>` : ""}
      ${blanks.length ? `<div class="jp-note">Required and still blank:
        ${blanks.map((b) => `<em>${b.label}</em>`).join(", ")}</div>` : ""}
      ${report.skipped.length ? `<div class="jp-note jp-dim">Deliberately skipped:
        ${report.skipped.join(", ")}</div>` : ""}
      ${notPlaced.length ? `<div class="jp-note jp-dim">Had an answer, no field here:
        ${notPlaced.slice(0, 6).map((m) => m.replace(/ \(no control.*/, "")).join(", ")}</div>` : ""}`;

    document.body.appendChild(el);
    document.getElementById("jp-close")?.addEventListener("click", () => el.remove());
  }

  function waitingNotice() {
    if (document.getElementById("jobpipe-banner")) return;
    const el = document.createElement("div");
    el.id = "jobpipe-banner";
    el.innerHTML = `<div class="jp-row">
      <strong>Job Pipeline is ready.</strong>
      <span>Click Apply and the form will fill itself.</span>
      <button id="jp-close" aria-label="Dismiss">×</button></div>`;
    document.body.appendChild(el);
    document.getElementById("jp-close")?.addEventListener("click", () => el.remove());
  }

  async function getResume() {
    const res = await chrome.runtime
      .sendMessage({ type: "getResume" })
      .catch(() => null);
    if (!res?.ok || !res.b64) return { bytes: null, name: "resume.pdf" };
    const bin = atob(res.b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return { bytes, name: res.filename || "resume.pdf" };
  }

  async function start() {
    const reply = await chrome.runtime
      .sendMessage({ type: "getPending" })
      .catch(() => null);
    if (!reply?.ok || !reply.data?.pending) return;

    const packet = reply.data;
    if (!sameJob(packet.apply_url, location.href)) return;

    // Fetch the resume while waiting; the form often appears first.
    const resumePromise = getResume();

    // Feature 16: click Apply rather than asking for it. The banner is still
    // the fallback, for a page whose Apply control we cannot identify.
    if (!formIsPresent()) {
      const opened = await clickApply();
      if (!opened && !formIsPresent()) waitingNotice();
    }
    const ready = await waitForForm();
    if (!ready) return;

    const { bytes, name } = await resumePromise;
    const report = await window.__jobpipe.fill(packet, {
      resumeBytes: bytes,
      resumeName: name,
    });
    banner(packet, report);
    chrome.runtime.sendMessage({ type: "clearPending" }).catch(() => {});
  }

  // Exposed for the same reason fill-engine.js exposes window.__jobpipe: so a
  // test drives exactly the code that ships, rather than a copy of it.
  window.__jobpipe_apply = { findApplyControls, formIsPresent, sameJob,
                             SUBMIT_WORDS, APPLY_TEXT };

  if (document.readyState === "complete") start();
  else window.addEventListener("load", start, { once: true });
})();
