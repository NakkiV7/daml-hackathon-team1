# Demo runbook

What the judges said they will do:

> "We will try to make your agent exceed its cap, and pay someone it should not.
> Both must fail **on the ledger**, not in your API. Be ready to show us the line
> of Daml that stops it. Then we will revoke and try again."

Every step below has been run against Canton DevNet and refused by the ledger.
Follow it in order.

## Before they arrive

```bash
git pull
./run.sh
```

Open <http://localhost:8000>. Press **Check ledger** — it should show a ledger
offset. Press **Return the coin** so the agent starts with its full balance.

Have `proj/daml/Mandate.daml` open in a second window at the `Charge` choice.
That is the file they will ask to see.

---

## 1. Grant the mandate

Panel **1 · Owner creates a mandate**, then **Grant mandate**.

Defaults: lifetime 500, period 100 per 24h, one approved payee (Cloud vendor).

> "The business gives its agent a mandate: up to 500 in total, no more than 100 a
> day, and only with one approved supplier."

## 2. Let the agent run itself

**Run the agent.** It works a seven-item backlog and decides each purchase
itself; three of them are things a real agent would plausibly get wrong.

Result: **4 settled, 3 refused, 510 of overspend prevented, 100% decision
accuracy.** Those are the numbers to say out loud.

> "Nobody clicked a button per purchase. The agent chose all seven. It does not
> know which three are wrong -- it tries them, and the ledger refuses."

**Run the agent on a freshly granted mandate.** The backlog is sized to use 90 of
the 100 period budget, so if you make a manual charge first, one legitimate
purchase gets refused for lack of headroom and the numbers come out 3 and 4
instead of 4 and 3. Press **Grant mandate** again before re-running.

## 3. A single charge by hand

Panel **2 · Agent spends** → amount 5, payee Cloud vendor → **Charge**.

Accepted. Point at **Live mandate state** and the audit log gaining a row.

## 4. Exceed the cap — what they will try

**Adversarial checks** → **Attempt all four**. Or by hand in panel 2:

| Amount | Payee | The ledger says |
|---|---|---|
| 999 | Cloud vendor | `charge would exceed the period cap` |
| 100000 | Cloud vendor | `charge would exceed the period cap` |
| -50 | Cloud vendor | `amount must be positive` |
| 5 | Unapproved vendor | `payee not in allow-list` |

**If they ask for the lifetime cap specifically**, the period cap is what fires
first on a big single charge. Grant a fresh mandate with the period cap set
*above* the lifetime cap so the lifetime one is reachable:

- lifetime cap **100**, period cap **1000**
- charge **90** → accepted
- charge **20** → `charge would exceed the cap`

That is the lifetime cap refusing, not the period cap. Verified.

## 5. Pay someone it should not

Amount 5, payee **Unapproved vendor** → `payee not in allow-list`.

Worth saying: that is a real party on the ledger, not a malformed name. It is
refused because it is not on the allow-list, which is the point.

## 6. Show the line of Daml

`proj/daml/Mandate.daml`, inside `choice Charge`:

```daml
assertMsg "charge would exceed the period cap" (curPeriodSpent + amount <= periodCap)
assertMsg "charge would exceed the cap"        (spent + amount <= cap)
assertMsg "payee not in allow-list"            (payee `elem` counterparties)
```

The messages in the browser are these strings, returned by Canton as
`DA.Exception.AssertionFailed`. Nothing in our Python produced them.

Then the argument: `controller spender` means the agent's authority alone drives
`Charge`, and these assertions sit in the choice body — so they apply to anyone
reaching the ledger, including someone bypassing our app entirely.

## 7. Revoke and try again

**Revoke now**, then charge anything → `mandate revoked`.

```daml
choice Revoke : ContractId Mandate
  controller owner        -- the owner alone; the agent cannot block or delay it
```

## 8. Real money

**Pay 0.1 for real** → balances change: agent −0.1, payee +0.1. Actual Canton
Coin, settled through the token standard registry.

**Return the coin** to reset.

---

## Things they may push on, and the honest answer

**"Is the cap in your backend?"**
No. Our backend calls `Charge` and reports whatever the ledger returns. Delete
our backend and the cap still holds. The MCP tools an LLM drives go through the
same path.

**"Show me it is not mocked."**
The ledger offset on screen is live and climbing. The mandate is a contract with
a real contract id. Kill the backend and restart it — it re-reads the mandate
from the ledger with the correct spent, because the state was never ours.

**"Does money actually move?"**
Yes, 0.1 at a time. The agent holds about 5 Canton Coin. The authorisation cap is
500 while the balance is 5, so the cap is proven by refusal rather than by
exhaustion. A charge of 25 authorises on the ledger but cannot settle, and the
response says so rather than pretending.

**"What is not done?"**
The allow-list is fixed when the mandate is granted. Period windows roll forward
on the next charge rather than on a timer, because nothing in Daml runs on a
schedule. `Charge` authorises and a separate transfer settles: a token-standard
transfer needs disclosed contracts fetched from the registry for that one
transaction, which a Daml choice body cannot go and get.

**"Contract could not be found"** appears if two people act on the same mandate,
since every charge archives it and creates a successor. Press the button again;
the backend re-reads the current one. That is Canton being correct.

---

## Recording it

There is no AI video generation here, and a synthesised UI would be the wrong
thing to show a panel that intends to break the real build. Record the real
thing: `Cmd+Shift+5` on macOS, select the browser window, run steps 1 to 8.
Roughly two minutes at a normal pace.
