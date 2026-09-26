"""Record a fingerprint for one spot.

Usage:
    python3 calibrate.py --spot kitchen --target aa:bb:cc:dd:ee:ff [--seconds 60]

Sits on indoor/sighting for the window, collects every RSSI sample the living
nodes report for the target device, takes the median per board, and upserts
that vector into spots.json (gitignored — fingerprints reference your own
device MACs).

Hardening in this version:
  * payloads validated (messages.parse_sighting) — garbage can't be counted;
  * boards with fewer than --min-per-board samples are dropped (with a loud
    warning) instead of polluting the median with a single glitch sample;
  * spots.json is written atomically (tmp + rename) so a crash can never
    truncate the map;
  * Ctrl-C aborts cleanly without writing anything.
"""
import argparse
import json
import os
import statistics
import sys
import time

import paho.mqtt.client as mqtt

from messages import normalize_mac, parse_sighting

SPOTS_PATH = os.path.join(os.path.dirname(__file__), "spots.json")


def median_per_board(samples):
    return {b: round(statistics.median(v), 1) for b, v in samples.items() if v}


def load_existing_spots():
    """Read the current map; {} when missing, exit(1) when corrupt.

    A corrupt spots.json is never silently replaced with the new spot —
    the existing fingerprints must be preserved, so this aborts instead.
    """
    if not os.path.exists(SPOTS_PATH):
        return {}
    try:
        with open(SPOTS_PATH) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        # never silently replace a corrupt map with {} and then overwrite it
        # with one new spot — the existing fingerprints must be preserved
        print(f"error: {SPOTS_PATH} is unreadable/corrupt — refusing to "
              "overwrite it; fix or remove the file first", flush=True)
        sys.exit(1)
    spots = raw.get("spots", {}) if isinstance(raw, dict) else None
    if not isinstance(spots, dict):
        # valid JSON that is not a {"spots": {...}} map (list / string /
        # number / null) is just as destructive if overwritten — abort too
        print(f"error: {SPOTS_PATH} has no spots map — refusing to overwrite "
              "it; fix or remove the file first", flush=True)
        sys.exit(1)
    return spots


def main(argv=None):
    ap = argparse.ArgumentParser(description="record an RSSI fingerprint")
    ap.add_argument("--spot", required=True, help="name of the spot, e.g. kitchen")
    ap.add_argument("--target", required=True,
                    help="target device MAC, e.g. aa:bb:cc:dd:ee:ff")
    ap.add_argument("--seconds", type=int, default=60,
                    help="how long to listen (min 5). Each node reports a "
                         "device about once per 45 s (15 s sniff + 30 s "
                         "home), so prefer >= 180 s for reliable samples")
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--mqtt-username",
                    default=os.environ.get("MQTT_USER"),
                    help="MQTT username if the broker requires auth")
    ap.add_argument("--mqtt-password",
                    default=os.environ.get("MQTT_PASS"),
                    help="MQTT password (used with --mqtt-username)")
    ap.add_argument("--min-samples", type=int, default=3,
                    help="minimum TOTAL RSSI samples before saving")
    ap.add_argument("--min-per-board", type=int, default=2,
                    help="minimum samples per board before it is trusted; "
                         "boards below this are dropped with a warning")
    args = ap.parse_args(argv)
    if args.seconds < 5:
        ap.error("--seconds must be >= 5 (a shorter window is pure noise)")

    target = normalize_mac(args.target)
    if target is None:
        ap.error(f"--target {args.target!r} is not a valid MAC address")
    if target != (args.target or "").strip().lower():
        print("note: --target lowercased (the firmware publishes lowercase "
              "MACs)", flush=True)
    args.target = target

    samples = {}  # board -> [rssi...]

    def on_message(client, userdata, msg):
        s = parse_sighting(msg.payload)
        if s is None or s["mac"] != args.target:
            return
        samples.setdefault(s["board"], []).append(s["rssi"])

    def on_connect(client, userdata, flags, reason_code, properties=None):
        # paho's auto-reconnect does NOT restore subscriptions: without this,
        # a broker restart or WiFi blip mid-window would silently collect
        # nothing and then overwrite the spot with a half-measured sample.
        if reason_code != 0:
            print(f"calibrate: connect refused (reason_code={reason_code}); "
                  f"not subscribed", flush=True)
            return
        client.subscribe("indoor/sighting")
        print("calibrate: connected; subscribed to indoor/sighting",
              flush=True)

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.on_connect = on_connect
    mqttc.on_message = on_message
    if args.mqtt_username:
        mqttc.username_pw_set(args.mqtt_username, args.mqtt_password)
        print("mqtt auth: username configured", flush=True)
    while True:                     # retry broker-down at startup
        try:
            mqttc.connect(args.host, args.port, 30)
            break
        except OSError as e:
            print(f"warning: broker {args.host}:{args.port} unavailable "
                  f"({e}); retrying in 5 s (Ctrl-C to abort)", flush=True)
            time.sleep(5)
    mqttc.loop_start()

    print(f"collecting for {args.spot}: {args.seconds}s on indoor/sighting "
          f"(target {args.target})")

    def wait_for_window():
        end = time.time() + args.seconds
        while time.time() < end:
            try:
                time.sleep(min(1.0, end - time.time()))
            except KeyboardInterrupt:
                print("\naborted by Ctrl-C — nothing was written", flush=True)
                mqttc.loop_stop()
                return False
        return True

    if not wait_for_window():
        return 1
    mqttc.loop_stop()

    total = sum(len(v) for v in samples.values())
    per_board = {b: len(v) for b, v in samples.items()}
    print("raw per-board samples:", json.dumps(per_board))

    if not samples or total < args.min_samples:
        print(f"aborted: {total} samples across {len(samples)} node(s); "
              f"need >= {args.min_samples} total", flush=True)
        return 1

    dropped = {b: n for b, n in per_board.items() if n < args.min_per_board}
    kept = {b for b in samples if b not in dropped}
    if dropped and kept:
        print(f"warning: dropping {sorted(dropped)} ({dropped} samples, "
              f"< {args.min_per_board}): too noisy to trust", flush=True)
    if not kept:
        print(f"aborted: every board has fewer than {args.min_per_board} "
              "samples; collect longer", flush=True)
        return 1

    vec = median_per_board({b: samples[b] for b in kept})
    print("median vector:", json.dumps(vec))
    if not vec:
        print("aborted: no usable samples", flush=True)
        return 1

    spots = load_existing_spots()
    spots[args.spot] = dict(vec, _meta={"samples": args.seconds,
                                        "device": args.target,
                                        "count": total,
                                        "per_board": per_board})
    doc = {"spots": spots}
    tmp = SPOTS_PATH + ".tmp"
    with open(tmp, "w") as f:                 # atomic: never truncate the map
        json.dump(doc, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SPOTS_PATH)
    print(f"saved {args.spot} -> {SPOTS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())