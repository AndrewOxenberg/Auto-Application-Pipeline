// The fill engine. Shared by the extension's content script and by the debug
// harness that injects it into a live form, so what gets tested is what ships.
//
// It NEVER submits, accepts terms, or creates an account. There is no code path
// here that clicks a submit control.
//
// Exposes window.__jobpipe.fill(packet) -> report

(() => {
  "use strict";

  const SUBMIT = /^(submit|apply|send|continue|next|save)\b/i;

  const norm = (s) =>
    (s || "")
      .replace(/[‘’]/g, "'")
      .replace(/[*✱]/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .toLowerCase();

  const slug = (s) => norm(s).replace(/[^a-z0-9]+/g, "");

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ------------------------------------------------------------ identifying

  /**
   * Greenhouse names its standard inputs (first_name, email, resume) and Lever
   * uses cards[...][field]. The name attribute is a far stronger signal than
   * any label scraped out of the DOM, so it is tried first.
   */
  const NAME_HINTS = [
    [/first[_-]?name|givenname/i, "first name"],
    [/last[_-]?name|familyname|surname/i, "last name"],
    [/preferred[_-]?name/i, "preferred name"],
    [/middle/i, "middle initial"],
    [/^email|[_-]email|email[_-]/i, "email"],
    [/phone|mobile/i, "phone"],
    [/resume|cv$/i, "resume"],
    [/cover[_-]?letter/i, "cover letter"],
    [/linkedin/i, "linkedin"],
    [/github/i, "github"],
    [/portfolio|website|\burl\b/i, "website portfolio"],
    [/school|university|org$/i, "school"],
    [/degree/i, "degree"],
    [/discipline|major/i, "discipline major"],
    [/^location|candidate[_-]?location|city/i, "location city"],
    [/postal|zip/i, "zip postal code"],
    [/^state|region/i, "state"],
    [/country/i, "country"],
    [/start[_-]?date|availability/i, "earliest start date"],
    [/gender/i, "gender"],
    [/hispanic|latino/i, "hispanic latino"],
    [/race|ethnic/i, "race ethnicity"],
    [/veteran/i, "veteran status"],
    [/disability/i, "disability status"],
    [/pronoun/i, "pronouns"],
  ];

  const AUTOCOMPLETE_HINTS = {
    "given-name": "first name",
    "family-name": "last name",
    "additional-name": "middle initial",
    name: "full name",
    email: "email",
    tel: "phone",
    "tel-national": "phone",
    "street-address": "address",
    "address-line1": "address",
    "address-line2": "address line 2",
    "address-level2": "city",
    "address-level1": "state",
    "postal-code": "zip postal code",
    country: "country",
    "country-name": "country",
    organization: "school",
    url: "website portfolio",
  };

  function labelFor(el) {
    const bits = [];

    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab) bits.push(lab.textContent);
    }
    const wrapping = el.closest("label");
    if (wrapping) bits.push(wrapping.textContent);

    const by = el.getAttribute("aria-labelledby");
    if (by) {
      for (const id of by.split(/\s+/)) {
        const n = document.getElementById(id);
        if (n) bits.push(n.textContent);
      }
    }
    if (el.getAttribute("aria-label")) bits.push(el.getAttribute("aria-label"));

    // Radios and checkboxes live in a group; the question is on the legend.
    const fs = el.closest("fieldset");
    if (fs) {
      const legend = fs.querySelector("legend");
      if (legend) bits.push(legend.textContent);
    }

    // Greenhouse and Ashby put the question in a sibling div above the control.
    let node = el.parentElement;
    for (let d = 0; node && d < 5; d++, node = node.parentElement) {
      for (const cand of node.querySelectorAll(
        "label, legend, .label, [class*='label'], [class*='question'], h3, h4, p, strong"
      )) {
        if (cand.contains(el)) continue;
        const t = cand.textContent.trim();
        if (t.length > 1 && t.length < 300) { bits.push(t); break; }
      }
      if (bits.length) break;
    }

    if (el.placeholder) bits.push(el.placeholder);
    return bits.map(norm).filter(Boolean);
  }

  /**
   * Reduce a label to a canonical token when it clearly names a known field.
   *
   * Ashby labels its LinkedIn box "URL (LinkedIn)" and its start date "When are
   * you available to start?". Neither has enough word overlap with "LinkedIn
   * Profile" or "Earliest Start Date" to clear the match threshold, so both
   * went unfilled. Running the page's label AND the answer's label through the
   * same table makes them meet in the middle.
   */
  const LABEL_HINTS = [
    [/linked\s*in/i, "linkedin"],
    [/git\s*hub/i, "github"],
    [/portfolio|personal\s*site|\bwebsite\b/i, "website portfolio"],
    [/cover\s*letter/i, "cover letter"],
    [/resume|\bcv\b|curriculum/i, "resume"],
    [/(available|availability).*(start|begin)|start\s*date|earliest[\s\S]{0,30}(start|begin)|when[\s\S]{0,40}(start|begin)\s*work/i,
     "earliest start date"],
    [/(legally\s*)?authoriz\w*\s*to\s*work|work\s*authoriz|eligible\s*to\s*work/i,
     "authorized to work"],
    [/sponsor/i, "sponsorship"],
    [/how\s*did\s*you\s*(first\s*)?hear|referral\s*source/i, "how did you hear"],
    [/pronoun/i, "pronouns"],
    [/gender/i, "gender"],
    [/hispanic|latino/i, "hispanic latino"],
    [/\brace\b|ethnic/i, "race ethnicity"],
    [/veteran/i, "veteran status"],
    [/disab/i, "disability status"],
    [/willing.*relocat|relocat/i, "willing to relocate"],
    [/willing.*travel|travel/i, "willing to travel"],
    [/\bgpa\b|grade\s*point/i, "gpa"],
    [/school|university|college/i, "school"],
    [/discipline|major|field\s*of\s*study/i, "discipline major"],
    [/degree/i, "degree"],
  ];

  function canon(text) {
    for (const [re, token] of LABEL_HINTS) if (re.test(text)) return token;
    return null;
  }

  /** Every string that might identify this control, best signal first. */
  function identity(el) {
    const out = [];
    const nm = el.name || el.id || "";
    for (const [re, token] of NAME_HINTS) if (re.test(nm)) { out.push(token); break; }
    const ac = (el.getAttribute("autocomplete") || "").toLowerCase();
    if (AUTOCOMPLETE_HINTS[ac]) out.push(AUTOCOMPLETE_HINTS[ac]);
    const labels = labelFor(el);
    for (const l of labels) {
      const c = canon(l);
      if (c && !out.includes(c)) out.push(c);
    }
    out.push(...labels);
    return out;
  }

  // -------------------------------------------------------------- filling

  function setNativeValue(el, value) {
    const proto =
      el instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
    for (const type of ["input", "change"]) {
      el.dispatchEvent(new Event(type, { bubbles: true }));
    }
  }

  function bestOption(want, options) {
    const w = norm(want);
    if (!w || !options.length) return null;
    let hit = options.find((o) => norm(o.text) === w);
    if (hit) return hit;
    hit = options.find((o) => slug(o.text) === slug(w));
    if (hit) return hit;
    hit = options.find((o) => norm(o.text).includes(w) || w.includes(norm(o.text)));
    if (hit) return hit;

    const num = parseFloat(w);
    if (!Number.isNaN(num)) {
      const scored = options
        .map((o) => ({ o, n: parseFloat(o.text) }))
        .filter((x) => !Number.isNaN(x.n));
      if (scored.length) {
        scored.sort((a, b) => Math.abs(a.n - num) - Math.abs(b.n - num));
        return scored[0].o;
      }
    }
    const words = new Set(w.split(/\W+/).filter((x) => x.length > 3));
    let best = null;
    let score = 0;
    for (const o of options) {
      const ow = new Set(norm(o.text).split(/\W+/).filter((x) => x.length > 3));
      let s = 0;
      for (const x of words) if (ow.has(x)) s++;
      if (s > score) { best = o; score = s; }
    }
    return best;
  }

  // The phone widget's country picker is a listbox of every country on earth.
  // Left unfiltered it is the first thing an option search finds, and the
  // school field ends up set to Afghanistan.
  const FOREIGN_MENU = /iti__|country-list|flag/i;

  /**
   * Options belonging to THIS control.
   *
   * Scoping matters twice over. Searching the whole document picks up the
   * phone widget's list of every country on earth — 250-odd nodes — and
   * `offsetParent` forces a layout on each one, which made the page hang long
   * enough to kill the tab. react-select points the input at its own listbox
   * through aria-controls, so ask for that first.
   */
  function visibleOptions(el) {
    const id = el?.getAttribute("aria-controls") || el?.getAttribute("aria-owns");
    let roots = [];
    if (id) {
      const box = document.getElementById(id);
      if (box) roots = [box];
    }
    if (!roots.length) {
      const near = el?.closest("[class*='select'], [class*='autocomplete'], [role='combobox']")
        ?.parentElement;
      roots = Array.from(
        (near || document).querySelectorAll('[role="listbox"], [class*="menu"]')
      ).filter((n) => !FOREIGN_MENU.test(`${n.className || ""} ${n.id || ""}`));
    }
    if (!roots.length) roots = [document];

    const out = [];
    for (const root of roots) {
      for (const n of root.querySelectorAll(
        '[role="option"], [class*="option"]:not([class*="options"]), li'
      )) {
        const text = n.textContent.trim();
        if (!text || text.length > 200) continue;
        const key = `${n.className || ""} ${n.id || ""}`;
        if (FOREIGN_MENU.test(key)) continue;
        if (n.offsetParent === null && root === document) continue;
        out.push({ text, node: n });
        if (out.length > 60) return out;
      }
    }
    return out;
  }

  /**
   * These menus open on real keystrokes, not on a synthetic `input` event.
   * Setting the value in one go leaves the listbox closed and the field
   * reverts on blur — which is most of why fields came back empty.
   */
  async function typeInto(el, text) {
    el.focus();
    el.click();
    setNativeValue(el, "");
    await sleep(40);
    const s = String(text).slice(0, 80);

    // Three real keystrokes are enough to open the menu and start the search;
    // the rest goes in one write. Typing all 36 characters of a school name
    // individually meant 36 React re-renders and took ten seconds per field.
    const seed = Math.min(3, s.length);
    for (let i = 1; i <= seed; i++) {
      setNativeValue(el, s.slice(0, i));
      const ch = s[i - 1];
      el.dispatchEvent(new KeyboardEvent("keydown", { key: ch, bubbles: true }));
      el.dispatchEvent(new KeyboardEvent("keyup", { key: ch, bubbles: true }));
    }
    if (s.length > seed) {
      setNativeValue(el, s);
      const last = s[s.length - 1];
      el.dispatchEvent(new KeyboardEvent("keydown", { key: last, bubbles: true }));
      el.dispatchEvent(new KeyboardEvent("keyup", { key: last, bubbles: true }));
    }
  }

  /**
   * react-select empties its input and shows the chosen label in a sibling
   * container, so an empty input does not mean failure.
   *
   * `closest("[class*='select']")` is a trap: the input's own class is
   * `select__input`, so closest returns the input and its text is always "".
   */
  function committed(el) {
    if (norm(el.value)) return true;
    let node = el.parentElement;
    for (let d = 0; node && d < 5; d++, node = node.parentElement) {
      const cls = String(node.className || "");
      if (/has-value|--has-value/.test(cls)) return true;
      if (/value-container|select__control|control/.test(cls)) {
        const shown = norm(node.textContent);
        if (shown && !/^(select|choose|search)\b/.test(shown)) return true;
      }
    }
    return false;
  }

  function key(el, k, code) {
    for (const type of ["keydown", "keypress", "keyup"]) {
      el.dispatchEvent(
        new KeyboardEvent(type, { key: k, code: code || k, bubbles: true, cancelable: true })
      );
    }
  }

  /**
   * Commit a typeahead selection with the keyboard, not a click.
   *
   * Clicking an option highlights it and looks right, then the value vanishes
   * as soon as focus moves on — which is exactly what was reported for the
   * race and veteran dropdowns. react-select and friends commit on Enter
   * against their internally highlighted option; a synthetic click often
   * never reaches that state, so the widget resets on blur.
   */
  async function selectByKeyboard(el, index) {
    for (let i = 0; i < index; i++) {
      key(el, "ArrowDown", "ArrowDown");
      await sleep(35);
    }
    key(el, "Enter", "Enter");
    await sleep(200);
    return committed(el);
  }

  async function fillCombobox(el, value) {
    const candidates = Array.isArray(value) ? value : [value];
    for (const want of candidates) {
      if (want === null || want === undefined || want === "") continue;
      await typeInto(el, want);

      for (let i = 0; i < 14; i++) {
        await sleep(160);
        const opts = visibleOptions(el);
        if (!opts.length) continue;
        const pick = bestOption(want, opts);
        if (!pick) break;
        const index = opts.indexOf(pick);

        // Keyboard first: it is the path the widget actually listens to.
        if (index >= 0 && (await selectByKeyboard(el, index))) return true;

        // Some plain autocompletes only respond to a click.
        pick.node.scrollIntoView({ block: "nearest" });
        pick.node.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
        pick.node.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
        pick.node.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
        pick.node.click();
        await sleep(200);

        // Prove it survives losing focus. A value that looks right until the
        // next field is worse than a blank one, because it submits empty.
        el.dispatchEvent(new Event("blur", { bubbles: true }));
        await sleep(120);
        if (committed(el)) return true;
      }
      // Clear before trying the next phrasing.
      setNativeValue(el, "");
      key(el, "Escape", "Escape");
      el.blur();
      await sleep(60);
    }
    return false;
  }

  async function fillSelect(el, value) {
    const candidates = Array.isArray(value) ? value : [value];
    const options = Array.from(el.options)
      .filter((o) => o.value !== "" && !/^(select|choose|--)/i.test(o.textContent.trim()))
      .map((o) => ({ text: o.textContent, value: o.value }));
    for (const want of candidates) {
      const pick = bestOption(want, options);
      if (pick) {
        el.value = pick.value;
        el.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
      }
    }
    return false;
  }

  /** The checkbox's or radio's OWN option text, not the group's question. */
  function optionLabel(el) {
    const wrap = el.closest("label");
    if (wrap) {
      const clone = wrap.cloneNode(true);
      for (const inp of clone.querySelectorAll("input, select, textarea")) inp.remove();
      const t = norm(clone.textContent);
      if (t) return t;
    }
    if (el.id) {
      const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab) return norm(lab.textContent);
    }
    if (el.getAttribute("aria-label")) return norm(el.getAttribute("aria-label"));
    const sib = el.nextElementSibling || el.parentElement;
    return norm(sib ? sib.textContent : "").slice(0, 80);
  }

  /**
   * A group of radios or checkboxes is one question with many controls. The
   * first version matched the answer to whichever member came first in the
   * DOM and ticked that, so "he/him" ticked whatever pronoun happened to be
   * at the top of Lever's eleven-checkbox list.
   */
  function fillGroup(members, value) {
    const candidates = (Array.isArray(value) ? value : [value])
      .filter((v) => v !== null && v !== undefined && v !== "")
      .map(String);
    if (!members.length || !candidates.length) return false;

    const options = members.map((el) => ({ text: optionLabel(el), node: el }));
    for (const want of candidates) {
      const pick = bestOption(want, options);
      if (pick && pick.text) {
        if (!pick.node.checked) pick.node.click();
        return true;
      }
    }
    // A single checkbox is a yes/no, whatever its label says. Ashby renders
    // "Are you legally authorized to work in the US?" as one checkbox whose
    // label is the whole question, so option matching never fires.
    if (members.length === 1) {
      const yes = /^(yes|true)/i.test(candidates[0]);
      if (yes && !members[0].checked) members[0].click();
      if (!yes && members[0].checked) members[0].click();
      return true;
    }
    return false;
  }

  /**
   * A content script cannot read the disk, but it CAN build a File from bytes
   * it was handed and assign it through DataTransfer. The bytes come from the
   * local pipeline server, so the resume upload is automatable after all.
   */
  function fillFile(el, bytes, filename) {
    if (!bytes) return false;
    try {
      const array = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
      const file = new File([array], filename || "resume.pdf", {
        type: "application/pdf",
      });
      const dt = new DataTransfer();
      dt.items.add(file);
      el.files = dt.files;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      el.dispatchEvent(new Event("input", { bubbles: true }));
      return el.files.length === 1;
    } catch (err) {
      return false;
    }
  }

  // ----------------------------------------------------------------- match

  function score(ids, fieldLabel, fieldName) {
    const target = norm(fieldLabel);
    const tslug = slug(fieldLabel);
    const tcanon = canon(fieldLabel);
    let best = 0;
    // Canonical hit on both sides is as good as an exact label match.
    if (tcanon && ids.includes(tcanon)) return 92;
    for (const id of ids) {
      if (!id) continue;
      if (id === target) return 100;
      if (slug(id) === tslug) return 95;
      if (fieldName && slug(id) === slug(fieldName)) return 90;
      if (id.startsWith(target) || target.startsWith(id)) best = Math.max(best, 80);
      if (id.includes(target) || target.includes(id)) best = Math.max(best, 70);
      const a = new Set(id.split(/\W+/).filter((x) => x.length > 3));
      const b = new Set(target.split(/\W+/).filter((x) => x.length > 3));
      let overlap = 0;
      for (const x of a) if (b.has(x)) overlap++;
      if (overlap && b.size) {
        best = Math.max(best, Math.round((overlap / b.size) * 60));
      }
    }
    return best;
  }

  // Widget internals that look like fields but are not. Typing into the phone
  // widget's country search or the reCAPTCHA textarea breaks the form.
  const NOT_A_FIELD =
    /recaptcha|captcha|iti-\d|country-?search|__search-input|csrf|authenticity/i;

  function controls() {
    return Array.from(document.querySelectorAll("input, textarea, select")).filter(
      (el) => {
        if (el.disabled || el.readOnly) return false;
        if (["hidden", "submit", "button", "image", "reset"].includes(el.type)) return false;
        if (el.type !== "file" && el.offsetParent === null) return false;
        if (SUBMIT.test(el.value || "") && el.tagName !== "TEXTAREA") return false;

        const key = `${el.name || ""} ${el.id || ""} ${el.className || ""}`;
        if (NOT_A_FIELD.test(key)) return false;
        return true;
      }
    );
  }

  /**
   * One control per widget.
   *
   * react-select renders a nameless inner input alongside the real one, both
   * carrying the same label. Left in, the pair either types the answer twice
   * or consumes the answer on the phantom and starves the real field — which
   * is why required questions came back blank on Anthropic's form.
   */
  function dedupeWidgets(list) {
    const seen = new Map();
    const out = [];
    for (const el of list) {
      const widget = el.closest(
        "[class*='select__control'], [class*='select-container'], [class*='autocomplete']"
      );
      if (!widget) { out.push(el); continue; }
      if (seen.has(widget)) {
        // Keep whichever of the pair carries a name or id.
        const prev = seen.get(widget);
        if (!(prev.name || prev.id) && (el.name || el.id)) {
          out[out.indexOf(prev)] = el;
          seen.set(widget, el);
        }
        continue;
      }
      seen.set(widget, el);
      out.push(el);
    }
    return out;
  }

  async function fill(packet, opts = {}) {
    const answers = packet.fields.filter(
      (f) => f.status === "filled" && f.value !== null && f.value !== ""
    );
    const report = { filled: [], missed: [], skipped: [], resume: false, seen: [] };
    const used = new Set();

    const fields = dedupeWidgets(controls());

    // Radios and checkboxes that share a name are one question. Collapse them
    // so the group is matched once and the right member is ticked.
    const groups = new Map();
    for (const el of fields) {
      if (el.type !== "radio" && el.type !== "checkbox") continue;
      const key = el.name || el.closest("fieldset") || el.id;
      if (!key) continue;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(el);
    }
    const groupOf = new Map();
    for (const [key, members] of groups) {
      for (const el of members) groupOf.set(el, key);
    }
    const groupDone = new Set();

    for (const el of fields) {
      const ids = identity(el);
      report.seen.push({ name: el.name || el.id || "", type: el.type || el.tagName, ids });

      if (el.type === "file") {
        const isResume = ids.some((i) => /resume|cv\b|curriculum/.test(i));
        if (isResume && opts.resumeBytes) {
          report.resume = fillFile(el, opts.resumeBytes, opts.resumeName);
          if (report.resume) report.filled.push("Resume/CV (file)");
          else report.missed.push("Resume/CV (file)");
        }
        continue;
      }

      const gkey = groupOf.get(el);
      if (gkey !== undefined && groupDone.has(gkey)) continue;

      let match = null;
      let bestScore = 55; // below this the guess is not worth making
      for (const f of answers) {
        // A canonical match (>=92) may serve more than one control: forms ask
        // sponsorship as two separate questions, now and in the future.
        if (used.has(f) && (f._canonUses || 0) >= 2) continue;
        if (used.has(f)) {
          const s2 = score(ids, f.label, f.name);
          if (s2 >= 92 && s2 > bestScore) { bestScore = s2; match = f; }
          continue;
        }
        const s = score(ids, f.label, f.name);
        if (s > bestScore) { bestScore = s; match = f; }
      }
      if (!match) continue;
      if (used.has(match)) match._canonUses = (match._canonUses || 1) + 1;

      if (gkey !== undefined) {
        groupDone.add(gkey);
        const ok = fillGroup(groups.get(gkey), match.value);
        if (ok) { used.add(match); report.filled.push(match.label); }
        else report.missed.push(match.label + " (no matching option in the group)");
        continue;
      }

      // A paste-your-resume textarea is not the upload field.
      if (/resume|cv\b/.test(norm(match.label)) &&
          String(match.value).endsWith(".pdf")) {
        continue;
      }

      let ok = false;
      try {
        if (el.tagName === "SELECT") ok = await fillSelect(el, match.value);
        else if (
          el.getAttribute("role") === "combobox" ||
          el.getAttribute("aria-autocomplete") ||
          el.closest("[class*='select__'], [class*='Select'], [class*='autocomplete']")
        )
          ok = await fillCombobox(el, match.value);
        else { setNativeValue(el, match.value); ok = true; }
      } catch (err) {
        ok = false;
      }

      if (ok) { used.add(match); report.filled.push(match.label); }
      else report.missed.push(match.label + " (control did not take it)");
    }

    for (const f of answers) {
      if (!used.has(f) && !report.filled.includes(f.label)) {
        report.missed.push(f.label + " (no control on this page)");
      }
    }
    report.skipped = packet.fields
      .filter((f) => f.status === "skipped")
      .map((f) => f.label);
    return report;
  }

  window.__jobpipe = { fill, identity, labelFor, bestOption, norm };
})();
