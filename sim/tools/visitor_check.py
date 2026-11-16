# SPDX-License-Identifier: Apache-2.0
"""Visitor paths of the simulator page in phone views of WebKit and Chromium (check L5e).

Use: python -I sim/tools/visitor_check.py SITE_ROOT OUT_DIR [BROWSERS]

What it does:
  Serves SITE_ROOT on 127.0.0.1 (the simulator under /sim/, as in page_check.py) and opens sim/index.html with Playwright
  in phone views: WebKit with the "iPhone 13" profile and Chromium with the "Pixel 7" profile, each at 390 and 320 CSS
  pixels wide (BROWSERS: comma list, default "webkit,chromium"). In each view a visitor follows three paths:
    A  choose, wait, tap Run: choose E3 (four threads), wait until its run is computed, tap the main button;
    B  tap during the computation: choose E15 and tap the main button at once, while the engine still computes;
    C  change example: tap the main button on E1, choose E9b (two threads) while E1 plays; when E9b is computed, tap
       the main button again.
  The page is read every 0.25 s as a visitor sees it (cycle counter, main button, status line, panels). Checks:
    moves         after each tap the cycle counter shows at least 8 increasing values before the run ends (2 s or more
                  of visible movement) and starts in the first tenth of the run;
    no silent stop  a sample in which the counter stands still before the end says so in the status line ("paused",
                  "waiting");
    result in view  at the end the counter is at the last computed cycle and a line with the test check result (pass or
                  fail) is inside the screen;
    own example   after a change of example, in every sample, every panel belongs to the chosen example: the example
                  card title, the pin uses of the pin table, the waveform's pin count, the cycle of the thread panel (not
                  past the cycles computed for the chosen example) and the editor tabs (files of the chosen run only);
    threads       when the run is computed, the example card names every thread that the example starts, and says why
                  the other threads stay stopped;
    no exception in the page.
  Writes OUT_DIR/visitor_check.json (every sample) and screenshots of each step (the screen as the visitor sees it).

Why:
  The examples' model results were correct while a visitor saw nothing happen: a run that ended in under a second, a tap
  that stopped the page at cycle 0 without saying so, panels of the previous example after a change. These checks test
  what a visitor sees, on the two phone browser engines in use (WebKit on iPhone, Chromium on Android), at the narrowest
  phone width in use (320 px) and a common one (390 px).
"""

import functools
import http.server
import json
import os
import re
import sys
import threading
import time

from playwright.sync_api import sync_playwright

VIEWS = {"webkit": "iPhone 13", "chromium": "Pixel 7"}
WIDTHS = (390, 320)
EVERY_S = 0.25
MOVES_MIN = 8
REPLAY_MAX_S = 60

DONE = ("(() => { const e = document.getElementById('ex-check'); "
        "return !!e && (e.className === 'pass' || e.className === 'fail'); })()")

# What a visitor sees, plus the run object of the page for the computed cycles.
STATE = r"""(() => { const v = window.__v1, r = v && v.run ? v.run() : null;
  const st = document.getElementById('status');
  const inView = (e) => { const b = e.getBoundingClientRect();
    return b.height > 0 && b.top >= 0 && b.bottom <= window.innerHeight && b.left >= 0 && b.right <= window.innerWidth; };
  const result = [st, document.getElementById('ex-check')].filter((e) => e && inView(e)).map((e) => e.textContent)
    .find((t) => /\b(pass|fail)\b/.test(t)) || null;
  return { cycle: +document.getElementById('cycle').textContent.replace(/[^0-9]/g, '') || 0,
    button: document.getElementById('run').textContent.trim(), status: st ? st.textContent.trim() : null,
    ex: r ? r.ex.id : null, done: r ? !!r.done : false, frontier: r ? r.frontier : 0,
    playing: !!(v && v.ui && v.ui.playing), result_in_view: result }; })()"""

# Panels that show something of another example than the chosen one (empty list: all belong to it).
PANELS = r"""(() => { const r = window.__v1.run(), ex = r.ex, bad = [];
  const title = document.getElementById('ex-title').textContent;
  if (!title.startsWith(ex.id + ':')) bad.push('example card: ' + title);
  const labels = new Set(ex.pins.map((p) => p[1]).filter(Boolean));
  for (const td of document.querySelectorAll('#pins td.fn')) {
    const u = td.textContent.trim(); if (u && !labels.has(u)) bad.push('pin use ' + u); }
  const wh = document.getElementById('wave-hint').textContent, m = /^(\d+) pins?/.exec(wh);
  if (!m || +m[1] !== ex.pins.length) bad.push('waveform: ' + wh);
  const sh = document.getElementById('state-hint').textContent, c = /cycle ([\d,]+)/.exec(sh);
  if (c && +c[1].replace(/,/g, '') > r.frontier) bad.push('threads: "' + sh + '", computed ' + r.frontier);
  const files = r.programs.map((p) => (p.name === '<test>' ? 'program (from the test)' : p.name));
  const tabs = [...document.querySelectorAll('#tabs .tab')].map((b) => b.dataset.label || b.textContent.trim());
  if (tabs.some((t) => !files.includes(t))) bad.push('editor tabs: ' + tabs.join(', '));
  return bad; })()"""

