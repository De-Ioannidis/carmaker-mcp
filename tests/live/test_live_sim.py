"""Live simulation tests: run control in the MATLAB-connected (CM4SL) session.

These START SIMULATIONS in the open CarMaker GUI and switch its storage mode to "Save all" (put back
at the end), so they are opt-in:

    CM_LIVE=1 CM_LIVE_SIM=1 CM_TESTRUN="Examples/VehicleDynamics/Handling/Racetrack_Hockenheim" \
        pytest -m live tests/live/test_live_sim.py -s

CM_TESTRUN is relative to Data/TestRun. Use a run that takes at least 20 s of wall time, otherwise
the "during the run" checks have nothing to observe. Every test prints what it saw, so the output can
be copied into docs/verification.md.
"""

import os
import time
from pathlib import Path

import pytest

from carmaker_mcp.backend_cm4sl import Cm4slBackend
from carmaker_mcp.config import Config
from carmaker_mcp.session import Session

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CM_LIVE") != "1" or os.environ.get("CM_LIVE_SIM") != "1",
        reason="set CM_LIVE=1 and CM_LIVE_SIM=1 (this starts simulations)",
    ),
]


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    cfg = Config.from_env()
    cfg.state_dir = tmp_path_factory.mktemp("state")
    s = Session(Cm4slBackend(cfg.matlab_session, cfg.engine_timeout), cfg)
    assert s.status()["sim_status"] == "idle", "the simulation must be idle before the live tests"
    yield s
    if not s.backend.status().idle:
        s.stop_sim(wait_s=60)
    print("\nrevert ->", s.revert_all())


def wait_until_running(session, timeout_s=180):
    """Poll until the simulation is actually running (past compile / preprocessing)."""
    t0 = time.time()
    states = []
    while time.time() - t0 < timeout_s:
        st = session.backend.status()
        if st.sim_status_text not in states:
            states.append(st.sim_status_text)
        if st.connected and st.sim_status is not None and st.sim_status >= 0:
            return states
        if st.idle and len(states) > 1:
            break
        time.sleep(0.3)
    pytest.fail(f"simulation never reached 'running' (states seen: {states})")


def test_run_monitoring_and_result(session):
    testrun = os.environ["CM_TESTRUN"]
    print("\nstatus before:", session.status())
    print("load  ->", session.load_testrun(testrun))
    start = session.start_sim(start_timeout_s=180)
    print("start ->", start)
    assert start["save_mode"] == "save"

    # while it runs: does MATLAB / the GUI still answer, and do live quantities move?
    states, samples, answered, failed = [], [], 0, 0
    t0 = time.time()
    while time.time() - t0 < 900:
        try:
            st = session.backend.status()
            answered += 1
            if st.sim_status_text not in states:
                states.append(st.sim_status_text)
            if st.idle:
                break
            if st.sim_status is not None and st.sim_status >= 0:
                samples.append(session.backend.quantity_read(["Time", "Car.v"]))
        except Exception as e:  # BackendError while MATLAB is busy
            failed += 1
            print("poll failed:", e)
        time.sleep(0.5)
    print("states seen:", states)
    print(f"polls answered={answered} failed={failed}; live samples while running={len(samples)}")
    if samples:
        print("first / last live sample:", samples[0], samples[-1])

    end = session.wait_end(timeout_s=60)
    print("end ->", end)
    assert end["finished"]
    assert end["result_file"], "no result file although the storage mode was 'save'"
    summary = session.erg(end["result_file"]).summary(["Time", "Car.v"])
    print("summary:", summary)
    assert summary["duration_s"] and summary["duration_s"] > 0
    assert Path(end["result_file"]).parent in session.result_dirs()
    assert samples, "the run ended before a live sample could be taken: use a longer CM_TESTRUN"
    times = [s["Time"] for s in samples if s.get("Time") is not None]
    assert times and times[-1] > times[0], "live Time did not advance during the run"


def test_bounded_wait_then_stop(session):
    session.load_testrun(os.environ["CM_TESTRUN"])
    session.start_sim(start_timeout_s=180)
    print("\nstates until running:", wait_until_running(session))
    waiting = session.wait_end(timeout_s=1, poll_s=0.3)  # short: a run can be much faster than real time
    print("bounded wait ->", waiting)
    assert waiting["finished"] is False and waiting["live"].get("Time")
    t0 = time.time()
    stopped = session.stop_sim(wait_s=60)
    print(f"stop -> {stopped} after {time.time() - t0:.1f}s")
    assert stopped["finished"]


def test_dva_write_during_run(session):
    quantity = os.environ.get("CM_DVA_QUANTITY", "DM.Gas")
    session.load_testrun(os.environ["CM_TESTRUN"])
    session.start_sim(start_timeout_s=180)
    wait_until_running(session)
    before = session.backend.quantity_read([quantity])
    session.backend.dva_write(quantity, 0.0, -1, "Abs")  # until released: a run can outpace real time
    during = session.backend.quantity_read([quantity])
    session.backend.dva_release()
    print(f"\n{quantity}: before {before}, during DVA {during}")
    print("stop ->", session.stop_sim(wait_s=60))
    assert during[quantity] == pytest.approx(0.0, abs=1e-6)


def test_three_cycles_without_hang(session):
    testrun = os.environ["CM_TESTRUN"]
    for i in range(3):
        t0 = time.time()
        session.load_testrun(testrun)
        start = session.start_sim(start_timeout_s=180)
        wait_until_running(session)
        end = session.stop_sim(wait_s=60)
        print(f"\ncycle {i + 1}: start after {start['after_s']}s, whole cycle {time.time() - t0:.1f}s, end {end}")
        assert end["finished"]
        assert session.status()["sim_status"] == "idle"
