# SPDX-License-Identifier: Apache-2.0
"""Browser checks of the simulator site in headless Chrome: L1 (fidelity, browser side) and L5 (page smoke test).

Use: python -I sim/tools/page_check.py SITE_ROOT OUT_DIR

What it does:
  Serves SITE_ROOT on 127.0.0.1 (the simulator under /sim/, as on GitHub Pages) and runs these checks in one headless
  Chrome (sim/tools/cdp.py); each must pass:
    L1  index.html?check=1: the page runs every example under Pyodide; each example's test check passes and its trace
        hash equals the CPython hash in check/cpython.json (made in the same build from the same files).
    L5a index.html?ex=E3 (four threads at once): the engine loads and the example runs to its end with its test check
        passing.
    L5b an unknown instruction put into the firmware editor gives the assembler's "file:line: message" text and an
        error marker in the editor, and the edited line stays in view when the page's scroll to the PC line comes
        after the edit (forced).
    L5c phone width (390 x 844 CSS pixels): E1 runs and the page is not wider than the screen.
    L5d wave/demo.html: every recorded sample loads and each of its decoders gives records without an error.
    No exception in the page or its worker during the checks.
  Writes OUT_DIR/page_check.json and PNG screenshots.

Why:
  CI must show that the deployed page loads, runs the examples bit-identical to CPython, and shows the waveforms,
  before the site is deployed.
"""

import functools
import http.server
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cdp import Browser  # noqa: E402

DONE = ("(() => { const e = document.getElementById('ex-check'); "
        "return !!e && (e.className === 'pass' || e.className === 'fail'); })()")

# Line 5 of the firmware editor inside the editor's visible box, and the first line in view.
LINE5_VIEW = ("(() => { const v = window.__v1.editor.view; const r = v.coordsAtPos(v.state.doc.line(5).from);"
              " const b = v.scrollDOM.getBoundingClientRect();"
              " return {in_view: !!r && r.top >= b.top - 1 && r.bottom <= b.bottom + 1,"
              " first: v.state.doc.lineAt(v.lineBlockAtHeight(v.scrollDOM.scrollTop).from).number}; })()")

# Innermost elements that pass the right edge of the screen and are not inside a box that scrolls or clips them.
OVERFLOW = """(() => { const W = document.documentElement.clientWidth, out = [];
  const name = (e) => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') +
    (typeof e.className === 'string' && e.className.trim() ? '.' + e.className.trim().split(/\\s+/).join('.') : '');
  const over = (e) => e.getBoundingClientRect().right > W + 0.5;
  for (const e of document.querySelectorAll('body *')) {
    if (!e.getBoundingClientRect().width || !over(e) || [...e.children].some(over)) continue;
    let p = e.parentElement, clipped = false;
    for (; p && p !== document.body; p = p.parentElement) if (getComputedStyle(p).overflowX !== 'visible') clipped = true;
    if (clipped) continue;
    const path = []; for (let q = e; q && q !== document.body && path.length < 4; q = q.parentElement) path.unshift(name(q));
    out.push(`${path.join(' > ')} (right edge ${Math.round(e.getBoundingClientRect().right)} px)`); }
  return out.slice(0, 10); })()"""


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript",
                      ".mjs": "text/javascript", ".wasm": "application/wasm", ".json": "application/json",
                      ".vcd": "text/plain", ".md": "text/plain", ".py": "text/plain", ".s": "text/plain"}

    def log_message(self, *args):
        pass


