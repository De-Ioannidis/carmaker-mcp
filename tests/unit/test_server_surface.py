"""What an MCP client sees: descriptions, titles, typed results, resources, prompts, list size."""

import asyncio
import json
import typing

import pytest
from fastmcp import Client

from carmaker_mcp import schemas, server
from carmaker_mcp.project import KINDS

FULL = server.create_server({"tcl", "experimental"})


@pytest.fixture(autouse=True)
def _session(session):
    server.set_session(session)
    yield
    server.set_session(None)


def with_client(fn, mcp=FULL):
    async def go():
        async with Client(mcp) as c:
            return await fn(c)

    return asyncio.run(go())


def dump(obj):
    return obj.model_dump(by_alias=True, exclude_none=True)


def test_every_tool_and_parameter_is_described():
    tools = with_client(lambda c: c.list_tools())
    for t in tools:
        d = dump(t)
        assert d.get("title") and not d["title"].startswith("Cm "), t.name
        assert len(d.get("description", "")) > 20, t.name
        for pname, schema in d["inputSchema"].get("properties", {}).items():
            assert schema.get("description"), f"{t.name}.{pname} has no description"


def test_kind_enum_matches_the_project_kinds():
    schema = dump({t.name: t for t in with_client(lambda c: c.list_tools())}["cm_list"])["inputSchema"]
    assert set(schema["properties"]["kind"]["enum"]) == set(KINDS)


def test_tool_list_stays_small():
    """tools/list is sent to the model in every conversation: keep an eye on its size."""
    for mcp, budget in ((server.create_server(), 48_000), (FULL, 54_000)):
        blob = json.dumps([dump(t) for t in with_client(lambda c: c.list_tools(), mcp)])
        assert len(blob) < budget, f"tools/list grew to {len(blob)} bytes"


def test_typed_results_lose_no_keys(session, mock, project):
    """FastMCP drops keys that the declared result type does not know."""
    direct_status = session.status()
    mock.polls_until_idle = 10_000
    real_start = mock.start_sim

    def start_with_popup():
        mock.popup_msgs.append(("warn", "w", 0))
        real_start()

    mock.start_sim = start_with_popup

    async def flow(c):
        status = (await c.call_tool("cm_status", {})).structured_content
        start = (await c.call_tool("cm_start_sim", {})).structured_content
        waiting = (await c.call_tool("cm_wait_end", {"timeout_s": 0.05, "poll_s": 0.01})).structured_content
        mock.popup_msgs.append(("err", "e", 0))
        stopped = (await c.call_tool("cm_stop_sim", {"wait_s": 2})).structured_content
        return status, start, waiting, stopped

    status, start, waiting, stopped = with_client(flow)
    assert set(status) == set(direct_status)
    assert set(start) == {"started", "state", "after_s", "save_mode", "popups"}
    assert set(waiting) == {"finished", "state", "waited_s", "live", "poll_errors", "note"}
    assert set(stopped) == {"stop_requested", "finished", "end_status", "elapsed_s", "poll_errors",
                            "result_file", "popups"}
    assert stopped["popups"] == [{"type": "err", "text": "e", "answer": 0}]

    # every key the session can produce is declared
    declared = set(typing.get_type_hints(schemas.WaitResult))
    for produced in (waiting, stopped, {"testrun", "sim_time_s", "distance_m", "log_errors"}):
        assert set(produced) <= declared
    assert set(direct_status) | {"details"} <= set(typing.get_type_hints(schemas.StatusResult))


def test_resources():
    async def read(c):
        listed = sorted(str(r.uri) for r in await c.list_resources())
        guide = (await c.read_resource("carmaker://guide"))[0].text
        status = json.loads((await c.read_resource("carmaker://status"))[0].text)
        changes = json.loads((await c.read_resource("carmaker://changelog"))[0].text)
        log = (await c.read_resource("carmaker://log"))[0].text
        return listed, guide, status, changes, log

    listed, guide, status, changes, log = with_client(read)
    assert listed == ["carmaker://changelog", "carmaker://guide", "carmaker://log", "carmaker://status"]
    assert "cm_wait_end" in guide and status["sim_status"] == "idle" and changes == [] and log == ""


def test_guide_and_prompts_only_name_existing_tools():
    import re

    names = {t.name for t in with_client(lambda c: c.list_tools())}

    async def prompts(c):
        listed = sorted(p.name for p in await c.list_prompts())
        texts = [
            (await c.get_prompt("run_and_summarise", {"testrun": "My Runs/Braking"})).messages[0].content.text,
            (await c.get_prompt("compare_settings", {"testrun": "A", "variable": "Kp", "value_a": "1",
                                                     "value_b": "2"})).messages[0].content.text,
            (await c.get_prompt("undo_session", {})).messages[0].content.text,
        ]
        return listed, texts

    listed, texts = with_client(prompts)
    assert listed == ["compare_settings", "run_and_summarise", "undo_session"]
    assert "My Runs/Braking" in texts[0] and "Kp = 1" in texts[1]
    from carmaker_mcp import guide

    for text in (*texts, guide.GUIDE, guide.INSTRUCTIONS):
        for tool in re.findall(r"cm_[a-z_]+", text):
            assert tool in names or tool.rstrip("_") + "_launch" in names, f"unknown tool {tool!r} in text"
