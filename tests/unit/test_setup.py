"""Setup check (doctor), client configuration and cold start, without CarMaker or MATLAB."""

import json
import sys

import pytest

from carmaker_mcp import doctor, server
from carmaker_mcp.backend import BackendError, Status
from carmaker_mcp.config import Config
from carmaker_mcp.launcher import Launcher, startup_code

PY = f"{sys.version_info.major}.{sys.version_info.minor}"


@pytest.fixture
def cm_home(tmp_path):
    home = tmp_path / "carmaker" / "win64-14.1.1"
    for v in ("3.9", PY):
        (home / "Python" / f"python{v}").mkdir(parents=True)
    for r in ("R2024a", "R2024b"):
        (home / "Matlab" / r).mkdir(parents=True)
    return home


@pytest.fixture
def cfg(project, tmp_path, cm_home):
    return Config(project=project, state_dir=tmp_path / "state", cm_home=cm_home)


INSTALLS = {"24.2": "C:/MATLAB/R2024b", "26.1": "C:/MATLAB/R2026a"}


def checks(rep):
    return {c.name: c for c in rep.checks}


def test_release_name():
    assert doctor.release_name("24.2") == "R2024b" and doctor.release_name("23.1") == "R2023a"


def test_healthy_machine(cfg):
    rep = doctor.diagnose(cfg, installs=INSTALLS, sessions=["cm_mcp"], engine="24.2.2",
                          gui_status=lambda: Status(connected=True, sim_status=-2, sim_status_text="idle"))
    assert rep.ok and all(c.ok for c in rep.checks)
    # the newest MATLAB is not the one CarMaker supports
    assert rep.matlab_release == "R2024b" and rep.engine_pin == "matlabengine==24.2.*"
    assert "R2026a" in checks(rep)["MATLAB"].detail
    assert doctor.uvx_args(rep) == ["--python", PY, "--with", "matlabengine==24.2.*", "carmaker-mcp"]
    assert "All checks passed" in doctor.format_report(rep)


def test_problems_come_with_fixes(cfg, tmp_path):
    cfg.project = tmp_path / "not-a-project"
    rep = doctor.diagnose(cfg, installs=INSTALLS, sessions=[], engine="26.1.0")
    c = checks(rep)
    assert not rep.ok
    assert c["matlabengine"].ok is False and "matlabengine==24.2.*" in c["matlabengine"].fix
    assert c["shared MATLAB session"].ok is False and "shareEngine('cm_mcp')" in c["shared MATLAB session"].fix
    assert c["project folder"].ok is False
    assert "CarMaker GUI" not in c  # not probed without a session
    text = doctor.format_report(rep)
    assert "FAIL" in text and "fix:" in text


def test_missing_engine_and_unsupported_matlab(cfg):
    rep = doctor.diagnose(cfg, installs={"26.1": "C:/MATLAB/R2026a"}, sessions=None, engine=None)
    c = checks(rep)
    assert c["MATLAB"].ok is False and "R2024b" in c["MATLAB"].detail
    assert "matlabengine" not in c and rep.engine_pin is None
    assert doctor.uvx_args(rep) == ["--python", PY, "carmaker-mcp"]

    rep = doctor.diagnose(cfg, installs=INSTALLS, sessions=None, engine=None)
    assert checks(rep)["matlabengine"].ok is False


def test_wrong_python_and_no_matlab(cfg, cm_home, monkeypatch):
    monkeypatch.setattr(doctor, "python_version", lambda: "3.99")
    rep = doctor.diagnose(cfg, installs={}, sessions=None, engine=None)
    c = checks(rep)
    assert c["Python version"].ok is False and f"--python {PY}" in c["Python version"].fix
    assert c["MATLAB"].ok is None  # fine for standalone use


def test_missing_carmaker(tmp_path, project):
    cfg = Config(project=project, state_dir=tmp_path / "s", cm_home=tmp_path / "nope")
    rep = doctor.diagnose(cfg, installs={}, sessions=None, engine=None)
    assert checks(rep)["CarMaker install"].ok is False and not rep.ok


