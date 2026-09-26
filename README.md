# pi-indoor-gps

Indoor position estimation from WiFi RSSI fingerprinting, on existing local
hardware.

The sibling project that builds on `presence-vigil`. Everything stays on the
home LAN: two ESP8266 sniffer nodes watch for 802.11 probe requests — and now
also report **signal strength**. A Raspberry Pi collects the RSSI that every
node measures for a chosen target device (the phone in your pocket), matches
the vector against a small map of measured spots, and estimates which
room/region the target is in.

**Status:** kickoff. Architecture + RSSI firmware + collectors scaffolded.
The fingerprint map is not recorded yet and the sensor fleet count is
undecided (see `docs/build-log.md`).

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

## Layout

- `firmware/pi-indoor-sniffer/` — probe sniffer publishing per-device RSSI
- `pi/` — collector, fingerprint calibrator, and the matcher
- `measurements/` — fingerprint maps and raw RSSI dumps (local, gitignored)
- `docs/` — build log and design notes

## Build phase checklist

1. Flash `firmware/pi-indoor-sniffer` — copy `config.h.example` to
   `config.h` and fill in board id, WiFi creds, broker, sniff channel.
2. Record fingerprints: `python3 pi/calibrate.py --spot kitchen --seconds 60 --target AA:BB:...`
3. Run the collector: `python3 pi/collector.py` — watch `indoor/estimate`.

## Sanitization rule

The repo never contains your SSID, device MACs, or subscriber identifiers.
WiFi credentials live only in the gitignored `config.h`. Fingerprint maps
reference your own devices, so `pi/spots.json` and raw dumps are gitignored
too. If content with identifiers ever needs to live here, it does so only in
a gitignored local file — never in a commit.