import pytest

from carmaker_mcp.backend_mock import MockBackend
from carmaker_mcp.config import Config
from carmaker_mcp.session import Session

VEHICLE = (
    "#INFOFILE1.1 (UTF-8) - Do not remove this line!\r\n"
    "FileIdent = CarMaker-Car 14\r\n"
    "\r\n"
    "## Assembly ####\r\n"
    "Description:\r\n"
    "\tTest car.\r\n"
    "\tSecond line.\r\n"
    "\r\n"
    "Body.mass = 285\r\n"
    "Hitch.System =\r\n"
    "Body.pos = 0.1 0 0.2\r\n"
)
TESTRUN = (
    "#INFOFILE1.1 (UTF-8) - Do not remove this line!\n"
    "Vehicle = Test_Vehicle\n"
    "Road.FName = Road_A.rd5\n"
)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "Data" / "Vehicle").mkdir(parents=True)
    (root / "Data" / "TestRun" / "Sub").mkdir(parents=True)
    (root / "Data" / "TestRun" / ".Hidden").mkdir(parents=True)
    (root / "Data" / "Vehicle" / "Test_Vehicle").write_bytes(VEHICLE.encode())
    (root / "Data" / "TestRun" / "Sub" / "Run1").write_bytes(TESTRUN.encode())
    (root / "Data" / "TestRun" / ".Hidden" / "Run2").write_bytes(TESTRUN.encode())
    (root / "Data" / "TestRun" / ".tmp_x").write_bytes(b"junk")
    (root / ".git").mkdir()
    return root


def no_window(exe_name):
    from carmaker_mcp.wincapture import CaptureError

    raise CaptureError(f"no window of {exe_name} was found")


@pytest.fixture
def mock():
    return MockBackend()


@pytest.fixture
def session(project, tmp_path, mock):
    cfg = Config(project=project, state_dir=tmp_path / "state")
    s = Session(mock, cfg, session_id="t1")
    s._window_capture = no_window  # never read a real window of the machine the tests run on
    return s
