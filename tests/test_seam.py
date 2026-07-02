"""The module seam is a wall: dev_agent must never import control_plane.

The image installs only the dev-agent package; if any dev_agent module imported
control_plane, platform code would ride into the box (ARCHITECTURE.md §8).
"""

import ast
from pathlib import Path

DEV_AGENT_SRC = Path(__file__).parent.parent / "packages" / "dev-agent" / "src" / "dev_agent"


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_dev_agent_never_imports_control_plane():
    for module in DEV_AGENT_SRC.rglob("*.py"):
        for name in _imports_of(module):
            assert not name.startswith("control_plane"), (
                f"{module.name} imports {name} — platform code must not enter the image"
            )


def test_dev_agent_dependencies_exclude_control_plane():
    pyproject = (DEV_AGENT_SRC.parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "control-plane" not in pyproject.replace(
        "must never depend on control-plane", ""
    )
