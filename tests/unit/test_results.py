import numpy as np
import pytest

from carmaker_mcp.results import Erg, ErgError, list_results


def make_erg(tmp_path, n=100, name="run.erg"):
    dt = np.dtype([("Time", "<f8"), ("Car.v", "<f4"), ("Cnt", "<i4")])
    data = np.zeros(n, dtype=dt)
    data["Time"] = np.arange(n) * 0.01
    data["Car.v"] = np.linspace(0, 10, n)
    data["Cnt"] = np.arange(n)
    p = tmp_path / name
    header = b"CM-ERG\0\0" + (1).to_bytes(2, "little") + dt.itemsize.to_bytes(2, "little") + bytes(4)
    p.write_bytes(header + data.tobytes())
    (tmp_path / (name + ".info")).write_text(
        "#INFOFILE1.1\nFile.Format = erg\nFile.ByteOrder = LittleEndian\n"
        "File.At.1.Name = Time\nFile.At.1.Type = Double\nQuantity.Time.Unit = s\n"
        "File.At.2.Name = Car.v\nFile.At.2.Type = Float\nQuantity.Car.v.Unit = m/s\n"
        "File.At.3.Name = Cnt\nFile.At.3.Type = Int\n"
    )
    return p


def test_summary_and_read(tmp_path):
    e = Erg(make_erg(tmp_path))
    assert e.n_rows == 100 and e.names == ["Time", "Car.v", "Cnt"]
    s = e.summary(["Time", "Car.v"])
    assert s["duration_s"] == pytest.approx(0.99)
    assert s["quantities"]["Car.v"]["max"] == pytest.approx(10.0)
    assert s["quantities"]["Car.v"]["unit"] == "m/s"
    r = e.read(["Time", "Cnt"], t_min=0.5, max_points=10)
    assert r["rows_returned"] <= 10 and r["data"]["Time"][0] >= 0.5
    assert e.search("car") == ["Car.v"]


def test_errors(tmp_path):
    p = make_erg(tmp_path)
    with pytest.raises(ErgError):
        Erg(p).column("Nope")
    data = bytearray(p.read_bytes())
    data[10:12] = (99).to_bytes(2, "little")  # header record size no longer matches the info file
    p.write_bytes(bytes(data))
    with pytest.raises(ErgError, match="record size"):
        Erg(p)
    with pytest.raises(ErgError):
        Erg(tmp_path / "missing.erg")


def test_truncated_file_reads_complete_rows(tmp_path):
    p = make_erg(tmp_path, n=10)
    p.write_bytes(p.read_bytes()[:-5])
    e = Erg(p)
    assert e.truncated and e.n_rows == 9


def test_list_results_skips_modelcheck(tmp_path):
    make_erg(tmp_path, name="a.erg")
    mc = tmp_path / "ModelCheck"
    mc.mkdir()
    make_erg(mc, name="check.erg")
    names = [r["name"] for r in list_results([tmp_path])]
    assert names == ["a.erg"]
