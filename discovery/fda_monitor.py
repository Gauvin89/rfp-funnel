#!/usr/bin/env python3
"""
openFDA leading-indicator monitor — Perigon specialty pharmacy funnel.

Thesis: a newly approved BRANDED specialty drug (biologic / NME / injectable in a
specialty therapeutic area) means its manufacturer is about to stand up a
limited-distribution network + patient-support / hub program — i.e. they will
soon need a specialty-pharmacy partner. Catching the approval lets Perigon pitch
PROACTIVELY, before any RFP is public (the Sanofi-type deal).

Source: openFDA Drugs@FDA API (free, no key required at our volume, no PII).
Generics are filtered out (brand name == active ingredient); they join existing
networks and don't trigger new RFPs.

Usage:
    python3 fda_monitor.py
    python3 fda_monitor.py --days 90       # wider backfill
    python3 fda_monitor.py --no-enrich     # skip label lookups (fewer calls)
    python3 fda_monitor.py --dry-run
"""
import argparse
import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

DRUGSFDA = "https://api.fda.gov/drug/drugsfda.json"
LABEL = "https://api.fda.gov/drug/label.json"
FILTERS_PATH = C.CONFIG_DIR / "fda_filters.json"
SEEN_PATH = C.OPP_DIR / "seen_fda.jsonl"


def yyyymmdd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


def norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def is_generic(brand: str, ingredients: list) -> bool:
    """Generics list the active ingredient as the brand name (e.g. 'TOFACITINIB CITRATE')."""
    b = norm(brand)
    if not b:
        return True
    for ing in ingredients or []:
        iname = norm(ing.get("name", ""))
        if iname and (iname in b or b in iname):
            return True
    return False


def fetch_recent_approvals(filters: dict, lookback: int) -> tuple:
    """Pull original approvals in the window. openFDA matches at document level, so we
    re-filter client-side to the specific ORIG/AP submissions dated in-window."""
    today = dt.date.today()
    start = today - dt.timedelta(days=lookback)
    search = (f'submissions.submission_type:ORIG '
              f'AND submissions.submission_status:AP '
              f'AND submissions.submission_status_date:[{yyyymmdd(start)} TO {yyyymmdd(today)}]')
    page_size = filters.get("page_size", 100)
    out, calls = [], 0
    for page in range(filters.get("max_pages", 6)):
        params = {"search": search, "limit": page_size, "skip": page * page_size}
        data = C.http_get_json(DRUGSFDA, params, timeout=filters.get("request_timeout_s", 60))
        calls += 1
        rows = data.get("results") or []
        out.extend(rows)
        total = (data.get("meta") or {}).get("results", {}).get("total", 0)
        if data.get("_http_404") or (page + 1) * page_size >= total or not rows:
            break

    cutoff = yyyymmdd(start)
    approvals = []
    for r in out:
        appno = r.get("application_number", "")
        win_subs = [s for s in r.get("submissions", [])
                    if s.get("submission_type") == "ORIG"
                    and s.get("submission_status") == "AP"
                    and (s.get("submission_status_date") or "") >= cutoff]
        if not win_subs:
            continue
        sub = max(win_subs, key=lambda s: s.get("submission_status_date", ""))
        approvals.append((r, appno, sub))
    return approvals, calls


def enrich_label(appno: str, timeout: int) -> dict:
    """Best-effort pharm_class + indications from the label endpoint."""
    try:
        data = C.http_get_json(LABEL, {"search": f'openfda.application_number:"{appno}"', "limit": 1},
                               timeout=timeout)
        res = (data.get("results") or [{}])[0]
        of = res.get("openfda", {})
        pclass = " ".join(of.get("pharm_class_epc", []) + of.get("pharm_class_moa", []))
        ind = " ".join(res.get("indications_and_usage", []))[:1500]
        return {"pharm_class": pclass, "indications": ind}
    except Exception:
        return {"pharm_class": "", "indications": ""}


