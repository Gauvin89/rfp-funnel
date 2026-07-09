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


def _env_val(key):
    """Read a value from config/.env (gitignored) or the environment; '' if unset."""
    import os
    envf = CFG / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            s = line.strip()
            if s.startswith(key + "="):
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get(key, "")


# Shared-notes backend (AWS Lambda + DynamoDB). Empty until deployed → board runs
# local-only (localStorage). Baked into the (encrypted) board, never committed.
SYNC_URL = _env_val("API_URL") or _env_val("WORKER_URL")
SYNC_TOKEN = _env_val("BOARD_API_TOKEN")
CALENDLY = _env_val("CALENDLY_URL") or "https://calendly.com/perigon-intake"


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
manuf = load(OPP / "current_manufacturers.json", {}).get("records", [])  # Lead List (gitignored)
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


def qualify_manuf(r):
    org = r.get("org", "the manufacturer")
    prods = r.get("products", "")
    drugs = r.get("drugs", [])
    c = r.get("contact", {})
    owner = c.get("owner", "")
    focus = (", ".join(drugs[:3]) if drugs else (prods[:60] if prods else "specialty portfolio"))
    fit = f"Manufacturer BD — {org}: {focus}"
    est = "~1–2h — capability pitch / intro meeting (relationship play, no public RFP)"
    blockers = ["No public RFP — relationship/timing play"]
    steps = [
        f"Owner {e(owner) or '(unassigned)'}: advance the {e(org)} conversation",
        "Send / append the Perigon + Medesto capability pitch",
        "Log next touch + outcome in the notes",
    ]
    links = [{"label": f"📄 {m['name']} PI (PDF)", "url": m["pdf"]} for m in r.get("med_pdfs", [])]
    links += [{"label": f"✉ {em}", "url": f"mailto:{em}"} for em in c.get("emails", [])]
    return fit, est, blockers, steps, links, ""


def make_card(r, kind):
    fit_analysis = None
    contact, products = {}, r.get("products", "")
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
    elif kind == "manuf":
        fit, est, blockers, steps, links, body = qualify_manuf(r)
        cid = r.get("id") or ("manuf:" + r.get("org", ""))
        contact = r.get("contact", {})
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
        "contact": contact, "products": products, "people": r.get("people", []),
    }


def med_emoji(c):
    t = (c["title"] + " " + " ".join(c.get("drugs", [])) + " " + c.get("products", "") + " " + c["fit"]).lower()
    if any(w in t for w in ("inject", "subcut", "intraven", "infus", "autoinject", "syringe", "vial", " iv ")):
        return "💉"
    if any(w in t for w in ("capsule", "tablet", "oral", "pill")):
        return "💊"
    return {"dose": "🧬", "rfp": "📋", "pipeline": "🧬"}.get(c["kind"], "💊")


def vertical(c):
    t = (c["org"] + " " + c.get("products", "") + " " + c["fit"] + " "
         + str((c.get("contact") or {}).get("notes", ""))).lower()
    if c["kind"] == "rfp":
        return ("🏛️", "Gov RFP")
    if "pbm" in t or "insur" in t or "payer" in t:
        return ("🏦", "PBM / Insurer")
    if any(w in t for w in ("device", "instrument", "monitor", " pump", "chair", "system for")):
        return ("🩺", "Device")
    if "hub" in t:
        return ("🏥", "HUB")
    if c["kind"] == "dose":
        return ("🧬", "Trial")
    return ("🧪", "Pharma")


def summarize(c):
    lead = {"rfp": "Government/agency solicitation.",
            "lead": "Newly FDA-approved therapy — early LDD/hub target.",
            "pipeline": "Phase-3 therapy nearing launch.",
            "dose": "Oral trial that needs pharmacy support.",
            "manuf": "Manufacturer business-development relationship."}.get(c["kind"], "")
    prod = ", ".join(c.get("drugs", [])[:3]) or (c.get("products", "")[:60])
    s = f"{lead} {c['org'] or 'Org TBD'}" + (f" — {prod}." if prod else ".")
    s += f" How we'd work together: {c['fit']}"
    return s[:280]


cards = ([make_card(r, "rfp") for r in recs["sam"]] +
         [make_card(r, "rfp") for r in recs["state"]] +
         [make_card(r, "lead") for r in recs["fda"]] +
         [make_card(r, "manuf") for r in manuf] +
         [make_card(r, "pipeline") for r in recs["pipeline"]] +
         [make_card(r, "dose") for r in recs["dose"]])
cards.sort(key=lambda c: c["score"], reverse=True)
for c in cards:
    c["emoji"] = med_emoji(c)
    c["vemoji"], c["vlabel"] = vertical(c)
    c["summary"] = summarize(c)


# ---------- render ----------

KIND_LABEL = {"rfp": "RFP", "lead": "FDA approval", "pipeline": "Clinical Trials", "dose": "DOSE trial",
              "manuf": "Lead List"}
