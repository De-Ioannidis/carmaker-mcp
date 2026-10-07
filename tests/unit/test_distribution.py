"""Registry entry (server.json) and one-click bundle (MCPB) stay consistent with the package."""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from carmaker_mcp import __version__, server

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def mcpb():
    spec = importlib.util.spec_from_file_location("build_mcpb", ROOT / "packaging" / "build_mcpb.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_mcpb"] = mod
    spec.loader.exec_module(mod)
    return mod


def source_env_vars():
    used = set()
    for py in (ROOT / "src" / "carmaker_mcp").glob("*.py"):
        used |= set(re.findall(r'"(CM_[A-Z_]+)"', py.read_text(encoding="utf-8")))
    return used


def test_server_json_matches_the_package():
    sj = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    pkg = sj["packages"][0]
    assert sj["version"] == pkg["version"] == __version__
    assert pkg["registryType"] == "pypi" and pkg["identifier"] == "carmaker-mcp"
    assert pkg["transport"] == {"type": "stdio"} and pkg["runtimeHint"] == "uvx"
    assert len(sj["description"]) <= 100
    # the registry verifies ownership of the PyPI package through this marker in its README
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"<!-- mcp-name: {sj['name']} -->" in readme
    assert sj["name"].lower() == "io.github.de-ioannidis/carmaker-mcp"
    assert {v["name"] for v in pkg["environmentVariables"]} <= source_env_vars()


def test_engine_pin(mcpb):
    assert mcpb.engine_pin("R2024b") == "matlabengine==24.2.*"
    assert mcpb.engine_pin("R2023b") == "matlabengine==23.2.*"
    assert mcpb.engine_pin("R2023a") == "matlabengine==9.14.*"  # before R2023b the numbers do not follow the year
    assert [mcpb.default_python(r) for r in ("R2022b", "R2023a", "R2023b", "R2024a", "R2024b", None)] == [
        "3.10", "3.10", "3.11", "3.11", "3.12", "3.12"]
    with pytest.raises(SystemExit):
        mcpb.default_python("R2022a")
    with pytest.raises(SystemExit):
        mcpb.engine_pin("2024b")


def test_bundle_manifest_and_environment(mcpb):
    assert mcpb.version() == __version__
    tools = mcpb.tool_list()
    m = mcpb.manifest(__version__, "3.12", "R2024b", tools)
    assert m["manifest_version"] == "0.4" and m["version"] == __version__
    assert m["server"]["type"] == "uv" and m["server"]["entry_point"] == "server.py"
    assert m["server"]["mcp_config"]["args"] == ["run", "--directory", "${__dirname}", "server.py"]
    assert m["compatibility"] == {"platforms": ["win32"], "runtimes": {"python": ">=3.12,<3.13"}}
    # every setting of the install dialog reaches the server as an environment variable it reads
    env = m["server"]["mcp_config"]["env"]
    assert set(env) <= source_env_vars()
    assert {v.removeprefix("${user_config.").removesuffix("}") for v in env.values()} == set(m["user_config"])
    assert m["user_config"]["project"]["required"] is True and m["user_config"]["project"]["type"] == "directory"
    assert {t["name"] for t in m["tools"]} == {s.fn.__name__ for s in server._TOOLS if s.feature is None}
    assert "standalone" in mcpb.manifest(__version__, "3.12", None, tools)["display_name"]

    py = mcpb.bundle_pyproject(__version__, "3.12", "R2024b")
    assert 'requires-python = ">=3.12,<3.13"' in py and '"matlabengine==24.2.*",' in py
    for dep in mcpb.project_dependencies():
        assert f'"{dep}",' in py
    assert "matlabengine" not in mcpb.bundle_pyproject(__version__, "3.12", None)


def test_unfilled_host_placeholders_count_as_unset(monkeypatch):
    from carmaker_mcp.config import Config

    monkeypatch.setenv("CM_MODEL", "${user_config.model}")
    monkeypatch.setenv("CM_POPUP_TIMEOUT", "")
    monkeypatch.setenv("CM_MATLAB_SESSION", "mine")
    cfg = Config.from_env()
    assert cfg.model is None and cfg.popup_timeout is None and cfg.matlab_session == "mine"
