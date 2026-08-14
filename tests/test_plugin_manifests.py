from __future__ import annotations

import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "codecanvas"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_claude_and_codex_plugins_share_product_identity_and_server() -> None:
    codex = _json(PLUGIN / ".codex-plugin" / "plugin.json")
    claude = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    mcp = _json(PLUGIN / ".mcp.json")
    project = tomllib.loads(
        (ROOT / "core" / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]

    assert codex["name"] == claude["name"] == "codecanvas"
    assert codex["version"] == claude["version"] == project["version"]
    assert codex["mcpServers"] == claude["mcpServers"] == "./.mcp.json"
    assert mcp == {
        "mcpServers": {
            "codecanvas": {
                "command": "uvx",
                "args": ["codecanvas-mcp"],
            }
        }
    }


def test_marketplaces_point_to_the_shared_plugin_package() -> None:
    claude = _json(ROOT / ".claude-plugin" / "marketplace.json")
    codex = _json(ROOT / ".agents" / "plugins" / "marketplace.json")

    assert claude["name"] == codex["name"] == "codecanvas"
    assert claude["plugins"][0]["source"] == "./plugins/codecanvas"
    assert codex["plugins"][0]["source"] == {
        "source": "local",
        "path": "./plugins/codecanvas",
    }
