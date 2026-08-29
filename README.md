# A spend-limited wallet for an AI agent

Canton hackathon, track D1.

The answer everywhere right now is to give the agent a hot key. If the agent is
tricked, or simply wrong, there is nothing between it and the money. In May 2026
a Morse-code-encoded instruction talked an AI trading bot into draining about
$150k; the safety layer decoded the message and then obeyed it.

The model cannot be the last line of defence. So the limit does not live in the
model, and it does not live in our backend either. It lives in a Daml contract on
Canton. The agent can attempt anything it likes; the ledger decides.

## What is enforced, and where

`proj/daml/Mandate.daml`. Every rule is a line in a choice body, so it applies to
anyone reaching the ledger — including someone who bypasses this app entirely.

| Rule | The line that enforces it |
|---|---|
| Lifetime cap | `assertMsg "charge would exceed the cap" (spent + amount <= cap)` |
| Per-period cap | `assertMsg "charge would exceed the period cap" (curPeriodSpent + amount <= periodCap)` |
| Approved payees only | `assertMsg "payee not in allow-list" (payee `elem` counterparties)` |
| Expiry | `assertMsg "mandate expired" (now < expiresAt)` |
| No negative amounts | `assertMsg "amount must be positive" (amount > 0.0)` |
| Revoked mandates are dead | `assertMsg "mandate revoked" (not revoked)` |
| Raising the cap needs both parties | `controller owner, spender` on `Adjust` |

Revocation is instant and the agent cannot block it: `Revoke` is controlled by
the owner alone.

## Measured outcome

From one autonomous agent run against the live ledger:

```
7 charges attempted     the agent chose all seven itself
4 settled                90.00 within the mandate
3 refused               510.00 of overspend prevented
3 distinct rules        period cap, allow-list, positive amount
100% decision accuracy  every verdict was the correct one
```

## Running it

```bash
set -a && source .env && set +a
python3 demo/server.py           # http://localhost:8000
```

Then: **Grant mandate** → **Run the agent** → read the numbers → **Revoke** and
charge again.

Daml rules and their tests, no node or network needed:

```bash
export JAVA_HOME=/opt/homebrew/opt/openjdk@17
export PATH="$HOME/.daml/bin:$JAVA_HOME/bin:$PATH"
cd proj && daml build && daml test
```

The autonomous agent on its own, without the web UI:

```bash
python3 demo/agent.py            # runs the backlog, prints the statement
python3 demo/parties.py          # allocate parties, grant act-as rights
```

## Try to break it

The **Adversarial checks** panel runs four attacks and scores them. Every one is
refused by Canton, and the reason shown is the ledger's own
`AssertionFailed`, not our error message:

- a charge that bursts the period cap
- a payment to a party nobody approved
- a negative amount, to run the balance backwards
- a charge after the owner has revoked

The same holds through the MCP tools, so an LLM that is talked into overspending
gets refused by the ledger rather than by a check in our code.

Killing the backend mid-flight loses nothing. The mandate is a contract on
Canton, so a fresh process reads it back with the correct `spent` and carries on.

## Honest limits

- **No Canton Coin moves yet.** Our parties hold a zero balance. `Charge` is the
  authorisation step and it is genuinely live; settlement is wired
  (`demo/live.py`, `L.settle`) and will run as soon as the parties are funded.
  The Settlement panel shows exactly what is missing.
- The allow-list is fixed when the mandate is granted.
- Period windows roll forward on the next charge rather than on a timer, because
  nothing in Daml runs on a schedule. The roll-forward is anchored to
  `periodStart`, never to `now`, so the agent cannot slide the window by timing
  its charges.
- `Charge` does not itself move funds. A token-standard transfer needs disclosed
  contracts fetched from the registry for that one transaction, which a choice
  body cannot go and get. Authorisation and settlement are therefore separate
  steps, in that order.

## Where things are

```
proj/daml/Mandate.daml     the rules, and the only place they are enforced
proj/daml/*Test*.daml      8 passing scripts, incl. over-cap and post-revoke refusals
demo/live.py               real Ledger API calls: propose, accept, charge, revoke, settle
demo/agent.py              the autonomous agent and its statement
demo/server.py             HTTP backend; holds the secret so the browser never does
demo/mcp_server.py         the same tools exposed to an LLM
demo/parties.py            DevNet party allocation and act-as grants
demo/index.html            the UI
```

## DevNet notes the toolkit does not mention

Four things cost us time and are not in `SETUP.md` or `TROUBLESHOOTING.md`:

1. `c8lab.py` defaults the ledger user to `ledger-api-user`, which does not exist
   on DevNet. The Keycloak client-credentials token's subject is
   `validator-backend@clients`. Until `C8_USER` is set, act-as grants 404 and
   submissions fail with "a security-sensitive error has been received".
2. `GET /v2/parties` is paginated and DevNet has ~125k parties. c8lab's
   reuse-scan only sees page one, so it re-allocates and fails with "Party
   already exists"; paging through everything makes the node return 503. Look a
   party up directly with `GET /v2/parties/{partyId}` instead.
3. Writes want the package **id** (`<pkgid>:Mandate:Mandate`); reads want the
   package **name** (`#proj:Mandate:Mandate`). Using an id in a filter gives
   "Received an identifier with package ID ..., but expected a package name".
4. `RelTime` is JSON-encoded as `{"microseconds": "86400000000"}` — a string. An
   integer is rejected with `Expected ujson.Str`. Decimals are strings too.
   Relatedly, `dso_party()` scans the same paginated endpoint, so set
   `C8_ADMIN_PARTY` or transfers fail with a misleading "could not find the DSO
   party".
