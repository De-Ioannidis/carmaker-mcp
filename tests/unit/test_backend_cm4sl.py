"""Cm4slBackend against a fake MATLAB engine (no MATLAB, no matlabengine needed)."""

import sys
import types

import pytest

from carmaker_mcp import backend_cm4sl
from carmaker_mcp.backend import BackendError, NotSupported
from carmaker_mcp.backend_cm4sl import Cm4slBackend, _py, _to_matlab


class Future:
    def __init__(self, fn):
        self.fn, self.cancelled = fn, False

    def result(self, timeout=None):
        return self.fn()

    def cancel(self):
        self.cancelled = True


class FakeEngine:
    """Records calls; ``handlers[name](*args)`` supplies the answer (a value or an exception)."""

    def __init__(self, **handlers):
        self.handlers, self.calls, self.workspace = handlers, [], {}

    def __getattr__(self, name):
        def call(*args, nargout=1, background=False, stdout=None, stderr=None):
            assert background is True
            assert stdout is not None and stderr is not None, "MATLAB output must be captured"
            self.calls.append((name, args, nargout))

            def run():
                out = self.handlers[name](*args)
                if isinstance(out, Exception):
                    raise out
                return out

            return Future(run)

        return call


def backend(**handlers):
    b = Cm4slBackend("cm_mcp", timeout=3)
    b._eng = FakeEngine(**handlers)
    return b


def gui(answers):
    """cmguicmd handler: answers[command] is (result, status); the default is ('', 0)."""
    return lambda cmd, timeout_ms: answers.get(cmd, ("", 0.0))


@pytest.fixture
def fake_matlab(monkeypatch):
    """A stand-in for the ``matlab`` package of matlabengine."""
    mod = types.ModuleType("matlab")

    class double(list):
        pass

    for name in ("double", "single", "int8", "int16", "int32", "int64", "uint8", "uint16", "uint32",
                 "uint64", "logical"):
        setattr(mod, name, double if name == "double" else type(name, (list,), {}))
    monkeypatch.setitem(sys.modules, "matlab", mod)
    return mod


# ---- Tcl and status --------------------------------------------------------------------
def test_gui_tcl_returns_status_and_text():
    b = backend(cmguicmd=gui({"Foo": ("bar", 0.0)}))
    assert b.gui_tcl("Foo", 2000) == (0, "bar")
    assert b._eng.calls[0] == ("cmguicmd", ("Foo", 2000.0), 2)
    with pytest.raises(BackendError, match=r"Tcl `Foo` failed \(status -1\): boom"):
        backend(cmguicmd=gui({"Foo": ("boom", -1.0)}))._tcl_ok("Foo")


def test_status_prefers_getsimstatus_and_falls_back():
    cm = {"simstate": "terminated", "activemodel": "mdl/CarMaker/IPG Vehicle", "endstatus": "completed",
          "getprojectdir": "C:/p"}
    b = backend(cmguicmd=gui({"GetSimStatus": ("-2", 0.0)}), cmcmd=lambda a: cm[a])
    st = b.status()
    assert st.connected and st.idle and st.sim_status_text == "idle"
    assert (st.simstate, st.active_model, st.end_status, st.project_dir) == ("terminated", "mdl", "completed", "C:/p")
    assert b._status_cmd == "GetSimStatus"

    old = backend(cmguicmd=gui({"GetSimStatus": ("invalid command name", -1.0), "SimStatus": ("3", 0.0)}),
                  cmcmd=lambda a: cm[a])
    st = old.status()
    assert st.sim_status == 3 and st.sim_status_text == "running" and old._status_cmd == "SimStatus"
    assert [c[1][0] for c in old._eng.calls if c[0] == "cmguicmd"] == ["GetSimStatus", "SimStatus"]
    old.status()
    assert [c[1][0] for c in old._eng.calls if c[0] == "cmguicmd"][-1] == "SimStatus"


def test_status_when_the_gui_or_matlab_does_not_answer():
    st = backend(cmguicmd=gui({"GetSimStatus": ("no connection", -2.0)})).status()
    assert st.connected is False and "status -2" in st.extra["error"]

    def busy(arg):
        raise RuntimeError("busy")

    st = backend(cmguicmd=gui({"GetSimStatus": ("-2", 0.0)}), cmcmd=busy).status()
    assert st.connected and "unavailable" in st.extra["simstate"]


