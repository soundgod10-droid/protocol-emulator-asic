"""Source folder of the documentation site: the pages of docs/, the README as the overview page, the census
pre-registration, the generated results page, the figures and the layout viewer files.

What it does:
  Copies README.md (as index.md), every file of docs/ and flex/PREREG.md (as census.md) into the site source
  folder, and rewrites their links for the site: a link to another page becomes the site page, a link to any
  other file of the repository becomes its GitHub address at this commit, and a figure address on the Pages
  site becomes the local figure, so that mkdocs --strict checks that it exists. With a tt_submission folder it
  adds the layout file for the 3D viewer under viewer/ (the page that the template's viewer job made).

Usage: python figures/site.py <site source folder> [<tt_submission folder>]
  figures/ and results.md must already be in the site source folder (.github/workflows/docs.yaml).
"""

import glob
import json
import os
import posixpath
import re
import shutil
import sys
from urllib.parse import urljoin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.environ.get("GITHUB_REPOSITORY", "soundgod10-droid/protocol-emulator-asic")
REF = os.environ.get("GITHUB_SHA") or "cmos5l"
OWNER, NAME = REPO.split("/")
PAGES = f"https://{OWNER.lower()}.github.io/{NAME}/"

LINK = re.compile(r"(!?\[[^\]\n]*\])\(([^)\s]+)((?:\s+\"[^\"]*\")?)\)")
REFDEF = re.compile(r"^(\s{0,3}\[[^\]\n]+\]:\s*)(\S+)", re.M)
HTML = re.compile(r"\b(src|href)=\"([^\"]+)\"")


def page_map():
    pages = {"README.md": "index.md", "flex/PREREG.md": "census.md"}
    for path in sorted(glob.glob(os.path.join(ROOT, "docs", "*.md"))):
        pages[f"docs/{os.path.basename(path)}"] = os.path.basename(path)
    return pages


def rewrite(target, src, pages):
    if target.startswith(PAGES + "sim/") or target.startswith(PAGES + "viewer/"):
        return target  # put next to the site after mkdocs; a relative link would fail mkdocs --strict
    if target.startswith(PAGES):
        rest, _, frag = target[len(PAGES):].partition("#")
        frag = "#" + frag if frag else ""
        if rest.endswith("/") and rest[:-1] + ".md" in set(pages.values()) | {"results.md"}:
            return rest[:-1] + ".md" + frag  # a page address on the site, for example .../results/
        return (rest or "index.md") + frag
    if target.startswith("#") or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
        return target
    path, _, frag = target.partition("#")
    frag = "#" + frag if frag else ""
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(src), path))
    if resolved.startswith(".."):
        return urljoin(f"https://github.com/{REPO}/blob/{REF}/{src}", target)
    if resolved in pages:
        return pages[resolved] + frag
    kind = "tree" if os.path.isdir(os.path.join(ROOT, resolved)) else "blob"
    return f"https://github.com/{REPO}/{kind}/{REF}/{resolved}{frag}"


def convert(text, src, pages):
    text = LINK.sub(lambda m: f"{m.group(1)}({rewrite(m.group(2), src, pages)}{m.group(3)})", text)
    text = REFDEF.sub(lambda m: m.group(1) + rewrite(m.group(2), src, pages), text)
    return HTML.sub(lambda m: f'{m.group(1)}="{rewrite(m.group(2), src, pages)}"', text)


def viewer(out, sub):
    oas = sorted(glob.glob(os.path.join(sub, "**", "*.oas"), recursive=True))
    pdk_json = sorted(glob.glob(os.path.join(sub, "**", "pdk.json"), recursive=True))
    if not oas or not pdk_json:
        print(f"no layout files in {sub}: no viewer page")
        return
    pdk = json.load(open(pdk_json[0], encoding="utf-8"))["PDK"]
    os.makedirs(os.path.join(out, "viewer"), exist_ok=True)
    shutil.copy(oas[0], os.path.join(out, "viewer", "tinytapeout.oas"))
    url = f"https://gds-viewer.tinytapeout.com/?pdk={pdk}&model=" + PAGES + "viewer/tinytapeout.oas"
    with open(os.path.join(out, "viewer", "index.html"), "w", encoding="utf-8") as f:
        f.write(f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>3D layout viewer</title>'
                f'<meta http-equiv="refresh" content="0; url={url}"></head>'
                f'<body><p><a href="{url}">Open the 3D layout viewer</a></p></body></html>\n')


def main(out, sub=None):
    os.makedirs(out, exist_ok=True)
    pages = page_map()
    for src, dst in pages.items():
        with open(os.path.join(ROOT, src), encoding="utf-8") as f:
            text = f.read()
        with open(os.path.join(out, dst), "w", encoding="utf-8") as f:
            f.write(convert(text, src, pages))
    for path in glob.glob(os.path.join(ROOT, "docs", "*")):
        if not path.endswith(".md") and os.path.isfile(path):
            shutil.copy(path, out)
    if sub:
        viewer(out, sub)
    print(f"site source: {sorted(os.listdir(out))}")


if __name__ == "__main__":
    main(*sys.argv[1:])
