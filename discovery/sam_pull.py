#!/usr/bin/env python3
"""
SAM.gov RFP discovery puller — Perigon specialty pharmacy funnel.

Pulls federal opportunities from the SAM.gov Get Opportunities API (v2),
filters/scores them for pharmacy relevance, dedupes against a local store,
and writes a ranked markdown digest of NEW opportunities.

Stdlib only — no pip installs.

QUOTA REALITY (learned the hard way): non-federal SAM.gov keys get ~10
requests/DAY, and the gateway *stalls* over-limit requests ~60s before
returning HTTP 429. So this does ONE bulk, unfiltered pull per run
(1-3 paginated calls), then filters/scores entirely client-side. It stops
the moment it sees a 429 and writes a digest from whatever it already has.

Usage:
    python3 sam_pull.py                # daily run
    python3 sam_pull.py --days 7       # wider backfill (uses more pages/calls)
    python3 sam_pull.py --enrich 5     # fetch description text for top 5 (extra calls — spend quota carefully)
    python3 sam_pull.py --dry-run      # don't update the seen-store
    python3 sam_pull.py --selftest     # offline: run filter/score/digest on synthetic data, no network
"""
import argparse
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
OPP_DIR = ROOT / "data" / "opportunities"
DIGEST_DIR = OPP_DIR / "digests"
SEEN_PATH = OPP_DIR / "seen.jsonl"
ENV_PATH = CONFIG_DIR / ".env"
FILTERS_PATH = CONFIG_DIR / "filters.json"

API_BASE = "https://api.sam.gov/opportunities/v2/search"


# ---------- config / env ----------

def load_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def load_filters(path: Path) -> dict:
    return json.loads(path.read_text())


# ---------- API ----------

class RateLimited(Exception):
    pass


def api_get(params: dict, api_key: str, timeout: int) -> dict:
    q = dict(params)
    q["api_key"] = api_key
    url = API_BASE + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited()
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {e.code}: {body}")


def fetch_window(date_params: dict, api_key: str, filters: dict) -> tuple:
    """One unfiltered bulk pull, paginated. Returns (records, calls_used, rate_limited, truncated)."""
    page_size = filters.get("page_size", 1000)
    max_pages = filters.get("max_pages", 6)
    timeout = filters.get("request_timeout_s", 45)
    wall = filters.get("max_seconds", 120)
    out, calls, offset = [], 0, 0
    start = time.monotonic()
    for page in range(max_pages):
        if time.monotonic() - start > wall:
            # When quota is exhausted SAM throttles via 45-60s latency (not always 429),
            # so cap total wall-clock to keep the daily refresh from hanging.
            print(f"  [wall-clock {wall}s exceeded after {calls} call(s) — stopping; API is throttling]")
            return out, calls, True, False
        params = dict(date_params, limit=page_size, offset=offset)
        try:
            data = api_get(params, api_key, timeout)
        except RateLimited:
            print(f"  [429 after {calls} call(s) — stopping, using partial data]")
            return out, calls, True, False
        except Exception as ex:
            print(f"  [request failed after {calls} call(s): {ex} — stopping]")
            return out, calls, True, False
        calls += 1
        rows = data.get("opportunitiesData") or []
        out.extend(rows)
        total = data.get("totalRecords", 0)
        print(f"  page {page + 1}: +{len(rows)}  (window total={total}, have={len(out)})")
        offset += page_size
        if offset >= total or not rows:
            return out, calls, False, False
    return out, calls, False, True  # hit max_pages


