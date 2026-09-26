# measurements

Fingerprint maps and raw RSSI dumps produced while building the radio map.

These can reference your own devices (router BSSIDs, phone MACs), so:

- `pi/spots.json` and `measurements/raw/` are **gitignored**.
- Keep sanitized, anonymized summaries (counts, errors, charts) if anything
  here ever needs to go into the repo.

Ideas for this dir: per-spot RSSI distributions before/after calibration,
confusion matrices for the matcher, node coverage maps.