"""Record a fingerprint for one spot.

Usage:
    python3 calibrate.py --spot kitchen --target aa:bb:cc:dd:ee:ff [--seconds 60]

Sits on indoor/sighting for the window, collects every RSSI sample the living
nodes report for the target device, takes the median per board, and upserts
that vector into spots.json (gitignored — fingerprints reference your own
device MACs).
"""
import argparse
import json
import os
import statistics
import time

import paho.mqtt.client as mqtt

SPOTS_PATH = os.path.join(os.path.dirname(__file__), "spots.json")


def median_per_board(samples):
    return {b: round(statistics.median(v), 1) for b, v in samples.items() if v}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spot", required=True, help="name of the spot, e.g. kitchen")
    ap.add_argument("--target", required=True, help="target device MAC (lowercase)")
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--min-samples", type=int, default=8)
    args = ap.parse_args()

    samples = {}  # board -> [rssi...]

    def on_message(client, userdata, msg):
        try:
            p = json.loads(msg.payload.decode())
        except (ValueError, UnicodeDecodeError):
            return
        if p.get("mac") != args.target or "rssi" not in p:
            return
        samples.setdefault(p["board"], []).append(int(p["rssi"]))

    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqttc.on_message = on_message
    mqttc.connect(args.host, args.port, 30)
    mqttc.subscribe("indoor/sighting")
    mqttc.loop_start()

    print(f"collecting for {args.spot}: {args.seconds}s on indoor/sighting")
    time.sleep(args.seconds)
    mqttc.loop_stop()

    vec = median_per_board(samples)
    print("raw per-board samples:", json.dumps(
        {b: len(v) for b, v in samples.items()}))
    print("median vector:", json.dumps(vec))

    if len(vec) < args.min_samples or not vec:
        print(f"aborted: need >= {args.min_samples} distinct boards with samples")
        return

    spots = {}
    if os.path.exists(SPOTS_PATH):
        with open(SPOTS_PATH) as f:
            spots = json.load(f).get("spots", {})
    spots[args.spot] = dict(vec, _meta={"samples": args.seconds,
                                        "device": args.target,
                                        "count": sum(len(v) for v in samples.values())})
    with open(SPOTS_PATH, "w") as f:
        json.dump({"spots": spots}, f, indent=2)
    print(f"saved {args.spot} -> {SPOTS_PATH}")


if __name__ == "__main__":
    main()