# firmware

- `pi-indoor-sniffer/` — ESP8266 probe-request sniffer that publishes per-device
  RSSI.

## Topics

| topic            | direction | payload (example)                                  |
|------------------|-----------|----------------------------------------------------|
| `indoor/sighting`| node → hub| `{"board":"a","mac":"aa:bb:..","ssid":"..","rssi":-58,"rssi_n":12}` |
| `indoor/online`  | node → hub| `{"board":"a","online":true}` (retained; LWT false) |

`rssi` is the **strongest** probe RSSI a device produced during the node's
15 s sniff session (rxctl byte 0). `rssi_n` counts the probes behind it.
Published MACs are **lowercase** colon-separated (`aa:bb:cc:dd:ee:ff`) — keep
`--target` lowercase on the Pi side.

## Libraries (pinned)

Compile in the Arduino IDE / arduino-cli with these exact versions — the
sketch uses `StaticJsonDocument` (ArduinoJson **v6** API, not v7) and the
ESP8266 PubSubClient API:

- **ArduinoJson** `6.21.5`
- **PubSubClient** `2.8` (or any 2.x)
- ESP8266 core `3.x` (bundles `ESP8266WiFi` + `user_interface.h`)

Please don't "upgrade" ArduinoJson to v7 in a local edit — v7 dropped
`StaticJsonDocument` and the sketch would need a migration, not a pin bump.

## Delivery semantics

- **Sniffed once per 15 s session, single channel** (`SNIFF_CHANNEL`) — the
  classic ESP8266 sniffer constraint: another channel's clients look absent
  that session. With a few nodes each on a different channel the fleet sees
  more.
- Sightings are queued to the MQTT socket and flushed over a second or two;
  the report phase now re-issues `WiFi.begin()` every 5 s while joining (a
  stalled association no longer silently eats the session) and drains the TCP
  queue before the restart that ends every cycle, reporting any sightings
  that couldn't leave.
- `indoor/online` is retained with a **LWT** `online:false` — the hub can
  detect a dead node.

## Note

This is a small delta on presence-vigil's proven v3.9 radio handling (cold
radio, sniff-before-associate, same-boot report from RAM). All the radio
lessons port over unchanged; the only new behaviour is the RSSI capture.