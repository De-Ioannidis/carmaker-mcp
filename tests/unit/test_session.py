import pytest

from carmaker_mcp.backend import BackendError, check_tcl_allowed, tcl_word
from carmaker_mcp.guard import GuardError


def test_status_and_idle_gating(session, mock):
    assert session.status()["sim_status"] == "idle"
    mock.sim_status, mock.simstate = 5, "running"
    with pytest.raises(BackendError, match="running"):
        session.load_testrun("x")
    with pytest.raises(BackendError):
        session.workspace_set("Kp", 1)


def test_run_cycle(session, mock):
    session.load_testrun("My Runs/Acceleration")
    assert mock.loaded == "My Runs/Acceleration"
    mock.polls_until_idle = 3
    assert session.start_sim()["started"]
    r = session.wait_end(timeout_s=5, poll_s=0.01)
    assert r["finished"] and r["end_status"] == "completed"


def test_wait_end_times_out(session, mock):
    mock.polls_until_idle = 10_000
    session.start_sim()
    assert session.wait_end(timeout_s=0.1, poll_s=0.01)["finished"] is False


def test_workspace_set_logs_and_reverts(session, mock):
    r = session.workspace_set("Kp", 5.0)
    assert (r["old"], r["new"]) == (2.0, 5.0)
    session.workspace_set("Brand_new", 1.0)
    assert mock.workspace[("base", "Kp")] == 5.0
    out = session.revert_all()
    assert mock.workspace[("base", "Kp")] == 2.0 and ("base", "Brand_new") not in mock.workspace
    assert len(out["reverted"]) == 2 and out["failed"] == []


def test_revert_is_reverse_order_and_idempotent(session, mock):
    session.workspace_set("Kp", 3.0)
    session.workspace_set("Kp", 4.0)
    session.revert_all()
    assert mock.workspace[("base", "Kp")] == 2.0
    mock.workspace[("base", "Kp")] = 9.0
    session.revert_all()  # nothing new to revert
    assert mock.workspace[("base", "Kp")] == 9.0


def test_model_workspace_scope(session, mock):
    assert session.workspace_get("TV_ON", "mdl") == 1.0
    r = session.workspace_set("TV_ON", 0.0, "mdl")
    assert (r["old"], r["new"], r["scope"]) == (1.0, 0.0, "mdl")
    assert mock.workspace[("base", "Kp")] == 2.0  # scopes are separate
    listing = session.workspace_list("mdl", pattern="tv*")
    assert [v["name"] for v in listing["variables"]] == ["TV_ON"]
    session.revert_all()
    assert mock.workspace[("mdl", "TV_ON")] == 1.0


def test_bad_scope_rejected(session):
    for bad in ["a b", "x'; evil", "1x", ""]:
        with pytest.raises(BackendError):
            session.workspace_get("Kp", bad)


def test_invalid_variable_names_rejected(session):
    for bad in ["x; system('calc')", "a b", "1x", "x'", ""]:
        with pytest.raises(BackendError):
            session.workspace_set(bad, 1)


def test_model_set_and_revert(session, mock):
    r = session.model_set("mdl/Gain", "Gain", 7)
    assert (r["old"], r["new"]) == ("1", "7")
    session.revert_all()
    assert mock.params[("mdl/Gain", "Gain")] == "1"


def test_model_save_refuses_file_outside_project(session, mock, tmp_path):
    mock.model_files["mdl"] = str(tmp_path / "elsewhere.slx")
    with pytest.raises(GuardError):
        session.model_save("mdl")


def test_model_save_backs_up_inside_project(session, mock, project):
    f = project / "mdl.slx"
    f.write_bytes(b"orig")
    mock.model_files["mdl"] = str(f)
    session.model_save("mdl")
    assert ("save", "mdl") in mock.calls
    assert (session.store.dir / "backups" / "t1" / "mdl.slx").read_bytes() == b"orig"


def test_model_struct_revert_reloads_from_disk(session, mock, project):
    f = project / "mdl.slx"
    f.write_bytes(b"orig")
    mock.model_files["mdl"] = str(f)
    session.model_struct("add_block", src="simulink/Math Operations/Gain", dest="mdl/NewGain", params={})
    session.model_set("mdl/Gain", "Gain", 8)
    out = session.revert_all()
    assert ("reload", "mdl", str(f)) in mock.calls
    assert mock.struct_ops and out["failed"] == []


def test_tcl_denylist_and_quoting(session):
    for bad in ["exit", "exec calc.exe", "file delete foo", "cd /", "source x.tcl", "set a [exec ls]"]:
        with pytest.raises(BackendError):
            check_tcl_allowed(bad)
    for ok in ["SimStatus", "expr {1+1}", "LoadTestRun {a b}", "set exit_code 3"]:
        check_tcl_allowed(ok)
    assert session.gui_tcl("SimStatus")["status"] == 0
    assert tcl_word("My Runs/Acc") == "{My Runs/Acc}"
    assert tcl_word("simple/path_1") == "simple/path_1"
    assert tcl_word("a{b") == "a\\{b"
    assert tcl_word("") == "{}"


def test_popups_are_reported_with_the_action(session, mock):
    mock.popup_msgs = [("info", "stale message of an earlier command", 0)]
    real_load = mock.load_testrun

    def load(name, force=False):
        mock.popup_msgs.append(("warn", "Vehicle not saved. All changes will be lost.\nOK to continue?", 0))
        return real_load(name, force)

    mock.load_testrun = load
    r = session.load_testrun("My Runs/Acceleration")
    assert r["popups"] == [
        {"type": "warn", "text": "Vehicle not saved. All changes will be lost.\nOK to continue?", "answer": 0}
    ]
    mock.load_testrun = real_load
    assert "popups" not in session.load_testrun("My Runs/Acceleration")


def test_failed_action_names_the_popup(session, mock):
    def load(name, force=False):
        mock.popup_msgs.append(("err", "Cannot load 'x':\n\nFile not found", 0))
        raise BackendError("CarMaker could not load test run 'x'")

    mock.load_testrun = load
    with pytest.raises(BackendError, match=r"GUI pop-ups: \[err\] Cannot load 'x'"):
        session.load_testrun("x")

    mock.polls_until_idle = 0
    mock.start_sim = lambda: None  # the GUI never leaves idle, e.g. it waits for a click
    with pytest.raises(BackendError, match="cm_popup_timeout"):
        session.start_sim(start_timeout_s=0.05)


def test_popup_timeout_set_logged_and_reverted(session, mock):
    assert session.popup_status() == {"timeout_s": -1.0, "messages": []}
    assert session.set_popup_timeout(5) == {"timeout_s": 5, "previous_timeout_s": -1.0}
    assert session.set_popup_timeout(0)["previous_timeout_s"] == 5.0
    assert mock.popup_timeout == 0.0
    assert session.changelog()[-1]["kind"] == "popup_timeout"
    with pytest.raises(BackendError, match=">= 0"):
        session.set_popup_timeout(-3)
    session.revert_all()
    assert mock.popup_timeout == -1.0


def test_popup_timeout_from_config(project, tmp_path, mock):
    from carmaker_mcp.config import Config
    from carmaker_mcp.session import Session

    assert Config.from_env().popup_timeout is None
    s = Session(mock, Config(project=project, state_dir=tmp_path / "state", popup_timeout=3), session_id="t2")
    s.load_testrun("My Runs/Acceleration")
    assert mock.popup_timeout == 3.0