# Threads that the example starts, and whether the example card names each one and the reason for the others.
THREADS = r"""(() => { const r = window.__v1.run(), el = document.getElementById('ex-threads');
  const used = (r.config ? r.config.run : []).map((x, n) => (x ? n : -1)).filter((n) => n >= 0);
  const text = el ? el.textContent : '';
  const missing = used.filter((n) => !new RegExp('(Thread |T)' + n + '\\b').test(text));
  const why = used.length === 4 || /not used|not start|stay stopped/i.test(text);
  return { used, text, missing, why, ok: !!el && used.length > 0 && !missing.length && why }; })()"""


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


class Visitor:
    def __init__(self, page, base, out, tag):
        self.page, self.base, self.out, self.tag = page, base, out, tag
        self.t0 = time.perf_counter()

    def sample(self, panels=False):
        s = self.page.evaluate(STATE)
        s["t"] = round(time.perf_counter() - self.t0, 2)
        if panels and s["ex"]:
            s["panels"] = self.page.evaluate(PANELS)
        return s

    def watch(self, seconds, stop=None, panels=False, every=EVERY_S, shot_at=None, shot_name=None, stop_all=None):
        out, end = [], time.perf_counter() + seconds
        while True:
            s = self.sample(panels)
            out.append(s)
            if shot_at is not None and shot_name and len(out) == shot_at:
                self.shot(shot_name)
            if (stop and stop(s)) or (stop_all and stop_all(out)) or time.perf_counter() >= end:
                return out
            self.page.wait_for_timeout(int(every * 1000))

    def shot(self, name):
        self.page.screenshot(path=os.path.join(self.out, f"{self.tag}_{name}.png"))

    def open(self, ex=None):
        self.page.goto(f"{self.base}/index.html" + (f"?ex={ex}" if ex else ""))
        self.page.wait_for_function(DONE, timeout=600_000, polling=250)
        self.page.wait_for_timeout(500)


def finished(s):
    return s["done"] and s["cycle"] >= s["frontier"] and not s["playing"]


def ended_or_stalled(samples):
    """Stop watching a replay: it finished, or the run is computed and the counter stood still for 3 s."""
    last = samples[-1]
    still = samples[-12:]
    return finished(last) or (last["done"] and len(still) == 12 and len({s["cycle"] for s in still}) == 1)


def moves(samples):
    n, top = 0, None
    for s in samples:
        if top is not None and s["cycle"] > top:
            n += 1
        top = s["cycle"] if top is None else max(top, s["cycle"])
    return n


def silent_stops(samples):
    return [b["t"] for a, b in zip(samples, samples[1:])
            if b["cycle"] == a["cycle"] and not finished(b) and not re.search(r"paus|wait", b["status"] or "", re.I)]


def replay_checks(samples):
    """Checks of the samples after a tap on the main button, up to the end of the run."""
    last = samples[-1]
    first = samples[0]
    n = moves(samples)
    stops = silent_stops(samples)
    total = max(last["frontier"], 1)
    r = {
        "moves": n, "moves_pass": n >= MOVES_MIN and first["cycle"] <= total // 10,
        "silent_stops_at_s": stops[:10], "no_silent_stop_pass": not stops,
        "end": {k: last[k] for k in ("cycle", "frontier", "done", "button", "status", "result_in_view")},
        "result_in_view_pass": finished(last) and last["result_in_view"] is not None,
    }
    r["pass"] = r["moves_pass"] and r["no_silent_stop_pass"] and r["result_in_view_pass"]
    return r


def path_a(v):
    """Choose E3, wait until it is computed, tap the main button."""
    v.open()
    v.page.select_option("#example", "E3")
    early = v.watch(1.0, panels=True, every=0.1)
    v.shot("A1_after_choose")
    v.page.wait_for_function(DONE, timeout=600_000, polling=250)
    v.page.wait_for_timeout(1000)
    v.shot("A2_computed")
    computed = v.sample()
    threads = v.page.evaluate(THREADS)
    v.page.tap("#run")
    after = v.watch(REPLAY_MAX_S, stop_all=ended_or_stalled, shot_at=9, shot_name="A3_two_s_after_tap")
    v.shot("A4_end")
    r = replay_checks(after)
    bad = [(s["t"], s["panels"]) for s in early if s.get("panels")]
    r.update(own_example_pass=not bad, own_example_bad=bad[:5], threads=threads, computed=computed)
    r["pass"] = r["pass"] and not bad and threads["ok"]
    return r, early + [computed] + after


