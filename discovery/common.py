"""Shared plumbing for discovery source adapters (FDA, Socrata, etc.).

Each adapter pulls from a source, scores for pharmacy/specialty relevance,
dedupes against its own seen-store, and writes a dated markdown digest.
Stdlib only.
"""
import datetime as dt
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
OPP_DIR = ROOT / "data" / "opportunities"
DIGEST_DIR = OPP_DIR / "digests"
ENV_PATH = CONFIG_DIR / ".env"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (RFP-Funnel/1.0)"


class RateLimited(Exception):
    pass


def load_env(path: Path = ENV_PATH) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def http_get_json(base_url: str, params: dict, timeout: int = 45, headers: dict = None):
    """GET JSON. Returns parsed body. openFDA returns HTTP 404 when zero matches —
    that's treated as an empty result, not an error. 429 -> RateLimited."""
    url = base_url + ("?" + urllib.parse.urlencode(params) if params else "")
    h = {"Accept": "application/json", "User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited()
        if e.code == 404:
            return {"results": [], "_http_404": True}
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {e.code} on {base_url}: {body}")


def days_until(deadline: str):
    if not deadline:
        return None
    try:
        s = deadline.replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s)
        now = dt.datetime.now(d.tzinfo) if d.tzinfo else dt.datetime.now()
        return (d - now).days
    except Exception:
        return None


def load_seen(path: Path) -> set:
    seen = set()
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                seen.add(json.loads(line)["id"])
            except Exception:
                pass
    return seen


def append_seen(path: Path, items: list):
    """items: list of (id, label) tuples."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    with p.open("a") as f:
        for _id, label in items:
            f.write(json.dumps({"id": _id, "firstSeen": stamp, "label": label}) + "\n")


def write_current(source: str, records: list) -> Path:
    """Persist the current relevant set as JSON for the dashboard to consume."""
    OPP_DIR.mkdir(parents=True, exist_ok=True)
    p = OPP_DIR / f"current_{source}.json"
    p.write_text(json.dumps({"updated": dt.datetime.now().isoformat(timespec="seconds"),
                             "records": records}, indent=2))
    return p


def write_digest(filename: str, title: str, intro_lines: list, blocks: list) -> Path:
    """blocks: list of lists-of-strings (each inner list is one item's markdown lines)."""
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    out = DIGEST_DIR / filename
    lines = [f"# {title}", ""] + intro_lines + [""]
    if not blocks:
        lines.append("_Nothing new above threshold this run._")
    for b in blocks:
        lines.extend(b)
        lines.append("")
    out.write_text("\n".join(lines))
    return out
