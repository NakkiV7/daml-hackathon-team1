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
# "live" = every rule enforced by Mandate.daml on Canton DevNet.
# "mock" = rules mirrored in Python (no ledger needed) for offline demoing.
MODE = os.environ.get("C8_MODE", "live")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proj"))
try:
    import c8lab
    C8LAB_OK, C8LAB_ERR = True, None
except Exception as e:
    C8LAB_OK, C8LAB_ERR = False, str(e)

try:
    import live as L
    import agent as AG
    LIVE_OK, LIVE_ERR = True, None
except Exception as e:
    LIVE_OK, LIVE_ERR = False, str(e)

# Live-mode handles. OWNER grants, AGENT spends, PAYEE is the approved payee.
LIVE = {"mandateCid": None, "owner": None, "agent": None, "payee": None}


def live_parties_setup():
    if not LIVE["owner"]:
        LIVE["owner"] = L.party("team1owner")
        LIVE["agent"] = L.party("team1agent")
        LIVE["payee"] = L.party("team1aws")
    return LIVE


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
    if MODE == "live":
        return live_state()
    return {"mode": "mock", "mandate": STATE["mandate"], "audit": STATE["audit"],
            "c8lab": C8LAB_OK, "base": (c8lab.BASE if C8LAB_OK else None)}


# ---------------------------------------------------------------------------
# LIVE mode: every rule below is enforced by Mandate.daml on Canton, not here.
# ---------------------------------------------------------------------------
def _short(p):
    return p.split("::")[0] if p else None


def live_state():
    p = live_parties_setup()
    # A restart empties this process's memory, but the mandate is still on the
    # ledger. Read it back rather than pretending it is gone.
    if not LIVE["mandateCid"]:
        cid, _ = L.recover(p["owner"], p["agent"])
        if cid:
            LIVE["mandateCid"] = cid
            LIVE["recovered"] = True
    out = {"mode": "live", "c8lab": C8LAB_OK, "base": c8lab.BASE,
           "parties": {k: p[k] for k in ("owner", "agent", "payee")},
           "mandateCid": LIVE["mandateCid"], "recovered": LIVE.get("recovered", False),
           "mandate": None, "audit": []}
    if LIVE["mandateCid"]:
        st = L.read_mandate(LIVE["mandateCid"], p["owner"])
        if st:
            out["mandate"] = {
                "owner": _short(st["owner"]), "spender": _short(st["spender"]),
                "cap": float(st["cap"]), "spent": float(st["spent"]),
                "periodCap": float(st["periodCap"]),
                "periodSpent": float(st["periodSpent"]),
                "periodHours": round(int(st["periodLength"]["microseconds"]) / 3.6e9, 2),
                "counterparties": [_short(c) for c in st["counterparties"]],
                "expiresAt": st["expiresAt"], "revoked": bool(st["revoked"]),
            }
    for i, a in enumerate(L.audit(p["owner"]), 1):
        out["audit"].append({
            "seq": i, "type": a["_kind"], "status": "ok",
            "at": (a.get("at") or "").replace("Z", ""),
            "amount": float(a["amount"]) if a.get("amount") else None,
            "payee": _short(a.get("payee")),
            "rule": a.get("rule") or a.get("reason") or "",
            "onLedger": True})
    for r in LIVE.setdefault("rejects", []):
        out["audit"].append(r)
    out["audit"].sort(key=lambda e: e["at"])
    for i, e in enumerate(out["audit"], 1):
        e["seq"] = i
    return out


def live_create(cap, period_cap, period_hours, _cps, hours):
    p = live_parties_setup()
    prop = L.propose(p["owner"], p["agent"], [p["payee"]],
                     cap, period_cap, period_hours, days=max(1, int(hours / 24)))
    LIVE["mandateCid"] = L.accept(prop, p["agent"])
    LIVE["rejects"] = []
    return live_state()["mandate"]


def live_charge(amount, payee):
    p = live_parties_setup()
    if not LIVE["mandateCid"]:
        raise ValueError("no mandate on the ledger yet — grant one first")
    # Resolve a short name to a full party id so the allow-list check is real.
    full = {"team1aws": p["payee"], "aws": p["payee"],
            "team1owner": p["owner"], "team1agent": p["agent"]}.get(payee, payee)
    ok, cid, why = L.charge(LIVE["mandateCid"], p["agent"], amount, full)
    LIVE["mandateCid"] = cid
    if not ok:
        LIVE.setdefault("rejects", []).append({
            "seq": 0, "type": "charge", "status": "rejected",
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", ""),
            "amount": float(amount) if str(amount).replace("-", "").replace(".", "").isdigit() else amount,
            "payee": _short(full), "rule": why, "onLedger": True})
        raise ValueError(why)
    return {"status": "ok", "amount": float(amount), "payee": _short(full)}


def live_revoke():
    p = live_parties_setup()
    if not LIVE["mandateCid"]:
        raise ValueError("no mandate on the ledger yet")
    ok, cid, why = L.revoke(LIVE["mandateCid"], p["owner"])
    LIVE["mandateCid"] = cid
    if not ok:
        raise ValueError(why)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Autonomous agent, metrics and statement.
