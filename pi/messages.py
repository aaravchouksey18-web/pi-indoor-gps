"""Shared MQTT payload handling for the pi scripts.

Indoor positioning runs on a LAN-trusted broker (mosquitto, no auth), so the
collectors must be defensive: every payload arriving on indoor/sighting is
untrusted input. parse_sighting() validates shape + range and returns a
normalized dict or None, so a malformed or hostile message can never crash a
collector or poison a fingerprint. The other untrusted file is the
fingerprint map (spots.json), which fingerprint.load_spots() validates
separately before anything scores against it.
"""
import json
import re

# firmware publishes lowercase "aa:bb:cc:dd:ee:ff" MACs (see publish_seen)
_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


def normalize_mac(raw):
    """Lowercase + validate a MAC address; None if it can't be one."""
    if not isinstance(raw, str):
        return None
    mac = raw.strip().lower()
    return mac if _MAC_RE.match(mac) else None


def parse_sighting(payload_bytes):
    """Validate a raw indoor/sighting payload.

    Returns {"board", "mac", "rssi"} (mac lowercased) or None. Rules:

      - must parse as a JSON object
      - board: non-empty string, at most 16 chars
      - mac:   a valid 6-byte MAC (lowercased)
      - rssi:  an integer in the plausible dBm band [-150, 0]

    Anything else is dropped silently — a collector must never die (or
    publish a bogus estimate) because one node sent garbage.
    """
    try:
        p = json.loads(payload_bytes.decode())
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    if not isinstance(p, dict):
        return None
    board = p.get("board")
    if not isinstance(board, str) or not board.strip() or len(board) > 16:
        return None
    mac = normalize_mac(p.get("mac"))
    if mac is None:
        return None
    rssi = p.get("rssi")
    try:
        rssi = int(rssi)
    except (TypeError, ValueError):
        return None
    if not -150 <= rssi <= 0:
        return None
    return {"board": board.strip(), "mac": mac, "rssi": rssi}