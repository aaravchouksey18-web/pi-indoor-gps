"""pi-indoor-gps collector.

Subscribes to indoor/sighting, keeps a rolling per-node RSSI view of every
device, and — when a target device is configured and spots.json exists —
publishes indoor/estimate for it.

Run:
    python3 collector.py                  # just watch what nodes report
    python3 collector.py --target aa:bb:..  # + estimate the target's spot
    python3 collector.py --boards a,b --target aa:bb:..  # fleet allowlist

The rolling window is intentionally simple for v1: each node's sighting
updates (board, mac) with the latest RSSI and a timestamp; a stale window
(older than WINDOW_S) drops the node from the live vector.

Hardening in this version:
  * resubscribes on every MQTT connect (on_connect) — a broker restart can
    no longer leave the collector permanently deaf with zero diagnostics;
  * payloads are validated by messages.parse_sighting() before touching any
    state, so a malformed/hostile message can't crash or poison the data;
  * --boards allowlist confines the collector to the nodes you actually
    trust (any LAN host can otherwise author sightings on this broker);
  * estimates only publish when the matcher is unambiguous (full fingerprint
    coverage + distance ceiling + margin), and carry a units tag so "distance"
    can't be mistaken for metres (it is the squared-Euclidean RSSI distance
    in dB).
"""
import argparse
import json
import os
import time

import paho.mqtt.client as mqtt

from fingerprint import best_match, format_vector, load_spots
from messages import normalize_mac, parse_sighting

WINDOW_S = 60.0

TOPIC_SIGHTING = "indoor/sighting"
TOPIC_ESTIMATE = "indoor/estimate"


class State:
    def __init__(self, target=None, boards=None):
        self.target = target          # lowercase mac, or None
        self.boards = boards or None  # allowed board ids, None = any
        self.nodes = {}               # board -> {mac: {"rssi": int, "ts": float}}
        self.spots = load_spots()

    def allowed(self, board):
        return self.boards is None or board in self.boards

    def prune(self):
        now = time.time()
        for board in list(self.nodes):
            for mac in list(self.nodes[board]):
                if now - self.nodes[board][mac]["ts"] > WINDOW_S:
                    del self.nodes[board][mac]
            if not self.nodes[board]:
                del self.nodes[board]

    def live_vector(self, mac):
        """{board: rssi} for one target across currently-fresh nodes."""
        now = time.time()
        vec = {}
        for board, devs in self.nodes.items():
            if mac in devs and now - devs[mac]["ts"] <= WINDOW_S:
                vec[board] = devs[mac]["rssi"]
        return vec


def make_client(state, min_boards=1):
    mqttc = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        # paho's auto-reconnect does NOT restore subscriptions: without this,
        # a broker restart would silently kill every estimate.
        client.subscribe(TOPIC_SIGHTING)
        print("collector: connected; subscribed to indoor/sighting", flush=True)

    def on_message(client, userdata, msg):
        s = parse_sighting(msg.payload)
        if s is None:
            return
        if not state.allowed(s["board"]):
            return
        state.nodes.setdefault(s["board"], {})[s["mac"]] = {"rssi": s["rssi"],
                                                            "ts": time.time()}
        state.prune()
        if state.target and s["mac"] == state.target:
            vec = state.live_vector(s["mac"])
            if len(vec) < min_boards:
                print(f"[match] {s['mac']} vector {format_vector(vec)}: "
                      f"only {len(vec)} board(s) fresh (< {min_boards})",
                      flush=True)
                return
            m = best_match(vec, state.spots)
            if m.spot is None:
                reason = ("no spots map" if not state.spots
                          else "ambiguous / out of range"
                          if vec else "no fresh nodes")
                print(f"[match] {s['mac']} vector {format_vector(vec)}: "
                      f"no match ({reason})", flush=True)
                return
            est = {"board": s["board"], "mac": s["mac"], "vector": vec,
                   "spot": m.spot, "distance": m.distance, "units": "dB",
                   "confidence": m.confidence, "margin": m.margin,
                   "matched_boards": m.boards, "ts": round(time.time(), 3)}
            client.publish(TOPIC_ESTIMATE, json.dumps(est))
            print("[estimate]", json.dumps(est), flush=True)
        else:
            print(f"[sighting] {s['board']} {s['mac']} rssi={s['rssi']}",
                  flush=True)

    mqttc.on_connect = on_connect
    mqttc.on_message = on_message
    return mqttc


def main(argv=None):
    ap = argparse.ArgumentParser(description="pi-indoor-gps collector")
    ap.add_argument("--target", default=None,
                    help="target device MAC to position (any case)")
    ap.add_argument("--boards", default=None,
                    help="comma-separated board allowlist; empty = all nodes")
    ap.add_argument("--min-boards", type=int, default=1,
                    help="minimum fresh boards before an estimate publishes")
    ap.add_argument("--host", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=1883)
    args = ap.parse_args(argv)

    target = normalize_mac(args.target) if args.target else None
    if args.target and target is None:
        print(f"warning: --target {args.target!r} is not a valid MAC; "
              "running as a passive observer", flush=True)
    if args.min_boards < 1:
        ap.error("--min-boards must be >= 1")

    boards = None
    if args.boards:
        boards = {b.strip() for b in args.boards.split(",") if b.strip()}
        print(f"allowlist: only boards {sorted(boards)} are trusted",
              flush=True)

    state = State(target=target, boards=boards)
    mqttc = make_client(state, min_boards=args.min_boards)

    while True:                     # retry broker-down at startup
        try:
            mqttc.connect(args.host, args.port, 30)
            break
        except OSError as e:
            print(f"warning: broker {args.host}:{args.port} unavailable "
                  f"({e}); retrying in 5 s", flush=True)
            time.sleep(5)
    print(f"collector on {args.host}:{args.port}, target={target or 'any'}")
    mqttc.loop_forever()


if __name__ == "__main__":
    main()