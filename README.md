# RFP Funnel — Perigon Specialty Pharmacy

Two-part system to (1) **discover** relevant RFPs and (2) **draft** responses from your historical RFP library.

## Quick start

```bash
cd ~/rfp-funnel
./refresh.sh            # pull all sources + rebuild the dashboard
open dashboard.html     # the command center
```
Runs automatically every day at **1:00am** via launchd (`com.perigon.rfp-funnel`).

## Status

| Piece | Status |
|---|---|
| SAM.gov discovery (federal) | ✅ `discovery/sam_pull.py` (direct doc links via resourceLinks) |
| openFDA leading-indicator monitor | ✅ `discovery/fda_monitor.py` (new specialty approvals → pitch targets) |
| State/local discovery | ✅ `discovery/state_monitor.py` (NYC Socrata + Michigan; FL = placeholder) |
| **Dashboard** (`dashboard.html`) | ✅ `response/build_dashboard.py` — opportunities + links + have/don't-have + answer bank |
| Asset inventory + answer bank | ✅ `response/scan_assets.py`, `knowledge_base/` |
| Daily automation (1am) | ✅ `refresh.sh` + launchd |
| Per-RFP draft document generator | 🔜 next (uses PSP Vendor Questionnaire Q&A) |
| FL / PBM / commercial sources | 🔜 paid API or headless scraper (see roadmap) |

## Folder map

```
rfp-funnel/
├── discovery/          source pullers + scoring
│   └── sam_pull.py     SAM.gov federal opportunities → ranked digest
├── response/           (drafting engine — to build)
├── knowledge_base/     parsed boilerplate / capability statements (generated)
├── drafts/             generated draft response packages (output)
├── config/
│   ├── .env            SAM_API_KEY (gitignored — expires ~2026-09-26)
│   └── filters.json    NAICS, PSC, keywords, scoring weights — TUNE HERE
└── data/
    ├── opportunities/
    │   ├── seen.jsonl  dedup store (noticeId → firstSeen)
    │   └── digests/    dated markdown digests
    └── historical/     ⬅️ DROP YOUR PAST RFP RESPONSES HERE
```

## Discovery — how it works

> **Hard constraint — API quota.** Non-federal SAM.gov keys allow only **~10 requests/day**, and the
> gateway *stalls* over-limit requests ~60s before returning HTTP 429. So `sam_pull.py` does **one bulk,
> unfiltered pull per run** (1–3 paginated calls for a few-day window), then filters/scores **client-side**.
> It stops the instant it sees a 429 and writes a digest from whatever it already pulled. Server-side
> full-text (`q=`) is silently ignored by the API anyway, so client-side filtering is also higher-recall.

Each run pulls the window, keeps notices matching our **NAICS / PSC / title keywords**, scores them on
pharmacy-relevance keywords + NAICS/PSC weight, dedupes by notice ID against `seen.jsonl`, and writes a
ranked digest of **new** opportunities to `data/opportunities/digests/`.

```bash
cd ~/rfp-funnel
python3 discovery/sam_pull.py            # daily run (default lookback 3d ≈ 1 call)
python3 discovery/sam_pull.py --days 7   # wider backfill (more pages/calls)
python3 discovery/sam_pull.py --enrich 5 # fetch description text for top 5 (spends extra calls)
python3 discovery/sam_pull.py --dry-run  # don't update the seen-store
python3 discovery/sam_pull.py --selftest # offline logic check, no network/quota
```

Tune everything in `config/filters.json` (NAICS, PSC, keywords, weights, `lookback_days`, `max_pages`).
No code edits needed. **To raise the quota:** associate the API key with an entity registration in SAM.gov,
or request a federal/system account — that lifts the ~10/day cap substantially.

**Reality check:** federal pharmacy RFP volume is genuinely low. Empty digest days are normal.
The bigger pipeline is non-federal (below).

## Roadmap — broader scope (you chose federal + state + PBM/commercial)

SAM.gov is **federal only**. The larger specialty-pharmacy pipeline needs source adapters that drop
into `discovery/` alongside `sam_pull.py`, each emitting the same record shape so scoring/digest are shared:

1. **State Medicaid / procurement portals** — most states post pharmacy & PBM RFPs on their own portals
   (e.g. CaleProcure, COMMBUYS, Texas SmartBuy). Some have APIs; many need light scraping.
2. **Aggregators** — BidNet / Bonfire / GovWin surface state + local in one place (paid, but one integration).
3. **PBM / health-system / employer RFPs** — rarely public APIs; usually sourced via vendor portals,
   GPO networks, and relationships. Best handled as a manual-entry + tracking layer that still feeds
   the same scoring + drafting engine.

## Roadmap — response drafting

Once historical RFPs are in `data/historical/`:
- Parse them → extract reusable capability statements, past-performance blurbs, boilerplate, Q&A pairs → `knowledge_base/`
- For a chosen opportunity: pull the solicitation doc + attachments, extract its questions/requirements
- Draft answers from the knowledge base, **flag gaps** needing human input
- Emit a pre-filled draft package to `drafts/` for review

**Submission stays human.** Federal responses go through SAM.gov / agency portals / email with
signatures and forms — the system gets you to draft-ready, you do the final submit.

## Scheduling (later)

A once-daily launchd job (same pattern as other ~/ projects) can run `sam_pull.py` and email the digest
via the Gmail connection. Not wired yet — ship/tune discovery manually first.
