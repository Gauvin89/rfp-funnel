#!/usr/bin/env python3
"""
RFP analyzer — download each SAM solicitation's PDFs, extract the text, and judge
fit for Perigon (a 50-state-licensed specialty pharmacy).

Decides: is this pharmacy SERVICES we can deliver, a drug-SUPPLY/manufacturing
contract (not us), or equipment (not us)? Flags set-aside blockers (SDVOSB/8a/
WOSB/HUBZone) and geographic scope. Produces a verdict + reasons + key requirements.

Results cache to data/opportunities/rfp_analysis.json (keyed by notice id) so we
don't re-download every day. The dashboard merges these onto the SAM cards.

Stdlib + pypdf. Usage: python3 analyze_rfp.py [--force]
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "discovery"))
import common as C  # noqa: E402

try:
    import pypdf
except Exception:
    pypdf = None

ENV = C.load_env()
API_KEY = ENV.get("SAM_API_KEY", "")
CURRENT_SAM = C.OPP_DIR / "current_sam.json"
ANALYSIS = C.OPP_DIR / "rfp_analysis.json"

MAX_DOCS = 2          # download at most the first N PDFs per opportunity
MAX_PAGES = 20        # extract at most N pages per PDF
MAX_CHARS = 40000     # cap text per opportunity

# fit signals
SERVICES = ["pharmacy services", "pharmaceutical services", "dispensing", "dispense",
            "mail order", "mail-order", "specialty pharmacy", "medication therapy",
            "clinical pharmacy", "prescription", "medication management", "pharmacy operations",
            "outpatient pharmacy", "retail pharmacy", "compounding", "unit dose"]
SUPPLY = ["manufacture", "manufacturer", "distribution and pricing", "wholesale",
          "fss schedule", "supply of", "bulk", "active pharmaceutical ingredient", "raw material"]
EQUIPMENT = ["instrument", "endoscopy", "magnetom", "stool", "booth", "catheter", "bed",
             "stimulator", "imaging", "x-ray", "scanner", "furniture", "software license"]
BLOCK_SETASIDE = {"service-disabled": "SDVOSB", "sdvosb": "SDVOSB", "8(a)": "8(a)",
                  "women-owned": "WOSB", "wosb": "WOSB", "hubzone": "HUBZone"}
NATIONWIDE = ["nationwide", "all 50 states", "fifty states", "conus", "multiple states",
              "across the united states", "national"]
LICENSE = ["licensed in all", "all states", "state pharmacy license", "state licensure",
           "board of pharmacy", "licensed pharmacy"]


def download_pdf(url: str) -> bytes:
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}api_key={urllib.parse.quote(API_KEY)}"
    req = urllib.request.Request(full, headers={"User-Agent": C.UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def pdf_text(data: bytes) -> str:
    if not pypdf:
        return ""
    import io
    try:
        r = pypdf.PdfReader(io.BytesIO(data))
        out = []
        for p in r.pages[:MAX_PAGES]:
            out.append(p.extract_text() or "")
        return " ".join(" ".join(out).split())
    except Exception:
        return ""


def hits(text: str, terms: list) -> list:
    t = text.lower()
    return sorted({kw for kw in terms if kw in t})


def evaluate(title: str, setaside: str, text: str) -> dict:
    blob = (title + " " + text).lower()
    svc = hits(blob, SERVICES)
    sup = hits(blob, SUPPLY)
    equ = hits(blob, EQUIPMENT)
    nat = hits(blob, NATIONWIDE)
    lic = hits(blob, LICENSE)

    score, reasons, flags = 0, [], []
    # what's being procured
    if svc and not equ:
        score += 3
        reasons.append(f"Pharmacy services match ({', '.join(svc[:4])})")
    if equ and not svc:
        score -= 4
        flags.append(f"Looks like equipment/goods, not pharmacy services ({', '.join(equ[:3])})")
    if sup and not svc:
        score -= 2
        flags.append(f"Drug supply/manufacturing signals ({', '.join(sup[:3])}) — Perigon dispenses, doesn't manufacture")
    # set-aside
    sa = (setaside or "") + " " + blob[:4000]
    block = sorted({lbl for k, lbl in BLOCK_SETASIDE.items() if k in sa.lower()})
    if block:
        score -= 3
        flags.append(f"Set-aside may block us: {', '.join(block)} (verify Perigon's status)")
    elif setaside and ("small business" in setaside.lower()):
        reasons.append(f"Set-aside: {setaside} — verify small-business size status")
    elif not setaside or setaside.lower() in ("none", ""):
        score += 1
        reasons.append("Full & open (no restrictive set-aside)")
    # geography / licensure — Perigon's 50-state edge
    if nat:
        score += 2
        reasons.append(f"Nationwide/multi-state scope — fits 50-state licensure ({', '.join(nat[:2])})")
    if lic:
        score += 1
        reasons.append("Requires broad state licensure (Perigon strength)")

    if score >= 4:
        verdict = "Strong fit"
    elif score >= 1:
        verdict = "Possible"
    else:
        verdict = "Likely out of scope"
    return {"verdict": verdict, "fit_score": score, "reasons": reasons, "flags": flags,
            "services": svc, "supply": sup, "equipment": equ, "nationwide": bool(nat)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-analyze even if cached")
    args = ap.parse_args()
    if not pypdf:
        sys.exit("pypdf not available")

    sam = json.loads(CURRENT_SAM.read_text()).get("records", []) if CURRENT_SAM.exists() else []
    cache = json.loads(ANALYSIS.read_text()) if ANALYSIS.exists() else {}

    analyzed = 0
    for r in sam:
        nid = r.get("id", "")
        links = r.get("extra", {}).get("doc_links", [])
        if not nid or not links:
            continue
        if nid in cache and not args.force:
            continue
        text, ok = "", 0
        for url in links[:MAX_DOCS]:
            try:
                data = download_pdf(url)
                t = pdf_text(data)
                if t:
                    text += " " + t
                    ok += 1
            except Exception as ex:
                print(f"  {r['title'][:30]}: download/parse failed ({ex})")
            if len(text) > MAX_CHARS:
                break
        text = text[:MAX_CHARS]
        ev = evaluate(r.get("title", ""), r.get("setaside", ""), text)
        ev["docs_read"] = ok
        ev["chars"] = len(text)
        cache[nid] = ev
        analyzed += 1
        print(f"  [{ev['verdict']}] {r['title'][:42]:42} fit={ev['fit_score']} docs={ok}")

    ANALYSIS.write_text(json.dumps(cache, indent=2))
    print(f"\nAnalyzed {analyzed} new SAM opportunities → {ANALYSIS} ({len(cache)} cached)")


if __name__ == "__main__":
    main()
