#!/usr/bin/env python3
"""Minimal backend for the D1 agent-wallet demo. Stdlib only, no pip install.

Bridges the browser frontend to the Canton ledger. The browser must never hold
the Keycloak secret, so every ledger call goes through here.

It REUSES c8lab.py (per the toolkit README: "Import it, do not just use the CLI").
c8lab reads its config from env vars at import time, so load DevNet env first:

    cd /Users/akq/daml-hackathon-team1
    set -a && source .env && set +a
    python3 demo/server.py

Endpoints the frontend calls:
  GET  /api/state                      -> mock mandate + audit log (D1 rules)
  POST /api/mandate {cap,counterparties,hours}  -> create a mock mandate
  POST /api/charge  {amount,payee}     -> agent spends (enforces cap+allow-list)
  POST /api/revoke                     -> owner revokes
  GET  /api/ledger                     -> LIVE: token check + ledger end (real C8)
  GET  /api/parties                    -> LIVE: local parties on the node

The mandate flow is MOCK (mirrors Mandate.daml exactly) because moving real
value needs a funded DevNet party we do not have yet. The /api/ledger and
/api/parties calls are LIVE through c8lab, to prove the connection is real.
"""
import json, os, sys, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))

# Import c8lab from the proj/ dir next to this demo/ dir.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proj"))
try:
    import c8lab
    C8LAB_OK = True
except Exception as e:
    C8LAB_OK = False
    C8LAB_ERR = str(e)


# ---------------------------------------------------------------------------
# MOCK ledger: mirrors the rules in Mandate.daml so the UI behaves like the
# real contract. Swap for real ledger calls once a funded party exists.
# ---------------------------------------------------------------------------
STATE = {"mandate": None, "audit": [], "revoked": False}


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def create_mandate(cap, counterparties, hours):
    STATE["mandate"] = {
        "owner": "Owner", "spender": "Agent", "cap": float(cap), "spent": 0.0,
        "counterparties": counterparties,
        "expiresAt": (now() + datetime.timedelta(hours=float(hours))).isoformat(),
    }
    STATE["revoked"] = False
    STATE["audit"] = [{"type": "created", "cap": float(cap),
                       "counterparties": counterparties, "at": now().isoformat()}]
    return STATE["mandate"]


def charge(amount, payee):
    """Same assertions as the Daml Charge choice. Raises on any rule breach."""
    m = STATE["mandate"]
    if m is None:
        raise ValueError("no mandate exists")
    if STATE["revoked"]:
        raise ValueError("mandate revoked")
    amount = float(amount)
    if now().isoformat() >= m["expiresAt"]:
        raise ValueError("mandate expired")
    if amount <= 0:
        raise ValueError("amount must be positive")
    if m["spent"] + amount > m["cap"]:
        raise ValueError(f"charge would exceed the cap "
                         f"({m['spent']} + {amount} > {m['cap']})")
    if payee not in m["counterparties"]:
        raise ValueError(f"payee '{payee}' not in allow-list {m['counterparties']}")
    m["spent"] += amount
    entry = {"type": "charge", "amount": amount, "payee": payee,
             "newSpent": m["spent"], "at": now().isoformat(),
             "rule": "total-cap + allow-list check"}
    STATE["audit"].append(entry)
    return entry


def revoke():
    if STATE["mandate"] is None:
        raise ValueError("no mandate exists")
    STATE["revoked"] = True
    entry = {"type": "revoke", "at": now().isoformat(), "reason": "owner revocation"}
    STATE["audit"].append(entry)
    return entry


def get_state():
    return {"mandate": STATE["mandate"], "revoked": STATE["revoked"],
            "audit": STATE["audit"], "c8lab": C8LAB_OK,
            "base": (c8lab.BASE if C8LAB_OK else None)}


# ---------------------------------------------------------------------------
# LIVE calls through c8lab. These work TODAY (read-only, no funded party needed).
# ---------------------------------------------------------------------------
def live_ledger():
    if not C8LAB_OK:
        raise ValueError(f"c8lab import failed: {C8LAB_ERR}")
    c8lab.token()  # raises LabError if auth fails
    return {"base": c8lab.BASE,
            "mode": "DevNet / Keycloak" if c8lab.IDP else "LocalNet",
            "token": "ok", "ledgerEnd": c8lab.ledger_end()}


def live_parties():
    if not C8LAB_OK:
        raise ValueError(f"c8lab import failed: {C8LAB_ERR}")
    ps = c8lab.local_parties()
    return {"count": len(ps), "parties": ps[:25]}  # cap the list for the UI


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
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
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html)
                return
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
                    b.get("cap", 100), b.get("counterparties", ["Merchant"]),
                    b.get("hours", 24)))
            if self.path == "/api/charge":
                return self._send(200, charge(b.get("amount"), b.get("payee")))
            if self.path == "/api/revoke":
                return self._send(200, revoke())
            self._send(404, {"error": "not found"})
        except ValueError as e:
            self._send(400, {"error": str(e)})


if __name__ == "__main__":
    print(f"backend up on http://localhost:{PORT}")
    print(f"c8lab imported: {C8LAB_OK}" + ("" if C8LAB_OK else f" ({C8LAB_ERR})"))
    if C8LAB_OK:
        print(f"ledger base:    {c8lab.BASE}")
    print("open the URL in a browser")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
