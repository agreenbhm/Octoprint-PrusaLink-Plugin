"""
Stabilized timelapse without G-code streaming.

PrusaLink can't inject G-code mid-print, so the parking moves are written into the file
before it is uploaded to the printer. While parked, the print speed (M220) is set to a
marker value; the status poller sees ``printer.speed == marker`` and grabs a snapshot.
Buddy firmware omits axis_x/axis_y from /api/v1/status while printing, so position can't
be used as the trigger.

Files the plugin did not process (e.g. sent to the printer by PrusaSlicer, or binary
.bgcode) can fall back to an unstabilized snapshot on each Z height change.
"""

import logging
import os
import re
import shutil
import threading
import time

import requests

LAYER_MARKERS = (";LAYER_CHANGE", ";LAYER:")
HEADER = "; processed by OctoPrint-PrusaLink stabilized timelapse"

_word = re.compile(r"([A-Za-z])\s*([-+]?[0-9]*\.?[0-9]+)")


def _fmt(v):
    return ("%.3f" % v).rstrip("0").rstrip(".")


def _park_block(cfg, layer, last_x, last_y, last_f, xyz_relative, e_relative):
    lift = _fmt(cfg["lift"])
    r = cfg["retract"]
    rf = int(cfg["retract_speed"])
    tf = int(cfg["travel_speed"])
    zf = int(cfg["z_speed"])
    out = [
        f";PRUSALINK_TIMELAPSE_BEGIN layer {layer}",
        "G91",
        "M83",
    ]
    if r > 0:
        out.append(f"G1 E-{_fmt(r)} F{rf}")
    out += [
        f"G1 Z{lift} F{zf}",
        "G90",
        f"G1 X{_fmt(cfg['park_x'])} Y{_fmt(cfg['park_y'])} F{tf}",
        "M400",
        "M220 B",
        f"M220 S{int(cfg['marker_speed'])}",
        f"G4 P{int(cfg['dwell_ms'])}",
        "M220 R",
        f"G1 X{_fmt(last_x)} Y{_fmt(last_y)} F{tf}",
        "G91",
        "M83",
        f"G1 Z-{lift} F{zf}",
    ]
    if r > 0:
        out.append(f"G1 E{_fmt(r)} F{rf}")
    out.append("G91" if xyz_relative else "G90")
    out.append("M83" if e_relative else "M82")
    if last_f:
        out.append(f"G1 F{_fmt(last_f)}")
    out.append(";PRUSALINK_TIMELAPSE_END")
    return out


def inject_parks(src, dst, cfg):
    """
    Copies G-code from ``src`` to ``dst`` with a park/dwell block before every Nth layer
    change. Returns the number of parks inserted. Layers are detected from slicer
    comments (PrusaSlicer/Orca/SuperSlicer ``;LAYER_CHANGE``, Cura ``;LAYER:``).

    cfg keys: park_x, park_y, lift, retract, retract_speed, travel_speed, z_speed,
    dwell_ms, marker_speed, every_n_layers
    """
    every = max(1, int(cfg.get("every_n_layers") or 1))
    xyz_relative = False
    e_relative = False
    x = y = None
    f = None
    layer = -1
    parks = 0
    extruded_since_marker = False

    with open(src, encoding="utf-8", errors="surrogateescape") as fin, open(
        dst, "w", encoding="utf-8", errors="surrogateescape", newline="\n"
    ) as fout:
        fout.write(HEADER + "\n")
        for raw in fin:
            stripped = raw.strip()

            if stripped.startswith(LAYER_MARKERS):
                layer += 1
                if (
                    layer > 0
                    and layer % every == 0
                    and extruded_since_marker
                    and not xyz_relative
                    and x is not None
                    and y is not None
                ):
                    fout.write(
                        "\n".join(
                            _park_block(cfg, layer, x, y, f, xyz_relative, e_relative)
                        )
                        + "\n"
                    )
                    parks += 1
                extruded_since_marker = False
                fout.write(raw)
                continue

            fout.write(raw)

            code = stripped.split(";", 1)[0].strip()
            if not code:
                continue
            head = code.split(None, 1)[0].upper()
            if head in ("G0", "G1", "G2", "G3"):
                words = {k.upper(): float(v) for k, v in _word.findall(code[len(head):])}
                if "F" in words:
                    f = words["F"]
                if not xyz_relative:
                    x = words.get("X", x)
                    y = words.get("Y", y)
                else:
                    if "X" in words and x is not None:
                        x += words["X"]
                    if "Y" in words and y is not None:
                        y += words["Y"]
                if words.get("E", 0) > 0 and ("X" in words or "Y" in words):
                    extruded_since_marker = True
            elif head == "G90":
                xyz_relative = False
                e_relative = False
            elif head == "G91":
                xyz_relative = True
                e_relative = True
            elif head == "M82":
                e_relative = False
            elif head == "M83":
                e_relative = True
            elif head == "G28":
                x = y = None
    return parks


def is_processable(path):
    return path.lower().endswith((".gcode", ".gco", ".g"))


