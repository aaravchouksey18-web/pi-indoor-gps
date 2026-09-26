"""Fingerprint map + nearest-neighbour matcher for pi-indoor-gps.

A "fingerprint" is a per-spot vector: {board: rssi}. A live vector from the
collector is matched against the map over the boards both sides share, using
plain Euclidean distance on the RSSI in dBm.

Matching policy (the parts that keep estimates honest):

  * **Full coverage** — a spot candidate must see *every* board its
    fingerprint records. A 1-board fingerprint can no longer win against a
    2-board one just because it happens to share one node; partial matches
    are dropped, not scored.
  * **Distance ceiling** — beyond MAX_DIST_DB the measured vector is too far
    from any spot to claim one.
  * **Margin** — the best spot must beat the runner-up by MIN_MARGIN_DB,
    otherwise the answer is ambiguous and we refuse to publish instead of
    guessing between two similar spots.
"""
import json
import os
from collections import namedtuple

SPOTS_PATH = os.path.join(os.path.dirname(__file__), "spots.json")

# Euclidean-distance ceiling for a "real" match (in dB), and the
# margin the winner must hold over the runner-up to be unambiguous.
MAX_DIST_DB = 48.0
MIN_MARGIN_DB = 2.5

Match = namedtuple("Match", "spot distance confidence margin boards")


def load_spots(path=SPOTS_PATH):
    """Return {spot_name: {board: rssi}} from the JSON map ({} if missing).

    Never raises: a missing or corrupted spots.json means "no map yet",
    which the caller already treats as no match. Spot records that are not
    {board: rssi} dicts (e.g. from a hand-edit) are dropped, and only finite
    numeric RSSI values are kept, so a malformed map file can never crash a
    collector inside an MQTT callback.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    spots = raw.get("spots") if isinstance(raw, dict) else None
    if not isinstance(spots, dict):
        return {}
    out = {}
    for name, fp in spots.items():
        if not isinstance(fp, dict):
            continue
        clean = {}
        for board, rssi in fp.items():
            if board == "_meta":
                continue
            try:
                value = float(rssi)
            except (TypeError, ValueError):
                continue
            if value == value and value != float("inf") and value != float("-inf"):
                clean[board] = value
        if clean:
            out[name] = clean
    return out


def compute_distances(live, spots):
    """Score live vs every fingerprint that has FULL coverage.

    Returns sorted [(spot, distance, boards_matched)] or [].
    """
    scored = []
    for name, fp in spots.items():
        fp_boards = {k: v for k, v in fp.items() if k != "_meta"}
        if not fp_boards:
            continue
        common = [b for b in live if b in fp_boards]
        if len(common) < len(fp_boards):    # incomplete fingerprint: no credit
            continue
        d = sum((live[b] - fp_boards[b]) ** 2 for b in common)
        dist = d ** 0.5
        scored.append((name, round(dist, 2), len(common)))
    scored.sort(key=lambda t: t[1])
    return scored


def best_match(live, spots=None, max_dist_db=MAX_DIST_DB,
               min_margin_db=MIN_MARGIN_DB):
    """Return a Match, or Match(None, None, None, None, 0) if no honest match.

    `live` is a {board: rssi} vector from the collector; `spots` defaults to
    the on-disk fingerprint map.
    """
    spots = spots if spots is not None else load_spots()
    if not live or not spots:
        return Match(None, None, None, None, 0)
    scored = compute_distances(live, spots)
    if not scored:
        return Match(None, None, None, None, 0)
    name, dist, boards = scored[0]
    if dist > max_dist_db:
        return Match(None, None, None, None, 0)
    margin = None
    if len(scored) > 1:
        margin = round(scored[1][1] - dist, 2)
        if margin < min_margin_db:
            return Match(None, None, None, None, 0)     # ambiguous
    confidence = round(1.0 / (1.0 + dist), 3)
    return Match(name, dist, confidence, margin, boards)


def format_vector(live):
    return ", ".join(f"{b}={v} dBm" for b, v in sorted(live.items())) or "{}"


if __name__ == "__main__":
    import sys

    # tiny self-check against the example map
    sample = {"a": -58, "b": -70}
    m = best_match(sample)
    print("example spots:", m)
    # a partial (1-of-2 board) vector must NOT win any match now
    one_board = {"a": -58}
    m2 = best_match(one_board)
    print("partial-coverage match:", m2, "-> should be None/empty")
    sys.exit(0)