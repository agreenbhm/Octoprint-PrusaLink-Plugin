import os
import threading
import time

import flask
import octoprint.plugin
from octoprint.access.permissions import Permissions
from octoprint.filemanager import ContentTypeMapping
from octoprint.filemanager.destinations import FileDestinations

from .client import PrusaLinkClient, PrusaLinkError
from .transport import PORT_NAME, PrusaLinkSerial


class PrusaLinkPlugin(
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.SimpleApiPlugin,
):
    def __init__(self):
        self._transport = None
        self._transport_lock = threading.Lock()

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
                remote_id = transport.upload(path, remote_name)
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
                remote_id = transport.upload(local_path, remote_name)
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