# "Bring a number" is 30% of the mark, so these are first-class product
# surfaces rather than something buried in a log.
# ---------------------------------------------------------------------------
def agent_run():
    """Let the agent work its own backlog against the live mandate."""
    p = live_parties_setup()
    if not LIVE["mandateCid"]:
        raise ValueError("no mandate on the ledger yet — grant one first")
    cid, results = AG.run(LIVE["mandateCid"], p["agent"])
    LIVE["mandateCid"] = cid
    LIVE["agentResults"] = results
    # Rejected agent charges belong in the audit view too.
    for r in results:
        if not r["allowed"]:
            LIVE.setdefault("rejects", []).append({
                "seq": 0, "type": "charge", "status": "rejected",
                "at": datetime.datetime.now(datetime.timezone.utc)
                        .isoformat(timespec="seconds").replace("+00:00", ""),
                "amount": r["amount"], "payee": r["vendor"],
                "rule": r["reason"], "onLedger": True})
    return {"results": results, "score": AG.score(results)}


def metrics():
    """Everything countable, from the on-ledger audit plus this session."""
    s = live_state() if MODE == "live" else get_state()
    audit = s.get("audit", [])
    m = s.get("mandate")
    accepted = [e for e in audit if e["type"] == "charge" and e["status"] == "ok"]
    rejected = [e for e in audit if e["status"] == "rejected"]
    prevented = sum(abs(e["amount"] or 0) for e in rejected)
    settled = sum(e["amount"] or 0 for e in accepted)
    by_rule = {}
    for e in rejected:
        key = (e["rule"] or "unknown").strip()
        by_rule[key] = by_rule.get(key, 0) + 1
    out = {
        "mode": s.get("mode"),
        "chargesAttempted": len(accepted) + len(rejected),
        "chargesAccepted": len(accepted),
        "chargesBlocked": len(rejected),
        "valueSettled": round(settled, 2),
        "valuePrevented": round(prevented, 2),
        "blockedByRule": by_rule,
        "enforcement": "Mandate.daml on Canton" if MODE == "live" else "Python mirror",
        "auditRecordsOnLedger": sum(1 for e in audit if e.get("onLedger")),
    }
    if m:
        out["headroomLifetime"] = round(m["cap"] - m["spent"], 2)
        out["headroomPeriod"] = round(m["periodCap"] - m["periodSpent"], 2)
        out["utilisationLifetime"] = round(100.0 * m["spent"] / m["cap"], 1) if m["cap"] else 0
    if LIVE.get("agentResults"):
        out["agent"] = AG.score(LIVE["agentResults"])
    return out


def statement_text():
    s = live_state() if MODE == "live" else get_state()
    results = LIVE.get("agentResults")
    if not results:
        # No autonomous run yet: build a statement from the audit trail instead.
        results = [{"item": f"charge to {e['payee']}", "amount": e["amount"] or 0,
                    "vendor": e["payee"], "allowed": e["status"] == "ok",
                    "reason": None if e["status"] == "ok" else e["rule"],
                    "expected": "", "correct": True}
                   for e in s.get("audit", []) if e["type"] == "charge"]
    if not results:
        return "No charges yet. Grant a mandate and let the agent run."
    return AG.statement(results, s.get("mandate"))


# ---------------------------------------------------------------------------
# Mode-aware dispatch. Every caller -- HTTP, the autonomous agent, and the MCP
# server an LLM drives -- must go through these, or a path could reach the
# Python mirror while the demo claims the ledger is enforcing the rules.
# ---------------------------------------------------------------------------
def do_create(cap, period_cap, period_hours, counterparties, hours):
    if MODE == "live":
        return live_create(cap, period_cap, period_hours, counterparties, hours)
    return create_mandate(cap, period_cap, period_hours, counterparties, hours)


def do_charge(amount, payee):
    if MODE == "live":
        return live_charge(amount, payee)
    return charge(amount, payee)


def do_revoke():
    return live_revoke() if MODE == "live" else revoke()


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
            if self.path == "/api/metrics":
                return self._send(200, metrics())
            if self.path == "/api/statement":
                return self._send(200, {"text": statement_text()})
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
                a = (b.get("cap", 500), b.get("periodCap", 100), b.get("periodHours", 24),
                     b.get("counterparties", ["team1aws"]), b.get("hours", 720))
                return self._send(200, do_create(*a))
            if self.path == "/api/charge":
                return self._send(200, do_charge(b.get("amount"), b.get("payee")))
            if self.path == "/api/revoke":
                return self._send(200, do_revoke())
            if self.path == "/api/agent/run":
                return self._send(200, agent_run())
            if self.path == "/api/reset":
                if MODE == "live":
                    LIVE["mandateCid"], LIVE["rejects"] = None, []
                    LIVE["agentResults"], LIVE["recovered"] = None, False
                else:
                    STATE["mandate"], STATE["audit"] = None, []
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(400, {"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    print(f"backend  http://localhost:{PORT}")
    print(f"mode     {MODE.upper()}" + ("  (rules enforced by Mandate.daml on Canton)"
                                        if MODE == "live" else "  (rules mirrored in Python)"))
    print(f"c8lab    {'imported' if C8LAB_OK else 'FAILED: ' + str(C8LAB_ERR)}")
    if MODE == "live" and not LIVE_OK:
        print(f"live.py  FAILED: {LIVE_ERR}")
    if C8LAB_OK:
        print(f"ledger   {c8lab.BASE}")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
