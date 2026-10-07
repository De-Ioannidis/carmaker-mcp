"""Build a one-click bundle (.mcpb) for Claude Desktop and other MCPB hosts.

    python packaging/build_mcpb.py                          # R2024b, Python 3.12
    python packaging/build_mcpb.py --matlab R2024a
    python packaging/build_mcpb.py --no-matlab              # standalone instances and --mock only
    python packaging/build_mcpb.py --stage-only             # write build/mcpb/<name>/ and stop

The bundle uses the MCPB "uv" server type: it contains the server's source and a small pyproject.toml,
and the host creates the Python environment with uv. Because the MATLAB engine package must have the
version of the user's MATLAB release, a bundle is built **per MATLAB release** and named after it.
Packing needs Node.js (`npx @anthropic-ai/mcpb`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "carmaker_mcp"
REPO = "https://github.com/De-Ioannidis/carmaker-mcp"

ENTRY = '''"""Entry point of the bundle (the carmaker_mcp package lies next to this file)."""

from carmaker_mcp.server import main

if __name__ == "__main__":
    main()
'''

USER_CONFIG = {
    "project": {
        "type": "directory", "title": "CarMaker project folder", "required": True,
        "description": "Your CarMaker project (the folder that contains Data and src_cm4sl). "
                       "All file changes are confined to it.",
    },
    "model": {
        "type": "string", "title": "Simulink model", "required": False, "default": "",
        "description": "Model that is opened when the agent starts the session (name on the MATLAB path "
                       "or a file). May be left empty.",
    },
    "matlab_init": {
        "type": "string", "title": "MATLAB setup script", "required": False, "default": "",
        "description": "Your project's own MATLAB script that must run before the model compiles (adds "
                       "folders to the path, loads parameters). A file name in src_cm4sl or a full path. "
                       "May be left empty.",
    },
    "matlab_session": {
        "type": "string", "title": "MATLAB session name", "required": False, "default": "cm_mcp",
        "description": "Name under which MATLAB shares its engine (matlab.engine.shareEngine).",
    },
    "popup_timeout": {
        "type": "string", "title": "Answer GUI pop-ups after (seconds)", "required": False, "default": "",
        "description": "Empty: CarMaker pop-ups wait for a click. A number: they answer themselves with "
                       "their default choice after that many seconds, which can discard unsaved GUI data.",
    },
}


def engine_pin(release: str) -> str:
    """'R2024b' -> 'matlabengine==24.2.*'."""
    m = re.fullmatch(r"R20(\d\d)([ab])", release)
    if not m:
        raise SystemExit(f"not a MATLAB release name: {release!r} (expected e.g. R2024b)")
    return f"matlabengine=={int(m[1])}.{1 if m[2] == 'a' else 2}.*"


def project_dependencies() -> list[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r"^dependencies = (\[.*\])$", text, re.M)
    if not m:
        raise SystemExit("could not read the dependencies from pyproject.toml")
    return json.loads(m[1])


def version() -> str:
    m = re.search(r'^__version__ = "([^"]+)"', (SRC / "__init__.py").read_text(encoding="utf-8"), re.M)
    if not m:
        raise SystemExit("could not read the version from src/carmaker_mcp/__init__.py")
    return m[1]


def tool_list() -> list[dict]:
    from fastmcp import Client

    from carmaker_mcp import server

    async def go():
        async with Client(server.create_server()) as c:
            return [{"name": t.name, "description": (t.title or t.name)} for t in await c.list_tools()]

    return asyncio.run(go())


def manifest(ver: str, python: str, matlab: str | None, tools: list[dict]) -> dict:
    nxt = f"3.{int(python.split('.')[1]) + 1}"
    flavour = f"MATLAB {matlab}" if matlab else "standalone runs only, no MATLAB"
    return {
        "manifest_version": "0.4",
        "name": "carmaker-mcp",
        "display_name": f"CarMaker ({matlab})" if matlab else "CarMaker (standalone)",
        "version": ver,
        "description": "Drive IPG CarMaker: run test runs, read results, edit vehicle data and "
                       "Simulink parameters.",
        "long_description": (
            "Lets Claude drive IPG CarMaker on this computer: start a CarMaker for Simulink session, load "
            "and run test runs, read results, edit vehicle / driver / tyre / test-run data and Simulink "
            "controller parameters, and run small parameter studies. Every change is backed up, logged and "
            f"revertible.\n\nThis bundle is built for **{flavour}** and Python {python}. You need your own "
            "CarMaker (with its Python API for that Python version) and MATLAB licences. Unofficial "
            "community project, not affiliated with IPG Automotive or MathWorks. The server edits real "
            f"project files in place: read {REPO}/blob/main/docs/safety.md first."
        ),
        "author": {"name": "Dimitrios Ioannidis", "url": "https://github.com/De-Ioannidis"},
        "repository": {"type": "git", "url": REPO},
        "homepage": REPO,
        "documentation": f"{REPO}/blob/main/docs/setup.md",
        "support": f"{REPO}/issues",
        "license": "MIT",
        "keywords": ["carmaker", "ipg", "simulink", "matlab", "vehicle dynamics", "simulation"],
        "server": {
            "type": "uv",
            "entry_point": "server.py",
            "mcp_config": {
                "command": "uv",
                "args": ["run", "--directory", "${__dirname}", "server.py"],
                "env": {
                    "CM_PROJECT": "${user_config.project}",
                    "CM_MODEL": "${user_config.model}",
                    "CM_MATLAB_INIT": "${user_config.matlab_init}",
                    "CM_MATLAB_SESSION": "${user_config.matlab_session}",
                    "CM_POPUP_TIMEOUT": "${user_config.popup_timeout}",
                },
            },
        },
        "user_config": USER_CONFIG,
        "tools": tools,
        "compatibility": {"platforms": ["win32"], "runtimes": {"python": f">={python},<{nxt}"}},
    }


def bundle_pyproject(ver: str, python: str, matlab: str | None) -> str:
    nxt = f"3.{int(python.split('.')[1]) + 1}"
    deps = project_dependencies() + ([engine_pin(matlab)] if matlab else [])
    return "\n".join([
        "# Environment of the bundled server; created by the host with uv.",
        "[project]",
        'name = "carmaker-mcp-bundle"',
        f'version = "{ver}"',
        f'requires-python = ">={python},<{nxt}"',
        "dependencies = [",
        *[f'    "{d}",' for d in deps],
        "]",
        "",
        "[tool.uv]",
        "package = false",
        "",
    ])


def stage(python: str, matlab: str | None) -> Path:
    ver = version()
    name = f"carmaker-mcp-{ver}-{matlab or 'standalone'}-py{python.replace('.', '')}"
    out = ROOT / "build" / "mcpb" / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copytree(SRC, out / "carmaker_mcp", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (out / "server.py").write_text(ENTRY, encoding="utf-8", newline="\n")
    (out / "pyproject.toml").write_text(bundle_pyproject(ver, python, matlab), encoding="utf-8", newline="\n")
    (out / ".python-version").write_text(python + "\n", encoding="utf-8", newline="\n")
    text = json.dumps(manifest(ver, python, matlab, tool_list()), indent=2) + "\n"
    (out / "manifest.json").write_text(text, encoding="utf-8", newline="\n")
    # never pack an environment that was created by trying the staged folder out
    (out / ".mcpbignore").write_text(".venv/\nuv.lock\n__pycache__/\n", encoding="utf-8", newline="\n")
    shutil.copy2(ROOT / "LICENSE", out / "LICENSE")
    shutil.copy2(ROOT / "README.md", out / "README.md")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--matlab", default="R2024b", help="MATLAB release the bundle is for (default R2024b)")
    ap.add_argument("--no-matlab", action="store_true", help="no MATLAB engine: standalone and --mock only")
    ap.add_argument("--python", default="3.12", help="Python version (your CarMaker must ship cmapi for it)")
    ap.add_argument("--stage-only", action="store_true", help="do not pack")
    args = ap.parse_args()

    staged = stage(args.python, None if args.no_matlab else args.matlab)
    print(f"staged {staged}")
    if args.stage_only:
        return 0
    npx = shutil.which("npx")
    if not npx:
        print("npx (Node.js) not found: install Node.js, or pack the staged folder elsewhere")
        return 1
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    target = dist / f"{staged.name}.mcpb"
    for cmd in (["validate", str(staged / "manifest.json")], ["pack", str(staged), str(target)]):
        r = subprocess.run([npx, "--yes", "@anthropic-ai/mcpb", *cmd])
        if r.returncode != 0:
            return r.returncode
    print(f"built {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
