"""The label reviewer page: one self-contained HTML string, no build step.

Same conventions as page.py — vanilla JS, textContent everywhere, no CDN — and
it borrows that module's stylesheet verbatim rather than restating it, so the
two reviewers cannot drift apart visually. Only the rules the label list needs
are added on top.

The interaction differs in one way that matters. An item here is a *quote plus
several labels*, and the reviewer rules on them one at a time: a cursor sits on
one label, `y`/`n`/`u` decides it and moves to the next undecided one, and the
group is left behind only when all of its labels are done. That keeps the
familiar keys while cutting the reading to one passage per three or four
decisions.
"""

from __future__ import annotations

import json

from .page import TEMPLATE as _BASE

# Reuse page.py's <style> block. Extracted rather than copied: a palette change
# there should reach this page without anyone remembering to do it twice.
_STYLE = _BASE.split("<style>", 1)[1].split("</style>", 1)[0]

_EXTRA_CSS = """
#labels { display: flex; flex-direction: column; gap: .3em; margin: .5em 0 .2em; }
.lab { display: flex; gap: .6em; align-items: baseline; padding: .4em .6em;
  border: 1px solid var(--line); border-radius: .4em; background: var(--panel);
  border-left: 3px solid transparent; }
.lab.cur { border-left-color: var(--accent); background: var(--panel2); }
.lab.done { opacity: .62; }
.lab .num { color: var(--faint); font-family: ui-monospace, Menlo, monospace;
  font-size: .82em; min-width: 1.2em; }
.lab .name { font-weight: 600; white-space: nowrap; }
.lab .fam { color: var(--faint); font-size: .78em; text-transform: uppercase;
  letter-spacing: .04em; }
.lab .def { color: var(--muted); font-size: .86em; flex: 1 1 auto; }
.lab .mine { font-weight: 600; white-space: nowrap; }
.lab .mine.yes { color: var(--good); }
.lab .mine.no { color: var(--bad); }
.lab .mine.uns { color: var(--faint); }
.lab .jt { white-space: nowrap; font-size: .82em; }
.lab .jt.hit { color: var(--good); }
.lab .jt.miss { color: var(--bad); }
.qhead { color: var(--muted); font-size: .9em; margin: .2em 0 .1em; }
"""

TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hand-validation · refine labels</title>
<style>
__STYLE__
__EXTRA__
</style>
</head>
<body>
<div id="bar">
  <span class="who" id="who"></span>
  <span class="prog" id="prog"></span>
  <span class="blind" id="blindtag" hidden>BLIND</span>
  <span class="flash" id="flash"></span>
  <button id="theme" type="button" title="light / dark">◐</button>
</div>
<div id="head">
  <div id="meta"></div>
  <div id="panel"></div>
</div>
<div id="passage" tabindex="0"></div>
<div id="keys"></div>
<div id="veil"><div id="modal"></div></div>

<script id="cfg" type="application/json">__CONFIG__</script>
<script>
"use strict";
const CFG = JSON.parse(document.getElementById("cfg").textContent);
const S = {
  items: [], gi: 0, li: 0, blind: CFG.blind,
  revealed: new Map(), full: new Set(),
  progress: {done: 0, total: 0, unsure: 0}, elapsed: 0,
  pendingNote: null, modal: null, done: false,
};

/* ── plumbing ──────────────────────────────────────────────────────────── */

async function api(path, opts) {
  const o = Object.assign({headers: {}}, opts || {});
  if (o.method === "POST") {
    o.headers["Content-Type"] = "application/json";
    o.headers["X-Validate-Token"] = CFG.token;
  }
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  return {ok: r.ok, status: r.status, data};
}

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
};
const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); };
const flash = (msg) => { document.getElementById("flash").textContent = msg || ""; };

setInterval(() => { if (!document.hidden) S.elapsed += 0.5; }, 500);
const resetTimer = () => { S.elapsed = 0; };

/* ── data ──────────────────────────────────────────────────────────────── */

async function load() {
  const r = await api("/api/items");
  if (!r.ok) { flash("could not load"); return false; }
  S.items = r.data.items;
  S.blind = r.data.blind;
  S.progress = r.data.progress;
  S.gi = r.data.resume;
  S.li = firstUndecided(S.gi);
  render();
  return true;
}

