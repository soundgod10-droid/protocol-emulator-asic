"""Signoff facts of a LibreLane run folder, for the run summary and as annotations.

Usage: python signoff_facts.py <folder that holds runs/wokwi> <label>

Reads the signoff STA step (runs/wokwi/<nn>-openroad-stapostpnr): summary.rpt of all
corners, and from each <corner>/checks.rpt the worst flip-flop setup slack (path
group "flops", src/signoff.sdc), the max slew / cap / fanout violation counts and the
fanout violators. Adds selected rows of runs/wokwi/final/metrics.csv. Writes markdown
to $GITHUB_STEP_SUMMARY and prints one notice annotation per item.
"""

import csv
import glob
import os
import re
import sys

CLOCK_TREE = ("clkbuf_", "clkload", "delaybuf_")
FANOUT_LINE = re.compile(r"^(\S+/\S+)\s+(\d+)\s+(\d+)\s+(-?\d+)?\s*\(VIOLATED\)\s*$")
METRICS = re.compile(
    r"^(timing__(setup|hold)(_r2r)?(_vio)?__(ws|count)"
    r"|design__max_(slew|fanout|cap)_violation__count"
    r"|clock__skew__worst_(setup|hold))(__corner:.*)?$"
    r"|^(route__drc_errors|design__lvs_error__count|antenna__violating__nets|power__total"
    r"|design__instance__utilization|design__instance__count__stdcell"
    r"|design__instance__area__stdcell|design__instance__count__hold_buffer"
    r"|design__instance__count__class:(clock_buffer|clock_inverter|timing_repair_buffer"
    r"|sequential_cell)|route__wirelength|design__disconnected_pin__count"
    r"|design__power_grid_violation__count)$"
)


def notice(title, text):
    text = text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::notice title={title}::{text}")


def summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def corner_facts(checks):
    facts = []
    group = checks.find("Path Group: flops")
    m = re.search(r"(-?\d+\.\d+)\s+slack \((MET|VIOLATED)\)", checks[group:]) if group >= 0 else None
    facts.append(f"flops setup slack {m.group(1)} ({m.group(2)})" if m else "flops setup slack not found")
    for kind in ("slew", "fanout", "cap"):
        c = re.search(rf"max {kind} violation count (\d+)", checks)
        facts.append(f"max {kind} violations {c.group(1) if c else '?'}")
    clock, other = [], []
    for line in checks.splitlines():
        v = FANOUT_LINE.match(line.strip())
        if v:
            pin, fanout = v.group(1), int(v.group(3))
            (clock if pin.startswith(CLOCK_TREE) else other).append((pin, fanout))
    if clock:
        fo = [f for _, f in clock]
        facts.append(f"fanout violators in the clock tree {len(clock)} (fanout {min(fo)} to {max(fo)})")
    if other:
        facts.append("other fanout violators " + ", ".join(f"{p} {f}" for p, f in other))
    return facts


def main(root, label):
    wokwi = os.path.join(root, "runs", "wokwi")
    sta_dirs = sorted(glob.glob(os.path.join(wokwi, "*-openroad-stapostpnr")))
    if not sta_dirs:
        notice(f"{label} signoff", f"no stapostpnr step under {wokwi}")
        return 1
    sta = sta_dirs[-1]
    summary(f"## Signoff facts of {label} ({os.path.basename(sta)})")

    rows = []
    for line in read(os.path.join(sta, "summary.rpt")).splitlines():
        if line.startswith("│"):
            rows.append([c.strip() for c in line.strip("│").split("│")])
    header = ("corner | hold ws | hold r2r ws | hold tns | hold vio | hold r2r vio | setup ws"
              " | setup r2r ws | setup tns | setup vio | setup r2r vio | max cap vio | max slew vio")
    table = [header] + [" | ".join(r) for r in rows]
    notice(f"{label} summary.rpt", "\n".join(table))
    summary("| " + header + " |\n|" + "---|" * 13 + "\n" + "\n".join("| " + " | ".join(r) + " |" for r in rows))

    for corner_dir in sorted(glob.glob(os.path.join(sta, "*", "checks.rpt"))):
        corner = os.path.basename(os.path.dirname(corner_dir))
        facts = corner_facts(read(corner_dir))
        notice(f"{label} {corner}", "\n".join(facts))
        summary(f"\n**{corner}**: " + "; ".join(facts))

    metrics_path = os.path.join(wokwi, "final", "metrics.csv")
    if os.path.exists(metrics_path):
        with open(metrics_path, newline="", encoding="utf-8") as f:
            picked = [f"{k}={v}" for k, v in csv.reader(f) if METRICS.match(k)]
        notice(f"{label} metrics.csv", "\n".join(picked))
        summary("\n<details><summary>metrics.csv (selected)</summary>\n\n```\n" + "\n".join(picked) + "\n```\n</details>")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
