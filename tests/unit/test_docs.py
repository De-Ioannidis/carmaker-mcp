"""The documentation that is derived from the code must match the code."""

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tools_reference_is_up_to_date():
    gen = load_script("gen_tools_doc")
    assert (ROOT / "docs" / "tools.md").read_text(encoding="utf-8") == gen.generate(), (
        "docs/tools.md is out of date: run `python scripts/gen_tools_doc.py`")


def test_readme_documents_every_environment_variable():
    """Every CM_* variable the code reads is explained in the README, and none is invented there."""
    used = set()
    for py in (ROOT / "src" / "carmaker_mcp").glob("*.py"):
        used |= set(re.findall(r'"(CM_[A-Z_]+)"', py.read_text(encoding="utf-8")))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`(CM_[A-Z_]+)`", readme))
    assert used - documented == set(), f"not in the README: {sorted(used - documented)}"
    assert documented - used == set(), f"README mentions unknown variables: {sorted(documented - used)}"