def test_client_configs(cfg):
    rep = doctor.diagnose(cfg, installs=INSTALLS, sessions=["cm_mcp"], engine="24.2.2")
    cc = doctor.client_config("claude-code", rep, cfg)
    assert cc.startswith("claude mcp add carmaker --env CM_PROJECT=") and '--with "matlabengine==24.2.*"' in cc
    vs = json.loads(doctor.client_config("vscode", rep, cfg))
    assert vs["servers"]["carmaker"]["type"] == "stdio"
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+

    cx = tomllib.loads(doctor.client_config("codex", rep, cfg))["mcp_servers"]["carmaker"]
    assert cx["command"] == "uvx" and cx["args"][-1] == "carmaker-mcp" and cx["env_vars"] == ["WINDIR"]
    assert cx["env"]["CM_PROJECT"] == str(cfg.project)
    for client in ("claude-desktop", "cursor", "antigravity"):
        entry = json.loads(doctor.client_config(client, rep, cfg))["mcpServers"]["carmaker"]
        assert entry["command"] == "uvx" and entry["env"]["CM_PROJECT"] == str(cfg.project)
    with pytest.raises(ValueError, match="unknown client"):
        doctor.client_config("emacs", rep, cfg)


def test_cli_doctor_and_config(monkeypatch, capsys, cfg):
    monkeypatch.setattr(server.Config, "from_env", classmethod(lambda cls: cfg))
    monkeypatch.setattr(doctor, "matlab_installs", lambda: INSTALLS)
    monkeypatch.setattr(doctor, "shared_sessions", lambda: None)
    monkeypatch.setattr(doctor, "engine_version", lambda: "24.2.2")
    with pytest.raises(SystemExit) as e:
        server.main(["doctor", "--json"])
    assert e.value.code == 0 and json.loads(capsys.readouterr().out)["matlab_release"] == "R2024b"
    with pytest.raises(SystemExit) as e:
        server.main(["config", "--client", "cursor"])
    assert e.value.code == 0 and "mcpServers" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        server.main(["config", "--client", "emacs"])
    assert e.value.code == 2


# ---- cold start --------------------------------------------------------------------
class FakeMatlab:
    """Backend stand-in: what the launcher asks MATLAB and the GUI."""

    def __init__(self, on_path=False, loaded=(), gui_after=0):
        self.on_path, self.loaded, self.gui_after = on_path, set(loaded), gui_after
        self.code, self.gui_polls, self.gui_started = [], 0, False

    def matlab(self, code, nargout=1, timeout=None):
        self.code.append(code)
        if code == "exist('cmguicmd')":
            return 2.0 if self.on_path else 0.0
        if code.startswith("bdIsLoaded("):
            return code[len("bdIsLoaded('"):-2] in self.loaded
        if code.startswith("find_system("):
            return sorted(self.loaded)
        if code.endswith("cmenv"):
            self.on_path = True
        if code == "CM_Simulink":
            self.gui_started = True
        return None

    def gui_tcl(self, command, timeout_ms=10000):
        if not self.gui_started:
            return -2, "no connection"
        self.gui_polls += 1
        return (0, "-2") if self.gui_polls > self.gui_after else (-2, "no connection")


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make_launcher(sessions_after=0, never=False):
    clock, started, polls = Clock(), [], [0]

    def find():
        polls[0] += 1
        if never or not started:
            return []
        return ["cm_mcp"] if polls[0] > sessions_after else []

    def start(exe, workdir, session):
        started.append((exe, workdir, session))
        return 4242

    return Launcher(find=find, start=start, sleep=clock.sleep, clock=clock.now), started, clock


@pytest.fixture
def start_cfg(cfg, tmp_path):
    exe = tmp_path / "matlab.exe"
    exe.write_bytes(b"")
    (cfg.project / "src_cm4sl").mkdir()
    cfg.matlab_exe = exe
    return cfg


def test_cold_start_does_every_step(start_cfg):
    launcher, started, _ = make_launcher(sessions_after=3)
    be = FakeMatlab(gui_after=2)
    r = launcher.ensure(be, start_cfg, model="models/MyModel.slx", max_wait_s=60)
    assert r["ready"] is True
    assert r["steps"]["matlab"] == "started (pid 4242)" and started[0][1] == start_cfg.project / "src_cm4sl"
    assert r["steps"]["cmenv"].startswith("ran cmenv") and r["steps"]["model"] == "opened MyModel"
    assert r["steps"]["gui"] == "opened with CM_Simulink"
    assert "open_system('models/MyModel.slx')" in be.code and "CM_Simulink" in be.code


def test_existing_session_is_left_alone(start_cfg):
    launcher = Launcher(find=lambda: ["cm_mcp"], start=lambda *a: pytest.fail("must not start MATLAB"))
    be = FakeMatlab(on_path=True, loaded={"MyModel"})
    be.gui_started = True
    r = launcher.ensure(be, start_cfg, model="MyModel")
    assert r == {"ready": True, "steps": {"matlab": "shared session found", "cmenv": "already set up",
                                          "model": "MyModel already loaded", "gui": "already open"}}
    assert "CM_Simulink" not in be.code and not any(c.startswith("open_system") for c in be.code)


