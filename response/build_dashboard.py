#!/usr/bin/env python3
"""
Build dashboard.html — a Jira-style Kanban board for RFP / lead pursuit.

Pulls opportunities from every source (SAM.gov, FDA approvals, manufacturer
pipeline, state/local), qualifies each one, estimates effort, lists blockers
from our content gaps, and gives the top-3 next steps. Cards are draggable across
columns (New → Reviewing → Preparing → Submitted → Closed); board state + notes
persist in the browser (localStorage), so regenerating the file keeps your board.

Stdlib only. Output: rfp-funnel/dashboard.html
"""
import datetime as dt
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPP = ROOT / "data" / "opportunities"
KB = ROOT / "knowledge_base"
CFG = ROOT / "config"
OUT = ROOT / "dashboard.html"


def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def e(s):
    return html.escape(str(s if s is not None else ""))


def days_left(deadline):
    if not deadline:
        return None
    try:
        s = str(deadline).replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s) if "T" in s else dt.datetime.fromisoformat(s[:10] + "T00:00:00")
        now = dt.datetime.now(d.tzinfo) if d.tzinfo else dt.datetime.now()
        return (d - now).days
    except Exception:
        return None


# ---------- load ----------
recs = {k: load(OPP / f"current_{k}.json", {}).get("records", [])
        for k in ("sam", "fda", "pipeline", "state", "dose")}
analysis = load(OPP / "rfp_analysis.json", {})  # SAM PDF fit verdicts, keyed by notice id
pitches = load(OPP / "pitches_index.json", {})  # per-drug outreach + pitch PDF, keyed by card id
inv = load(KB / "asset_inventory.json", {"by_category": {}})
profile = load(KB / "company_profile.json", {})
checklist = load(CFG / "submission_checklist.json", {"items": []})
cats = inv.get("by_category", {})

# checklist status + gaps
items, have_ct = [], 0
for it in checklist["items"]:
    mt = it.get("maps_to")
    files = cats.get(mt, []) if mt else []
    if files:
        status = "have"; have_ct += 1
    elif it["id"] == "soc2_hitrust" or "VERIFY" in (it.get("notes") or "").upper():
        status = "verify"
    else:
        status = "missing"
    items.append({**it, "status": status, "files": files})
required = [i for i in items if i["required"]]
req_have = sum(1 for i in required if i["status"] == "have")
gaps = [i for i in items if i["status"] in ("missing", "verify")]
req_blockers = [g["label"] for g in gaps if g["required"]]


# ---------- qualification per opportunity ----------

def qualify_rfp(r):
    ex = r.get("extra", {})
    federal = "sam.gov" in (r.get("link") or "").lower() or "SAM.gov" in r.get("source", "")
    matched = ", ".join(r.get("matched", []))
    cat = ex.get("category", "")
    fit = f"Specialty-pharmacy match ({matched})" if matched else "Pharmacy-related solicitation"
    if cat:
        fit += f" · {cat}"
    blockers = list(req_blockers)
    if federal and "W-9 / business registration / SAM UEI" not in " ".join(blockers):
        blockers.insert(0, "SAM.gov UEI registration (federal)")
    est = f"~{4 + len(blockers) * 2}h — questionnaire pre-fills from answer bank; {len(blockers)} doc(s) to gather"
    dl = r.get("deadline")
    steps = [
        f"Confirm fit &amp; deadline ({e(str(dl)[:10]) if dl else 'see notice'})",
        f"Pre-fill questionnaire from answer bank ({req_have}/{len(required)} docs ready)",
        f"Gather blockers: {', '.join(blockers[:3]) if blockers else 'none — package ready'}",
    ]
    links = []
    for n, dl_ in enumerate(ex.get("doc_links", [])[:6], 1):
        links.append({"label": f"📄 Doc {n}", "url": dl_})
    if r.get("link"):
        links.append({"label": "Open on SAM.gov" if federal else "🔎 Find in City Record", "url": r["link"]})
    c = ex.get("contact", {})
    email = c.get("email") or next((v for v in c.values() if "@" in str(v)), "")
    if email:
        links.append({"label": "✉ Request package", "url": f"mailto:{email}"})
    return fit, est, blockers, steps, links, ex.get("body", "")