KIND_DOT = {"rfp": "🔵", "lead": "🟢", "manuf": "🟠", "pipeline": "🟣", "dose": "🔴"}
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
    to_email = ""
    ct = c.get("contact") or {}
    if ct.get("emails"):
        to_email = ct["emails"][0]
    if not to_email:
        for l in c["links"]:
            if str(l.get("url", "")).startswith("mailto:"):
                to_email = l["url"][7:]
                break
    pitch_block = ""
    p = c.get("pitch")
    pdf = (p or {}).get("pdf", "")
    if p:
        safeid = "".join(ch for ch in c["id"] if ch.isalnum())
        outreach = p.get("outreach", "")
        if CALENDLY:
            outreach = outreach.rstrip() + f"\n\nBook 20 minutes: {CALENDLY}"
        pdf_link = f'<a class="dl" href="file://{e(pdf)}" target="_blank">📄 pitch PDF</a>' if pdf else ""
        gmail_btn = f'<button class="gmailb" data-to="{e(to_email)}" data-t="o{safeid}" title="Open a Gmail draft with this message">✉ Open in Gmail</button>'
        cal_btn = f'<a class="callink" href="{e(CALENDLY)}" target="_blank">📅 Book 20 minutes ↗</a>' if CALENDLY else ""
        pitch_block = ('<details class="pitchd"><summary>✉ Outreach draft + pitch PDF</summary>'
                       f'<textarea class="outbox" id="o{safeid}" readonly>{e(outreach)}</textarea>'
                       f'<div class="prow"><button class="copyb" data-t="o{safeid}">⧉ Copy</button>{gmail_btn}{cal_btn}{pdf_link}</div>'
                       '<div class="muted" style="margin-top:5px">Gmail opens pre-filled — attach the PDF manually (a compose link can’t auto-attach files).</div>'
                       '</details>')
    steps = "".join(f"<li>{s}</li>" for s in c["steps"])
    links = "".join(f'<a class="dl" href="{e(l["url"])}" target="_blank">{l["label"]}</a>' for l in c["links"]) \
        or '<span class="muted">contact via notice</span>'
    body = f'<details class="bd"><summary>RFP details</summary><div class="body">{e(c["body"])}</div></details>' if c["body"] else ""
    ddl = e(str(c["deadline"])[:10]) if c["deadline"] else ""
    sortdate = str(c["deadline"] or c["award_date"] or "")[:10]
    date_lbl = {"rfp": "Due", "lead": "Approved", "dose": "Trial", "pipeline": "Launch"}.get(c["kind"], "")
    date_chip = f'<div class="kc-date">📅 {date_lbl} {e(sortdate)}</div>' if sortdate else ""
    org_line = f'<div class="kc-org">{e(c["org"])}</div>' if c["org"] and c["org"] != c["title"] else ""
    return f"""<div class="kcard" draggable="true" data-id="{e(c['id'])}" data-kind="{c['kind']}" data-score="{e(c['score'])}" data-date="{e(sortdate)}" data-deadline="{ddl}" data-text="{e((c['title']+' '+c['org']+' '+' '.join(c['drugs'])).lower())}">
      <div class="kc-load"></div>
      <span class="kc-corner t-{c['kind']}" title="{KIND_LABEL[c['kind']]}"></span>
      <div class="kc-top"><span class="kc-topr"><button class="expand" title="Expand / collapse">▾</button><button class="fu" data-id="{e(c['id'])}" title="Set follow-up">📞</button><button class="pdfbtn2" data-id="{e(c['id'])}" data-pdf="{e(pdf)}" title="Custom pitch PDF">📄</button><button class="addc" data-id="{e(c['id'])}" title="Contact &amp; details">+</button><span class="kc-score" title="Best-fit score">{e(c['score'])}</span></span></div>
      <div class="kc-headline"><span class="kc-emoji">{c['emoji']}</span><h4>{e(c['title'])}</h4><span class="vbadge2 inline">{c['vemoji']} {e(c['vlabel'])}</span></div>
      {org_line}
      {date_chip}
      <div class="kc-followup"></div>
      <div class="kc-summary">{e(c['summary'])}</div>
      <div class="kc-more">
        {f'<div class="vrow">{vbadge}</div>' if vbadge else ''}
        {due_line}
        <div class="kc-touch"></div>
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
      </div>
    </div>"""


