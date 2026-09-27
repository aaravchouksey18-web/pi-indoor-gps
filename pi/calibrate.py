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
  * spots.json is written atomically (mkstemp + rename) so a crash can never
    truncate the map, and an exclusive lock keeps two concurrent calibrations
    from clobbering each other's spots;
  * a map recorded for a DIFFERENT device (different --target MAC) is refused
    — mixing radios makes distances meaningless;
  * Ctrl-C aborts cleanly without writing anything.
"""
import argparse
import fcntl
import json
import os
import statistics
import sys
import tempfile
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
    spot = (args.spot or "").strip()
    if not spot:
        ap.error("--spot must be a non-empty spot name")
    args.spot = spot
    if args.seconds < 5:
        ap.error("--seconds must be >= 5 (a shorter window is pure noise)")
    if not (1 <= args.port <= 65535):
        ap.error("--port must be 1..65535")
    if args.min_samples < 1:
        ap.error("--min-samples must be >= 1")
    if args.min_per_board < 1:
        ap.error("--min-per-board must be >= 1")

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
        except (OSError, ValueError) as e:
            # ValueError covers paho's "Invalid host." for MQTT_HOST=""
            print(f"warning: broker {args.host}:{args.port} unavailable "
                  f"({e}); retrying in 5 s (Ctrl-C to abort)", flush=True)
            time.sleep(5)
    mqttc.loop_start()

    print(f"collecting for {args.spot}: {args.seconds}s on indoor/sighting "
          f"(target {args.target})")

    def wait_for_window():
        # monotonic: a wall-clock jump (first NTP sync on a Pi with no RTC)
        # must neither abort the window early nor make sleep() raise on a
        # negative duration. max(0.0, ...) covers a clock step between the
        # loop test and the sleep call itself.
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            try:
                time.sleep(max(0.0, min(1.0, end - time.monotonic())))
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
              f"< {args.min_per_board}): too noisy to trust — this spot will "
              "cover fewer boards than you measured, so under full-coverage "
              "matching it can only compete with equally narrow spots; "
              "re-measure with every fleet board listening if you can",
              flush=True)
    if not kept:
        print(f"aborted: every board has fewer than {args.min_per_board} "
              "samples; collect longer", flush=True)
        return 1

    vec = median_per_board({b: samples[b] for b in kept})
    print("median vector:", json.dumps(vec))
    if not vec:
        print("aborted: no usable samples", flush=True)
        return 1

    # Serialize concurrent calibrations: two processes reading the same map
    # and both writing a fixed ".tmp" path used to lose one whole spot
    # (FileNotFoundError mid-replace, or silent last-writer-wins when both
    # finish). An exclusive advisory lock on a sidecar file is held across
    # the whole read-modify-write, so a co-run aborts fast instead of
    # clobbering the map.
    lock_fd = open(SPOTS_PATH + ".lock", "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"error: another calibrate.py is writing {SPOTS_PATH}; retry "
              "when it finishes (this run wrote nothing)", flush=True)
        return 1

    try:
        spots = load_existing_spots()
        # A map recorded for a DIFFERENT device gives confident nonsense: two
        # radios (different TX power / antenna) have no meaningful distance
        # between their fingerprints. Refuse to extend such a map silently.
        foreign = sorted({
            fp["_meta"]["device"]
            for fp in spots.values()
            if isinstance(fp, dict)
            and isinstance(fp.get("_meta"), dict)
            and fp["_meta"].get("device")
            and fp["_meta"]["device"] != args.target
        })
        if foreign:
            print(f"error: spots.json was recorded for device(s) "
                  f"{', '.join(foreign)} but --target is {args.target}; "
                  "mixing devices produces meaningless distances — re-record "
                  "this spot with the same device or start a fresh map",
                  flush=True)
            return 1

        # A spot narrower than an existing one can never win a match under
        # exact-set-equality matching — say so BEFORE saving, not as a
        # surprise only visible in the collector's refusal log.
        for name, fp in spots.items():
            fset = {b for b in fp if b != "_meta"} if isinstance(fp, dict) else set()
            if fset and len(fset) > len(vec):
                print(f"warning: this spot covers {len(vec)} board(s) but "
                      f"existing \"{name}\" covers {len(fset)} — the "
                      "collector refuses a live vector narrower than the "
                      "map's widest spot, so this spot can never win until "
                      "every fleet board is heard", flush=True)
                break

        # metadata must describe the vector actually saved: count/per_board
        # over the KEPT boards only, never the dropped ones
        kept_count = sum(len(samples[b]) for b in kept)
        kept_per_board = {b: per_board[b] for b in sorted(kept)}
        spots[args.spot] = dict(vec, _meta={"samples": args.seconds,
                                            "device": args.target,
                                            "count": kept_count,
                                            "per_board": kept_per_board})
        doc = {"spots": spots}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SPOTS_PATH) or ".",
                                   prefix=".spots-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:      # atomic: never truncate the map
            json.dump(doc, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SPOTS_PATH)
        # fsync the DIRECTORY too, so the rename survives a power cut on the
        # Pi's SD card (page-cache-only metadata can vanish on such devices)
        dir_fd = os.open(os.path.dirname(SPOTS_PATH) or ".", os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        print(f"saved {args.spot} -> {SPOTS_PATH}")
        return 0
    finally:
        lock_fd.close()


if __name__ == "__main__":
    sys.exit(main())