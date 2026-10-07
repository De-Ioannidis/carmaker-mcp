import asyncio

import pytest
from fastmcp import Client

from carmaker_mcp import server

OPTIONAL = {"cm_gui_tcl", "cm_model_add_block", "cm_model_add_line", "cm_model_delete_block"}
DEFAULT = {
    "cm_session_start", "cm_doctor",
    "cm_status", "cm_load_testrun", "cm_start_sim", "cm_stop_sim", "cm_wait_end", "cm_log", "cm_live",
    "cm_dva_write", "cm_dva_release", "cm_popups", "cm_popup_timeout",
    "cm_results_list", "cm_results_summary", "cm_results_read",
    "cm_list", "cm_read", "cm_edit", "cm_clone",
    "cm_list_workspace_vars", "cm_get_workspace_var", "cm_set_workspace_var",
    "cm_model_get", "cm_model_set", "cm_model_save", "cm_model_logs_save",
    "cm_standalone_servers", "cm_standalone_launch", "cm_standalone_attach", "cm_standalone_status",
    "cm_standalone_control", "cm_standalone_wait_end", "cm_standalone_dva_write",
    "cm_standalone_results", "cm_standalone_close",
    "cm_study_start", "cm_study_status", "cm_study_cancel",
    "cm_changelog", "cm_revert_all", "cm_restore",
    "cm_output_quantities", "cm_output_quantities_edit", "cm_movie_open", "cm_movie_snapshot",
}
FULL = server.create_server({"tcl", "experimental"})


@pytest.fixture(autouse=True)
def _session(session):
    server.set_session(session)
    yield
    server.set_session(None)


def call(name, args=None, mcp=FULL):
    async def go():
        async with Client(mcp) as c:
            return await c.call_tool(name, args or {}, raise_on_error=False)

    return asyncio.run(go())


def tools(mcp):
    async def go():
        async with Client(mcp) as c:
            return {t.name: t for t in await c.list_tools()}

    return asyncio.run(go())


def test_default_and_optional_tools(monkeypatch):
    assert set(tools(server.create_server())) == DEFAULT
    assert set(tools(FULL)) == DEFAULT | OPTIONAL
    assert set(tools(server.create_server({"tcl"}))) == DEFAULT | {"cm_gui_tcl"}
    monkeypatch.setenv("CM_ENABLE", " TCL, experimental ,nonsense")
    assert server.features_from_env() == {"tcl", "experimental"}
    monkeypatch.delenv("CM_ENABLE")
    assert server.features_from_env() == frozenset()

    slim = set(tools(server.create_server(disabled={"standalone", "study", "matlab", "movie"})))
    assert slim == {t for t in DEFAULT if server._group(t) is None} and len(slim) == 25
    monkeypatch.setenv("CM_DISABLE", "standalone,what")
    assert server.disabled_from_env() == {"standalone"}


def test_status_and_edit_flow(project):
    assert call("cm_status").structured_content["sim_status"] == "idle"
    r = call("cm_edit", {"kind": "vehicle", "name": "Test_Vehicle", "overrides": {"Body.mass": 123}})
    assert r.structured_content["changes"][0]["new"] == "123"
    assert call("cm_changelog").structured_content["result"][0]["key"] == "Body.mass"
    call("cm_revert_all")
    assert b"Body.mass = 285" in (project / "Data/Vehicle/Test_Vehicle").read_bytes()


def test_domain_errors_become_tool_errors():
    r = call("cm_edit", {"kind": "vehicle", "name": "../../../x", "overrides": {"a": 1}})
    assert r.is_error and "outside the project root" in r.content[0].text
    # inside the project but outside the kind's own folder
    r = call("cm_edit", {"kind": "vehicle", "name": "../TestRun/Sub/Run1", "overrides": {"a": 1}})
    assert r.is_error and "outside Data/Vehicle" in r.content[0].text
    r = call("cm_gui_tcl", {"command": "exit"})
    assert r.is_error and "deny-list" in r.content[0].text
    # disabled groups are not callable on the default server
    r = call("cm_gui_tcl", {"command": "SimStatus"}, mcp=server.create_server())
    assert r.is_error


def test_bad_arguments_are_rejected_by_the_schema():
    assert call("cm_list", {"kind": "spaceship"}).is_error
    assert call("cm_start_sim", {"save": "yes"}).is_error
    assert call("cm_log", {"level": "loud"}).is_error
    assert call("cm_results_read", {"erg": "x.erg", "quantities": ["Time"], "max_points": 1}).is_error


def test_tool_annotations_and_version():
    ann = {name: t.annotations for name, t in tools(FULL).items()}
    assert all(a is not None for a in ann.values())

    def hint(tool, name):
        return ann[tool].model_dump(by_alias=True)[name]

    assert hint("cm_status", "readOnlyHint") and hint("cm_results_read", "readOnlyHint")
    assert not hint("cm_edit", "readOnlyHint") and hint("cm_edit", "destructiveHint")
    assert not hint("cm_clone", "readOnlyHint") and not hint("cm_clone", "destructiveHint")
    assert not any(hint(t, "openWorldHint") for t in ann)

    from carmaker_mcp import __version__

    assert server.mcp.name == "carmaker" and server.mcp.version == __version__
