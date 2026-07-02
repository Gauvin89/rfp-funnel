#!/usr/bin/env python3
"""
DOSE monitor — clinical-trial support opportunities.

Finds ClinicalTrials.gov studies that have NOT yet started (NOT_YET_RECRUITING)
with a start date today or in the future, testing ORAL / pill medications. Each is
a chance for Perigon to win the trial's pharmacy support (IMP dispensing, patient
adherence, Medesto-Dose) BEFORE the trial begins.

Source: ClinicalTrials.gov API v2 (free, no key, public data).
Usage: python3 dose_monitor.py [--dry-run]
"""
import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

API = "https://clinicaltrials.gov/api/v2/studies"
FILTERS_PATH = C.CONFIG_DIR / "dose_filters.json"
SEEN_PATH = C.OPP_DIR / "seen_dose.jsonl"


def fetch_page(token, filters):
    params = {
        "aggFilters": "status:not",
        "query.intr": "oral",
        "pageSize": filters.get("page_size", 100),
        "format": "json",
    }
    if token:
        params["pageToken"] = token
    return C.http_get_json(API, params, timeout=filters.get("request_timeout_s", 30))


def extract(study):
    p = study.get("protocolSection", {})
    idm = p.get("identificationModule", {})
    sp = p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {})
    st = p.get("statusModule", {})
    des = p.get("designModule", {})
    arms = p.get("armsInterventionsModule", {}).get("interventions", [])
    cond = p.get("conditionsModule", {}).get("conditions", [])
    drugs = [a.get("name", "") for a in arms if a.get("type") == "DRUG"] or [a.get("name", "") for a in arms]
    return {
        "nct": idm.get("nctId", ""), "title": idm.get("briefTitle", ""),
        "sponsor": sp.get("name", ""), "sponsor_class": sp.get("class", ""),
        "status": st.get("overallStatus", ""),
        "start": (st.get("startDateStruct", {}) or {}).get("date", ""),
        "phases": des.get("phases", []), "conditions": cond[:3],
        "drugs": [d for d in drugs if d][:3],
        "interventions": [a.get("name", "") for a in arms],
    }


def is_oral(rec, filters):
    blob = (rec["title"] + " " + " ".join(rec["interventions"])).lower()
    if any(x in blob for x in filters["oral_terms"]):
        # not dominated by a non-oral route
        if not any(x in blob for x in filters["exclude_terms"]):
            return True
    return False


def future_start(rec, today):
    s = rec["start"]
    if not s:
        return False
    s = (s + "-01")[:10] if len(s) == 7 else s
    return s >= today


def score(rec, filters, today):
    s, why = 0, []
    if future_start(rec, today):
        s += 2
        why.append(f"starts {rec['start']} (not yet begun)")
    if is_oral(rec, filters):
        s += 1
        why.append("oral / pill medication")
    if rec["sponsor_class"] == "INDUSTRY":
        s += 1
        why.append("industry sponsor")
    if any(ph in ("PHASE2", "PHASE3") for ph in rec["phases"]):
        s += 1
        why.append(", ".join(rec["phases"]))
    return {"score": s, "why": why}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    filters = C.load_json(FILTERS_PATH)
    today = dt.date.today().isoformat()
    print(f"DOSE monitor: not-yet-started ORAL trials starting >= {today}")

    collected, token, seen_nct = [], None, set()
    for _ in range(filters.get("max_pages", 8)):
        data = fetch_page(token, filters)
        studies = data.get("studies", []) if isinstance(data, dict) else []
        for st in studies:
            rec = extract(st)
            if rec["status"] != "NOT_YET_RECRUITING":
                continue
            if not future_start(rec, today) or not is_oral(rec, filters):
                continue
            if rec["nct"] in seen_nct:
                continue
            rec["_s"] = score(rec, filters, today)
            if rec["_s"]["score"] >= filters.get("min_score", 2):
                seen_nct.add(rec["nct"])
                collected.append(rec)
        token = data.get("nextPageToken")
        if not token:
            break

    collected.sort(key=lambda r: (r["_s"]["score"], r["start"]), reverse=True)
    prior = C.load_seen(SEEN_PATH)
    new = [r for r in collected if r["nct"] not in prior]

    blocks = []
    for r in collected:
        blocks.append([
            f"## [{r['_s']['score']}] {', '.join(r['drugs']) or r['title'][:50]} — {r['sponsor']}",
            f"- **Sponsor:** {r['sponsor']} ({r['sponsor_class']})",
            f"- **Drug(s):** {', '.join(r['drugs']) or '(see title)'}",
            f"- **Starts:** {r['start']} · {', '.join(r['phases'])} · {r['status']}",
            f"- **Indication:** {', '.join(r['conditions'])}",
            f"- **Why:** {'; '.join(r['_s']['why'])}",
            f"- **Link:** https://clinicaltrials.gov/study/{r['nct']}",
        ])
    run_date = dt.date.today().strftime("%Y-%m-%d")
    intro = [f"_{len(new)} new of {len(collected)} not-yet-started oral trials — trial-support opportunities (dispense/adherence) before kickoff._"]
    digest = C.write_digest(f"digest_dose_{run_date}.md", f"DOSE — Trial Support Watchlist — {run_date}", intro, blocks)

    current = [{
        "kind": "dose", "source": "Trial support (DOSE)", "title": ", ".join(r["drugs"]) or r["title"],
        "org": r["sponsor"], "score": r["_s"]["score"], "drugs": r["drugs"], "conditions": r["conditions"],
        "phases": r["phases"], "start": r["start"], "deadline": r["start"], "why": r["_s"]["why"],
        "nct": r["nct"], "link": f"https://clinicaltrials.gov/study/{r['nct']}",
    } for r in collected]
    C.write_current("dose", current)

    if not args.dry_run and new:
        C.append_seen(SEEN_PATH, [(r["nct"], f"{r['sponsor']}: {', '.join(r['drugs'])[:40]}") for r in new])

    print(f"\nDone. {len(collected)} oral trials ({len(new)} new). Digest: {digest}")
    for r in collected[:8]:
        print(f"  [{r['_s']['score']}] starts {r['start']} {(', '.join(r['drugs']) or r['title'])[:42]}")


if __name__ == "__main__":
    main()