def test_without_a_model_the_candidates_are_listed(start_cfg):
    import os

    src = start_cfg.project / "src_cm4sl"
    (src / "Controls").mkdir()
    (src / "slprj" / "cache").mkdir(parents=True)
    for rel, age in (("generic.mdl", 300), ("Controls/My Controller.slx", 100), ("slprj/cache/x.slx", 1)):
        f = src / rel
        f.write_bytes(b"")
        os.utime(f, (f.stat().st_atime, f.stat().st_mtime - age))
    launcher = Launcher(find=lambda: ["cm_mcp"])
    be = FakeMatlab(on_path=True)
    be.gui_started = True
    r = launcher.ensure(be, start_cfg)
    assert r["ready"] and r["steps"]["model"] == "none requested"
    assert r["models_found"] == ["Controls/My Controller.slx", "generic.mdl"]  # newest first, no build cache
    assert r["models_loaded"] == [] and "ask the user" in r["model_note"]
    assert not any(c.startswith("open_system") for c in be.code)

    # a path from models_found is resolved against the start folder
    r = launcher.ensure(be, start_cfg, model="Controls/My Controller.slx")
    assert r["steps"]["model"] == "opened My Controller" and "models_found" not in r
    assert f"open_system('{src / 'Controls' / 'My Controller.slx'}')" in be.code

    # a model that is already open needs no question
    loaded = FakeMatlab(on_path=True, loaded={"MyModel"})
    loaded.gui_started = True
    r = launcher.ensure(loaded, start_cfg)
    assert r["models_loaded"] == ["MyModel"] and "model_note" not in r


def test_slow_matlab_is_waited_for_not_started_twice(start_cfg):
    launcher, started, clock = make_launcher(never=True)
    be = FakeMatlab()
    r = launcher.ensure(be, start_cfg, max_wait_s=10)
    assert r["ready"] is False and "MATLAB" in r["waiting_for"] and "call again" in r["note"]
    r = launcher.ensure(be, start_cfg, max_wait_s=10)
    assert r["steps"]["matlab"] == "starting" and len(started) == 1
    clock.t += 3600  # that MATLAB evidently never came up
    launcher.ensure(be, start_cfg, max_wait_s=10)
    assert len(started) == 2


def test_gui_that_does_not_connect_in_time(start_cfg):
    launcher = Launcher(find=lambda: ["cm_mcp"], sleep=lambda s: None)
    be = FakeMatlab(on_path=True, gui_after=10**9)
    r = launcher.ensure(be, start_cfg, max_wait_s=0.05)
    assert r["ready"] is False and "GUI" in r["waiting_for"]


def test_cold_start_errors(cfg, tmp_path):
    launcher, _, _ = make_launcher(never=True)
    with pytest.raises(BackendError, match="start folder does not exist"):
        launcher.ensure(FakeMatlab(), cfg)
    cfg.project = None
    with pytest.raises(BackendError, match="CM_PROJECT"):
        launcher.ensure(FakeMatlab(), cfg)
    with pytest.raises(BackendError, match="matlabengine is not installed"):
        Launcher(find=lambda: None).ensure(FakeMatlab(), cfg)
    cfg.matlab_exe = tmp_path / "missing.exe"
    cfg.matlab_dir = tmp_path
    with pytest.raises(BackendError, match="CM_MATLAB_EXE does not exist"):
        launcher.ensure(FakeMatlab(), cfg)


def test_model_that_opens_slowly_is_waited_for(start_cfg):
    from carmaker_mcp.backend import BackendTimeout

    class Slow(FakeMatlab):
        def matlab(self, code, nargout=1, timeout=None):
            if code.startswith("open_system("):
                self.loaded.add("Big")  # MATLAB keeps loading after the call was given up
                raise BackendTimeout("MATLAB did not answer eval within 30s")
            return super().matlab(code, nargout, timeout)

    launcher = Launcher(find=lambda: ["cm_mcp"])
    be = Slow(on_path=True)
    r = launcher.ensure(be, start_cfg, model="Big")
    assert r["ready"] is False and r["waiting_for"] == "MATLAB to finish opening Big"
    r = launcher.ensure(be, start_cfg, model="Big")
    assert r["ready"] is True and r["steps"]["model"] == "Big already loaded"


def test_startup_code_quotes_paths(tmp_path):
    code = startup_code(tmp_path / "it's here", "cm_mcp")
    assert "it''s here" in code and code.endswith("matlab.engine.shareEngine('cm_mcp');")


def test_session_start_and_doctor_tools_with_mock(session):
    assert session.session_start()["ready"] is True  # the mock has no MATLAB to start
    d = session.doctor()
    assert "checks" in d and d["uvx_args"][-1] == "carmaker-mcp"
