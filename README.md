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
  inject G-code mid-print (Octolapse, OctoPrint's own timelapse, layer-change plugins) won't work.
  The plugin has its own timelapse instead, see below.
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
| timelapse_mode | off | `off`, `stabilized`, `layer` |
| timelapse_fallback_layer | on | layer-change recording for files without parks |
| timelapse_snapshot_url | – | blank = OctoPrint's snapshot webcam |
| timelapse_poll_interval | 0.5 s | poll rate while recording |
| park_x / park_y | auto | back right from printer profile |
| park_lift / park_retract | 1.0 / 0.8 mm | |
| park_dwell_ms | 2500 | pause while parked |
| park_marker_speed | 91 | M220 value used as the "parked" signal |
| park_every_n_layers | 1 | |

## Timelapse

Octolapse and OctoPrint's built-in timelapse need to inject G-code during the print, which PrusaLink
can't do. The plugin records timelapses itself. Frames go to OctoPrint's `timelapse_tmp` folder and are
rendered by OctoPrint, so you need ffmpeg set up in OctoPrint and a snapshot webcam (or a snapshot URL).
Videos show up in the normal **Timelapse** tab. Turn OctoPrint's own timelapse **off**.

**Stabilized** mode (similar to Octolapse's stabilized snapshots):

1. When a `.gcode` file is uploaded to the printer through OctoPrint (print from local storage, or
   upload to SD), a park block is inserted before each layer change (from the slicer's `;LAYER_CHANGE` or
   `;LAYER:` comments). The block retracts, lifts Z, moves to the park position, waits for moves to
   finish (`M400`) and sets the speed to a marker value (`M220 B`, `M220 S91`). It then pauses
   (`G4`), restores the speed (`M220 R`), moves back, lowers Z and unretracts. Absolute/relative
   modes (G90/G91, M82/M83) and the feedrate are restored afterwards.
2. While recording, the plugin polls PrusaLink faster (0.5 s). When the reported print speed equals the
   marker, the head is parked, so it takes a snapshot. Buddy firmware doesn't report X/Y during a print,
   so the speed field is the only reliable signal.

Notes:
- The pause per park has to be longer than the fast poll interval plus the snapshot time. The default
  2.5 s adds about 3–4 s per layer.
- Processed files park whenever they're printed, including from the printer's screen.
- Binary G-code (`.bgcode`) can't be edited. Turn off *Printer Settings → General → Binary G-code* in
  PrusaSlicer to get stabilized timelapses. Files that weren't processed (bgcode, or sent to the printer
  directly from PrusaSlicer) fall back to layer-change mode, unless you disable that.
- The default park position is the back right corner, 10 mm in, taken from the OctoPrint printer
  profile. Check it against your printer before the first print.
- Don't set the marker speed (default 91 %) on the printer yourself during a recording.

**Layer change** mode: no file changes. A snapshot is taken whenever the reported Z height increases and
stays there for two polls. The head isn't parked, so frames aren't stabilized.

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
print is already running or paused. It also covers a stabilized timelapse: parks inserted at upload,
one frame per park plus a final one, and the render through OctoPrint into the Timelapse tab. The
mock printer "runs" the uploaded G-code in real time, reporting Z and M220 speed like Buddy firmware.
For the render step, install ffmpeg or `pip install imageio-ffmpeg`.

API reference: [Prusa-Link-Web `spec/openapi.yaml`](https://github.com/prusa3d/Prusa-Link-Web/blob/master/spec/openapi.yaml).

## License

AGPLv3
