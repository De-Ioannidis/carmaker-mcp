"""StandaloneManager against a fake ``cmapi`` (IPG's Python API is not available in CI)."""

import asyncio
import types
from pathlib import Path

import pytest

from carmaker_mcp import standalone
from carmaker_mcp.backend import BackendError
from carmaker_mcp.standalone import StandaloneManager


def make_cmapi():
    """A minimal stand-in for the parts of cmapi the manager uses. ``state`` records what happened."""
    state = types.SimpleNamespace(controls=[], overrides={}, quantities=None, rtf=None, exe=None,
                                  sims=[], dva=[], disconnected=[], attached_pid=None, project=None)

    class TestRun:
        def set_parameter_value(self, key, value):
            state.overrides[key] = value

    class ProjectObj:
        def load_testrun_parametrization(self, path):
            state.testrun = path
            return TestRun()

    class Project:
        @staticmethod
        def load(path):
            state.project = path

        @staticmethod
        def instance():
            return ProjectObj()

    class Variation:
        @staticmethod
        def create_from_testrun(tr):
            return Variation()

        def set_storage_mode(self, mode):
            state.storage = mode

        def set_outputquantities(self, oq):
            state.quantities = oq.names

        def set_initial_realtimefactor(self, f):
            state.rtf = f

        def get_simend_info(self):
            return types.SimpleNamespace(sim_time=12.5, sim_dist=100.0, error_flag=0, user_stop=False)

        def get_result_file_paths(self):
            return [Path("SimOutput/host/run.erg")]

    class OutputQuantities:
        def add_quantities(self, names):
            self.names = list(names)

    class CarMaker:
        def set_executable_path(self, exe):
            state.exe = exe

        def get_pid(self):
            return 4711

    class ApoServerInfo:
        def __init__(self, pid=None):
            self.pid_arg = pid

    class ApoServer:
        def set_sinfo(self, info):
            state.attached_pid = info.pid_arg

        def set_host(self, host):
            state.host = host

    class Condition:
        def __init__(self, event):
            self.event = event

        async def wait(self):
            await self.event.wait()

    class SimIO:
        async def dva_read_async(self, *names):
            return [1.0 + i for i in range(len(names))]

        def dva_write_absolute_value(self, name, value, duration):
            state.dva.append((name, value, duration))

    class SessionLog:
        def get_entries_with_pattern(self, pattern):
            return ["Entry 1 Message: ERROR something broke"]

    class SimControlInteractive:
        def __init__(self):
            self.finished = asyncio.Event()
            self.time_reached = asyncio.Event()
            self.simio, self.sessionlog = SimIO(), SessionLog()
            state.sims.append(self)

        @staticmethod
        async def create_with_master(master):
            return SimControlInteractive()

        def set_variation(self, var):
            self.var = var

        async def set_master(self, master):
            self.master = master

        async def start_and_connect(self):
            if getattr(state, "on_start", None):
                state.on_start()
            state.controls.append("start_and_connect")

        async def connect(self):
            state.controls.append("connect")

        async def start_sim(self):
            state.controls.append("start")

        def stop_sim(self):  # synchronous in cmapi; the manager accepts both
            state.controls.append("stop")
            self.finished.set()

        async def pause_sim(self):
            state.controls.append("pause")

        async def resume_sim(self):
            state.controls.append("resume")

        def create_simstate_condition(self, which):
            return Condition(self.finished)

        def create_quantity_condition(self, predicate, name):
            state.stop_predicate = predicate
            return Condition(self.time_reached)

        async def stop_and_disconnect(self):
            state.disconnected.append("stop_and_disconnect")

        async def disconnect(self):
            state.disconnected.append("disconnect")

    class Task:
        @staticmethod
        def run_main_task(coro):
            asyncio.run(coro)

    mod = types.SimpleNamespace(
        Project=Project, Variation=Variation, OutputQuantities=OutputQuantities, CarMaker=CarMaker,
        ApoServer=ApoServer, ApoServerInfo=ApoServerInfo, SimControlInteractive=SimControlInteractive,
        StorageMode=types.SimpleNamespace(save="save"), ConditionSimState=types.SimpleNamespace(finished=1),
        Task=Task, query_aposerverinfos=lambda host: [types.SimpleNamespace(spec={"pid": 4711, "identity": "CM"})],
    )
    return mod, state


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    home = tmp_path / "cm"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "CarMaker.win64.exe").write_bytes(b"")
    cmapi, state = make_cmapi()
    monkeypatch.setattr(standalone, "import_cmapi", lambda cm_home: cmapi)
    m = StandaloneManager(home, tmp_path / "proj")
    m.state = state
    return m


def on_loop(mgr, fn):
    """Run ``fn`` on the manager's event loop (asyncio events are not thread safe)."""
    mgr._loop.call_soon_threadsafe(fn)