const item = () => S.items[S.gi];
const labels = () => (item() ? item().labels : []);
const cur = () => labels()[S.li];

function firstUndecided(gi) {
  const it = S.items[gi];
  if (!it) return 0;
  const i = it.labels.findIndex((l) => !l.decided);
  return i < 0 ? 0 : i;
}

const groupDone = (it) => it && it.labels.every((l) => l.decided);

/* ── render ────────────────────────────────────────────────────────────── */

function renderBar() {
  document.getElementById("who").textContent = "refine judge · labels";
  const p = S.progress;
  document.getElementById("prog").textContent =
    "quote " + (S.gi + 1) + "/" + S.items.length + "  ·  done " + p.done + "/" + p.total +
    (p.unsure ? "  ·  unsure " + p.unsure : "");
  document.getElementById("blindtag").hidden = !S.blind;
}

function renderMeta(it) {
  const m = document.getElementById("meta");
  clear(m);
  const line = el("div");
  line.appendChild(el("span", "jur", it.jurisdiction));
  line.appendChild(el("span", null, " · " + it.source + " · " +
    (it.doc_date || "?") + " · " + (it.language || "?") + "   cand#" + it.candidate_id));
  if (!it.is_verbatim) line.appendChild(el("span", "warn", "  [not verbatim]"));
  m.appendChild(line);
  for (const [k, v] of [["speaker", it.speaker], ["setting", it.setting]]) {
    const r = el("div", "row");
    r.appendChild(el("span", "k", k));
    r.appendChild(el("span", null, v));
    m.appendChild(r);
  }
  if (it.url) {
    const r = el("div", "row");
    r.appendChild(el("span", "k", "url"));
    const a = el("a", null, it.url);
    a.href = it.url; a.target = "_blank"; a.rel = "noopener noreferrer";
    r.appendChild(a);
    m.appendChild(r);
  }
}

function renderPanel(it) {
  const p = document.getElementById("panel");
  clear(p);
  const fam = it.family === "risk" ? "MIT risk subdomains" : "AGORA policy instruments";
  p.appendChild(el("div", "qhead",
    "Does this statement substantively engage each of these? (" + fam + ")"));
  const box = el("div");
  box.id = "labels";
  const rev = S.revealed.get(it.grp);
  it.labels.forEach((l, i) => {
    const row = el("div", "lab" + (i === S.li ? " cur" : "") + (l.decided ? " done" : ""));
    row.appendChild(el("span", "num", String(i + 1)));
    row.appendChild(el("span", "name", l.title));
    row.appendChild(el("span", "fam", l.slug));
    row.appendChild(el("span", "def", l.definition));
    if (l.decided) {
      const said = l.human_applies === null ? "unsure" : l.human_applies ? "yes" : "no";
      const cls = l.human_applies === null ? "uns" : l.human_applies ? "yes" : "no";
      row.appendChild(el("span", "mine " + cls, said));
    }
    if (rev && rev[i]) {
      const ja = rev[i].judge_applied;
      const ag = rev[i].agreement;
      row.appendChild(el("span", "jt " + (ag === "agree" ? "hit" : ag === "disagree" ? "miss" : ""),
        ja ? "judge: applied" : "judge: left off"));
    }
    box.appendChild(row);
  });
  p.appendChild(box);
}

function renderPassage(it) {
  const box = document.getElementById("passage");
  clear(box);
  const segs = S.full.has(it.grp) && it.fullPassage ? it.fullPassage : it.passage;
  const focus = S.full.has(it.grp) && it.fullPassage ? it.fullFocus : it.focus;
  segs.forEach((s, i) => {
    const n = el(s.k ? "mark" : "span", s.k ? "k" + s.k : null, s.t);
    n.dataset.seg = String(i);
    box.appendChild(n);
  });
  const target = box.querySelector('[data-seg="' + focus + '"]');
  if (target) target.scrollIntoView({block: "center"});
  else box.scrollTop = 0;
}

function renderKeys() {
  const box = document.getElementById("keys");
  clear(box);
  const keys = [["y", "engages"], ["n", "doesn't"], ["u", "unsure"], ["v", "reveal"],
                ["1-9", "pick label"], ["o", "note"], ["←→", "quote"], ["↑↓", "scroll"],
                ["t", "text"], ["g", "goto"], ["s", "report"], ["?", "help"], ["q", "quit"]];
  for (const [k, lbl] of keys) {
    const kb = el("span", "kb");
    kb.appendChild(el("kbd", null, k));
    kb.appendChild(el("span", null, lbl));
    box.appendChild(kb);
  }
}

