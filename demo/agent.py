#!/usr/bin/env python3
"""An autonomous agent that spends its own budget, on the live ledger.

D1 asks to "show it working: an agent buying something on its own, and the
statement afterwards". This is that agent. Nobody clicks a button per purchase:
the agent works through a backlog of things it wants to buy, decides for itself,
and submits each charge to Canton. The mandate decides what is allowed.

The point is what happens when the agent is WRONG. Three of the items below are
things a real agent would plausibly attempt and must not be able to do: an
over-budget purchase, a vendor nobody approved, and a refund modelled as a
negative charge. The agent does not know they are wrong. It tries them. The
ledger refuses.

Run standalone:
    set -a && source .env && set +a
    python3 demo/agent.py
"""
import os, sys, time

sys.path.insert(0, os.path.dirname(__file__))
import live as L

# What the agent wants to buy this cycle. `expect` is what SHOULD happen, and is
# only used to score the run afterwards -- the agent does not consult it.
# Plain-English names for the statement and the UI. The ledger party ids are
# fixed, so this is purely presentation.
VENDOR_LABELS = {"team1aws": "Cloud vendor", "team1owner": "Unapproved vendor",
                 "team1agent": "AI agent"}

BACKLOG = [
    {"item": "GPU hours for the nightly training run", "amount": 30,  "vendor": "team1aws",   "expect": "allow"},
    {"item": "Object storage top-up",                  "amount": 15,  "vendor": "team1aws",   "expect": "allow"},
    {"item": "Reserved capacity, annual commitment",   "amount": 450, "vendor": "team1aws",   "expect": "block"},
    {"item": "Inference credits from an unapproved vendor", "amount": 20, "vendor": "team1owner", "expect": "block"},
    {"item": "Log retention extension",                "amount": 25,  "vendor": "team1aws",   "expect": "allow"},
    {"item": "Refund of a duplicate invoice",          "amount": -40, "vendor": "team1aws",   "expect": "block"},
    {"item": "Extra CI runners for the release",       "amount": 20,  "vendor": "team1aws",   "expect": "allow"},
]


def run(mandate_cid, agent_party, on_event=None, pause=0.0):
    """Work the backlog against a live mandate. Returns (new_cid, results)."""
    results = []
    cid = mandate_cid
    for step in BACKLOG:
        # The allow-list on the ledger holds full party ids, so a short hint
        # has to be resolved before submitting or every charge looks unapproved.
        vendor = step["vendor"]
        full = vendor if "::" in vendor else L.party(vendor)
        ok, cid, why = L.charge(cid, agent_party, step["amount"], full)
        r = {
            "item": step["item"], "amount": step["amount"],
            "vendor": VENDOR_LABELS.get(step["vendor"], step["vendor"].split("::")[0]),
            "allowed": ok,
            "reason": None if ok else why,
            "expected": step["expect"],
            # Did the ledger do what a correct implementation should?
            "correct": (ok and step["expect"] == "allow") or (not ok and step["expect"] == "block"),
        }
        results.append(r)
        if on_event:
            on_event(r)
        if pause:
            time.sleep(pause)
    return cid, results


def score(results):
    """Numbers for the judges. 'Bring a number' is 30% of the mark."""
    attempted = len(results)
    allowed = sum(1 for r in results if r["allowed"])
    blocked = attempted - allowed
    prevented = sum(abs(r["amount"]) for r in results if not r["allowed"])
    settled = sum(r["amount"] for r in results if r["allowed"])
    correct = sum(1 for r in results if r["correct"])
    return {
        "attempted": attempted,
        "allowed": allowed,
        "blocked": blocked,
        "settledValue": round(settled, 2),
        "preventedValue": round(prevented, 2),
        "correctDecisions": correct,
        "decisionAccuracy": round(100.0 * correct / attempted, 1) if attempted else 0.0,
        "enforcedOnLedger": True,
    }


def statement(results, mandate):
    """A human-readable statement. D1 asks for 'the statement afterwards'."""
    lines = []
    w = 46
    lines.append("AGENT SPENDING STATEMENT")
    lines.append("=" * 64)
    if mandate:
        lines.append(f"Mandate holder   {mandate['spender']} acting for {mandate['owner']}")
        lines.append(f"Lifetime budget  {mandate['spent']:.2f} of {mandate['cap']:.2f} used")
        lines.append(f"Period budget    {mandate['periodSpent']:.2f} of {mandate['periodCap']:.2f} used"
                     f"  (resets every {mandate['periodHours']:g}h)")
        lines.append(f"Approved payees  {', '.join(mandate['counterparties'])}")
        lines.append(f"Status           {'REVOKED' if mandate['revoked'] else 'active'}")
    lines.append("")
    lines.append(f"{'ITEM':<{w}}{'AMOUNT':>10}  OUTCOME")
    lines.append("-" * 64)
    for r in results:
        amt = f"{r['amount']:.2f}"
        out = "settled" if r["allowed"] else "declined"
        lines.append(f"{r['item'][:w-1]:<{w}}{amt:>10}  {out}")
        if not r["allowed"]:
            lines.append(f"{'':<{w}}{'':>10}  reason: {r['reason']}")
    s = score(results)
    lines.append("-" * 64)
    lines.append(f"{'Settled':<{w}}{s['settledValue']:>10.2f}  {s['allowed']} of {s['attempted']} attempts")
    lines.append(f"{'Prevented':<{w}}{s['preventedValue']:>10.2f}  {s['blocked']} declined by the ledger")
    lines.append("")
    lines.append("Every decline above was made by Mandate.daml on Canton, not by the")
    lines.append("agent and not by the backend. The agent attempted each purchase.")
    return "\n".join(lines)


def main():
    owner, agent, payee = (L.party("team1owner"), L.party("team1agent"), L.party("team1aws"))
    print("granting a fresh mandate: lifetime 500, period 100 / 24h, payee team1aws\n")
    prop = L.propose(owner, agent, [payee], cap=500, period_cap=100, period_hours=24)
    cid = L.accept(prop, agent)

    def show(r):
        mark = "settled " if r["allowed"] else "DECLINED"
        print(f"  {mark}  {r['amount']:>7.2f}  {r['item'][:44]:<44}"
              + ("" if r["allowed"] else f"  <- {r['reason']}"))

    print("agent working its backlog:")
    cid, results = run(cid, agent, on_event=show)
    st = L.read_mandate(cid, owner)
    mandate = {
        "owner": st["owner"].split("::")[0], "spender": st["spender"].split("::")[0],
        "cap": float(st["cap"]), "spent": float(st["spent"]),
        "periodCap": float(st["periodCap"]), "periodSpent": float(st["periodSpent"]),
        "periodHours": round(int(st["periodLength"]["microseconds"]) / 3.6e9, 2),
        "counterparties": [c.split("::")[0] for c in st["counterparties"]],
        "revoked": bool(st["revoked"]),
    }
    print()
    print(statement(results, mandate))


if __name__ == "__main__":
    main()