def test_launch_run_and_results(mgr):
    r = mgr.launch("My Runs/Braking", overrides={"Vehicle.DriverTemplate.FName": "X", "Body.mass": 300},
                   quantities=["Time", "Car.ax"], realtime_factor=10)
    st = mgr.state
    assert r["instance"] == "sa1" and r["pid"] == 4711 and r["started"] is True
    assert st.project == mgr.project and st.testrun == Path("My Runs/Braking")
    assert st.overrides == {"Vehicle.DriverTemplate.FName": "X", "Body.mass": "300"}
    assert st.quantities == ["Time", "Car.ax"] and st.rtf == 10.0 and st.storage == "save"
    assert st.controls == ["start_and_connect", "start"]
    assert mgr.servers()[0]["managed_by_us"] is True

    status = mgr.status()
    assert status["finished"] is False and status["live"] == {"Time": 1.0, "Car.v": 2.0}
    assert mgr.wait_end(timeout_s=0.05) == {"instance": "sa1", "finished": False, "timeout_s": 0.05}

    on_loop(mgr, st.sims[0].finished.set)
    end = mgr.wait_end(timeout_s=5)
    assert end["finished"] and end["sim_time_s"] == 12.5 and end["sim_dist_m"] == 100.0
    assert end["error_flag"] == 0 and end["log_errors"] == ["ERROR something broke"]
    assert mgr.results()["result_files"] == [str(Path("SimOutput/host/run.erg"))]
    assert mgr.close() == {"instance": "sa1", "closed": True, "process_stopped": True}
    assert st.disconnected == ["stop_and_disconnect"] and mgr.instances == {}


def test_safety_stop_on_simulation_time(mgr):
    mgr.launch("A", stop_after_s=30)
    st = mgr.state
    assert st.stop_predicate(31) and not st.stop_predicate(29)
    on_loop(mgr, st.sims[0].time_reached.set)
    end = mgr.wait_end(timeout_s=5)
    assert end["finished"] and end["stop_reason"] == "stopped by server at Time > 30 s"
    assert st.controls[-1] == "stop"


def test_control_and_dva(mgr):
    mgr.launch("A", start=False, stop_after_s=None)
    for action in ("start", "pause", "resume", "stop"):
        assert mgr.control(action)["ok"] is True
    assert mgr.state.controls == ["start_and_connect", "start", "pause", "resume", "stop"]
    assert mgr.wait_end(timeout_s=5)["stop_reason"] == "stopped by request"
    assert mgr.dva_write("DM.Gas", 0.5, 100)["dva_written"] == "DM.Gas"
    assert mgr.state.dva == [("DM.Gas", 0.5, 100)]
    with pytest.raises(BackendError, match="action must be"):
        mgr.control("explode")


def test_attach_without_and_with_testrun(mgr):
    r = mgr.attach(1234)
    assert r == {"instance": "sa1", "pid": 1234, "attached": True, "testrun": None, "started": False}
    assert mgr.state.attached_pid == 1234 and mgr.state.controls == ["connect"]
    with pytest.raises(BackendError, match="no test run configured"):
        mgr.control("start", "sa1")
    assert mgr.results("sa1")["result_files"] == []
    assert mgr.close("sa1")["process_stopped"] is False and mgr.state.disconnected == ["disconnect"]

    r = mgr.attach(1234, "A", overrides={"k": 1}, start=True)
    assert r["started"] is True and mgr.state.controls[-2:] == ["connect", "start"]


def test_instance_selection_and_errors(mgr, tmp_path):
    with pytest.raises(BackendError, match="specify instance"):
        mgr.status()
    mgr.launch("A")
    mgr.launch("B")
    with pytest.raises(BackendError, match="specify instance"):
        mgr.status()
    assert mgr.status("sa2")["instance"] == "sa2"
    with pytest.raises(BackendError, match="unknown instance"):
        mgr.status("sa9")
    with pytest.raises(BackendError, match="executable not found"):
        mgr.launch("A", executable=tmp_path / "nope.exe")
    mgr.close_all()
    assert mgr.instances == {}
    mgr.project = None
    with pytest.raises(BackendError, match="no project directory"):
        mgr.launch("A")


def test_failed_launch_reports_the_log_and_leaves_no_process(mgr, monkeypatch):
    killed = []
    monkeypatch.setattr(standalone.os, "kill", lambda pid, sig: killed.append(pid))
    log = mgr.project / "SimOutput" / "host" / "Log" / "host_20260101_120000.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"ERROR\t\told error\r\n")

    def program_stops_without_licence():
        with log.open("ab") as f:
            f.write(b"ERROR\t\tUnable to obtain CarMaker license: too many licenses in use (-17)\r\n"
                    b"ERROR\t\tFATAL ERROR - Application stopped\r\n")
        raise RuntimeError("Cannot find a server which matches the requested parameters {...}\nAvailable: []")

    mgr.state.on_start = program_stops_without_licence
    with pytest.raises(BackendError) as e:
        mgr.launch("A")
    msg = str(e.value)
    assert "too many licenses in use" in msg and "holding the licence" in msg
    assert "old error" not in msg and "FATAL ERROR" not in msg
    assert killed == [4711] and mgr.state.disconnected == ["stop_and_disconnect"] and mgr.instances == {}

    def no_log():
        raise RuntimeError("no such executable\nmore detail")

    mgr.state.on_start = no_log
    with pytest.raises(BackendError, match="CarMaker did not start: RuntimeError: no such executable$"):
        mgr.launch("A")


def test_errors_and_timeouts_from_cmapi(mgr):
    async def boom():
        raise ValueError("bad thing")

    with pytest.raises(BackendError, match="ValueError: bad thing"):
        mgr._call(boom)

    async def slow():
        await asyncio.sleep(5)

    with pytest.raises(BackendError, match="did not answer within 0s"):
        mgr._call(slow, timeout=0.05)
