#!/usr/bin/env python3
"""
Manufacturer-pipeline monitor — Perigon specialty pharmacy funnel.

Surfaces big-pharma specialty drugs in late-stage trials nearing completion. When
a manufacturer's Phase 3 specialty drug is about to read out / launch, THAT is when
they build the limited-distribution network and choose pharmacy partners. Catch it
early and pitch before the (private) RFP.

Source: ClinicalTrials.gov API v2 (free, no key, public data — no PII).
This is the COMPLIANT alternative to scraping LinkedIn for "manufacturer seeking pharmacy".

Usage:
    python3 pipeline_monitor.py
    python3 pipeline_monitor.py --dry-run
"""
import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

API = "https://clinicaltrials.gov/api/v2/studies"
FILTERS_PATH = C.CONFIG_DIR / "pipeline_filters.json"
SEEN_PATH = C.OPP_DIR / "seen_pipeline.jsonl"

FIELDS = ("NCTId,BriefTitle,LeadSponsorName,LeadSponsorClass,Phase,Condition,"
          "PrimaryCompletionDate,OverallStatus,InterventionName")


def months_out(months: int) -> str:
    d = dt.date.today() + dt.timedelta(days=int(months * 30.4))
    return d.isoformat()


def fetch_sponsor(sponsor: str, filters: dict) -> list:
    params = {
        "query.spons": sponsor,
        "aggFilters": "phase:3,status:rec act com",
        "fields": FIELDS,
        "pageSize": filters.get("max_per_sponsor", 80),
        "format": "json",
    }
    data = C.http_get_json(API, params, timeout=filters.get("request_timeout_s", 30))
    return data.get("studies", []) if isinstance(data, dict) else []


def extract(study: dict) -> dict:
    p = study.get("protocolSection", {})
    idm = p.get("identificationModule", {})
    sp = p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {})
    st = p.get("statusModule", {})
    des = p.get("designModule", {})
    cond = p.get("conditionsModule", {}).get("conditions", [])
    arms = p.get("armsInterventionsModule", {}).get("interventions", [])
    drugs = [a.get("name", "") for a in arms if a.get("type") in ("DRUG", "BIOLOGICAL")] or \
            [a.get("name", "") for a in arms]
    return {
        "nct": idm.get("nctId", ""),
        "title": idm.get("briefTitle", ""),
        "sponsor": sp.get("name", ""),
        "sponsor_class": sp.get("class", ""),
        "phases": des.get("phases", []),
        "status": st.get("overallStatus", ""),
        "completion": (st.get("primaryCompletionDateStruct", {}) or {}).get("date", ""),
        "conditions": cond,
        "drugs": [d for d in drugs if d][:3],
    }


def score(rec: dict, filters: dict) -> dict:
    why = []
    s = 0
    if any(sp.lower() in rec["sponsor"].lower() for sp in filters["sponsors"]):
        s += 5
        why.append("major manufacturer")
    blob = (" ".join(rec["conditions"]) + " " + rec["title"]).lower()
    tas = sorted({c for c in filters["specialty_conditions"] if c in blob})
    if tas:
        s += 2
        why.append("specialty: " + ", ".join(tas[:4]))
    # completion proximity
    comp = rec["completion"]
    if comp:
        try:
            cdate = dt.date.fromisoformat((comp + "-01")[:10]) if len(comp) == 7 else dt.date.fromisoformat(comp)
            months = (cdate - dt.date.today()).days / 30.4
            if -6 <= months <= 12:
                s += 3
                why.append("launches within ~12mo")
            elif 12 < months <= 18:
                s += 1
                why.append("launches ~12-18mo")
        except Exception:
            pass
    return {"score": s, "why": why, "tas": tas}


def in_window(rec: dict, lo: str, hi: str) -> bool:
    c = rec["completion"]
    if not c:
        return False
    c = (c + "-01")[:10] if len(c) == 7 else c
    return lo <= c <= hi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    filters = C.load_json(FILTERS_PATH)

    lo = months_out(filters.get("completion_from_months", -6))
    hi = months_out(filters.get("completion_to_months", 18))
    print(f"Pipeline monitor: industry Phase 3 completing {lo} → {hi}")

    seen_nct, collected = set(), []
    for sponsor in filters["sponsors"]:
        try:
            studies = fetch_sponsor(sponsor, filters)
        except Exception as ex:
            print(f"  {sponsor}: error {ex}")
            continue
        kept = 0
        for st in studies:
            rec = extract(st)
            if rec["sponsor_class"] != "INDUSTRY":
                continue
            if not in_window(rec, lo, hi):
                continue
            if rec["nct"] in seen_nct:
                continue
            rec["_s"] = score(rec, filters)
            if rec["_s"]["score"] >= filters.get("min_score", 5):
                seen_nct.add(rec["nct"])
                collected.append(rec)
                kept += 1
        print(f"  {sponsor}: {kept}")

    collected.sort(key=lambda r: (r["_s"]["score"], r["completion"]), reverse=True)

    prior = C.load_seen(SEEN_PATH)
    new = [r for r in collected if r["nct"] not in prior]

    blocks = []
    for r in collected:
        blocks.append([
            f"## [{r['_s']['score']}] {', '.join(r['drugs']) or r['title'][:50]} — {r['sponsor']}",
            f"- **Manufacturer:** {r['sponsor']}",
            f"- **Drug(s):** {', '.join(r['drugs']) or '(see title)'}",
            f"- **Indication:** {', '.join(r['conditions'][:3])}",
            f"- **Phase/Status:** {', '.join(r['phases'])} · {r['status']} · est. completion {r['completion']}",
            f"- **Why:** {'; '.join(r['_s']['why'])}",
            f"- **Link:** https://clinicaltrials.gov/study/{r['nct']}",
        ])
    run_date = dt.date.today().strftime("%Y-%m-%d")
    intro = [f"_{len(new)} new of {len(collected)} late-stage specialty programs from major manufacturers — pitch ahead of the LDD network._"]
    digest = C.write_digest(f"digest_pipeline_{run_date}.md",
                            f"Manufacturer Pipeline Watchlist — {run_date}", intro, blocks)

    current = [{
        "kind": "pipeline", "source": "Manufacturer pipeline", "title": ", ".join(r["drugs"]) or r["title"],
        "org": r["sponsor"], "score": r["_s"]["score"], "drugs": r["drugs"],
        "conditions": r["conditions"][:3], "status": r["status"], "phases": r["phases"],
        "award_date": r["completion"], "why": r["_s"]["why"], "nct": r["nct"],
        "link": f"https://clinicaltrials.gov/study/{r['nct']}",
    } for r in collected]
    C.write_current("pipeline", current)

    if not args.dry_run and new:
        C.append_seen(SEEN_PATH, [(r["nct"], f"{r['sponsor']}: {', '.join(r['drugs'])[:40]}") for r in new])

    print(f"\nDone. {len(collected)} programs ({len(new)} new). Digest: {digest}")
    for r in collected[:8]:
        print(f"  [{r['_s']['score']}] {r['sponsor'][:18]:18} {(', '.join(r['drugs']) or r['title'])[:46]}")


if __name__ == "__main__":
    main()
