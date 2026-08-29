import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "demo" / "mcp_server.py"


def load_module():
    spec = importlib.util.spec_from_file_location("d1_wallet_mcp_server", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_mcp_server_exposes_wallet_tools():
    mod = load_module()

    for name in (
        "create_mandate_tool",
        "charge_mandate_tool",
        "revoke_mandate_tool",
        "get_wallet_state_tool",
    ):
        assert hasattr(mod, name), f"missing tool: {name}"
        assert callable(getattr(mod, name)), f"not callable: {name}"


def test_mcp_server_has_fastmcp_instance():
    mod = load_module()
    assert hasattr(mod, "mcp")
