#!/usr/bin/env python3
"""
Per-opportunity pitch generator (FDA approvals + RFPs).

Produces, per opportunity:
  1. a custom copy-paste OUTREACH message
  2. a branded PITCH PDF: cover -> Pulse patient screens (2x2) -> Bridge
     care-coordination console (Picture 4) -> Medesto platform + drug education.

Uses real brand assets in assets/ (Perigon logo, Pulse + Bridge logos, favicon)
and platform copy scraped from medestohealth.com / perigonpharmacy.org.

Output: pitches/<slug>/{outreach.txt, pitch.html, pitch.pdf}
        data/opportunities/pitches_index.json

Usage: python3 generate_pitch.py [--only SUBSTR] [--limit N]
"""
import argparse
import base64
import datetime as dt
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "discovery"))
import common as C  # noqa: E402

LABEL = "https://api.fda.gov/drug/label.json"
PITCH_DIR = C.ROOT / "pitches"
INDEX = C.OPP_DIR / "pitches_index.json"
ASSETS = C.ROOT / "assets"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
INJECT = ["SUBCUTANEOUS", "INTRAVENOUS", "INTRAMUSCULAR", "INJECTION", "INTRAVITREAL", "INTRATHECAL"]
SKIP_DRUG_WORDS = {"pharmacy", "services", "service", "solution", "system", "web-based", "prescription", "online", "data"}


def b64(path, mime):
    try:
        return f"data:{mime};base64," + base64.b64encode(Path(path).read_bytes()).decode()
    except Exception:
        return ""


FAV = b64(ASSETS / "cropped-Favicon1.png", "image/png")
PERIGON = b64(ASSETS / "Perigon-Logo-Half-768x229.png", "image/png")
PULSE = b64(ASSETS / "Pulse-Green.4bba4b09.svg", "image/svg+xml")
BRIDGE = b64(ASSETS / "bridge-logo.svg", "image/svg+xml")
FOOTER = b64(ASSETS / "footer.png", "image/png")
ILLUS = {n: b64(ASSETS / f"illus_{n}.png", "image/png") for n in ("signature", "approved", "assessment", "video")}

# platform copy (scraped from medestohealth.com/#platform + perigonpharmacy.org/pulse/)
BRIDGE_TAG = "The command center for specialty pharmacy operations"
BRIDGE_FEAT = [
    "Unified patient record built on pharmacy + EMR data",
    "Longitudinal outcomes scoring surfaces at-risk patients automatically",
    "Care plans auto-generate from assessment data — accreditation-ready",
    "Direct messaging into the prescriber's EHR via FHIR R4",
]
PULSE_TAG = "The patient experience, reimagined"
PULSE_FEAT = [
    "Profile auto-populated from EMR — allergies, history, medications",
    "Drug-specific education & training in text, image and video",
    "Real-time order updates — coverage, copays, delivery tracking",
    "Direct messaging and video calls with the pharmacy team",
]


def e(s):
    return html.escape(str(s or ""))


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "opp"


def pick_drug(drugs):
    """Choose the cleanest real drug name from a lead's product list."""
    for d in drugs:
        if " " not in d and "-" not in d and d.isalpha() and len(d) > 3:
            return d
    return drugs[0] if drugs else ""


def clean(text, limit=300):
    t = re.sub(r"^\s*\d+(\.\d+)*\s*", "", text or "")  # leading section number
    words = t.split()
    i = 0
    while i < len(words) and words[i].isupper() and len(words[i]) > 1:  # leading ALL-CAPS header words
        i += 1
    if i:
        t = " ".join(words[i:])
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def clean_recipient(r):
    parts, seen = [p.strip().title() for p in re.split(r"[.\n]", r or "") if p.strip()], []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return " — ".join(seen[:2]) if seen else (r or "")


def fetch_label(app="", name=""):
    if app:
        q = f'openfda.application_number:"{app}"'
    elif name and name.lower() not in SKIP_DRUG_WORDS:
        q = f'openfda.generic_name:"{name}" OR openfda.brand_name:"{name}"'
    else:
        return {}
    try:
        d = C.http_get_json(LABEL, {"search": q, "limit": 1}, timeout=30)
        r = (d.get("results") or [{}])[0]
        if not r:
            return {}
        of = r.get("openfda", {})
        return {"brand": (of.get("brand_name") or [name])[0], "generic": (of.get("generic_name") or [""])[0],
                "route": (of.get("route") or [""])[0],
                "indication": clean(" ".join(r.get("indications_and_usage", [])), 300),
                "dosing": clean(" ".join(r.get("dosage_and_administration", [])), 240),
                "warnings": clean(" ".join(r.get("warnings_and_cautions", []) or r.get("warnings", [])), 240)}
    except Exception:
        return {}