# ---- run control ------------------------------------------------------------------------
@pytest.mark.parametrize("answer, message", [
    ("failed", "could not load it"), ("cancel", "answered with Cancel"),
    ("incomplete", "loaded incompletely"), ("weird", "CarMaker answered 'weird'"),
])
def test_load_testrun_maps_carmaker_keywords(answer, message):
    b = backend(cmguicmd=lambda cmd, t: (answer, 0.0))
    with pytest.raises(BackendError, match=message):
        b.load_testrun("My Runs/A B")


def test_load_testrun_quotes_and_forces():
    b = backend(cmguicmd=gui({}))
    assert b.load_testrun("My Runs/A B") == ""
    b.load_testrun("Plain", force=True)
    cmds = [c[1] for c in b._eng.calls]
    assert cmds == [("LoadTestRun {My Runs/A B}", 60000.0), ("LoadTestRun Plain 1", 60000.0)]


def test_start_and_stop_do_not_wait():
    b = backend(cmguicmd=gui({}))
    b.start_sim()
    b.stop_sim()
    assert [c[1] for c in b._eng.calls] == [("StartSim", 0.0), ("StopSim", 0.0)]


def test_quantities_and_dva():
    def cmd(command, timeout_ms):
        return ("12.5 nan oops", 0.0) if command.startswith("set __r") else ("", 0.0)

    b = backend(cmguicmd=cmd)
    assert b.quantity_read(["Car.v", "Time", "X"]) == {"Car.v": 12.5, "Time": None, "X": None}
    b.dva_write("DM.Gas", 0.5, 2000, "Abs")
    b.dva_release()
    sent = [c[1][0] for c in b._eng.calls]
    assert sent[0] == "QuantSubscribe {Car.v Time X}"
    assert sent[-2:] == ["DVAWrite DM.Gas 0.5 2000 Abs", "DVAReleaseQuants"]


# ---- engine plumbing -----------------------------------------------------------------------
def test_timeout_and_matlab_errors_become_backend_errors():
    class Slow(Future):
        def result(self, timeout=None):
            raise TimeoutError

    b = backend()
    slow = Slow(None)
    b._eng.eval = lambda *a, **k: slow  # type: ignore[method-assign]
    with pytest.raises(BackendError, match="did not answer eval within 3s"):
        b._call("eval", "pause(100)")
    assert slow.cancelled

    from carmaker_mcp.backend import BackendTimeout

    engine_timeout = type("TimeoutError", (Exception,), {})("timeout from execution of the MATLAB function")
    with pytest.raises(BackendTimeout, match="did not answer eval"):
        backend(eval=lambda code: engine_timeout).matlab("open_system('big')")

    b = backend(eval=lambda code: RuntimeError("Undefined function 'foo'"))
    with pytest.raises(BackendError, match="MATLAB error in eval: Undefined function"):
        b.matlab("foo")


def test_reconnects_once_when_the_session_was_closed(monkeypatch):
    b = backend(eval=lambda code: RuntimeError("MATLAB session is closed"))
    fresh = FakeEngine(eval=lambda code: 4.0)
    monkeypatch.setattr(b, "_engine", lambda: b._eng or setattr(b, "_eng", fresh) or fresh)
    assert b.matlab("2+2") == 4.0


def test_missing_engine_package_and_missing_session(monkeypatch):
    monkeypatch.setitem(sys.modules, "matlab", None)
    monkeypatch.setitem(sys.modules, "matlab.engine", None)
    with pytest.raises(BackendError, match="matlabengine is not installed"):
        Cm4slBackend()._engine()

    pkg, eng = types.ModuleType("matlab"), types.ModuleType("matlab.engine")

    def connect(name):
        raise RuntimeError("no such session")

    eng.connect_matlab = connect  # type: ignore[attr-defined]
    pkg.engine = eng  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "matlab", pkg)
    monkeypatch.setitem(sys.modules, "matlab.engine", eng)
    with pytest.raises(BackendError, match=r"shareEngine\('other'\)"):
        Cm4slBackend("other")._engine()


