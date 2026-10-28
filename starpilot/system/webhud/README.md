# StarPilot web HUD bridge

Ported from moistpope/sunnypilot (`openpilot/sunnypilot/webhud`). The differences are listed under
[StarPilot](#starpilot) below; keep them in mind when syncing the two.

The comma's half of the Fisker Ocean HUD. The HUD itself is
[Oceanfront View](https://github.com/moistpope/oceanfront-view): a web page with a 3D model of the car,
the lanes, cars, signs and lights its ADAS sees, the road from the map, the car's own state and
controls, music and navigation. It runs on the car's own screen in its Android app, or in a browser
from its `serve.py`, and decodes the buses and runs its world model itself, in a web worker. This
process feeds it.

**What the bridge does.** It reads the ADAS bus, the radar's private bus (panda bus 1) and openpilot's
own services and streams them as they are, in ticks at 20 Hz over a WebSocket (`/ws`): raw CAN frames
allowlisted by message (the set in `third_party/webhud/dbc/fisker_ocean_adas_world.dbc`, of
which Oceanfront View keeps the same copy, and the radar's `opendbc/dbc/fisker_ocean_mrr.dbc`), and the
services in compact form (`extract.py`). A viewer that just connected first gets a snapshot of the latest
frame of every message; one that falls behind gets its missed ticks merged, never dropped. The REST API
carries status, replay of recorded routes (rlog/qlog, uploads too), the editable CAN overrides
(a JSON file, applied in place by `selfdrive/car/can_overrides.py`) and a few params; `/map/...`
fetches the OSM road tiles the page's map matcher reads (`maptiles.py`, mapd's own files) and point
features from Overpass. `starpilot.local` is published over mDNS. The process is niced and reads nothing
while no viewer is connected.

**It serves no page.** The Android app carries the page in its APK and relays `/api` and `/ws` here; on
a PC, `python3 serve.py --bridge starpilot.local` in the Oceanfront View checkout does the same. `/`
answers with a short text saying so. Set `WEBHUD_PAGE_DIR=<oceanfront-view checkout>` and the bridge
serves the page as well (`/`, `/vendor/`, `/models/`, `/dbc/` from that checkout's `static/`,
`third_party/` and `dbc/`), for a plain browser against `http://starpilot.local:8088`.

**Process:** `webhud` in `process_config.py`, always on (a prebuilt tree can't add an enable param). Port
8088, and 80 when the process may bind it.

## Pieces

| File | Role |
|------|------|
| `server.py` | Process entry point (`webhud` in process_config). stdlib `http.server` + WebSocket, 20 Hz tick stream (raw frames + service extracts), REST API, the DBCs as static files. |
| `state.py`, `extract.py` | `StreamBuilder`: the frames and service extracts gathered since the last tick, and the latest of each for a viewer that just connected; the extractors turn openpilot/StarPilot services into compact JSON. |
| `sources.py` | Live (cereal `can` + services) and rlog/qlog replay (zst/bz2, multi-segment, seek, speed). |
| `demo.py` | Synthetic drive that encodes real ADASBUS frames: `server.py --demo` or *Playback → Play demo drive* in the HUD. |
| `dbc.py` | Small DBC reader/decoder (the demo encodes with it; the overrides API shows the ICC's live values through it). Oceanfront View's `static/js/world/dbc.js` is its twin in the page. |
| `mdns.py` | Publishes `starpilot.local` as an alias + `_http._tcp` service through avahi's D-Bus API (jeepney); falls back to a built-in A-record responder. Doesn't change the device hostname. |
| `maptiles.py` | OSM road tiles (mapd's cells, downloaded on first use), Overpass point features, and the prefetch of everything within a radius of the car. |
| `websocket.py` | The WebSocket framing. |
| `paths.py` | Where the DBCs are, and the optional page directory. |
| `selfdrive/car/can_overrides.py` | Validates/applies the overrides (`OverrideStore`, `/data/fisker/can_overrides.json`); card polls it at 10 Hz and updates the dicts carcontroller reads, in place. |
| `tools/fisker_handoff_audit.py` | Audits a recorded drive's relay handoffs with opendbc's `relay_audit`. |
| `third_party/webhud/dbc/` | The ADASBUS subset the bridge streams by, generated in Oceanfront View (`tools/gen_world_dbc.py` there) from the FM29 matrix. |

## API

`GET /api/status`, `GET /api/routes`, `POST /api/replay {action: load|play|pause|toggle|seek|step|speed|live|demo, ...}`,
`GET /map/tile/<lat>/<lon>/<name>` (an OSM road tile, fetched into mapd's folder on first use),
`GET /map/features/<klat>/<klon>`, `POST /map/prefetch {lat, lon, radius_km}`, `GET /map/status`,
`PUT /api/upload?name=<file>` (raw rlog/qlog body), `GET|PUT /api/params` (personality, experimental
mode, units), `GET|PUT|DELETE /api/overrides`, `GET /api/ssh` (SSH on or off, the keys let in, by
fingerprint), `POST|DELETE /api/ssh/authorize {publicKey}` (add or remove one OpenSSH public key line in
`GithubSshKeys`, beside the keys already there; adding also turns SSH on: the Oceanfront View app puts its own
key on the comma this way, then manages it over SSH as `comma`). Static: `/dbc/` (the ADASBUS subset and the radar DBC,
which the page decodes with), and the page with `WEBHUD_PAGE_DIR` set. WebSocket `/ws`: the server sends
`{type: hello}` then `{type: tick, data: {now, reset?, brand, can: [[t, [[addr, bus, hex], ...]], ...],
op: [[service, t, extract], ...], mode, replay, server}}` at 20 Hz (the first tick for a new viewer is a
snapshot of the latest frame of every message, with `reset`); the client sends `{type: replay, ...}`.

Writes are accepted from private addresses only, never cross-origin, and CAN override changes, SSH keys and
replay are refused while the car is moving (replay also stops itself if the car starts moving).

## Development

```
python -m openpilot.starpilot.system.webhud.server --demo            # synthetic drive
python -m openpilot.starpilot.system.webhud.server --replay <rlog.zst | segment dir | route dir>
python -m openpilot.starpilot.system.webhud.server --replay <rlog.zst> --map-root <dir of OSM cells>   # the map from somewhere other than mapd's folder
WEBHUD_PAGE_DIR=~/oceanfront-view python -m openpilot.starpilot.system.webhud.server --demo   # and serve the page from that checkout
pytest starpilot/system/webhud/tests                                  # the bridge
```

On a PC without the native params library, settings are kept in `~/.comma/webhud_dev_params.json`.
The page's own tests and tools (`node --test`, the tick recorder and replayer) are in Oceanfront View.

## StarPilot

What differs from the sunnypilot bridge, all because StarPilot ships prebuilt (no new param keys without a
rebuilt `params_pyx.so`) or names things differently:

- **Services.** The page reads sunnypilot's `selfdriveStateSP`, `longitudinalPlanSP` and `liveMapDataSP`.
  `extract.py` fills those names, in the same shapes, from `starpilotCarState` (Always On Lateral as the MADS
  state), `starpilotPlan` (the speed limit controller) and `mapdOut` (`OP_NAMES`). The Oceanfront View app
  needs no change. With Always On Lateral steering and openpilot not engaged, the page labels it "MADS steering".
- **CAN overrides** live in `/data/fisker/can_overrides.json` (`OverrideStore`), not the `FiskerCanOverrides`
  param.
- **Params.** No `EnableWebHud` (the process always runs) and no `FiskerACCAutoSpeed`/`FiskerACCTerrain`: the
  0x52A spoof's baseline for those is `FiskerFlagsSP` (auto speed on, terrain off), and an ICC_0x52A override
  set from the page wins over it. `/api/params` offers only keys StarPilot has (a test checks `params_keys.h`).
- **Layout.** `openpilot.starpilot.system.webhud` instead of `openpilot.sunnypilot.webhud`, `cereal` instead of
  `openpilot.cereal`, `openpilot.system.hardware.hw` instead of `openpilot.common.hardware.hw`, mapd's folder
  from `maptiles.mapd_root()`, the DBC under the repo's `third_party/webhud`, pytest instead of unittest.