def route_words(route):
    r = (route or "").upper()
    if any(x in r for x in INJECT):
        return {"verb": "inject", "noun": "injection", "device": "autoinjector pen", "oral": False}
    if "ORAL" in r:
        return {"verb": "take", "noun": "dose", "device": "tablet/capsule", "oral": True}
    return {"verb": "take", "noun": "dose", "device": "medication", "oral": None}


CAPS = [
    ("🔔", "Event-driven notifications", "Triggered by refill windows, shipments and clinical milestones — not random phone calls."),
    ("📝", "Direct-to-patient document feed", "Intake forms, enrollment and consents pushed straight to the patient's feed."),
    ("✍️", "In-app e-signatures", "HIPAA authorizations and enrollment signed on-device with biometrics."),
    ("🎓", "Drug-specific education", "Text, images, video and PDFs — what to expect, how to take it, what to watch for."),
    ("📦", "Refills + delivery tracking", "Reminders so they never run out, with real-time delivery status."),
    ("💬", "Messaging + video visits", "A direct line to the pharmacist and care coordinator, in-app."),
]


def drug_benefits(drug, label, rw, ta):
    cond = (label.get("indication", "") + " " + (ta or "")).lower()
    out = []
    if rw["oral"]:
        out.append(f"Daily {drug} dose reminders with adherence tracking and automatic missed-dose follow-up")
    else:
        out.append(f"A step-by-step “How to {rw['verb']} {drug}” video plus first-{rw['noun']} and injection-site reaction check-ins")
    cmap = [
        (("cancer", "oncolog", "tumor", "carcinoma", "lymphoma", "leukemia", "myeloma"),
         "Symptom and side-effect check-ins between cycles, escalated to a pharmacist when flagged"),
        (("hepatitis", "hiv", "infect", "antivir"),
         "Adherence support to protect viral suppression, with lab-result and follow-up reminders"),
        (("multiple sclerosis",),
         "Relapse and mobility check-ins with education on what to watch for"),
        (("rheumat", "psoria", "crohn", "colitis", "lupus", "autoimmun"),
         "Flare tracking and persistence support so therapy isn’t abandoned between doses"),
        (("hemophilia", "bleed"),
         "Bleed-log capture and on-demand pharmacist access"),
        (("postmenopausal", "endometrial", "hormone", "amenorrhea", "estrogen", "progest", "menopaus"),
         "Cycle-aware bedtime-dosing reminders and plain-language hormone-therapy education"),
        (("cystic fibrosis",),
         "Pulmonary symptom check-ins and adherence support across the regimen"),
        (("pulmonary arterial hypertension", "pah", "hypertension"),
         "Titration-schedule reminders and symptom monitoring"),
        (("growth", "pediatr", "child"),
         "Growth-tracking check-ins and caregiver-facing education"),
    ]
    for keys, txt in cmap:
        if any(k in cond for k in keys):
            out.append(txt)
            break
    else:
        out.append(f"Education tailored to the condition — what to expect on {drug} and when to call the pharmacy")
    out.append(f"First-fill onboarding and benefits investigation so {drug} therapy starts on time")
    out.append("Real-world adherence and outcomes reported back to the prescriber and manufacturer")
    return out


# ---------- outreach ----------

def outreach_fda(drug, mfr, label, rw, ta):
    return f"""Subject: An AI-powered patient support platform for {drug} — Perigon + Medesto

Hi {mfr} team,

Congratulations on {drug}{f" ({label['generic']})" if label.get('generic') else ""}. As you stand up the
limited-distribution network and patient support program, Perigon Specialty Pharmacy pairs 50-state
specialty dispensing with Medesto — our AI-powered, automation-first Patient Engagement Platform built
for specialty therapies like {drug}.

Medesto is two connected products, branded to {drug}:
  • Pulse (patient app) — event-driven engagement that replaces cold calls with smart notifications;
    HIPAA e-signatures and document capture; drug-specific education in text, video and PDF;
    {rw['noun']} check-ins; refill + delivery tracking; and in-app messaging/video with the pharmacist.
  • Bridge (clinical-ops command center) — a unified pharmacy + EMR patient record, automated outreach,
    longitudinal outcomes scoring that auto-surfaces at-risk {drug} patients, accreditation-ready care
    plans, and FHIR R4 messaging straight into the prescriber's EHR.

Backed by 50-state licensure, URAC + ACHC accreditation, and 45,000 specialty fills/month — with
real-world adherence and outcomes reported back to your team.

I've attached a short, {drug}-branded mockup of the patient experience on Medesto. Worth 20 minutes?

Best,
[Your name] · Perigon Specialty Pharmacy
"""


