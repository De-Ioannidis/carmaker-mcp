"""Results storage, session log, bounded waiting, stop and locking (mock backend)."""

import threading
import time

import pytest

from carmaker_mcp import log as sessionlog
from carmaker_mcp.backend import BackendError

TAB, CRLF = chr(9), chr(13) + chr(10)


def write_log(project, *records):
    """Append records (tuples of fields) to the project's session log, CarMaker style."""
    d = project / "SimOutput" / "host" / "Log"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "host_20260101_120000.log"
    with f.open("ab") as fh:
        for rec in records:
            fh.write((TAB.join(rec) + CRLF).encode())
    return f


def test_start_saves_results_by_default_and_revert_restores_mode(session, mock):
    st = session.status()
    assert st["save_mode"] == "collect" and st["gui_all_saved"] is True
    mock.polls_until_idle = 2
    assert session.start_sim()["save_mode"] == "save"
    assert session.changelog()[-1]["kind"] == "save_mode"
    session.wait_end(timeout_s=5, poll_s=0.01)
    session.revert_all()
    assert mock.save_mode == "collect"

    mock.polls_until_idle = 2
    session.start_sim(save="keep")
    assert mock.save_mode == "collect"
    session.wait_end(timeout_s=5, poll_s=0.01)
    with pytest.raises(BackendError, match="save must be"):
        session.start_sim(save="yes")


def test_wait_end_reports_result_file_log_and_sim_end(session, mock, project):
    write_log(project, ("APPLICATION", "CarMaker"), ("ERROR", "", "old error of an earlier run"))
    erg = project / "SimOutput" / "host" / "20260101" / "Run_120000.erg"
    mock.polls_until_idle = 2
    session.start_sim()
    erg.parent.mkdir(parents=True)
    erg.write_bytes(b"x")
    mock.last_result = "SimOutput/host/20260101/Run_120000.erg"
    write_log(project, ("SIM_START", "Sub/Run1", "2026-01-01 12:00:00"), ("WARNING", "", "w"),
              ("ERROR", "", "boom"), ("ERROR", "", "boom"), ("SIM_END", "", "Sub/Run1", "5.055s", "76.1026m"))
    r = session.wait_end(timeout_s=5, poll_s=0.01)
    assert r["finished"] and r["sim_time_s"] == 5.055 and r["distance_m"] == 76.1026
    assert r["result_file"] == str(erg) and r["log_errors"] == ["ERROR boom (x2)"]
    assert erg.parent in session.result_dirs()

    t = session.log_tail(lines=2, level="warning")
    assert t["total"] == 4 and [ln.split(TAB)[0] for ln in t["lines"]] == ["ERROR", "ERROR"]
    assert session.log_tail(level="error")["total"] == 3
    with pytest.raises(BackendError, match="level must be"):
        session.log_tail(level="loud")


def test_old_result_file_is_not_reported(session, mock, project):
    erg = project / "SimOutput" / "old.erg"
    erg.parent.mkdir(parents=True, exist_ok=True)
    erg.write_bytes(b"x")
    mock.last_result = str(erg)
    session._run_started = time.time() + 60  # the file is older than the run
    assert session._last_result_file() is None


def test_wait_end_explains_missing_result(session, mock):
    mock.polls_until_idle = 2
    session.start_sim(save="collect")
    r = session.wait_end(timeout_s=5, poll_s=0.01)
    assert r["result_file"] is None and "collect only" in r["note"]


def test_wait_end_returns_snapshot_when_still_running(session, mock):
    mock.polls_until_idle = 10_000
    session.start_sim()
    r = session.wait_end(timeout_s=0.05, poll_s=0.01)
    assert r["finished"] is False and r["state"] == "running" and r["live"]["Car.v"] == 12.5
    assert "call again" in r["note"]


