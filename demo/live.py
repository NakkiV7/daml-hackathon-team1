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


# ---------------------------------------------------------------------------
# DevNet reliability.
#
# api.validator.dev.digik.cantor8.tech resolves to several load-balancer IPs and
# at least one of them blackholes traffic: a plain `curl` stalls for 30-75s on
# TCP connect roughly one attempt in five, then a retry to a different IP
# succeeds immediately. c8lab uses a single 30s timeout with no retry, so a demo
# can appear to hang for a minute at random.
#
# Wrapping c8lab's one HTTP chokepoint with a short timeout and a couple of
# retries turns a 75s freeze into a sub-second hiccup. Nothing here changes what
# is sent; it only gives up on a stalled socket sooner and tries again.
# ---------------------------------------------------------------------------
# A healthy connect takes ~0.02s, so 3s is already very generous; the only
# thing a longer wait buys is a longer freeze on a blackholed IP.
CONNECT_TIMEOUT = float(os.environ.get("C8_TIMEOUT", "3"))
RETRIES = int(os.environ.get("C8_RETRIES", "5"))

_raw_request = c8lab._request


def _retrying_request(url, body=None, headers=None, method=None, timeout=None):
    writing = "/v2/commands/" in url
    # Submissions legitimately take longer than a read, so give them the full
    # timeout rather than the short one tuned for spotting a dead IP. Some reads
    # are heavy too: /v2/parties returns 10k records a page, which cannot finish
    # in the short window even on a healthy connection.
    heavy = "/v2/parties" in url and "?" not in url and url.rstrip("/").endswith("parties")
    per_try = 30.0 if (writing or heavy) else CONNECT_TIMEOUT
    attempts = RETRIES
    last = None
    for attempt in range(attempts):
        try:
            return _raw_request(url, body, headers, method, timeout=per_try)
        except c8lab.LabError as e:
            msg = str(e)
            last = e
            if writing:
                # Retrying a submission is safe *because the body is byte-identical*,
                # commandId included: if the first attempt did commit, Canton answers
                # DUPLICATE_COMMAND rather than charging twice, and callers treat that
                # as "our contract id is stale" and re-read from the ledger.
                # Only retry gateway-level refusals and stalls, where the command
                # most likely never reached the ledger at all. A 4xx is a real
                # verdict -- an assertion failure, say -- and must surface as-is.
                retryable = ("HTTP 503" in msg or "HTTP 502" in msg
                             or "HTTP 504" in msg or "timed out" in msg
                             or "cannot reach" in msg or "network error" in msg)
            else:
                retryable = ("timed out" in msg or "timeout" in msg.lower()
                             or "cannot reach" in msg or "network error" in msg
                             or "HTTP 503" in msg or "HTTP 502" in msg)
            if not retryable or attempt == attempts - 1:
                raise
    raise last


c8lab._request = _retrying_request


# token() does not go through _request: it calls urllib directly with a
# hardcoded 30s timeout and no retry. The Keycloak host has the same flaky-IP
# problem, and since the token is fetched once per process a single stall there
# delays everything after it. Re-fetch it ourselves with a short timeout, and
# fall back to c8lab for the LocalNet (self-signed HS256) path.
_raw_token = c8lab.token


def _retrying_token(sub=None):
    if not c8lab.IDP:
        return _raw_token() if sub is None else _raw_token(sub)
    if "t" in c8lab._tok:
        return c8lab._tok["t"]
    if not c8lab.CSEC:
        raise c8lab.LabError("C8_IDP is set but C8_CLIENT_SECRET is not.")
    import json as _json, urllib.parse as _up, urllib.request as _ur
    data = _up.urlencode({"grant_type": "client_credentials",
                          "client_id": c8lab.CID,
                          "client_secret": c8lab.CSEC}).encode()
    url = f"{c8lab.IDP}/realms/master/protocol/openid-connect/token"
    last = None
    for attempt in range(RETRIES):
        try:
            raw = _ur.urlopen(_ur.Request(url, data=data),
                              timeout=CONNECT_TIMEOUT).read()
            c8lab._tok["t"] = _json.loads(raw)["access_token"]
            return c8lab._tok["t"]
        except Exception as e:
            last = e
    raise c8lab.LabError(f"could not get a token from {c8lab.IDP} "
                         f"after {RETRIES} attempts: {last}")


c8lab.token = _retrying_token


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
        # Testing leaves plenty of spent-out and revoked mandates lying around.
        # Rank on usefulness: not revoked, then still has headroom in both caps,
        # then most spent (so a restart resumes the one actually in use rather
        # than jumping to some pristine leftover).
        spent = float(a.get("spent") or 0)
        cap = float(a.get("cap") or 0)
        pspent = float(a.get("periodSpent") or 0)
        pcap = float(a.get("periodCap") or 0)
        usable = (not a.get("revoked")) and spent < cap and pspent < pcap
        key = (0 if a.get("revoked") else 1, 1 if usable else 0, spent)
        if best_key is None or key > best_key:
            best_key, best = key, (ev.get("contractId"), a)
    return best