function render() {
  const it = item();
  if (!it) return;
  renderBar();
  renderMeta(it);
  renderPanel(it);
  renderPassage(it);
  renderKeys();
}

/* ── actions ───────────────────────────────────────────────────────────── */

function goto(gi) {
  S.gi = Math.max(0, Math.min(S.items.length - 1, gi));
  S.li = firstUndecided(S.gi);
  resetTimer();
  render();
}

function pick(li) {
  const n = labels().length;
  if (!n) return;
  S.li = Math.max(0, Math.min(n - 1, li));
  resetTimer();
  render();
}

async function decide(applies) {
  const l = cur();
  if (!l) return;
  const r = await api("/api/label", {
    method: "POST",
    body: JSON.stringify({id: l.id, human_applies: applies, note: S.pendingNote,
                          seconds: Math.round(S.elapsed * 10) / 10}),
  });
  if (!r.ok) { flash("could not save"); return; }
  Object.assign(l, r.data.label);
  S.progress = r.data.progress;
  S.pendingNote = null;
  resetTimer();
  /* the reviewer is told their own answer back, never whether it matched */
  flash("· " + (applies === null ? "unsure" : applies ? "yes" : "no"));
  const next = labels().findIndex((x) => !x.decided);
  if (next >= 0) { S.li = next; render(); return; }
  /* group complete: on to the next quote with anything left in it */
  const nextGroup = S.items.findIndex((g, i) => i > S.gi && !groupDone(g));
  if (nextGroup >= 0) { goto(nextGroup); return; }
  if (S.items.every(groupDone)) { finish(); return; }
  goto(Math.min(S.gi + 1, S.items.length - 1));
}

async function reveal() {
  const it = item();
  if (S.revealed.has(it.grp)) { render(); return; }
  const r = await api("/api/reveal?grp=" + it.grp);
  if (r.status === 409) { flash("· decide every label here first"); return; }
  if (!r.ok) { flash("could not reveal"); return; }
  S.revealed.set(it.grp, r.data.labels);
  render();
}

async function toggleFull() {
  const it = item();
  if (S.full.has(it.grp)) { S.full.delete(it.grp); render(); return; }
  if (!it.fullPassage) {
    const r = await api("/api/text?grp=" + it.grp);
    if (!r.ok) { flash("could not load the full text"); return; }
    it.fullPassage = r.data.passage;
    it.fullFocus = r.data.focus;
  }
  S.full.add(it.grp);
  render();
}

function finish() {
  S.done = true;
  showReport("every label decided — thank you");
}

async function quit() {
  await api("/api/quit", {method: "POST", body: "{}"});
  S.done = true;
  document.body.innerHTML = "";
  document.body.appendChild(el("p", "hint", "closed — you can shut this tab"));
}

/* ── modals ────────────────────────────────────────────────────────────── */

function openModal(build) {
  const veil = document.getElementById("veil");
  const m = document.getElementById("modal");
  clear(m);
  build(m);
  veil.style.display = "flex";
  S.modal = true;
}

function closeModal() {
  document.getElementById("veil").style.display = "none";
  S.modal = null;
}

function askInput(kind, title, hint, onSubmit) {
  openModal((m) => {
    m.appendChild(el("h3", null, title));
    const input = el("input");
    input.type = "text";
    input.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") { const v = input.value; closeModal(); onSubmit(v); }
      if (e.key === "Escape") closeModal();
    });
    m.appendChild(input);
    m.appendChild(el("div", "hint", hint));
    setTimeout(() => input.focus(), 0);
  });
}

async function showReport(title) {
  const r = await api("/api/report");
  openModal((m) => {
    m.appendChild(el("h3", null, title || "progress"));
    if (!r.ok) { m.appendChild(el("div", "hint", "no report yet")); return; }
    const d = r.data;
    const rows = [["labelled", d.labelled + "/" + d.sample_size],
                  ["unsure", String(d.unsure)]];
    for (const fam of Object.keys(d.by_family || {})) {
      const f = d.by_family[fam];
      const p = f.precision.rate === null ? "—" : f.precision.rate.toFixed(2);
      const n = f.npv.rate === null ? "—" : f.npv.rate.toFixed(2);
      rows.push([fam, "applied-correct " + p + " (" + f.precision.n + ")   " +
                      "left-off-correct " + n + " (" + f.npv.n + ")"]);
    }
    for (const [k, v] of rows) {
      const row = el("div", "row");
      row.appendChild(el("span", "k", k));
      row.appendChild(el("span", null, v));
      m.appendChild(row);
    }
    m.appendChild(el("div", "hint", "any key closes"));
  });
}

