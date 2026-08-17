"""The reviewer page: one self-contained HTML string, no build step.

Plain HTML, a <style> block, vanilla JS, and a light/dark CSS-variable palette.
Nothing is fetched from a CDN and no file is served off disk, so the whole UI is
this module plus web.py's JSON API.

The page renders passages from the segment lists web.py sends (`{"t","k"}`) and
writes every one of them with textContent. Government text arrives here having
passed through several HTML parsers; it never goes near innerHTML.
"""

from __future__ import annotations

import json

from ..models import RELEVANT


def criteria() -> list[list[str]]:
    """The blind reviewer's whole brief, shown in place of the verdict.

    Mirrors AdjudicationVerdict.accept: one topic over its bar in RELEVANT, and
    all three gates. Without it "do you agree" has no referent once the judge's
    answer is out of sight. Interpolated from RELEVANT rather than typed out,
    so a threshold change cannot leave the brief quietly lying to the reviewer.
    """
    return [
        [
            "counts if",
            f"AGI/ASI/RSI ≥{RELEVANT['agi']}, or x-risk ≥{RELEVANT['x_risk']}, "
            f"or AI regulation ≥{RELEVANT['regulation']} (of 100) — the low "
            f"frontier bars mean genuine engagement, not a passing word",
        ],
        ["and", "the speaker's own engagement, ≥1 sentence — not a joke or a bill title"],
        ["and", "the view is their own, not a quote of someone else"],
        ["and", "they are a lawmaker or a senior executive official"],
    ]


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hand-validation · judge labels</title>
<style>
:root {
  --bg: #ffffff; --fg: #1b1f24; --muted: #5b6470; --faint: #8a939f;
  --line: #e2e6ea; --panel: #f6f8fa; --panel2: #eef1f4;
  --accent: #2a78d6; --mark: #ffe9a8; --link: #0a63c9;
  --span: #cdefd6; --span-line: #2f9e54; --bad: #c0392b; --good: #1e8449;
  --bar-track: #e2e6ea;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171b; --fg: #e6e9ec; --muted: #9aa4b0; --faint: #6b7480;
    --line: #2a2f36; --panel: #1c2026; --panel2: #232830;
    --accent: #5b9bf0; --mark: #6b5a1f; --link: #6aa9f0;
    --span: #1f4d31; --span-line: #4cc47a; --bad: #e8756a; --good: #6fd18f;
    --bar-track: #2a2f36;
  }
}
:root[data-theme="light"] {
  --bg: #ffffff; --fg: #1b1f24; --muted: #5b6470; --faint: #8a939f;
  --line: #e2e6ea; --panel: #f6f8fa; --panel2: #eef1f4;
  --accent: #2a78d6; --mark: #ffe9a8; --link: #0a63c9;
  --span: #cdefd6; --span-line: #2f9e54; --bad: #c0392b; --good: #1e8449;
  --bar-track: #e2e6ea;
}
:root[data-theme="dark"] {
  --bg: #14171b; --fg: #e6e9ec; --muted: #9aa4b0; --faint: #6b7480;
  --line: #2a2f36; --panel: #1c2026; --panel2: #232830;
  --accent: #5b9bf0; --mark: #6b5a1f; --link: #6aa9f0;
  --span: #1f4d31; --span-line: #4cc47a; --bad: #e8756a; --good: #6fd18f;
  --bar-track: #2a2f36;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin: 0; background: var(--bg); color: var(--fg); line-height: 1.5;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  display: grid; grid-template-rows: auto auto 1fr auto; height: 100dvh; }

/* status bar */
#bar { display: flex; align-items: center; gap: .8em; flex-wrap: wrap;
  padding: .45em .9em; background: var(--panel2); border-bottom: 1px solid var(--line);
  font-size: .85em; }
#bar .who { font-weight: 600; }
#bar .prog { font-variant-numeric: tabular-nums; color: var(--muted); }
#bar .blind { background: var(--mark); color: var(--fg); border-radius: 999px;
  padding: .05em .6em; font-size: .9em; font-weight: 600; }
