# SPDX-License-Identifier: Apache-2.0
"""Builds the simulator site folder: the page, the chip files of this commit and the pinned third-party files.

Use: python -I sim/tools/build_site.py OUT [SAMPLES]

What it does:
  1. Copies the page files of sim/ (everything but sim/tools/) to OUT and lists the SHA-256 of each one.
  2. Copies the files that the page runs, unchanged, from this commit: LICENSE, model/pe_model.py, asm/pe_asm.py, fw/*.s
     and the model test modules that hold the device models (test_fw_full.py, test_flex.py, test_holdout.py,
     test_isa.py), to OUT/py/pe/, and writes OUT/py/SOURCES.json: repository, branch, commit, and the SHA-256, size and
     git blob id of each file. A file that differs from the commit (a local change, a line-ending conversion) stops the
     build. The page's worker checks every file against this list before it runs it.
  3. Downloads the third-party files named in sim/tools/vendor.json from the npm registry, checks the SHA-256 of each,
     and writes them with their licence texts and a notice (OUT/vendor/README.md) to OUT/vendor/.
  4. Copies the waveform samples (SAMPLES: *.vcd and manifest.json from sim/tools/wave_samples.py) to OUT/wave/samples/.
  5. Size budget (check L7): writes OUT/check/size.json and OUT/check/files.json; stops if a budget is exceeded.

Why:
  The page must run the files of the same commit as the rest of the site, and each third-party file must be the
  released file, so that a reader can check both.
"""

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "sim")
CHIP_FILES = ["LICENSE", "model/pe_model.py", "asm/pe_asm.py", "test/test_fw_full.py", "test/test_flex.py",
              "test/test_holdout.py", "test/test_isa.py"]
# Budgets (L7). Page code: the HTML, CSS, JavaScript and Python of the page with the editor library, without Pyodide and
# the samples, gzip-compressed as a server sends it. Samples: one file each, read whole by the waveform page. Site
# folder: everything under OUT.
PAGE_GZIP_MAX = 1_000_000
SAMPLE_MAX = 1_000_000
SAMPLES_MAX = 2_000_000
SITE_MAX = 20_000_000


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], check=True, capture_output=True, text=True).stdout.strip()


