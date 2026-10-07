import asyncio

import pytest
from fastmcp import Client

from carmaker_mcp import server
from carmaker_mcp.backend import BackendError
from carmaker_mcp.standalone import find_cm_home, import_cmapi


class FakeManager:
    def __init__(self):
        self.calls = []

    def servers(self):
        return [{"pid": 1, "identity": "CarMaker", "description": "Idle", "managed_by_us": False}]

    def launch(self, testrun, **kw):
        self.calls.append(("launch", testrun, kw))
        return {"instance": "sa1", "pid": 42, "testrun": testrun}

    def attach(self, pid, testrun=None, **kw):
        self.calls.append(("attach", pid, testrun, kw))
        return {"instance": "sa2", "pid": pid}

    def status(self, instance=None, quantities=None):
        return {"instance": instance, "live": {"Time": 1.0}}

    def control(self, action, instance=None):
        self.calls.append(("control", action, instance))
        return {"ok": True}

    def wait_end(self, instance=None, timeout_s=600.0):
        return {"finished": True}

    def dva_write(self, name, value, duration_ms=-1, instance=None):
        self.calls.append(("dva", name, value))
        return {"dva_written": name}

    def results(self, instance=None):
        return {"result_files": []}

    def close(self, instance=None):
        return {"closed": True}


@pytest.fixture
def fake(session):
    server.set_session(session)
    m = FakeManager()
    server.set_standalone(m)
    yield m
    server.set_standalone(None)
    server.set_session(None)


def call(name, args=None):
    async def go():
        async with Client(server.mcp) as c:
            return await c.call_tool(name, args or {}, raise_on_error=False)

    return asyncio.run(go())


def test_launch_passes_arguments_through(fake):
    r = call("cm_standalone_launch", {
        "testrun": "A/B", "overrides": {"Vehicle.DriverTemplate.FName": "X"},
        "realtime_factor": 5.0, "stop_after_s": 30.0,
    })
    assert r.structured_content["instance"] == "sa1"
    _, testrun, kw = fake.calls[0]
    assert testrun == "A/B" and kw["overrides"] == {"Vehicle.DriverTemplate.FName": "X"}
    assert kw["realtime_factor"] == 5.0 and kw["stop_after_s"] == 30.0 and kw["start"] is True


def test_other_standalone_tools(fake):
    assert call("cm_standalone_servers").structured_content["result"][0]["pid"] == 1
    assert call("cm_standalone_attach", {"pid": 7}).structured_content["pid"] == 7
    assert call("cm_standalone_control", {"action": "pause"}).structured_content["ok"]
    assert ("control", "pause", None) in fake.calls
    assert call("cm_standalone_wait_end").structured_content["finished"]
    assert call("cm_standalone_results").structured_content["result_files"] == []
    assert call("cm_standalone_close").structured_content["closed"]


def test_standalone_errors_become_tool_errors(fake):
    def boom(*a, **k):
        raise BackendError("no such instance")

    fake.status = boom
    r = call("cm_standalone_status", {"instance": "zz"})
    assert r.is_error and "no such instance" in r.content[0].text


def test_cm_home_detection_errors(tmp_path):
    with pytest.raises(BackendError, match="does not exist"):
        find_cm_home(tmp_path / "nope")
    home = tmp_path / "cm"
    (home / "Python").mkdir(parents=True)
    with pytest.raises(BackendError, match="no cmapi"):
        import_cmapi(home)


def test_servers_reads_cmapi_spec(tmp_path, monkeypatch):
    """cmapi's ApoServerInfo has no pid/identity attributes; the fields live in ``spec``."""
    from types import SimpleNamespace

    from carmaker_mcp.standalone import StandaloneManager

    info = SimpleNamespace(sid=0, spec={"pid": 7, "identity": "CarMaker 14 - Car_Generic",
                                        "description": "Idle", "hostname": "pc",
                                        "appclass": "CarMaker:Version=14"})
    mgr = StandaloneManager(tmp_path)
    monkeypatch.setattr(mgr, "_start", lambda: None)
    mgr._cmapi = SimpleNamespace(query_aposerverinfos=lambda host: [info, SimpleNamespace(sid=1)])
    assert mgr.servers() == [
        {"pid": 7, "identity": "CarMaker 14 - Car_Generic", "description": "Idle",
         "appclass": "CarMaker:Version=14", "host": "pc", "managed_by_us": False},
        {"pid": None, "identity": None, "description": None, "appclass": None, "host": None,
         "managed_by_us": False},
    ]
