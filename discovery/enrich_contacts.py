#!/usr/bin/env python3
"""
Enrich Lead List contacts via the Apollo.io People Match API.

For every lead contact (name + company) it fills LinkedIn URL, title, verified
email, and phone where available, writing a structured `people` array into
data/opportunities/current_manufacturers.json (gitignored) so the board shows
first/last + email + LinkedIn per person.

⚠️ The Apollo enrichment API requires a PAID plan (blocked on Free). Key is read
from APOLLO_API_KEY in config/.env (gitignored). Contact data never leaves the
gitignored data tree; only names + company are sent to Apollo.

Results are cached in data/opportunities/apollo_cache.json so re-runs (and the
nightly refresh) only spend credits on NEW contacts.

Usage:
  python3 discovery/enrich_contacts.py --dry-run     # who'd be matched, no API calls / no credits
  python3 discovery/enrich_contacts.py --limit 3     # enrich first 3 leads (test)
  python3 discovery/enrich_contacts.py --only Salix  # one company
  python3 discovery/enrich_contacts.py               # enrich all (uncached only)
  python3 discovery/enrich_contacts.py --refresh     # re-enrich even cached contacts
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

MANUF = C.OPP_DIR / "current_manufacturers.json"
CACHE = C.OPP_DIR / "apollo_cache.json"
ENVF = C.ROOT / "config" / ".env"
MATCH = "https://api.apollo.io/api/v1/people/match"


def env_val(key):
    if ENVF.exists():
        for line in ENVF.read_text().splitlines():
            if line.strip().startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get(key, "")


def split_name(n):
    n = re.sub(r"\s+", " ", (n or "").strip())
    n = re.sub(r",.*$", "", n)                 # drop ", MD" / ", PharmD"
    n = re.sub(r"\s+or\s+.*$", "", n, flags=re.I)  # "Mike or Dana" -> "Mike"
    parts = [p for p in n.split(" ") if p]
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return (parts[0] if parts else ""), ""


def apollo_match(first, last, org, key, timeout=30):
    body = json.dumps({"first_name": first, "last_name": last, "organization_name": org,
                       "reveal_personal_emails": True}).encode()
    req = urllib.request.Request(MATCH, data=body, method="POST", headers={
        "Content-Type": "application/json", "Cache-Control": "no-cache", "X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def person_from(resp, first, last, org):
    p = (resp or {}).get("person") or {}
    email = p.get("email") or ""
    if "email_not_unlocked" in email or "not_unlocked" in email:
        email = ""
    phone = ""
    for ph in (p.get("phone_numbers") or []):
        phone = ph.get("sanitized_number") or ph.get("raw_number") or ""
        if phone:
            break
    return {
        "first": first, "last": last,
        "name": p.get("name") or f"{first} {last}".strip(),
        "title": p.get("title", "") or "",
        "linkedin": p.get("linkedin_url", "") or "",
        "email": email, "email_status": p.get("email_status", "") or "",
        "phone": phone,
        "org": ((p.get("organization") or {}) or {}).get("name", "") or org,
        "matched": bool(p),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", default=None)
    ap.add_argument("--refresh", action="store_true", help="ignore cache, re-enrich")
    ap.add_argument("--sleep", type=float, default=0.6)
    args = ap.parse_args()

    if not MANUF.exists():
        raise SystemExit(f"✗ {MANUF} not found — run leads_import.py first.")
    data = json.loads(MANUF.read_text())
    recs = data.get("records", [])
    targets = recs
    if args.only:
        targets = [r for r in recs if args.only.lower() in (r.get("org", "") + " " + r.get("title", "")).lower()]
    if args.limit:
        targets = targets[:args.limit]

    key = env_val("APOLLO_API_KEY")
    if not key and not args.dry_run:
        raise SystemExit("✗ APOLLO_API_KEY not set in config/.env")
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}

    leads_done, calls, filled = 0, 0, 0
    for r in targets:
        org = r.get("org", "")
        names = (r.get("contact") or {}).get("names") or []
        people = []
        for nm in names:
            first, last = split_name(nm)
            if not first:
                continue
            ck = f"{first.lower()}|{last.lower()}|{org.lower()}"
            if ck in cache and not args.refresh:
                people.append(cache[ck])
                continue
            if args.dry_run:
                print(f"  would match: {first} {last} @ {org}")
                people.append({"first": first, "last": last, "name": nm, "title": "",
                               "linkedin": "", "email": "", "phone": "", "org": org, "matched": None})
                continue
            try:
                resp = apollo_match(first, last, org, key)
                calls += 1
                if isinstance(resp, dict) and resp.get("error_code") == "API_INACCESSIBLE":
                    raise SystemExit("✗ " + resp.get("error", "Apollo API not accessible") +
                                     "\n  Upgrade Apollo to a paid plan (Professional), then re-run.")
                per = person_from(resp, first, last, org)
                cache[ck] = per
                people.append(per)
                if per.get("linkedin") or per.get("email") or per.get("phone"):
                    filled += 1
                time.sleep(args.sleep)
            except urllib.error.HTTPError as ex:
                detail = ex.read().decode()[:160]
                print(f"  ✗ {first} {last} @ {org}: HTTP {ex.code} {detail}")
                people.append({"first": first, "last": last, "name": nm, "org": org, "matched": False})
            except Exception as ex:  # noqa: BLE001
                print(f"  ✗ {first} {last} @ {org}: {ex}")
                people.append({"first": first, "last": last, "name": nm, "org": org, "matched": False})
        if people:
            r["people"] = people
            c = r.setdefault("contact", {})
            em = [p["email"] for p in people if p.get("email")]
            c["emails"] = list(dict.fromkeys((c.get("emails") or []) + em))
            leads_done += 1
            got = sum(1 for p in people if p.get("linkedin") or p.get("email") or p.get("phone"))
            print(f"  ✓ {org}: {got}/{len(people)} contacts have data")

    if not args.dry_run:
        MANUF.write_text(json.dumps(data, indent=2))
        CACHE.write_text(json.dumps(cache, indent=2))
    print(f"\n{'DRY RUN — ' if args.dry_run else ''}{leads_done} leads processed, "
          f"{calls} Apollo calls, {filled} newly filled.")


if __name__ == "__main__":
    main()