def copy_page(out):
    files = {}
    for d, dirs, names in os.walk(SIM):
        rel_d = os.path.relpath(d, SIM)
        dirs[:] = sorted(x for x in dirs if x != "__pycache__" and not (rel_d == "." and x == "tools"))
        for n in sorted(names):
            rel = os.path.normpath(os.path.join(rel_d, n)).replace(os.sep, "/")
            data = open(os.path.join(d, n), "rb").read()
            dst = os.path.join(out, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(data)
            files[rel] = {"sha256": sha256(data), "bytes": len(data)}
    return files


def copy_chip(out):
    fw = sorted("fw/" + n for n in os.listdir(os.path.join(ROOT, "fw")) if n.endswith(".s"))
    entries = {}
    for rel in CHIP_FILES + fw:
        data = open(os.path.join(ROOT, rel), "rb").read()
        blob = git("rev-parse", f"HEAD:{rel}")
        if hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest() != blob:
            raise SystemExit(f"{rel}: the file differs from the commit (local change or line endings)")
        dst = os.path.join(out, "py", "pe", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(data)
        entries[rel] = {"sha256": sha256(data), "bytes": len(data), "git_blob": blob}
    server, repo = os.environ.get("GITHUB_SERVER_URL"), os.environ.get("GITHUB_REPOSITORY")
    branch = os.environ.get("SIM_BRANCH") or git("rev-parse", "--abbrev-ref", "HEAD")
    manifest = {
        "repository": f"{server}/{repo}" if server and repo else git("remote", "get-url", "origin"),
        "branch": branch,
        "commit": git("rev-parse", "HEAD"),
        "commit_date": git("log", "-1", "--format=%cI"),
        "files": entries,
    }
    with open(os.path.join(out, "py", "SOURCES.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=1)
        f.write("\n")
    return manifest


def fetch(url, cache):
    path = os.path.join(cache, url.rsplit("/", 1)[1])
    if not os.path.exists(path):
        os.makedirs(cache, exist_ok=True)
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
        with open(path, "wb") as f:
            f.write(data)
    return open(path, "rb").read()


def copy_vendor(out, cache):
    spec = json.load(open(os.path.join(SIM, "tools", "vendor.json"), encoding="utf-8"))
    files = {}
    notice = ["# Third-party files of the simulator page", "",
              "Each file below is the unchanged file of the npm release named, downloaded at build time and checked "
              "against the SHA-256 in sim/tools/vendor.json of the chip repository.", "",
              "| Package | Version | Licence | Files |", "|---|---|---|---|"]
    for p in spec["packages"]:
        name, ver = p["name"], p["version"]
        url = f"https://registry.npmjs.org/{name}/-/{name.split('/')[-1]}-{ver}.tgz"
        tar = tarfile.open(fileobj=io.BytesIO(fetch(url, cache)), mode="r:gz")
        dest = os.path.join(out, "vendor", p["dest"])
        os.makedirs(dest, exist_ok=True)
        names = []
        for member, (target, want) in p["files"].items():
            data = tar.extractfile(f"package/{member}").read()
            got = sha256(data)
            if got != want:
                raise SystemExit(f"{name}@{ver} {member}: SHA-256 {got}, pinned {want}")
            with open(os.path.join(dest, target), "wb") as f:
                f.write(data)
            files[f"vendor/{p['dest']}/{target}"] = {"sha256": got, "bytes": len(data), "from": f"{name}@{ver}/{member}"}
            names.append(member)
        if "license_url" in p:  # the npm package has no licence file: take it from the release tag
            with urllib.request.urlopen(p["license_url"], timeout=60) as r:
                text = r.read()
            if not text.startswith(b"Mozilla Public License Version 2.0"):
                raise SystemExit(f"{p['license_url']}: not the MPL-2.0 text")
            with open(os.path.join(dest, "LICENSE"), "wb") as f:
                f.write(text)
            names.append(f"LICENSE ({p['license_url']})")
        notice.append(f"| [{name}](https://www.npmjs.com/package/{name}/v/{ver}) | {ver} | {p['license']} | "
                      f"{', '.join(names)} |")
    notice += ["", "Pyodide's python_stdlib.zip holds the standard library of CPython (Python Software Foundation "
               "License). Source code of Pyodide: https://github.com/pyodide/pyodide (tag of the version above).", ""]
    with open(os.path.join(out, "vendor", "README.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(notice))
    return files


def copy_samples(out, samples):
    dest = os.path.join(out, "wave", "samples")
    os.makedirs(dest, exist_ok=True)
    n = 0
    for name in sorted(os.listdir(samples)):
        if name.endswith(".vcd") or name == "manifest.json":
            shutil.copyfile(os.path.join(samples, name), os.path.join(dest, name))
            n += 1
    return n


def sizes(out):
    page_raw = page_gz = samples = site = 0
    biggest_sample = ("", 0)
    for d, _, names in os.walk(out):
        for n in names:
            p = os.path.join(d, n)
            rel = os.path.relpath(p, out).replace(os.sep, "/")
            data = open(p, "rb").read()
            site += len(data)
            if rel.startswith("wave/samples/") and n.endswith(".vcd"):
                samples += len(data)
                if len(data) > biggest_sample[1]:
                    biggest_sample = (rel, len(data))
            elif not rel.startswith("vendor/pyodide-") and not rel.startswith("check/") and not rel.startswith("wave/samples/"):
                page_raw += len(data)
                page_gz += len(gzip.compress(data, 9))
    return {"page_bytes": page_raw, "page_gzip_bytes": page_gz, "page_gzip_max": PAGE_GZIP_MAX,
            "samples_bytes": samples, "samples_max": SAMPLES_MAX, "largest_sample": biggest_sample[0],
            "largest_sample_bytes": biggest_sample[1], "sample_max": SAMPLE_MAX, "site_bytes": site, "site_max": SITE_MAX}


def main(argv):
    if len(argv) not in (2, 3):
        print(__doc__)
        return 2
    out = os.path.abspath(argv[1])
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "check"))
    page = copy_page(out)
    src = copy_chip(out)
    vendor = copy_vendor(out, os.environ.get("SIM_VENDOR_CACHE", os.path.join(ROOT, "build", "vendor-cache")))
    nsamples = copy_samples(out, argv[2]) if len(argv) == 3 else 0
    print(f"page files ({len(page)}):")
    for rel, e in page.items():
        print(f"  {e['sha256']}  {e['bytes']:>7}  {rel}")
    print(f"chip files ({len(src['files'])}) of {src['repository']} {src['branch']} {src['commit']}")
    print(f"third-party files: {len(vendor)}, all equal to the pinned SHA-256; samples: {nsamples} files")
    with open(os.path.join(out, "check", "files.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"page": page, "vendor": vendor}, f, indent=1)
        f.write("\n")
    s = sizes(out)
    with open(os.path.join(out, "check", "size.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(s, f, indent=1)
        f.write("\n")
    print("size (L7): " + ", ".join(f"{k} {v:,}" if isinstance(v, int) else f"{k} {v}" for k, v in s.items()))
    over = [k for k, lim in (("page_gzip_bytes", PAGE_GZIP_MAX), ("samples_bytes", SAMPLES_MAX),
                             ("largest_sample_bytes", SAMPLE_MAX), ("site_bytes", SITE_MAX)) if s[k] > lim]
    if over:
        print(f"size budget exceeded: {over}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
