# OctoPrint-PrusaLink

Connect OctoPrint to a Prusa printer (MK4/MK4S, MK3.9, XL, MINI, Core One, MK3S+ with PrusaLink) over the
printer's **local PrusaLink HTTP API** instead of a USB serial cable. Anything that talks to OctoPrint
(OctoApp, Printoid, OctoPod, OctoEverywhere, Home Assistant, Cura/PrusaSlicer "OctoPrint" upload, …) can
then see and control the printer. No Prusa Connect / cloud involved.

## How it works

The plugin registers a virtual serial port, `PRUSALINK`, via OctoPrint's
`octoprint.comm.transport.serial.factory` hook. It answers OctoPrint's Marlin G-code with data from the
PrusaLink v1 API and turns print control into API calls.

| OctoPrint side | PrusaLink side |
|---|---|
| Temps (M105 / autoreport) | `GET /api/v1/status` (polled) |
| "SD card" file list, select, delete (M20/M23/M30) | `GET/DELETE /api/v1/files/{storage}/…` |
| Upload to SD | `PUT /api/v1/files/{storage}/…` (direct HTTP upload, no M28 streaming) |
| Start / pause / resume / cancel | `POST files/…`, `PUT job/{id}/pause\|resume`, `DELETE job/{id}` |
| Progress / time left | job `progress` / `time_remaining` (the printer's own estimate) |
| Print started, paused, stopped or finished on the printer | picked up and reflected in OctoPrint |
| Printing a file in OctoPrint's *local* storage | uploaded to the printer, then printed from there |

The printer's storage (USB on Buddy printers) shows up as OctoPrint's **SD card**. Binary G-code
(`.bgcode`) is registered as a printable file type.

### Limitations (PrusaLink API, not fixable in the plugin)

PrusaLink has **no raw G-code endpoint**. These are acknowledged with `ok` and ignored (a warning
shows once in the terminal):

- Setting temperatures, jogging/homing, fan, flow/feed rate, filament load/unload, terminal G-code.
- Pause/cancel G-code scripts in OctoPrint are not executed on the printer (the printer runs its own).
- OctoPrint can't stream files line by line; every print runs from printer storage. Plugins that
  inject G-code mid-print (e.g. some timelapse or layer-change plugins) won't affect the printer.
- One PrusaLink printer per OctoPrint instance (as with serial). Run multiple instances for multiple
  printers.

## Install

In OctoPrint: **Settings → Plugin Manager → Get More → from URL**:

```
https://github.com/agreenbhm/Octoprint-PrusaLink-Plugin/archive/main.zip
```

or `pip install .` from a checkout into OctoPrint's venv. Restart OctoPrint.

## Configure

1. On the printer: **Settings → Network → PrusaLink** – note the username (`maker`) and password.
   Older firmware / MK3 PrusaLink may show an API key instead.
2. OctoPrint **Settings → PrusaLink Connector**: host/IP, username, password (or API key).
   Click **Test connection**.
3. Connection panel: serial port **PRUSALINK**, any baud rate, then **Connect**.
   Set a printer profile with the right bed size / heated bed so apps render correctly.
   Enable *auto-connect* if you want it to connect on startup.

Settings:

| Setting | Default | Notes |
|---|---|---|
| host | – | `192.168.1.50`, `mk4.local`, or full URL with port |
| username / password | `maker` / – | HTTP digest auth |
| api_key | – | `X-Api-Key` for older firmware; overrides user/password |
| use_https / verify_tls | off / on | |
| storage | auto | `usb`, `local`, `sdcard`; blank = first writable |
| poll_interval | 2 s | status poll rate |
| redirect_local_prints | on | upload local files to the printer on print. Needs restart to toggle the hook itself |

The plugin supplies OctoPrint's printer factory (`octoprint.printer.factory`) to implement the
local-print redirect. Only one plugin can do that; if another installed plugin also does, one of them
will win.

## Development

```
python -m venv venv && . venv/bin/activate
pip install octoprint pytest && pip install -e .

# unit tests (transport against a mock PrusaLink)
pytest tests/

# full end-to-end with a real OctoPrint server
octoprint --basedir /tmp/opbase user add admin --password admin --admin
sed -i 's/apikey: null/apikey: e2ekey/' /tmp/opbase/users.yaml
python tests/e2e_octoprint.py --basedir /tmp/opbase

# manual testing: mock printer + virtual OctoPrint
python tests/mock_prusalink.py --port 8081 --api-key test
```

The e2e test covers: connect, temps, file list with long names, local-print redirect, pause/resume/cancel
from OctoPrint, prints started/paused/stopped on the printer, direct SD upload, and connecting while a
print is already running or paused.

API reference: [Prusa-Link-Web `spec/openapi.yaml`](https://github.com/prusa3d/Prusa-Link-Web/blob/master/spec/openapi.yaml).

## License

AGPLv3
