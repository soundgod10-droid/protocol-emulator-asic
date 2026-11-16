"""Render of the final layout (PNG) from the GDS of a gds run, with KLayout in batch mode (no display).

What it does:
  Loads the GDS of the tt_submission artifact of a gds run into a KLayout layout view (the KLayout Python package,
  no window), shows every hierarchy level with the layer colours of the PDK, draws the tile outline from the die
  box of the run (final metrics.csv, design__die__bbox) and saves two images: layout.png (the whole tile) and
  layout_detail.png (a 160 x 70 um window at the centre of the tile, where single cells and wires can be seen).
  layout.json gives the tile size and the files used.

Usage: python figures/layout.py <tt_submission folder> <layer properties .lyp> <out folder>
  The .lyp is the KLayout layer table of the PDK revision that the run used (tt_submission/pdk.json PDK_VERSION);
  .github/workflows/docs.yaml downloads it. Without it KLayout uses its default colours.
"""

import csv
import glob
import json
import os
import sys

import klayout.db as db
import klayout.lay as lay

OUTLINE = db.LayerInfo(1000, 0)  # a layer that the PDK does not use, for the outline only (not saved)


def die_box(sub, top):
    for path in glob.glob(os.path.join(sub, "**", "metrics.csv"), recursive=True):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if row and row[0] == "design__die__bbox":
                    return [float(x) for x in row[1].split()], os.path.relpath(path, sub)
    b = top.dbbox()
    return [b.left, b.bottom, b.right, b.top], "GDS bounding box"


def main(sub, lyp, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    gds = sorted(glob.glob(os.path.join(sub, "**", "*.gds"), recursive=True))[0]
    view = lay.LayoutView()
    view.set_config("background-color", "#0d1117")
    view.set_config("grid-visible", "false")
    idx = view.load_layout(gds, True)
    if lyp and os.path.exists(lyp):
        view.load_layer_props(lyp)
    view.max_hier()
    cv = view.cellview(idx)
    ly, top = cv.layout(), cv.cell
    (x0, y0, x1, y1), die_src = die_box(sub, top)
    top.shapes(ly.layer(OUTLINE)).insert(db.DBox(x0, y0, x1, y1))
    lp = lay.LayerPropertiesNode()
    lp.source = f"{OUTLINE.layer}/{OUTLINE.datatype}@1"
    lp.name = "tile outline"
    lp.frame_color = 0xFFFFFF
    lp.fill_color = 0xFFFFFF
    lp.dither_pattern = 1  # hollow
    lp.width = 3
    view.insert_layer(view.end_layers(), lp)

    w, h = x1 - x0, y1 - y0
    m = 0.015 * w
    full = db.DBox(x0 - m, y0 - m, x1 + m, y1 + m)
    px = 2000
    py = round(px * full.height() / full.width())
    view.zoom_box(full)
    view.save_image_with_options(os.path.join(out_dir, "layout.png"), px, py, 0, 2, 0, full, False)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    detail = db.DBox(cx - 80, cy - 35, cx + 80, cy + 35)
    view.zoom_box(detail)
    view.save_image_with_options(os.path.join(out_dir, "layout_detail.png"), 1600, 700, 0, 2, 0, detail, False)
    facts = dict(gds=os.path.relpath(gds, sub), top=top.name, die_um=[x0, y0, x1, y1], die_source=die_src,
                 width_um=w, height_um=h, detail_um=[detail.left, detail.bottom, detail.right, detail.top],
                 lyp=os.path.basename(lyp) if lyp and os.path.exists(lyp) else None)
    with open(os.path.join(out_dir, "layout.json"), "w", encoding="utf-8") as f:
        json.dump(facts, f, indent=1)
    print(f"layout render: {facts}")


if __name__ == "__main__":
    main(*sys.argv[1:])
