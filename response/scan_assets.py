#!/usr/bin/env python3
"""
Scan the historical RFP folder and build an asset inventory — "what we have".

Categorizes every reusable artifact (supporting docs, CVs/bios, case studies,
questionnaires, pricing, proposals) so the dashboard can show have/don't-have
against a new RFP's submission checklist. Reads inside .zip archives too.

Output: knowledge_base/asset_inventory.json
Stdlib only.
"""
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORICAL = ROOT / "RFP Perigon Folder Historical"
OUT = ROOT / "knowledge_base" / "asset_inventory.json"

# (category, regex) — first match wins. Categories align with the submission checklist.
RULES = [
    ("company_overview_360_bios", r"360.*bio|perigonhealth.*bio"),
    ("cv_bio", r"\bcv\b|curriculum|bios?\b"),
    ("job_description", r"\bjd\b|job descr"),
    ("org_chart", r"org chart|organizational|account management structure"),
    ("case_study", r"case study"),
    ("vendor_questionnaire", r"questionnaire|rfp questions|vendor question"),
    ("pricing_cost", r"cost template|pricing|\bcost\b"),
    ("financials", r"financ"),
    ("code_of_conduct", r"code of conduct|cmp001"),
    ("privacy_policy", r"privacy"),
    ("emergency_dr", r"emergency|disaster|adm005"),
    ("insurance", r"insurance"),
    ("nda_noncompete", r"noncompet|non-competition|nondisclosure|nonsolicit"),
    ("fulfillment_flow", r"fulfillment flow|pharmacy fulfillment"),
    ("process_flow", r"process flow|patient program"),
    ("platform_screenshots", r"platform screenshot|medesto platform"),
    ("supplier_diversity", r"supplier diversit"),
    ("intent_to_bid", r"intent"),
    ("proposal_deck", r"proposal|\brfp\b.*\.pptx|hub rfp|neurology"),
]


def categorize(name: str) -> str:
    low = name.lower()
    for cat, pat in RULES:
        if re.search(pat, low):
            return cat
    return "other"


def collect():
    items = []  # (display_name, source_path, category)
    if not HISTORICAL.exists():
        return items
    for p in sorted(HISTORICAL.rglob("*")):
        if p.is_dir() or p.name == ".DS_Store":
            continue
        if p.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(p) as z:
                    for n in z.namelist():
                        if n.endswith("/"):
                            continue
                        base = Path(n).name
                        items.append((base, f"{p.name}::{n}", categorize(base)))
            except Exception:
                items.append((p.name, str(p), categorize(p.name)))
        else:
            items.append((p.name, str(p.relative_to(ROOT)), categorize(p.name)))
    return items


def main():
    items = collect()
    cats = {}
    seen_names = {}  # cat -> set of lowercased names (dedupe copies that exist loose + in the zip)
    for name, path, cat in items:
        key = name.lower()
        if key in seen_names.setdefault(cat, set()):
            continue
        seen_names[cat].add(key)
        cats.setdefault(cat, []).append({"name": name, "path": path})
    inv = {
        "historical_folder": str(HISTORICAL),
        "total_files": len(items),
        "categories_present": sorted(c for c in cats if c != "other"),
        "by_category": cats,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(inv, indent=2))
    print(f"Scanned {len(items)} files into {len(cats)} categories -> {OUT}")
    for c in sorted(cats):
        print(f"  {c}: {len(cats[c])}")


if __name__ == "__main__":
    main()
