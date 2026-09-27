"""
Virtual serial port that speaks just enough Marlin to keep OctoPrint's comm layer happy and
translates everything it can into PrusaLink v1 API calls.

Model: the printer's storage (USB / SD / local) is exposed to OctoPrint as its "SD card".
Prints always run from printer storage; OctoPrint never streams G-code line by line.
"""

import logging
import queue
import re
import threading
import time

from .client import PrusaLinkError

PORT_NAME = "PRUSALINK"

ACTIVE_STATES = ("PRINTING", "PAUSED", "ATTENTION", "BUSY")
MACHINECODE_EXTENSIONS = (".gcode", ".gco", ".g", ".bgcode")

# commands that PrusaLink's API offers no equivalent for (no raw G-code endpoint)
UNSUPPORTED = {
    "G0", "G1", "G2", "G3", "G28", "G29", "G30", "G80",
    "M104", "M109", "M140", "M190", "M141", "M191",
    "M106", "M107", "M17", "M18", "M84",
    "M220", "M221", "M600", "M701", "M702", "M80", "M81",
}  # fmt: skip

_line_number = re.compile(r"^N\d+\s*")
_checksum = re.compile(r"\*\d+\s*$")
_param_s = re.compile(r"\bS(\d+)")


def _sanitize_id(path):
    return re.sub(r"\s+", "_", path.strip("/"))


