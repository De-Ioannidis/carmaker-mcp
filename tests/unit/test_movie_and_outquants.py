"""Output quantities (what runs write to result files) and pictures from IPGMovie."""

import asyncio
import json

import numpy as np
import pytest
from fastmcp import Client

from carmaker_mcp import outquants, server, wincapture
from carmaker_mcp.backend import BackendError
from carmaker_mcp.infofile import InfoFile
from carmaker_mcp.movie import MISSING_FILE, missing_quantities
from carmaker_mcp.results import Erg

OUTQUANTS = (
    "#INFOFILE1.1 (UTF-8) - Do not remove this line!\r\n"
    "FileIdent = CarMaker-OutputQuantities 1\r\n"
    "\r\n"
    "DStore.Format = erg\r\n"
    "DStore.dt.fast = 0.001\r\n"
    "DStore.dt.normal = 0.01\r\n"
    "DStore.dt.slow = 0.1\r\n"
    "\r\n"
    "DStore.Quantities.fast:\r\n"
    "DStore.Quantities.normal:\r\n"
    "\tCar.Fx*\r\n"
    "\tCar.v\r\n"
    "\tTime\r\n"
    "DStore.Quantities.slow:\r\n"
)


@pytest.fixture
def config_file(project):
    f = project / "Data" / "Config" / "OutputQuantities"
    f.parent.mkdir(parents=True)
    f.write_bytes(OUTQUANTS.encode())
    return f


def make_erg(folder, names, name="run.erg"):
    dt = np.dtype([(n, "<f8") for n in names])
    data = np.zeros(5, dtype=dt)
    p = folder / name
    p.write_bytes(b"CM-ERG\0\0" + (1).to_bytes(2, "little") + dt.itemsize.to_bytes(2, "little") + bytes(4)
                  + data.tobytes())
    info = "#INFOFILE1.1\nFile.Format = erg\nFile.ByteOrder = LittleEndian\n"
    for i, n in enumerate(names, 1):
        info += f"File.At.{i}.Name = {n}\nFile.At.{i}.Type = Double\n"
    (folder / (name + ".info")).write_text(info)
    return p


# ---- output quantities -----------------------------------------------------------------------
def test_read_output_quantities(session, config_file):
    r = session.output_quantities()
    assert r["file"] == "Data/Config/OutputQuantities" and r["format"] == "erg"
    assert r["sample_time_s"] == {"fast": 0.001, "normal": 0.01, "slow": 0.1}
    assert r["quantities"] == {"fast": [], "normal": ["Car.Fx*", "Car.v", "Time"], "slow": []}
    assert r["count"] == 3 and r["movie_replay"] is False


def test_add_and_remove_keep_the_rest_of_the_file(session, config_file, mock):
    r = session.output_quantities_edit(add=["Car.az", "Car.v", "PT.Motor*.Trq"], remove=["Time", "Nope"])
    assert r["changed"] and r["added"] == ["Car.az", "PT.Motor*.Trq"] and r["already_listed"] == ["Car.v"]
    assert r["removed"] == ["Time"] and r["not_listed"] == ["Nope"] and r["count"] == 4
    text = config_file.read_bytes().decode()
    assert "DStore.Quantities.normal:\r\n\tCar.Fx*\r\n\tCar.v\r\n\tCar.az\r\n\tPT.Motor*.Trq\r\n" in text
    assert text.startswith(OUTQUANTS[: OUTQUANTS.index("DStore.Quantities.normal")])  # byte for byte
    assert "DStore.Quantities.fast:\r\nDStore.Quantities.normal:" in text  # the empty list stays empty

    r = session.output_quantities_edit(add=["Car.Jerk"], rate="fast")
    assert InfoFile.load(config_file).get("DStore.Quantities.fast") == "Car.Jerk"
    r = session.output_quantities_edit(remove=["Car.Jerk"])  # the last entry of a list
    assert outquants.lists(InfoFile.load(config_file))["fast"] == []
    assert session.output_quantities_edit(add=["Car.v"])["changed"] is False

    session.revert_all()
    assert config_file.read_bytes() == OUTQUANTS.encode()


def test_movie_preset_and_checks(session, config_file, mock):
    r = session.output_quantities_edit(preset="movie")
    assert "Vhcl.Fr1.x" in r["added"] and "Vhcl.RR.rot" in r["added"] and len(r["added"]) == 50
    assert session.output_quantities()["movie_replay"] is True
    for bad in ({"add": ["Car v"]}, {"add": ["x; exec"]}, {"add": [""]}, {"preset": "all"}, {},
                {"add": ["Car.v"], "rate": "medium"}):
        with pytest.raises((BackendError, outquants.OutQuantsError)):
            session.output_quantities_edit(**bad)
    mock.sim_status = 3
    with pytest.raises(BackendError, match="while the simulation state is 'running'"):
        session.output_quantities_edit(add=["Car.az"])


