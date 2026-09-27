import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mock_prusalink import serve  # noqa: E402

from octoprint_prusalink.client import PrusaLinkClient, PrusaLinkError  # noqa: E402
from octoprint_prusalink.transport import PrusaLinkSerial  # noqa: E402


@pytest.fixture
def printer():
    server, sim = serve(0, api_key="k", step=10.0)
    yield server, sim
    server.shutdown()


def make(server, **kw):
    client = PrusaLinkClient(f"127.0.0.1:{server.server_address[1]}", api_key="k", timeout=5)
    t = PrusaLinkSerial(client, read_timeout=0.2, poll_interval=kw.pop("poll_interval", 60), **kw)
    t.open()
    return t


def cmd(t, line, until="ok", timeout=5):
    t.write((line + "\n").encode())
    out, end = [], time.time() + timeout
    while time.time() < end:
        r = t.readline().decode().strip()
        if not r:
            continue
        out.append(r)
        if r == until or r.startswith(until + " "):
            return out
    raise AssertionError(f"no {until!r} after {line!r}: {out}")


def test_auth_failure(printer):
    server, _ = printer
    client = PrusaLinkClient(f"127.0.0.1:{server.server_address[1]}", api_key="wrong")
    with pytest.raises(PrusaLinkError) as e:
        PrusaLinkSerial(client).open()
    assert e.value.status_code == 401


def test_checksum_and_line_numbers_are_stripped(printer):
    t = make(printer[0])
    out = cmd(t, "N5 M105*34")
    assert out[-1].startswith("ok T:")
    t.close()


def test_file_list_select_start(printer):
    server, sim = printer
    sim.add_file("My Part v2.bgcode", b"x" * 1234)
    sim.add_file("readme.txt", b"x")
    t = make(server)
    out = cmd(t, "M20 L T")
    assert "My_Part_v2.bgcode 1234 My Part v2.bgcode" in out
    assert not any("readme" in line for line in out)

    out = cmd(t, "M23 /my_part_v2.bgcode")
    assert "File opened: My_Part_v2.bgcode Size: 1234" in out and "File selected" in out

    cmd(t, "M24")
    assert sim.state == "PRINTING" and sim.job["file"] == sim.find("My Part v2.bgcode")
    t.close()


def test_unknown_file(printer):
    t = make(printer[0])
    out = cmd(t, "M23 nope.gcode")
    assert out[0].startswith("open failed")
    t.close()


def test_unsupported_command_warns_once(printer):
    t = make(printer[0])
    out1 = cmd(t, "G28")
    out2 = cmd(t, "G28")
    assert any("not available through the PrusaLink API" in line for line in out1)
    assert out2 == ["ok"]
    t.close()


def test_cancel_sequence_stops_without_pausing(printer):
    server, sim = printer
    sim.add_file("a.gcode", b"G28\n")
    sim.step = 0.1
    t = make(server)
    sim.start("A~1.GCO")
    t._update_status(t._client.status())
    mark = len(sim.requests)
    cmd(t, "M25")
    cmd(t, "M27")
    cmd(t, "M26 S0")
    time.sleep(1.5)  # past the pause deferral
    assert sim.state == "STOPPED"
    assert not any(p.endswith("/pause") for _, p in sim.requests[mark:])
    t.close()


def test_m28_m29_fallback_upload(printer):
    server, sim = printer
    t = make(server)
    cmd(t, "M28 streamed.gcode")
    cmd(t, "N1 G28*18")
    cmd(t, "N2 G1 X10*99")
    cmd(t, "M29", until="Done saving file.")
    assert sim.files[sim.find("streamed.gcode")]["data"] == b"G28\nG1 X10\n"
    t.close()