def fetch_description(url: str, api_key: str, timeout: int) -> str:
    if not url:
        return ""
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}api_key={urllib.parse.quote(api_key)}"
    try:
        req = urllib.request.Request(full, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            j = json.loads(raw)
            return j.get("description", raw) if isinstance(j, dict) else raw
        except json.JSONDecodeError:
            return raw
    except Exception:
        return ""


# ---------- filter / score ----------

def is_candidate(rec: dict, filters: dict) -> bool:
    """Cheap client-side prefilter: NAICS, PSC, or a title-keyword hit."""
    if str(rec.get("naicsCode") or "") in set(filters["naics_codes"]):
        return True
    if str(rec.get("classificationCode") or "") in set(filters["psc_codes"]):
        return True
    title = (rec.get("title") or "").lower()
    return any(kw.lower() in title for kw in filters["title_keywords"])


def days_until(deadline: str):
    if not deadline:
        return None
    try:
        d = dt.datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        now = dt.datetime.now(d.tzinfo) if d.tzinfo else dt.datetime.now()
        return (d - now).days
    except Exception:
        return None


def score_record(rec: dict, filters: dict, text_blob: str) -> dict:
    blob = text_blob.lower()
    score, matched = 0, []
    for kw, w in filters["score_keywords"].items():
        if kw.lower() in blob:
            score += w
            matched.append(kw)
    if str(rec.get("naicsCode") or "") in set(filters["naics_codes"]):
        score += 5
    if str(rec.get("classificationCode") or "") in set(filters["psc_codes"]):
        score += 5
    return {"score": score, "matched": sorted(set(matched)),
            "days_left": days_until(rec.get("responseDeadLine"))}


# ---------- store ----------

def load_seen(path: Path) -> set:
    seen = set()
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                seen.add(json.loads(line)["noticeId"])
            except Exception:
                pass
    return seen


def append_seen(path: Path, records: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in records:
            f.write(json.dumps({"noticeId": r["noticeId"],
                                "firstSeen": dt.datetime.now().isoformat(timespec="seconds"),
                                "title": r.get("title", "")}) + "\n")


# ---------- digest ----------

def write_digest(scored_new: list, run_date: str, stats: dict) -> Path:
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    out = DIGEST_DIR / f"digest_{run_date}.md"
    lines = [f"# RFP Digest — {run_date}", ""]
    lines.append(f"_{stats['new']} new pharmacy-relevant opportunities "
                 f"({stats['candidates']} candidates from {stats['scanned']} federal notices, "
                 f"{stats['api_calls']} API call(s))._")
    if stats.get("rate_limited"):
        lines.append("\n> ⚠️ Hit the SAM.gov daily rate limit mid-run — results are PARTIAL. "
                     "Quota resets ~midnight ET. Lower `lookback_days`/`max_pages` if this recurs.")
    if stats.get("truncated"):
        lines.append("\n> ℹ️ Hit `max_pages` — the window had more notices than we paged through. "
                     "Raise `max_pages` or shorten `lookback_days`.")
    lines.append("")
    if not scored_new:
        lines.append("No new opportunities above threshold today. Federal pharmacy volume is low — "
                     "this is normal. The bigger pipeline is non-federal (see README roadmap).")
        out.write_text("\n".join(lines))
        return out

    for r in scored_new:
        s = r["_score"]
        dl = s["days_left"]
        dl_str = (f"{dl} days left" if dl is not None and dl >= 0
                  else "deadline passed/unknown" if dl is None else f"CLOSED ({dl}d)")
        lines += [
            f"## [{s['score']}] {r.get('title', '(no title)')}",
            f"- **Agency:** {r.get('fullParentPathName', 'n/a')}",
            f"- **Type:** {r.get('type', 'n/a')}  |  **Set-aside:** {r.get('typeOfSetAsideDescription') or 'None'}",
            f"- **NAICS:** {r.get('naicsCode', 'n/a')}  |  **PSC:** {r.get('classificationCode', 'n/a')}",
            f"- **Posted:** {r.get('postedDate', 'n/a')}  |  **Deadline:** {r.get('responseDeadLine', 'n/a')} ({dl_str})",
            f"- **Matched:** {', '.join(s['matched']) or '(metadata match only)'}",
            f"- **Attachments:** {len(r.get('resourceLinks') or [])}",
            f"- **Link:** {r.get('uiLink', 'n/a')}",
            f"- **Notice ID:** `{r.get('noticeId')}`",
            "",
        ]
    out.write_text("\n".join(lines))
    return out


# ---------- pipeline (shared by live run + selftest) ----------

def process(records: list, filters: dict, run_date: str, stats: dict,
            api_key: str = "", enrich_n: int = 0, dry_run: bool = False) -> tuple:
    candidates = [r for r in records if is_candidate(r, filters)]
    keep_types = set(filters.get("notice_types_include", []))
    if keep_types:
        candidates = [r for r in candidates if r.get("type") in keep_types]

    for r in candidates:
        r["_score"] = score_record(r, filters, r.get("title", ""))
    candidates.sort(key=lambda r: r["_score"]["score"], reverse=True)

    if enrich_n and api_key:
        for r in candidates[:enrich_n]:
            desc = fetch_description(r.get("description", ""), api_key, filters.get("request_timeout_s", 90))
            if desc:
                r["_score"] = score_record(r, filters, r.get("title", "") + "\n" + desc)
        candidates.sort(key=lambda r: r["_score"]["score"], reverse=True)

    min_score = filters.get("min_score", 4)
    relevant = [r for r in candidates if r["_score"]["score"] >= min_score]
    prior_seen = load_seen(SEEN_PATH)
    new = [r for r in relevant if r.get("noticeId") not in prior_seen]

    stats.update(candidates=len(candidates), new=len(new))
    digest = write_digest(new, run_date, stats)
    if not dry_run and new:
        append_seen(SEEN_PATH, new)
    return relevant, new, digest


# ---------- selftest ----------

def selftest(filters: dict):
    print("SELFTEST (offline, no network)")
    fake = [
        {"noticeId": "T1", "title": "Specialty Pharmacy Services for VA Medical Center",
         "type": "Solicitation", "naicsCode": "446110", "classificationCode": "Q510",
         "responseDeadLine": "2026-07-30T17:00:00-04:00", "fullParentPathName": "VA",
         "uiLink": "https://sam.gov/opp/T1", "resourceLinks": ["a.pdf"]},
        {"noticeId": "T2", "title": "340B Program Administration and Medication Adherence Support",
         "type": "Combined Synopsis/Solicitation", "naicsCode": "621999", "classificationCode": "6505",
         "responseDeadLine": "2026-08-15T12:00:00-05:00", "fullParentPathName": "HHS"},
        {"noticeId": "T3", "title": "Dredging of Inner Harbor",
         "type": "Solicitation", "naicsCode": "237990", "classificationCode": "Z2AA"},
        {"noticeId": "T4", "title": "Motor, Alternating Current",
         "type": "Award Notice", "naicsCode": "335312", "classificationCode": "6105"},
    ]
    rel, new, digest = process(fake, filters, "selftest",
                               {"scanned": len(fake), "api_calls": 0, "rate_limited": False, "truncated": False},
                               dry_run=True)
    print(f"  scanned={len(fake)} relevant={len(rel)} new={len(new)}")
    for r in rel:
        print(f"    [{r['_score']['score']}] {r['title'][:55]}  matched={r['_score']['matched']}")
    assert {r["noticeId"] for r in rel} == {"T1", "T2"}, "expected T1+T2 to pass, others filtered"
    print(f"  PASS — filtering+scoring correct. (digest written to {digest})")


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--enrich", type=int, default=None, help="fetch descriptions for top N (extra API calls)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    filters = load_filters(FILTERS_PATH)
    if args.selftest:
        selftest(filters)
        return

    env = load_env(ENV_PATH)
    api_key = env.get("SAM_API_KEY")
    if not api_key:
        sys.exit(f"No SAM_API_KEY in {ENV_PATH}")

    lookback = args.days or filters.get("lookback_days", 3)
    today = dt.date.today()
    posted_from = (today - dt.timedelta(days=lookback)).strftime("%m/%d/%Y")
    posted_to = today.strftime("%m/%d/%Y")
    print(f"SAM.gov bulk pull: {posted_from} → {posted_to} (lookback {lookback}d)")

    records, calls, rate_limited, truncated = fetch_window(
        {"postedFrom": posted_from, "postedTo": posted_to}, api_key, filters)
    print(f"Pulled {len(records)} federal notices in {calls} call(s).")

    enrich_n = args.enrich if args.enrich is not None else filters.get("enrich_top_n", 0)
    if rate_limited:
        enrich_n = 0  # don't spend more quota once limited

    run_date = today.strftime("%Y-%m-%d")
    stats = {"scanned": len(records), "api_calls": calls,
             "rate_limited": rate_limited, "truncated": truncated}
    relevant, new, digest = process(records, filters, run_date, stats,
                                    api_key=api_key, enrich_n=enrich_n, dry_run=args.dry_run)

    try:
        import common as _C
        current = [{
            "kind": "rfp", "source": "SAM.gov (federal)", "title": r.get("title", ""),
            "org": r.get("fullParentPathName", ""), "score": r["_score"]["score"],
            "date": r.get("postedDate", ""), "deadline": r.get("responseDeadLine", ""),
            "notice": r.get("type", ""), "matched": r["_score"]["matched"],
            "naics": r.get("naicsCode", ""), "setaside": r.get("typeOfSetAsideDescription") or "",
            "attachments": len(r.get("resourceLinks") or []),
            "link": r.get("uiLink", ""), "id": r.get("noticeId", ""),
            "extra": {
                "doc_links": r.get("resourceLinks") or [],
                "office": (r.get("officeAddress") or {}).get("city", ""),
                "contact": {(c.get("type") or "poc"): c.get("email", "")
                            for c in (r.get("pointOfContact") or []) if c.get("email")},
            },
        } for r in relevant]
        _C.OPP_DIR.mkdir(parents=True, exist_ok=True)
        _C.write_current("sam", current)
    except Exception as _e:
        print(f"  (current_sam.json not written: {_e})")

    print(f"\nDone. {len(relevant)} relevant ({len(new)} new). Digest: {digest}")
    for r in new[:8]:
        print(f"  [{r['_score']['score']}] {r.get('title', '')[:70]}")


if __name__ == "__main__":
    main()
