"""Live test: start an independent CarMaker (no MATLAB) through the MCP tools and run a short simulation.

Starts a real CarMaker simulation program, so it is opt-in:

    CM_LIVE=1 CM_LIVE_STANDALONE=1 CM_PROJECT="C:\\path\\to\\project" CM_TESTRUN="MyRuns/NoTV_Run" \
        pytest -m live tests/live/test_live_standalone.py -s

CM_TESTRUN must use a vehicle that does not need CM4SL. Optional CM_OVERRIDES as ``key=value;key2=value2``
to patch test-run keys in memory (for example a missing driver template).
"""

import asyncio
import os

import pytest
from fastmcp import Client

from carmaker_mcp import server
from carmaker_mcp.config import Config
from carmaker_mcp.results import Erg
from carmaker_mcp.session import Session

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CM_LIVE") != "1" or os.environ.get("CM_LIVE_STANDALONE") != "1",
        reason="set CM_LIVE=1 and CM_LIVE_STANDALONE=1 (this starts a CarMaker process)",
    ),
]


def test_launch_run_and_read_results(tmp_path):
    from carmaker_mcp.backend_mock import MockBackend

    cfg = Config.from_env()
    cfg.state_dir = tmp_path
    server.set_session(Session(MockBackend(), cfg))  # no MATLAB needed for standalone tools
    server.set_standalone(None)
    overrides = dict(kv.split("=", 1) for kv in os.environ.get("CM_OVERRIDES", "").split(";") if kv)

    async def go():
        async with Client(server.mcp) as c:
            launched = (await c.call_tool("cm_standalone_launch", {
                "testrun": os.environ["CM_TESTRUN"], "overrides": overrides,
                "realtime_factor": 10.0, "stop_after_s": 15.0})).structured_content
            print("launched:", launched)
            iid = launched["instance"]
            await asyncio.sleep(3)
            print("status:", (await c.call_tool("cm_standalone_status", {"instance": iid})).structured_content)
            end = (await c.call_tool("cm_standalone_wait_end", {"instance": iid, "timeout_s": 120})).structured_content
            print("end:", end)
            res = (await c.call_tool("cm_standalone_results", {"instance": iid})).structured_content
            print("results:", res)
            await c.call_tool("cm_standalone_close", {"instance": iid})
            return end, res

    try:
        end, res = asyncio.run(go())
    finally:
        server.set_standalone(None)
    assert end["finished"] and end["sim_time_s"] > 10
    assert res["result_files"], "no result file reported"
    s = Erg(res["result_files"][0]).summary(["Time", "Car.v"])
    print("summary:", s)
    assert s["duration_s"] > 10
