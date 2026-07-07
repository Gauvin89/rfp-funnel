"""
Perigon RFP Board — shared activity API (AWS Lambda, Function URL, payload v2.0).

Mirrors the frontend contract exactly:
  GET  /health           -> {"ok": true}            (no auth)
  GET  /activity         -> {"<cardId>": {touch, log}, ...}   (Bearer)
  POST /activity/<cardId>-> body {ts, text, by} ; appends one note  (Bearer)

Storage: DynamoDB table (one item per card): {cardId, touch, log:[{ts,text,by}]}.
No secrets in source: API_TOKEN comes from the Lambda environment.
CORS is handled by the Function URL config (see template.yaml).
"""
import base64
import json
import os
import urllib.parse

import boto3

_TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
_API_TOKEN = os.environ.get("API_TOKEN", "")


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _read_body(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    return json.loads(raw or "{}")


def lambda_handler(event, context):
    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method", "GET")
    path = event.get("rawPath", "/")

    if path == "/health":
        return _resp(200, {"ok": True})

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    token = (headers.get("authorization") or "").split(" ")[-1].strip()
    if not _API_TOKEN or token != _API_TOKEN:
        return _resp(401, {"error": "unauthorized"})

    if path == "/activity" and method == "GET":
        out, kwargs = {}, {}
        while True:
            page = _TABLE.scan(**kwargs)
            for it in page.get("Items", []):
                out[it["cardId"]] = {"touch": it.get("touch", ""), "log": it.get("log", [])}
            lek = page.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return _resp(200, out)

    if path.startswith("/activity/") and method == "POST":
        card_id = urllib.parse.unquote(path[len("/activity/"):])
        try:
            body = _read_body(event)
        except Exception:
            return _resp(400, {"error": "bad json"})
        text = str(body.get("text", "")).strip()[:4000]
        if not text:
            return _resp(400, {"error": "empty note"})
        ts = str(body.get("ts", ""))[:16]
        by = str(body.get("by", ""))[:40]

        cur = _TABLE.get_item(Key={"cardId": card_id}).get("Item") or {"log": []}
        log = cur.get("log", [])
        if not any(n.get("ts") == ts and n.get("text") == text for n in log):
            log.append({"ts": ts, "text": text, "by": by})
        item = {"cardId": card_id, "touch": ts[:10], "log": log}
        _TABLE.put_item(Item=item)
        return _resp(200, {"touch": item["touch"], "log": item["log"]})

    return _resp(404, {"error": "not found"})
