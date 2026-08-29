#!/usr/bin/env python3
"""DevNet party helpers. Works around three things c8lab.py does not handle
on the shared Cantor8 DevNet.

1. c8lab defaults C8_USER to `ledger-api-user`, which does NOT exist on DevNet.
   The Keycloak client-credentials token's subject is `validator-backend@clients`.
   Without this, allocation succeeds but the act-as grant 404s.

2. c8lab.allocate_party() and find_party() call GET /v2/parties and scan the
   result. DevNet has ~125k parties and that endpoint is PAGINATED, so the
   reuse-scan only sees page one, then re-allocates and fails with
   "Party already exists". Paging through every page 503s the node.
   Fix: look a party up directly with GET /v2/parties/{partyId}.

3. Submitting as a party needs act-as rights for the token's user, or every
   command returns 403 with an otherwise-valid token.

Usage:
    set -a && source .env && set +a
    python3 demo/parties.py
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proj"))
import c8lab

# DevNet token subject. c8lab's default `ledger-api-user` is LocalNet-only.
DEVNET_USER = os.environ.get("C8_USER", "validator-backend@clients")

# This participant's namespace: the part after "::" in every party it hosts.
NAMESPACE = os.environ.get(
    "C8_NAMESPACE",
    "12204e94c0e449c0efcd270dd1e68259c36471cebef132e5c7dfc2750fe8c9eed77f")

# The demo needs a human owner, the agent that spends, and a payee.
WANTED = ["team1owner", "team1agent", "team1aws"]


def party_id(hint):
    return f"{hint}::{NAMESPACE}"


def lookup(hint):
    """Full party id if this node hosts it, else None. Single call, no paging."""
    try:
        r = c8lab.call(f"/v2/parties/{party_id(hint)}")
    except c8lab.LabError:
        return None
    for d in (r.get("partyDetails") or []):
        if d.get("isLocal"):
            return d["party"]
    return None


def ensure_party(hint):
    """Reuse if this node already hosts it, otherwise allocate."""
    got = lookup(hint)
    if got:
        return got, False
    try:
        r = c8lab.call("/v2/parties", {"partyIdHint": hint})
        return r["partyDetails"]["party"], True
    except c8lab.LabError as e:
        if "already exists" in str(e):
            return lookup(hint), False
        raise


def grant(party, user=DEVNET_USER):
    """act-as rights for `user` on `party`. Without this, submits 403."""
    return c8lab.grant_act_as(user, party)


def resolve_all(grant_rights=True):
    """Everything the demo needs: {hint: party_id}. Idempotent."""
    out = {}
    for hint in WANTED:
        party, _ = ensure_party(hint)
        if party and grant_rights:
            try:
                grant(party)
            except c8lab.LabError:
                pass  # already granted, or not permitted; surfaced by callers
        out[hint] = party
    return out


def main():
    print(f"user     {DEVNET_USER}")
    print(f"ledger   {c8lab.BASE}")
    print(f"offset   {c8lab.ledger_end()}\n")

    resolved = {}
    for hint in WANTED:
        party, created = ensure_party(hint)
        resolved[hint] = party
        print(f"{'allocated' if created else 'reused   '} {hint:11} {party}")

    print()
    for hint, party in resolved.items():
        bits = []
        try:
            grant(party)
            bits.append("act-as ok")
        except c8lab.LabError as e:
            bits.append(f"grant failed: {str(e).splitlines()[0][:70]}")
        try:
            h = c8lab.holdings(party)
            bits.append(f"{len(h)} holding(s), total {sum(float(x['amount'] or 0) for x in h)}")
        except c8lab.LabError as e:
            bits.append(f"holdings: {str(e).splitlines()[0][:60]}")
        print(f"{hint:11} {' · '.join(bits)}")

    print("\nGive these party ids to the Cantor8 team to receive Canton Coin:")
    for p in resolved.values():
        print(f"  {p}")


if __name__ == "__main__":
    main()
