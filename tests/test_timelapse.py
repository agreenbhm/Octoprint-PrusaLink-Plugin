import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from octoprint_prusalink.timelapse import TimelapseSession, inject_parks  # noqa: E402

CFG = {
    "park_x": 240, "park_y": 200, "lift": 1.0, "retract": 0.8, "retract_speed": 2100,
    "travel_speed": 12000, "z_speed": 720, "dwell_ms": 2500, "marker_speed": 91,
    "every_n_layers": 1,
}

PRUSASLICER = """M107
G90
M83
G28
G1 Z0.2 F720
;LAYER_CHANGE
;Z:0.2
G1 X10 Y10 F9000
G1 X50 Y10 E2.5 F1500
G1 X50 Y50 E2.5
;LAYER_CHANGE
;Z:0.4
G1 Z0.4 F720
G1 X60 Y60 F9000
G1 X10 Y60 E2.5 F1500
;LAYER_CHANGE
;Z:0.6
G1 Z0.6 F720
G1 X20 Y20 E1
M84
"""


class Machine:
    """Minimal Marlin motion-state model: G90/G91 (incl. E), M82/M83, G92 not needed."""

    def __init__(self):
        self.pos = {"X": 0.0, "Y": 0.0, "Z": 0.0, "E": 0.0}
        self.rel = False
        self.erel = False
        self.f = None
        self.speed = 100
        self.backup = 100
        self.speeds_seen = []

    def run(self, line):
        code = line.split(";", 1)[0].strip()
        if not code:
            return
        head = code.split()[0]
        words = {k: float(v) for k, v in re.findall(r"([A-Z])([-0-9.]+)", code[len(head):])}
        if head in ("G0", "G1"):
            for ax in "XYZ":
                if ax in words:
                    self.pos[ax] = self.pos[ax] + words[ax] if self.rel else words[ax]
            if "E" in words:
                self.pos["E"] = self.pos["E"] + words["E"] if self.erel else words["E"]
            if "F" in words:
                self.f = words["F"]
        elif head == "G90":
            self.rel = self.erel = False
        elif head == "G91":
            self.rel = self.erel = True
        elif head == "M82":
            self.erel = False
        elif head == "M83":
            self.erel = True
        elif head == "M220":
            if "B" in code.split()[1:] or code.strip().endswith("B"):
                self.backup = self.speed
            if "R" in code.split()[1:] or code.strip().endswith("R"):
                self.speed = self.backup
            if "S" in words:
                self.speed = int(words["S"])
                self.speeds_seen.append(self.speed)

    def state(self):
        return ({k: round(v, 6) for k, v in self.pos.items()}, self.rel, self.erel, self.f, self.speed)


def _process(tmp_path, text, cfg=CFG):
    src, dst = tmp_path / "in.gcode", tmp_path / "out.gcode"
    src.write_text(text)
    n = inject_parks(str(src), str(dst), cfg)
    return n, dst.read_text()


def test_parks_inserted_before_layer_changes_after_first(tmp_path):
    n, out = _process(tmp_path, PRUSASLICER)
    assert n == 2
    assert out.count(";PRUSALINK_TIMELAPSE_BEGIN") == 2
    # park comes right before the marker, not after the first one
    first_marker = out.index(";LAYER_CHANGE")
    assert out.index(";PRUSALINK_TIMELAPSE_BEGIN") > first_marker


def test_park_block_is_state_neutral(tmp_path):
    """Run original and processed through the machine model: every park must leave
    position, modes, feedrate and speed exactly as before, and actually park."""
    _, out = _process(tmp_path, PRUSASLICER)
    m = Machine()
    before = None
    parked_positions = []
    for line in out.splitlines():
        if line.startswith(";PRUSALINK_TIMELAPSE_BEGIN"):
            before = m.state()
        m.run(line)
        if line.startswith("M220 S"):
            parked_positions.append((m.pos["X"], m.pos["Y"]))
        if line.startswith(";PRUSALINK_TIMELAPSE_END"):
            after = m.state()
            assert after == before, (before, after)
    assert parked_positions == [(240.0, 200.0)] * 2
    assert m.speeds_seen == [91, 91]


def test_absolute_extrusion_and_relative_xyz(tmp_path):
    text = PRUSASLICER.replace("M83\n", "M82\n").replace("E2.5", "E5").replace("E1\n", "E9\n")
    _, out = _process(tmp_path, text)
    m = Machine()
    for line in out.splitlines():
        if line.startswith(";PRUSALINK_TIMELAPSE_BEGIN"):
            before = m.state()
        m.run(line)
        if line.startswith(";PRUSALINK_TIMELAPSE_END"):
            assert m.state() == before
    # a layer change reached while in G91 is skipped rather than parked from an unknown position
    text = PRUSASLICER.replace(";LAYER_CHANGE\n;Z:0.4", "G91\n;LAYER_CHANGE\n;Z:0.4\nG90", 1)
    n, _ = _process(tmp_path, text)
    assert n == 1

    # relative XY moves are accumulated so the return move goes back to the right spot
    text = PRUSASLICER.replace("G1 X50 Y50 E2.5\n", "G1 X50 Y50 E2.5\nG91\nG1 X5 Y-5\nG90\n", 1)
    _, out = _process(tmp_path, text)
    m = Machine()
    for line in out.splitlines():
        if line.startswith(";PRUSALINK_TIMELAPSE_BEGIN"):
            before = m.state()
            assert (before[0]["X"], before[0]["Y"]) == (55.0, 45.0)
        m.run(line)
        if line.startswith(";PRUSALINK_TIMELAPSE_END"):
            assert m.state() == before
            break


def test_every_n_layers_and_cura_markers(tmp_path):
    cura = PRUSASLICER.replace(";LAYER_CHANGE", ";LAYER:0", 1).replace(";LAYER_CHANGE", ";LAYER:1", 1).replace(";LAYER_CHANGE", ";LAYER:2", 1)
    n, _ = _process(tmp_path, cura)
    assert n == 2
    n, _ = _process(tmp_path, PRUSASLICER, dict(CFG, every_n_layers=2))
    assert n == 1


def test_no_markers_no_parks(tmp_path):
    n, _ = _process(tmp_path, "G28\nG1 X10 Y10 E1\nG1 Z1\nG1 X20 Y20 E1\n")
    assert n == 0


class FakeSession(TimelapseSession):
    def __init__(self, stabilized, tmp_path):
        super().__init__("x.gcode", stabilized, 91, capture_dir=str(tmp_path))
        self.captures = 0

    def _capture_async(self):
        self.captures += 1


def st(state="PRINTING", speed=100, z=0.2):
    return {"printer": {"state": state, "speed": speed, "axis_z": z}}


def test_stabilized_trigger_once_per_park(tmp_path):
    s = FakeSession(True, tmp_path)
    for status in [st(), st(speed=91), st(speed=91), st(), st(), st(speed=91), st()]:
        s.on_status(status)
    assert s.captures == 2


def test_stabilized_ignores_pause(tmp_path):
    s = FakeSession(True, tmp_path)
    for status in [st(), st(state="PAUSED", speed=91), st(speed=91), st()]:
        s.on_status(status)
    assert s.captures == 0  # latched while paused, still at marker after resume


def test_layer_trigger_needs_stable_height(tmp_path):
    s = FakeSession(False, tmp_path)
    zs = [0.2, 0.2, 0.8, 0.4, 0.4, 0.4, 1.0, 0.6, 0.6]  # 0.8/1.0 are travel hops
    for z in zs:
        s.on_status(st(z=z))
    assert s.captures == 3  # 0.2, 0.4, 0.6