# ---- workspace and Simulink ------------------------------------------------------------------
def test_workspace_get_base_model_and_objects(fake_matlab):
    def ev(code):
        if code == "Kp":
            return fake_matlab.double([[2.0]])
        if code.startswith("class("):
            return "Simulink.LookupTable"
        return RuntimeError("cannot convert")

    b = backend(eval=ev)
    assert b.workspace_get("Kp") == 2.0
    assert b.workspace_get("Table", "mdl") == {
        "class": "Simulink.LookupTable", "note": "MATLAB object; address a field, e.g. Name.Table.Value"}
    assert b._eng.calls[1][1] == ("evalin(get_param('mdl','ModelWorkspace'), 'Table')",)


def test_workspace_set_clear_and_list(fake_matlab):
    b = backend(eval=lambda code: '{"name":"Kp","class":"double","size":[1,1]}' if "jsonencode" in code
                else None)
    b.workspace_set("Kp", 3, "base")
    assert b._eng.workspace["cm_mcp_tmp"] == 3.0
    b.workspace_set("Gains", [[1, 2], [3, 4]], "mdl")
    b.workspace_clear("Kp")
    b.workspace_clear("Kp", "mdl")
    code = [c[1][0] for c in b._eng.calls]
    assert code[0] == "Kp = cm_mcp_tmp;" and code[1] == "clear cm_mcp_tmp"
    assert code[2].startswith("assignin(get_param('mdl','ModelWorkspace'), 'cm_mcp_tmp', cm_mcp_tmp);")
    assert code[4:] == ["clear Kp", "evalin(get_param('mdl','ModelWorkspace'), 'clear Kp')"]
    assert b.workspace_list("base") == [{"name": "Kp", "class": "double", "size": [1, 1]}]
    assert b.workspace_list("mdl")[0]["name"] == "Kp"


def test_model_calls():
    b = backend(get_param=lambda path, p: "C:/proj/m.slx" if p == "FileName" else "5",
                set_param=lambda *a: None, save_system=lambda m: None, close_system=lambda m, f: None,
                load_system=lambda f: None, add_block=lambda *a: 1.0, add_line=lambda *a: 1.0,
                delete_block=lambda p: None)
    assert b.model_get("m/Gain", "Gain") == "5" and b.model_file("m") == "C:/proj/m.slx"
    b.model_set("m/Gain", "Gain", 2.5)
    b.model_set("m/Gain", "Gain", "Kp")
    b.model_save("m")
    b.model_reload("m", "C:/proj/m.slx")
    b.model_struct("add_block", src="simulink/Math Operations/Gain", dest="m/G2", params={"Gain": 3})
    b.model_struct("add_line", system="m", out="G1/1", inp="G2/1")
    b.model_struct("delete_block", path="m/G2")
    calls = {c[0]: c[1] for c in b._eng.calls}
    assert calls["set_param"] == ("m/Gain", "Gain", "Kp")
    assert ("set_param", ("m/Gain", "Gain", "2.5"), 0) in b._eng.calls
    assert calls["close_system"] == ("m", 0) and calls["load_system"] == ("C:/proj/m.slx",)
    assert calls["add_block"] == ("simulink/Math Operations/Gain", "m/G2", "Gain", "3")
    assert calls["add_line"] == ("m", "G1/1", "G2/1", "autorouting", "on")
    with pytest.raises(NotSupported):
        b.model_struct("paint", colour="red")


def test_value_conversion(fake_matlab):
    d = fake_matlab.double
    assert _py(d([[1.0]])) == 1.0 and _py(d([[1.0, 2.0]])) == [1.0, 2.0]
    assert _py(d([[1.0, 2.0], [3.0, 4.0]])) == [[1.0, 2.0], [3.0, 4.0]]
    assert _py({"a": d([[1.0]]), "b": ("x", None)}) == {"a": 1.0, "b": ["x", None]}
    assert _py(object).startswith("<class")
    assert _to_matlab(True) is True and _to_matlab(2) == 2.0 and _to_matlab("s") == "s"
    assert _to_matlab([1, 2]) == [[1.0, 2.0]] and _to_matlab([[1, 2], [3, 4]]) == [[1.0, 2.0], [3.0, 4.0]]
    assert _to_matlab({"k": 1}) == {"k": 1.0}
    assert backend_cm4sl._NOWAIT == 0
