"""Coverage facts of a coverage artifact (coverage.yaml), as notice annotations.

Usage:
    python coverage_facts.py <artifact folder> <label> summary
    python coverage_facts.py <artifact folder> <label> <file.v> ...

"summary": the coverage table (coverage_summary.md), the test results (results.xml)
and, for each annotated source file, the number of lines with no hit point ("%")
and with some points not hit ("~"). With file names: the lines of those files that
have no hit point, with their line numbers (the annotated files keep the source
line order).
"""

import glob
import os
import sys
import xml.etree.ElementTree as ET


def notice(title, text):
    text = text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::notice title={title}::{text}")


def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def annotated_files(root):
    return sorted(glob.glob(os.path.join(root, "coverage_annotated", "**", "*.v"), recursive=True))


def main(root, label, args):
    if args == ["summary"]:
        notice(f"{label} coverage summary", read(os.path.join(root, "coverage_summary.md")))
        cases = ET.parse(os.path.join(root, "results.xml")).findall(".//testcase")
        failed = [c.get("name") for c in cases if c.find("failure") is not None or c.find("error") is not None]
        skipped = [c.get("name") for c in cases if c.find("skipped") is not None]
        notice(f"{label} results.xml",
               f"{len(cases)} testcases, {len(failed)} failed, {len(skipped)} skipped\n"
               f"failed: {', '.join(failed)}\nskipped: {', '.join(skipped)}")
        counts = []
        for path in annotated_files(root):
            lines = read(path).splitlines()
            zero = sum(1 for l in lines if l.startswith("%"))
            partial = sum(1 for l in lines if l.startswith("~"))
            counts.append(f"{os.path.basename(path)}: {zero} lines with no point hit, {partial} lines with some points not hit")
        notice(f"{label} annotated lines", "\n".join(counts))
        return 0
    for path in annotated_files(root):
        name = os.path.basename(path)
        if name not in args:
            continue
        lines = read(path).splitlines()
        zero = [f"{n}: {l.strip()}" for n, l in enumerate(lines, 1) if l.startswith(("%", "~"))]
        notice(f"{label} {name} lines with points not hit (% none hit, ~ some)", "\n".join(zero[:80]) or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3:]))