#bar .flash { color: var(--accent); margin-left: auto; }
#theme { font: inherit; font-size: .95em; cursor: pointer; background: none;
  border: 1px solid var(--line); border-radius: 6px; color: var(--muted);
  padding: .1em .5em; }

/* meta + brief/verdict */
#head { padding: .7em .9em .55em; border-bottom: 1px solid var(--line);
  overflow: auto; max-height: 42vh; }
#meta { font-size: .86em; color: var(--muted); }
#meta .jur { font-weight: 700; color: var(--accent); }
#meta .row { margin-top: .12em; }
#meta .k { display: inline-block; min-width: 4.6em; color: var(--faint); }
#meta a { color: var(--link); word-break: break-all; }
#meta .warn { color: var(--bad); font-weight: 600; }

#panel { margin-top: .6em; padding: .6em .75em; border: 1px solid var(--line);
  border-radius: 8px; background: var(--panel); font-size: .88em; }
#panel .tag { display: inline-block; border-radius: 999px; padding: .05em .7em;
  font-weight: 700; font-size: .92em; }
#panel .tag.hidden { background: var(--mark); color: var(--fg); }
#panel .tag.accept { background: var(--good); color: #fff; }
#panel .tag.reject { background: var(--bad); color: #fff; }
#panel .crit { margin-top: .45em; }
#panel .crit div { margin-top: .1em; }
#panel .crit .lbl { display: inline-block; min-width: 5.4em; color: var(--faint); }
.bars { display: grid; grid-template-columns: repeat(auto-fit, minmax(8.5em, 1fr));
  gap: .15em .8em; margin: .5em 0 .35em; font-size: .95em;
  font-variant-numeric: tabular-nums; }
.bars .b { display: flex; gap: .4em; align-items: baseline; color: var(--muted); }
.bars .b.hit { color: var(--fg); font-weight: 700; }
.bars .b .sc { margin-left: auto; }
.gate { margin-right: .8em; }
.gate.ok { color: var(--good); }
.gate.no { color: var(--bad); font-weight: 700; }
#panel .span { font-style: italic; margin-top: .35em; }
#panel .why { margin-top: .3em; color: var(--muted); }
#panel .why.clip { display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
  overflow: hidden; }

/* passage */
#passage { overflow: auto; padding: .9em 1.1em 1.4em; white-space: pre-wrap;
  font-size: .95em; word-wrap: break-word; }
#passage .k1 { background: var(--mark); border-radius: 3px; }
#passage .k2 { background: var(--span); border-bottom: 2px solid var(--span-line); }

/* key bar */
#keys { display: flex; flex-wrap: wrap; gap: .3em; padding: .4em .9em;
  background: var(--panel2); border-top: 1px solid var(--line); font-size: .8em; }
#keys .kb { display: inline-flex; gap: .35em; align-items: baseline; }
#keys kbd { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  background: var(--panel); border: 1px solid var(--line); border-radius: 4px;
  padding: 0 .32em; color: var(--accent); font-weight: 700; }
#keys .kb span { color: var(--muted); margin-right: .5em; }

/* overlays */
#veil { position: fixed; inset: 0; background: rgba(0,0,0,.45); display: none;
  align-items: center; justify-content: center; padding: 2em; }
#veil.on { display: flex; }
#modal { background: var(--bg); border: 1px solid var(--line); border-radius: 10px;
  max-width: 52em; width: 100%; max-height: 82vh; overflow: auto; padding: 1.1em 1.3em; }
#modal h2 { margin: 0 0 .5em; font-size: 1.05em; }
#modal pre { white-space: pre-wrap; font-size: .82em; background: var(--panel);
  padding: .7em; border-radius: 6px; overflow: auto; }
#modal .note { color: var(--bad); font-size: .85em; margin: 0 0 .7em; }
#modal input { font: inherit; width: 100%; padding: .45em .6em; border-radius: 6px;
  border: 1px solid var(--line); background: var(--panel); color: var(--fg); }