def serve(root):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=root))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    root, out = os.path.abspath(argv[1]), os.path.abspath(argv[2])
    os.makedirs(out, exist_ok=True)
    httpd = serve(root)
    base = f"http://127.0.0.1:{httpd.server_address[1]}/sim"
    ref = json.load(open(os.path.join(root, "sim", "check", "cpython.json"), encoding="utf-8"))
    man = json.load(open(os.path.join(root, "sim", "wave", "samples", "manifest.json"), encoding="utf-8"))
    res = {"checks": {}}
    ok = {}
    with Browser(9333, tempfile.mkdtemp(prefix="simcheck_")) as b:
        # L1
        b.viewport(1440, 900)
        b.open(f"{base}/index.html?check=1")
        _, t = b.wait("document.title.startsWith('CHECK')", timeout=1800, every=1.0)
        r = json.loads(b.eval("document.getElementById('check-result').textContent"))
        rows = [{k: x.get(k) for k in ("id", "ok", "hash", "ref", "equal", "cycles", "cycles_main", "cycles_other",
                                       "wall_s", "cycles_per_s")} for x in r["rows"]]
        ok["L1"] = bool(r["all_equal"]) and len(rows) == len(ref["examples"])
        res["checks"]["L1"] = {"pass": ok["L1"], "examples": len(rows), "cpython_examples": len(ref["examples"]),
                               "cpython": ref["meta"]["python"], "wait_s": round(t, 1), "boot": r["boot"],
                               "user_agent": r["ua"], "rows": rows}
        b.shot(os.path.join(out, "check_1440.png"), full=True)
        for x in rows:
            print(f"L1 {x['id']:5} {'pass' if x['ok'] else 'FAIL'} {x['cycles'] or 0:>9,} cycles {x['wall_s']:7.2f} s "
                  f"{x['cycles_per_s'] or 0:>9,.0f} cycles/s  {str(x['hash'])[:16]}  equal to CPython: {x['equal']}")
        print(f"L1 {'PASS' if ok['L1'] else 'FAIL'}: {sum(1 for x in rows if x['equal'])} of {len(ref['examples'])} "
              f"hashes equal; engine ready {r['boot']['ready_page_ms'] / 1000:.2f} s after page start")

        # L5a
        b.open(f"{base}/index.html?ex=E3")
        _, t = b.wait(DONE, timeout=600)
        check = b.eval("document.getElementById('ex-check').textContent")
        run = b.eval("(() => { const r = window.__v1.run(); return {done: r.done, frontier: r.frontier, pins: r.pins.length,"
                     " words: r.words.map((w) => w.length), ready_ms: window.__v1.boot.ready_page_ms}; })()")
        ok["L5a"] = check.startswith("pass") and run["done"] and run["pins"] > 0
        b.shot(os.path.join(out, "e3_1440.png"), full=True)
        res["checks"]["L5a"] = {"pass": ok["L5a"], "test_check": check, "run": run, "wait_s": round(t, 1)}
        print(f"L5a {'PASS' if ok['L5a'] else 'FAIL'}: E3 {check[:100]}; {run}")

        # L5b
        b.open(f"{base}/index.html?ex=E1")
        b.wait(DONE, timeout=600)
        # The editor draws only the lines in view and follows the thread's PC line after a run, so put the cursor on
        # the changed line and scroll it into view: the lint marker is then in the page.
        name = b.eval("(() => { const v = window.__v1.editor.view; const ln = v.state.doc.line(5);"
                      " v.dispatch({changes: {from: ln.from, insert: 'FOO r0\\n'}, selection: {anchor: ln.from},"
                      " scrollIntoView: true}); return window.__v1.editor.current(); })()")
        # The editor draws the lint marker after the message text is set (a later frame), so wait for both.
        has_marker = "!!document.querySelector('.cm-lint-marker-error, .cm-lintRange-error')"
        try:
            b.wait("document.getElementById('asm-msg').textContent.includes(':5: unknown instruction') && "
                   + has_marker, timeout=30)
        except TimeoutError:
            pass
        # After a run the page scrolls the editor to the PC line once, on the first render of the exact state, and
        # that render can come after the edit. Force it: in an edited file the view must stay on the edited line.
        try:
            b.wait("(() => { const u = window.__v1.ui; return !!u.exact && u.exact.cycle === u.cursor; })()", timeout=30)
        except TimeoutError:
            pass
        b.eval("(() => { const u = window.__v1.ui; u.followCycle = null; u.dirty = true; })()")
        time.sleep(1.0)
        view = b.eval(LINE5_VIEW)
        msg = b.eval("document.getElementById('asm-msg').textContent")
        marker = b.eval(has_marker)
        ok["L5b"] = msg.startswith(f"{name}:5: ") and marker and view["in_view"]
        b.shot(os.path.join(out, "asm_error_1440.png"))
        res["checks"]["L5b"] = {"pass": ok["L5b"], "file": name, "message": msg, "marker": marker,
                                "line5_in_view": view["in_view"], "first_line_in_view": view["first"]}
        print(f"L5b {'PASS' if ok['L5b'] else 'FAIL'}: {msg!r}, marker {marker}, line 5 in view {view['in_view']} "
              f"(first line in view {view['first']})")

        # L5c (320 px: the narrowest phone screens in use; 390 px: a common one)
        widths, checks, wide = {}, [], {}
        for w in (320, 390):
            b.viewport(w, 844, scale=2, mobile=True)
            b.open(f"{base}/index.html?ex=E1&w={w}")
            b.wait(DONE, timeout=600)
            checks.append(b.eval("document.getElementById('ex-check').textContent"))
            widths[w] = b.eval("document.documentElement.scrollWidth")
            wide[w] = b.eval(OVERFLOW)
            b.shot(os.path.join(out, f"e1_{w}.png"), full=True)
        ok["L5c"] = all(c.startswith("pass") for c in checks) and all(widths[w] <= w for w in widths)
        res["checks"]["L5c"] = {"pass": ok["L5c"], "test_check": checks, "scroll_width": widths, "past_the_edge": wide}
        print(f"L5c {'PASS' if ok['L5c'] else 'FAIL'}: E1 page width {widths} at screen widths 320 and 390 px, {checks[0][:60]}")
        for w in wide:
            for x in wide[w]:
                print(f"L5c   past the edge of the {w} px screen: {x}")

        # L5d
        rows = []
        for k, s in enumerate(man["samples"]):
            phone = s["file"] == "four_protocols_rtl.vcd"
            b.viewport(1440, 900)
            b.open(f"{base}/wave/demo.html?n={k}#s={s['file'][:-4]}")
            b.wait(f"document.body.dataset.ready === '{s['file']}'", timeout=120)
            dec = b.eval("window.__wave.sample.decoders.map((d) => { const r = window.__wave.view.decoded(d.name);"
                         " return [d.name, r.error || null, r.records.length]; })")
            good = all(e is None and n > 0 for _, e, n in dec)
            rows.append({"file": s["file"], "level": s["level"], "decoders": dec, "pass": good})
            print(f"L5d {'PASS' if good else 'FAIL'}: {s['file']}: {dec}")
            if phone or s["file"] in ("can_gl.vcd", "i2c_fm_rtl.vcd"):
                b.shot(os.path.join(out, f"wave_{s['file'][:-4]}_1440.png"), full=True)
            if phone:
                pw = {}
                for w in (320, 390):
                    b.viewport(w, 844, scale=2, mobile=True)
                    b.open(f"{base}/wave/demo.html?n=p{k}w{w}#s={s['file'][:-4]}")
                    b.wait(f"document.body.dataset.ready === '{s['file']}'", timeout=120)
                    pw[w] = b.eval("document.documentElement.scrollWidth")
                    b.shot(os.path.join(out, f"wave_{s['file'][:-4]}_{w}.png"), full=True)
                rows[-1]["phone_scroll_width"] = pw
                rows[-1]["pass"] = good and all(pw[w] <= w for w in pw)
        ok["L5d"] = bool(rows) and all(x["pass"] for x in rows)
        res["checks"]["L5d"] = {"pass": ok["L5d"], "samples": rows}
        errors = [m for m in b.console if m.startswith("[exception]")]
        ok["console"] = not errors
        res["checks"]["console"] = {"pass": ok["console"], "exceptions": errors, "messages": b.console[-200:]}
        print(f"console {'PASS' if ok['console'] else 'FAIL'}: {len(errors)} exceptions")
    httpd.shutdown()
    res["pass"] = all(ok.values())
    with open(os.path.join(out, "page_check.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    print("all browser checks " + ("PASS" if res["pass"] else f"FAIL: {[k for k, v in ok.items() if not v]}"))
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