def qualify_lead(r):
    org = r.get("org", "the manufacturer")
    drug = r.get("title", "the drug")
    why = "; ".join(r.get("why", [])) or "new specialty approval"
    fit = f"{why} — {org} will need a limited-distribution / hub partner"
    est = "~1–2h — capability pitch (no public RFP)"
    blockers = ["No public RFP — relationship/timing play"]
    steps = [
        f"Identify trade/market-access lead at {e(org)}",
        "Send capability pitch (360 bios + 2 case studies)",
        f"Track for the LDD-network RFP on {e(drug)}",
    ]
    links = [{"label": "FDA approval record", "url": r.get("link", "")}]
    return fit, est, blockers, steps, links, ""


def qualify_pipeline(r):
    org = r.get("org", "")
    drugs = ", ".join(r.get("drugs", [])) or r.get("title", "")
    conds = ", ".join(r.get("conditions", []))
    comp = r.get("award_date", "")
    fit = f"Phase 3 {drugs} ({conds}) — {org} nearing launch"
    est = "~2h — research + early outreach"
    blockers = ["Pre-approval — build the relationship before the RFP exists"]
    steps = [
        f"Monitor {e(drugs)} approval timeline (est. completion {e(comp)})",
        f"Open relationship with {e(org)} trade/access team NOW",
        f"Prep LDD / specialty pitch for {e(conds) or 'this therapy'}",
    ]
    links = [{"label": "ClinicalTrials.gov", "url": r.get("link", "")}]
    return fit, est, blockers, steps, links, ""


def qualify_dose(r):
    org = r.get("org", "")
    drugs = ", ".join(r.get("drugs", [])) or r.get("title", "")
    conds = ", ".join(r.get("conditions", []))
    start = r.get("start", "")
    fit = f"Oral trial — {drugs} ({conds}); {org} starts {start}. Win the trial pharmacy support."
    est = "~1–2h — trial-support pitch (IMP dispensing / adherence)"
    blockers = ["Pre-start — reach the trial's sponsor/CRO before kickoff"]
    steps = [
        f"Contact {e(org)} clinical ops / CRO before {e(start)}",
        "Pitch IMP dispensing + adherence (Medesto-Dose) for the oral arm",
        "Confirm sites &amp; states — leverage 50-state licensure",
    ]
    links = [{"label": "ClinicalTrials.gov", "url": r.get("link", "")}]
    return fit, est, blockers, steps, links, ""


def make_card(r, kind):
    fit_analysis = None
    if kind == "rfp":
        fit, est, blockers, steps, links, body = qualify_rfp(r)
        cid = r.get("id", "")
        fit_analysis = analysis.get(cid)
    elif kind == "lead":
        fit, est, blockers, steps, links, body = qualify_lead(r)
        cid = "fda:" + r.get("app", r.get("title", ""))
    elif kind == "dose":
        fit, est, blockers, steps, links, body = qualify_dose(r)
        cid = "dose:" + r.get("nct", "")
    else:
        fit, est, blockers, steps, links, body = qualify_pipeline(r)
        cid = "pipe:" + r.get("nct", "")
    return {
        "id": cid, "kind": kind, "source": r.get("source", ""), "title": r.get("title", "(untitled)"),
        "org": r.get("org", ""), "drugs": r.get("drugs", []), "deadline": r.get("deadline", ""),
        "award_date": r.get("award_date", "") or (r.get("date", "") if kind == "lead" else ""),
        "score": r.get("score", 0), "fit": fit, "est": est, "blockers": blockers,
        "steps": steps, "links": [l for l in links if l.get("url")], "body": body,
        "naics": r.get("naics", ""), "notice": r.get("notice", ""), "pin": r.get("extra", {}).get("pin", ""),
        "analysis": fit_analysis, "pitch": pitches.get(cid),
    }


cards = ([make_card(r, "rfp") for r in recs["sam"]] +
         [make_card(r, "rfp") for r in recs["state"]] +
         [make_card(r, "lead") for r in recs["fda"]] +
         [make_card(r, "pipeline") for r in recs["pipeline"]] +
         [make_card(r, "dose") for r in recs["dose"]])
cards.sort(key=lambda c: c["score"], reverse=True)


# ---------- render ----------

