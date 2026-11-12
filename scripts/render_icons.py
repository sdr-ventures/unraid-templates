"""Rebuild every icon PNG from its SVG source.

Each icons/<name>.png (and icons/planned/<name>.png) is a 256x256 render of icons/src/<name>.svg.
The renderer is resvg-py 0.5.0, installed into a venv outside the repo (never the user profile):

    python -m venv C:/path/to/venv
    C:/path/to/venv/Scripts/python -m pip install resvg-py==0.5.0
    C:/path/to/venv/Scripts/python scripts/render_icons.py

A source with no PNG beside it is listed and skipped, never written: a PNG is only made for an
icon that a template or the planned folder already holds. tests/test_icon_sources.py checks the
sizes this script produces, without a renderer.
"""

import sys
from pathlib import Path

import resvg_py

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "icons" / "src"
SIZE = 256


def main() -> int:
    pngs = {p.stem: p for p in (ROOT / "icons").glob("*.png")}
    pngs.update({p.stem: p for p in (ROOT / "icons" / "planned").glob("*.png")})
    for svg in sorted(SOURCES.glob("*.svg")):
        png = pngs.get(svg.stem)
        if png is None:
            print(f"skip {svg.name}: no PNG beside it")
            continue
        png.write_bytes(bytes(resvg_py.svg_to_bytes(svg_path=str(svg), width=SIZE, height=SIZE)))
        print(f"rendered {png.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
