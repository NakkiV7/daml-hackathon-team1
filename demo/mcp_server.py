#!/usr/bin/env python3
"""MCP server for the D1 agent-wallet demo.

This layer is intentionally thin. All real enforcement remains in the Daml
contract and the backend mirrors those rules for the demo. The MCP server is the
agent-facing API: it provides a stable set of tools for creating mandates,
charging, revoking, and reading state without duplicating wallet logic.
"""
from __future__ import annotations

import sys
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
if str(DEMO_DIR) not in sys.path:
    sys.path.insert(0, str(DEMO_DIR))

import server as wallet_server
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "D1-Agent-Wallet",
    instructions=(
        "Use these tools to manage the D1 agent-wallet mandate. "
        "The Daml contract is the source of truth for all spending limits, "
        "allow-lists, expiry checks, and revocation logic."
    ),
)


def _normalise_counterparties(counterparties):
    if counterparties is None:
        return ["Merchant", "Cloud"]
    if isinstance(counterparties, str):
        counterparties = [counterparties]
    values = []
    for item in counterparties:
        text = str(item).strip()
        if text:
            values.append(text)
    return values or ["Merchant", "Cloud"]


@mcp.tool()
def create_mandate_tool(
    cap: float,
    counterparties: list[str] | None = None,
    hours: float = 720,
    period_cap: float | None = None,
    period_hours: float = 24,
) -> dict:
    """Create a new mandate for the owner/agent wallet.

    The Daml contract enforces the final limit checks. This tool simply invokes
    the existing mandate creation flow used by the demo.
    """
    if cap <= 0:
        raise ValueError("cap must be greater than zero")
    if period_cap is None:
        period_cap = cap
    if period_hours <= 0:
        raise ValueError("period_hours must be greater than zero")
    return wallet_server.create_mandate(
        cap=cap,
        period_cap=period_cap,
        period_hours=period_hours,
        counterparties=_normalise_counterparties(counterparties),
        hours=hours,
    )


@mcp.tool()
def charge_mandate_tool(amount: float, payee: str) -> dict:
    """Charge the mandate if the Daml rules allow it."""
    if amount <= 0:
        raise ValueError("amount must be greater than zero")
    if not payee or not str(payee).strip():
        raise ValueError("payee is required")
    result = wallet_server.charge(amount=amount, payee=str(payee).strip())
    return {
        "status": "ok",
        "event": result,
    }


@mcp.tool()
def revoke_mandate_tool() -> dict:
    """Revoke the active mandate. This matches the owner-controlled Daml choice."""
    result = wallet_server.revoke()
    return {
        "status": "ok",
        "event": result,
    }


@mcp.tool()
def get_wallet_state_tool() -> dict:
    """Return the current mandate state and audit trail for the wallet."""
    return wallet_server.get_state()


@mcp.tool()
def healthcheck_tool() -> dict:
    """Return the ledger and backend status for the wallet."""
    try:
        return wallet_server.live_ledger()
    except Exception as exc:  # pragma: no cover - defensive path for demo mode
        return {"status": "unavailable", "error": str(exc)}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
