"""The gate instruments (`scripts/`) still parse, and still name real things.

They run by hand, at a gate, against a live rig — so nothing in CI would notice a
rename that broke them until someone needed them mid-rehearsal. A6 renamed
`transcript_key` to `session_key`; a script that had said the old name would have
failed at exactly the wrong moment. These two checks are cheap and catch that
class: the file parses, and every name it imports from the control plane exists.
"""

import ast
import importlib
from pathlib import Path

import pytest

SCRIPTS = sorted((Path(__file__).parent.parent / "scripts").glob("*.py"))


def test_there_are_instruments_to_check():
    assert SCRIPTS, "scripts/ holds the byte-for-byte gate instruments (STORES.md)"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_the_instrument_parses_and_its_imports_resolve(script):
    tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not (node.module or "").startswith("control_plane"):
            continue
        module = importlib.import_module(node.module)
        for alias in node.names:
            assert hasattr(module, alias.name), (
                f"{script.name} imports {alias.name} from {node.module}, which no longer has it"
            )
