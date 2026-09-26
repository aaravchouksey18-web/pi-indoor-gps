"""Fingerprint map + nearest-neighbour matcher for pi-indoor-gps.

A "fingerprint" is a per-spot vector: {board: rssi}. A live vector from the
collector is matched against the map over the boards both sides share, using
plain Euclidean distance on the RSSI in dBm. The closest spot wins; confidence
is a soft 1/(1+d) so it degrades gracefully with distance.
"""
import json
import os

SPOTS_PATH = os.path.join(os.path.dirname(__file__), "spots.json")


def load_spots(path=SPOTS_PATH):
    """Return {spot_name: {board: rssi}} from the JSON map ({} if missing)."""
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f).get("spots", {})


def compute_distances(live, spots):
    """Match a live {board: rssi} vector. Returns sorted [(spot, distance)]. """
    scored = []
    for name, fp in spots.items():
        fp_boards = {k: v for k, v in fp.items() if k != "_meta"}
        common = [b for b in live if b in fp_boards]
        if not common:
            continue
        d = sum((live[b] - fp_boards[b]) ** 2 for b in common) ** 0.5
        scored.append((name, d, len(common)))
    scored.sort(key=lambda t: t[1])
    return scored


def best_match(live, spots=None):
    """Return (spot, distance, confidence) or (None, None, None) if no match."""
    spots = spots if spots is not None else load_spots()
    if not live or not spots:
        return None, None, None
    scored = compute_distances(live, spots)
    if not scored:
        return None, None, None
    name, d, _ = scored[0]
    return name, d, 1.0 / (1.0 + d)


def format_vector(live):
    return ", ".join(f"{b}={v} dBm" for b, v in sorted(live.items())) or "{}"


if __name__ == "__main__":
    import sys

    # tiny self-check against the example map
    sample = {"a": -58, "b": -70}
    print("example spots:", best_match(sample))
    sys.exit(0)