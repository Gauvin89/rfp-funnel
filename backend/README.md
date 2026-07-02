# Shared activity backend (Cloudflare Worker + KV)

Gives every teammate the **same** per-card notes / touch log on the RFP board.
The board still works without it (falls back to per-browser `localStorage`); this
just syncs the notes across people and devices.

## What it is
- `worker.js` — a tiny REST API. Bearer-token auth, CORS locked to the board's
  GitHub Pages origin, notes stored in a KV namespace (`note:<cardId>`).
- `wrangler.toml` — Worker config (KV id filled in at deploy).
- `deploy.sh` — one command that creates the KV namespace, sets the API-token
  secret, deploys, and writes `WORKER_URL` + `BOARD_API_TOKEN` into `config/.env`.

No data and no secrets live in this folder. The API token is a Worker *secret*;
the frontend token lives only in the gitignored `config/.env` and, from there,
inside the **encrypted** `index.html`.

## Deploy (one time)
1. Free account at <https://dash.cloudflare.com>.
2. **My Profile → API Tokens → Create Token → "Edit Cloudflare Workers"** template → Create.
3. Put the token in `config/.env` as `CLOUDFLARE_API_TOKEN="..."` (gitignored), or `export` it.
4. Run:
   ```bash
   ./backend/deploy.sh
   python3 response/build_dashboard.py && python3 response/encrypt_board.py
   git add index.html && git commit -m "enable shared notes" && git push
   ```

## API
| Method | Path              | Auth   | Body                 | Returns                         |
|--------|-------------------|--------|----------------------|---------------------------------|
| GET    | `/health`         | none   | —                    | `{ok:true}`                     |
| GET    | `/activity`       | Bearer | —                    | `{ "<cardId>": {touch,log}, …}` |
| POST   | `/activity/:id`   | Bearer | `{ts,text,by}`       | updated `{touch,log}`           |

## Notes
- **Security boundary:** the API token is embedded in the *encrypted* board, so
  only someone who already has the board passphrase can obtain it. Rotate it any
  time: `echo "<new>" | npx wrangler secret put API_TOKEN`, then update
  `BOARD_API_TOKEN` in `config/.env`, rebuild, re-encrypt, push.
- **Consistency:** KV is eventually consistent (a teammate's note can take up to
  ~60s to appear elsewhere). The board also polls every 45s. If you need instant,
  swap KV for D1 (SQLite) — same Worker shape.
- Column positions and theme remain local per browser; only the notes/touch log is shared.