def test_wait_end_calls_back_while_waiting(session, mock, monkeypatch):
    mock.polls_until_idle = 10_000
    session.start_sim()
    clock = iter(range(0, 1000, 3))  # every look at the clock advances 3 s
    monkeypatch.setattr("carmaker_mcp.session.time.time", lambda: next(clock))
    monkeypatch.setattr("carmaker_mcp.session.time.sleep", lambda s: None)
    seen = []
    session.wait_end(timeout_s=30, poll_s=1, on_poll=lambda elapsed, live: seen.append((elapsed, live)))
    assert len(seen) >= 2 and seen[0][1] == {"Time": 3.0, "Car.v": 12.5}


def test_stop_waits_for_idle(session, mock):
    mock.polls_until_idle = 10_000
    session.start_sim()
    r = session.stop_sim(wait_s=2)
    assert r["stop_requested"] and r["finished"] and r["end_status"] == "aborted"
    mock.start_sim()
    assert session.stop_sim(wait_s=0) == {"stop_requested": True}


def test_failed_start_quotes_only_new_log_errors(session, mock, project):
    write_log(project, ("ERROR", "", "older"))

    def start():
        write_log(project, ("ERROR", "", "PowerTrain: cannot initialise"))

    mock.start_sim = start
    with pytest.raises(BackendError, match="Session log: ERROR PowerTrain: cannot initialise") as e:
        session.start_sim(start_timeout_s=0.05)
    assert "older" not in str(e.value)


def test_load_force_is_passed_on(session, mock):
    session.load_testrun("My Runs/Acceleration", force=True)
    assert ("load", "My Runs/Acceleration", True) in mock.calls


def test_state_changing_calls_are_serialised(session, mock):
    order = []
    real = mock.load_testrun

    def slow(name, force=False):
        order.append("in")
        time.sleep(0.05)
        order.append("out")
        return real(name, force)

    mock.load_testrun = slow
    threads = [threading.Thread(target=session.load_testrun, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert order == ["in", "out", "in", "out"]


def test_log_helpers(tmp_path):
    assert sessionlog.tail(tmp_path) == {"file": None, "lines": []}
    assert sessionlog.since(tmp_path, sessionlog.mark(tmp_path)) == []
    f = write_log(tmp_path, ("A", "1"))
    m = sessionlog.mark(tmp_path)
    write_log(tmp_path, ("B", "2"))
    assert sessionlog.since(tmp_path, m) == ["B" + TAB + "2"]
    # a newer log file (the application was restarted) is read from its beginning
    newer = f.with_name("host_20260101_130000.log")
    newer.write_bytes(("C" + TAB + "3" + CRLF).encode())
    assert sessionlog.since(tmp_path, m) == ["C" + TAB + "3"]
    assert sessionlog.sim_end(["SIM_END" + TAB + TAB + "My Runs/A B" + TAB + "1.5s" + TAB + "2m"]) == {
        "testrun": "My Runs/A B", "sim_time_s": 1.5, "distance_m": 2.0}
    assert sessionlog.sim_end(["TIME" + TAB + "0.0"]) is None


def test_gui_settings_are_put_back_when_the_server_exits(session, mock):
    mock.polls_until_idle = 10_000
    session.popup_timeout(5)
    session.start_sim()  # storage mode becomes 'save'
    assert mock.save_mode == "save" and mock.popup_timeout == 5
    session.at_exit()  # a run is going on: hands off
    assert mock.save_mode == "save"
    session.stop_sim(wait_s=2)
    session.at_exit()
    assert mock.save_mode == "collect" and mock.popup_timeout == -1
    tcl = len([c for c in mock.calls if c[0] == "tcl"])
    session.at_exit()  # nothing left to do, and nothing is sent
    assert len([c for c in mock.calls if c[0] == "tcl"]) == tcl
    mock.status = lambda: (_ for _ in ()).throw(RuntimeError("MATLAB is gone"))
    session.popup_timeout(3)
    session.at_exit()  # must not raise
