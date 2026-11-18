# SPDX-License-Identifier: Apache-2.0
"""Published order of the non-pool candidates (flex/candidates.csv) and the beacon split of the blind arm.

What it does:
  Without an argument it prints the score order of list C (docs/counting.md section 7): score V = O + R + E - H,
  rows marked last=yes at the end, ties by lower central hours, then by id.
  With the drand randomness R of the round named in flex/BLIND.md (64 lowercase hexadecimal characters), or with
  --round N to fetch it from the drand quicknet chain, it also prints the blind arm B (the 8 ids of C with the
  smallest SHA-256(R + id), in that digest order) and the pre-freeze list P (the other ids of C in score order).

Usage: python flex/order.py [R | --round N]
"""

import csv
import hashlib
import json
import os
import sys
import urllib.request

CHAIN = "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"
GENESIS, PERIOD = 1692803367, 3
BLIND_N = 8


def rows(list_name):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "candidates.csv")
    with open(path, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r["list"] == list_name]


def score(r):
    return int(r["o"]) + int(r["r"]) + int(r["e"]) - int(r["h"])


def score_order(rs):
    return sorted(rs, key=lambda r: (r["last"] == "yes", -score(r), float(r["h_central"]), r["id"]))


def digest(randomness, ident):
    return hashlib.sha256((randomness + ident).encode("ascii")).hexdigest()


def split(randomness, c_rows):
    ids = [r["id"] for r in c_rows]
    blind = sorted(ids, key=lambda d: digest(randomness, d))[:BLIND_N]
    rest = [r for r in score_order(c_rows) if r["id"] not in blind]
    return blind, rest


def fetch(round_no):
    url = f"https://api.drand.sh/{CHAIN}/public/{round_no}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())["randomness"]


def main(argv):
    c_rows = rows("C")
    randomness = None
    if argv[:1] == ["--round"]:
        randomness = fetch(int(argv[1]))
        print(f"round {argv[1]} (published {GENESIS + PERIOD * (int(argv[1]) - 1)} unix time): randomness {randomness}")
    elif argv:
        randomness = argv[0].strip().lower()
    print(f"list C: {len(c_rows)} rows; reserve R: {len(rows('R'))} rows")
    print("score order of C (order, id, V, central hours):")
    for i, r in enumerate(score_order(c_rows), 1):
        print(f"  {i:2d} {r['id']:<12} V {score(r):+d}  {r['h_central']} h{'  (last)' if r['last'] == 'yes' else ''}")
    if randomness is None:
        return
    assert len(randomness) == 64 and all(ch in "0123456789abcdef" for ch in randomness), "R: 64 hex characters"
    blind, rest = split(randomness, c_rows)
    print(f"blind arm B ({BLIND_N}, by SHA-256(R + id)): {' '.join(blind)}")
    print(f"pre-freeze list P ({len(rest)}, score order): {' '.join(r['id'] for r in rest)}")


if __name__ == "__main__":
    main(sys.argv[1:])
