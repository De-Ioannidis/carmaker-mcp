"""Live tests against a real MATLAB session, using a throwaway scratch Simulink model.

Run with:  CM_LIVE=1 pytest -m live tests/live
Needs MATLAB sharing its engine (matlab.engine.shareEngine('cm_mcp')). A running CarMaker is
optional for these tests; they never touch the active CarMaker model.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from carmaker_mcp.backend import BackendError
from carmaker_mcp.backend_cm4sl import Cm4slBackend
from carmaker_mcp.config import Config
from carmaker_mcp.guard import GuardError
from carmaker_mcp.session import Session

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("CM_LIVE") != "1", reason="set CM_LIVE=1 to run live tests"),
]

MODEL = "cm_mcp_scratch"


class AlwaysIdle(Cm4slBackend):
    """Real MATLAB engine, but reports 'idle' so the tests also run without CarMaker."""

    def status(self):
        from carmaker_mcp.backend import IDLE, Status

        return Status(connected=True, sim_status=IDLE, sim_status_text="idle",
                      project_dir=str(Path(tempfile.gettempdir()) / "cm_mcp_live_proj"))


@pytest.fixture
def env(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    be = AlwaysIdle(os.environ.get("CM_MATLAB_SESSION", "cm_mcp"), 60)
    slx = tmp_path / "outside" / f"{MODEL}.slx"
    slx.parent.mkdir()
    be._call("eval", f"try, close_system('{MODEL}', 0); end", nargout=0)
    be._call(
        "eval",
        f"new_system('{MODEL}'); add_block('simulink/Math Operations/Gain', '{MODEL}/Gain', 'Gain', '1'); "
        f"mw = get_param('{MODEL}', 'ModelWorkspace'); assignin(mw, 'Kp', 2); "
        f"assignin(mw, 'Tbl', [1 2 3]); save_system('{MODEL}', '{str(slx).replace(chr(92), '/')}'); clear mw;",
        nargout=0,
    )
    s = Session(be, Config(project=proj, state_dir=tmp_path / "state"), session_id="live1")
    yield s, be, slx
    be._call("eval", f"try, close_system('{MODEL}', 0); end", nargout=0)
    shutil.rmtree(tmp_path, ignore_errors=True)


def test_model_workspace_roundtrip(env):
    s, be, _ = env
    names = {v["name"] for v in s.workspace_list(MODEL)["variables"]}
    assert {"Kp", "Tbl"} <= names
    assert s.workspace_get("Kp", MODEL) == 2.0
    assert s.workspace_get("Tbl", MODEL) == [1.0, 2.0, 3.0]
    r = s.workspace_set("Kp", 7.5, MODEL)
    assert (r["old"], r["new"]) == (2.0, 7.5)
    s.workspace_set("Tbl", [4, 5, 6, 7], MODEL)
    s.workspace_set("Fresh", 1, MODEL)
    assert s.workspace_get("Tbl", MODEL) == [4.0, 5.0, 6.0, 7.0]
    out = s.revert_all()
    assert out["failed"] == [], out
    assert s.workspace_get("Kp", MODEL) == 2.0
    assert s.workspace_get("Tbl", MODEL) == [1.0, 2.0, 3.0]
    with pytest.raises(BackendError):
        s.workspace_get("Fresh", MODEL)


def test_base_workspace_roundtrip(env):
    s, be, _ = env
    be._call("eval", "cm_mcp_live_var = 3;", nargout=0)
    try:
        s.workspace_set("cm_mcp_live_var", 4)
        assert s.workspace_get("cm_mcp_live_var") == 4.0
        s.revert_all()
        assert s.workspace_get("cm_mcp_live_var") == 3.0
    finally:
        be._call("eval", "clear cm_mcp_live_var", nargout=0)


def test_block_param_and_structure_revert(env):
    s, be, slx = env
    assert s.model_get(f"{MODEL}/Gain", "Gain") == "1"
    s.model_set(f"{MODEL}/Gain", "Gain", 5)
    assert s.model_get(f"{MODEL}/Gain", "Gain") == "5"
    s.model_struct("add_block", src="simulink/Math Operations/Gain", dest=f"{MODEL}/Gain2", params={"Gain": "9"})
    blocks = be._call("eval", f"numel(find_system('{MODEL}', 'SearchDepth', 1, 'BlockType', 'Gain'))", nargout=1)
    assert blocks == 2
    out = s.revert_all()
    assert out["failed"] == [], out
    blocks = be._call("eval", f"numel(find_system('{MODEL}', 'SearchDepth', 1, 'BlockType', 'Gain'))", nargout=1)
    assert blocks == 1  # reloaded from disk: the added block is gone
    assert s.model_get(f"{MODEL}/Gain", "Gain") == "1"


def test_model_save_outside_project_is_refused(env):
    s, be, slx = env
    s.model_set(f"{MODEL}/Gain", "Gain", 3)
    before = slx.read_bytes()
    with pytest.raises(GuardError):
        s.model_save(MODEL)
    assert slx.read_bytes() == before  # file untouched
    s.revert_all()


def test_gui_tcl_roundtrip_if_carmaker_running(env):
    s, be, _ = env
    status, result = be.gui_tcl("SimStatus", 5000)
    assert status == 0 and result.lstrip("-").isdigit()