def score(rec, appno, sub, product, label, filters) -> dict:
    sc = filters["scoring"]
    score, why = 0, []

    if appno.upper().startswith("BLA"):
        score += sc["bla_biologic"]
        why.append("biologic (BLA)")

    cls = (sub.get("submission_class_code_description") or sub.get("submission_class_code") or "")
    if re.search(r"type\s*1|new molecular entity|\bNME\b|original", cls, re.I):
        score += sc["nme_type1"]
        why.append(f"novel ({cls})" if cls else "novel")

    route = (product.get("route") or "")
    if any(x in route for x in filters["specialty_routes"]):
        score += sc["specialty_route"]
        why.append(f"specialty route ({route})")

    blob = " ".join([
        product.get("brand_name", ""),
        " ".join(i.get("name", "") for i in product.get("active_ingredients", [])),
        label.get("pharm_class", ""), label.get("indications", ""),
    ]).lower()
    ta = sorted({t for t in filters["therapeutic_areas"] if t in blob})
    if ta:
        score += min(len(ta) * sc["therapeutic_match_each"], sc["therapeutic_match_cap"])
        why.append("TA: " + ", ".join(ta[:5]))

    score += sc["novel_branded_base"]
    return {"score": score, "why": why, "ta": ta,
            "approved": sub.get("submission_status_date", "")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--no-enrich", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    filters = C.load_json(FILTERS_PATH)
    lookback = args.days or filters.get("lookback_days", 45)
    enrich = filters.get("enrich_label", True) and not args.no_enrich
    timeout = filters.get("request_timeout_s", 60)

    print(f"openFDA monitor: original approvals in last {lookback}d")
    approvals, calls = fetch_recent_approvals(filters, lookback)
    print(f"  pulled {len(approvals)} original approvals ({calls} call(s))")

    scored = []
    for rec, appno, sub in approvals:
        product = (rec.get("products") or [{}])[0]
        brand = product.get("brand_name", "")
        if is_generic(brand, product.get("active_ingredients", [])):
            continue  # generic — joins existing networks, not a new-RFP signal
        label = enrich_label(appno, timeout) if enrich else {"pharm_class": "", "indications": ""}
        s = score(rec, appno, sub, product, label, filters)
        if s["score"] >= filters.get("min_score", 8):
            scored.append((s["score"], rec, appno, sub, product, s))
    scored.sort(key=lambda x: (x[5]["approved"], x[0]), reverse=True)

    seen = C.load_seen(SEEN_PATH)
    new = [x for x in scored if x[2] not in seen]

    blocks = []
    for sc_val, rec, appno, sub, product, s in new:
        sponsor = rec.get("sponsor_name", "n/a")
        brand = product.get("brand_name", "(no brand)")
        ing = ", ".join(i.get("name", "") for i in product.get("active_ingredients", []))
        blocks.append([
            f"## [{sc_val}] {brand} — {sponsor}",
            f"- **Manufacturer (pitch target):** {sponsor}",
            f"- **Drug:** {brand}  ({ing})",
            f"- **Approved:** {s['approved']}  |  **App:** {appno}  |  **Route:** {product.get('route','n/a')}",
            f"- **Why specialty:** {'; '.join(s['why']) or 'novel branded'}",
            f"- **Therapeutic area(s):** {', '.join(s['ta']) or 'unclassified'}",
            f"- **Action:** {sponsor} likely needs an LDD / patient-support / hub partner for {brand} — "
            f"proactive outreach BEFORE any public RFP.",
            f"- **Ref:** https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={re.sub('[^0-9]','',appno)}",
        ])

    run_date = dt.date.today().strftime("%Y-%m-%d")
    intro = [f"_{len(new)} new branded specialty approvals (of {len(scored)} relevant, "
             f"{len(approvals)} approvals scanned). Each = a manufacturer about to need a specialty-pharmacy partner._"]
    digest = C.write_digest(f"digest_fda_{run_date}.md", f"FDA Leading-Indicator Watchlist — {run_date}",
                            intro, blocks)

    current = []
    for sc_val, rec, appno, sub, product, s in scored:
        current.append({
            "kind": "lead", "source": "FDA approval",
            "title": product.get("brand_name", ""), "org": rec.get("sponsor_name", ""),
            "score": sc_val, "date": s["approved"], "app": appno,
            "route": product.get("route", ""), "ta": s["ta"], "why": s["why"],
            "ingredient": ", ".join(i.get("name", "") for i in product.get("active_ingredients", [])),
            "link": f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={re.sub('[^0-9]','',appno)}",
        })
    C.write_current("fda", current)

    if not args.dry_run and new:
        C.append_seen(SEEN_PATH, [(x[2], product_brand(x)) for x in new])

    print(f"\nDone. {len(scored)} relevant ({len(new)} new). Digest: {digest}")
    for sc_val, rec, appno, sub, product, s in new[:8]:
        print(f"  [{sc_val}] {product.get('brand_name','')[:28]:28} {rec.get('sponsor_name','')[:24]:24} {s['approved']}")


def product_brand(x):
    return f"{x[4].get('brand_name','')} ({x[2]})"  # x[2]=application number


if __name__ == "__main__":
    main()
