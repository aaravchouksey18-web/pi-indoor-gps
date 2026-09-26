"""pi-indoor-gps collector.

Subscribes to indoor/sighting, keeps a rolling per-node RSSI view of every
device, and — when a target device is configured and spots.json exists —
publishes indoor/estimate for it.

Run:
    python3 collector.py                  # just watch what nodes report
    python3 collector.py --target aa:bb:..  # + estimate the target's spot

The rolling window is intentionally simple for v1: each node's sighting
updates (board, mac) with the latest RSSI and a timestamp; a stale window
(older than WINDOW_S) drops the node from the live vector.
"""
import argparse
import json
import os
import time

import paho.mqtt.client as mqtt

from fingerprint import best_match, format_vector, load_spots

WINDOW_S = 60.0

TOPIC_SIGHTING = "indoor/sighting"
TOPIC_ESTIMATE = "indoor/estimate"


class State:
    def __init__(self, target=None):
        self.target = target          # lowercase mac, or None
        self.boards = {}              # board -> {mac: {"rssi": int, "ts": float}}
        self.spots = load_spots()

    def prune(self):
        now = time.time()
        for board in list(self.boards):
            for mac in list(self.boards[board]):
                if now - self.boards[board][mac]["ts"] > WINDOW_S:
                    del self.boards[board][mac]
            if not self.boards[board]:
                del self.boards[board]

    def live_vector(self, mac):
        """{board: rssi} for one target across currently-fresh nodes."""
        now = time.time()
        vec = {}
        for board, devs in self.boards.items():
            if mac in devs and now - devs[mac]["ts"] <= WINDOW_S:
                vec[board] = devs[mac]["rssi"]
        return vec


def make_client(state):
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def on_message(client, userdata, msg):
        try:
            p = json.loads(msg.payload.decode())
        except (ValueError, UnicodeDecodeError):
            return
        board, mac = p.get("board"), p.get("mac")
        if not board or not mac:
            return
        rssi = p.get("rssi")
        if rssi is None:
            return
        state.boards.setdefault(board, {})[mac] = {"rssi": int(rssi),
                                                   "ts": time.time()}
        state.prune()
        if state.target and mac == state.target:
            vec = state.live_vector(mac)
            if len(vec) >= 1:
                spot, dist, conf = best_match(vec, state.spots)
                if spot:
                    est = {"board": board, "mac": mac, "vector": vec,
                           "spot": spot, "distance": round(dist, 1),
                           "confidence": round(conf, 3)}
                    client.publish(TOPIC_ESTIMATE, json.dumps(est))
                    print("[estimate]", json.dumps(est), flush=True)
                else:
                    print(f"[match] {mac} vector {format_vector(vec)}: no spots map",
                          flush=True)
        else:
            print(f"[sighting] {board} {mac} rssi={rssi}", flush=True)

    mqttc.on_message = on_message
    return mqttc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=None, help="target device MAC to position")
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=1883)
    args = ap.parse_args()

    state = State(target=args.target)
    mqttc = make_client(state)
    mqttc.connect(args.host, args.port, 30)
    mqttc.subscribe(TOPIC_SIGHTING)
    print(f"collector on {args.host}:{args.port}, target={state.target or 'any'}")
    mqttc.loop_forever()


if __name__ == "__main__":
    main()