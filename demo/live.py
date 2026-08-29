#!/usr/bin/env python3
"""LIVE Mandate operations against the real Canton ledger.

Everything here is a genuine Ledger API submission: the cap, the period cap and
the allow-list are enforced by daml/Mandate.daml running on Canton, not by this
process. A rejected charge comes back as an AssertionFailed from the ledger.

Prerequisites (see demo/parties.py):
  * parties allocated and act-as granted to the token's user
  * proj/.daml/dist/proj-0.0.1.dar uploaded to the participant

Three DevNet gotchas encoded below:
  1. userId on a submission must be the token subject
     (validator-backend@clients), not c8lab's LocalNet default.
  2. RelTime is JSON-encoded as {"microseconds": "<string>"} — an integer is
     rejected with 'Expected ujson.Str'.
  3. Decimals go over the wire as strings.
"""
import os, re, sys, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proj"))
import c8lab

NAMESPACE = os.environ.get(
    "C8_NAMESPACE",
    "12204e94c0e449c0efcd270dd1e68259c36471cebef132e5c7dfc2750fe8c9eed77f")

# Package id of proj-0.0.1.dar. Read from the dar manifest by upload_dar().
PKG = os.environ.get(
    "C8_PKG", "373262fc5d33b09c1233ca01e6f2ebb63cb61d0b9cc9b2c55ec41f0b861730eb")

T_PROPOSAL = f"{PKG}:Mandate:MandateProposal"
T_MANDATE  = f"{PKG}:Mandate:Mandate"
T_CHARGE_AUDIT = f"{PKG}:Mandate:MandateChargeAudit"
T_REVOKE_AUDIT = f"{PKG}:Mandate:MandateRevokeAudit"

# Reads want the package NAME, not the package id: an id here is rejected with
# "Received an identifier with package ID ..., but expected a package name."
R_MANDATE      = "#proj:Mandate:Mandate"
R_CHARGE_AUDIT = "#proj:Mandate:MandateChargeAudit"
R_REVOKE_AUDIT = "#proj:Mandate:MandateRevokeAudit"


def party(hint):
    return f"{hint}::{NAMESPACE}"


def _created(res, want):
    """Contract id of the first created event whose templateId contains `want`."""
    for e in res.get("transaction", {}).get("events", []):
        v = (e.get("CreatedTreeEvent") or {}).get("value") or e.get("CreatedEvent") or {}
        if v and want in str(v.get("templateId", "")):
            return v.get("contractId")
    return None


def ledger_reason(err):
    """Pull the Daml assertion message out of a Canton error payload."""
    s = str(err)
    m = re.search(r"AssertionFailed \(error category \d+\): ([^\"\\]+)", s)
    if m:
        return m.group(1).strip()
    m = re.search(r'"cause":"([^"]{0,200})', s)
    return (m.group(1) if m else s.splitlines()[0])[:200]