def test_wildcards_count_for_the_replay_check(session, config_file):
    session.output_quantities_edit(add=["Vhcl.*"])
    assert session.output_quantities()["movie_replay"] is True


def test_the_active_file_is_the_one_the_gui_names(session, config_file, mock):
    other = config_file.with_name("MyQuantities")
    other.write_bytes(OUTQUANTS.encode())
    mock.outquants_file = "MyQuantities"
    assert session.output_quantities_edit(add=["Car.az"])["file"] == "Data/Config/MyQuantities"
    assert config_file.read_bytes() == OUTQUANTS.encode()
    mock.outquants_file = "Gone"
    with pytest.raises(BackendError, match="no output-quantities file"):
        session.output_quantities()


def test_result_search_understands_wildcards(tmp_path):
    e = Erg(make_erg(tmp_path, ["Time", "Car.v", "PT.WFL.rotv", "PT.WFR.rotv"]))
    assert e.search("PT.W*") == ["PT.WFL.rotv", "PT.WFR.rotv"]
    assert e.search("pt.w") == ["PT.WFL.rotv", "PT.WFR.rotv"] and e.search("*") == e.names
    assert e.search("car.?") == ["Car.v"] and e.search("*", limit=2) == ["Time", "Car.v"]


# ---- IPGMovie ----------------------------------------------------------------------------
def test_open_and_picture_of_the_last_run(session, mock):
    with pytest.raises(BackendError, match="IPGMovie is not open. Call cm_movie_open before the run"):
        session.movie_snapshot()
    r = session.movie_open()
    assert r["open"] and r["started"] and "Bird's Eye View" in r["cameras"]
    assert session.movie_open()["started"] is False

    r = session.movie_snapshot(time_s=12.5, camera="Bird's Eye View", width=640, height=360)
    assert r["time_s"] == 12.5 and r["camera"] == "Bird's Eye View" and r["source"] == "the last run"
    assert open(r["file"], "rb").read().startswith(b"\xff\xd8") and "note" not in r
    export = [c for _, c in mock.calls if c.startswith("Movie export window")][-1]
    assert "-start 12.5 -end 12.5 -width 640 -height 360 -format jpeg -overwrite -async" in export
    assert session.movie_snapshot()["time_s"] == "latest"

    with pytest.raises(BackendError, match="IPGMovie has no camera 'Roof'. Built in: DEFAULT"):
        session.movie_snapshot(camera="Roof")


def test_time_without_data_is_an_error_not_a_hang(session, mock):
    session.movie_open()
    session._movie._sleep = lambda s: None
    mock.movie_recorded_s = 30.0
    with pytest.raises(BackendError, match=r"wrote no picture for t = 60 s.*must be open during the run"):
        session._movie.export(60.0, 320, 180, timeout_s=0.3)


def test_picture_during_a_run_is_read_from_the_window(session, mock):
    """IPGMovie's export would freeze it (seen live), so the window's pixels are taken instead."""
    session.movie_open()
    mock.polls_until_idle = 10_000
    session.start_sim()
    asked = []

    def window(exe_name):
        asked.append(exe_name)
        return wincapture.png(bytes(4 * 20 * 16), 20, 16), 20, 16

    session._window_capture = window
    r = session.movie_snapshot(camera="Left Side")
    assert asked == ["Movie.exe"] and r["file"].endswith(".png") and (r["width"], r["height"]) == (20, 16)
    assert r["time_s"] == 3.0 and r["follows_simulation"] is True and r["camera"] == "Left Side"
    assert "keeps following" in r["note"] and open(r["file"], "rb").read().startswith(b"\x89PNG")
    assert not [c for c in mock.calls if c[0] == "tcl" and c[1].startswith("Movie export window")]

    session.stop_sim(wait_s=2)  # after the run the export is used again
    assert session.movie_snapshot(time_s=1)["file"].endswith(".jpg") and asked == ["Movie.exe"]


def test_without_a_window_or_with_a_time_the_export_is_used_and_says_what_it_costs(session, mock):
    session.movie_open()
    mock.polls_until_idle = 10_000
    session.start_sim()
    r = session.movie_snapshot()  # the test session finds no window
    assert r["file"].endswith(".jpg") and "no window of Movie.exe" in r["capture_error"]
    assert "has stopped following and recording this run" in r["note"] and r["follows_simulation"] is False
    session.stop_sim(wait_s=2)

    session.start_sim()
    session._window_capture = lambda exe: pytest.fail("a time was asked for: not the window")
    assert "has stopped following" in session.movie_snapshot(time_s=1.0)["note"]