KIND_LABEL = {"rfp": "RFP", "lead": "FDA APPROVAL", "pipeline": "PIPELINE", "dose": "DOSE · TRIAL"}
VERDICT_CLS = {"Strong fit": "ok", "Possible": "warn", "Likely out of scope": "bad"}


def card_html(c):
    dl = days_left(c["deadline"])
    due_line = ""
    if c["deadline"]:
        if dl is None:
            due_txt, duecls = e(str(c["deadline"])[:10]), ""
        elif dl < 0:
            due_txt, duecls = f"{e(str(c['deadline'])[:10])} · PAST DUE", "bad"
        else:
            due_txt, duecls = f"{e(str(c['deadline'])[:10])} · {dl}d left", ("warn" if dl <= 14 else "ok")
        label = "Trial starts" if c["kind"] == "dose" else "Offers due"
        due_line = f'<div class="kc-due {duecls}">📅 {label}: {due_txt}</div>'
    chips = []
    if c["drugs"]:
        chips.append(f'<span class="chip">💊 {e(", ".join(c["drugs"])[:46])}</span>')
    if c["award_date"]:
        lbl = "approved" if c["kind"] == "lead" else "est. launch"
        chips.append(f'<span class="chip">🏁 {lbl} {e(str(c["award_date"])[:10])}</span>')
    if c["pin"]:
        chips.append(f'<span class="chip">PIN {e(c["pin"])}</span>')
    # SAM PDF fit verdict
    vbadge, fit_block = "", ""
    a = c.get("analysis")
    if a:
        vcls = VERDICT_CLS.get(a.get("verdict"), "")
        vbadge = f'<span class="vbadge {vcls}">{e(a.get("verdict"))} · fit {e(a.get("fit_score"))}</span>'
        rs = "".join(f"<li class='gd'>✓ {e(x)}</li>" for x in a.get("reasons", []))
        fl = "".join(f"<li class='bd2'>⚠ {e(x)}</li>" for x in a.get("flags", []))
        fit_block = f'<details class="fitd"><summary>📑 Read the solicitation PDF — fit analysis</summary><ul class="fitlist">{rs}{fl}</ul></details>'
    blockers = "; ".join(c["blockers"])
    pitch_block = ""
    p = c.get("pitch")
    if p:
        safeid = "".join(ch for ch in c["id"] if ch.isalnum())
        pdf = p.get("pdf", "")
        pdf_link = f'<a class="dl" href="file://{e(pdf)}" target="_blank">📄 {e(p.get("drug", c["title"]))} pitch PDF</a>' if pdf else ""
        pitch_block = ('<details class="pitchd"><summary>✉ Outreach draft + pitch PDF</summary>'
                       f'<textarea class="outbox" id="o{safeid}" readonly>{e(p.get("outreach", ""))}</textarea>'
                       f'<div class="prow"><button class="copyb" data-t="o{safeid}">⧉ Copy message</button>{pdf_link}</div>'
                       '</details>')
    steps = "".join(f"<li>{s}</li>" for s in c["steps"])
    links = "".join(f'<a class="dl" href="{e(l["url"])}" target="_blank">{l["label"]}</a>' for l in c["links"]) \
        or '<span class="muted">contact via notice</span>'
    body = f'<details class="bd"><summary>RFP details</summary><div class="body">{e(c["body"])}</div></details>' if c["body"] else ""
    ddl = e(str(c["deadline"])[:10]) if c["deadline"] else ""
    return f"""<div class="kcard" draggable="true" data-id="{e(c['id'])}" data-kind="{c['kind']}" data-deadline="{ddl}" data-text="{e((c['title']+' '+c['org']+' '+' '.join(c['drugs'])).lower())}">
      <div class="kc-top"><span class="tag t-{c['kind']}">{KIND_LABEL[c['kind']]} · {e(c['source'])}</span><span class="kc-score">{e(c['score'])}</span></div>
      <h4>{e(c['title'])}</h4>
      <div class="kc-org">{e(c['org'])}</div>
      {f'<div class="vrow">{vbadge}</div>' if vbadge else ''}
      {due_line}
      <div class="chips">{''.join(chips)}</div>
      <div class="kc-fit">🎯 {e(c['fit'])}</div>
      <div class="kc-est">⏱ {e(c['est'])}</div>
      {f'<div class="kc-block">⛔ {e(blockers)}</div>' if c['blockers'] else ''}
      {fit_block}
      {pitch_block}
      <details class="steps"><summary>Top 3 steps</summary><ol>{steps}</ol></details>
      {body}
      <div class="kc-links">{links}</div>
      <button class="oos" data-id="{e(c['id'])}" title="Mark as not a fit for our business">✕ Out of scope</button>
    </div>"""


