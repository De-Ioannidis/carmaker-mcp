"""Live cold start: brings up MATLAB, the model and the CarMaker GUI as far as they are missing.

    CM_LIVE=1 CM_LIVE_COLDSTART=1 CM_PROJECT=C:\\CM_Projects\\my-project CM_MODEL=generic \
        pytest -m live tests/live/test_live_coldstart.py -s

Run it three times to cover the cases: with MATLAB and CarMaker closed (starts everything; this
opens a MATLAB window that stays open), with MATLAB open but the CarMaker GUI closed, and with
everything open (must change nothing). Nothing is closed by the test.
"""

import os
import time

import pytest

from carmaker_mcp.backend_cm4sl import Cm4slBackend
from carmaker_mcp.config import Config
from carmaker_mcp.session import Session

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CM_LIVE") != "1" or os.environ.get("CM_LIVE_COLDSTART") != "1",
        reason="set CM_LIVE=1 and CM_LIVE_COLDSTART=1 (this may start MATLAB and the CarMaker GUI)",
    ),
]


def test_session_start_until_ready(tmp_path):
    cfg = Config.from_env()
    cfg.state_dir = tmp_path
    s = Session(Cm4slBackend(cfg.matlab_session, cfg.engine_timeout), cfg)
    t0 = time.time()
    while True:
        r = s.session_start(max_wait_s=45)
        print(f"\n{time.time() - t0:6.1f}s ->", r)
        if r["ready"]:
            break
        assert time.time() - t0 < 600, "the session did not come up within 10 minutes"
    st = s.status()
    print("status:", st)
    assert st["connected"] and st["sim_status"] == "idle"
    again = s.session_start(max_wait_s=45)
    print("second call:", again)
    assert again["ready"] and again["steps"]["matlab"] == "shared session found"
    assert again["steps"]["gui"] == "already open"
