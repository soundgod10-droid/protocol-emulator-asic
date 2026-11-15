"""Summarize a Verilator coverage.dat by coverage type and source file.

Usage: python coverage_summary.py coverage.dat

Prints a markdown table: for each coverage type (line, branch, toggle, ...) and
each RTL source file, the number of coverage points and how many were hit at
least once. The test bench (tb.v) is left out.

coverage.dat holds one record per point:
    C '<sep1>key<sep2>value<sep1>key<sep2>value...' count
with sep1 = 0x01 and sep2 = 0x02. Key "page" is "v_<type>/<module>", key "f"
is the source file.
"""

import sys
from collections import defaultdict

TESTBENCH = {"tb.v"}


def parse(line):
    start = line.index("'") + 1
    end = line.rindex("'")
    fields = {}
    for item in line[start:end].split("\x01"):
        if "\x02" in item:
            key, value = item.split("\x02", 1)
            fields[key] = value
    return fields, int(line[end + 1 :].strip())


def main(path):
    total = defaultdict(int)
    hit = defaultdict(int)
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.startswith("C "):
                continue
            fields, count = parse(line)
            kind = fields.get("page", "?").split("/")[0].removeprefix("v_")
            src = fields.get("f", "?").replace("\\", "/").rsplit("/", 1)[-1]
            if src in TESTBENCH:
                continue
            for key in ((kind, src), (kind, "(all RTL files)")):
                total[key] += 1
                hit[key] += count > 0

    print("| Type | File | Points hit | Points | Coverage |")
    print("|---|---|---|---|---|")
    for kind, src in sorted(total, key=lambda k: (k[0], k[1].startswith("("), k[1])):
        n = total[(kind, src)]
        h = hit[(kind, src)]
        print(f"| {kind} | {src} | {h} | {n} | {100.0 * h / n:.1f} % |")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "coverage.dat")