COLS = [("new", "New"), ("reviewing", "Reviewing"), ("preparing", "Preparing"),
        ("submitted", "Submitted"), ("closed", "Closed"),
        ("pastdue", "Past Deadline"), ("outofscope", "Out of Scope")]
cards_html = "\n".join(card_html(c) for c in cards)
col_html = "".join(
    f'<div class="kcol" id="col-{cid}"><div class="col-h">{name} <span class="cc" id="cc-{cid}">0</span></div><div class="col-body" data-col="{cid}"></div></div>'
    for cid, name in COLS)

n_rfp = sum(1 for c in cards if c["kind"] == "rfp")
n_lead = sum(1 for c in cards if c["kind"] == "lead")
n_pipe = sum(1 for c in cards if c["kind"] == "pipeline")
n_dose = sum(1 for c in cards if c["kind"] == "dose")
readiness = round(100 * req_have / max(len(required), 1))
gaps_html = "".join(f"<li><b>{e(g['label'])}</b> — {e(g.get('notes') or '')}</li>" for g in gaps)


def answer_blocks():
    out = []
    for section, body in profile.items():
        if section.startswith("_") or not isinstance(body, dict):
            continue
        rows = []
        for k, v in body.items():
            if "confidence" in k:
                continue
            val = ", ".join(v) if isinstance(v, list) else v
            rows.append(f'<div class="ans"><div class="ans-k">{e(k.replace("_"," ").title())}</div><div>{e(val)}</div></div>')
        conf = body.get("security_confidence", "")
        flag = f'<div class="conf">⚑ {e(conf)}</div>' if "verify" in str(conf).lower() else ""
        out.append(f'<details class="ansec"><summary>{e(section.replace("_"," ").title())}</summary>{flag}{"".join(rows)}</details>')
    return "".join(out)


updated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
sam_note = "" if recs["sam"] else "SAM.gov empty this run (daily quota); refreshes at 1am with direct doc links."