#modal .hint { color: var(--muted); font-size: .82em; margin-top: .5em; }
#modal dl { margin: 0; display: grid; grid-template-columns: 7em 1fr; gap: .25em .8em; }
#modal dt { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  color: var(--accent); }
#modal dd { margin: 0; color: var(--muted); }
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
  judge: CFG.judge, items: [], idx: 0, blind: CFG.blind,
  revealed: new Map(), full: new Set(), fullRationale: false,
  progress: {done: 0, total: 0, unsure: 0}, shownAt: Date.now(), elapsed: 0,
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

/* the timer must not run while the tab is in the background: a session left
   open overnight would otherwise record a forty-thousand-second decision */
setInterval(() => { if (!document.hidden) S.elapsed += 0.5; }, 500);
const resetTimer = () => { S.elapsed = 0; };

/* ── data ──────────────────────────────────────────────────────────────── */

async function load(judge, opts) {
  const r = await api("/api/items?judge=" + encodeURIComponent(judge));
  if (!r.ok) { flash("could not load " + judge); return false; }
  S.judge = judge;
  S.items = r.data.items;
  S.blind = r.data.blind;
  S.progress = r.data.progress;
  S.revealed = new Map();
  S.full = new Set();
  S.idx = (opts && opts.keepIdx) ? Math.min(S.idx, S.items.length - 1) : r.data.resume;
  render();
  return true;
}

const item = () => S.items[S.idx];

/* the judge's decision, only if this client has been given it */
function verdictOf(it) {
  if (!S.blind && it.verdict) return it.verdict;
  return S.revealed.get(it.ord) || null;
}

/* ── render ────────────────────────────────────────────────────────────── */

