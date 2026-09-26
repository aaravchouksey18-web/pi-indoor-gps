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