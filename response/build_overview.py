#!/usr/bin/env python3
"""
Build overview.pdf — a plain-language summary of the whole RFP Funnel:
what it does, where it pulls from, how it searches, and what filtering it applies.

Reads the real config files + current data so the document stays accurate.
Stdlib + headless Chrome (HTML -> PDF). Usage: python3 build_overview.py
"""
import datetime as dt
import html
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPP = ROOT / "data" / "opportunities"
CFG = ROOT / "config"
OUT_HTML = ROOT / "overview.html"
OUT_PDF = ROOT / "overview.pdf"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def e(s):
    return html.escape(str(s or ""))


def load(p, d):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return d


def count(name):
    return len(load(OPP / f"current_{name}.json", {}).get("records", []))


f_sam = load(CFG / "filters.json", {})
f_fda = load(CFG / "fda_filters.json", {})
f_pipe = load(CFG / "pipeline_filters.json", {})
f_dose = load(CFG / "dose_filters.json", {})
f_state = load(CFG / "state_sources.json", {})
chk = load(CFG / "submission_checklist.json", {"items": []})

c_sam, c_fda, c_pipe, c_dose, c_state = (count(x) for x in ("sam", "fda", "pipeline", "dose", "state"))
total = c_sam + c_fda + c_pipe + c_dose + c_state

# data-source rows: (name, what it finds, source/method, the filter)
SOURCES = [
    ("SAM.gov", "Federal RFPs & solicitations (VA, DLA, IHS, federal facilities)",
     "Get Opportunities API v2 — one bulk pull/day, filter client-side",
     f"NAICS {', '.join(f_sam.get('naics_codes', [])[:5])}…; PSC {', '.join(f_sam.get('psc_codes', []))} (drugs only); "
     f"title keywords ({len(f_sam.get('title_keywords', []))}); score keywords ({len(f_sam.get('score_keywords', {}))}); "
     f"notice types {', '.join(f_sam.get('notice_types_include', [])[:3])}…; min score {f_sam.get('min_score')}. "
     f"~10 req/day quota → {f_sam.get('max_seconds')}s wall-clock cap."),
    ("openFDA (Drugs@FDA)", "Newly approved branded specialty drugs = manufacturers about to need a pharmacy partner",
     "Drugs@FDA API; label API for education text",
     f"Original approvals (ORIG+AP) in last {f_fda.get('lookback_days')}d; DROP generics (brand≈ingredient); "
     f"score: BLA biologic +{f_fda.get('scoring', {}).get('bla_biologic')}, NME +{f_fda.get('scoring', {}).get('nme_type1')}, "
     f"specialty route +{f_fda.get('scoring', {}).get('specialty_route')}, therapeutic match; min {f_fda.get('min_score')}."),
    ("ClinicalTrials.gov (Pipeline)", "Late-stage specialty drugs from major manufacturers nearing launch",
     "ClinicalTrials.gov API v2, queried per sponsor",
     f"{len(f_pipe.get('sponsors', []))} major sponsors (Pfizer, AbbVie, Gilead, Vertex, …); Phase 3; INDUSTRY-sponsored; "
     f"completion {f_pipe.get('completion_from_months')}→{f_pipe.get('completion_to_months')} months; "
     f"{len(f_pipe.get('specialty_conditions', []))} specialty conditions; min {f_pipe.get('min_score')}."),
    ("ClinicalTrials.gov (DOSE)", "Not-yet-started ORAL/pill trials = trial-support opportunities before kickoff",
     "ClinicalTrials.gov API v2, NOT_YET_RECRUITING",
     f"Status NOT_YET_RECRUITING; start date today-or-future; ORAL terms (tablet/capsule/pill) AND exclude injectables; min {f_dose.get('min_score')}."),
    ("State/Local", "State & local government pharmacy RFPs",
     "NYC City Record (Socrata JSON API) + Michigan DTMB (HTML scrape); Florida = placeholder",
     f"Pharmacy keywords ({len(f_state.get('keywords', []))}); last {[s.get('since_days') for s in f_state.get('sources', []) if s.get('since_days')]} days; "
     f"EXCLUDE construction category; Michigan skips awarded/cancelled."),
    ("Historical RFP library", "Your past winning proposals (Sanofi, Lumicera) → answer bank + asset inventory",
     "Local folder scan (PDF/Word/xlsx incl. inside .zip)",
     f"Categorized into asset types; {len(chk.get('items', []))}-item submission checklist; pre-filled answer bank for new RFPs."),
]

PIPE = [
    ("1 · Discover", "Five monitors pull from the sources above every day at 1:00am (launchd)."),
    ("2 · Filter & score", "Each source applies its own keyword/NAICS/PSC/status filters, drops noise (generics, construction, equipment), and scores relevance."),
    ("3 · Analyze", "SAM solicitation PDFs are downloaded and read (pypdf) to judge fit for a 50-state-licensed specialty pharmacy: services vs drug-supply vs equipment, set-aside blockers, geographic scope."),
    ("4 · Pitch", "For each drug opportunity, a custom outreach message + a branded PDF (Medesto Pulse mockups, route-aware, FDA-label education) are generated."),
    ("5 · Track", "Everything lands on a Kanban board (New → Reviewing → Preparing → Submitted → Closed → Past Deadline → Out of Scope). Team decisions export to JSON to refine filters."),
]

src_rows = "".join(
    f'<tr><td class="s">{e(n)}</td><td>{e(w)}</td><td>{e(m)}</td><td class="flt">{e(flt)}</td></tr>'
    for n, w, m, flt in SOURCES)
pipe_rows = "".join(f'<div class="step"><div class="sn">{e(s)}</div><div>{e(d)}</div></div>' for s, d in PIPE)

