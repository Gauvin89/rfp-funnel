#!/usr/bin/env python3
"""
State/local procurement monitor — Perigon specialty pharmacy funnel.

Pulls pharmacy-relevant solicitations from state/local sources via pluggable
adapters, dedupes, and writes a ranked digest. Adapter types:
  - socrata : clean JSON API (SoQL keyword filter)  [preferred, durable]
  - html    : scrape a server-rendered listing       [used where no API exists]
  - placeholder : documented source with no free automated path (e.g. Florida)

Config: config/state_sources.json  (add states by appending entries).
Stdlib only.

Usage:
    python3 state_monitor.py
    python3 state_monitor.py --dry-run
"""
import argparse
import datetime as dt
import html as Html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

CONFIG_PATH = C.CONFIG_DIR / "state_sources.json"
SEEN_PATH = C.OPP_DIR / "seen_state.jsonl"


# ---------- adapters: each returns list of normalized records ----------

def _record(source, _id, title, agency, notice, posted, deadline, link, extra=None):
    return {"source": source, "id": f"{source}:{_id}", "title": (title or "").strip(),
            "agency": agency or "", "notice": notice or "", "posted": posted or "",
            "deadline": deadline or "", "link": link or "", "extra": extra or {}}


def _clean_html(s: str, limit: int = 700) -> str:
    if not s:
        return ""
    t = Html.unescape(re.sub(r"<[^>]+>", " ", s))
    t = re.sub(r"[\x00-\x1f\xa0]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def adapter_socrata(src: dict, keywords: list, timeout: int) -> list:
    base = f"https://{src['domain']}/resource/{src['resource']}.json"
    clauses = []
    for f in src["text_fields"]:
        for kw in keywords:
            clauses.append(f"upper({f}) like '%{kw.upper()}%'")
    where = "(" + " OR ".join(clauses) + ")"
    if src.get("since_days") and src.get("date_field"):
        cutoff = (dt.date.today() - dt.timedelta(days=src["since_days"])).isoformat()
        where += f" AND {src['date_field']} >= '{cutoff}T00:00:00'"
    params = {"$where": where, "$limit": src.get("limit", 200)}
    if src.get("order"):
        params["$order"] = src["order"]
    # Socrata full-table LIKE scans can be slow/flaky — retry once on timeout.
    rows = None
    for attempt in range(2):
        try:
            rows = C.http_get_json(base, params, timeout=timeout)
            break
        except Exception:
            if attempt == 1:
                raise
    if isinstance(rows, dict):
        rows = rows.get("results", [])
    exclude = [c.lower() for c in src.get("exclude_categories", [])]
    cf = src.get("contact_fields", {})
    out = []
    for r in rows:
        category = r.get(src.get("category_field", ""), "") or ""
        if any(x in category.lower() for x in exclude):
            continue  # scrub noise (e.g. construction projects that merely mention "pharmacy")
        contact = {k: r.get(v, "") for k, v in cf.items() if r.get(v)}
        extra = {
            "pin": r.get(src.get("pin_field", ""), ""),
            "category": category,
            "selection": r.get(src.get("selection_field", ""), ""),
            "body": _clean_html(r.get(src.get("body_field", ""), "")),
            "contact": contact,
            "search_url": src.get("human_url", ""),
        }
        out.append(_record(
            src["name"], r.get(src["id_field"], r.get("pin", "")),
            r.get(src["title_field"], ""), r.get(src.get("agency_field", ""), ""),
            r.get(src.get("notice_field", ""), ""),
            (r.get(src.get("date_field", "")) or "")[:10],
            (r.get(src.get("deadline_field", "")) or "")[:10],
            src.get("human_url", ""), extra=extra))
    return out


def adapter_html(src: dict, keywords: list, timeout: int) -> list:
    raw = _http_text(src["url"], timeout)
    if src.get("parser") == "mi_accordion":
        return _parse_mi_accordion(raw, src, keywords)
    return []


def _parse_mi_accordion(raw: str, src: dict, keywords: list) -> list:
    # Split on item boundaries — accordion items contain nested <li>, so a
    # non-greedy '.*?</li>' would truncate them mid-content.
    parts = re.split(r'(?=<li class="item accordion-item")', raw)
    items = [p for p in parts if p.startswith('<li class="item accordion-item"')]
    skip = set(s.lower() for s in src.get("skip_status", []))
    out = []
    for it in items:
        text = Html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", it))).strip()
        low = text.lower()
        if not any(kw.lower() in low for kw in keywords):
            continue
        head = text.split("Download")[0].strip()
        parts = [p.strip() for p in head.split("|")]
        bid_no = parts[0] if parts else ""
        title = parts[1] if len(parts) > 1 else head
        status_part = parts[2] if len(parts) > 2 else ""
        status_word = status_part.split()[0].lower() if status_part else ""
        if status_word in skip:
            continue
        href = next((h for h in re.findall(r'href="([^"]+)"', it) if not h.startswith("mailto")), "")
        link = (src.get("link_base", "") + href) if href.startswith("/") else href
        out.append(_record(src["name"], bid_no, title, "State of Michigan (DTMB)",
                           status_part or "Bid Proposal", "", "", link))
    return out


def _http_text(url: str, timeout: int) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": C.UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


# ---------- scoring ----------

def score(rec: dict, keywords: list) -> dict:
    blob = (rec["title"] + " " + rec["notice"]).lower()
    matched = sorted({kw for kw in keywords if kw.lower() in blob})
    return {"score": len(matched), "matched": matched}


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = C.load_json(CONFIG_PATH)
    keywords = cfg["keywords"]
    timeout = cfg.get("request_timeout_s", 45)
    min_score = cfg.get("min_score", 1)

    all_recs, notes = [], []
    for src in cfg["sources"]:
        t = src.get("type")
        try:
            if t == "socrata":
                recs = adapter_socrata(src, keywords, timeout)
            elif t == "html":
                recs = adapter_html(src, keywords, timeout)
            elif t == "placeholder":
                notes.append(f"- **{src['name']}** — not automated. {src.get('note','')}")
                continue
            else:
                continue
            print(f"  {src['name']}: {len(recs)} pharmacy-matched")
            all_recs.extend(recs)
        except C.RateLimited:
            print(f"  {src['name']}: rate limited — skipped")
            notes.append(f"- **{src['name']}** — rate limited this run.")
        except Exception as e:
            print(f"  {src['name']}: ERROR {e}")
            notes.append(f"- **{src['name']}** — error: {e}")

    scored = []
    for r in all_recs:
        s = score(r, keywords)
        if s["score"] >= min_score:
            r["_s"] = s
            scored.append(r)
    scored.sort(key=lambda r: (r["posted"], r["_s"]["score"]), reverse=True)

    seen = C.load_seen(SEEN_PATH)
    new = [r for r in scored if r["id"] not in seen]

    blocks = []
    for r in new:
        dl = C.days_until(r["deadline"]) if r["deadline"] else None
        dl_str = f"  ({dl} days left)" if dl is not None else ""
        blocks.append([
            f"## [{r['_s']['score']}] {r['title'] or '(untitled)'}",
            f"- **Source:** {r['source']}  |  **Agency:** {r['agency']}",
            f"- **Notice:** {r['notice']}  |  **Posted:** {r['posted'] or 'n/a'}  |  **Deadline:** {r['deadline'] or 'n/a'}{dl_str}",
            f"- **Matched:** {', '.join(r['_s']['matched'])}",
            f"- **Link:** {r['link'] or 'n/a'}",
            f"- **ID:** `{r['id']}`",
        ])

    run_date = dt.date.today().strftime("%Y-%m-%d")
    intro = [f"_{len(new)} new pharmacy-relevant solicitations (of {len(scored)} matched across automated sources)._"]
    if notes:
        intro.append("")
        intro.append("**Source notes:**")
        intro.extend(notes)
    digest = C.write_digest(f"digest_state_{run_date}.md", f"State/Local RFP Digest — {run_date}",
                            intro, blocks)

    current = []
    for r in scored:
        current.append({
            "kind": "rfp", "source": r["source"], "title": r["title"], "org": r["agency"],
            "score": r["_s"]["score"], "date": r["posted"], "deadline": r["deadline"],
            "notice": r["notice"], "matched": r["_s"]["matched"], "link": r["link"], "id": r["id"],
            "extra": r.get("extra", {}),
        })
    C.write_current("state", current)

    if not args.dry_run and new:
        C.append_seen(SEEN_PATH, [(r["id"], r["title"]) for r in new])

    print(f"\nDone. {len(scored)} matched ({len(new)} new). Digest: {digest}")
    for r in new[:8]:
        print(f"  [{r['_s']['score']}] {r['source'][:18]:18} {r['title'][:50]}")


if __name__ == "__main__":
    main()
