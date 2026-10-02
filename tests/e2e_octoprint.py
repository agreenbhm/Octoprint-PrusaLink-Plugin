"""
End-to-end check: real OctoPrint server + this plugin + mock PrusaLink.

    python tests/e2e_octoprint.py --basedir /tmp/opbase

The basedir must contain a users.yaml with user "admin" whose apikey is "e2ekey"
(create with ``octoprint --basedir X user add admin --password admin --admin`` and set
``apikey: e2ekey``). The OctoPrint executable is taken from the same venv as python.
"""

import argparse
import os
import subprocess
import sys
import time

import requests
import yaml

sys.path.insert(0, os.path.dirname(__file__))
from mock_prusalink import serve  # noqa: E402

API_KEY = "e2ekey"
OP_PORT = 5099


def wait_for(fn, timeout=60, interval=0.5, what="condition"):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timeout waiting for {what} (last={last!r})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--basedir", required=True)
    args = ap.parse_args()

    server, sim = serve(0, api_key="plkey", step=20.0)
    pl_port = server.server_address[1]
    # a file that already exists on the printer, with a space in its long name
    sim.add_file("Existing Part.bgcode", b"\x00" * 5000)

    cfg_path = os.path.join(args.basedir, "config.yaml")
    cfg = {}
    if os.path.exists(cfg_path):
        cfg = yaml.safe_load(open(cfg_path)) or {}
    cfg.setdefault("server", {})["firstRun"] = False
    cfg["server"]["onlineCheck"] = {"enabled": False}
    cfg["server"]["pluginBlacklist"] = {"enabled": False}
    cfg.setdefault("plugins", {})["prusalink"] = {
        "host": f"127.0.0.1:{pl_port}",
        "api_key": "plkey",
        "poll_interval": 1.0,
    }
    cfg["plugins"]["tracking"] = {"enabled": False}
    cfg.setdefault("serial", {})["log"] = True
    ffmpeg = _find_ffmpeg()
    if ffmpeg:
        cfg.setdefault("webcam", {})["ffmpeg"] = ffmpeg
        cfg["webcam"].setdefault("timelapse", {})["fps"] = 10
        sim.snapshot = subprocess.run(
            [ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=64x48",
             "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-"],
            check=True, capture_output=True).stdout
    cfg["plugins"]["_disabled"] = ["tracking", "announcements", "softwareupdate", "pluginmanager"]
    yaml.safe_dump(cfg, open(cfg_path, "w"))

    octoprint = os.path.join(os.path.dirname(sys.executable), "octoprint")
    log = open(os.path.join(args.basedir, "e2e_server.log"), "w")
    proc = subprocess.Popen(
        [octoprint, "serve", "--basedir", args.basedir, "--port", str(OP_PORT), "--host", "127.0.0.1"] + (["--iknowwhatimdoing"] if os.geteuid() == 0 else []),
        stdout=log, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{OP_PORT}"
    s = requests.Session()
    s.headers["X-Api-Key"] = API_KEY

    def state():
        r = s.get(base + "/api/job")
        return r.json() if r.ok else None

    try:
        wait_for(lambda: _ok(s, base + "/api/version"), 90, what="OctoPrint up")

        ports = s.get(base + "/api/connection").json()["options"]["ports"]
        assert "PRUSALINK" in ports, ports
        print("port listed:", ports)

        r = s.post(base + "/api/connection", json={"command": "connect", "port": "PRUSALINK", "baudrate": 115200})
        assert r.status_code == 204, r.text
        wait_for(lambda: state() and state()["state"].startswith("Operational"), 30, what="Operational")
        print("connected: Operational")

        # temperatures come through
        temps = wait_for(lambda: s.get(base + "/api/printer").json().get("temperature", {}).get("tool0"), 15, what="temps")
        print("temps:", temps)

        # printer files appear as SD files, bgcode accepted, long name preserved
        s.post(base + "/api/printer/sd", json={"command": "refresh"})
        sd = wait_for(lambda: s.get(base + "/api/files/sdcard").json().get("files"), 15, what="sd files")
        names = [f["name"] for f in sd]
        print("sd files:", [(f["name"], f.get("display")) for f in sd])
        assert "Existing_Part.bgcode" in names, names

        # 1) print a file from OctoPrint's local storage -> redirected upload + print
        gcode = b"G28\nG1 X10 Y10\n" * 200
        r = s.post(base + "/api/files/local",
                   files={"file": ("cube test.gcode", gcode)}, data={"select": "true", "print": "true"})
        assert r.status_code == 201, r.text
        wait_for(lambda: sim.find("cube_test.gcode"), 30, what="upload reached printer")
        print("redirected upload reached printer")
        wait_for(lambda: state()["state"].startswith("Printing"), 30, what="Printing")
        job = state()
        print("printing:", job["job"]["file"]["origin"], job["job"]["file"]["name"], job["progress"])
        assert job["job"]["file"]["origin"] == "sdcard"
        wait_for(lambda: state()["state"].startswith("Operational") and (state()["progress"]["completion"] or 0) >= 99, 60, what="finished")
        print("finished:", state()["progress"])
        with sim.lock:
            sim.state = "IDLE"

        # 2) pause / resume / cancel from OctoPrint
        r = s.post(base + "/api/files/sdcard/Existing_Part.bgcode", json={"command": "select", "print": True})
        assert r.status_code == 204, r.text
        wait_for(lambda: state()["state"].startswith("Printing"), 30, what="Printing 2")
        sim.step = 0.5
        s.post(base + "/api/job", json={"command": "pause", "action": "pause"})
        wait_for(lambda: sim.state == "PAUSED", 15, what="printer paused")
        wait_for(lambda: state()["state"] == "Paused", 15, what="OctoPrint paused")
        print("paused ok")
        s.post(base + "/api/job", json={"command": "pause", "action": "resume"})
        wait_for(lambda: sim.state == "PRINTING", 15, what="printer resumed")
        wait_for(lambda: state()["state"].startswith("Printing"), 15, what="OctoPrint resumed")
        print("resumed ok")
        try:
            eta = wait_for(lambda: state()["progress"]["printTimeLeftOrigin"] == "estimate" and state()["progress"], 15, what="printer ETA")
        except AssertionError:
            print("DEBUG", state(), sim.state, sim.job)
            raise
        print("eta from printer:", eta["printTimeLeft"])
        mark = len(sim.requests)
        s.post(base + "/api/job", json={"command": "cancel"})
        wait_for(lambda: sim.state == "STOPPED", 15, what="printer stopped")
        wait_for(lambda: state()["state"].startswith("Operational"), 30, what="OctoPrint operational after cancel")
        assert not any(m == "PUT" and p.endswith("/pause") for m, p in sim.requests[mark:]), \
            "cancel should not pause first"
        print("cancel ok")

        # 3) print started on the printer itself is picked up
        with sim.lock:
            sim.state = "IDLE"
        sim.step = 1.0
        sim.start(sim.find("Existing Part.bgcode"))
        wait_for(lambda: state()["state"].startswith("Printing"), 30, what="external print detected")
        print("external print detected:", state()["job"]["file"]["name"])
        # 4) paused on the printer
        with sim.lock:
            sim.state = "PAUSED"
        wait_for(lambda: state()["state"] == "Paused", 15, what="external pause")
        with sim.lock:
            sim.state = "PRINTING"
        wait_for(lambda: state()["state"].startswith("Printing"), 15, what="external resume")
        # 5) stopped on the printer
        with sim.lock:
            sim.state = "STOPPED"
        wait_for(lambda: state()["state"].startswith("Operational"), 30, what="external stop")
        print("external pause/resume/stop ok")

        # 6) upload directly to "SD" (printer storage) via OctoPrint API
        r = s.post(base + "/api/files/sdcard", files={"file": ("direct.gcode", b"G28\n" * 10)})
        assert r.status_code == 201, r.text
        wait_for(lambda: sim.find("direct.gcode"), 30, what="sd upload")
        print("sd upload ok")

        # 7) connecting while the printer is already printing / paused
        for pre_state, expect in (("PRINTING", "Printing"), ("PAUSED", "Paused")):
            s.post(base + "/api/connection", json={"command": "disconnect"})
            wait_for(lambda: state()["state"] in ("Offline", "Closed"), 15, what="disconnected")
            with sim.lock:
                sim.state = "IDLE"
                sim.job = None
            sim.step = 0.2
            sim.start(sim.find("direct.gcode"))
            with sim.lock:
                sim.job["progress"] = 40.0
                sim.state = pre_state
            s.post(base + "/api/connection", json={"command": "connect", "port": "PRUSALINK", "baudrate": 115200})
            wait_for(lambda: state()["state"].startswith(expect), 30, what=f"picked up {pre_state} job on connect")
            print(f"connect while {pre_state}: {state()['state']} {state()['job']['file']['name']}")

        # 8) stabilized timelapse: parks injected at upload, frames captured while parked,
        #    rendered by OctoPrint into the timelapse folder
        s.post(base + "/api/connection", json={"command": "disconnect"})
        wait_for(lambda: state()["state"] in ("Offline", "Closed"), 15, what="disconnected")
        with sim.lock:
            sim.state = "IDLE"
            sim.job = None
        sim.layer_time = 0.6
        r = s.post(base + "/api/settings", json={"plugins": {"prusalink": {
            "timelapse_mode": "stabilized",
            "timelapse_snapshot_url": f"http://127.0.0.1:{pl_port}/snapshot.jpg",
            "park_dwell_ms": 1500,
            "timelapse_post_roll": 0,
        }}})
        assert r.ok, r.text
        s.post(base + "/api/connection", json={"command": "connect", "port": "PRUSALINK", "baudrate": 115200})
        wait_for(lambda: state()["state"].startswith("Operational"), 30, what="Operational for timelapse")

        layers = 6
        gcode = "G90\nM83\nG28\nG1 Z0.2 F720\n"
        for i in range(layers):
            z = round(0.2 * (i + 1), 2)
            gcode += f";LAYER_CHANGE\n;Z:{z}\nG1 Z{z} F720\nG1 X10 Y10 F9000\nG1 X50 Y10 E2 F1500\nG1 X50 Y50 E2\n"
        snaps_before = sim.snapshot_count
        r = s.post(base + "/api/files/local", files={"file": ("tl test.gcode", gcode.encode())},
                   data={"select": "true", "print": "true"})
        assert r.status_code == 201, r.text
        wait_for(lambda: sim.find("tl_test.gcode"), 30, what="timelapse upload")
        uploaded = sim.files[sim.find("tl_test.gcode")]["data"].decode()
        assert uploaded.count(";PRUSALINK_TIMELAPSE_BEGIN") == layers - 1, uploaded
        wait_for(lambda: state()["state"].startswith("Printing"), 30, what="timelapse print started")
        wait_for(lambda: state()["state"].startswith("Operational"), 60, what="timelapse print done")
        frames = sim.snapshot_count - snaps_before
        print(f"timelapse frames captured: {frames} (parks: {layers - 1}, +1 final)")
        assert frames == layers, frames
        if ffmpeg:
            movie = wait_for(
                lambda: [f for f in s.get(base + "/api/timelapse").json()["files"]
                         if f["name"].startswith("tl_test") and f["name"].endswith(".mp4")],
                60, what="rendered timelapse")
            print("timelapse rendered:", movie[0]["name"], movie[0]["size"])
        else:
            print("ffmpeg not found, skipped render check")

        print("E2E PASSED")
    finally:
        proc.terminate()
        proc.wait(20)
        server.shutdown()


def _find_ffmpeg():
    import shutil

    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _ok(s, url):
    try:
        return s.get(url, timeout=2).ok
    except requests.RequestException:
        return False


if __name__ == "__main__":
    main()
