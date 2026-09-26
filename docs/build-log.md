# build log

## Kickoff — 2026-09-26

Repo started after presence-vigil closed out. The pitch: the two sniffers
already watch probe requests; add RSSI and fingerprint rooms using a carried
phone as the target.

Recon findings (from the `presence-vigil` repo):

- The sniffer publishes `presence/sighting` = `{board, mac, ssid}` — **no
  signal strength** anywhere in the feed. The aggregator only cared about who
  is present, and it got that fine.
- The promiscuous callback receives the SDK rxctl header first, and **RSSI is
  byte 0** of the raw packet — per-frame signal strength costs a few lines,
  no radio surgery.
- Duty cycle: 15 s sniff session (radio in capture mode from a cold radio)
  → up to 30 s home/report phase (associate + MQTT) → `ESP.restart()`.
  Sightings publish straight from RAM in the same boot.

Design decisions:

- New firmware sketch lives in this repo; presence-vigil's pinned code stays
  untouched. New sketch publishes to `indoor/sighting` and LWT
  `indoor/online`.
- v1 pipeline: node fleet → mosquitto → collector builds a per-node RSSI
  window for the target device → nearest-neighbour match against
  `spots.json` → `indoor/estimate`.
- Fingerprints are device-derived → gitignored. Repo content carries zero
  personal identifiers (WiFi creds in `config.h`, gitignored).

Deferred / open:

- Sensor fleet count — 2 existing nodes vs building more (undecided).
- Calibration layout — which spots, how many samples per spot, median window.
- Target device choice — phone probe cadence varies a lot by OS and by
  whether the screen is on.
- Where the estimate lands in the UI (dashboard integration).

Next: flash one node with the RSSI sketch, sanity-check `indoor/sighting` on
the live broker, then calibrate a first two-spot map.

## Session 2 — first flash + synthetic end-to-end (2026-09-26)

- `arduino-cli` 1.5.1 installed on the Pi (Linux ARM64 binary; no brew). The
  esp8266 core (3.1.2) and toolchain were already in `~/.arduino15` from the
  earlier project.
- Node **b** (V3, CH340) plugged into the Pi → `/dev/ttyUSB0`. `config.h`
  scaffolded in the new repo (`BOARD_ID "b"`, broker = Pi LAN address,
  user-filled WiFi creds). Compiled and flashed: 288048 bytes, hash verified, hard reset.
- First cycle on the live broker delivered `indoor/online` (LWT) and
  `indoor/sighting` with per-device `rssi` / `rssi_n`. The RSSI extension
  works on real silicon. Node b currently sees 3 ambient emitters from the Pi
  (neighbours; the phone isn't probing while idle).
- `paho-mqtt` installed into the Pi user-python env (only host dependency; the
  presence-vigil stack runs it inside its container).
- Collector smoke test: parses live sightings into per-node RSSI views.
- Phone was unavailable for real calibration, so **`pi/inject.py`** became a
  synthetic sighting injector, and the whole matcher chain was proven
  end-to-end with it: calibrate (at-pi -45 / other-room -80) → spots.json →
  collector in `--target` mode → `indoor/estimate` flipping as rssi swept.
- Bug found + fixed: `calibrate.py --min-samples` counted *distinct boards*
  (would always abort on a 1-2 node fleet); now counts total samples.

Notes for real calibration:

- `pi/spots.json` currently holds the **synthetic** test map (fake MAC
  `de:ad:be:ef:00:02`, gitignored). Real phone calibration will overwrite or
  add spots.
- Single-node maps are buckets along distance (one RSSI value per spot);
  two+ nodes make the map discriminate direction, not just proximity.
- Confidence/distances were perfect (1.0 / 0.0) only because injected values
  exactly matched fingerprint medians — real RSSI variance will lower them.