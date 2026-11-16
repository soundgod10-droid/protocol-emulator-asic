# SPDX-License-Identifier: Apache-2.0
"""Reference trace hashes of every example under CPython (check L1, CPython side).

Use: python -I fidelity_cpython.py <web folder> <output.json> [example ids ...]

What it does:
  Imports web/py/v1_runner.py and web/py/examples.py with the chip files of web/py/pe (the same
  files that the page loads), runs each example procedure to its end and writes, for each example:
  the test check result, the trace hash (SHA-256), the cycles of the main bench, the cycles of the
  other benches of the procedure (searches), the wall time and the cycles per second. It also runs
  each example a second time with the page's split of the run (a run() part ends every 997 cycles
  and at every state copy) to show that the hash does not depend on how the run is split.

Why:
  The page in check mode runs the same examples under Pyodide and shows its hashes; equal hashes
  mean that the browser run is the CPython run, cycle for cycle.
"""

import json
import os
import platform
import sys
import time


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    web, out = os.path.abspath(argv[1]), os.path.abspath(argv[2])
    sys.path.insert(0, os.path.join(web, "py"))
    import examples  # noqa: E402
    import v1_runner  # noqa: E402

    ids = argv[3:] or [e["id"] for e in examples.EXAMPLES]
    rows = []
    for ex_id in ids:
        ex = examples.BY_ID[ex_id]
        s = v1_runner.run_example(ex)
        r = dict(s.result)
        # Second run with a different split: SNAP 997 cycles.
        saved = v1_runner.SNAP
        v1_runner.SNAP = 997
        try:
            s2 = v1_runner.run_example(ex)
        finally:
            v1_runner.SNAP = saved
        r["hash_split_997"] = s2.result.get("hash")
        r["id"] = ex_id
        r["cycles_per_s"] = r["cycles_main"] / r["wall_s"] if r["wall_s"] else None
        r["all_cycles_per_s"] = (r["cycles_main"] + r["cycles_other"]) / r["wall_s"] if r["wall_s"] else None
        r.pop("log", None)
        r.pop("failed_files", None)
        rows.append(r)
        print(f"{ex_id:5} {'pass' if r['ok'] else 'FAIL'} cycles {r.get('cycles', 0):>9,} "
              f"other {r['cycles_other']:>9,} {r['wall_s']:7.2f} s {r['cycles_per_s'] or 0:>9,.0f} c/s "
              f"hash {str(r.get('hash'))[:16]} split-equal {r.get('hash') == r['hash_split_997']}  {r['text'][:90]}",
              flush=True)
    meta = {"python": sys.version, "implementation": platform.python_implementation(), "machine": platform.machine(),
            "processor": platform.processor(), "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sources": json.load(open(os.path.join(web, "py", "SOURCES.json")))["commit"]}
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "examples": rows}, f, indent=1)
    return 0 if all(r["ok"] and r.get("hash") == r["hash_split_997"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