DOC = f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page{{size:letter;margin:0.6in}}
*{{box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
body{{margin:0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#1b2436;font-size:12px;line-height:1.5}}
.cover{{margin:-0.6in -0.6in 0;padding:0.6in;background:linear-gradient(135deg,#0e7490,#10b981 60%,#3b82f6);color:#fff}}
.logo{{font-size:15px;font-weight:800}}.h1{{font-size:30px;font-weight:800;margin:18px 0 6px}}.lede{{font-size:15px;opacity:.95;max-width:6in}}
.kpis{{display:flex;gap:10px;margin-top:18px;flex-wrap:wrap}}
.kpi{{background:rgba(255,255,255,.16);border-radius:10px;padding:10px 16px}}.kpi b{{font-size:22px;display:block}}.kpi span{{font-size:11px;opacity:.9}}
h2{{font-size:16px;color:#0e7490;margin:22px 0 8px;border-bottom:2px solid #d1fae5;padding-bottom:4px}}
table{{width:100%;border-collapse:collapse;margin-top:6px}}
th,td{{text-align:left;vertical-align:top;padding:7px 8px;border-bottom:1px solid #e5e7eb;font-size:11px}}
th{{background:#f0fdfa;color:#0e7490;font-size:10px;text-transform:uppercase;letter-spacing:.04em}}
td.s{{font-weight:700;width:1.35in}}td.flt{{color:#475467;width:2.7in}}
.step{{display:flex;gap:12px;margin:8px 0;padding:9px 12px;background:#f8fafc;border-left:3px solid #10b981;border-radius:0 8px 8px 0}}
.sn{{font-weight:700;color:#0e7490;min-width:1.1in}}
.note{{font-size:10.5px;color:#667085;margin-top:6px}}
.page2{{page-break-before:always}}
.cap{{display:inline-block;background:#ecfdf5;color:#047857;border-radius:20px;padding:4px 11px;margin:3px 4px 0 0;font-size:11px}}
.foot{{margin-top:24px;font-size:10px;color:#98a2b3;border-top:1px solid #e5e7eb;padding-top:8px}}
</style></head><body>
<div class="cover">
  <div class="logo">Perigon Specialty Pharmacy · RFP Funnel</div>
  <div class="h1">How the opportunity engine works</div>
  <div class="lede">An automated system that finds specialty-pharmacy business opportunities, judges whether they fit Perigon, drafts the outreach, and tracks every one to submission — refreshed daily.</div>
  <div class="kpis">
    <div class="kpi"><b>{total}</b><span>live opportunities</span></div>
    <div class="kpi"><b>6</b><span>data sources</span></div>
    <div class="kpi"><b>{c_fda+c_pipe+c_dose}</b><span>manufacturer leads</span></div>
    <div class="kpi"><b>1am</b><span>auto-refresh daily</span></div>
  </div>
</div>

<h2>What it pulls from, and how it filters each source</h2>
<table>
  <tr><th>Source</th><th>What it finds</th><th>How it looks</th><th>Filtering applied</th></tr>
  {src_rows}
</table>
<div class="note">Current mix: {c_sam} federal RFPs · {c_fda} FDA approvals · {c_pipe} pipeline leads · {c_dose} DOSE trials · {c_state} state/local.</div>

<div class="page2"></div>
<h2>The pipeline — discover → filter → analyze → pitch → track</h2>
{pipe_rows}

<h2>How it decides fit (the filtering logic in plain terms)</h2>
<p><b>Relevance scoring.</b> Every opportunity is scored on pharmacy-specific signals — NAICS/PSC drug codes, keyword matches (specialty pharmacy, 340B, limited distribution, medication adherence), drug class (biologic/NME), and route. Below a minimum score it's dropped.</p>
<p><b>Noise removal.</b> Generic drug approvals are dropped (they join existing networks); medical-equipment and construction solicitations are filtered out by code/category; awarded/closed notices are excluded.</p>
<p><b>PDF fit analysis (SAM).</b> The actual solicitation PDFs are downloaded and read to classify the work as pharmacy <i>services</i> (good), drug <i>supply/manufacturing</i> (not us), or <i>equipment</i> (not us), and to flag set-aside blockers (SDVOSB / 8(a) / WOSB / HUBZone) and whether the scope is nationwide — where Perigon's 50-state licensure is an advantage.</p>
<p><b>Submission readiness.</b> A {len(chk.get('items', []))}-item package checklist maps each requirement to what Perigon already has on file vs. gaps to gather (accreditation certs, state licenses, SAM UEI).</p>

<h2>What you get</h2>
<span class="cap">Kanban board (dashboard.html)</span><span class="cap">Per-drug outreach drafts</span>
<span class="cap">Branded Medesto Pulse pitch PDFs</span><span class="cap">PDF fit verdicts</span>
<span class="cap">Deadline tracking + Past-Deadline column</span><span class="cap">Out-of-Scope feedback + JSON export</span>
<span class="cap">Daily digests</span><span class="cap">Pre-filled answer bank</span>

<div class="foot">Perigon Specialty Pharmacy · RFP Funnel · generated {dt.date.today().isoformat()} · sources: SAM.gov, openFDA, ClinicalTrials.gov, NYC City Record, Michigan DTMB, historical RFP library. Submission stays human — the system gets you draft-ready.</div>
</body></html>"""

OUT_HTML.write_text(DOC)
try:
    subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={OUT_PDF}", f"file://{OUT_HTML}"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    print(f"Overview PDF: {OUT_PDF} ({'ok' if OUT_PDF.exists() else 'FAILED'})")
except Exception as ex:
    print(f"PDF render failed: {ex}")
print(f"Overview HTML: {OUT_HTML}")