def outreach_rfp(title, agency):
    return f"""Subject: Perigon Specialty Pharmacy + Medesto — response to "{title}"

To the {agency} contracting team,

Perigon Specialty Pharmacy is pleased to respond to "{title}." Beyond 50-state-licensed, URAC + ACHC-
accredited specialty dispensing (45,000 fills/month), we deliver Medesto — our AI-powered, automation-
first Patient Engagement Platform:

  • Pulse (patient app) — event-driven notifications instead of cold calls; HIPAA e-signatures and
    in-app document capture; drug-specific education in text, video and PDF; dose/refill check-ins;
    delivery tracking; and direct pharmacist messaging and video.
  • Bridge (clinical-ops command center) — a unified pharmacy + EMR patient record, automated outreach,
    outcomes scoring that auto-surfaces at-risk patients, accreditation-ready care plans, and FHIR R4
    connectivity into the prescriber's EHR.

The result: higher adherence and persistence, fewer care gaps, and measurable outcomes — with
real-world reporting delivered under this contract.

I've attached a brief overview of the patient experience we'd stand up. We welcome the full discussion.

Respectfully,
[Your name] · Perigon Specialty Pharmacy
"""


def outreach_manuf(drug, mfr, label, rw, ta):
    gen = f" ({label['generic']})" if label.get("generic") and label["generic"].lower() != drug.lower() else ""
    return f"""Subject: Perigon Specialty Pharmacy + Medesto — a patient program for {drug}

Hi {mfr} team,

I lead partnerships at Perigon Specialty Pharmacy. As you manage the {drug}{gen} patient experience,
I wanted to introduce Perigon + Medesto — 50-state, URAC + ACHC-accredited specialty dispensing
(45,000 fills/month) paired with Medesto, our AI-powered, automation-first patient engagement
platform, branded to {drug}:

  • Pulse (patient app) — event-driven engagement instead of cold calls; HIPAA e-signatures and
    document capture; {drug}-specific education in text, video and PDF; {rw['noun']} check-ins;
    refill + delivery tracking; and in-app messaging/video with the pharmacist.
  • Bridge (clinical-ops command center) — a unified pharmacy + EMR record, automated outreach,
    outcomes scoring that auto-surfaces at-risk {drug} patients, accreditation-ready care plans,
    and FHIR R4 messaging into the prescriber's EHR.

We report real-world adherence and outcomes back to your team. I've attached a short, {drug}-branded
mockup of the patient experience — worth 20 minutes to explore the fit?

Best,
[Your name] · Perigon Specialty Pharmacy
"""


# ---------- mockup components ----------

def msg(frm, title, body, buttons, bullets=None):
    btns = "".join(f'<span class="b {col}">{e(lbl)}</span>' for lbl, col in (buttons or []))
    bl = ("<ul class='bl'>" + "".join(f"<li>{e(x)}</li>" for x in bullets) + "</ul>") if bullets else ""
    return f"""<div class="msg">
      <span class="ic"></span>
      <div class="mc">
        <div class="from">{e(frm)}</div>
        <div class="mt">{e(title)}</div>
        <div class="mb">{e(body)}</div>{bl}
        {f'<div class="btns">{btns}</div>' if btns else ''}
      </div>
    </div>"""


def phone(cards, hero=None, play=False):
    hero_html = ""
    if hero:
        pb = '<span class="playbtn">▶</span>' if play else ''
        hero_html = f'<div class="hero"><img src="{hero}">{pb}</div>'
    return f"""<div class="phone">
      <div class="hdr"><div class="notch"></div><img class="plogo" src="{PULSE}"></div>
      {hero_html}
      <div class="screen">{cards}</div>
      <div class="tabs"><span class="on">All</span><span>To-Do</span><span>Documents</span><span>Health News</span></div>
    </div>"""


def footer(n, total=5):
    return f"""<div class="pfoot">
      <div class="pfoot-line"><span>Confidential · Perigon Pharmacy 360 LLC · © 2026</span><span>Page {n} of {total}</span></div>
      <img class="pfoot-img" src="{FOOTER}">
    </div>"""


def b_alert(sev, atype, tcolor, title, body, patient, drug, ago, selected=False):
    return f"""<div class="ba{' sel' if selected else ''}">
      <div class="ba-dot" style="background:{sev}"></div>
      <div class="ba-main">
        <div class="ba-type" style="color:{tcolor}">● {e(atype)}</div>
        <div class="ba-title">{e(title)}</div>
        <div class="ba-body">{e(body)}</div>
        <div class="ba-pat"><span class="ba-av">{e(patient[:1])}</span>{e(patient)} · <span class="ba-prog">{e(drug)}</span></div>
      </div>
      <div class="ba-right"><div class="ba-ago">{e(ago)}</div><span class="ba-done">✓ Mark Complete</span></div>
    </div>"""


def detail_row(label, open_=False):
    return f'<div class="dr">{e(label)}<span>{"▴" if open_ else "▾"}</span></div>'