COLS = [("new", "New Lead"), ("reviewing", "In Progress"), ("preparing", "Preparing"),
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
n_manuf = sum(1 for c in cards if c["kind"] == "manuf")

# per-card contact + details payload for the "+" popup (embedded as JSON, gitignored output)
contacts_map = {c["id"]: {
    "title": c["title"], "org": c["org"], "kind": c["kind"], "kindLabel": KIND_LABEL[c["kind"]],
    "source": c["source"], "products": c.get("products", ""), "drugs": c.get("drugs", []),
    "contact": c.get("contact", {}) or {}, "fit": c["fit"], "score": c["score"],
    "links": c["links"], "people": c.get("people", []), "vlabel": c.get("vlabel", ""),
} for c in cards}
contacts_json = json.dumps(contacts_map, ensure_ascii=False).replace("</", "<\\/")
sync_url_js = json.dumps(SYNC_URL)
sync_token_js = json.dumps(SYNC_TOKEN)
col_options = "".join(f'<option value="{cid}">{e(name)}</option>' for cid, name in COLS)
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
.t-manuf{{background:rgba(240,140,60,.16);color:#f0913c}}
.kc-topr{{display:flex;align-items:center;gap:8px;margin-left:auto}}
.addc{{background:var(--inset);border:1px solid var(--line);color:var(--acc);width:22px;height:22px;border-radius:6px;cursor:pointer;font-size:16px;line-height:1;display:flex;align-items:center;justify-content:center;padding:0}}
.addc:hover{{border-color:var(--acc);background:var(--panel)}}
.cmodal{{position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center}}
.cmodal[hidden]{{display:none}}
.cback{{position:absolute;inset:0;background:rgba(4,8,16,.62)}}
.cbox{{position:relative;background:var(--panel);border:1px solid var(--line);border-radius:14px;max-width:520px;width:92%;max-height:86vh;overflow:auto;padding:20px 22px;box-shadow:0 24px 70px rgba(0,0,0,.55)}}
.cx{{position:absolute;top:11px;right:13px;background:transparent;border:none;color:var(--mut);font-size:16px;cursor:pointer}}
.cbody h3{{margin:2px 0 0;font-size:18px}}
.cm-kind{{font-size:11px;font-weight:700;letter-spacing:.05em;color:var(--mut);text-transform:uppercase}}
.cm-sec{{margin-top:14px}}.cm-lbl{{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin-bottom:5px}}
.cm-owner{{display:inline-block;background:rgba(77,139,240,.16);color:var(--acc);border-radius:6px;padding:2px 9px;font-size:12.5px;font-weight:600}}
.cm-person{{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:7px 10px;background:var(--inset);border:1px solid var(--line);border-radius:8px;margin:5px 0;font-size:12.5px}}
.cm-person a{{font-size:11.5px;text-decoration:none;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:3px 8px;margin-left:5px;white-space:nowrap}}
.cm-person a:hover{{border-color:var(--acc)}}
.cm-notes{{white-space:pre-wrap;font-size:12.5px;background:var(--inset);border:1px solid var(--line);border-radius:8px;padding:9px 11px;line-height:1.5}}
.cm-chips{{display:flex;flex-wrap:wrap;gap:5px}}.cm-chip{{font-size:11.5px;background:var(--inset);border:1px solid var(--line);border-radius:5px;padding:2px 8px}}
.cm-links{{display:flex;flex-wrap:wrap;gap:6px}}
.legend{{background:var(--inset);border:1px solid var(--line);color:var(--acc);width:22px;height:22px;border-radius:50%;cursor:pointer;font-size:13px;line-height:1;padding:0;vertical-align:middle;margin-left:4px}}
.legend:hover{{border-color:var(--acc)}}
.kc-touch{{display:none;font-size:11px;color:var(--warn);margin:1px 0 5px}}
.cm-touch{{font-size:12.5px;margin-bottom:8px;color:var(--txt)}}.cm-touch b{{color:var(--warn)}}
.cm-ta{{width:100%;height:56px;background:var(--inset);border:1px solid var(--line);border-radius:8px;color:var(--txt);padding:8px;font-size:12.5px;resize:vertical;margin-bottom:6px;font-family:inherit}}
.cm-thread{{margin-top:10px;display:flex;flex-direction:column;gap:6px}}
.cm-note{{background:var(--inset);border:1px solid var(--line);border-radius:8px;padding:7px 10px;font-size:12.5px;white-space:pre-wrap}}
.cm-note-ts{{font-size:10.5px;color:var(--mut);margin-bottom:2px}}
.cm-empty{{font-size:12px;color:var(--mut)}}
.cm-who{{width:100%;background:var(--inset);border:1px solid var(--line);border-radius:8px;color:var(--txt);padding:7px 9px;font-size:12.5px;margin-bottom:6px;font-family:inherit}}
.cm-by{{color:var(--acc);font-weight:600}}
.cm-sync{{font-size:11px;color:var(--ok)}}.cm-sync.off{{color:var(--mut)}}
.lg{{margin:5px 0;font-size:12.5px;display:flex;align-items:center;gap:7px}}.lg .tag{{flex:0 0 auto}}
/* condensed / expandable tiles */
.kcard{{position:relative;overflow:hidden}}
.kc-headline{{display:flex;align-items:center;gap:7px;margin:6px 0 2px}}
.kc-emoji{{font-size:16px;line-height:1;flex:0 0 auto}}
.kcard h4{{margin:0;font-size:14px;line-height:1.25}}
.kc-badges{{display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin:5px 0}}
.vbadge2{{font-size:10.5px;background:var(--inset);border:1px solid var(--line);border-radius:5px;padding:2px 7px;color:var(--mut)}}
.pdfbtn{{font-size:10.5px;background:rgba(240,140,60,.16);border:1px solid var(--line);border-radius:5px;padding:2px 8px;color:#f0913c;text-decoration:none;font-weight:600}}
.pdfbtn:hover{{border-color:#f0913c}}
.kc-summary{{font-size:11.5px;color:var(--mut);line-height:1.4;margin:5px 0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.kcard.open .kc-summary{{-webkit-line-clamp:none;overflow:visible}}
.expand{{background:var(--inset);border:1px solid var(--line);color:var(--mut);width:22px;height:22px;border-radius:6px;cursor:pointer;font-size:12px;line-height:1;padding:0;transition:transform .15s}}
.expand:hover{{border-color:var(--acc);color:var(--acc)}}
.kcard.open .expand{{transform:rotate(180deg)}}
.kc-more{{display:none;margin-top:6px;border-top:1px solid var(--line);padding-top:8px}}
.kcard.open .kc-more{{display:block}}
.kcard.enriching{{border-color:var(--acc);animation:pulseb 1.4s ease-in-out infinite}}
@keyframes pulseb{{0%,100%{{box-shadow:0 0 0 1px var(--acc)}}50%{{box-shadow:0 0 0 3px var(--acc)}}}}
.kc-load{{display:none;height:3px;background:linear-gradient(90deg,transparent,var(--acc),transparent);background-size:200% 100%;animation:load 1.1s linear infinite;margin:-11px -11px 7px;border-radius:10px 10px 0 0}}
.kcard.enriching .kc-load{{display:block}}
@keyframes load{{0%{{background-position:200% 0}}100%{{background-position:-200% 0}}}}
.sortsel{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 10px;color:var(--txt);font-size:12.5px;cursor:pointer}}
.collapseAll{{background:transparent;border:1px solid var(--line);color:var(--mut);border-radius:8px;padding:6px 12px;cursor:pointer;font-size:12.5px}}
.addlead{{background:var(--acc);border:none;color:#fff;border-radius:8px;padding:6px 14px;cursor:pointer;font-size:12.5px;font-weight:700}}
.addlead:hover{{filter:brightness(1.08)}}
.al-row2{{display:flex;gap:8px}}.al-row2>div{{flex:1}}.al-row2 input{{flex:1;min-width:0}}
.al-contact{{position:relative;border:1px solid var(--line);border-radius:8px;padding:9px;margin-bottom:8px;background:var(--inset)}}
.al-rm{{position:absolute;top:6px;right:6px;background:transparent;border:none;color:var(--mut);cursor:pointer;font-size:12px}}
.addc-row{{background:transparent;border:1px dashed var(--line);color:var(--acc);border-radius:8px;padding:6px 12px;cursor:pointer;font-size:12.5px;width:100%}}
.addc-row:hover{{border-color:var(--acc)}}
.cm-note-hint{{font-size:11.5px;color:var(--mut);background:var(--inset);border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:12px 0}}
.vbadge2.enrich{{background:rgba(77,139,240,.16);color:var(--acc);border-color:var(--acc)}}
/* header layout — actions on the title row (no wasted row) */
.hrow{{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap}}
.hactions{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.htitle h1{{margin:0 0 3px}}
/* kind dot tag + inline vertical badge */
.tag{{background:none!important;border:none;padding:0;font-size:12px;line-height:1}}
.kc-headline{{flex-wrap:wrap}}
.vbadge2.inline{{margin-left:auto}}
/* colored filter pills (light shade of each color) */
.pill.t-rfp{{background:rgba(77,139,240,.13)}}
.pill.t-lead{{background:rgba(47,191,113,.13)}}
.pill.t-manuf{{background:rgba(240,140,60,.13)}}
.pill.t-pipeline{{background:rgba(176,125,240,.13)}}
.pill.t-dose{{background:rgba(239,91,107,.13)}}
/* follow-up chip + rotting tile color */
.kc-followup{{display:none;font-size:11.5px;font-weight:700;margin:3px 0 2px}}
.kc-followup.fu-overdue{{color:var(--bad)}}.kc-followup.fu-soon{{color:var(--warn)}}.kc-followup.fu-ok{{color:var(--ok)}}
.kcard.fu-overdue{{border-left:3px solid var(--bad)}}
.kcard.fu-soon{{border-left:3px solid var(--warn)}}
.kcard.fu-ok{{border-left:3px solid var(--ok)}}
/* small icon buttons (phone / pdf) */
.fu,.pdfbtn2{{background:var(--inset);border:1px solid var(--line);color:var(--mut);width:22px;height:22px;border-radius:6px;cursor:pointer;font-size:12px;line-height:1;padding:0;display:inline-flex;align-items:center;justify-content:center}}
.fu:hover,.pdfbtn2:hover{{border-color:var(--acc)}}
.gmailb{{background:var(--acc);border:none;color:#fff;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:11.5px;font-weight:600}}
/* higher-contrast tile buttons (dark mode was too dim) */
.kc-topr .expand,.kc-topr .fu,.kc-topr .pdfbtn2,.kc-topr .addc{{color:var(--txt);border:1px solid var(--mut);background:var(--panel)}}
.kc-topr button:hover{{border-color:var(--acc);color:var(--acc)}}
/* corner kind tab (color-coded, folded-corner look) */
.kc-corner{{position:absolute;right:0;bottom:0;width:0;height:0;border-left:15px solid transparent;border-bottom:15px solid var(--line)}}
.kc-corner.t-rfp{{border-bottom-color:#4d8bf0}}
.kc-corner.t-lead{{border-bottom-color:#2fbf71}}
.kc-corner.t-manuf{{border-bottom-color:#f0913c}}
.kc-corner.t-pipeline{{border-bottom-color:#b07df0}}
.kc-corner.t-dose{{border-bottom-color:#ef5b6b}}
/* intrinsic date chip on the condensed tile */
.kc-date{{font-size:11px;color:var(--mut);font-weight:600;margin:2px 0}}
.callink{{color:var(--acc);font-weight:600;font-size:11.5px;text-decoration:underline}}
.fu-note{{width:100%;height:70px;background:var(--inset);border:1px solid var(--line);border-radius:8px;color:var(--txt);padding:8px;font-size:12.5px;resize:vertical;margin-bottom:6px;font-family:inherit}}
.cm-person{{align-items:flex-start}}
.cm-actions{{display:flex;gap:6px;flex-shrink:0;white-space:nowrap}}
</style></head><body>
<header>
  <div class="hrow">
    <div class="htitle">
      <h1>Perigon RFP Board <button class="legend" id="legendBtn" title="Legend — what am I looking at?">&#9432;</button></h1>
      <div class="sub">Discover → qualify → submit · {len(cards)} opportunities · generated {updated}{(' · ' + sam_note) if sam_note else ''}</div>
    </div>
    <div class="hactions">
      <button class="addlead" title="Add a new lead">＋ Add Lead</button>
      <button class="export" title="Download board decisions (incl. out-of-scope) as JSON">⬇ Export</button>
      <button class="theme" title="Toggle light/dark">🌙 Dark</button>
      <button class="reset">Reset board</button>
    </div>
  </div>
  <div class="toolbar">
    <span class="stat"><b>{n_rfp}</b> RFPs · <b>{n_lead}</b> FDA · <b>{n_manuf}</b> leads · <b>{n_pipe}</b> trials · <b>{n_dose}</b> DOSE · <b>{readiness}%</b> docs ready</span>
    <span class="pill on t-all" data-f="all">⚫ All</span>
    <span class="pill t-rfp" data-f="rfp">🔵 RFPs</span>
    <span class="pill t-lead" data-f="lead">🟢 FDA approvals</span>
    <span class="pill t-manuf" data-f="manuf">🟠 Lead List</span>
    <span class="pill t-pipeline" data-f="pipeline">🟣 Clinical Trials</span>
    <span class="pill t-dose" data-f="dose">🔴 DOSE</span>
    <input class="search" placeholder="search drug / manufacturer / program…">
    <select class="sortsel" title="Sort tiles within each column">
      <option value="score">Sort: Best fit</option>
      <option value="followup">Sort: Follow-up date</option>
      <option value="date">Sort: Upcoming date</option>
      <option value="fresh">Sort: Freshest touch</option>
    </select>
    <button class="collapseAll" title="Collapse or expand all tiles">⤢ Expand all</button>
  </div>
</header>
<div class="board">{col_html}</div>
<div id="stash" style="display:none">{cards_html}</div>
<div id="cmodal" class="cmodal" hidden><div class="cback"></div><div class="cbox"><button class="cx" title="Close">✕</button><div class="cbody"></div></div></div>
<template id="legendTpl">
  <div class="cm-kind">Legend &middot; how to use this board</div><h3>What am I looking at?</h3>
  <div class="cm-sec"><div class="cm-lbl">A tile</div><div>Each card is one opportunity or lead. Drag it between columns as it moves through your pipeline — your columns save in this browser.</div></div>
  <div class="cm-sec"><div class="cm-lbl">Card type (colored tag, top-left)</div>
    <div class="lg"><span class="tag t-rfp">RFP</span> government / agency solicitation</div>
    <div class="lg"><span class="tag t-lead">FDA APPROVAL</span> new drug approval → LDD / hub target</div>
    <div class="lg"><span class="tag t-manuf">LEAD LIST</span> manufacturer BD contact (your list)</div>
    <div class="lg"><span class="tag t-pipeline">PIPELINE</span> phase-3 drug nearing launch</div>
    <div class="lg"><span class="tag t-dose">DOSE · TRIAL</span> oral trial needing pharmacy support</div>
  </div>
  <div class="cm-sec"><div class="cm-lbl">Score (number, top-right)</div><div>Priority score — higher = more relevant / more ready to pursue. RFPs add a solicitation-PDF fit verdict; Lead-List cards score on named product, contact, assigned owner and momentum in your notes.</div></div>
  <div class="cm-sec"><div class="cm-lbl">The + button</div><div>Opens contact &amp; details: contact name(s), LinkedIn lookup, email, the Perigon owner, official medication PDFs, and a place to log touches and add notes.</div></div>
  <div class="cm-sec"><div class="cm-lbl">🕓 Last touch</div><div>The date of your most recent note on a card. Add notes in the + panel — the thread and last-touch date save in this browser.</div></div>
  <div class="cm-sec"><div class="cm-lbl">Columns</div><div>New → Reviewing → Preparing → Submitted → Closed → Past Deadline → Out of Scope. “✕ Out of scope” quick-files a card.</div></div>
  <div class="cm-sec"><div class="cm-lbl">Filter &amp; search</div><div>Pills filter by type; search matches drug / manufacturer / program. ⬇ Export downloads your column decisions; 🌙 toggles theme.</div></div>
  <div class="cm-sec"><div class="cm-notes">Everything you change — columns, notes, touches, theme — is stored locally in this browser. It is not shared between devices or teammates.</div></div>
</template>
<div id="addmodal" class="cmodal" hidden><div class="cback" data-close="add"></div><div class="cbox">
  <button class="cx" data-close="add" title="Close">✕</button>
  <div class="cm-kind">New lead</div><h3>Add a lead</h3>
  <div class="cm-sec"><div class="cm-lbl">Company name *</div><input id="al-company" class="cm-who" placeholder="e.g. Salix Pharmaceuticals"></div>
  <div class="al-row2">
    <div><div class="cm-lbl">Drug name</div><input id="al-drug" class="cm-who" placeholder="e.g. Xifaxan"></div>
    <div><div class="cm-lbl">Program name</div><input id="al-program" class="cm-who" placeholder="e.g. IBS-D program"></div>
  </div>
  <div class="cm-sec"><div class="cm-lbl">Add to column</div><select id="al-col" class="sortsel" style="width:100%">{col_options}</select></div>
  <div class="cm-sec"><div class="cm-lbl">Contacts</div><div id="al-contacts"></div>
    <button class="addc-row" id="al-addcontact">＋ Add a contact</button></div>
  <div class="cm-note-hint">On save this lands as a <b>blue “enriching” tile</b> in the chosen column — queued to auto-fill blank emails/phones/LinkedIn and generate a custom pitch PDF once the enrichment backend is live.</div>
  <div class="prow"><button class="copyb" id="al-save">Save lead</button><button class="collapseAll" data-close="add">Cancel</button></div>
</div></div>
<template id="al-contactTpl">
  <div class="al-contact">
    <button class="al-rm" title="Remove contact">✕</button>
    <div class="al-row2"><input class="al-first cm-who" placeholder="First name"><input class="al-last cm-who" placeholder="Last name"></div>
    <input class="al-email cm-who" placeholder="Email (leave blank to enrich)">
    <div class="al-row2"><input class="al-phone cm-who" placeholder="Phone (leave blank to enrich)"><input class="al-linkedin cm-who" placeholder="LinkedIn URL (optional)"></div>
  </div>
</template>
<div id="fumodal" class="cmodal" hidden><div class="cback" data-close="fu"></div><div class="cbox">
  <button class="cx" data-close="fu" title="Close">✕</button>
  <div class="cm-kind">📞 Follow-up</div><h3 id="fu-title">Set follow-up</h3>
  <div class="cm-sec"><div class="cm-lbl">Follow-up date</div><input type="date" id="fu-date" class="cm-who" onclick="this.showPicker&&this.showPicker()"></div>
  <div class="cm-sec"><div class="cm-lbl">Note (added to the activity log)</div>
    <textarea id="fu-note" class="fu-note" placeholder="e.g. Left VM — resend Ibsrela one-pager, revisit after ASX…"></textarea>
    <input id="fu-who" class="cm-who" placeholder="Your name / initials"></div>
  <div class="cm-note-hint">Sets a 📞 MM/DD (±days) chip on the tile, color-codes it as it ages (rots), and logs the date + your note to the activity log. Sort by <b>Follow-up date</b> to work your day.</div>
  <div class="prow"><button class="copyb" id="fu-save">Set follow-up</button><button class="collapseAll" id="fu-clear">Clear</button></div>
</div></div>
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
function counts(){{cols.forEach(c=>{{var v=0;document.querySelectorAll('#col-'+c+' .kcard').forEach(k=>{{if(k.style.display!=='none')v++;}});document.getElementById('cc-'+c).textContent=v;}})}}
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
  counts();
}}
// expand / collapse a tile
document.addEventListener('click',e=>{{var b=e.target.closest('.expand');if(!b)return;e.stopPropagation();var card=b.closest('.kcard');if(card)card.classList.toggle('open');}});
// sort tiles within each column
function sortCards(mode){{
  cols.forEach(col=>{{
    var body=document.querySelector('#col-'+col+' .col-body');if(!body)return;
    var arr=Array.prototype.slice.call(body.querySelectorAll('.kcard'));
    arr.sort((a,b)=>{{
      if(mode==='score')return (+b.dataset.score||0)-(+a.dataset.score||0);
      if(mode==='followup'){{var fa=(noteData(a.dataset.id).followUp)||'9999-99',fb=(noteData(b.dataset.id).followUp)||'9999-99';return fa<fb?-1:(fa>fb?1:0);}}
      if(mode==='date'){{var da=a.dataset.date||'9999-99',db=b.dataset.date||'9999-99';return da<db?-1:(da>db?1:0);}}
      var ta=(noteData(a.dataset.id).touch)||'',tb=(noteData(b.dataset.id).touch)||'';
      return tb<ta?-1:(tb>ta?1:0);
    }});
    arr.forEach(c=>body.appendChild(c));
  }});
}}
document.querySelector('.sortsel').addEventListener('change',e=>sortCards(e.target.value));
// collapse / expand all
var allOpen=false;
document.querySelector('.collapseAll').addEventListener('click',function(){{
  allOpen=!allOpen;
  document.querySelectorAll('.kcard').forEach(c=>c.classList.toggle('open',allOpen));
  this.textContent=allOpen?'▴ Collapse all':'⤢ Expand all';
}});
document.querySelectorAll('.pill').forEach(p=>p.addEventListener('click',()=>{{
  document.querySelectorAll('.pill').forEach(x=>x.classList.remove('on'));p.classList.add('on');curF=p.dataset.f;applyFilter();
}}));
document.querySelector('.search').addEventListener('input',e=>{{curQ=e.target.value.toLowerCase().trim();applyFilter()}});
document.querySelector('.reset').addEventListener('click',()=>{{if(confirm('Reset board to defaults? (clears your saved columns)')){{localStorage.removeItem(KEY);location.reload()}}}});
// quick "out of scope" button on each card
document.addEventListener('click',e=>{{var b=e.target.closest('.oos');if(b){{e.stopPropagation();moveTo(b.dataset.id,'outofscope');}}}});
// copy outreach message to clipboard
document.querySelectorAll('.copyb').forEach(b=>b.addEventListener('click',e=>{{
  e.stopPropagation();const t=document.getElementById(b.dataset.t);
  const done=()=>{{b.textContent='✓ Copied';setTimeout(()=>b.textContent='⧉ Copy message',1500)}};
  if(navigator.clipboard){{navigator.clipboard.writeText(t.value).then(done).catch(()=>{{t.select();document.execCommand('copy');done()}})}}
  else{{t.select();document.execCommand('copy');done()}}
}}));
// export the team's decisions (columns incl. out-of-scope) so we can learn/refine filters
function csvCell(v){{v=(v==null?'':String(v));return /[",\\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v;}}
const COL_LABEL={{new:'New Lead',reviewing:'In Progress',preparing:'Preparing',submitted:'Submitted',closed:'Closed',pastdue:'Past Deadline',outofscope:'Out of Scope'}};
document.querySelector('.export').addEventListener('click',()=>{{
  const s=st();
  const head=['Company','Type','Vertical','Stage','Score','Product / drug','Contacts','Emails','Phones','LinkedIn','Perigon lead','Follow-up','Days to follow-up','Last touch','Upcoming date','Fit','Latest note'];
  const rows=[head];
  document.querySelectorAll('.kcard').forEach(c=>{{
    const id=c.dataset.id,cc=CONTACTS[id]||{{}},ct=cc.contact||{{}},nd=noteData(id);
    const people=cc.people||[];
    const names=people.length?people.map(p=>((p.first||'')+' '+(p.last||'')).trim()).filter(Boolean):(ct.names||[]);
    const emails=people.length?people.map(p=>p.email).filter(Boolean):(ct.emails||[]);
    const phones=people.length?people.map(p=>p.phone).filter(Boolean):((ct.notes||'').match(/[0-9][0-9\\-().\\s]{{7,}}/g)||[]);
    const lis=(cc.links||[]).filter(l=>/linkedin/.test(l.url||'')).map(l=>l.url);
    const fu=nd.followUp||'';var dtf=fu?daysUntil(fu):'';
    const lastNote=(nd.log&&nd.log.length)?nd.log[nd.log.length-1].text:'';
    rows.push([cc.title||'',cc.kindLabel||c.dataset.kind,(cc.vlabel||''),COL_LABEL[(s[id]&&s[id].col)||defCol(c)]||'',c.dataset.score||'',
      (cc.products||(cc.drugs||[]).join(', ')),names.join(' | '),emails.join(' | '),phones.join(' | '),lis.join(' | '),
      (ct.owner||''),fu,dtf,(nd.touch||''),c.dataset.date||'',(cc.fit||''),lastNote].map(csvCell));
  }});
  const csv=rows.map(r=>r.join(',')).join('\\n');
  const blob=new Blob([csv],{{type:'text/csv'}});
  var d=new Date();var stamp=d.getFullYear()+pad(d.getMonth()+1)+pad(d.getDate());
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='perigon-pipeline-'+stamp+'.csv';a.click();
}});
// contact / details popup ("+" on each card)
const CONTACTS={contacts_json};
const modal=document.getElementById('cmodal');
const cbody=modal.querySelector('.cbody');
function esc(s){{return (s==null?'':String(s)).replace(/[&<>"]/g,m=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[m]))}}
function liLink(name,org){{return 'https://www.linkedin.com/search/results/people/?keywords='+encodeURIComponent((name+' '+(org||'')).trim())}}
function openContact(id){{
  const c=CONTACTS[id]; if(!c)return; const ct=c.contact||{{}};
  let h='<div class="cm-kind">'+esc(c.kindLabel)+' · '+esc(c.source)+' · score '+esc(c.score)+'</div><h3>'+esc(c.title)+'</h3>';
  if(c.org&&c.org!==c.title)h+='<div class="kc-org">'+esc(c.org)+'</div>';
  if(c.fit)h+='<div class="cm-sec"><div class="cm-lbl">Fit</div><div>'+esc(c.fit)+'</div></div>';
  if(ct.owner)h+='<div class="cm-sec"><div class="cm-lbl">Perigon lead</div><span class="cm-owner">'+esc(ct.owner)+'</span></div>';
  const names=ct.names||[], emails=ct.emails||[], people=c.people||[];
  if(people.length){{
    h+='<div class="cm-sec"><div class="cm-lbl">Contacts</div>';
    people.forEach(function(p){{
      var nm=((p.first||'')+' '+(p.last||'')).trim()||'(contact)';
      var li=p.linkedin||liLink(nm,c.org);
      h+='<div class="cm-person"><div><b>'+esc(nm)+'</b>'+(p.email?'<div class="muted">'+esc(p.email)+'</div>':'<div class="muted">email — enriching</div>')+(p.phone?'<div class="muted">'+esc(p.phone)+'</div>':'')+'</div><span class="cm-actions">'+(p.email?'<a href="mailto:'+esc(p.email)+'">✉ Email</a>':'')+'<a href="'+esc(li)+'" target="_blank">LinkedIn ↗</a></span></div>';
    }});
    h+='</div>';
  }} else if(names.length||emails.length){{
    h+='<div class="cm-sec"><div class="cm-lbl">Contact</div>';
    names.forEach(n=>{{h+='<div class="cm-person"><span>'+esc(n)+'</span><a href="'+esc(liLink(n,c.org))+'" target="_blank">LinkedIn ↗</a></div>';}});
    emails.forEach(m=>{{h+='<div class="cm-person"><span>'+esc(m)+'</span><a href="mailto:'+esc(m)+'">✉ Email</a></div>';}});
    h+='</div>';
  }}
  const chips=(c.drugs&&c.drugs.length)?c.drugs:(c.products?[c.products]:[]);
  if(chips.length)h+='<div class="cm-sec"><div class="cm-lbl">Products / focus</div><div class="cm-chips">'+chips.map(x=>'<span class="cm-chip">💊 '+esc(x)+'</span>').join('')+'</div></div>';
  if(ct.notes)h+='<div class="cm-sec"><div class="cm-lbl">Notes</div><div class="cm-notes">'+esc(ct.notes)+'</div></div>';
  if(c.links&&c.links.length)h+='<div class="cm-sec"><div class="cm-lbl">Links</div><div class="cm-links">'+c.links.map(l=>'<a class="dl" href="'+esc(l.url)+'" target="_blank">'+esc(l.label)+'</a>').join('')+'</div></div>';
  if(!people.length&&!names.length&&!emails.length&&!ct.owner&&!(c.links&&c.links.length))h+='<div class="cm-sec"><div class="cm-notes">No stored contact — reach out via the notice/source.</div></div>';
  var nd=noteData(id);
  h+='<div class="cm-sec"><div class="cm-lbl">Activity / touch log</div>';
  h+='<div class="cm-touch">Last touch: <b id="cm-lt">'+(nd.touch||'—')+'</b>'+(syncEnabled?' <span class="cm-sync">· shared</span>':' <span class="cm-sync off">· this browser only</span>')+'</div>';
  h+='<input id="cm-who" class="cm-who" placeholder="Your name / initials" value="'+esc(localStorage.getItem(WHO_KEY)||'')+'">';
  h+='<textarea id="cm-note" class="cm-ta" placeholder="Add a note — call, email, meeting outcome…"></textarea>';
  h+='<button class="copyb" id="cm-add">+ Add note (logs today)</button>';
  h+='<div class="cm-thread" id="cm-thread">'+renderThread(nd.log)+'</div></div>';
  cbody.innerHTML=h; modal.hidden=false;
  var add=document.getElementById('cm-add');
  if(add)add.addEventListener('click',function(){{
    var ta=document.getElementById('cm-note'),who=document.getElementById('cm-who');
    addNote(id,ta.value,who?who.value:'');ta.value='';
    var d=noteData(id);document.getElementById('cm-thread').innerHTML=renderThread(d.log);
    document.getElementById('cm-lt').textContent=d.touch||'—';
  }});
}}
function closeContact(){{modal.hidden=true;}}
// per-card notes + last-touch date (localStorage cache; optional shared backend)
const NKEY='rfpNotes';
const SYNC_URL={sync_url_js},SYNC_TOKEN={sync_token_js},WHO_KEY='rfpWho';
const syncEnabled=!!(SYNC_URL&&SYNC_TOKEN);
function nst(){{try{{return JSON.parse(localStorage.getItem(NKEY)||'{{}}')}}catch(e){{return {{}}}}}}
function nsave(o){{localStorage.setItem(NKEY,JSON.stringify(o))}}
function noteData(id){{var o=nst();return o[id]||{{touch:'',log:[]}}}}
function pad(n){{return (n<10?'0':'')+n}}
function nowStamp(){{var d=new Date();return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+' '+pad(d.getHours())+':'+pad(d.getMinutes())}}
function addNote(id,text,who){{text=(text||'').trim();if(!text)return;who=(who||'').trim();if(who)localStorage.setItem(WHO_KEY,who);var o=nst();var en=o[id]||{{touch:'',log:[]}};var note={{ts:nowStamp(),text:text,by:who}};en.log.push(note);en.touch=note.ts.slice(0,10);o[id]=en;nsave(o);paintTouch(id);if(syncEnabled)postNote(id,note).catch(function(){{}});}}
function renderThread(log){{if(!log||!log.length)return '<div class="cm-empty">No notes yet — add your first touch above.</div>';return log.slice().reverse().map(function(n){{var by=n.by?('<span class="cm-by">'+esc(n.by)+'</span> · '):'';return '<div class="cm-note"><div class="cm-note-ts">'+by+esc(n.ts)+'</div><div>'+esc(n.text)+'</div></div>';}}).join('');}}
function paintTouch(id){{var card=document.querySelector('.kcard[data-id="'+CSS.escape(id)+'"]');if(!card)return;var el=card.querySelector('.kc-touch');if(!el)return;var t=noteData(id).touch;if(t){{el.textContent='🕓 last touch '+t;el.style.display='';}}else{{el.style.display='none';}}}}
function paintAllTouches(){{document.querySelectorAll('.kcard').forEach(function(c){{paintTouch(c.dataset.id);}});}}
// ---- follow-up dates (rotting) + pdf + gmail ----
function daysUntil(d){{var t=new Date(d+'T00:00:00');var n=new Date();n.setHours(0,0,0,0);return Math.round((t-n)/86400000);}}
function fmtMMDD(d){{var p=d.split('-');return p[1]+'/'+p[2];}}
function paintFollowUp(id){{
  var card=document.querySelector('.kcard[data-id="'+CSS.escape(id)+'"]');if(!card)return;
  var el=card.querySelector('.kc-followup');var fu=(noteData(id).followUp)||'';
  card.classList.remove('fu-overdue','fu-soon','fu-ok');
  if(!fu){{if(el){{el.textContent='';el.style.display='none';}}return;}}
  var d=daysUntil(fu);var cls=d<0?'fu-overdue':(d<=3?'fu-soon':'fu-ok');card.classList.add(cls);
  if(el){{el.className='kc-followup '+cls;el.style.display='';el.textContent='📞 '+fmtMMDD(fu)+' ('+(d>=0?'+':'')+d+')';}}
}}
function paintAllFollowUps(){{document.querySelectorAll('.kcard').forEach(function(c){{paintFollowUp(c.dataset.id);}});}}
function saveFollowUp(id,date,noteText,who){{
  who=(who||'').trim();if(who)localStorage.setItem(WHO_KEY,who);
  var o=nst();var en=o[id]||{{touch:'',log:[]}};en.followUp=date;
  var txt='📞 Follow-up '+fmtMMDD(date)+'/'+date.slice(0,4)+((noteText||'').trim()?(' — '+noteText.trim()):'');
  var note={{ts:nowStamp(),text:txt,by:who||localStorage.getItem(WHO_KEY)||''}};
  en.log.push(note);en.touch=note.ts.slice(0,10);o[id]=en;nsave(o);
  paintFollowUp(id);paintTouch(id);
  if(syncEnabled)postNote(id,note).catch(function(){{}});
}}
function clearFollowUp(id){{var o=nst();if(o[id]){{o[id].followUp='';nsave(o);}}paintFollowUp(id);}}
var fuModal=document.getElementById('fumodal');var fuTarget=null;
function openFollowUp(id){{fuTarget=id;var fu=noteData(id).followUp||'';var inp=document.getElementById('fu-date');
  if(fu){{inp.value=fu;}}else{{var d=new Date();d.setDate(d.getDate()+7);inp.value=d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate());}}
  document.getElementById('fu-note').value='';document.getElementById('fu-who').value=localStorage.getItem(WHO_KEY)||'';
  document.getElementById('fu-title').textContent='Follow-up · '+((CONTACTS[id]&&CONTACTS[id].title)||id);
  fuModal.hidden=false;}}
document.getElementById('fu-save').addEventListener('click',function(){{if(fuTarget&&document.getElementById('fu-date').value){{saveFollowUp(fuTarget,document.getElementById('fu-date').value,document.getElementById('fu-note').value,document.getElementById('fu-who').value);}}fuModal.hidden=true;}});
document.getElementById('fu-clear').addEventListener('click',function(){{if(fuTarget)clearFollowUp(fuTarget);fuModal.hidden=true;}});
document.addEventListener('click',function(e){{
  if(e.target.dataset&&e.target.dataset.close==='fu')fuModal.hidden=true;
  var f=e.target.closest&&e.target.closest('.fu');if(f){{e.stopPropagation();openFollowUp(f.dataset.id);}}
  var pb=e.target.closest&&e.target.closest('.pdfbtn2');if(pb){{e.stopPropagation();if(pb.dataset.pdf){{window.open('file://'+pb.dataset.pdf,'_blank');}}else{{alert('The custom pitch PDF for this lead has not been generated yet.\\nPDFs render in the nightly build (or when Chrome/Brave are closed).');}}}}
  var gb=e.target.closest&&e.target.closest('.gmailb');if(gb){{e.stopPropagation();var ta=document.getElementById(gb.dataset.t);var txt=ta?ta.value:'';var subj='';var body=txt;var m=txt.match(/^Subject:\\s*(.*)\\n/);if(m){{subj=m[1];body=txt.slice(m[0].length);}}window.open('https://mail.google.com/mail/?view=cm&fs=1&tf=1&to='+encodeURIComponent(gb.dataset.to||'')+'&su='+encodeURIComponent(subj)+'&body='+encodeURIComponent(body),'_blank');}}
}});
// ---- shared backend sync (no-op unless syncEnabled) ----
function mergeLogs(a,b){{var seen={{}},out=[];(a||[]).concat(b||[]).forEach(function(n){{var k=n.ts+'|'+n.text;if(!seen[k]){{seen[k]=1;out.push(n);}}}});out.sort(function(x,y){{return x.ts<y.ts?-1:(x.ts>y.ts?1:0);}});return out;}}
function postNote(id,n){{return fetch(SYNC_URL+'/activity/'+encodeURIComponent(id),{{method:'POST',headers:{{'Authorization':'Bearer '+SYNC_TOKEN,'Content-Type':'application/json'}},body:JSON.stringify(n)}});}}
async function syncPull(){{if(!syncEnabled)return;try{{var r=await fetch(SYNC_URL+'/activity',{{headers:{{'Authorization':'Bearer '+SYNC_TOKEN}}}});if(!r.ok)return;var server=await r.json();var local=nst();var ids={{}};Object.keys(server).forEach(function(k){{ids[k]=1;}});Object.keys(local).forEach(function(k){{ids[k]=1;}});var pushes=[];Object.keys(ids).forEach(function(id){{var s=server[id]||{{log:[]}},l=local[id]||{{log:[]}};var merged=mergeLogs(s.log,l.log);var touch=merged.length?merged[merged.length-1].ts.slice(0,10):'';local[id]={{touch:touch,log:merged}};var sset={{}};(s.log||[]).forEach(function(n){{sset[n.ts+'|'+n.text]=1;}});(l.log||[]).forEach(function(n){{if(!sset[n.ts+'|'+n.text])pushes.push({{id:id,n:n}});}});}});nsave(local);paintAllTouches();pushes.forEach(function(p){{postNote(p.id,p.n).catch(function(){{}});}});}}catch(e){{}}}}
document.addEventListener('click',e=>{{var b=e.target.closest('.addc');if(b){{e.stopPropagation();openContact(b.dataset.id);}}}});
modal.querySelector('.cx').addEventListener('click',closeContact);
modal.querySelector('.cback').addEventListener('click',closeContact);
document.addEventListener('keydown',e=>{{if(e.key==='Escape')closeContact();}});
var lb=document.getElementById('legendBtn');
if(lb)lb.addEventListener('click',function(){{cbody.innerHTML=document.getElementById('legendTpl').innerHTML;modal.hidden=false;}});
// ---- ADD LEAD ----
const NLKEY='rfpNewLeads';
function nlst(){{try{{return JSON.parse(localStorage.getItem(NLKEY)||'[]')}}catch(e){{return []}}}}
function nlsave(a){{localStorage.setItem(NLKEY,JSON.stringify(a))}}
function nlEmoji(d){{return /inject|syringe|subcut|infus|\\biv\\b/i.test(d||'')?'💉':'💊';}}
const addModal=document.getElementById('addmodal');
const alContacts=document.getElementById('al-contacts');
function alAddContactRow(){{
  var t=document.getElementById('al-contactTpl').content.cloneNode(true);
  alContacts.appendChild(t);
}}
function openAdd(){{alContacts.innerHTML='';alAddContactRow();
  ['al-company','al-drug','al-program'].forEach(function(id){{document.getElementById(id).value='';}});
  addModal.hidden=false;}}
function closeAdd(){{addModal.hidden=true;}}
document.querySelector('.addlead').addEventListener('click',openAdd);
document.getElementById('al-addcontact').addEventListener('click',alAddContactRow);
document.addEventListener('click',function(e){{
  if(e.target.dataset&&e.target.dataset.close==='add')closeAdd();
  var rm=e.target.closest&&e.target.closest('.al-rm');if(rm){{var c=rm.closest('.al-contact');if(c)c.remove();}}
}});
function nlRegister(l){{
  var names=(l.contacts||[]).map(function(c){{return ((c.first||'')+' '+(c.last||'')).trim();}}).filter(Boolean);
  var emails=(l.contacts||[]).map(function(c){{return c.email;}}).filter(Boolean);
  var phones=(l.contacts||[]).map(function(c){{return c.phone;}}).filter(Boolean);
  var links=(l.contacts||[]).map(function(c){{return c.linkedin?{{label:'LinkedIn ↗',url:c.linkedin}}:null;}}).filter(Boolean);
  CONTACTS[l.id]={{title:l.company,org:[l.drug,l.program].filter(Boolean).join(' · '),kind:'manuf',kindLabel:'NEW LEAD',vlabel:'New lead',
    source:'manual',products:l.drug||'',drugs:l.drug?[l.drug]:[],people:l.contacts||[],
    contact:{{names:names,emails:emails,owner:'',notes:'Manually added '+l.created+(phones.length?(' · phones: '+phones.join(', ')):'')+'\\nStatus: queued for enrichment (contact info + LinkedIn + custom PDF).'}},
    fit:'Manually-added lead — pending auto-enrichment.',score:l.score||'—',links:links}};
}}
function nlCard(l){{
  var s=st();var col=(s[l.id]&&s[l.id].col)||l.col||'new';
  var d=document.createElement('div');
  d.className='kcard enriching';d.setAttribute('draggable','true');
  d.dataset.id=l.id;d.dataset.kind='manuf';d.dataset.score=l.score||50;d.dataset.deadline='';
  d.dataset.text=((l.company||'')+' '+(l.drug||'')+' '+(l.program||'')).toLowerCase();
  var sub=[l.drug,l.program].filter(Boolean).join(' · ');
  d.dataset.date=l.created||'';
  d.innerHTML='<div class="kc-load"></div>'
    +'<span class="kc-corner t-manuf" title="New lead"></span>'
    +'<div class="kc-top"><span class="kc-topr"><button class="expand" title="Expand">▾</button><button class="fu" data-id="'+esc(l.id)+'" title="Set follow-up">📞</button><button class="pdfbtn2" data-id="'+esc(l.id)+'" data-pdf="" title="Custom pitch PDF">📄</button><button class="addc" data-id="'+esc(l.id)+'">+</button><span class="kc-score">'+esc(l.score||'—')+'</span></span></div>'
    +'<div class="kc-headline"><span class="kc-emoji">'+nlEmoji(l.drug)+'</span><h4>'+esc(l.company||'New lead')+'</h4><span class="vbadge2 inline enrich">⏳ enriching…</span></div>'
    +(sub?'<div class="kc-org">'+esc(sub)+'</div>':'')
    +(l.created?'<div class="kc-date">📅 Added '+esc(l.created)+'</div>':'')
    +'<div class="kc-followup"></div>'
    +'<div class="kc-summary">New lead — queued to auto-enrich contact info + LinkedIn and generate a custom pitch PDF once enrichment is live.</div>'
    +'<div class="kc-more"><div class="kc-touch"></div><button class="oos" data-id="'+esc(l.id)+'">✕ Out of scope</button></div>';
  return {{el:d,col:col}};
}}
function renderNewLeads(){{
  nlst().forEach(function(l){{
    if(document.querySelector('.kcard[data-id="'+CSS.escape(l.id)+'"]'))return;
    nlRegister(l);var r=nlCard(l);
    var body=document.querySelector('#col-'+r.col+' .col-body')||document.querySelector('#col-new .col-body');
    body.insertBefore(r.el,body.firstChild);
  }});
  counts();
}}
document.getElementById('al-save').addEventListener('click',function(){{
  var company=document.getElementById('al-company').value.trim();
  if(!company){{document.getElementById('al-company').focus();return;}}
  var contacts=[];
  alContacts.querySelectorAll('.al-contact').forEach(function(c){{
    var o={{first:c.querySelector('.al-first').value.trim(),last:c.querySelector('.al-last').value.trim(),
      email:c.querySelector('.al-email').value.trim(),phone:c.querySelector('.al-phone').value.trim(),
      linkedin:c.querySelector('.al-linkedin').value.trim()}};
    if(o.first||o.last||o.email||o.phone||o.linkedin)contacts.push(o);
  }});
  var d=new Date();var created=d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate());
  var l={{id:'new:'+d.getTime().toString(36)+Math.floor(d.getTime()%1000).toString(36),
    company:company,drug:document.getElementById('al-drug').value.trim(),
    program:document.getElementById('al-program').value.trim(),
    col:document.getElementById('al-col').value,contacts:contacts,created:created,score:50,status:'enriching'}};
  var arr=nlst();arr.push(l);nlsave(arr);
  nlRegister(l);var r=nlCard(l);
  var body=document.querySelector('#col-'+r.col+' .col-body');body.insertBefore(r.el,body.firstChild);
  var s=st();s[l.id]=s[l.id]||{{}};s[l.id].col=r.col;save(s);
  closeAdd();counts();
}});
place();
paintAllTouches();
renderNewLeads();
paintAllFollowUps();
if(syncEnabled){{syncPull();setInterval(syncPull,45000);}}
</script></body></html>"""

OUT.write_text(DOC)
print(f"Board written: {OUT}")
print(f"  {len(cards)} cards: {n_rfp} RFP · {n_lead} FDA · {n_pipe} pipeline · readiness {readiness}% · {len(gaps)} gaps")
