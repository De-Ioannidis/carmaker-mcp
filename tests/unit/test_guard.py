import pytest

from carmaker_mcp.guard import GuardError, PathGuard


def test_inside_ok(project):
    g = PathGuard(project)
    assert g.resolve("Data/Vehicle/x").parent.name == "Vehicle"
    assert g.relative(g.resolve("Data/Vehicle/x")) == "Data/Vehicle/x"


@pytest.mark.parametrize("bad", ["../outside", "Data/../../outside", ".git/config", "Data/../.git/x"])
def test_rejects_escape_and_git(project, bad):
    with pytest.raises(GuardError):
        PathGuard(project).resolve(bad)


def test_rejects_absolute_outside(project, tmp_path):
    with pytest.raises(GuardError):
        PathGuard(project).resolve(tmp_path / "other.txt")


def test_rejects_forbidden_state_dir(project):
    state = project / "state"
    with pytest.raises(GuardError):
        PathGuard(project, forbidden=(state,)).resolve(state / "backups" / "x")


def test_sibling_prefix_is_not_inside(project, tmp_path):
    sib = tmp_path / "proj_evil"
    sib.mkdir()
    with pytest.raises(GuardError):
        PathGuard(project).resolve(sib / "f")