def pitch_html(drug, recipient, label, rw, ta, is_rfp):
    rec = clean_recipient(recipient) if is_rfp else recipient
    inj = not rw["oral"]
    GREEN, RED, ORANGE = "#10b981", "#ef4444", "#f59e0b"
    ind = label.get("indication") or (f"{ta} therapy" if ta else "the prescribed therapy")

    # ----- Pulse screens (faithful; favicon icon, white pulse logo, no drug chip)
    s1 = phone(
        '<div class="hipaa"><div class="hp-tag">READY TO SIGN</div><div class="hp-t">HIPAA Privacy Notice &amp; Consent</div>'
        '<div class="hp-b">We protect your health information and only share it with your care team.</div>'
        '<span class="b green">✎ Sign Now</span></div>'
        + msg("Perigon Pharmacy", "Here's What Happens Next", "Should take about 10 minutes. Message us anytime.",
              [], bullets=["Sign HIPAA consent form", "Review what we already know about your health",
                           "Complete your baseline assessment", "We'll create your personalized goals",
                           "We'll handle insurance and ship your medication"])
        + msg("Perigon Pharmacy", "Prescription Received", f"Good news! We received your {drug} prescription and are getting started right away.", []), hero=ILLUS['signature'])
    s2 = phone(
        msg("Perigon Pharmacy", "Refill Due Soon", f"Hi Emma! Are you ready for your {drug} refill?", [("Yes", "green"), ("No", "red")])
        + msg("Perigon Pharmacy", "Monthly Check-In", "Time for your regular check-in — about 3–4 minutes.", [("Start Assessment", "green")])
        + msg("Perigon Pharmacy", f"First {'Injection' if inj else 'Dose'} Check-In", f"Quick check-in after your first {drug} {rw['noun']}. Your answers help us keep you on track.", [("Start Assessment", "green")]), hero=ILLUS['assessment'])
    s3 = phone(
        msg("Perigon Pharmacy", "Confirm Delivery Address", "Confirm where you'd like your medication delivered. 1247 Maple St, Madison WI 53717.", [("Confirm", "green"), ("Change Address", "ghost")])
        + msg("Perigon Pharmacy", f"{drug} Important Safety Information", f"Important safety information about {drug} — what to watch for and when to call your doctor.", [])
        + msg("Perigon Pharmacy", f"Watch: How to {rw['verb'].title()} {drug}", f"A short video walks you through how to {rw['verb']} {drug}, step by step.", [("▶ Watch", "green")]), hero=ILLUS['video'], play=True)
    s4 = phone(
        msg("Perigon Pharmacy", "Your Pharmacist is Calling", "Your pharmacist is ready for your scheduled call. Tap Accept.", [("Accept", "green"), ("Decline", "red")])
        + msg("Perigon Pharmacy", "Refill Shipped", f"Your {drug} is on the way — tap to track your delivery.", [("Track", "green")])
        + msg("Perigon Pharmacy", "Health News", f"New education about {drug} and your condition is available.", [("Read", "ghost")]), hero=ILLUS['approved'])

    # ----- Bridge console (faithful to Picture 4), customized to the drug
    a_pr = ("Side Effect: Dizziness After Dose", "Patient reported dizziness and drowsiness after the evening dose.") if rw["oral"] else ("Severe Injection Site Reaction", "Patient reported severe swelling and fever after injection.")
    alerts = (
        b_alert(RED, "LAB RESULT", RED, f"{drug}: Lab Value Out of Range", "Lab value flagged — therapy review may be required.", "Elena Vasquez", drug, "2h ago")
        + b_alert(RED, "PATIENT REPORTED", RED, a_pr[0], a_pr[1], "Marcus Johnson", drug, "6h ago")
        + '<div class="hi-div">▾ High</div>'
        + b_alert(ORANGE, "CARE GAP", ORANGE, "Delivery Address Not Confirmed", f"Patient has not confirmed shipping address. {drug} first fill is on hold pending address verification.", "Emma Davis", drug, "2h ago", selected=True)
        + b_alert(ORANGE, "FOLLOW UP", ORANGE, "No Response to Refill Request", "Multiple refill outreach attempts with no patient response.", "Sarah Mitchell", drug, "2h ago")
        + b_alert(ORANGE, "FOLLOW UP", ORANGE, "Refill Request Pending", "Patient submitted a refill request. Awaiting pharmacist review and dispense authorization.", "Maya Patel", drug, "2h ago"))
    detail = f"""<div class="dpanel">
      <div class="dp-card"><div class="dp-h"><span class="dp-type">● CARE GAP</span> · Delivery Address Not Confirmed<span class="dp-x">✕</span></div>
        <div class="dp-b">Patient has not confirmed shipping address. {e(drug)} first fill is on hold pending address verification.</div>
        <div class="dp-tags"><span>delivery-address</span><span>first-fill</span></div></div>
      <div class="dp-card"><div class="dp-sec">Assignment <span class="dp-link">Assign to me</span></div><div class="dp-b">Unassigned <span class="dp-assign">Assign ⟳</span></div></div>
      <div class="dp-card"><div class="dp-sec">Actions</div><div class="dp-b">🔒 Assign this alert first to take actions.</div></div>
      {detail_row(f"👤 Emma Davis · {drug}")}
      {detail_row("🛡 Insurance Information")}
      <div class="dp-card open">{detail_row("📄 Alert Documents  3", open_=True)}
        <div class="docs"><div>📄 Lab Results - Hormone Panel</div><div>📄 Medication Reconciliation</div><div>📄 Specialist Referral</div></div></div>
      {detail_row("✎ Notes")}{detail_row("🕓 Activity History")}
    </div>"""

    edu = "".join(f'<div class="ed"><div class="edk">{k}</div><div>{e(v)}</div></div>'
                  for k, v in [("Indication", ind), ("How it's given", label.get("dosing") or ""),
                               ("Key safety", label.get("warnings") or "")] if v)
    caps_html = "".join(f'<div class="cap-card"><b>{ic} {t}</b><span>{e(d)}</span></div>' for ic, t, d in CAPS)
    benefits_html = "".join(f"<li>{e(b)}</li>" for b in drug_benefits(drug, label, rw, ta))

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page{{size:letter;margin:0}}
*{{box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
body{{margin:0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#111}}
.page{{width:8.5in;height:11in;padding:.5in;page-break-after:always;position:relative}}
.cover{{background:linear-gradient(135deg,#0e7490,#10b981 55%,#355aea)}}.cover *{{color:#fff}}
.cv-logos{{display:flex;align-items:center;gap:16px}}.cv-logos img{{height:30px;filter:brightness(0) invert(1)}}
.cv-title{{font-size:40px;font-weight:800;margin:34px 0 6px;line-height:1.08}}.cv-sub{{font-size:17px;opacity:.95;max-width:6in}}
.cv-brand{{display:inline-block;margin-top:22px;padding:8px 18px;border-radius:30px;background:rgba(255,255,255,.2);font-size:19px;font-weight:700}}
.vp{{margin-top:34px}}.vp div{{font-size:15px;margin:9px 0;padding-left:24px;position:relative}}.vp div:before{{content:'✓';position:absolute;left:0;font-weight:800}}
.cv-foot{{position:absolute;bottom:.8in;left:.5in;font-size:12px;opacity:.9}}
h2{{font-size:21px;margin:0 0 4px;color:#0e7490}}.sub{{color:#667085;margin:0 0 14px;font-size:13px}}
.ic{{width:17px;height:17px;border-radius:50%;flex-shrink:0;background:url('{FAV}') center/contain no-repeat}}
/* phones: 2x2 grid, larger */
.grid2{{display:flex;gap:.55in;justify-content:center;margin-top:12px}}
.phone{{width:2.55in;height:5.15in;border:8px solid #0e1726;border-radius:34px;overflow:hidden;background:#f4f6f8;display:flex;flex-direction:column}}
.hero{{position:relative;line-height:0;border-bottom:1px solid #e5e7eb}}.hero img{{width:100%;display:block}}
.playbtn{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:34px;height:34px;border-radius:50%;background:rgba(0,0,0,.5);color:#fff;display:flex;align-items:center;justify-content:center;font-size:13px}}
.pfoot{{position:absolute;bottom:0;left:0;right:0}}
.pfoot-line{{display:flex;justify-content:space-between;font-size:8.5px;color:#98a2b3;padding:0 .5in 4px}}
.pfoot-img{{display:block;width:100%}}
.hdr{{height:54px;background:linear-gradient(120deg,#34d399,#10b981 55%,#3b82f6);position:relative;display:flex;align-items:center;justify-content:center}}
.notch{{position:absolute;top:7px;left:50%;transform:translateX(-50%);width:42px;height:6px;border-radius:6px;background:rgba(0,0,0,.32)}}
.plogo{{height:20px;filter:brightness(0) invert(1)}}
.screen{{flex:1;overflow:hidden;padding:7px}}
.hipaa{{background:#fff;border:1px solid #e5e7eb;border-radius:11px;padding:8px;margin-bottom:7px}}
.hp-tag{{font-size:7px;font-weight:800;color:#0e7490;letter-spacing:.05em}}.hp-t{{font-size:10px;font-weight:700;margin:2px 0}}.hp-b{{font-size:8px;color:#475467;margin-bottom:5px}}
.msg{{display:flex;gap:6px;background:#fff;border:1px solid #e9ecf1;border-radius:11px;padding:7px;margin-bottom:7px}}
.mc{{flex:1;min-width:0}}.from{{font-size:8.5px;font-weight:700;color:#355aea}}.mt{{font-size:9.5px;font-weight:700;margin:1px 0}}.mb{{font-size:8px;color:#475467;line-height:1.32}}
.bl{{margin:3px 0 0;padding-left:12px}}.bl li{{font-size:7.5px;color:#475467;line-height:1.5}}
.btns{{display:flex;gap:4px;margin-top:5px;flex-wrap:wrap}}.b{{font-size:8px;font-weight:700;padding:3px 9px;border-radius:11px;color:#fff;display:inline-block}}
.b.green{{background:#10b981}}.b.red{{background:#ef4444}}.b.ghost{{background:#fff;color:#475467;border:1px solid #cdd5df}}
.tabs{{display:flex;justify-content:space-around;padding:5px;border-top:1px solid #e5e7eb;background:#fff;font-size:7px;color:#98a2b3}}.tabs .on{{color:#355aea;font-weight:700}}
/* bridge console */
.btopbar{{display:flex;align-items:center;justify-content:space-between;background:#fff;border:1px solid #e5e7eb;border-radius:10px 10px 0 0;padding:8px 14px}}
.btopbar img{{height:20px}}.bsearch{{background:#f2f4f7;border-radius:18px;padding:4px 14px;font-size:10px;color:#98a2b3;width:2in}}
.bhead{{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border:1px solid #e5e7eb;border-top:0;background:#fafbfc}}
.bhead h3{{margin:0;font-size:15px;color:#0e7490}}.bstats span{{font-size:10.5px;margin-left:12px;color:#475467}}.bstats b{{color:#111}}
.bfilters{{display:flex;gap:6px;padding:8px 14px;border:1px solid #e5e7eb;border-top:0;flex-wrap:wrap}}
.bfilters span{{font-size:9.5px;background:#fff;border:1px solid #e5e7eb;border-radius:7px;padding:3px 9px;color:#475467}}
.bbody{{display:flex;border:1px solid #e5e7eb;border-top:0;border-radius:0 0 10px 10px;overflow:hidden}}
.balist{{flex:1.25;border-right:1px solid #eef1f5}}
.ba{{display:flex;gap:9px;padding:9px 14px;border-bottom:1px solid #f3f5f8;align-items:flex-start}}.ba.sel{{background:#f5f8ff;border-left:3px solid #355aea}}
.ba-dot{{width:8px;height:8px;border-radius:50%;margin-top:4px;flex-shrink:0}}.ba-main{{flex:1}}
.ba-type{{font-size:8.5px;font-weight:800;letter-spacing:.03em}}.ba-title{{font-size:12px;font-weight:700;margin:1px 0}}.ba-body{{font-size:10px;color:#475467}}
.ba-pat{{font-size:9.5px;color:#667085;margin-top:4px}}.ba-av{{display:inline-flex;width:14px;height:14px;border-radius:50%;background:#355aea;color:#fff;font-size:7.5px;font-weight:700;align-items:center;justify-content:center;margin-right:5px;vertical-align:middle}}.ba-prog{{color:#0e7490;font-weight:600}}
.ba-right{{text-align:right;min-width:.95in}}.ba-ago{{font-size:8.5px;color:#98a2b3;margin-bottom:5px}}.ba-done{{display:inline-block;border:1px solid #10b981;color:#10b981;font-size:9px;font-weight:700;padding:3px 8px;border-radius:7px}}
.hi-div{{padding:5px 14px;background:#fff7ed;color:#c2410c;font-size:9.5px;font-weight:700}}
.dpanel{{flex:1;padding:10px;background:#f7fdfb}}
.dp-card{{background:#fff;border:1.5px solid #6ee7b7;border-radius:10px;padding:9px;margin-bottom:8px}}
.dp-h{{font-size:11px;font-weight:700}}.dp-type{{color:#f59e0b;font-weight:800;font-size:9px}}.dp-x{{float:right;color:#98a2b3}}
.dp-b{{font-size:9.5px;color:#475467;margin-top:3px}}.dp-tags span{{display:inline-block;background:#eef2ff;color:#355aea;font-size:8px;border-radius:9px;padding:2px 7px;margin:4px 4px 0 0}}
.dp-sec{{font-size:10px;font-weight:700;color:#344054}}.dp-link{{color:#355aea;font-size:9px;float:right}}.dp-assign{{color:#355aea;font-size:9px}}
.dr{{background:#fff;border:1.5px solid #6ee7b7;border-radius:10px;padding:9px;margin-bottom:8px;font-size:10.5px;font-weight:600;color:#344054;display:flex;justify-content:space-between}}
.dp-card.open .docs{{margin-top:6px}}.docs div{{font-size:9.5px;color:#355aea;padding:3px 0}}
/* platform */
.plat{{display:flex;gap:16px;margin-top:6px}}.pcol{{flex:1;border:1px solid #e5e7eb;border-radius:12px;padding:14px}}
.pcol img{{height:22px;margin-bottom:6px}}.pcol .ptag{{font-size:13px;font-weight:700;color:#0e7490;margin-bottom:8px}}
.pcol li{{font-size:11.5px;color:#344054;margin:6px 0;list-style:none;padding-left:18px;position:relative}}.pcol li:before{{content:'✓';position:absolute;left:0;color:#10b981;font-weight:800}}
.ed{{margin:9px 0;padding:10px 14px;border-left:3px solid #10b981;background:#f8fafc;border-radius:0 8px 8px 0}}.edk{{font-weight:700;font-size:12px;color:#0e7490}}.ed div:last-child{{font-size:12.5px;color:#344054}}
.csec{{font-size:15px;color:#0e7490;margin:20px 0 8px;font-weight:700}}
.caps-grid{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}}
.cap-card{{border:1px solid #e5e7eb;border-radius:10px;padding:9px 11px;background:#fafffe}}
.cap-card b{{display:block;font-size:11.5px;color:#0e7490;margin-bottom:3px}}.cap-card span{{font-size:10.5px;color:#475467;line-height:1.35}}
.benefits{{list-style:none;padding:0;margin:0;columns:2;column-gap:28px}}
.benefits li{{font-size:12.5px;color:#344054;margin:0 0 11px;padding-left:20px;position:relative;break-inside:avoid}}
.benefits li:before{{content:'✓';position:absolute;left:0;color:#10b981;font-weight:800}}
.fine{{position:absolute;bottom:.74in;left:.5in;right:.5in;font-size:9px;color:#98a2b3}}
</style></head><body>
<div class="page cover">
  <div class="cv-logos"><img src="{PERIGON}"><span style="opacity:.5">|</span><img src="{PULSE}"><img src="{BRIDGE}"></div>
  <div class="cv-title">The {e(drug)}<br>patient program</div>
  <div class="cv-sub">A branded dispensing, engagement &amp; care-coordination experience — {('in response to <b>'+e(drug)+'</b> · '+e(rec)) if is_rfp else ('prepared for <b>'+e(rec)+'</b>')}</div>
  <div class="cv-brand">{e(drug)}{f" · {e(label['generic'])}" if label.get('generic') and label['generic'].lower() != drug.lower() else ""}</div>
  <div class="vp"><div>50-state licensed specialty dispensing — URAC + ACHC, 45,000 fills/month</div>
    <div>{e(drug)}-branded Medesto Pulse app: onboarding, {rw['noun']} check-ins, refills, safety</div>
    <div>Bridge command center: every alert worked, outcomes scored, care plans auto-generated</div>
    <div>Real-world adherence + outcomes reporting</div></div>
  <div class="cv-foot">Prepared {dt.date.today().isoformat()}</div>
  {footer(1)}
</div>
<div class="page">
  <h2>The {e(drug)} patient experience — Medesto Pulse</h2>
  <p class="sub">Branded to {e(drug)} — HIPAA onboarding and proactive assessments.</p>
  <div class="grid2">{s1}{s2}</div>
  <h3 class="csec">What's possible in Medesto Pulse for {e(drug)} patients</h3>
  <div class="caps-grid">{caps_html}</div>
  {footer(2)}
</div>
<div class="page">
  <h2>The {e(drug)} experience — delivery, safety &amp; support</h2>
  <p class="sub">"How to {rw['verb']} {e(drug)}," direct pharmacist access, and approval.</p>
  <div class="grid2">{s3}{s4}</div>
  <h3 class="csec">How Pulse helps {e(drug)} patients specifically</h3>
  <ul class="benefits">{benefits_html}</ul>
  {footer(3)}
</div>
<div class="page">
  <h2>Bridge — {e(drug)} care coordination</h2>
  <p class="sub">Your pharmacist team works every {e(drug)} alert: lab values, side effects, delivery gaps, refills.</p>
  <div class="btopbar"><img src="{BRIDGE}"><div class="bsearch">Search patients…</div></div>
  <div class="bhead"><h3>🔔 Care Coordination Alerts</h3>
    <div class="bstats"><span>Total <b>20</b></span><span>Critical <b style="color:#ef4444">3</b></span><span>High <b style="color:#f59e0b">11</b></span><span>Resolved <b>0</b></span></div></div>
  <div class="bfilters"><span>Search by patient</span><span>Type ▾</span><span>Severity ▾</span><span>Status ▾</span><span>Assignee ▾</span><span>Age ▾</span><span>↕ Sort</span></div>
  <div class="bbody"><div class="balist">{alerts}</div>{detail}</div>
  <div class="fine">Illustrative recreation of the Bridge console, scoped to the {e(drug)} program.</div>
  {footer(4)}
</div>
<div class="page">
  <h2>The Medesto platform — built for {e(drug)}</h2>
  <p class="sub">One clinical engine. {e(drug)} dispensing wrapped in patient engagement + clinical-ops coordination.</p>
  <div class="plat">
    <div class="pcol"><img src="{BRIDGE}"><div class="ptag">{BRIDGE_TAG}</div>
      <ul>{''.join(f'<li>{e(x)}</li>' for x in BRIDGE_FEAT)}</ul></div>
    <div class="pcol"><img src="{PULSE}" style="filter:none"><div class="ptag">{PULSE_TAG}</div>
      <ul>{''.join(f'<li>{e(x)}</li>' for x in PULSE_FEAT)}</ul></div>
  </div>
  <h2 style="margin-top:18px">{e(drug)} patient education</h2>
  <p class="sub">Delivered in-app, in plain language — text, image and video.</p>
  {edu or '<p class="sub">Drug-specific education compiled from the FDA prescribing information.</p>'}
  <div class="fine">Platform copy: medestohealth.com, perigonpharmacy.org/pulse</div>
  {footer(5)}
</div>
</body></html>"""


def render_pdf(html_path, pdf_path):
    # isolated profile so it works even if a Chrome/Brave GUI window is open
    prof = tempfile.mkdtemp(prefix="rfp-chrome-")
    try:
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                        "--no-first-run", "--no-default-browser-check",
                        f"--user-data-dir={prof}", "--no-pdf-header-footer",
                        f"--print-to-pdf={pdf_path}", f"file://{html_path}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
    finally:
        shutil.rmtree(prof, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="only opportunities whose title contains this")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-pdf", action="store_true",
                    help="write outreach + html + index only; skip Chrome PDF render "
                         "(keeps any PDF already on disk). Use when a browser is open.")
    args = ap.parse_args()

    def recs(n):
        p = C.OPP_DIR / f"current_{n}.json"
        return json.loads(p.read_text()).get("records", []) if p.exists() else []

    targets = []
    for r in recs("fda"):
        targets.append({"cid": "fda:" + r.get("app", ""), "kind": "fda", "title": r.get("title", ""),
                        "recipient": r.get("org", ""), "app": r.get("app", ""), "route": r.get("route", ""),
                        "ta": ", ".join(r.get("ta", []))})
    for src in ("sam", "state"):
        for r in recs(src):
            targets.append({"cid": r.get("id", ""), "kind": "rfp", "title": r.get("title", ""),
                            "recipient": r.get("org", "the agency"), "app": "", "route": "", "ta": ""})
    for r in recs("manufacturers"):  # Lead List — only companies with a concrete named product
        d = pick_drug(r.get("drugs", []))
        if not d:
            continue
        targets.append({"cid": r.get("id", ""), "kind": "manuf", "title": d,
                        "recipient": r.get("org", "the manufacturer"), "app": "", "route": "", "ta": ""})
    if args.only:
        targets = [t for t in targets if args.only.lower() in t["title"].lower()]
    if args.limit:
        targets = targets[: args.limit]

    index = json.loads(INDEX.read_text()) if (args.only and INDEX.exists()) else {}
    for t in targets:
        title, recipient = t["title"], t["recipient"]
        if not title or not t["cid"]:
            continue
        is_rfp = t["kind"] == "rfp"
        if t["kind"] == "fda":
            label, drug = fetch_label(app=t["app"]), title
        else:
            label = fetch_label(name=re.split(r"[\s,:/]", title.strip())[0])
            drug = label.get("brand") or title
        rw = route_words(label.get("route") or t["route"])
        folder = slug(f"{recipient} {title}") if t["kind"] == "manuf" else slug(title)
        d = PITCH_DIR / folder
        d.mkdir(parents=True, exist_ok=True)
        if t["kind"] == "fda":
            msg_txt = outreach_fda(drug, recipient, label, rw, t["ta"])
        elif t["kind"] == "manuf":
            msg_txt = outreach_manuf(drug, recipient, label, rw, t["ta"])
        else:
            msg_txt = outreach_rfp(title, clean_recipient(recipient))
        (d / "outreach.txt").write_text(msg_txt)
        hp, pp = d / "pitch.html", d / "pitch.pdf"
        hp.write_text(pitch_html(drug, recipient, label, rw, t["ta"], is_rfp))
        if args.no_pdf:
            ok = pp.exists()  # keep any previously-rendered PDF; don't launch Chrome
        else:
            try:
                render_pdf(hp, pp)
                ok = pp.exists()
            except Exception as ex:
                ok = False
                print(f"  {drug}: pdf failed ({ex})")
        index[t["cid"]] = {"drug": drug, "mfr": clean_recipient(recipient) if is_rfp else recipient,
                           "pdf": str(pp) if ok else "", "outreach": msg_txt, "kind": t["kind"]}
        print(f"  ✓ [{t['kind']}] {drug[:18]:18} -> {'pdf' if ok else 'no-pdf'}")

    INDEX.write_text(json.dumps(index, indent=2))
    print(f"\n{len(index)} pitches in index → {INDEX}")


if __name__ == "__main__":
    main()
