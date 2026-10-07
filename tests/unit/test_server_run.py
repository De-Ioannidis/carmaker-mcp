"""Run-control tools through the MCP layer (in-memory client, mock backend)."""

import asyncio

import pytest
from fastmcp import Client

from carmaker_mcp import server


@pytest.fixture(autouse=True)
def _session(session):
    server.set_session(session)
    yield
    server.set_session(None)


def run(coro_fn, **client_kw):
    async def go():
        async with Client(server.mcp, **client_kw) as c:
            return await coro_fn(c)

    return asyncio.run(go())


def test_run_cycle_through_tools(mock):
    mock.polls_until_idle = 3

    async def cycle(c):
        start = await c.call_tool("cm_start_sim", {})
        end = await c.call_tool("cm_wait_end", {"timeout_s": 5, "poll_s": 0.01})
        return start.structured_content, end.structured_content

    start, end = run(cycle)
    assert start["started"] and start["save_mode"] == "save"
    assert end["finished"] and end["end_status"] == "completed" and end["result_file"] is None


def test_wait_end_is_bounded_and_stop_ends_the_run(mock):
    mock.polls_until_idle = 10_000

    async def cycle(c):
        await c.call_tool("cm_start_sim", {})
        waiting = await c.call_tool("cm_wait_end", {"timeout_s": 0.05, "poll_s": 0.01})
        stopped = await c.call_tool("cm_stop_sim", {"wait_s": 2})
        return waiting.structured_content, stopped.structured_content

    waiting, stopped = run(cycle)
    assert waiting["finished"] is False and waiting["live"]["Time"] == 3.0
    assert stopped["finished"] and stopped["end_status"] == "aborted"


def test_wait_end_reports_progress(mock, monkeypatch):
    mock.polls_until_idle = 10_000
    clock = iter(range(0, 10_000, 3))
    monkeypatch.setattr("carmaker_mcp.session.time.time", lambda: next(clock))
    monkeypatch.setattr("carmaker_mcp.session.time.sleep", lambda s: None)
    seen = []

    async def handler(progress, total, message):
        seen.append((progress, total, message))

    async def cycle(c):
        await c.call_tool("cm_start_sim", {})
        r = await c.call_tool("cm_wait_end", {"timeout_s": 30})
        await asyncio.sleep(0.05)  # let the last notifications arrive
        return r.structured_content

    assert run(cycle, progress_handler=handler)["finished"] is False
    assert seen and seen[0][1] == 30 and seen[0][2] == "simulation time 3.0 s"


def test_log_tool_and_bad_level(project):
    d = project / "SimOutput" / "host" / "Log"
    d.mkdir(parents=True)
    (d / "host.log").write_bytes(("ERROR" + chr(9) + chr(9) + "boom" + chr(13) + chr(10)).encode())

    async def calls(c):
        ok = await c.call_tool("cm_log", {"level": "error"})
        bad = await c.call_tool("cm_log", {"level": "loud"}, raise_on_error=False)
        return ok.structured_content, bad

    ok, bad = run(calls)
    assert ok["total"] == 1 and ok["lines"][0].endswith("boom")
    assert bad.is_error