def path_b(v):
    """Choose E15 and tap the main button while the engine computes."""
    v.open()
    v.page.select_option("#example", "E15")
    v.page.tap("#run")
    at_tap = v.sample()
    after = v.watch(REPLAY_MAX_S + 60, stop_all=ended_or_stalled, shot_at=5, shot_name="B1_one_s_after_tap")
    v.shot("B2_end")
    r = replay_checks(after)
    threads = v.page.evaluate(THREADS)
    r.update(tapped_while_computing=not at_tap["done"], at_tap=at_tap, threads=threads)
    r["pass"] = r["pass"] and r["tapped_while_computing"] and threads["ok"]
    return r, [at_tap] + after


def path_c(v):
    """Tap the main button on E1, choose E9b while E1 plays, tap again when E9b is computed."""
    v.open()
    v.page.tap("#run")
    v.page.wait_for_timeout(300)
    playing = v.sample()
    v.page.select_option("#example", "E9b")
    change = v.watch(120, stop=lambda s: s["done"], panels=True, every=0.1, shot_at=2, shot_name="C1_after_change")
    v.page.wait_for_timeout(1000)
    v.shot("C2_computed")
    threads = v.page.evaluate(THREADS)
    v.page.tap("#run")
    after = v.watch(REPLAY_MAX_S, stop=finished, shot_at=9, shot_name="C3_two_s_after_tap")
    v.shot("C4_end")
    r = replay_checks(after)
    bad = [(s["t"], s["panels"]) for s in change if s.get("panels")]
    r.update(playing_before_change=playing, own_example_pass=not bad, own_example_bad=bad[:5], threads=threads)
    r["pass"] = r["pass"] and not bad and threads["ok"]
    return r, [playing] + change + after


PATHS = (("A", path_a), ("B", path_b), ("C", path_c))


def main(argv):
    if len(argv) not in (3, 4):
        print(__doc__)
        return 2
    root, out = os.path.abspath(argv[1]), os.path.abspath(argv[2])
    kinds = argv[3].split(",") if len(argv) == 4 else list(VIEWS)
    os.makedirs(out, exist_ok=True)
    httpd = serve(root)
    base = f"http://127.0.0.1:{httpd.server_address[1]}/sim"
    res = {"views": []}
    ok = True
    with sync_playwright() as pw:
        for kind in kinds:
            browser = getattr(pw, kind).launch()
            for width in WIDTHS:
                dev = {k: v for k, v in pw.devices[VIEWS[kind]].items() if k != "default_browser_type"}
                dev["viewport"] = {"width": width, "height": dev["viewport"]["height"]}
                if "screen" in dev:
                    dev["screen"] = {"width": width, "height": dev["screen"]["height"]}
                tag = f"{kind}_{width}"
                view = {"view": tag, "device": VIEWS[kind], "browser": f"{kind} {browser.version}", "paths": {}}
                for name, fn in PATHS:
                    ctx = browser.new_context(**dev)
                    page = ctx.new_page()
                    errors = []
                    page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                    try:
                        r, samples = fn(Visitor(page, base, out, f"{tag}_{name}"))
                    except Exception as err:  # noqa: BLE001 - a path that cannot finish fails with its reason
                        r, samples = {"pass": False, "error": f"{type(err).__name__}: {err}"}, []
                    r["page_errors"] = errors
                    r["pass"] = r["pass"] and not errors
                    r["samples"] = samples
                    view["paths"][name] = r
                    ok = ok and r["pass"]
                    ctx.close()
                    print(f"L5e {tag:15} {name} {'PASS' if r['pass'] else 'FAIL'}: "
                          + (r["error"] if "error" in r else
                             f"moves {r['moves']}, silent stops {len(r['silent_stops_at_s'])}, "
                             f"result in view {r['result_in_view_pass']}, own example {r.get('own_example_pass', '-')}, "
                             f"threads {r['threads']['ok']} {r['threads']['used']}; end: {r['end']['status']!r} "
                             f"button {r['end']['button']!r}") + (f"; page errors {errors[:2]}" if errors else ""))
                res["views"].append(view)
            browser.close()
    httpd.shutdown()
    res["pass"] = ok
    with open(os.path.join(out, "visitor_check.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    print("visitor paths (L5e) " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
