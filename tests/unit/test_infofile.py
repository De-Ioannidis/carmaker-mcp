import pytest
from conftest import VEHICLE

from carmaker_mcp.infofile import InfoFile, InfoFileError, diff_values


def test_roundtrip_is_identical():
    assert InfoFile(VEHICLE).dumps() == VEHICLE


def test_get_values_and_text():
    f = InfoFile(VEHICLE)
    assert f.get("Body.mass") == "285"
    assert f.get("Hitch.System") == ""
    assert f.get("Description") == "Test car.\nSecond line."
    assert f.get("Nope") is None
    assert f.keys() == ["FileIdent", "Description", "Body.mass", "Hitch.System", "Body.pos"]


def test_set_changes_only_that_line_and_keeps_crlf():
    f = InfoFile(VEHICLE)
    f.set("Body.mass", "300")
    expected = VEHICLE.replace("Body.mass = 285", "Body.mass = 300")
    assert f.dumps() == expected
    assert "\n" not in f.dumps().replace("\r\n", "")


def test_set_empty_value_and_fill_empty():
    f = InfoFile(VEHICLE)
    f.set("Body.mass", "")
    assert "Body.mass =\r\n" in f.dumps()
    f.set("Hitch.System", "Foo")
    assert "Hitch.System = Foo\r\n" in f.dumps()


def test_set_new_key_appends_with_file_eol():
    f = InfoFile("A = 1\nB = 2")  # no trailing newline
    f.set("C", "3")
    assert f.dumps() == "A = 1\nB = 2\nC = 3\n"


def test_set_text_replaces_block():
    f = InfoFile(VEHICLE)
    f.set_text("Description", "New\nlines")
    assert f.get("Description") == "New\nlines"
    assert "Body.mass = 285" in f.dumps()


def test_set_refuses_multiline_key_and_bad_input():
    f = InfoFile(VEHICLE)
    with pytest.raises(InfoFileError):
        f.set("Description", "x")
    with pytest.raises(InfoFileError):
        f.set("Body.mass", "1\n2")
    with pytest.raises(InfoFileError):
        f.set("bad key", "1")


def test_unset_and_duplicate_keys_last_wins():
    f = InfoFile("A = 1\nA = 2\nB = 3\n")
    assert f.get("A") == "2"
    f.set("A", "9")
    assert f.dumps() == "A = 1\nA = 9\nB = 3\n"
    assert f.unset("A") and f.get("A") is None
    assert f.dumps() == "B = 3\n"


def test_diff_values():
    a = InfoFile(VEHICLE)
    b = InfoFile(VEHICLE)
    b.set("Body.mass", "1")
    b.set("New.key", "2")
    assert diff_values(a, b) == [
        {"key": "Body.mass", "old": "285", "new": "1"},
        {"key": "New.key", "old": None, "new": "2"},
    ]