def propose(owner, spender, counterparties, cap, period_cap, period_hours, days=30):
    """Owner offers a mandate. Returns the proposal contract id."""
    t0 = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    args = {
        "owner": owner, "spender": spender,
        "cap": str(float(cap)),
        "counterparties": counterparties,
        "expiresAt": (t0 + datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "periodCap": str(float(period_cap)),
        "periodLength": {"microseconds": str(int(period_hours * 3600 * 1_000_000))},
    }
    r = c8lab.submit([{"CreateCommand": {"templateId": T_PROPOSAL,
                                         "createArguments": args}}],
                     act_as=owner, want_transaction=True)
    return _created(r, "Mandate:MandateProposal")


def accept(proposal_cid, spender):
    """Spender accepts. Returns the live Mandate contract id."""
    r = c8lab.submit([{"ExerciseCommand": {
        "templateId": T_PROPOSAL, "contractId": proposal_cid,
        "choice": "Accept", "choiceArgument": {}}}],
        act_as=spender, want_transaction=True)
    return _created(r, "Mandate:Mandate")


def charge(mandate_cid, spender, amount, payee):
    """Exercise Charge. The LEDGER decides. Returns (ok, new_cid_or_None, reason)."""
    try:
        r = c8lab.submit([{"ExerciseCommand": {
            "templateId": T_MANDATE, "contractId": mandate_cid,
            "choice": "Charge",
            "choiceArgument": {"amount": str(float(amount)), "payee": payee}}}],
            act_as=spender, want_transaction=True)
        return True, _created(r, "Mandate:Mandate"), None
    except c8lab.LabError as e:
        # Charge is consuming; a rejected exercise leaves the original active.
        return False, mandate_cid, ledger_reason(e)


def revoke(mandate_cid, owner):
    """Owner revokes. Returns (ok, new_cid_or_None, reason)."""
    try:
        r = c8lab.submit([{"ExerciseCommand": {
            "templateId": T_MANDATE, "contractId": mandate_cid,
            "choice": "Revoke", "choiceArgument": {}}}],
            act_as=owner, want_transaction=True)
        return True, _created(r, "Mandate:Mandate"), None
    except c8lab.LabError as e:
        return False, mandate_cid, ledger_reason(e)


def read_mandate(mandate_cid, reader):
    """Current on-ledger state of a Mandate, via the active contract set."""
    body = {"filter": {"filtersByParty": {reader: {"cumulative": [
                {"identifierFilter": {"TemplateFilter": {"value": {
                    "templateId": R_MANDATE, "includeCreatedEventBlob": False}}}}]}}},
            "verbose": False, "activeAtOffset": c8lab.ledger_end()}
    for item in c8lab.call("/v2/state/active-contracts", body):
        ev = item.get("contractEntry", {}).get("JsActiveContract", {}).get("createdEvent", {})
        if ev.get("contractId") == mandate_cid:
            return ev.get("createArgument")
    return None


def audit(reader):
    """Every charge and revoke record this party can see, on-ledger."""
    out = []
    for tid, kind in [(R_CHARGE_AUDIT, "charge"), (R_REVOKE_AUDIT, "revoke")]:
        body = {"filter": {"filtersByParty": {reader: {"cumulative": [
                    {"identifierFilter": {"TemplateFilter": {"value": {
                        "templateId": tid, "includeCreatedEventBlob": False}}}}]}}},
                "verbose": False, "activeAtOffset": c8lab.ledger_end()}
        for item in c8lab.call("/v2/state/active-contracts", body):
            ev = item.get("contractEntry", {}).get("JsActiveContract", {}).get("createdEvent", {})
            a = ev.get("createArgument") or {}
            if a:
                a["_kind"] = kind
                out.append(a)
    return sorted(out, key=lambda x: x.get("at") or "")


def recover(owner, spender=None):
    """Find an active Mandate on the ledger for these parties.

    The backend holds the current contract id in memory, so a restart would
    otherwise 'lose' a mandate that is still perfectly alive on Canton. This
    reads it back out of the active contract set instead, which is the whole
    argument for keeping state on a ledger. Returns (cid, args) or (None, None).
    Prefers the un-revoked one with the most spent, i.e. the latest in the chain.
    """
    body = {"filter": {"filtersByParty": {owner: {"cumulative": [
                {"identifierFilter": {"TemplateFilter": {"value": {
                    "templateId": R_MANDATE, "includeCreatedEventBlob": False}}}}]}}},
            "verbose": False, "activeAtOffset": c8lab.ledger_end()}
    best = (None, None)
    best_key = None
    for item in c8lab.call("/v2/state/active-contracts", body):
        ev = item.get("contractEntry", {}).get("JsActiveContract", {}).get("createdEvent", {})
        a = ev.get("createArgument") or {}
        if not a or a.get("owner") != owner:
            continue
        if spender and a.get("spender") != spender:
            continue
        # An active, un-revoked mandate beats a revoked one; then most spent wins.
        key = (0 if a.get("revoked") else 1, float(a.get("spent") or 0))
        if best_key is None or key > best_key:
            best_key, best = key, (ev.get("contractId"), a)
    return best
