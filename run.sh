#!/usr/bin/env bash
# Start the demo. One command, no arguments.
#
#   ./run.sh
#
# Then open http://localhost:8000 ON YOUR OWN MACHINE. Everyone runs their own
# copy: localhost is per-machine, so you cannot open a teammate's URL.

set -euo pipefail
cd "$(dirname "$0")"

fail() { printf '\n  %s\n\n' "$*" >&2; exit 1; }

# --- credentials -----------------------------------------------------------
[ -f .env ] || fail "No .env in $(pwd). It is committed to this repo, so a plain
  'git pull' should give you one. If it is still missing, copy .env.example to
  .env and ask the team for C8_CLIENT_SECRET."

set -a; . ./.env; set +a

for v in C8_BASE C8_IDP C8_CLIENT_ID C8_CLIENT_SECRET; do
  [ -n "${!v:-}" ] || fail "$v is not set in .env"
done

# --- python ----------------------------------------------------------------
command -v python3 >/dev/null || fail "python3 not found. Install Python 3.9 or newer."

# No pip install needed anywhere: the backend and c8lab are stdlib only.

# --- is the network actually up? -------------------------------------------
printf 'checking Canton DevNet '
if TOKEN=$(curl -s -m 20 -X POST \
      "$C8_IDP/realms/master/protocol/openid-connect/token" \
      -d grant_type=client_credentials \
      -d "client_id=$C8_CLIENT_ID" \
      -d "client_secret=$C8_CLIENT_SECRET" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])' 2>/dev/null)
then
  printf 'token ok '
else
  printf '\n'
  fail "Could not get a token from $C8_IDP.
  Either the secret in .env is stale, or you are offline.
  Ask the team for a current C8_CLIENT_SECRET."
fi

if OFFSET=$(curl -s -m 20 "$C8_BASE/v2/state/ledger-end" \
      -H "Authorization: Bearer $TOKEN" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["offset"])' 2>/dev/null)
then
  printf '· ledger offset %s\n' "$OFFSET"
else
  printf '\n'
  fail "Got a token but could not read the ledger at $C8_BASE.
  DevNet has a flaky load-balancer IP; try again, it usually works on a retry."
fi

# --- port ------------------------------------------------------------------
PORT="${PORT:-8000}"
if lsof -ti tcp:"$PORT" >/dev/null 2>&1; then
  fail "Port $PORT is already in use. Stop the other server, or run:
  PORT=8001 ./run.sh"
fi

printf '\n  open http://localhost:%s in your browser\n\n' "$PORT"
exec python3 demo/server.py
