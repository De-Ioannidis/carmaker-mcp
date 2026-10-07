"""Project init script at session start, and Simulink errors reaching the agent."""

import time

import pytest
from test_backend_cm4sl import backend  # pytest puts tests/unit on the import path
from test_setup import FakeMatlab

from carmaker_mcp.backend import IDLE, BackendError, BackendTimeout, Status, sim_status_text
from carmaker_mcp.config import Config
from carmaker_mcp.launcher import Launcher, find_init_scripts


@pytest.fixture
def cfg(project, tmp_path):
    src = project / "src_cm4sl"
    src.mkdir()
    (src / "cmenv.m").write_text("% ipg")
    (src / "Project_Init.m").write_text("% adds paths, opens the model and the GUI")
    return Config(project=project, state_dir=tmp_path / "state")


class InitMatlab(FakeMatlab):
    """MATLAB stand-in that knows the marker variable and a project init script."""

    def __init__(self, slow=False, **kw):
        super().__init__(**kw)
        self.marker, self.slow = False, slow

    def matlab(self, code, nargout=1, timeout=None):
        if code == "exist('cm_mcp_init', 'var')":
            self.code.append(code)
            return 1.0 if self.marker else 0.0
        if code.startswith("cm_mcp_init = "):
            self.code.append(code)
            self.marker = True  # the marker is set before the script runs
            self.loaded.add("MyModel")
            self.gui_started = True
            if self.slow:
                raise BackendTimeout("MATLAB did not answer eval within 30s")
            return None
        return super().matlab(code, nargout, timeout)


def test_init_script_runs_once_per_matlab_session(cfg):
    launcher = Launcher(find=lambda: ["cm_mcp"])
    be = InitMatlab(on_path=True)
    r = launcher.ensure(be, cfg, model="MyModel", init="Project_Init.m")
    assert r["ready"] and r["steps"]["init"] == "ran Project_Init.m"
    assert r["steps"]["model"] == "MyModel already loaded" and r["steps"]["gui"] == "already open"
    run = [c for c in be.code if c.startswith("cm_mcp_init = ")]
    assert run == [f"cm_mcp_init = '{cfg.project / 'src_cm4sl' / 'Project_Init.m'}'; run(cm_mcp_init);"]

    r = launcher.ensure(be, cfg, model="MyModel", init="Project_Init.m")
    assert r["steps"]["init"] == "Project_Init.m already run in this MATLAB session"
    assert len([c for c in be.code if c.startswith("cm_mcp_init = ")]) == 1


def test_init_from_config_and_slow_script(cfg):
    cfg.matlab_init = "Project_Init.m"
    launcher = Launcher(find=lambda: ["cm_mcp"])
    be = InitMatlab(on_path=True, slow=True)
    r = launcher.ensure(be, cfg)
    assert r["ready"] is False and r["waiting_for"] == "MATLAB to finish Project_Init.m"
    be.slow = False
    r = launcher.ensure(be, cfg)  # not started a second time
    assert r["ready"] and len([c for c in be.code if c.startswith("cm_mcp_init = ")]) == 1


def test_without_init_the_candidates_are_listed(cfg):
    assert find_init_scripts(cfg.project / "src_cm4sl") == ["Project_Init.m"]
    be = InitMatlab(on_path=True)
    be.gui_started = True
    r = Launcher(find=lambda: ["cm_mcp"]).ensure(be, cfg)
    assert r["init_scripts_found"] == ["Project_Init.m"] and "may not compile" in r["init_note"]
    with pytest.raises(BackendError, match="init script not found"):
        Launcher(find=lambda: ["cm_mcp"]).ensure(be, cfg, init="Nope.m")


def test_config_reads_the_init_setting(monkeypatch):
    monkeypatch.setenv("CM_MATLAB_INIT", "Project_Init.m")
    assert Config.from_env().matlab_init == "Project_Init.m"


# ---- Simulink errors -----------------------------------------------------------------------
def test_backend_reads_and_clears_the_last_simulink_error():
    answers = {"jsonencode(sllasterror)": '{"Type":"error","MessageID":"x","Message":"Invalid Simulink '
                                          'object name: \'Integrator/State\'.","Handle":[]}'}
    b = backend(eval=lambda code: answers.get(code))
    assert b.simulink_error() == "Invalid Simulink object name: 'Integrator/State'."
    assert b.simulink_error(clear=True) is None and b._eng.calls[-1][1] == ("sllasterror([]);",)
    assert backend(eval=lambda code: "[]").simulink_error() is None
    two = '[{"Message":"first"},{"Message":"second"}]'
    assert backend(eval=lambda code: two).simulink_error() == "first | second"
    assert backend(eval=lambda code: RuntimeError("busy")).simulink_error() is None


