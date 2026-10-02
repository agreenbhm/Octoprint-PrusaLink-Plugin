import json
import os
import tempfile
import threading
import time

import flask
import octoprint.plugin
from octoprint.access.permissions import Permissions
from octoprint.filemanager import ContentTypeMapping
from octoprint.events import Events
from octoprint.filemanager.destinations import FileDestinations

from .client import PrusaLinkClient, PrusaLinkError
from .timelapse import TimelapseSession, inject_parks, is_processable
from .transport import PORT_NAME, PrusaLinkSerial

TIMELAPSE_END_EVENTS = (Events.PRINT_DONE, Events.PRINT_FAILED, Events.PRINT_CANCELLED)


class PrusaLinkPlugin(
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.EventHandlerPlugin,
):
    def __init__(self):
        self._transport = None
        self._transport_lock = threading.Lock()
        self._timelapse = None
        self._timelapse_lock = threading.Lock()
        self._normal_poll_interval = None
        self._processed_lock = threading.Lock()

    # ~~ SettingsPlugin

    def get_settings_defaults(self):
        return {
            "host": "",
            "username": "maker",
            "password": "",
            "api_key": "",
            "use_https": False,
            "verify_tls": True,
            "storage": "",
            "poll_interval": 2.0,
            "request_timeout": 10.0,
            "redirect_local_prints": True,
            # timelapse
            "timelapse_mode": "off",  # off | stabilized | layer
            "timelapse_fallback_layer": True,
            "timelapse_snapshot_url": "",
            "timelapse_poll_interval": 0.5,
            "timelapse_post_roll": 1.0,
            "park_x": None,
            "park_y": None,
            "park_lift": 1.0,
            "park_retract": 0.8,
            "park_retract_speed": 2100,
            "park_travel_speed": 12000,
            "park_z_speed": 720,
            "park_dwell_ms": 2500,
            "park_marker_speed": 91,
            "park_every_n_layers": 1,
        }

    def get_settings_restricted_paths(self):
        return {"admin": [["password"], ["api_key"]]}

    # ~~ TemplatePlugin / AssetPlugin

    def is_template_autoescaped(self):
        return True

    def get_template_configs(self):
        return [{"type": "settings", "custom_bindings": True}]

    def get_assets(self):
        return {"js": ["js/prusalink.js"]}

    # ~~ SimpleApiPlugin: connection test from the settings dialog

    def is_api_protected(self):
        return True

    def get_api_commands(self):
        return {"test": []}

    def on_api_command(self, command, data):
        if not Permissions.SETTINGS.can():
            flask.abort(403)
        if command != "test":
            return None

        cfg = {k: data.get(k, self._settings.get([k])) for k in self.get_settings_defaults()}
        # an empty password in the form means "keep the stored one"
        for secret in ("password", "api_key"):
            if not cfg.get(secret):
                cfg[secret] = self._settings.get([secret])
        try:
            client = self._client_from(cfg)
            version = client.version() or {}
            status = client.status() or {}
            storage = client.storage()
            client.close()
        except (PrusaLinkError, ValueError) as e:
            return flask.jsonify(ok=False, error=str(e))
        return flask.jsonify(
            ok=True,
            printer=version.get("printer"),
            firmware=version.get("firmware"),
            state=(status.get("printer") or {}).get("state"),
            storage=[s.get("path") for s in storage if s.get("available", True)],
        )

    # ~~ helpers

    def _client_from(self, cfg):
        return PrusaLinkClient(
            cfg.get("host"),
            username=cfg.get("username") or "maker",
            password=cfg.get("password") or "",
            api_key=cfg.get("api_key") or "",
            use_https=bool(cfg.get("use_https")),
            verify_tls=bool(cfg.get("verify_tls", True)),
            timeout=float(cfg.get("request_timeout") or 10.0),
        )

    def _cfg(self):
        return {k: self._settings.get([k]) for k in self.get_settings_defaults()}

    def active_transport(self):
        t = self._transport
        if t is not None and t.is_open:
            return t
        return None

    # ~~ uploads (with optional timelapse preprocessing)

    def _upload(self, transport, local_path, remote_name):
        """Uploads to printer storage, inserting timelapse parks first if enabled.
        Returns the remote file id."""
        tmp = None
        upload_path = local_path
        parks = 0
        if self._settings.get(["timelapse_mode"]) == "stabilized" and is_processable(remote_name):
            fd, tmp = tempfile.mkstemp(suffix=".gcode", prefix="prusalink_tl_")
            os.close(fd)
            try:
                parks = inject_parks(local_path, tmp, self._park_config())
                if parks:
                    upload_path = tmp
                    self._logger.info(f"Inserted {parks} timelapse parks into {remote_name}")
                else:
                    self._logger.info(
                        f"No layer change markers found in {remote_name}, uploading unmodified"
                    )
            except Exception:
                self._logger.exception(f"Timelapse preprocessing of {remote_name} failed, uploading unmodified")
        try:
            remote_id = transport.upload(upload_path, remote_name)
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        self._mark_processed(remote_id, parks > 0)
        return remote_id

    def _park_config(self):
        g = self._settings.get
        park_x, park_y = g(["park_x"]), g(["park_y"])
        if park_x in (None, "") or park_y in (None, ""):
            dx, dy = self._default_park()
            park_x = dx if park_x in (None, "") else park_x
            park_y = dy if park_y in (None, "") else park_y
        return {
            "park_x": float(park_x),
            "park_y": float(park_y),
            "lift": float(g(["park_lift"])),
            "retract": float(g(["park_retract"])),
            "retract_speed": float(g(["park_retract_speed"])),
            "travel_speed": float(g(["park_travel_speed"])),
            "z_speed": float(g(["park_z_speed"])),
            "dwell_ms": int(g(["park_dwell_ms"])),
            "marker_speed": int(g(["park_marker_speed"])),
            "every_n_layers": int(g(["park_every_n_layers"]) or 1),
        }

    def _default_park(self):
        """Back right corner, 10 mm in, from the active printer profile."""
        try:
            vol = self._printer_profile_manager.get_current_or_default()["volume"]
            w, d = float(vol["width"]), float(vol["depth"])
            if vol.get("origin") == "center":
                return w / 2 - 10, d / 2 - 10
            return w - 10, d - 10
        except Exception:
            return 240.0, 200.0

    def _processed_file(self):
        return os.path.join(self.get_plugin_data_folder(), "timelapse_files.json")

    def _load_processed(self):
        try:
            with open(self._processed_file()) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _mark_processed(self, remote_id, processed):
        key = remote_id.strip("/").lower()
        with self._processed_lock:
            data = self._load_processed()
            if processed:
                data[key] = int(time.time())
            elif key in data:
                del data[key]  # overwritten with an unprocessed version
            else:
                return
            with open(self._processed_file(), "w") as f:
                json.dump(data, f)

    def _is_processed(self, path):
        key = (path or "").strip("/").lower()
        with self._processed_lock:
            return key in self._load_processed()

    # ~~ EventHandlerPlugin: timelapse lifecycle

    def on_event(self, event, payload):
        if event == Events.PRINT_STARTED:
            self._start_timelapse(payload or {})
        elif event in TIMELAPSE_END_EVENTS or event == Events.DISCONNECTED:
            self._stop_timelapse(success=event == Events.PRINT_DONE)

    def _start_timelapse(self, payload):
        mode = self._settings.get(["timelapse_mode"])
        transport = self.active_transport()
        if mode not in ("stabilized", "layer") or transport is None:
            return
        if payload.get("origin") != FileDestinations.SDCARD:
            return

        path = payload.get("path") or payload.get("name") or ""
        stabilized = mode == "stabilized" and self._is_processed(path)
        if mode == "stabilized" and not stabilized:
            if not self._settings.get_boolean(["timelapse_fallback_layer"]):
                self._logger.info(f"{path} has no timelapse parks, not recording")
                return
            self._send_ui(
                "info",
                f"{os.path.basename(path)} wasn't prepared for a stabilized timelapse "
                "(binary G-code or not uploaded through OctoPrint); recording on layer change instead.",
            )

        fps = self._fps()
        session = TimelapseSession(
            payload.get("name") or os.path.basename(path),
            stabilized=stabilized,
            marker_speed=self._settings.get_int(["park_marker_speed"]),
            snapshot_url=self._settings.get(["timelapse_snapshot_url"]),
            post_roll_frames=int(round(fps * float(self._settings.get(["timelapse_post_roll"]) or 0))),
            logger=self._logger,
        )
        with self._timelapse_lock:
            if self._timelapse is not None:
                self._detach_timelapse()
            self._timelapse = session
            transport.add_status_listener(session.on_status)
            self._normal_poll_interval = transport.poll_interval
            transport.poll_interval = float(self._settings.get(["timelapse_poll_interval"]) or 0.5)
        self._logger.info(
            f"Timelapse started for {path} ({'stabilized' if stabilized else 'layer change'})"
        )

    def _detach_timelapse(self):
        session, self._timelapse = self._timelapse, None
        transport = self._transport
        if transport is not None and session is not None:
            transport.remove_status_listener(session.on_status)
            if self._normal_poll_interval:
                transport.poll_interval = self._normal_poll_interval
        return session

    def _stop_timelapse(self, success):
        with self._timelapse_lock:
            session = self._detach_timelapse()
        if session is None:
            return

        def run():
            try:
                session.finish(success=success)
            except Exception:
                self._logger.exception("Timelapse rendering failed")

        threading.Thread(target=run, name="PrusaLink timelapse finish", daemon=True).start()

    def _fps(self):
        from octoprint.settings import settings

        return settings().getInt(["webcam", "timelapse", "fps"]) or 25

    # ~~ hook: octoprint.comm.transport.serial.additional_port_names

    def get_additional_port_names(self, *args, **kwargs):
        return [PORT_NAME]

    # ~~ hook: octoprint.comm.transport.serial.factory

    def serial_factory(self, comm_instance, port, baudrate, read_timeout, *args, **kwargs):
        if port != PORT_NAME:
            return None

        cfg = self._cfg()
        client = self._client_from(cfg)
        transport = PrusaLinkSerial(
            client,
            read_timeout=float(read_timeout),
            poll_interval=float(cfg.get("poll_interval") or 2.0),
            storage=cfg.get("storage"),
            logger=self._logger,
        )
        transport.open()  # raises -> OctoPrint shows a connection error

        with self._transport_lock:
            if self._transport is not None:
                self._transport.close()
            self._transport = transport
        self._logger.info(
            f"Connected to {transport.printer_name} via PrusaLink at {client.base_url}"
        )
        return transport

    # ~~ hook: octoprint.printer.sdcardupload
    # Uploads straight to printer storage over HTTP instead of streaming via M28/M29.

    def sd_card_upload(
        self, printer, filename, path, started_f, success_f, failure_f, *args, **kwargs
    ):
        transport = self.active_transport()
        if transport is None:
            return None

        remote_name = os.path.basename(filename).replace(" ", "_")

        def run():
            start = time.monotonic()
            started_f(filename, remote_name)
            try:
                remote_id = self._upload(transport, path, remote_name)
            except Exception as e:
                self._logger.error(f"Upload of {filename} to printer failed: {e}")
                failure_f(filename, remote_name, int(time.monotonic() - start))
                return
            printer.refresh_sd_files()
            success_f(filename, remote_id, int(time.monotonic() - start))

        threading.Thread(target=run, name="PrusaLink upload", daemon=True).start()
        return remote_name

    # ~~ local print redirect (used by the Printer subclass below)

    def redirect_local_print(self, printer, path_in_storage, user=None):
        """
        OctoPrint can't stream G-code to a PrusaLink printer, so a print of a file in
        OctoPrint's local storage is turned into upload-to-printer + print-from-printer.
        """
        transport = self.active_transport()
        local_path = printer._fileManager.path_on_disk(FileDestinations.LOCAL, path_in_storage)
        remote_name = os.path.basename(path_in_storage).replace(" ", "_")

        def run():
            self._send_ui("info", f"Uploading {remote_name} to printer…")
            try:
                remote_id = self._upload(transport, local_path, remote_name)
            except Exception as e:
                self._logger.error(f"Upload of {path_in_storage} failed: {e}")
                self._send_ui("error", f"Upload of {remote_name} to printer failed: {e}")
                return
            printer.refresh_sd_files()
            printer.select_file(remote_id, True, printAfterSelect=True, user=user)

        threading.Thread(target=run, name="PrusaLink print redirect", daemon=True).start()

    def _send_ui(self, level, text):
        self._plugin_manager.send_plugin_message(self._identifier, {"type": level, "text": text})

    # ~~ hook: octoprint.printer.factory

    def printer_factory(self, components, *args, **kwargs):
        from octoprint.printer.standard import Printer

        plugin = self

        class PrusaLinkPrinter(Printer):
            def start_print(self, pos=None, user=None, *args, **kwargs):
                with self._selectedFileMutex:
                    selected = dict(self._selectedFile) if self._selectedFile else None

                if (
                    selected is not None
                    and not selected.get("sd")
                    and plugin.active_transport() is not None
                    and self._comm is not None
                    and self._comm.isOperational()
                    and not self._comm.isPrinting()
                ):
                    if plugin._settings.get_boolean(["redirect_local_prints"]):
                        plugin.redirect_local_print(self, selected["filename"], user=user)
                    else:
                        plugin._send_ui(
                            "error",
                            "Printing from OctoPrint storage is not possible over PrusaLink. "
                            "Upload the file to the printer or enable redirection.",
                        )
                    return

                return super().start_print(pos=pos, user=user, *args, **kwargs)

        return PrusaLinkPrinter(
            components["file_manager"],
            components["analysis_queue"],
            components["printer_profile_manager"],
        )

    # ~~ hook: octoprint.printer.estimation.factory (use the printer's own estimate)

    def estimator_factory(self, *args, **kwargs):
        from octoprint.printer.estimation import PrintTimeEstimator

        plugin = self

        class PrusaLinkEstimator(PrintTimeEstimator):
            def estimate(self, *args, **kwargs):
                transport = plugin.active_transport()
                remaining = transport.time_remaining if transport else None
                if remaining is not None and remaining >= 0:
                    return remaining, "estimate"
                return super().estimate(*args, **kwargs)

        return PrusaLinkEstimator

    # ~~ hook: octoprint.filemanager.extension_tree (Prusa binary G-code)

    def extension_tree(self, *args, **kwargs):
        return {
            "machinecode": {
                "bgcode": ContentTypeMapping(["bgcode"], "application/octet-stream")
            }
        }

    # ~~ hook: octoprint.plugin.softwareupdate.check_config

    def get_update_information(self):
        return {
            "prusalink": {
                "displayName": "PrusaLink Connector",
                "displayVersion": self._plugin_version,
                "type": "github_release",
                "user": "agreenbhm",
                "repo": "Octoprint-PrusaLink-Plugin",
                "current": self._plugin_version,
                "pip": "https://github.com/agreenbhm/Octoprint-PrusaLink-Plugin/archive/{target_version}.zip",
            }
        }


__plugin_name__ = "PrusaLink Connector"
__plugin_pythoncompat__ = ">=3.7,<4"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = PrusaLinkPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.comm.transport.serial.factory": __plugin_implementation__.serial_factory,
        "octoprint.comm.transport.serial.additional_port_names": __plugin_implementation__.get_additional_port_names,
        "octoprint.printer.sdcardupload": __plugin_implementation__.sd_card_upload,
        "octoprint.printer.factory": __plugin_implementation__.printer_factory,
        "octoprint.printer.estimation.factory": __plugin_implementation__.estimator_factory,
        "octoprint.filemanager.extension_tree": __plugin_implementation__.extension_tree,
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
    }