# ---------------------------------------------------------------------------
# Settlement: actually moving Canton Coin.
#
# Authorisation and settlement are two separate things and it matters which is
# which. Charge is the AUTHORISATION step -- the mandate decides whether the
# agent is allowed to spend, and that decision is final and on-ledger. Moving
# the Amulet afterwards is SETTLEMENT.
#
# We keep them separate rather than making Charge transfer funds itself, because
# a token-standard transfer needs disclosed contracts fetched from the registry
# for that one transaction, which a Daml choice body cannot go and get. So the
# agent authorises on-ledger first, and only then settles. An unauthorised
# charge never reaches this code at all.
# ---------------------------------------------------------------------------
def balance(p, instrument="Amulet"):
    """Spendable holdings for a party. Locked holdings do not count.

    Raises on failure rather than returning zero. DevNet intermittently takes
    longer than c8lab's 30s timeout, and a timeout reported as a 0.00 balance
    looks exactly like the money having gone -- which is the worst possible thing
    to show during a demo. Callers must handle the error and say "unknown".
    """
    hs = c8lab.holdings(p)
    return sum(float(h["amount"] or 0) for h in hs
               if not h["locked"] and h["instrument"] == instrument)


def balance_or_none(p, instrument="Amulet"):
    """Balance, or None if the ledger could not be reached. Never lies with 0."""
    try:
        return balance(p, instrument)
    except c8lab.LabError:
        return None


def settlement_ready(sender, receiver):
    """Can we actually move money yet? Reports every precondition separately."""
    out = {"registry": False, "instrument": None, "senderBalance": 0.0,
           "receiverBalance": 0.0, "ready": False, "blockedBy": []}
    try:
        info = c8lab.registry("/registry/metadata/v1/instruments")
        names = [i.get("id") for i in info.get("instruments", [])]
        out["registry"] = True
        out["instrument"] = "Amulet" if "Amulet" in names else (names[0] if names else None)
    except Exception as e:
        out["blockedBy"].append(f"registry unreachable: {str(e).splitlines()[0][:80]}")
    sb, rb = balance_or_none(sender), balance_or_none(receiver)
    out["senderBalance"], out["receiverBalance"] = sb, rb
    if sb is None:
        out["blockedBy"].append("could not read the sender's balance (ledger timed out)")
    elif sb <= 0:
        out["blockedBy"].append("sender holds no Canton Coin — ask the Cantor8 team to fund it")
    out["ready"] = bool(out["registry"] and sb is not None and sb > 0)
    return out


def preapprove(receiver, provider=None):
    """Ask for a TransferPreapproval so incoming transfers settle directly.

    Without one, a transfer arrives as an offer the receiver must accept, and
    their balance does not move until they do.
    """
    return c8lab.create_preapproval_proposal(receiver, provider or receiver)


def pending_instructions(receiver):
    """Transfer offers waiting for `receiver` to accept."""
    body = {"filter": {"filtersByParty": {receiver: {"cumulative": [
                {"identifierFilter": {"InterfaceFilter": {"value": {
                    "interfaceId": c8lab.TRANSFER_INSTRUCTION,
                    "includeInterfaceView": True,
                    "includeCreatedEventBlob": False}}}}]}}},
            "verbose": False, "activeAtOffset": c8lab.ledger_end()}
    out = []
    for item in c8lab.call("/v2/state/active-contracts", body):
        ev = item.get("contractEntry", {}).get("JsActiveContract", {}).get("createdEvent", {})
        for iv in ev.get("interfaceViews", []):
            v = iv.get("viewValue") or {}
            t = v.get("transfer") or {}
            out.append({"contractId": ev.get("contractId"),
                        "sender": t.get("sender"), "receiver": t.get("receiver"),
                        "amount": t.get("amount")})
    return out


def accept_pending(receiver):
    """Accept every offer waiting for `receiver`. Returns what was accepted."""
    done = []
    for i in pending_instructions(receiver):
        try:
            c8lab.accept_transfer(i["contractId"], receiver)
            done.append({"amount": i["amount"], "from": (i["sender"] or "").split("::")[0]})
        except c8lab.LabError as e:
            done.append({"amount": i["amount"], "error": str(e).splitlines()[0][:120]})
    return done


def settle(sender, receiver, amount, instrument="Amulet", auto_accept=True):
    """Move Canton Coin. Returns (ok, detail).

    Called only after the ledger has already authorised the charge. If there is
    no coin yet this fails cleanly and says so, rather than pretending.

    A transfer comes back as `direct` when the receiver holds a live
    TransferPreapproval, and as `offer` otherwise -- and an offer does not move
    the receiver's balance until they accept it. Since the demo controls the
    payee party, we accept on their behalf so the money actually arrives.
    """
    try:
        r = c8lab.transfer(sender, receiver, str(amount), instrument=instrument)
    except c8lab.LabError as e:
        return False, {"error": str(e).splitlines()[0][:200]}

    detail = {"transferKind": r.get("transferKind"),
              "instructionCid": r.get("instructionCid")}
    if auto_accept and r.get("transferKind") == "offer" and r.get("instructionCid"):
        try:
            c8lab.accept_transfer(r["instructionCid"], receiver)
            detail["accepted"] = True
        except c8lab.LabError as e:
            detail["accepted"] = False
            detail["acceptError"] = str(e).splitlines()[0][:150]
    return True, detail
