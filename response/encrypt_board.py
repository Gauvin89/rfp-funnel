#!/usr/bin/env python3
"""
Encrypt dashboard.html into a public, password-gated page (index.html).

Only an AES-256-GCM ciphertext is written into index.html — the plaintext board
(with every contact/name/email/note) is NEVER committed. The page decrypts in the
browser via the Web Crypto API using a passphrase you share with your team
out-of-band. A wrong passphrase reveals nothing: there is no plaintext to read.

Crypto: PBKDF2-HMAC-SHA256 (310,000 iters) -> AES-256-GCM. The browser loader uses
identical parameters. Safe to commit index.html to a public repo.

Passphrase source (in order):
  1. $BOARD_PASSPHRASE
  2. BOARD_PASSPHRASE=... in config/.env  (gitignored)
  3. auto-generated strong passphrase -> saved to config/.env, printed once.

Usage: python3 response/encrypt_board.py [--in dashboard.html] [--out index.html]
"""
import argparse
import base64
import os
import re
import secrets
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = Path(__file__).resolve().parent.parent
ENVF = ROOT / "config" / ".env"
ITER = 310_000
WORDS = ("amber azure basin birch cedar cobalt coral delta ember falcon fern flint "
         "harbor heron indigo ivory jasper juniper lagoon lunar maple meadow mesa nimbus "
         "onyx opal orbit pine quartz raven ridge river slate spruce summit tidal timber "
         "topaz umber vale verde willow zephyr").split()


def read_env_pass():
    p = os.environ.get("BOARD_PASSPHRASE")
    if p:
        return p.strip()
    if ENVF.exists():
        for line in ENVF.read_text().splitlines():
            if line.strip().startswith("BOARD_PASSPHRASE="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def gen_pass():
    words = "-".join(secrets.choice(WORDS) for _ in range(3))
    return f"{words}-{secrets.token_hex(5)}"  # 3 words + 10 hex chars (~57 bits + KDF)


def save_pass(p):
    line = f'BOARD_PASSPHRASE="{p}"\n'
    txt = ENVF.read_text() if ENVF.exists() else ""
    if "BOARD_PASSPHRASE=" in txt:
        txt = re.sub(r"BOARD_PASSPHRASE=.*\n?", line, txt)
    else:
        txt = (txt + ("\n" if txt and not txt.endswith("\n") else "")) + line
    ENVF.parent.mkdir(parents=True, exist_ok=True)
    ENVF.write_text(txt)


def encrypt(plaintext, passphrase):
    salt = secrets.token_bytes(16)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(passphrase.encode())
    iv = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(iv, plaintext.encode(), None)  # ct includes the 16-byte GCM tag
    b = lambda x: base64.b64encode(x).decode()
    return b(salt), b(iv), b(ct)


LOADER = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Perigon RFP Board — Secure</title>
<style>
*{box-sizing:border-box}
body{background:#0e1320;color:#e7ecf5;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center}
.box{background:#161d2d;border:1px solid #26314a;border-radius:14px;padding:30px 28px;width:340px;max-width:92vw;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.5)}
h1{font-size:18px;margin:0 0 4px}.s{color:#8a97b0;font-size:13px;margin-bottom:18px}
input{width:100%;padding:10px 12px;border-radius:8px;border:1px solid #26314a;background:#0c1322;color:#e7ecf5;font-size:15px;margin-bottom:10px}
button{width:100%;padding:10px;border:none;border-radius:8px;background:#4d8bf0;color:#fff;font-weight:700;font-size:15px;cursor:pointer}
button:disabled{opacity:.6;cursor:default}
.err{color:#ef5b6b;font-size:13px;min-height:18px;margin-top:10px}
.lk{color:#5e6b85;font-size:11px;margin-top:16px}
</style></head><body>
<div class="box">
  <h1>&#128274; Perigon RFP Board</h1>
  <div class="s">Confidential. Enter the team passphrase.</div>
  <form id="f"><input id="p" type="password" placeholder="Passphrase" autocomplete="current-password" autofocus>
  <button id="b" type="submit">Unlock</button></form>
  <div class="err" id="e"></div>
  <div class="lk">Encrypted client-side &middot; AES-256-GCM &middot; nothing readable without the passphrase</div>
</div>
<script>
var SALT="__SALT__",IV="__IV__",CT="__CT__",ITER=__ITER__;
function b64(s){var d=atob(s),a=new Uint8Array(d.length);for(var i=0;i<d.length;i++)a[i]=d.charCodeAt(i);return a;}
async function unlock(pass){
  var enc=new TextEncoder();
  var km=await crypto.subtle.importKey("raw",enc.encode(pass),"PBKDF2",false,["deriveKey"]);
  var key=await crypto.subtle.deriveKey({name:"PBKDF2",salt:b64(SALT),iterations:ITER,hash:"SHA-256"},km,{name:"AES-GCM",length:256},false,["decrypt"]);
  var pt=await crypto.subtle.decrypt({name:"AES-GCM",iv:b64(IV)},key,b64(CT));
  return new TextDecoder().decode(pt);
}
document.getElementById("f").addEventListener("submit",async function(ev){
  ev.preventDefault();
  var err=document.getElementById("e"),btn=document.getElementById("b");
  err.textContent="Decrypting\\u2026";btn.disabled=true;
  try{
    var html=await unlock(document.getElementById("p").value);
    document.open();document.write(html);document.close();
  }catch(x){err.textContent="Wrong passphrase \\u2014 try again.";btn.disabled=false;}
});
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default=str(ROOT / "dashboard.html"))
    ap.add_argument("--out", dest="outfile", default=str(ROOT / "index.html"))
    args = ap.parse_args()

    src = Path(args.infile)
    if not src.exists():
        raise SystemExit(f"✗ input not found: {src} (run build_dashboard.py first)")
    plaintext = src.read_text()

    passphrase = read_env_pass()
    generated = False
    if not passphrase:
        passphrase = gen_pass()
        save_pass(passphrase)
        generated = True

    salt, iv, ct = encrypt(plaintext, passphrase)
    page = (LOADER.replace("__SALT__", salt).replace("__IV__", iv)
            .replace("__CT__", ct).replace("__ITER__", str(ITER)))

    # ---- leak guard: the encrypted page must contain NO plaintext PII ----
    # base64 alphabet has no '@', so an '@' or 'linkedin.com' means plaintext bled through.
    bad = [tok for tok in ("@", "linkedin.com", "Perigon RFP Board</title>") if tok in page]
    # (the loader's own <title> is "Perigon RFP Board — Secure", not "...</title>" verbatim)
    if bad:
        raise SystemExit(f"✗ ABORT: encrypted page appears to contain plaintext: {bad}")

    Path(args.outfile).write_text(page)
    kb = len(page) // 1024
    print(f"🔒 Encrypted board → {Path(args.outfile).name}  ({kb} KB, {ITER:,} PBKDF2 iters)")
    if generated:
        print("\n  ┌────────────────────────────────────────────────────────────┐")
        print("  │  TEAM PASSPHRASE (saved to config/.env, NOT committed):     │")
        print(f"  │     {passphrase:<54}│")
        print("  │  Share it out-of-band. Change it: edit BOARD_PASSPHRASE in  │")
        print("  │  config/.env and re-run encrypt_board.py.                   │")
        print("  └────────────────────────────────────────────────────────────┘")
    else:
        print("  (using existing BOARD_PASSPHRASE from env/.env)")


if __name__ == "__main__":
    main()
