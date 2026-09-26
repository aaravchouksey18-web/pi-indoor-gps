# pi-indoor-gps

Indoor position estimation from WiFi RSSI fingerprinting, on existing local
hardware.

The sibling project that builds on `presence-vigil`. Everything stays on the
home LAN: two ESP8266 sniffer nodes watch for 802.11 probe requests — and now
also report **signal strength**. A Raspberry Pi collects the RSSI that every
node measures for a chosen target device (the phone in your pocket), matches
the vector against a small map of measured spots, and estimates which
room/region the target is in.

**Status:** plumbing is live end-to-end on the home broker. RSSI firmware
(report phase + same-boot publish), the collector, calibrator and matcher all
run; `pi/inject.py` proved a full synthetic floor-to-broker trip (sighting →
collector → fingerprint match → `indoor/estimate`) against the real pipeline.
What's missing is one **real** fingerprint pass — the calibration phone that
carries the target MAC isn't around, so `spots.json` has no measured vectors
yet (see `docs/build-log.md`).

```
 [phone in living room]
        | probe requests (802.11)
        v
 [NodeMCU A] -- indoor/sighting {mac, rssi} -->+
        |                                      |
 [NodeMCU B] -- indoor/sighting {mac, rssi} -->+--> [mosquitto] --> [collector]
        |                                                                  |
                                                          match vs spots.json
                                                                  |
                                                        [indoor/estimate]
```

**Honest accuracy:** v1 discriminates *room / region*, not sub-meter. Two
nodes give coarse separation between rooms with distinct RF exposure; more
nodes sharpen the map.

**Honest matching:** the matcher refuses guesses. A spot is only a candidate
when *every* board its fingerprint records has reported a fresh RSSI (no
partial-credit "1-board spot beats a 2-board spot"), when the measured vector
is within a distance ceiling, and when the best spot beats the runner-up by a
margin — otherwise no `indoor/estimate` is published. The estimate payload
carries `units:"dB"` for its `distance`, plus `confidence` and `margin`, so a
dashboards can't mistake the RSSI distance for metres.

## Layout

- `firmware/pi-indoor-sniffer/` — probe sniffer publishing per-device RSSI
- `pi/` — `messages.py` (payload validation), collector, calibrator, matcher,
  and `inject.py` (synthetic-signal test harness)
- `measurements/` — fingerprint maps and raw RSSI dumps (local, gitignored)
- `docs/` — build log and design notes

## Build phase checklist

1. Flash `firmware/pi-indoor-sniffer` — copy `config.h.example` to
   `config.h` and fill in board id, WiFi creds, broker, sniff channel.
2. Install the Paho dependency: `pip install -r requirements.txt`
   (`paho-mqtt>=2.0` — every script uses the 2.x API).
3. Record fingerprints: `python3 pi/calibrate.py --spot kitchen --seconds 60 --target aa:bb:cc:dd:ee:ff`
   (the firmware publishes **lowercase** colon-separated MACs; the scripts
   lowercase the `--target` for you and refuse an invalid one).
4. Run the collector: `python3 pi/collector.py --target aa:bb:cc:dd:ee:ff`
   (add `--boards a,b` to trust only specific nodes) — watch
   `indoor/estimate`. No phone handy for a smoke test?
   `python3 pi/inject.py --target aa:bb:cc:dd:ee:ff --rssi -58 --count 20`.

## Sanitization rule

The repo never contains your SSID, device MACs, or subscriber identifiers.
WiFi credentials live only in the gitignored `config.h`. Fingerprint maps
reference your own devices, so `pi/spots.json` and raw dumps are gitignored
too. If content with identifiers ever needs to live here, it does so only in
a gitignored local file — never in a commit.