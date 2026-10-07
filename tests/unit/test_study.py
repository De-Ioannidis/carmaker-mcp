"""Parameter studies on the mock backend and a fake standalone manager."""

import threading

import pytest

from carmaker_mcp.backend import BackendError
from carmaker_mcp.study import StudyRunner, check_variations


class FakeStandalone:
    def __init__(self, never_ends=False):
        self.launched, self.closed, self.controls = [], [], []
        self.never_ends = never_ends
        self.stopped = False

    def launch(self, testrun, overrides=None, quantities=None, **kw):
        self.launched.append((testrun, overrides, quantities))
        return {"instance": f"sa{len(self.launched)}"}

    def wait_end(self, instance, timeout_s):
        if self.never_ends and not self.stopped:
            return {"finished": False}
        return {"finished": True, "sim_time_s": 4.0, "sim_dist_m": 50.0,
                "error_flag": 0 if not self.stopped else 1, "log_errors": []}

    def control(self, action, instance):
        self.controls.append((action, instance))
        self.stopped = True

    def results(self, instance):
        return {"result_files": []}

    def close(self, instance):
        self.closed.append(instance)


@pytest.fixture
def runner(session, mock):
    mock.polls_until_idle = 2
    return StudyRunner(session, lambda: pytest.fail("standalone manager not expected"))


def test_variations_are_validated():
    ok = check_variations([{"keys": {"Body.mass": 300, "Vehicle:Body.mass": 1}}, {"label": "b"}], "cm4sl")
    assert ok[0]["label"] == "run 1" and ok[1] == {"label": "b", "keys": {}, "workspace": {}}
    for bad, msg in (
        ([{"nope": 1}], "use only the fields"),
        ([{"keys": {"bad key; exit": 1}}], "invalid infofile key"),
        ([{"keys": [1]}], "must be objects"),
    ):
        with pytest.raises(BackendError, match=msg):
            check_variations(bad, "cm4sl")
    with pytest.raises(BackendError, match="no MATLAB workspace"):
        check_variations([{"workspace": {"Kp": 1}}], "standalone")


def test_cm4sl_study_runs_each_variation_and_restores(runner, session, mock):
    variations = [
        {"label": "soft", "keys": {"Body.mass": 250}, "workspace": {"Kp": 5.0}},
        {"label": "new var", "workspace": {"Brand_New": 1.0}},
        {"label": "plain"},
    ]
    r = runner.start("My Runs/Acceleration", variations, quantities=["Time"])
    assert r["runs"] == 3 and r["state"] == "running"
    st = runner.wait(r["study"])
    assert st["state"] == "done" and st["done"] == 3 and st["current_label"] is None
    assert [row["label"] for row in st["rows"]] == ["soft", "new var", "plain"]
    assert all(row["end_status"] == "completed" and "error" not in row for row in st["rows"])
    # values went back, the new variable is gone again, KeyValues were set and reset
    assert mock.workspace[("base", "Kp")] == 2.0 and ("base", "Brand_New") not in mock.workspace
    tcl = [c[1] for c in mock.calls if c[0] == "tcl"]
    assert "KeyValue set Body.mass 250" in tcl and tcl.count("KeyValue reset") >= 3
    assert [c for c in mock.calls if c[0] == "load"] == [("load", "My Runs/Acceleration")] * 3
    assert session.study_owner is None
    assert (session.store.dir / "studies" / f"{r['study']}.json").exists()
    assert runner.status()["id"] == r["study"]  # default: the latest


def test_study_blocks_other_state_changes_and_second_study(runner, session, mock):
    mock.polls_until_idle = 10_000
    r = runner.start("A", [{"label": "long"}], max_run_s=60)
    try:
        for _ in range(200):
            if session.study_owner is not None:
                break
            threading.Event().wait(0.01)
        with pytest.raises(BackendError, match="parameter study is running"):
            session.workspace_set("Kp", 9.0)
        with pytest.raises(BackendError, match="still running"):
            runner.start("B", [{}])
    finally:
        c = runner.cancel(r["study"])
    assert c["state"] == "cancelled"
    st = runner.status(r["study"])
    assert st["rows"][0]["stopped"] == "cancelled" and st["rows"][0]["end_status"] == "aborted"
    assert runner.cancel(r["study"])["note"] == "not running"
    session.workspace_set("Kp", 9.0)  # possible again


def test_run_that_takes_too_long_is_stopped(runner, mock):
    mock.polls_until_idle = 10_000
    st = runner.wait(runner.start("A", [{"label": "slow"}, {"label": "also slow"}], max_run_s=0.2)["study"])
    assert st["state"] == "done" and len(st["rows"]) == 2
    assert all(row["stopped"].startswith("max_run_s") for row in st["rows"])


def test_failing_run_is_reported_and_the_study_goes_on(runner, session, mock):
    real_load, n = mock.load_testrun, [0]

    def load(name, force=False):
        n[0] += 1
        if n[0] == 1:
            raise BackendError("test run 'A' not loaded: CarMaker could not load it")
        return real_load(name, force)

    mock.load_testrun = load
    st = runner.wait(runner.start("A", [{"workspace": {"Kp": 7.0}}, {}])["study"])
    assert "not loaded" in st["rows"][0]["error"] and st["rows"][1]["end_status"] == "completed"
    assert mock.workspace[("base", "Kp")] == 2.0


def test_standalone_study(session, mock):
    sa = FakeStandalone()
    runner = StudyRunner(session, lambda: sa)
    st = runner.wait(runner.start("A", [{"keys": {"Body.mass": 300}}, {}], quantities=["Car.ax"],
                                  mode="standalone")["study"])
    assert st["state"] == "done" and [r["end_status"] for r in st["rows"]] == ["completed"] * 2
    assert st["rows"][0]["sim_time_s"] == 4.0 and st["rows"][0]["distance_m"] == 50.0
    assert sa.launched[0][1] == {"Body.mass": 300} and "Car.ax" in sa.launched[0][2]
    assert sa.launched[1][1] is None and sa.closed == ["sa1", "sa2"]
    assert not [c for c in mock.calls if c[0] == "load"]  # the MATLAB session is not touched


def test_standalone_study_cancel(session):
    sa = FakeStandalone(never_ends=True)
    runner = StudyRunner(session, lambda: sa)
    runner.start("A", [{}, {}], mode="standalone")
    for _ in range(200):
        if sa.launched:
            break
        threading.Event().wait(0.01)
    assert runner.cancel()["state"] == "cancelled"
    st = runner.status()
    assert sa.controls == [("stop", "sa1")] and st["rows"][0]["end_status"] == "aborted" and st["done"] == 1


def test_errors(runner):
    with pytest.raises(BackendError, match="no study"):
        runner.status()
    with pytest.raises(BackendError, match="mode must be"):
        runner.start("A", [{}], mode="cloud")
    runner.wait(runner.start("A", [{}])["study"])
    with pytest.raises(BackendError, match="unknown study"):
        runner.status("nope")
