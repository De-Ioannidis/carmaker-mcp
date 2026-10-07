"""Saving Simulink's logged data and the workspaces to a MAT file."""

import pytest
from test_backend_cm4sl import backend

from carmaker_mcp.backend import BackendError, BackendTimeout
from carmaker_mcp.guard import GuardError
from carmaker_mcp.matlab_code import SAVE_LOGS, SAVE_LOGS_NAME


def test_default_file_is_next_to_the_result_file(session, mock, project):
    erg = project / "SimOutput" / "host" / "20260101" / "Run1_120000.erg"
    erg.parent.mkdir(parents=True)
    erg.write_bytes(b"x")
    mock.last_result = str(erg)
    r = session.model_logs_save()
    assert r["file"] == str(erg.with_suffix(".mat")) and erg.with_suffix(".mat").is_file()
    assert r["model"] == "mdl" and r["run"] == "Run 1: mdl" and r["signals"] == 12
    assert r["variables"] == ["signals", "base_workspace", "model_workspace", "info"]
    assert r["base_variables"] == 1 and r["model_variables"] == 1 and "notes" not in r
    assert mock.calls[-1] == ("save_logs", str(erg.with_suffix(".mat")), "mdl", "inspector", "dataset", True, True)

    with pytest.raises(BackendError, match=r"Run1_120000.mat exists already \(pass overwrite=true"):
        session.model_logs_save()
    assert session.model_logs_save(overwrite=True)["size_mb"] == 0.0


def test_without_a_result_file_it_goes_to_a_folder_of_its_own(session, mock, project):
    r = session.model_logs_save(logs="both", fmt="struct", base_workspace=False)
    assert r["file"].startswith(str(project / "SimOutput" / "SimulinkLogs" / "mdl_")) and r["file"].endswith(".mat")
    assert r["workspace_logs"] == ["logsout"] and "base_workspace" not in r["variables"]
    assert mock.calls[-1][3:] == ("both", "struct", False, True)


def test_named_file_stays_inside_the_project(session, mock, project, tmp_path):
    r = session.model_logs_save(file="SimOutput/mine/logs")
    assert r["file"] == str(project / "SimOutput" / "mine" / "logs.mat")
    with pytest.raises(GuardError):
        session.model_logs_save(file=str(tmp_path / "elsewhere.mat"))
    with pytest.raises(GuardError):
        session.model_logs_save(file="../outside.mat")
    assert not (tmp_path / "elsewhere.mat").exists()


def test_checks_and_notes(session, mock):
    for bad in ({"logs": "all"}, {"fmt": "csv"}, {"model": "mdl; delete"}, {"model": "a/b"}):
        with pytest.raises(BackendError):
            session.model_logs_save(**bad)
    mock.logged_signals = 0
    r = session.model_logs_save()
    assert r["signals"] == 0 and r["run"] is None and "no run of this model" in r["notes"][0]
    mock.sim_status = 3
    with pytest.raises(BackendError, match="cannot save the logged data while the simulation state is 'running'"):
        session.model_logs_save()


def test_a_slow_matlab_is_reported(session, mock):
    def slow(*a):
        raise BackendTimeout("MATLAB did not answer cm_mcp_save_logs within 5s")

    mock.save_logs = slow
    with pytest.raises(BackendError, match="did not finish writing the file within 5s"):
        session.model_logs_save(timeout_s=5)


def test_backend_runs_its_matlab_function_and_leaves_no_trace(tmp_path):
    answer = '{"run":"Run 3: M","signals":2,"workspace_logs":[],"skipped":["base: big (80 MB)"],"variables":["signals","info"]}'
    b = backend(addpath=lambda d: None, rmpath=lambda d: None, **{SAVE_LOGS_NAME: lambda *a: answer})
    helper = tmp_path / "state" / "matlab"
    r = b.save_logs(tmp_path / "x.mat", "M", "inspector", "dataset", True, False, 50e6, helper, 60)
    assert r["signals"] == 2 and r["skipped"] == ["base: big (80 MB)"]
    assert (helper / f"{SAVE_LOGS_NAME}.m").read_text(encoding="utf-8") == SAVE_LOGS
    names = [c[0] for c in b._eng.calls]
    assert names == ["addpath", SAVE_LOGS_NAME, "rmpath"]
    assert b._eng.calls[1][1] == (str(tmp_path / "x.mat"), "M", "inspector", "dataset", True, False, 50e6)

    failing = backend(addpath=lambda d: None, rmpath=lambda d: None,
                      **{SAVE_LOGS_NAME: lambda *a: RuntimeError("Unable to write file")})
    with pytest.raises(BackendError, match="Unable to write file"):
        failing.save_logs(tmp_path / "y.mat", "M", "both", "struct", True, True, 1e6, helper, 60)
    assert [c[0] for c in failing._eng.calls][-1] == "rmpath"  # the path is cleaned up also then


