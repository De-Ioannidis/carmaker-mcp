"""Live parameter study in the MATLAB-connected session.

STARTS SIMULATIONS in the open CarMaker GUI (one per variation), so it is opt-in:

    CM_LIVE=1 CM_LIVE_SIM=1 CM_TESTRUN="Examples/..." \
    CM_STUDY_VARIATIONS='[{"label": "light", "keys": {"Body.mass": 250}}, {"label": "heavy", "keys": {"Body.mass": 350}}]' \
        pytest -m live tests/live/test_live_study.py -s

CM_STUDY_VARIATIONS is the JSON list that cm_study_start takes ("keys": infofile keys applied in memory,
"workspace": MATLAB variables in CM_STUDY_SCOPE, default "base"). Without it the test run is simply run
twice. Choose values whose effect shows in CM_STUDY_QUANTITIES (default "Time,Car.v") to see whether the
in-memory keys reach the simulation: the test prints the table and checks that every run completed and
that workspace values are back afterwards.
"""

import json
import os
import time

import pytest

from carmaker_mcp.backend_cm4sl import Cm4slBackend
from carmaker_mcp.config import Config
from carmaker_mcp.session import Session
from carmaker_mcp.study import StudyRunner

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CM_LIVE") != "1" or os.environ.get("CM_LIVE_SIM") != "1",
        reason="set CM_LIVE=1 and CM_LIVE_SIM=1 (this starts simulations)",
    ),
]


def test_study_in_the_session(tmp_path):
    cfg = Config.from_env()
    cfg.state_dir = tmp_path
    session = Session(Cm4slBackend(cfg.matlab_session, cfg.engine_timeout), cfg)
    variations = json.loads(os.environ.get("CM_STUDY_VARIATIONS", "[{}, {}]"))
    scope = os.environ.get("CM_STUDY_SCOPE", "base")
    quantities = os.environ.get("CM_STUDY_QUANTITIES", "Time,Car.v").split(",")
    before = {name: session.workspace_get(name, scope) for v in variations for name in v.get("workspace", {})}

    runner = StudyRunner(session, lambda: pytest.fail("standalone manager not expected"))
    try:
        start = runner.start(os.environ["CM_TESTRUN"], variations, quantities, "cm4sl", scope, max_run_s=900)
        print("\nstart ->", start)
        t0 = time.time()
        while True:
            st = runner.status()
            if st["state"] != "running":
                break
            assert time.time() - t0 < 3600
            time.sleep(5)
        print(f"state {st['state']} after {time.time() - t0:.0f}s")
        for row in st["rows"]:
            print(json.dumps(row, default=str)[:1500])
        assert st["state"] == "done" and len(st["rows"]) == len(variations)
        for row in st["rows"]:
            assert "error" not in row and "restore_errors" not in row
            assert row["end_status"] == "completed" and row["result_file"]
        after = {name: session.workspace_get(name, scope) for name in before}
        assert after == before, "workspace values were not restored"
    finally:
        if runner._studies and runner.status()["state"] == "running":
            runner.cancel()
        print("revert ->", session.revert_all())
