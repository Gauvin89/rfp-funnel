#!/usr/bin/env python3
"""
Import the Lead List (manufacturer BD contacts) into the board.

Reads a LOCAL CSV (kept under data/leads/, which is gitignored) and writes
data/opportunities/current_manufacturers.json in the shared record shape so the
dashboard can render a card + contact popup per manufacturer.

⚠️ This file contains NO contact data itself — every name/email/note stays in the
gitignored data/ tree and never touches version control. Safe to commit.

Usage: python3 discovery/leads_import.py [--csv PATH]
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

LEADS_DIR = C.ROOT / "data" / "leads"
DEFAULT_CSV = LEADS_DIR / "lead_list_2026.csv"
OUT = C.OPP_DIR / "current_manufacturers.json"
DAILYMED = "https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json"

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# words that mean "no concrete product named" — used to reject vague product tokens
STOP = {
    "drugs", "drug", "several", "multiple", "various", "study", "studies", "phase",
    "opportunities", "opportunity", "product", "products", "services", "service",
    "trials", "trial", "other", "also", "new", "current", "late", "stage", "etc",
    "development", "pipeline", "non", "payer", "sp", "focus", "pbm", "workman", "comp",
    "pdt", "partner", "bag", "white", "we", "dispense", "using", "hub", "as", "and",
    "want", "access", "to", "the", "our", "their", "brand", "name", "or", "equal",
    "therapies", "therapy", "nearing", "pdufa", "acquisition", "commercialization",
    "whoever", "is", "raremed", "orphan", "immunomodulating", "plus", "through", "any",
    "for", "clinical", "ldd", "ldds", "injection", "bio", "similars", "biosimilars",
    "trade", "clients", "they", "service", "several", "workman",
}


def named_products(field):
    """Return concrete drug/brand names from a free-text Product(s) cell (empty if vague)."""
    out = []
    for part in re.split(r"[;,/\n]| and ", field or ""):
        part = re.sub(r"\(.*?\)", "", part)          # drop parentheticals
        toks = [t.strip("-") for t in re.findall(r"[A-Za-z0-9\-]+", part)]
        clean = [t for t in toks if t and t.lower() not in STOP]
        cand = " ".join(clean[:2]).strip()
        if len(cand) > 2 and re.search(r"[A-Za-z]", cand) and not cand.isdigit():
            out.append(cand)
    seen = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return seen[:4]


def parse_contacts(field):
    raw = field or ""
    emails = list(dict.fromkeys(EMAIL_RE.findall(raw)))
    text = EMAIL_RE.sub("", raw)
    names = []
    for chunk in re.split(r"[\n,]", text):
        n = re.sub(r"\*+", "", chunk).strip(" \t-")
        n = re.sub(r"\s+", " ", n)
        low = n.lower()
        if len(n) > 1 and not low.startswith(("whoever", "trade")):
            names.append(n)
    return names[:6], emails[:6]


def score(named, emails, owner, notes):
    s = 30
    if named:
        s += 20
    if emails:
        s += 12
    if owner:
        s += 8
    nl = (notes or "").lower()
    for kw, pts in (("scheduled", 15), ("had meeting", 12), ("meeting", 10),
                    ("met ", 8), ("follow up", 5), ("sent", 3)):
        if kw in nl:
            s += pts
            break
    return min(s, 99)


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def _dm_lookup(name):
    try:
        d = C.http_get_json(DAILYMED, {"drug_name": name, "pagesize": 1}, timeout=15)
        data = d.get("data") or []
        if data and data[0].get("setid"):
            return data[0]["setid"], (data[0].get("title") or "")
    except Exception:
        pass
    return None, ""


def resolve_med(drug, cache):
    """Resolve a drug name to its DailyMed prescribing-information PDF (public URL)."""
    key = drug.lower()
    if key in cache:
        return cache[key]
    sid, title = _dm_lookup(drug)
    if not sid and " " in drug:  # retry on the longest (brand-like) word
        tok = max(drug.split(), key=len)
        if len(tok) > 3:
            sid, title = _dm_lookup(tok)
    res = None
    if sid:
        res = {"name": drug, "setid": sid,
               "pdf": f"https://dailymed.nlm.nih.gov/dailymed/downloadpdffile.cfm?setId={sid}",
               "info": f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={sid}",
               "label": title[:90]}
    cache[key] = res
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(DEFAULT_CSV))
    ap.add_argument("--no-enrich", action="store_true",
                    help="skip DailyMed medication-PDF lookups (offline / faster)")
    args = ap.parse_args()
    path = Path(args.csv)
    OUT.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        print(f"⚠ lead CSV not found: {path} — no manufacturer cards this run.")
        if not OUT.exists():
            OUT.write_text(json.dumps({"records": []}, indent=2))
        return

    by_id, recs = {}, []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            org = (row.get("Manufacturer") or "").strip()
            if not org:
                continue
            prods = (row.get("Product(s)") or "").strip()
            owner = (row.get("Perigon Lead") or "").strip()
            notes = (row.get("Perigon Notes") or "").strip()
            names, emails = parse_contacts(row.get("Contact(s)"))
            named = named_products(prods)
            cid = "manuf:" + slugify(org)

            if cid in by_id:  # merge duplicate manufacturers (e.g. Alexion, Novo Nordisk twice)
                r = by_id[cid]
                c = r["contact"]
                c["names"] = list(dict.fromkeys(c["names"] + names))
                c["emails"] = list(dict.fromkeys(c["emails"] + emails))
                r["drugs"] = list(dict.fromkeys(r["drugs"] + named))
                if prods and prods not in r["products"]:
                    r["products"] = (r["products"] + "; " + prods).strip("; ")
                if owner and owner not in c["owner"]:
                    c["owner"] = (c["owner"] + " / " + owner).strip(" /")
                if notes:
                    c["notes"] = (c["notes"] + "  |  " + notes).strip(" |")
                continue

            rec = {
                "id": cid, "source": "Lead List 2026", "org": org, "title": org,
                "products": prods, "drugs": named,
                "score": score(named, emails, owner, notes),
                "contact": {"names": names, "emails": emails, "owner": owner, "notes": notes},
            }
            by_id[cid] = rec
            recs.append(rec)

    for r in recs:  # rescore after merges
        c = r["contact"]
        r["score"] = score(r["drugs"], c["emails"], c["owner"], c["notes"])
    recs.sort(key=lambda r: r["score"], reverse=True)

    n_pdf = 0
    if args.no_enrich:
        for r in recs:
            r["med_pdfs"] = []
    else:
        cache = {}
        for r in recs:
            meds = []
            for dr in r["drugs"][:3]:
                m = resolve_med(dr, cache)
                if m:
                    meds.append(m)
            r["med_pdfs"] = meds
            n_pdf += len(meds)

    OUT.write_text(json.dumps({"records": recs}, indent=2))
    named_ct = sum(1 for r in recs if r["drugs"])
    print(f"✓ {len(recs)} manufacturer leads → {OUT.name}  "
          f"({named_ct} with a named product, {n_pdf} medication PDFs linked)")


if __name__ == "__main__":
    main()