def test_diagnostic_log_gives_the_real_error_not_a_handled_one(tmp_path):
    """sllasterror keeps an error a block raised and handled; the Diagnostic Viewer log does not."""
    handled = '{"Message":"Invalid Simulink object name: \'Integrator/State\'."}'
    b = backend(eval=lambda code: handled if code == "jsonencode(sllasterror)" else None)
    b.diag_file = tmp_path / "state" / "it's" / "diag.txt"
    assert b.simulink_error(clear=True) is None
    f = str(b.diag_file).replace("'", "''")
    assert [c[1][0] for c in b._eng.calls] == [
        f"sldiagviewer.diary('off'); if isfile('{f}'), delete('{f}'); end; sldiagviewer.diary('{f}', 'UTF-8');",
        "sllasterror([]);",
    ]
    calls = len(b._eng.calls)
    assert b.simulink_error(certain=True) is None  # nothing logged yet, and MATLAB is not asked
    assert len(b._eng.calls) == calls
    assert b.simulink_error() == "Invalid Simulink object name: 'Integrator/State'."  # last resort

    b.diag_file.write_text(
        "\r\nWarning: a block is not connected\r\nmore about the warning\r\n\r\n"
        "Error: Invalid setting in 'M/Constant19' for parameter 'Value'.\r\nsecond line\r\n\r\n"
        "an information\r\nError: Invalid setting in 'M/Constant19' for parameter 'Value'.\r\nsecond line\r\n"
        "Error: another one\r\n", encoding="utf-8", newline="")
    real = "Invalid setting in 'M/Constant19' for parameter 'Value'. second line | another one"
    assert b.simulink_error(certain=True) == real and b.simulink_error() == real

    b.simulink_capture_stop()
    b.simulink_capture_stop()  # switched off once
    assert [c[1][0] for c in b._eng.calls].count("sldiagviewer.diary('off');") == 1
    assert b.simulink_error() == real  # still readable after the run


def test_without_a_diagnostic_log_the_last_error_is_used(tmp_path):
    def matlab(code):
        if code.startswith("sldiagviewer"):
            return RuntimeError("Undefined function")
        return '{"Message":"Algebraic loop"}' if code == "jsonencode(sllasterror)" else None

    b = backend(eval=matlab)
    b.diag_file = tmp_path / "diag.txt"
    b.simulink_error(clear=True)
    assert b.simulink_error(certain=True) == "Algebraic loop"
    b.simulink_capture_stop()
    assert all("diary('off');" != c[1][0] for c in b._eng.calls)


def test_session_gives_the_backend_a_place_for_the_log(tmp_path):
    from carmaker_mcp.session import Session

    b = backend()
    Session(b, Config(project=tmp_path, state_dir=tmp_path / "state"))
    assert b.diag_file == tmp_path / "state" / "simulink-diagnostics.txt"


class Errors:
    """Adds Simulink's last-error memory to the mock backend."""

    def __init__(self, mock):
        self.message = "stale error of an earlier run"
        mock.simulink_error = self
        mock.model_stopped = lambda model: self.stopped
        mock.simulink_capture_stop = self.stop
        self.stopped = True
        self.capture_stops = 0

    def __call__(self, clear=False, certain=False):
        if clear:
            self.message = None
        return self.message

    def stop(self):
        self.capture_stops += 1


def test_start_fails_at_once_with_the_simulink_error(session, mock):
    errors = Errors(mock)

    def compile_fails():
        errors.message = "Invalid Simulink object name: 'Integrator/State'."

    mock.start_sim = compile_fails  # the state never leaves idle
    t0 = time.time()
    with pytest.raises(BackendError) as e:
        session.start_sim(start_timeout_s=60)
    assert time.time() - t0 < 10, "must not wait for the start time-out"
    msg = str(e.value)
    assert msg.startswith("the simulation did not start. Simulink: Invalid Simulink object name")
    assert "stale" not in msg and "pop-up" not in msg
    assert errors.capture_stops == 1