class PrusaLinkSerial:
    def __init__(
        self,
        client,
        read_timeout=2.0,
        poll_interval=2.0,
        storage=None,
        start_grace=45.0,
        pause_defer=1.0,
        logger=None,
    ):
        self._client = client
        self._read_timeout = read_timeout
        self._write_timeout = 10.0
        self._poll_interval = max(0.5, float(poll_interval))
        self._storage_pref = (storage or "").strip("/")
        self._start_grace = start_grace
        self._pause_defer = pause_defer
        self._logger = logger or logging.getLogger("octoprint.plugins.prusalink.transport")

        self._outgoing = queue.Queue()
        self._incoming = queue.Queue()
        self._lock = threading.RLock()
        self._closed = threading.Event()

        # printer info
        self._version = {}
        self._info = {}
        self._storage = None

        # telemetry
        self._status = {}
        self._state = None
        self._comm_failures = 0

        # files / jobs
        self._files = {}  # id -> entry
        self._selected = None  # entry
        self._job_id = None
        self._job_size = 0
        self._job_progress = 0.0
        self._time_remaining = None
        self._last_sd_current = None

        # bookkeeping to tell our own actions apart from ones done on the printer
        self._start_requested_at = None
        self._pause_requested = False
        self._resume_requested = False
        self._stop_requested = False
        self._pending_pause = None  # threading.Timer
        self._attention_reported = False
        self._sd_initialized = False
        self._pending_announce = None
        self._announced_at = None
        self._deferred_paused = False

        # M28/M29 fallback
        self._writing_file = None
        self._write_buffer = None

        self._warned = set()
        self._threads = []

    # ~~ serial-like interface used by octoprint.util.comm

    @property
    def port(self):
        return PORT_NAME

    @property
    def baudrate(self):
        return 115200

    @property
    def timeout(self):
        return self._read_timeout

    @timeout.setter
    def timeout(self, value):
        self._read_timeout = value

    @property
    def write_timeout(self):
        return self._write_timeout

    @write_timeout.setter
    def write_timeout(self, value):
        self._write_timeout = value

    @property
    def is_open(self):
        return not self._closed.is_set()

    def write(self, data):
        if self._closed.is_set():
            raise OSError("PrusaLink transport is closed")
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
        for line in text.splitlines():
            if line.strip():
                self._incoming.put(line)
        return len(data)

    def readline(self):
        try:
            line = self._outgoing.get(timeout=self._read_timeout)
        except queue.Empty:
            return b""
        return (line + "\n").encode("utf-8")

    def close(self):
        self._closed.set()
        with self._lock:
            if self._pending_pause:
                self._pending_pause.cancel()
        self._client.close()

    # ~~ lifecycle

    def open(self):
        """Verifies connectivity, fetches initial state and starts worker threads.
        Raises on failure so OctoPrint reports a connection error."""
        self._version = self._client.version() or {}
        try:
            self._info = self._client.info() or {}
        except PrusaLinkError:
            self._info = {}
        self._select_storage()
        self._update_status(self._client.status() or {}, initial=True)

        for target, name in (
            (self._command_loop, "PrusaLink command worker"),
            (self._poll_loop, "PrusaLink status poller"),
        ):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def _select_storage(self):
        try:
            storages = self._client.storage()
        except PrusaLinkError as e:
            self._logger.warning(f"Could not query storage list: {e}")
            storages = []

        candidates = [
            s for s in storages if s.get("available", True) and s.get("path")
        ]
        chosen = None
        if self._storage_pref:
            for s in candidates:
                if s["path"].strip("/") == self._storage_pref:
                    chosen = s
                    break
            if chosen is None:
                # trust the user even if the printer doesn't list it
                self._storage = self._storage_pref
                return
        if chosen is None:
            writable = [s for s in candidates if not s.get("read_only")]
            chosen = (writable or candidates or [None])[0]
        self._storage = chosen["path"].strip("/") if chosen else "usb"
        self._logger.info(f"Using printer storage /{self._storage}")

    # ~~ public helpers for the plugin

    @property
    def storage(self):
        return self._storage

    @property
    def time_remaining(self):
        with self._lock:
            return self._time_remaining if self._job_id is not None else None

    @property
    def printer_name(self):
        return self._info.get("name") or self._info.get("hostname") or "Prusa"

    def upload(self, local_path, remote_name, print_after_upload=False):
        """Uploads a local file to printer storage. Returns the file id OctoPrint
        should use (M23 name)."""
        remote_id = _sanitize_id(remote_name)
        self._client.upload_file(
            self._storage, remote_id, local_path, print_after_upload=print_after_upload
        )
        try:
            self._refresh_files()
        except PrusaLinkError as e:
            self._logger.warning(f"Could not refresh file list after upload: {e}")
        entry = self._resolve(remote_id)
        return entry["id"] if entry else remote_id

    # ~~ output

    def _send(self, *lines):
        for line in lines:
            self._outgoing.put(line)

    def _warn_once(self, key, message):
        if key in self._warned:
            return
        self._warned.add(key)
        self._send(f"// PrusaLink: {message}")
        self._logger.warning(message)

    # ~~ command processing

    def _command_loop(self):
        while not self._closed.is_set():
            try:
                line = self._incoming.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._process(line)
            except Exception as e:
                self._logger.exception(f"Error processing {line!r}")
                self._send(f"// PrusaLink: error processing {line.strip()}: {e}", "ok")

    def _process(self, raw):
        line = _checksum.sub("", _line_number.sub("", raw.strip()))

        if self._writing_file is not None:
            if line.split(None, 1)[0].upper() == "M29":
                self._finish_m28()
            else:
                self._write_buffer.append(line)
                self._send("ok")
            return

        line = line.split(";", 1)[0].strip()
        if not line:
            self._send("ok")
            return

        parts = line.split(None, 1)
        code = parts[0].upper()
        arg = parts[1].strip() if len(parts) > 1 else ""

        handler = getattr(self, f"_cmd_{code}", None)
        if handler is not None:
            handler(arg)
            return

        if code in UNSUPPORTED or re.match(r"^T\d+$", code):
            self._warn_once(
                code,
                f"{code} is not available through the PrusaLink API (no G-code "
                f"endpoint); ignored. Use the printer's display for manual control.",
            )
        self._send("ok")

    # handshake / info

    def _cmd_M110(self, arg):
        self._send("ok")

    def _cmd_M115(self, arg):
        v = self._version
        fw = v.get("firmware") or "unknown"
        self._send(
            "FIRMWARE_NAME:PrusaLink-Bridge (PrusaLink {} / FW {}) SOURCE_CODE_URL:"
            "https://github.com/agreenbhm/Octoprint-PrusaLink-Plugin PROTOCOL_VERSION:1.0 "
            "MACHINE_TYPE:{} EXTRUDER_COUNT:1".format(
                v.get("server") or v.get("api") or "?", fw, self.printer_name
            ),
            "Cap:AUTOREPORT_TEMP:1",
            "Cap:AUTOREPORT_SD_STATUS:1",
            "Cap:AUTOREPORT_POS:0",
            "Cap:EXTENDED_M20:1",
            "Cap:LFN_WRITE:0",
            "Cap:EMERGENCY_PARSER:0",
            "Cap:BUSY_PROTOCOL:0",
            "ok",
        )

    def _cmd_M105(self, arg):
        self._send("ok " + self._temp_line())

    def _cmd_M114(self, arg):
        p = self._status.get("printer", {})
        self._send(
            "X:{:.2f} Y:{:.2f} Z:{:.2f} E:0.00 Count X:0 Y:0 Z:0".format(
                p.get("axis_x") or 0.0, p.get("axis_y") or 0.0, p.get("axis_z") or 0.0
            ),
            "ok",
        )

    def _cmd_M155(self, arg):
        self._send("ok")

    # SD card emulation

    def _cmd_M21(self, arg):
        self._send("SD card ok", "ok")
        with self._lock:
            self._sd_initialized = True
            pending, self._pending_announce = self._pending_announce, None
        if pending:
            self._send(*pending)
            with self._lock:
                self._announced_at = time.monotonic()

    def _cmd_M22(self, arg):
        self._send("ok")

    def _cmd_M20(self, arg):
        try:
            self._refresh_files()
        except PrusaLinkError as e:
            self._send(f"// PrusaLink: could not list files: {e}")
        lines = ["Begin file list"]
        with self._lock:
            for entry in self._files.values():
                lines.append("{} {} {}".format(entry["id"], entry["size"], entry["display"]))
        lines += ["End file list", "ok"]
        self._send(*lines)

    def _cmd_M23(self, arg):
        name = arg.strip()
        entry = self._resolve(name)
        if entry is None:
            try:
                self._refresh_files()
            except PrusaLinkError:
                pass
            entry = self._resolve(name)
        if entry is None:
            self._send(f"open failed, File: {name}.", "ok")
            return
        with self._lock:
            self._selected = entry
        self._send(
            "File opened: {} Size: {}".format(entry["id"], entry["size"]),
            "File selected",
            "ok",
        )

    def _cmd_M24(self, arg):
        with self._lock:
            state, job_id, selected = self._state, self._job_id, self._selected
        try:
            if job_id is not None and state in ("PAUSED", "ATTENTION"):
                with self._lock:
                    self._resume_requested = True
                self._client.resume_job(job_id)
            elif job_id is not None:
                pass  # already printing
            elif selected is not None:
                with self._lock:
                    self._start_requested_at = time.monotonic()
                    self._stop_requested = False
                self._client.start_print(self._storage, selected["path"])
            else:
                self._send("// PrusaLink: no file selected")
        except PrusaLinkError as e:
            with self._lock:
                self._start_requested_at = None
                self._resume_requested = False
            self._send(f"// PrusaLink: M24 failed: {e}", "ok", "//action:cancel")
            return
        self._send("ok")

    def _cmd_M25(self, arg):
        # OctoPrint cancels SD prints with M25 + M27 + M26 S0, so pausing is deferred
        # briefly to avoid parking the head right before a stop.
        with self._lock:
            if self._pending_pause:
                self._pending_pause.cancel()
            self._pending_pause = threading.Timer(self._pause_defer, self._do_pause)
            self._pending_pause.daemon = True
            self._pending_pause.start()
        self._send("ok")

    def _do_pause(self):
        with self._lock:
            self._pending_pause = None
            job_id, state = self._job_id, self._state
            starting = self._start_requested_at is not None
        if job_id is None and starting:
            # paused right after starting, before the poller saw the job
            try:
                job = self._client.job() or {}
                job_id, state = job.get("id"), job.get("state")
            except PrusaLinkError:
                job_id = None
        if job_id is None or state != "PRINTING":
            return
        with self._lock:
            self._pause_requested = True
        try:
            self._client.pause_job(job_id)
        except PrusaLinkError as e:
            with self._lock:
                self._pause_requested = False
            self._send(f"// PrusaLink: pause failed: {e}")

    def _cmd_M26(self, arg):
        m = _param_s.search(arg)
        if m and int(m.group(1)) == 0:
            with self._lock:
                if self._pending_pause:
                    self._pending_pause.cancel()
                    self._pending_pause = None
            self._stop()
        self._send("ok")

    def _cmd_M112(self, arg):
        self._stop()
        self._send("ok")

    def _stop(self):
        with self._lock:
            job_id = self._job_id
            self._stop_requested = True
            self._start_requested_at = None
        if job_id is None:
            try:
                job = self._client.job() or {}
                job_id = job.get("id")
            except PrusaLinkError:
                job_id = None
        if job_id is not None:
            try:
                self._client.stop_job(job_id)
            except PrusaLinkError as e:
                self._send(f"// PrusaLink: stop failed: {e}")

    def _cmd_M27(self, arg):
        if _param_s.search(arg):
            self._send("ok")  # autoreport interval, we report on every poll anyway
            return
        self._send(self._sd_line(), "ok")

    def _cmd_M30(self, arg):
        name = arg.strip()
        entry = self._resolve(name)
        if entry is None:
            self._send(f"Deletion failed, File: {name}.", "ok")
            return
        try:
            self._client.delete_file(self._storage, entry["path"])
        except PrusaLinkError as e:
            self._send(f"Deletion failed, File: {name}. ({e})", "ok")
            return
        with self._lock:
            self._files.pop(entry["id"], None)
            if self._selected is entry:
                self._selected = None
        self._send(f"File deleted:{entry['id']}", "ok")

    def _cmd_M28(self, arg):
        name = arg.strip().lstrip("/")
        self._writing_file = name
        self._write_buffer = []
        self._send(f"Writing to file: {name}", "ok")

    def _finish_m28(self):
        name, data = self._writing_file, self._write_buffer
        self._writing_file, self._write_buffer = None, None
        try:
            payload = ("\n".join(data) + "\n").encode("utf-8")
            self._client.upload_bytes(self._storage, _sanitize_id(name), payload)
            self._refresh_files()
        except PrusaLinkError as e:
            self._send(f"// PrusaLink: upload of {name} failed: {e}")
        # Marlin doesn't send ok after M29; OctoPrint synthesizes one (triggerOkForM29)
        self._send("Done saving file.")

    # ~~ files

    def _refresh_files(self):
        entries = self._client.list_print_files(self._storage)
        files = {}
        for e in entries:
            display = e["display_path"]
            if not display.lower().endswith(MACHINECODE_EXTENSIONS):
                continue
            fid = _sanitize_id(display)
            if fid in files:
                fid = _sanitize_id(e["path"])
            files[fid] = {
                "id": fid,
                "path": e["path"],
                "display": display.rsplit("/", 1)[-1],
                "size": e["size"],
                "m_timestamp": e.get("m_timestamp"),
            }
        with self._lock:
            self._files = files

    def _resolve(self, name):
        key = name.strip().lstrip("/")
        if not key:
            return None
        lkey = key.lower()
        with self._lock:
            if key in self._files:
                return self._files[key]
            for entry in self._files.values():
                if lkey in (
                    entry["id"].lower(),
                    entry["path"].lower(),
                    _sanitize_id(entry["display"]).lower(),
                ):
                    return entry
        return None

    def _entry_for_job(self, job):
        f = (job or {}).get("file") or {}
        path = f.get("path") or ""
        name = f.get("name") or ""
        display = f.get("display_name") or name
        # job file path is absolute incl. storage, e.g. /usb/FOO~1.BGC
        rel = path.strip("/")
        if self._storage and rel.lower().startswith(self._storage.lower() + "/"):
            rel = rel[len(self._storage) + 1 :]
        if rel and name and not rel.endswith(name):
            rel = f"{rel}/{name}"
        rel = rel or name
        entry = self._resolve(rel) or self._resolve(display)
        if entry is None:
            entry = {
                "id": _sanitize_id(display or rel or "unknown.gcode"),
                "path": rel,
                "display": display,
                "size": f.get("size") or 0,
                "m_timestamp": f.get("m_timestamp"),
            }
        return entry

    # ~~ telemetry

    def _temp_line(self):
        p = self._status.get("printer", {})
        return "T:{:.1f} /{:.1f} B:{:.1f} /{:.1f} @:0 B@:0".format(
            p.get("temp_nozzle") or 0.0,
            p.get("target_nozzle") or 0.0,
            p.get("temp_bed") or 0.0,
            p.get("target_bed") or 0.0,
        )

    def _sd_line(self):
        with self._lock:
            if self._job_id is not None:
                total = self._job_size or 1000000
                paused = (
                    self._state != "PRINTING"
                    or self._pause_requested
                    or self._pending_pause is not None
                )
                if paused:
                    # OctoPrint treats a rising byte count while paused as an externally
                    # started print, so freeze the position until we're printing again
                    current = self._last_sd_current or 0
                else:
                    current = int(total * self._job_progress / 100.0)
                    current = min(max(current, 1), total - 1)
                    self._last_sd_current = current
                return f"SD printing byte {current}/{total}"
            if self._start_requested_at is not None:
                total = (self._selected or {}).get("size") or 1000000
                return f"SD printing byte 0/{total}"
        return "Not SD printing"

    def _poll_loop(self):
        while not self._closed.wait(self._poll_interval):
            try:
                status = self._client.status() or {}
            except PrusaLinkError as e:
                self._comm_failures += 1
                if self._comm_failures == 3:
                    self._send(f"// PrusaLink: printer unreachable: {e}")
                    self._logger.warning(f"Printer unreachable: {e}")
                continue
            except Exception:
                self._logger.exception("Unexpected error polling PrusaLink")
                continue

            if self._comm_failures >= 3:
                self._send("// PrusaLink: connection restored")
            self._comm_failures = 0

            try:
                self._update_status(status)
            except Exception:
                self._logger.exception("Error processing PrusaLink status")

            self._send(self._temp_line())
            with self._lock:
                job_running = self._job_id is not None
                if (
                    self._deferred_paused
                    and self._announced_at is not None
                    and time.monotonic() - self._announced_at >= 1.0
                ):
                    self._deferred_paused = False
                    if job_running and self._state in ("PAUSED", "ATTENTION"):
                        self._send("//action:paused")
            if job_running:
                self._send(self._sd_line())

    def _update_status(self, status, initial=False):
        printer = status.get("printer") or {}
        job = status.get("job") or {}
        state = printer.get("state")
        job_id = job.get("id")
        active = job_id is not None and state in ACTIVE_STATES

        out = []
        with self._lock:
            prev_state = self._state
            prev_job = self._job_id
            self._status = status
            self._state = state

            if active:
                self._job_progress = float(job.get("progress") or 0.0)
                self._time_remaining = job.get("time_remaining")

        if active and job_id != prev_job:
            self._on_job_started(job_id, initial, out)
        elif not active and prev_job is not None:
            self._on_job_ended(state, out)

        if active and prev_job == job_id:
            with self._lock:
                if prev_state == "PRINTING" and state in ("PAUSED", "ATTENTION"):
                    if not self._pause_requested:
                        out.append("//action:paused")
                    self._pause_requested = False
                elif prev_state in ("PAUSED", "ATTENTION") and state == "PRINTING":
                    if not self._resume_requested:
                        out.append("//action:resumed")
                    self._resume_requested = False

        if state == "ATTENTION":
            if not self._attention_reported:
                msg = (printer.get("status_printer") or {}).get("message") or ""
                out.append(f"// PrusaLink: printer needs attention {msg}".rstrip())
                self._attention_reported = True
        else:
            self._attention_reported = False

        # a print we asked for never showed up
        with self._lock:
            started = self._start_requested_at
            if (
                started is not None
                and self._job_id is None
                and time.monotonic() - started > self._start_grace
            ):
                self._start_requested_at = None
                out += [
                    "// PrusaLink: printer did not start the job (state {})".format(state),
                    "//action:cancel",
                ]

        self._send(*out)

    def _on_job_started(self, job_id, initial, out):
        try:
            job = self._client.job() or {}
        except PrusaLinkError as e:
            self._logger.warning(f"Could not fetch job info: {e}")
            job = {}
        entry = self._entry_for_job(job)

        with self._lock:
            ours = self._start_requested_at is not None
            self._job_id = job_id
            self._job_size = entry.get("size") or 0
            self._last_sd_current = None
            self._start_requested_at = None
            self._stop_requested = False
            if not ours:
                self._pause_requested = False
                self._resume_requested = False
            if ours and self._selected is not None:
                self._job_size = self._selected.get("size") or self._job_size
            else:
                self._selected = entry

        if not ours:
            # started from the printer's display, PrusaConnect, PrusaSlicer upload-and-print,
            # or already running when we connected: let OctoPrint pick it up as an
            # externally started SD print (File opened/selected + increasing byte count)
            self._logger.info(f"Detected job {job_id} started outside OctoPrint")
            total = self._job_size or 1000000
            lines = ["File opened: {} Size: {}".format(entry["id"], total), "File selected"]
            with self._lock:
                if self._state in ("PAUSED", "ATTENTION"):
                    # already paused: make OctoPrint start tracking it, then pause
                    current = min(max(int(total * self._job_progress / 100.0), 1), total - 1)
                    self._last_sd_current = current
                    lines.append(f"SD printing byte {current}/{total}")
                    # sent on a later poll, OctoPrint ignores it while still "Starting"
                    self._deferred_paused = True
            with self._lock:
                if self._sd_initialized:
                    out += lines
                    self._announced_at = time.monotonic()
                else:
                    # OctoPrint is still handshaking, announce after M21
                    self._pending_announce = lines

    def _on_job_ended(self, state, out):
        with self._lock:
            stopped_by_us = self._stop_requested
            last_progress = self._job_progress
            self._job_id = None
            self._last_sd_current = None
            self._job_progress = 0.0
            self._time_remaining = None
            self._stop_requested = False
            self._pause_requested = False
            self._resume_requested = False

        if state == "FINISHED" or (
            not stopped_by_us and state in ("IDLE", "READY") and last_progress >= 99.5
        ):
            out.append("Done printing file")
        elif not stopped_by_us:
            out += [f"// PrusaLink: job ended on printer (state {state})", "//action:cancel"]