def test_matlab_source_is_one_function_file():
    assert SAVE_LOGS.startswith(f"function out = {SAVE_LOGS_NAME}(") and chr(92) not in SAVE_LOGS
    for name in ("signals", "workspace_logs", "base_workspace", "model_workspace", "info"):
        assert f"S.{name}" in SAVE_LOGS


# ---- saved automatically -------------------------------------------------------------------
def test_start_with_save_logs_saves_when_the_end_is_reported(session, mock, project):
    mock.polls_until_idle = 2
    session.start_sim(save_logs=True)
    assert not [c for c in mock.calls if c[0] == "save_logs"]  # not before the run is over
    r = session.wait_end(timeout_s=5, poll_s=0.01)
    assert r["finished"] and r["logs_file"].endswith(".mat") and (project / "SimOutput").is_dir()
    again = session.wait_end(timeout_s=5, poll_s=0.01)
    assert "logs_file" not in again and len([c for c in mock.calls if c[0] == "save_logs"]) == 1

    session.start_sim()  # the option holds for one run
    assert "logs_file" not in session.wait_end(timeout_s=5, poll_s=0.01)


def test_stopped_run_is_saved_and_a_failed_one_is_not(session, mock):
    mock.polls_until_idle = 10_000
    session.start_sim(save_logs=True)
    assert "logs_file" in session.stop_sim(wait_s=2)  # aborted: Simulink logged up to the stop

    mock.polls_until_idle = 2
    session.start_sim(save_logs=True)
    real = mock.status

    def failed():
        st = real()
        if st.idle:
            st.end_status = "failed"
        return st

    mock.status = failed
    r = session.wait_end(timeout_s=5, poll_s=0.01)
    assert r["end_status"] == "failed" and "logs_file" not in r and "logs_error" not in r


def test_a_save_that_fails_is_reported_with_the_result(session, mock):
    mock.polls_until_idle = 2
    mock.save_logs = lambda *a: (_ for _ in ()).throw(BackendError("MATLAB error: disk full"))
    session.start_sim(save_logs=True)
    r = session.wait_end(timeout_s=5, poll_s=0.01)
    assert r["end_status"] == "completed" and r["logs_error"] == "MATLAB error: disk full"


def test_study_saves_the_logs_of_every_run_with_that_runs_values(session, mock):
    from carmaker_mcp.study import StudyRunner

    mock.polls_until_idle = 2
    seen = []
    real = mock.save_logs

    def save(file, *a):
        seen.append(mock.workspace[("base", "Kp")])  # the value in MATLAB while the file is written
        return real(file, *a)

    mock.save_logs = save
    runner = StudyRunner(session, lambda: pytest.fail("standalone manager not expected"))
    r = runner.start("Sub/Run1", [{"label": "a", "workspace": {"Kp": 5.0}}, {"label": "b", "workspace": {"Kp": 7.0}}],
                     save_logs=True)
    st = runner.wait(r["study"])
    assert st["state"] == "done" and seen == [5.0, 7.0] and mock.workspace[("base", "Kp")] == 2.0
    files = [row["logs_file"] for row in st["rows"]]
    assert len(set(files)) == 2 and all(f.endswith(".mat") for f in files)

    plain = runner.wait(runner.start("Sub/Run1", [{"label": "c"}])["study"])
    assert "logs_file" not in plain["rows"][0]
    with pytest.raises(BackendError, match="save_logs needs the MATLAB-connected session"):
        runner.start("Sub/Run1", [{"label": "d"}], mode="standalone", save_logs=True)
