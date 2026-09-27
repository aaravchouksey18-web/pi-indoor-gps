# firmware

- `pi-indoor-sniffer/` — ESP8266 probe-request sniffer that publishes per-device
  RSSI.

## Topics

| topic                    | direction | payload (example)                                  |
|--------------------------|-----------|----------------------------------------------------|
| `indoor/sighting`        | node → hub| `{"board":"a","mac":"aa:bb:..","ssid":"..","rssi":-58,"rssi_n":12}` |
| `indoor/online/<board>`  | node → hub| `{"board":"a","online":true}` (retained; LWT false) |

`rssi` is the **strongest** probe RSSI a device produced during the node's
15 s sniff session (rxctl byte 0). `rssi_n` counts the probes behind it.
Published MACs are **lowercase** colon-separated (`aa:bb:cc:dd:ee:ff`) — keep
`--target` lowercase on the Pi side.

## Libraries (versions used)

Built and tested with these exact versions — the sketch uses
`StaticJsonDocument` (ArduinoJson **v6** API, not v7), the ESP8266
PubSubClient API, and ESP8266 core 3.x (bundles `ESP8266WiFi` +
`user_interface.h`). With arduino-cli you can reproduce the build with:

```sh
arduino-cli core install esp8266:esp8266@3.1.2
arduino-cli lib install ArduinoJson@6.21.5
arduino-cli lib install PubSubClient@2.8.0
arduino-cli compile --fqbn esp8266:esp8266:nodemcuv2 pi-indoor-sniffer
```

(No `sketch.yaml`/`lib.json` is shipped, so the pins are documented here
rather than enforced by the toolchain.)

Please don't "upgrade" ArduinoJson to v7 in a local edit — v7 dropped
`StaticJsonDocument` and the sketch would need a migration, not a pin bump.

## Delivery semantics

- **Sniffed once per 15 s session, single channel** (`SNIFF_CHANNEL`) — the
  classic ESP8266 sniffer constraint: another channel's clients look absent
  that session. With a few nodes each on a different channel the fleet sees
  more.
- Sightings are queued to the MQTT socket and flushed over a second or two;
  the report phase now re-issues `WiFi.begin()` every 5 s while joining (a
  stalled association no longer silently eats the session) and keeps trying up
  to a 120 s backstop before giving up, then drains the TCP queue
  (unconditionally, for the full drain window) before the restart that ends
  every cycle, reporting any sightings that couldn't leave.
- The heartbeat and the LWT both live on a **per-node** topic
  `indoor/online/<board>` (retained; LWT `online:false` publishes here too),
  at the same QoS 1 — sharing one flat `indoor/online` across the fleet would
  let the last publisher hide every other node, and a dead node's retained LWT
  would overwrite a live node's retained heartbeat. Subscribe to
  `indoor/online/+` on the hub to see the whole fleet.

## Note

This is a small delta on presence-vigil's proven v3.9 radio handling (cold
radio, sniff-before-associate, same-boot report from RAM). All the radio
lessons port over unchanged; the only new behaviour is the RSSI capture.