class TimelapseSession:
    """Collects frames for one print and renders them with OctoPrint's renderer."""

    def __init__(
        self,
        name,
        stabilized,
        marker_speed,
        snapshot_url=None,
        post_roll_frames=0,
        logger=None,
        capture_dir=None,
    ):
        self.name = name
        self.stabilized = stabilized
        self._marker = int(marker_speed)
        self._snapshot_url = (snapshot_url or "").strip()
        self._post_roll = int(post_roll_frames)
        self._logger = logger or logging.getLogger("octoprint.plugins.prusalink.timelapse")

        if capture_dir is None:
            from octoprint.settings import settings

            capture_dir = settings().getBaseFolder("timelapse_tmp")
        self._capture_dir = capture_dir
        self.prefix = "{}_{}".format(os.path.splitext(os.path.basename(name))[0], time.strftime("%Y%m%d%H%M%S"))
        self._lock = threading.Lock()
        self._frames = 0
        self._errors = 0
        self._latched = False
        self._last_z = None
        self._candidate_z = None
        self._closed = False

    @property
    def frames(self):
        return self._frames

    # ~~ triggers

    def on_status(self, status):
        if self._closed:
            return
        printer = status.get("printer") or {}
        if printer.get("state") != "PRINTING":
            # paused or similar: don't count the parked head of a pause as a frame
            self._latched = printer.get("speed") == self._marker
            return

        if self.stabilized:
            parked = printer.get("speed") == self._marker
            if parked and not self._latched:
                self._capture_async()
            self._latched = parked
        else:
            z = printer.get("axis_z")
            if z is None:
                return
            # require the same new height on two polls so travel z-hops don't trigger
            if self._last_z is None or z > self._last_z + 0.04:
                if self._candidate_z is not None and abs(z - self._candidate_z) < 0.01:
                    self._last_z = z
                    self._candidate_z = None
                    self._capture_async()
                else:
                    self._candidate_z = z
            else:
                self._candidate_z = None

    # ~~ capture

    def _capture_async(self):
        threading.Thread(target=self.capture, name="PrusaLink timelapse capture", daemon=True).start()

    def _snapshot(self):
        if self._snapshot_url:
            r = requests.get(self._snapshot_url, timeout=5)
            r.raise_for_status()
            return r.content

        from octoprint.webcams import get_snapshot_webcam

        webcam = get_snapshot_webcam()
        if webcam is None:
            raise RuntimeError("no snapshot-capable webcam configured in OctoPrint")
        return b"".join(
            chunk for chunk in webcam.providerPlugin.take_webcam_snapshot(webcam.config.name) if chunk
        )

    def _frame_path(self, n):
        return os.path.join(self._capture_dir, f"{self.prefix}-{n}.jpg")

    def capture(self):
        try:
            data = self._snapshot()
        except Exception as e:
            self._errors += 1
            self._logger.warning(f"Timelapse snapshot failed: {e}")
            return False
        with self._lock:
            if self._closed:
                return False
            path = self._frame_path(self._frames)
            self._frames += 1
        with open(path, "wb") as f:
            f.write(data)
        return True

    # ~~ finish

    def finish(self, success=True):
        """Takes a final frame, adds post roll and renders. Returns True if a render was started."""
        if self._closed:
            return False
        if success:
            self.capture()
        with self._lock:
            self._closed = True
            frames = self._frames
        if frames == 0:
            self._logger.info(f"Timelapse {self.prefix}: no frames captured, nothing to render")
            return False

        if success and self._post_roll > 0:
            last = self._frame_path(frames - 1)
            for i in range(self._post_roll):
                shutil.copyfile(last, self._frame_path(frames + i))

        self._logger.info(
            f"Rendering timelapse {self.prefix} from {frames} frames ({self._errors} capture errors)"
        )
        self._render(postfix=None if success else "-fail")
        return True

    def _render(self, postfix=None):
        from octoprint import timelapse as tl
        from octoprint.settings import settings
        from octoprint.webcams import get_snapshot_webcam

        s = settings()
        webcam = get_snapshot_webcam()
        cfg = webcam.config if webcam else None
        job = tl.TimelapseRenderJob(
            self._capture_dir,
            s.getBaseFolder("timelapse"),
            self.prefix,
            postfix=postfix,
            fps=s.getInt(["webcam", "timelapse", "fps"]) or 25,
            threads=s.get(["webcam", "ffmpegThreads"]),
            videocodec=s.get(["webcam", "ffmpegVideoCodec"]),
            watermark=s.getBoolean(["webcam", "watermark"]),
            flipH=bool(cfg and cfg.flipH),
            flipV=bool(cfg and cfg.flipV),
            rotate=bool(cfg and cfg.rotate90),
            on_start=tl._create_render_start_handler(self.prefix, gcode=self.name),
            on_success=tl._create_render_success_handler(self.prefix, gcode=self.name),
            on_fail=tl._create_render_fail_handler(self.prefix, gcode=self.name),
            on_always=tl._create_render_always_handler(self.prefix, gcode=self.name),
        )
        job.process()

    def discard(self):
        with self._lock:
            self._closed = True
