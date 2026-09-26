"""Fingerprint map + nearest-neighbour matcher for pi-indoor-gps.

A "fingerprint" is a per-spot vector: {board: rssi}. A live vector from the
collector is matched against the map over the boards both sides share, using
plain Euclidean distance on the RSSI in dBm.

Matching policy (the parts that keep estimates honest):

  * **Full coverage, both ways** — a spot candidate must explain
    *every* board the live vector reports AND see every board its own
    fingerprint records. A 1-board fingerprint can no longer beat a 2-board
    one by sharing one node while ignoring a contradicting one; a node the
    map does not know blocks the match until the map is recalibrated.
  * **Distance ceiling** — beyond MAX_DIST_DB the measured vector is too far
    from any spot to claim one.
  * **Margin** — the best spot must beat the runner-up by MIN_MARGIN_DB,
    otherwise the answer is ambiguous and we refuse to publish instead of
    guessing between two similar spots.
"""
import json
import math
import os
from collections import namedtuple

SPOTS_PATH = os.path.join(os.path.dirname(__file__), "spots.json")

# Euclidean-distance ceiling for a "real" match (in dB), and the
# margin the winner must hold over the runner-up to be unambiguous.
MAX_DIST_DB = 48.0
MIN_MARGIN_DB = 2.5

Match = namedtuple("Match", "spot distance confidence margin boards reason")


def _sanitize_spots(spots):
    """Keep only {board: finite float} fingerprints from a loaded map.

    Never raises: spot records that are not dicts, boards named "_meta",
    and any rssi that does not coerce to a finite float (including huge
    integer literals, Infinity and NaN) are dropped.
    """
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
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(value):
                clean[board] = value
        if clean:
            out[name] = clean
    return out


def load_spots(path=SPOTS_PATH):
    """Return {spot_name: {board: rssi}} from the JSON map ({} if missing).

    Never raises: a missing or corrupted spots.json means "no map yet",
    which the caller already treats as no match. The returned map is fully
    sanitized (see _sanitize_spots), so State() and the matcher can always
    assume plain finite floats.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    spots = raw.get("spots") if isinstance(raw, dict) else None
    return _sanitize_spots(spots)


def compute_distances(live, spots):
    """Score live vs every fingerprint that has FULL coverage.

    Returns sorted [(spot, distance, boards_matched)] or [].
    """
    scored = []
    for name, fp in spots.items():
        fp_boards = {k: v for k, v in fp.items() if k != "_meta"}
        if not fp_boards:
            continue
        # coverage runs BOTH ways and must be exact: the live vector and the
        # fingerprint must name the same boards. A spot that ignores a live
        # board could dodge contradicting evidence (a 1-board spot must not
        # beat a 2-board one), and a vector missing a board the spot was
        # calibrated with has not fully verified it — both are refused.
        if set(live) != set(fp_boards):
            continue
        d = sum((live[b] - fp_boards[b]) ** 2 for b in live)
        dist = d ** 0.5
        scored.append((name, round(dist, 2), len(live)))
    scored.sort(key=lambda t: t[1])
    return scored


def best_match(live, spots=None, max_dist_db=MAX_DIST_DB,
               min_margin_db=MIN_MARGIN_DB):
    """Return a Match, or Match(None, ..., reason) if no honest match.

    `reason` describes how the refusal happened so the collector can give a
    useful diagnostic: "no_live" / "no_map" / "coverage" (no candidate
    explains every live board) / "distance" / "ambiguous" / None on success.

    `live` is a {board: rssi} vector from the collector; `spots` defaults to
    the on-disk fingerprint map and is sanitized either way.
    """
    spots = _sanitize_spots(spots) if spots is not None else load_spots()
    if not live:
        return Match(None, None, None, None, 0, "no_live")
    if not spots:
        return Match(None, None, None, None, 0, "no_map")
    scored = compute_distances(live, spots)
    if not scored:
        return Match(None, None, None, None, 0, "coverage")
    name, dist, boards = scored[0]
    if dist > max_dist_db:
        return Match(None, None, None, None, 0, "distance")
    margin = None
    if len(scored) > 1:
        margin = round(scored[1][1] - dist, 2)
        if margin < min_margin_db:
            return Match(None, None, None, None, 0, "ambiguous")  # too close
    confidence = round(1.0 / (1.0 + dist), 3)
    return Match(name, dist, confidence, margin, boards, None)


def format_vector(live):
    return ", ".join(f"{b}={v} dBm" for b, v in sorted(live.items())) or "{}"


if __name__ == "__main__":
    import sys

    # tiny self-check vs the checked-in example map (spots.json is
    # gitignored; spots.json.example is the shape reference)
    example = os.path.join(os.path.dirname(__file__), "spots.json.example")
    spots = load_spots(example)
    print("example spots:", spots)
    sample = {"a": -58, "b": -70}
    m = best_match(sample, spots=spots)
    print("example match:", m)
    # a partial (1-of-2 board) vector must NOT win any match now
    one_board = {"a": -58}
    m2 = best_match(one_board, spots=spots)
    print("partial-coverage match:", m2, "-> should be None/empty")
    sys.exit(0 if (m.spot and m2.spot is None) else 1)