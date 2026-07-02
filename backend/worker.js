/**
 * Perigon RFP Board — shared activity backend (Cloudflare Worker + KV).
 *
 * Stores the per-card notes / touch log so the whole team sees the same threads.
 * Contains NO data and NO secrets in source: the API token is a Worker secret
 * (`wrangler secret put API_TOKEN`) and the allowed origin is public config.
 *
 * Auth:   Authorization: Bearer <API_TOKEN>   (checked on every request)
 * CORS:   locked to ALLOWED_ORIGIN (comma-separated allowed origins)
 * Store:  KV namespace binding ACTIVITY, one key per card: note:<cardId>
 *         value = { touch: "YYYY-MM-DD", log: [ { ts, text, by } ] }
 *
 * Routes:
 *   OPTIONS *              -> CORS preflight
 *   GET  /activity         -> { "<cardId>": {touch, log}, ... }  (all cards)
 *   POST /activity/:cardId -> body { ts, text, by } ; appends one note
 *   GET  /health           -> { ok: true }   (no auth)
 */

function corsHeaders(env, origin) {
  const allowed = (env.ALLOWED_ORIGIN || "").split(",").map((s) => s.trim()).filter(Boolean);
  const allow = allowed.length ? (allowed.includes(origin) ? origin : allowed[0]) : "*";
  return {
    "Access-Control-Allow-Origin": allow,
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    "Access-Control-Allow-Headers": "Authorization,Content-Type",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
}

function json(obj, status, cors) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...cors, "Content-Type": "application/json" },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin") || "";
    const cors = corsHeaders(env, origin);

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });

    if (url.pathname === "/health") return json({ ok: true }, 200, cors);

    // ---- auth (constant-ish comparison) ----
    const token = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
    if (!env.API_TOKEN || token !== env.API_TOKEN) {
      return json({ error: "unauthorized" }, 401, cors);
    }

    // ---- GET all activity ----
    if (url.pathname === "/activity" && request.method === "GET") {
      const out = {};
      let cursor;
      do {
        const list = await env.ACTIVITY.list({ prefix: "note:", cursor });
        await Promise.all(
          list.keys.map(async (k) => {
            const v = await env.ACTIVITY.get(k.name);
            if (v) out[k.name.slice(5)] = JSON.parse(v);
          })
        );
        cursor = list.list_complete ? null : list.cursor;
      } while (cursor);
      return json(out, 200, cors);
    }

    // ---- POST a note to a card ----
    const m = url.pathname.match(/^\/activity\/(.+)$/);
    if (m && request.method === "POST") {
      const id = decodeURIComponent(m[1]);
      let body;
      try {
        body = await request.json();
      } catch (e) {
        return json({ error: "bad json" }, 400, cors);
      }
      const text = String((body && body.text) || "").trim().slice(0, 4000);
      if (!text) return json({ error: "empty note" }, 400, cors);
      const ts = String((body && body.ts) || "").slice(0, 16);
      const by = String((body && body.by) || "").slice(0, 40);

      const key = "note:" + id;
      const cur = await env.ACTIVITY.get(key);
      const entry = cur ? JSON.parse(cur) : { touch: "", log: [] };
      const dupe = entry.log.some((n) => n.ts === ts && n.text === text);
      if (!dupe) {
        entry.log.push({ ts, text, by });
        entry.touch = ts.slice(0, 10);
        await env.ACTIVITY.put(key, JSON.stringify(entry));
      }
      return json(entry, 200, cors);
    }

    return json({ error: "not found" }, 404, cors);
  },
};
