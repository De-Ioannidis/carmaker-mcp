import json

import pytest

from carmaker_mcp.guard import GuardError
from carmaker_mcp.project import ProjectError


def test_list_hides_tmp_and_hidden(session):
    p = session.project
    assert p.list("testrun")["names"] == ["Sub/Run1"]
    assert p.list("testrun", include_hidden=True)["names"] == [".Hidden/Run2", "Sub/Run1"]
    assert p.list("testrun", pattern="sub/*")["names"] == ["Sub/Run1"]
    assert p.list("vehicle")["names"] == ["Test_Vehicle"]


def test_read_filters(session):
    r = session.project.read("vehicle", "Test_Vehicle", prefix="Body.")
    assert r["values"] == {"Body.mass": "285", "Body.pos": "0.1 0 0.2"}
    assert session.project.read("vehicle", "Test_Vehicle", keys=["Description"])["values"] == {
        "Description": "Test car.\nSecond line."
    }


def test_edit_backs_up_once_logs_and_restores_bytes(session, project):
    f = project / "Data" / "Vehicle" / "Test_Vehicle"
    original = f.read_bytes()
    r = session.project.edit("vehicle", "Test_Vehicle", {"Body.mass": 300})
    assert r["changed"] and r["changes"] == [{"key": "Body.mass", "old": "285", "new": "300"}]
    session.project.edit("vehicle", "Test_Vehicle", {"Body.mass": 310, "Body.pos": [1, 2, 3]})
    assert b"Body.pos = 1 2 3" in f.read_bytes()
    log = session.store.entries("t1")
    assert [e["key"] for e in log] == ["Body.mass", "Body.mass", "Body.pos"]
    assert log[1]["old"] == "300"
    # one backup, taken before the very first edit
    backup = session.store.dir / "backups" / "t1" / "Data" / "Vehicle" / "Test_Vehicle"
    assert backup.read_bytes() == original
    session.store.restore_files()
    assert f.read_bytes() == original


def test_edit_noop_does_not_touch_or_log(session, project):
    r = session.project.edit("testrun", "Sub/Run1", {"Vehicle": "Test_Vehicle"})
    assert r == {"changed": False, "changes": []}
    assert session.store.entries() == []
    assert not (session.store.dir / "backups").exists()


def test_edit_unset_and_text(session):
    session.project.edit("vehicle", "Test_Vehicle", {"Hitch.System": None, "Description": "A\nB"})
    d = session.project.read("vehicle", "Test_Vehicle")["values"]
    assert "Hitch.System" not in d and d["Description"] == "A\nB"


def test_edit_rejects_traversal_and_binary(session, project):
    with pytest.raises(GuardError):
        session.project.edit("vehicle", "../../../etc/x", {"a": 1})
    (project / "Data" / "Road").mkdir()
    (project / "Data" / "Road" / "bin.rd5").write_bytes(b"\x00\x01binary")
    with pytest.raises(ProjectError):
        session.project.edit("road", "bin.rd5", {"a": 1})
    with pytest.raises(ProjectError):
        session.project.read("nonsense", "x")


def test_clone_never_overwrites_and_revert_moves_to_trash(session, project):
    c = session.project.clone("testrun", "Sub/Run1", "Sub/Run1_copy")
    assert c["created"] == "Data/TestRun/Sub/Run1_copy"
    assert (project / "Data/TestRun/Sub/Run1_copy").exists()
    with pytest.raises(ProjectError):
        session.project.clone("testrun", "Sub/Run1", "Sub/Run1_copy")
    out = session.store.restore_files()
    assert not (project / "Data/TestRun/Sub/Run1_copy").exists()
    assert (session.store.dir / "trash" / "t1" / "Data/TestRun/Sub/Run1_copy").exists()
    assert len(out["moved_to_trash"]) == 1


def test_second_revert_brings_nothing_back(session, project):
    """Found live: a cloned and then edited file came back at the second revert, from its backup."""
    copy = project / "Data/TestRun/Sub/Run1_copy"
    vehicle = project / "Data/Vehicle/Test_Vehicle"
    original = vehicle.read_bytes()
    session.project.clone("testrun", "Sub/Run1", "Sub/Run1_copy")
    session.project.edit("testrun", "Sub/Run1_copy", {"Road.FName": "Other.rd5"})
    session.project.edit("vehicle", "Test_Vehicle", {"Body.mass": 300})
    out = session.store.restore_files()
    assert not copy.exists() and vehicle.read_bytes() == original
    assert [p.replace("\\", "/").endswith("Vehicle/Test_Vehicle") for p in out["restored"]] == [True]

    vehicle.write_bytes(original + b"## edited by the user after the revert\r\n")
    out = session.store.restore_files()
    assert out == {"restored": [], "moved_to_trash": []}
    assert not copy.exists() and vehicle.read_bytes().endswith(b"after the revert\r\n")

    session.project.edit("vehicle", "Test_Vehicle", {"Body.mass": 310})  # edited again: backed up again
    session.store.restore_files()
    assert vehicle.read_bytes().endswith(b"after the revert\r\n") and b"Body.mass = 285" in vehicle.read_bytes()


def test_log_is_valid_jsonl(session):
    session.project.edit("vehicle", "Test_Vehicle", {"Body.mass": 1})
    for line in session.store.log_path.read_text().splitlines():
        assert json.loads(line)["session"] == "t1"
