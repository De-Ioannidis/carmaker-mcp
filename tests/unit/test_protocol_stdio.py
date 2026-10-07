"""The server as a real subprocess speaking MCP over stdio (mock backend)."""

import asyncio
import os
import subprocess
import sys

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


def test_stdio_handshake_tools_resources_and_clean_stdout(project, tmp_path):
    env = {**os.environ, "CM_PROJECT": str(project), "CM_STATE_DIR": str(tmp_path / "state"),
           "CM_ENABLE": "", "CM_DISABLE": "study"}
    transport = StdioTransport(command=sys.executable, args=["-m", "carmaker_mcp.server", "--mock"],
                               env=env, keep_alive=False)

    async def go():
        async with Client(transport) as c:
            names = {t.name for t in await c.list_tools()}
            status = (await c.call_tool("cm_status", {})).structured_content
            listed = (await c.call_tool("cm_list", {"kind": "testrun"})).structured_content
            edit = (await c.call_tool("cm_edit", {"kind": "vehicle", "name": "Test_Vehicle",
                                                  "overrides": {"Body.mass": 300}})).structured_content
            guide = (await c.read_resource("carmaker://guide"))[0].text
            return names, status, listed, edit, guide

    names, status, listed, edit, guide = asyncio.run(go())
    assert "cm_start_sim" in names and "cm_gui_tcl" not in names and "cm_study_start" not in names
    assert status["sim_status"] == "idle" and listed["names"] == ["Sub/Run1"]
    assert edit["changes"][0]["new"] == "300" and guide.startswith("# carmaker-mcp guide")
    # the server logged its start and the calls to the state folder, not to stdout
    log = (tmp_path / "state" / "server.log").read_text(encoding="utf-8")
    assert "starting" in log and "cm_edit ok" in log


def test_cli_version_and_help():
    for arg, expect in (("--version", "carmaker-mcp "), ("--help", "doctor")):
        out = subprocess.run([sys.executable, "-m", "carmaker_mcp.server", arg], capture_output=True,
                             text=True, timeout=60)
        assert out.returncode == 0 and expect in out.stdout
