#!/bin/bash
# One command to refresh all sources and rebuild the dashboard.
# Usage:  ./refresh.sh
# Also run by launchd daily at 1am (com.perigon.rfp-funnel) — so set a full PATH.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd "$(dirname "$0")" || exit 1
echo "===== refresh $(date) ====="

echo "==> SAM.gov (federal)"
python3 discovery/sam_pull.py || echo "   (sam skipped/failed — likely daily quota)"
echo "==> Analyze SAM solicitation PDFs (fit for 50-state pharmacy)"
python3 response/analyze_rfp.py || echo "   (analyzer failed)"
echo "==> openFDA leading indicators"
python3 discovery/fda_monitor.py || echo "   (fda failed)"
echo "==> Manufacturer pipeline (ClinicalTrials.gov)"
python3 discovery/pipeline_monitor.py || echo "   (pipeline failed)"
echo "==> DOSE — not-yet-started oral trials (trial support)"
python3 discovery/dose_monitor.py || echo "   (dose failed)"
echo "==> State/local (NYC, Michigan)"
python3 discovery/state_monitor.py || echo "   (state failed)"
echo "==> Asset inventory"
python3 response/scan_assets.py
echo "==> Per-drug pitches (outreach + branded PDF)"
python3 response/generate_pitch.py || echo "   (pitch gen failed)"
echo "==> Dashboard"
python3 response/build_dashboard.py
echo "==> System overview PDF"
python3 response/build_overview.py || echo "   (overview failed)"

echo
echo "Done. Open this file in your browser:"
echo "  $(pwd)/dashboard.html"
