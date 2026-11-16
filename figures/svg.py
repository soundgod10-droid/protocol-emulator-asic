"""Shared drawing style of the figures: SVG written as text, so no drawing package is needed.

All figures of the documentation use these fonts and colours, so that every page looks the same:
blue for the design (blocks, waveforms), green for a measured value, red for a limit (a standard or a test rule).
"""

from xml.sax.saxutils import escape

FONT = "Helvetica, Arial, sans-serif"
MONO = "Menlo, Consolas, 'DejaVu Sans Mono', monospace"
INK = "#1d2733"  # text and outlines
LINE = "#1f4e9c"  # waveforms and block outlines
FILL = "#eef3fb"  # block fill
FILL2 = "#dce7f7"  # inner block fill
MEAS = "#17804a"  # measured values
LIMIT = "#b03a2e"  # limits
GRID = "#d5dbe3"
MUTED = "#5f6b7a"
COLOURS = {"ink": INK, "line": LINE, "meas": MEAS, "limit": LIMIT, "muted": MUTED}


def fmt(v):
    return f"{v:.1f}".rstrip("0").rstrip(".")


class Svg:
    def __init__(self, width, height):
        self.w, self.h = width, height
        self.items = []

    def add(self, s):
        self.items.append(s)

    def rect(self, x, y, w, h, fill=FILL, stroke=LINE, sw=1.5, rx=6, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<rect x="{fmt(x)}" y="{fmt(y)}" width="{fmt(w)}" height="{fmt(h)}" rx="{rx}" fill="{fill}" '
                 f'stroke="{stroke}" stroke-width="{sw}"{d}/>')

    def line(self, x1, y1, x2, y2, colour="line", sw=1.5, dash=None, start=False, end=False):
        c = COLOURS.get(colour, colour)
        d = f' stroke-dasharray="{dash}"' if dash else ""
        m = (f' marker-start="url(#ah-{colour})"' if start else "") + (f' marker-end="url(#ah-{colour})"' if end else "")
        self.add(f'<line x1="{fmt(x1)}" y1="{fmt(y1)}" x2="{fmt(x2)}" y2="{fmt(y2)}" stroke="{c}" '
                 f'stroke-width="{sw}"{d}{m}/>')

    def path(self, points, colour="line", sw=1.5, dash=None, end=False):
        """Open polyline through the points; end=True puts an arrow head on the last point."""
        c = COLOURS.get(colour, colour)
        d = f' stroke-dasharray="{dash}"' if dash else ""
        m = f' marker-end="url(#ah-{colour})"' if end else ""
        pts = " ".join(f"{fmt(x)},{fmt(y)}" for x, y in points)
        self.add(f'<polyline points="{pts}" fill="none" stroke="{c}" stroke-width="{sw}" '
                 f'stroke-linejoin="round"{d}{m}/>')

    def polygon(self, points, fill, stroke="none", sw=1):
        pts = " ".join(f"{fmt(x)},{fmt(y)}" for x, y in points)
        self.add(f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')

    def text(self, x, y, s, size=13, anchor="start", weight="normal", colour="ink", mono=False, italic=False):
        c = COLOURS.get(colour, colour)
        style = ' font-style="italic"' if italic else ""
        self.add(f'<text x="{fmt(x)}" y="{fmt(y)}" font-family="{MONO if mono else FONT}" font-size="{size}" '
                 f'text-anchor="{anchor}" font-weight="{weight}" fill="{c}"{style}>{escape(str(s))}</text>')

    def lines(self, x, y, rows, size=12, gap=None, **kw):
        """Several text lines, top line at y."""
        gap = gap or size * 1.35
        for i, row in enumerate(rows):
            self.text(x, y + i * gap, row, size=size, **kw)

    def save(self, path, title, desc):
        markers = "".join(
            f'<marker id="ah-{name}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>'
            for name, c in COLOURS.items())
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" role="img" aria-labelledby="title desc">\n'
                f'<title id="title">{escape(title)}</title>\n<desc id="desc">{escape(desc)}</desc>\n'
                f'<defs>{markers}</defs>\n<rect width="100%" height="100%" fill="#ffffff"/>\n')
        with open(path, "w", encoding="utf-8") as f:
            f.write(head + "\n".join(self.items) + "\n</svg>\n")