DOC = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Perigon RFP Board</title>
<style>
:root{{--bg:#0e1320;--panel:#161d2d;--col:#121929;--line:#26314a;--txt:#e7ecf5;--mut:#8a97b0;--ok:#2fbf71;--bad:#ef5b6b;--warn:#f0b429;--acc:#4d8bf0;--rfp:#4d8bf0;--lead:#2fbf71;--pipe:#b07df0;--inset:#0c1322}}
body.light{{--bg:#eef1f7;--panel:#ffffff;--col:#f4f6fb;--line:#dbe1ec;--txt:#1b2436;--mut:#5e6b85;--ok:#1a9e5a;--bad:#d6455a;--warn:#b07d12;--acc:#2563eb;--rfp:#2563eb;--lead:#1a9e5a;--pipe:#8b3fd6;--inset:#eef2f8}}
*{{box-sizing:border-box}}body{{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt);transition:background .2s,color .2s}}
header{{padding:16px 22px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}}
h1{{font-size:20px;margin:0 0 4px}}.sub{{color:var(--mut);font-size:13px}}
.toolbar{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}}
.pill{{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:5px 12px;cursor:pointer;font-size:12.5px;color:var(--txt)}}
.pill.on{{border-color:var(--acc);color:var(--acc)}}
input.search{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 12px;color:var(--txt);min-width:220px}}
.stat{{color:var(--mut);font-size:12.5px}}.stat b{{color:var(--txt)}}
.reset{{background:transparent;border:1px solid var(--line);color:var(--mut);border-radius:8px;padding:6px 12px;cursor:pointer}}
.theme{{margin-left:auto;background:transparent;border:1px solid var(--line);color:var(--mut);border-radius:8px;padding:6px 12px;cursor:pointer}}
.board{{display:flex;gap:14px;padding:18px 22px;overflow-x:auto;align-items:flex-start;min-height:70vh}}
.kcol{{background:var(--col);border:1px solid var(--line);border-radius:12px;min-width:300px;max-width:330px;flex:0 0 auto}}
.col-h{{padding:12px 14px;font-weight:700;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--mut);border-bottom:1px solid var(--line);position:sticky;top:0}}
.cc{{background:var(--inset);border:1px solid var(--line);border-radius:20px;padding:1px 8px;font-size:12px;color:var(--txt);margin-left:4px}}
.col-body{{padding:10px;min-height:120px;display:flex;flex-direction:column;gap:10px}}
.col-body.drag{{outline:2px dashed var(--acc);outline-offset:-4px;border-radius:8px}}
.kcard{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px;cursor:grab}}
.kcard.dragging{{opacity:.5}}
.kc-top{{display:flex;justify-content:space-between;align-items:center;gap:6px}}
.tag{{font-size:10px;letter-spacing:.05em;font-weight:700;padding:2px 6px;border-radius:5px}}
.t-rfp{{background:rgba(77,139,240,.16);color:var(--rfp)}}.t-lead{{background:rgba(47,191,113,.16);color:var(--lead)}}.t-pipeline{{background:rgba(176,125,240,.16);color:var(--pipe)}}
.kc-score{{font-weight:700;color:var(--acc);font-size:13px}}
.kcard h4{{margin:7px 0 2px;font-size:14px;line-height:1.3}}
.kc-org{{color:var(--mut);font-size:12.5px;margin-bottom:7px}}
.chips{{display:flex;flex-wrap:wrap;gap:5px;margin:6px 0}}
.chip{{font-size:11px;background:var(--inset);border:1px solid var(--line);border-radius:5px;padding:2px 7px}}
.chip.ok{{color:var(--ok)}}.chip.bad{{color:var(--bad)}}.chip.warn{{color:var(--warn)}}
.kc-fit{{font-size:12.5px;margin:6px 0;color:var(--txt)}}.kc-est{{font-size:12px;color:var(--mut)}}
.kc-block{{font-size:11.5px;color:var(--bad);background:rgba(239,91,107,.08);border-radius:6px;padding:5px 7px;margin:6px 0}}
.vrow{{margin:4px 0}}.vbadge{{display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:5px}}
.vbadge.ok{{background:rgba(47,191,113,.18);color:var(--ok)}}.vbadge.warn{{background:rgba(240,180,41,.18);color:var(--warn)}}.vbadge.bad{{background:rgba(239,91,107,.18);color:var(--bad)}}
.kc-due{{font-size:12.5px;font-weight:600;margin:5px 0;padding:4px 8px;border-radius:6px;background:var(--inset);border:1px solid var(--line)}}
.kc-due.ok{{color:var(--ok)}}.kc-due.warn{{color:var(--warn)}}.kc-due.bad{{color:var(--bad)}}
.fitd{{margin:6px 0}}.fitd summary{{cursor:pointer;font-size:12px;color:var(--acc)}}
.fitlist{{list-style:none;padding:6px 0 0;margin:0;font-size:12px}}.fitlist li{{padding:3px 0}}.fitlist .gd{{color:var(--ok)}}.fitlist .bd2{{color:var(--bad)}}
.oos{{margin-top:8px;width:100%;background:transparent;border:1px dashed var(--line);color:var(--mut);border-radius:6px;padding:4px;cursor:pointer;font-size:11.5px}}
.oos:hover{{border-color:var(--bad);color:var(--bad)}}
.export{{background:transparent;border:1px solid var(--line);color:var(--mut);border-radius:8px;padding:6px 12px;cursor:pointer}}
.t-dose{{background:rgba(240,140,60,.18);color:#f0913c}}
.pitchd{{margin:6px 0;border-top:1px solid var(--line);padding-top:8px}}.pitchd summary{{cursor:pointer;font-size:12px;color:var(--lead);font-weight:600}}
.outbox{{width:100%;height:130px;margin-top:6px;background:var(--inset);border:1px solid var(--line);border-radius:6px;color:var(--txt);font-size:10.5px;line-height:1.4;padding:8px;resize:vertical;font-family:ui-monospace,Menlo,monospace}}
.prow{{display:flex;gap:8px;margin-top:6px;flex-wrap:wrap;align-items:center}}
.copyb{{background:var(--lead);border:none;color:#fff;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:11.5px;font-weight:600}}
.steps,.bd{{margin:6px 0}}.steps summary,.bd summary{{cursor:pointer;font-size:12px;color:var(--mut)}}
.steps ol{{margin:6px 0;padding-left:18px;font-size:12.5px}}.steps li{{margin:3px 0}}
.body{{font-size:12px;color:var(--txt);max-height:200px;overflow:auto;background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:8px;margin-top:6px}}
.kc-links{{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}}
.dl{{background:var(--inset);border:1px solid var(--line);border-radius:6px;padding:3px 8px;font-size:11.5px;color:var(--acc);text-decoration:none}}
.dl:hover{{border-color:var(--acc)}}.muted{{color:var(--mut);font-size:11.5px}}
a{{color:var(--acc)}}
.foot{{padding:18px 22px;border-top:1px solid var(--line)}}
.foot h2{{font-size:14px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}}
.gaps{{background:rgba(239,91,107,.07);border:1px solid #3a2630;border-radius:10px;padding:6px 20px}}.gaps li{{margin:6px 0}}
.ansec{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 12px;margin:6px 0}}.ansec summary{{cursor:pointer;font-weight:600}}
.ans{{display:flex;gap:10px;padding:5px 0;border-top:1px solid var(--line);font-size:12.5px}}.ans-k{{min-width:160px;color:var(--mut)}}
.conf{{color:var(--warn);font-size:12px;margin:5px 0}}
</style></head><body>
<header>
  <h1>Perigon RFP Board</h1>
  <div class="sub">Discover → qualify → submit · {len(cards)} opportunities · generated {updated}{(' · ' + sam_note) if sam_note else ''}</div>
  <div class="toolbar">
    <span class="stat"><b>{n_rfp}</b> RFPs · <b>{n_lead}</b> FDA · <b>{n_pipe}</b> pipeline · <b>{n_dose}</b> DOSE · <b>{readiness}%</b> docs ready</span>
    <span class="pill on" data-f="all">All</span>
    <span class="pill" data-f="rfp">RFPs</span>
    <span class="pill" data-f="lead">FDA approvals</span>
    <span class="pill" data-f="pipeline">Pipeline</span>
    <span class="pill" data-f="dose">DOSE (trials)</span>
    <input class="search" placeholder="search drug / manufacturer / program…">
    <button class="export" title="Download board decisions (incl. out-of-scope) as JSON">⬇ Export</button>
    <button class="theme" title="Toggle light/dark">🌙 Dark</button>
    <button class="reset">Reset board</button>
  </div>
</header>
<div class="board">{col_html}</div>
<div id="stash" style="display:none">{cards_html}</div>
<div class="foot">
  <h2>What we don't have yet — {len(gaps)} gaps blocking submission</h2>
  <ul class="gaps">{gaps_html}</ul>
  <h2 style="margin-top:24px">Pre-filled answer bank (paste-ready)</h2>
  {answer_blocks()}
  <p class="sub" style="margin-top:20px">Sources: SAM.gov · openFDA · ClinicalTrials.gov · NYC City Record · Michigan DTMB · your historical RFP library. Board state saves in this browser. Submission stays human.</p>
</div>
<script>
// theme toggle (persisted)
const TKEY='rfpTheme';
const tbtn=document.querySelector('.theme');
function applyTheme(t){{document.body.classList.toggle('light',t==='light');tbtn.textContent=t==='light'?'☀️ Light':'🌙 Dark'}}
let theme=localStorage.getItem(TKEY)||'dark';applyTheme(theme);
tbtn.addEventListener('click',()=>{{theme=theme==='light'?'dark':'light';localStorage.setItem(TKEY,theme);applyTheme(theme)}});

const KEY='rfpBoardV2';
const cols=['new','reviewing','preparing','submitted','closed','pastdue','outofscope'];
function st(){{try{{return JSON.parse(localStorage.getItem(KEY)||'{{}}')}}catch(e){{return {{}}}}}}
function save(s){{localStorage.setItem(KEY,JSON.stringify(s))}}
function counts(){{cols.forEach(c=>{{document.getElementById('cc-'+c).textContent=document.querySelectorAll('#col-'+c+' .kcard').length}})}}
function defCol(card){{
  const d=card.dataset.deadline;
  if(d){{const today=new Date().toISOString().slice(0,10); if(d<today) return 'pastdue';}}
  return 'new';
}}
function place(){{
  const s=st();
  document.querySelectorAll('#stash .kcard').forEach(card=>{{
    const id=card.dataset.id; const col=(s[id]&&s[id].col)||defCol(card);
    const body=document.querySelector('#col-'+col+' .col-body')||document.querySelector('#col-new .col-body');
    body.appendChild(card);
  }});
  counts();
}}
function moveTo(id,col){{
  const card=document.querySelector('.kcard[data-id="'+CSS.escape(id)+'"]'); if(!card)return;
  document.querySelector('#col-'+col+' .col-body').appendChild(card);
  const s=st(); s[id]=s[id]||{{}}; s[id].col=col; save(s); counts();
}}
let dragId=null;
document.addEventListener('dragstart',e=>{{if(e.target.classList.contains('kcard')){{dragId=e.target.dataset.id;e.target.classList.add('dragging')}}}});
document.addEventListener('dragend',e=>{{if(e.target.classList&&e.target.classList.remove)e.target.classList.remove('dragging')}});
document.querySelectorAll('.col-body').forEach(body=>{{
  body.addEventListener('dragover',e=>{{e.preventDefault();body.classList.add('drag')}});
  body.addEventListener('dragleave',()=>body.classList.remove('drag'));
  body.addEventListener('drop',e=>{{
    e.preventDefault();body.classList.remove('drag');
    const card=document.querySelector('.kcard[data-id="'+CSS.escape(dragId)+'"]');
    if(!card)return; body.appendChild(card);
    const col=body.dataset.col; const s=st(); s[dragId]=s[dragId]||{{}}; s[dragId].col=col; save(s); counts();
  }});
}});
// filters
let curF='all',curQ='';
function applyFilter(){{
  document.querySelectorAll('.kcard').forEach(c=>{{
    const okF=curF==='all'||c.dataset.kind===curF;
    const okQ=!curQ||c.dataset.text.includes(curQ);
    c.style.display=(okF&&okQ)?'':'none';
  }});
}}
document.querySelectorAll('.pill').forEach(p=>p.addEventListener('click',()=>{{
  document.querySelectorAll('.pill').forEach(x=>x.classList.remove('on'));p.classList.add('on');curF=p.dataset.f;applyFilter();
}}));
document.querySelector('.search').addEventListener('input',e=>{{curQ=e.target.value.toLowerCase().trim();applyFilter()}});
document.querySelector('.reset').addEventListener('click',()=>{{if(confirm('Reset board to defaults? (clears your saved columns)')){{localStorage.removeItem(KEY);location.reload()}}}});
// quick "out of scope" button on each card
document.querySelectorAll('.oos').forEach(b=>b.addEventListener('click',e=>{{e.stopPropagation();moveTo(b.dataset.id,'outofscope')}}));
// copy outreach message to clipboard
document.querySelectorAll('.copyb').forEach(b=>b.addEventListener('click',e=>{{
  e.stopPropagation();const t=document.getElementById(b.dataset.t);
  const done=()=>{{b.textContent='✓ Copied';setTimeout(()=>b.textContent='⧉ Copy message',1500)}};
  if(navigator.clipboard){{navigator.clipboard.writeText(t.value).then(done).catch(()=>{{t.select();document.execCommand('copy');done()}})}}
  else{{t.select();document.execCommand('copy');done()}}
}}));
// export the team's decisions (columns incl. out-of-scope) so we can learn/refine filters
document.querySelector('.export').addEventListener('click',()=>{{
  const s=st(),rows=[];
  document.querySelectorAll('.kcard').forEach(c=>{{const id=c.dataset.id;rows.push({{id:id,kind:c.dataset.kind,column:(s[id]&&s[id].col)||defCol(c),deadline:c.dataset.deadline||'',title:(c.querySelector('h4')||{{textContent:''}}).textContent,org:(c.querySelector('.kc-org')||{{textContent:''}}).textContent}})}});
  const blob=new Blob([JSON.stringify({{exported:new Date().toISOString(),decisions:rows}},null,2)],{{type:'application/json'}});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='rfp-board-decisions.json';a.click();
}});
place();
</script></body></html>"""

OUT.write_text(DOC)
print(f"Board written: {OUT}")
print(f"  {len(cards)} cards: {n_rfp} RFP · {n_lead} FDA · {n_pipe} pipeline · readiness {readiness}% · {len(gaps)} gaps")