function renderBar() {
  const n = S.judge === "primary" ? "judge 1 · primary" : "judge 2 · confirm";
  document.getElementById("who").textContent = n;
  const p = S.progress;
  document.getElementById("prog").textContent =
    (S.idx + 1) + "/" + S.items.length + "  ·  done " + p.done +
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
  const rows = [["speaker", it.speaker], ["setting", it.setting]];
  for (const [k, v] of rows) {
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

function renderBrief(p) {
  p.appendChild(el("span", "tag hidden", " VERDICT WITHHELD "));
  p.appendChild(el("span", null, "   your call: "));
  const keys = S.blind
    ? [["y", "it counts"], ["n", "it does not"], ["u", "unsure"]]
    : [["a", "agree"], ["d", "disagree"], ["u", "unsure"]];
  p.appendChild(el("span", null, keys.map(k => k[0] + " " + k[1]).join("    ")));
  const c = el("div", "crit");
  for (const [lbl, rule] of CFG.criteria) {
    const d = el("div");
    d.appendChild(el("span", "lbl", lbl));
    d.appendChild(el("span", null, rule));
    c.appendChild(d);
  }
  p.appendChild(c);
}

function renderVerdict(p, v) {
  p.appendChild(el("span", "tag " + (v.accept ? "accept" : "reject"),
    v.accept ? " JUDGE: ACCEPT " : " JUDGE: REJECT "));
  p.appendChild(el("span", null, "   topics: " + (v.topics.join(", ") || "none") +
    "   " + v.model));
  const bars = el("div", "bars");
  for (const s of v.scores) {
    const hit = s.bar !== null && s.score >= s.bar;
    const b = el("div", "b" + (hit ? " hit" : ""));
    b.appendChild(el("span", null, s.topic));
    b.appendChild(el("span", "sc", s.score + (s.bar !== null ? "/" + s.bar : "")));
    bars.appendChild(b);
  }
  p.appendChild(bars);
  const g = el("div");
  for (const gate of v.gates) {
    g.appendChild(el("span", "gate " + (gate.ok ? "ok" : "no"),
      (gate.ok ? "✓ " : "✗ ") + gate.label));
  }
  g.appendChild(el("span", null, "type " + v.quote_type + " · stance " + v.stance +
    (v.speaker_name ? " · as: " + v.speaker_name : "")));
  p.appendChild(g);
  p.appendChild(el("div", "span", "“" + v.span + "”"));
  if (v.quote_en) p.appendChild(el("div", "span", "en: “" + v.quote_en + "”"));
  p.appendChild(el("div", "why" + (S.fullRationale ? "" : " clip"), v.rationale));
}

function renderPanel(it) {
  const p = document.getElementById("panel");
  clear(p);
  const v = verdictOf(it);
  if (v) renderVerdict(p, v); else renderBrief(p);
  if (it.label) {
    const said = it.label.human_accept === null ? "unsure"
      : it.label.human_accept ? "you said it counts" : "you said it does not count";
    const d = el("div", "why", "— " + said + (it.label.note ? "  · note: " +
      it.label.note : ""));
    p.appendChild(d);
  }
}

function renderPassage(it) {
  const box = document.getElementById("passage");
  clear(box);
  const segs = S.full.has(it.ord) && it.fullPassage ? it.fullPassage : it.passage;
  const focus = S.full.has(it.ord) && it.fullPassage ? it.fullFocus : it.focus;
  segs.forEach((s, i) => {
    /* textContent, always: this is government HTML run through several parsers */
    const n = el(s.k ? "mark" : "span", s.k ? "k" + s.k : null, s.t);
    n.dataset.seg = String(i);
    box.appendChild(n);
  });
  const target = box.querySelector('[data-seg="' + focus + '"]');
  if (target) target.scrollIntoView({block: "center"});
  else box.scrollTop = 0;
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

function renderKeys() {
  const box = document.getElementById("keys");
  clear(box);
  const decide = S.blind
    ? [["y", "counts"], ["n", "doesn't"], ["u", "unsure"], ["v", "reveal"]]
    : [["a", "agree"], ["d", "disagree"], ["u", "unsure"], ["r", "why"]];
  const nav = [["o", "note"], ["←→", "item"], ["↑↓", "scroll"],
               ["t", "text"], ["g", "goto"], ["Tab", "judge"], ["s", "report"],
               ["?", "help"], ["q", "quit"]];
  for (const [k, lbl] of decide.concat(nav)) {
    const kb = el("span", "kb");
    kb.appendChild(el("kbd", null, k));
    kb.appendChild(el("span", null, lbl));
    box.appendChild(kb);
  }
}

/* ── actions ───────────────────────────────────────────────────────────── */

function goto(i) {
  S.idx = Math.max(0, Math.min(S.items.length - 1, i));
  S.fullRationale = false;
  resetTimer();
  render();
}

async function decide(humanAccept) {
  const it = item();
  const r = await api("/api/label", {
    method: "POST",
    body: JSON.stringify({judge: S.judge, ord: it.ord, human_accept: humanAccept,
                          note: S.pendingNote, seconds: Math.round(S.elapsed * 10) / 10}),
  });
  if (!r.ok) { flash("could not save"); return; }
  it.label = r.data.label;
  S.progress = r.data.progress;
  S.pendingNote = null;
  /* the reviewer is told their own answer back, never whether it matched the
     judge: hearing "agree" a hundred times is how a reviewer drifts */
  flash("· recorded: " + (humanAccept === null ? "unsure"
        : humanAccept ? "it counts" : "it does not count"));
  const next = S.items.findIndex((x, i) => i > S.idx && !x.label);
  goto(next === -1 ? S.idx + 1 : next);
}

async function reveal() {
  const it = item();
  /* already visible (a prior reveal, or --no-blind): the key expands the
     rationale instead, which is the only thing still clipped */
  if (verdictOf(it)) { S.fullRationale = !S.fullRationale; render(); return; }
  const r = await api("/api/reveal?judge=" + encodeURIComponent(S.judge) + "&ord=" + it.ord);
  if (r.status === 409) { flash("· decide first, then v reveals"); return; }
  if (!r.ok) { flash("could not reveal"); return; }
  S.revealed.set(it.ord, r.data.verdict);
  flash("· revealed");
  render();
}

async function toggleFull() {
  const it = item();
  if (S.full.has(it.ord)) { S.full.delete(it.ord); render(); return; }
  if (!it.fullPassage) {
    const r = await api("/api/text?judge=" + encodeURIComponent(S.judge) + "&ord=" + it.ord);
    if (!r.ok) { flash("could not load the full text"); return; }
    it.fullPassage = r.data.passage;
    it.fullFocus = r.data.focus;
  }
  S.full.add(it.ord);
  render();
}

async function switchJudge() {
  const other = CFG.judges[(CFG.judges.indexOf(S.judge) + 1) % CFG.judges.length];
  const before = S.judge;
  S.idx = 0;
  if (await load(other)) flash("· switched to " + other);
  else S.judge = before;
}

async function quit() {
  await api("/api/quit", {method: "POST", body: "{}"});
  S.done = true;
  document.body.innerHTML = "";
  const p = el("div", null, "Reviewer closed — every decision was saved. " +
    "Run `tracker validate-report` for the numbers.");
  p.style.padding = "2em";
  document.body.appendChild(p);
}

/* ── overlays ──────────────────────────────────────────────────────────── */

function closeModal() {
  S.modal = null;
  document.getElementById("veil").classList.remove("on");
  clear(document.getElementById("modal"));
}

function openModal(kind, build) {
  const veil = document.getElementById("veil");
  const modal = document.getElementById("modal");
  clear(modal);
  build(modal);
  veil.classList.add("on");
  S.modal = kind;
  const input = modal.querySelector("input");
  if (input) input.focus();
}

function askInput(kind, title, hint, onSubmit) {
  openModal(kind, (m) => {
    m.appendChild(el("h2", null, title));
    const input = el("input");
    input.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") { const v = input.value; closeModal(); onSubmit(v); }
      if (e.key === "Escape") closeModal();
    });
    m.appendChild(input);
    m.appendChild(el("div", "hint", hint));
  });
}

async function showReport() {
  const r = await api("/api/report?judge=" + encodeURIComponent(S.judge));
  openModal("report", (m) => {
    m.appendChild(el("h2", null, "agreement so far · " + S.judge));
    m.appendChild(el("p", "note",
      "Reading this mid-session anchors you — the rates below are the judge's answers in "
      + "aggregate."));
    m.appendChild(el("pre", null, r.ok ? JSON.stringify(r.data, null, 2) : "unavailable"));
    m.appendChild(el("div", "hint", "any key closes"));
  });
}

const HELP = [
  ["y / n", "your own call: the quote counts / does not count (blind)"],
  ["a / d", "agree / disagree with the shown verdict (--no-blind only)"],
  ["u", "unsure — recorded, excluded from the rates"],
  ["v", "reveal the verdict, once you have committed to this item"],
  ["r", "expand the clipped rationale (after a reveal)"],
  ["o", "attach a note to this item"],
  ["→ .", "next item without deciding"],
  ["← p", "previous item"],
  ["↑↓ j k", "scroll the passage · space/b, PgUp/PgDn page"],
  ["t", "toggle the judge's passage ↔ the full utterance"],
  ["g", "jump to an item number"],
  ["Tab", "switch between judge 1 (primary) and judge 2 (confirm)"],
  ["s", "the agreement report so far"],
  ["q", "quit — every decision is already saved"],
];

function showHelp() {
  openModal("help", (m) => {
    m.appendChild(el("h2", null, "Hand-validation of one judge's accept/reject label"));
    m.appendChild(el("p", "hint",
      "Blind by default: the server withholds the judge's scores, its rationale and even the "
      + "span it picked until you have committed, so each item opens on the keyword that made "
      + "it a candidate rather than on the judge's answer. Agreement is worked out afterwards "
      + "by `tracker validate-report`."));
    const dl = el("dl");
    for (const [k, v] of HELP) {
      dl.appendChild(el("dt", null, k));
      dl.appendChild(el("dd", null, v));
    }
    m.appendChild(dl);
    m.appendChild(el("div", "hint",
      "The sample is half the judge's accepts and half its rejects, apportioned over "
      + "jurisdiction and year, English only. That balance buys equally tight CIs on precision "
      + "and on negative predictive value; the report reweights them back to corpus rates."));
    m.appendChild(el("div", "hint", "any key closes"));
  });
}

/* ── keyboard ──────────────────────────────────────────────────────────── */

const SCROLL = {ArrowDown: 60, j: 60, ArrowUp: -60, k: -60,
                PageDown: 400, " ": 400, PageUp: -400, b: -400};

document.addEventListener("keydown", (e) => {
  if (S.done) return;
  /* never steal the browser's own chords */
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  /* never label an item because the reviewer typed y into a note */
  const tag = e.target && e.target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA") return;
  if (S.modal) { e.preventDefault(); closeModal(); return; }

  const k = e.key;
  if (k in SCROLL) {
    e.preventDefault();
    document.getElementById("passage").scrollTop += SCROLL[k];
    return;
  }
  if (k === "Home") { e.preventDefault(); document.getElementById("passage").scrollTop = 0; return; }
  if (k === "End") {
    e.preventDefault();
    const p = document.getElementById("passage");
    p.scrollTop = p.scrollHeight;
    return;
  }
  if (k === "ArrowRight" || k === ".") { e.preventDefault(); goto(S.idx + 1); return; }
  if (k === "ArrowLeft" || k === "p") { e.preventDefault(); goto(S.idx - 1); return; }
  if (k === "Tab") { e.preventDefault(); switchJudge(); return; }
  if (k === "?" || k === "h") { e.preventDefault(); showHelp(); return; }
  if (k === "s") { e.preventDefault(); showReport(); return; }
  if (k === "q") { e.preventDefault(); quit(); return; }
  if (k === "t") { e.preventDefault(); toggleFull(); return; }
  if (k === "v" || k === "r") { e.preventDefault(); reveal(); return; }
  if (k === "g") {
    e.preventDefault();
    askInput("goto", "go to item", "1–" + S.items.length + ", Enter to jump", (v) => {
      const i = parseInt(v, 10);
      if (!isNaN(i)) goto(i - 1);
    });
    return;
  }
  if (k === "o") {
    e.preventDefault();
    askInput("note", "note on this item", "saved with your decision", (v) => {
      if (!v) return;
      const it = item();
      if (it.label) {
        api("/api/label", {method: "POST", body: JSON.stringify({
          judge: S.judge, ord: it.ord, human_accept: it.label.human_accept, note: v})})
          .then(() => { it.label.note = v; flash("· note saved"); render(); });
      } else {
        S.pendingNote = v;
        flash("· note held; now decide");
      }
    });
    return;
  }
  if (k === "u") { e.preventDefault(); decide(null); return; }
  if (S.blind && (k === "y" || k === "n")) { e.preventDefault(); decide(k === "y"); return; }
  if (!S.blind && (k === "a" || k === "d")) {
    e.preventDefault();
    const v = verdictOf(item());
    if (v) decide(k === "a" ? v.accept : !v.accept);
    return;
  }
});

document.getElementById("veil").addEventListener("click", (e) => {
  if (e.target.id === "veil") closeModal();
});
document.getElementById("theme").addEventListener("click", () => {
  const now = document.documentElement.getAttribute("data-theme");
  const next = now === "dark" ? "light" : now === "light" ? "dark"
    : (window.matchMedia("(prefers-color-scheme: dark)").matches ? "light" : "dark");
  document.documentElement.setAttribute("data-theme", next);
});

load(CFG.judge);
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
    return TEMPLATE.replace("__CONFIG__", blob)


__all__ = ["TEMPLATE", "criteria", "render_page"]
