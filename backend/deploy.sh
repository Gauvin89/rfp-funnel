#!/bin/bash
# Deploy the shared-activity Worker to Cloudflare, wire config/.env, done.
#
# Prereq (one-time, on your side):
#   1. Create a free account at https://dash.cloudflare.com
#   2. My Profile > API Tokens > Create Token > "Edit Cloudflare Workers" template
#   3. export CLOUDFLARE_API_TOKEN="<that token>"   (or put it in config/.env)
#
# Then:  ./backend/deploy.sh
set -euo pipefail
cd "$(dirname "$0")"
ENVF="../config/.env"

# allow the token to live in config/.env too
if [ -z "${CLOUDFLARE_API_TOKEN:-}" ] && [ -f "$ENVF" ]; then
  CLOUDFLARE_API_TOKEN="$(sed -n 's/^CLOUDFLARE_API_TOKEN=//p' "$ENVF" | tr -d '"'"'"'' | head -1)"
fi
: "${CLOUDFLARE_API_TOKEN:?Set CLOUDFLARE_API_TOKEN (see header of this script)}"
export CLOUDFLARE_API_TOKEN
WR="npx --yes wrangler@3"

# 1. KV namespace (only if not already wired)
if grep -q 'REPLACE_WITH_KV_ID' wrangler.toml; then
  echo "==> creating KV namespace ACTIVITY"
  OUT="$($WR kv namespace create ACTIVITY 2>&1)" || { echo "$OUT"; exit 1; }
  KVID="$(printf '%s' "$OUT" | grep -oE '[a-f0-9]{32}' | head -1)"
  [ -n "$KVID" ] || { echo "could not parse KV id from:"; echo "$OUT"; exit 1; }
  sed -i.bak "s/REPLACE_WITH_KV_ID/$KVID/" wrangler.toml && rm -f wrangler.toml.bak
  echo "   KV id: $KVID"
fi

# 2. API token secret (generate a strong one unless BOARD_API_TOKEN is preset)
BOARD_API_TOKEN="${BOARD_API_TOKEN:-$(openssl rand -hex 24)}"
echo "==> setting Worker secret API_TOKEN"
printf '%s' "$BOARD_API_TOKEN" | $WR secret put API_TOKEN >/dev/null

# 3. Deploy
echo "==> deploying Worker"
DEPLOY="$($WR deploy 2>&1)"; echo "$DEPLOY"
WORKER_URL="$(printf '%s' "$DEPLOY" | grep -oE 'https://[a-z0-9._-]+\.workers\.dev' | head -1)"
[ -n "$WORKER_URL" ] || { echo "could not parse Worker URL"; exit 1; }

# 4. Persist frontend config (gitignored)
touch "$ENVF"
upsert(){ grep -q "^$1=" "$ENVF" && sed -i.bak "s#^$1=.*#$1=\"$2\"#" "$ENVF" || printf '%s="%s"\n' "$1" "$2" >> "$ENVF"; rm -f "$ENVF.bak"; }
upsert WORKER_URL "$WORKER_URL"
upsert BOARD_API_TOKEN "$BOARD_API_TOKEN"

echo
echo "✓ Worker live:     $WORKER_URL"
echo "✓ config/.env set: WORKER_URL, BOARD_API_TOKEN"
echo "  Next: python3 response/build_dashboard.py && python3 response/encrypt_board.py"
echo "        then commit & push index.html — the board is now team-shared."