def test_png_encoding():
    import struct
    import zlib

    bgra = bytes([10, 20, 30, 255, 40, 50, 60, 255])  # two pixels in one row, blue first
    data = wincapture.png(bgra, 2, 1)
    assert data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR"
    assert struct.unpack(">IIBB", data[16:26]) == (2, 1, 8, 2)
    start = data.index(b"IDAT") + 4
    length = struct.unpack(">I", data[start - 8:start - 4])[0]
    assert zlib.decompress(data[start:start + length]) == bytes([0, 30, 20, 10, 60, 50, 40])  # red first


def test_result_file_without_motion_is_refused_before_ipgmovie_warns(session, mock, project):
    erg = make_erg(project, ["Time", "Car.v", "Car.Fr1.tx"])
    with pytest.raises(BackendError) as e:
        session.movie_snapshot(erg=str(erg))
    assert "cannot be replayed in IPGMovie" in str(e.value) and "preset='movie'" in str(e.value)
    assert mock.movie_loaded == "" and not mock.movie_open  # nothing was sent to IPGMovie


def test_result_file_is_loaded_once_and_missing_quantities_are_reported(session, mock, project):
    erg = make_erg(project, ["Time", *outquants.MOVIE_CORE])
    real = mock.gui_tcl

    def gui(command, timeout_ms=10000):
        if command.startswith("Movie loadsimdata"):  # IPGMovie notes what it misses
            with open(project / MISSING_FILE, "a", newline="") as f:
                f.write("\r\n---\r\nDate:\r\n\tnow\r\nTestrun:\r\n\tSub/Run1\r\nMotion Data:\r\n"
                        f"\t{erg.as_posix()}\r\nMissing Quantitites:\r\n"
                        "\tVhcl.Distance   Vhcl.FR.Fz      Vhcl.RL.FyVhcl.RR.rot\r\n\tVhcl.FL.Fx      \r\n")
        return real(command, timeout_ms)

    mock.gui_tcl = gui
    session._movie_ctl()._sleep = lambda s: None
    (project / MISSING_FILE).write_text("---\nMotion Data:\n\tother.erg\nMissing Quantitites:\n\tOld.One\n")
    r = session.movie_snapshot(time_s=1, erg=str(erg))
    assert r["source"] == "run.erg" and mock.movie_loaded == erg.as_posix()
    assert r["missing_quantities"] == ["Vhcl.Distance", "Vhcl.FR.Fz", "Vhcl.RL.Fy", "Vhcl.RR.rot", "Vhcl.FL.Fx"]
    assert "IPGMovie showed a warning" in r["note"]
    session.movie_snapshot(time_s=2, erg=str(erg))
    assert sum(c.startswith("Movie loadsimdata") for _, c in mock.calls) == 1

    session.start_sim()  # IPGMovie follows the new run again
    assert session.movie_snapshot()["source"] == "the last run"
    assert missing_quantities(project, erg) != [] and missing_quantities(project / "x", erg) == []


def test_only_the_newest_pictures_are_kept(session, mock):
    session.movie_open()
    for _ in range(23):
        last = session.movie_snapshot()["file"]
    kept = sorted((session.cfg.state_dir / "movie").glob("*.jpg"))
    assert len(kept) == 20 and last in map(str, kept)


# ---- through the protocol ------------------------------------------------------------------
def test_tools_return_the_picture_as_an_image(session, config_file):
    server.set_session(session)
    mcp = server.create_server()

    async def go():
        async with Client(mcp) as c:
            await c.call_tool("cm_movie_open", {})
            pic = await c.call_tool("cm_movie_snapshot", {"time_s": 3})
            quants = await c.call_tool("cm_output_quantities_edit", {"add": ["Car.az"]})
            listed = await c.call_tool("cm_output_quantities", {})
            bad = await c.call_tool("cm_output_quantities_edit", {"add": ["a b"]}, raise_on_error=False)
            return pic, quants, listed, bad

    try:
        pic, quants, listed, bad = asyncio.run(go())
    finally:
        server.set_session(None)
    kinds = [type(b).__name__ for b in pic.content]
    assert kinds == ["TextContent", "ImageContent"] and json.loads(pic.content[0].text)["time_s"] == 3
    assert "image/jpeg" in pic.content[1].model_dump().values()  # the field's name depends on the SDK
    assert quants.structured_content["added"] == ["Car.az"]
    assert "Car.az" in listed.structured_content["quantities"]["normal"]
    assert bad.is_error and "not a quantity name" in bad.content[0].text
