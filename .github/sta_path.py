"""Worst setup path of each signoff corner, as annotations.

Usage: python sta_path.py <folder that holds runs/wokwi>

Reads runs/wokwi/<nn>-openroad-stapostpnr/<corner>/checks.rpt and prints, for each corner, the
first path of the path group "flops" (src/signoff.sdc) from its Startpoint line to its slack line,
as one notice annotation (the public GitHub API returns it as text: check-runs/<job id>/annotations).
"""

import glob
import os
import sys


def notice(title, text):
    text = text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::notice title={title}::{text}")


def first_path(checks, group):
    at = checks.find(f"Path Group: {group}")
    if at < 0:
        return None
    start = checks.rfind("Startpoint:", 0, at)
    end = checks.find("slack", at)
    if start < 0 or end < 0:
        return None
    end = checks.find("\n", end)
    return checks[start:end if end >= 0 else len(checks)]


def main(root):
    sta = sorted(glob.glob(os.path.join(root, "runs", "wokwi", "*-openroad-stapostpnr")))
    if not sta:
        notice("sta path", f"no stapostpnr step under {root}")
        return 1
    for checks_path in sorted(glob.glob(os.path.join(sta[-1], "*", "checks.rpt"))):
        corner = os.path.basename(os.path.dirname(checks_path))
        with open(checks_path, encoding="utf-8", errors="replace") as f:
            path = first_path(f.read(), "flops")
        notice(f"{corner} worst flops setup path", path or "path group flops not found")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
