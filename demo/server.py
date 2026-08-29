#!/usr/bin/env python3
"""Backend for the D1 agent-wallet demo. Stdlib only, no pip install.

Bridges the browser to the Canton ledger. The browser must never hold the
Keycloak secret, so every ledger call goes through here.

It REUSES c8lab.py (toolkit README: "Import it, do not just use the CLI").
c8lab reads config from env at import time, so load DevNet env first:

    cd /Users/akq/daml-hackathon-team1
    set -a && source .env && set +a
    python3 demo/server.py

The mandate rules below MIRROR daml/Mandate.daml exactly: lifetime cap,
per-period cap with lazy window roll-forward, counterparty allow-list,
expiry, and a revoked flag. Real enforcement lives in the Daml contract;
this mirror exists so the UI is demonstrable before a funded DevNet party.

Endpoints:
  GET  /api/state      mandate + audit log
  GET  /api/ledger     LIVE: token + ledger end via c8lab
  GET  /api/parties    LIVE: local parties on the node
  POST /api/mandate    {cap, periodCap, periodHours, counterparties, hours}
  POST /api/charge     {amount, payee}   -> enforces every rule
  POST /api/revoke     owner kills it, agent cannot block
  POST /api/reset      clear state for a clean demo
"""
import json, os, sys, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proj"))
try:
    import c8lab
    C8LAB_OK, C8LAB_ERR = True, None
except Exception as e:
    C8LAB_OK, C8LAB_ERR = False, str(e)


def now():
    return datetime.datetime.now(datetime.timezone.utc)


STATE = {"mandate": None, "audit": []}


def _audit(entry):
    entry["at"] = now().isoformat(timespec="seconds")
    entry["seq"] = len(STATE["audit"]) + 1
    STATE["audit"].append(entry)
    return entry


def create_mandate(cap, period_cap, period_hours, counterparties, hours):
    t = now()
    STATE["mandate"] = {
        "owner": "Owner", "spender": "Agent",
        "cap": float(cap), "spent": 0.0,
        "periodCap": float(period_cap), "periodSpent": 0.0,
        "periodStart": t.isoformat(timespec="seconds"),
        "periodHours": float(period_hours),
        "counterparties": counterparties,
        "expiresAt": (t + datetime.timedelta(hours=float(hours))).isoformat(timespec="seconds"),
        "revoked": False,
    }
    STATE["audit"] = []
    _audit({"type": "created", "detail": f"cap {cap}, period {period_cap}/{period_hours}h",
            "allow": counterparties, "status": "ok"})
    return STATE["mandate"]


def charge(amount, payee):
    """Mirrors the Daml Charge choice, assertion for assertion."""
    m = STATE["mandate"]
    if m is None:
        raise ValueError("no mandate exists")

    def reject(rule, msg):
        _audit({"type": "charge", "amount": amount, "payee": payee,
                "status": "rejected", "rule": rule, "detail": msg})
        raise ValueError(msg)

    try:
        amount = float(amount)
    except (TypeError, ValueError):
        reject("amount must be positive", f"amount '{amount}' is not a number")

    if m["revoked"]:
        reject("mandate revoked", "mandate revoked")
    if now() >= datetime.datetime.fromisoformat(m["expiresAt"]):
        reject("mandate expired", "mandate expired")
    if amount <= 0:
        reject("amount must be positive", "amount must be positive")

    # Lazy window roll-forward, anchored to periodStart (never to `now`, or the
    # spender could slide the window by timing charges). Same as the Daml.
    plen = datetime.timedelta(hours=m["periodHours"])
    start = datetime.datetime.fromisoformat(m["periodStart"])
    whole = int((now() - start) / plen)
    rolled = whole >= 1
    cur_start = start + whole * plen if rolled else start
    cur_period_spent = 0.0 if rolled else m["periodSpent"]

    if cur_period_spent + amount > m["periodCap"]:
        reject("period cap", f"charge would exceed the period cap "
                             f"({cur_period_spent} + {amount} > {m['periodCap']})")
    if m["spent"] + amount > m["cap"]:
        reject("total cap", f"charge would exceed the cap "
                            f"({m['spent']} + {amount} > {m['cap']})")
    if payee not in m["counterparties"]:
        reject("allow-list", f"payee '{payee}' not in allow-list {m['counterparties']}")

    m["spent"] += amount
    m["periodSpent"] = cur_period_spent + amount
    m["periodStart"] = cur_start.isoformat(timespec="seconds")
    return _audit({"type": "charge", "amount": amount, "payee": payee,
                   "status": "ok", "newSpent": m["spent"],
                   "rule": ("period rolled over; " if rolled else "")
                           + "period-cap + total-cap + allow-list"})


def revoke():
    m = STATE["mandate"]
    if m is None:
        raise ValueError("no mandate exists")
    m["revoked"] = True
    return _audit({"type": "revoke", "status": "ok", "detail": "owner revocation"})


def get_state():
    return {"mandate": STATE["mandate"], "audit": STATE["audit"],
            "c8lab": C8LAB_OK, "base": (c8lab.BASE if C8LAB_OK else None)}


def live_ledger():
    if not C8LAB_OK:
        raise ValueError(f"c8lab import failed: {C8LAB_ERR}")
    c8lab.token()
    return {"base": c8lab.BASE,
            "mode": "DevNet / Keycloak" if c8lab.IDP else "LocalNet",
            "token": "ok", "ledgerEnd": c8lab.ledger_end()}


def live_parties():
    if not C8LAB_OK:
        raise ValueError(f"c8lab import failed: {C8LAB_ERR}")
    ps = c8lab.local_parties()
    return {"count": len(ps), "parties": ps[:25]}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        for h, v in [("Content-Type", "application/json"),
                     ("Access-Control-Allow-Origin", "*"),
                     ("Access-Control-Allow-Headers", "Content-Type"),
                     ("Access-Control-Allow-Methods", "GET, POST, OPTIONS")]:
            self.send_header(h, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def log_message(self, *a):
        pass

    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        try:
            if self.path == "/api/state":
                return self._send(200, get_state())
            if self.path == "/api/ledger":
                return self._send(200, live_ledger())
            if self.path == "/api/parties":
                return self._send(200, live_parties())
            if self.path in ("/", "/index.html"):
                with open(os.path.join(os.path.dirname(__file__), "index.html"), "rb") as f:
                    html = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                return self.wfile.write(html)
            self._send(404, {"error": "not found"})
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        try:
            b = self._body()
            if self.path == "/api/mandate":
                return self._send(200, create_mandate(
                    b.get("cap", 500), b.get("periodCap", 100),
                    b.get("periodHours", 24),
                    b.get("counterparties", ["AWS", "OpenAI"]),
                    b.get("hours", 720)))
            if self.path == "/api/charge":
                return self._send(200, charge(b.get("amount"), b.get("payee")))
            if self.path == "/api/revoke":
                return self._send(200, revoke())
            if self.path == "/api/reset":
                STATE["mandate"], STATE["audit"] = None, []
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})
        except ValueError as e:
            self._send(400, {"error": str(e)})


if __name__ == "__main__":
    print(f"backend  http://localhost:{PORT}")
    print(f"c8lab    {'imported' if C8LAB_OK else 'FAILED: ' + str(C8LAB_ERR)}")
    if C8LAB_OK:
        print(f"ledger   {c8lab.BASE}")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
