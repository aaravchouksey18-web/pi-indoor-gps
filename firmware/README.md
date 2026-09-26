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

## Note

This is a small delta on presence-vigil's proven v3.9 radio handling (cold
radio, sniff-before-associate, same-boot report from RAM). All the radio
lessons port over unchanged; the only new behaviour is the RSSI capture.