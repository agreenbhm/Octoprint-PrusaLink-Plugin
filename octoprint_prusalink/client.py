"""Minimal client for the PrusaLink v1 HTTP API (Prusa-Link-Web spec/openapi.yaml)."""

import logging
import os
from urllib.parse import quote

import requests
from requests.auth import HTTPDigestAuth


class PrusaLinkError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class PrusaLinkClient:
    def __init__(
        self,
        host,
        username="maker",
        password="",
        api_key="",
        use_https=False,
        verify_tls=True,
        timeout=10.0,
    ):
        host = (host or "").strip().rstrip("/")
        if not host:
            raise ValueError("PrusaLink host is not configured")
        if "://" not in host:
            host = ("https://" if use_https else "http://") + host
        self.base_url = host
        self.timeout = timeout
        self._logger = logging.getLogger("octoprint.plugins.prusalink.client")

        self._session = requests.Session()
        self._session.verify = verify_tls
        if api_key:
            # older PrusaLink (MK3 / early Buddy firmware) uses an API key header
            self._session.headers["X-Api-Key"] = api_key
        elif password:
            self._session.auth = HTTPDigestAuth(username or "maker", password)

    # ~~ low level

    def _url(self, path):
        return self.base_url + path

    def _request(self, method, path, expected=(200,), timeout=None, **kwargs):
        try:
            r = self._session.request(
                method, self._url(path), timeout=timeout or self.timeout, **kwargs
            )
        except requests.RequestException as e:
            raise PrusaLinkError(f"{method} {path} failed: {e}") from e

        if r.status_code not in expected:
            detail = ""
            try:
                body = r.json()
                detail = body.get("message") or body.get("title") or ""
            except Exception:
                detail = (r.text or "")[:200]
            raise PrusaLinkError(
                f"{method} {path} returned HTTP {r.status_code} {detail}".strip(),
                status_code=r.status_code,
            )
        return r

    def _json(self, method, path, **kwargs):
        r = self._request(method, path, expected=(200, 204), **kwargs)
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    @staticmethod
    def _file_path(storage, path):
        storage = storage.strip("/")
        path = path.lstrip("/")
        return "/api/v1/files/{}/{}".format(
            quote(storage, safe=""), quote(path, safe="/")
        )

    # ~~ info

    def version(self):
        return self._json("GET", "/api/version")

    def info(self):
        return self._json("GET", "/api/v1/info")

    def status(self):
        return self._json("GET", "/api/v1/status")

    def job(self):
        return self._json("GET", "/api/v1/job")

    def storage(self):
        data = self._json("GET", "/api/v1/storage") or {}
        return data.get("storage_list", [])

    # ~~ job control

    def pause_job(self, job_id):
        self._request("PUT", f"/api/v1/job/{int(job_id)}/pause", expected=(200, 204))

    def resume_job(self, job_id):
        self._request("PUT", f"/api/v1/job/{int(job_id)}/resume", expected=(200, 204))

    def stop_job(self, job_id):
        self._request("DELETE", f"/api/v1/job/{int(job_id)}", expected=(200, 204))

    # ~~ files

    def file_info(self, storage, path=""):
        return self._json(
            "GET",
            self._file_path(storage, path),
            headers={"Accept": "application/json"},
        )

    def start_print(self, storage, path):
        self._request("POST", self._file_path(storage, path), expected=(200, 201, 204))

    def delete_file(self, storage, path):
        self._request(
            "DELETE", self._file_path(storage, path), expected=(200, 204)
        )

    def upload_file(
        self,
        storage,
        path,
        local_path,
        print_after_upload=False,
        overwrite=True,
        timeout=None,
    ):
        size = os.path.getsize(local_path)
        headers = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
            "Print-After-Upload": "?1" if print_after_upload else "?0",
            "Overwrite": "?1" if overwrite else "?0",
        }
        with open(local_path, "rb") as f:
            # uploads to the printer can be slow (the Buddy board writes to USB at
            # a few hundred kB/s), so scale the timeout with file size
            upload_timeout = timeout or max(60.0, size / (50 * 1024))
            self._request(
                "PUT",
                self._file_path(storage, path),
                expected=(200, 201, 204),
                data=f,
                headers=headers,
                timeout=upload_timeout,
            )
        return size

    def upload_bytes(
        self, storage, path, data, print_after_upload=False, overwrite=True
    ):
        headers = {
            "Content-Type": "application/octet-stream",
            "Print-After-Upload": "?1" if print_after_upload else "?0",
            "Overwrite": "?1" if overwrite else "?0",
        }
        self._request(
            "PUT",
            self._file_path(storage, path),
            expected=(200, 201, 204),
            data=data,
            headers=headers,
            timeout=max(60.0, len(data) / (50 * 1024)),
        )

    def list_print_files(self, storage, max_depth=3):
        """
        Recursively lists printable files in a storage.

        Returns a list of dicts with keys: path (API path relative to storage, built from
        the ``name`` fields, which are SFNs on Buddy firmware), display_path (built from
        ``display_name``), size, m_timestamp.
        """
        result = []

        def walk(api_path, display_path, depth):
            info = self.file_info(storage, api_path) or {}
            for child in info.get("children", []) or []:
                name = child.get("name")
                if not name:
                    continue
                display = child.get("display_name") or name
                child_api = f"{api_path}/{name}" if api_path else name
                child_display = f"{display_path}/{display}" if display_path else display
                ctype = child.get("type")
                if ctype == "FOLDER":
                    if depth < max_depth:
                        try:
                            walk(child_api, child_display, depth + 1)
                        except PrusaLinkError as e:
                            self._logger.warning(f"Could not list {child_api}: {e}")
                elif ctype in ("PRINT_FILE", None):
                    result.append(
                        {
                            "path": child_api,
                            "display_path": child_display,
                            "size": child.get("size") or 0,
                            "m_timestamp": child.get("m_timestamp"),
                        }
                    )

        walk("", "", 0)
        return result

    def close(self):
        try:
            self._session.close()
        except Exception:
            pass