def test_error_while_the_model_is_still_initialising_is_not_a_failure(session, mock):
    """Blocks may raise and handle errors during initialisation of a model that starts fine."""
    errors = Errors(mock)
    errors.stopped = False  # Simulink is compiling
    polls = [0]
    real_status = mock.status

    def status():
        polls[0] += 1
        if polls[0] == 6:
            mock.sim_status = 3  # ... and then the run begins
        return real_status()

    def start():
        errors.message = "Invalid Simulink object name: 'Integrator/State'."

    mock.start_sim, mock.status = start, status
    mock.polls_until_idle = 10_000
    assert session.start_sim(start_timeout_s=30)["started"] is True


def test_failed_run_reports_the_simulink_error(session, mock):
    errors = Errors(mock)
    mock.polls_until_idle = 2
    session.start_sim()
    assert errors.message is None  # cleared at the start
    errors.message = "Algebraic loop"
    r = session.wait_end(timeout_s=5, poll_s=0.01)
    assert r["end_status"] == "completed" and "simulink_error" not in r
    assert errors.capture_stops == 1  # the log is switched off when the run is over

    mock.polls_until_idle = 10_000
    session.start_sim()
    errors.message = "Derivative is not finite"
    r = session.stop_sim(wait_s=2)
    assert r["end_status"] == "aborted" and "simulink_error" not in r  # stopped on request, not failed

    mock.polls_until_idle = 2
    session.start_sim()
    errors.message = "Derivative is not finite"
    real_status = mock.status

    def failed_status():
        st = real_status()
        if st.idle:
            st.end_status = "failed"
        return st

    mock.status = failed_status
    r = session.wait_end(timeout_s=5, poll_s=0.01)
    assert r["end_status"] == "failed" and r["simulink_error"] == "Derivative is not finite"


# ---- the phases of a start -------------------------------------------------------------------
INIT = -11  # 'simulink initialization'


def scripted(mock, steps):
    """Make the mock report these (sim_status, end_status) pairs, the last one for ever."""
    steps = list(steps)

    def status():
        code, end = steps.pop(0) if len(steps) > 1 else steps[0]
        return Status(connected=True, sim_status=code, sim_status_text=sim_status_text(code),
                      end_status=end, active_model="mdl")

    mock.status = status
    mock.start_sim = lambda: None


def test_compile_failure_after_the_initialisation_phase_fails_the_start(session, mock):
    """Seen live: idle, 'simulink initialization', then idle again with end status 'failed'."""
    errors = Errors(mock)
    session.require_idle = lambda action: None
    scripted(mock, [(IDLE, "completed"), (INIT, ""), (INIT, ""), (IDLE, "failed")])
    errors_at_start = "Invalid setting in 'M/Constant19' for parameter 'Value'."
    mock.start_sim = lambda: setattr(errors, "message", errors_at_start)
    with pytest.raises(BackendError) as e:
        session.start_sim(start_timeout_s=30)
    assert str(e.value).startswith("the simulation did not start. Simulink: Invalid setting in 'M/Constant19'")
    assert errors.capture_stops == 1


def test_start_returns_when_the_run_is_running_not_when_it_is_being_prepared(session, mock):
    Errors(mock)
    session.require_idle = lambda action: None
    scripted(mock, [(IDLE, "completed"), (INIT, ""), (INIT, ""), (0, "")])
    r = session.start_sim(start_timeout_s=30)
    assert r["started"] is True and r["state"] == "running" and "note" not in r


def test_start_still_preparing_at_the_time_limit_is_not_an_error(session, mock):
    errors = Errors(mock)
    session.require_idle = lambda action: None
    scripted(mock, [(IDLE, "completed"), (INIT, "")])
    r = session.start_sim(start_timeout_s=2)
    assert r["started"] is True and r["state"] == "simulink initialization"
    assert "cm_wait_end" in r["note"] and errors.capture_stops == 0  # the capture goes on


def test_run_that_is_over_between_two_looks_counts_as_started(session, mock):
    Errors(mock)
    session.require_idle = lambda action: None
    scripted(mock, [(INIT, ""), (IDLE, "completed")])
    r = session.start_sim(start_timeout_s=30)
    assert r["started"] is True and r["state"] == "idle"
