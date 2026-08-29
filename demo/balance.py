#!/usr/bin/env python3
"""Show Canton Coin balances. Read-only.

    set -a && source .env && set +a
    python3 demo/balance.py            # balances only, about a second
    python3 demo/balance.py --offers   # also list transfer offers awaiting accept

Checking for offers uses an interface-filter query over the active contract set,
which takes about ten seconds per party on DevNet, so it is off by default.
demo/parties.py is slower still because it re-grants act-as rights every run.
"""
import os, sys

sys.path.insert(0, os.path.dirname(__file__))
import live as L

PARTIES = ["team1agent", "team1owner", "team1aws"]

if __name__ == "__main__":
    show_offers = "--offers" in sys.argv
    print(f"ledger offset {L.c8lab.ledger_end()}\n")
    total = 0.0
    for hint in PARTIES:
        p = L.party(hint)
        bal = L.balance_or_none(p)
        if bal is not None:
            total += bal
        note = ""
        if show_offers:
            pend = L.pending_instructions(p)
            if pend:
                note = "  " + ", ".join(
                    f"offer {i['amount']} from {(i['sender'] or '').split('::')[0]}"
                    for i in pend)
        shown = "   unknown" if bal is None else f"{bal:>10.4f}"
        print(f"  {hint:12} {shown} Amulet{note}" + ("  <- query failed, not necessarily zero" if bal is None else ""))
    print(f"  {'TOTAL':12} {total:>10.4f} Amulet")
    if not show_offers:
        print("\n(add --offers to check for transfers awaiting acceptance)")
    if total == 0:
        print("\nNo coin yet. Ask the Cantor8 team to fund:")
        print(f"  {L.party('team1agent')}")