function showHelp() {
  openModal((m) => {
    m.appendChild(el("h3", null, "what you are deciding"));
    m.appendChild(el("div", null,
      "Each quote comes with several labels from one taxonomy. For each, say "
      + "whether the statement substantively engages it, using the definition "
      + "shown — that is the same wording the judge was given."));
    m.appendChild(el("div", null,
      "Half the labels across the whole sample were applied by the judge and "
      + "half were not, but the split inside any one quote varies, so you "
      + "cannot read the answers off the group. Which is which is withheld by "
      + "the server until you have decided every label here; v then shows it."));
    m.appendChild(el("div", "hint", "any key closes"));
  });
}

/* ── keyboard ──────────────────────────────────────────────────────────── */

const SCROLL = {ArrowDown: 60, j: 60, ArrowUp: -60, k: -60,
                PageDown: 400, " ": 400, PageUp: -400, b: -400};

document.addEventListener("keydown", (e) => {
  if (S.done) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const tag = e.target && e.target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA") return;
  if (S.modal) { e.preventDefault(); closeModal(); return; }

  const k = e.key;
  if (k in SCROLL) {
    e.preventDefault();
    document.getElementById("passage").scrollTop += SCROLL[k];
    return;
  }
  if (k >= "1" && k <= "9") { e.preventDefault(); pick(parseInt(k, 10) - 1); return; }
  if (k === "ArrowRight" || k === ".") { e.preventDefault(); goto(S.gi + 1); return; }
  if (k === "ArrowLeft" || k === "p") { e.preventDefault(); goto(S.gi - 1); return; }
  if (k === "?" || k === "h") { e.preventDefault(); showHelp(); return; }
  if (k === "s") { e.preventDefault(); showReport(); return; }
  if (k === "q") { e.preventDefault(); quit(); return; }
  if (k === "t") { e.preventDefault(); toggleFull(); return; }
  if (k === "v" || k === "r") { e.preventDefault(); reveal(); return; }
  if (k === "g") {
    e.preventDefault();
    askInput("goto", "go to quote", "1–" + S.items.length + ", Enter to jump", (v) => {
      const i = parseInt(v, 10);
      if (!isNaN(i)) goto(i - 1);
    });
    return;
  }
  if (k === "o") {
    e.preventDefault();
    askInput("note", "note on this label", "saved with your decision", (v) => {
      if (!v) return;
      const l = cur();
      if (l && l.decided) {
        api("/api/label", {method: "POST", body: JSON.stringify({
          id: l.id, human_applies: l.human_applies, note: v})})
          .then(() => { l.note = v; flash("· note saved"); render(); });
      } else {
        S.pendingNote = v;
        flash("· note held; now decide");
      }
    });
    return;
  }
  if (k === "u") { e.preventDefault(); decide(null); return; }
  if (k === "y") { e.preventDefault(); decide(true); return; }
  if (k === "n") { e.preventDefault(); decide(false); return; }
});

/* ── theme ─────────────────────────────────────────────────────────────── */

document.getElementById("theme").addEventListener("click", () => {
  const root = document.documentElement;
  const now = root.getAttribute("data-theme");
  root.setAttribute("data-theme", now === "dark" ? "light" : "dark");
});

load();
</script>
</body>
</html>
"""


def render_page(cfg: dict) -> str:
    """The whole UI, with `cfg` embedded as JSON.

    `</` is escaped because the config sits inside a <script> element, where an
    unescaped closing tag in any string value would end the block early.
    """
    blob = json.dumps(cfg, ensure_ascii=False).replace("</", "<\\/")
    return (
        TEMPLATE.replace("__STYLE__", _STYLE)
        .replace("__EXTRA__", _EXTRA_CSS)
        .replace("__CONFIG__", blob)
    )


__all__ = ["TEMPLATE", "render_page"]
