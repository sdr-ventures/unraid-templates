"""Every icon has its SVG source at the house size, and its PNG is the render of that source.

scripts/render_icons.py rebuilds each PNG from icons/src/<name>.svg. CI has no renderer, so this
file checks what can be checked without one: each source is a 256-unit square, each PNG it renders
into is 256 pixels square, and every icon a template names has a source.
"""

import struct
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ICONS = REPO / "icons"
SOURCES = ICONS / "src"
PLANNED = ICONS / "planned"

# Icons a template names that still have no SVG source (cef-tracker, iron-tide). A new icon gets
# its source in icons/src in the same change; this list only shrinks as sources land.
SOURCELESS_ICONS = frozenset({"cef-tracker", "iron-tide"})


def png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", data[16:24])


def rendered_pngs():
    # The PNG each source renders into: icons/<name>.png or icons/planned/<name>.png.
    pngs = {p.stem: p for p in ICONS.glob("*.png")}
    pngs.update({p.stem: p for p in PLANNED.glob("*.png")})
    return pngs


def test_each_source_is_256_units_and_its_png_is_256_pixels():
    pngs = rendered_pngs()
    checked = []
    for svg in sorted(SOURCES.glob("*.svg")):
        if svg.stem not in pngs:
            continue  # a source with no PNG beside it is not rendered
        viewbox = ET.parse(svg).getroot().get("viewBox")
        assert viewbox == "0 0 256 256", f"{svg.name}: viewBox is {viewbox!r}"
        assert png_size(pngs[svg.stem]) == (256, 256), f"{pngs[svg.stem].name} is not 256x256"
        checked.append(svg.stem)
    assert checked, "no SVG source matched a PNG; the glob is wrong"


def test_every_icon_a_template_names_has_an_svg_source():
    named = set()
    for xml in (REPO / "templates").glob("*.xml"):
        icon = (ET.parse(xml).getroot().findtext("Icon") or "").strip()
        if icon:
            named.add(icon.rsplit("/", 1)[-1].removesuffix(".png"))
    missing = sorted(n for n in named if n not in SOURCELESS_ICONS and not (SOURCES / f"{n}.svg").is_file())
    assert not missing, f"icons with no SVG source in icons/src: {missing}"


def test_the_sourceless_list_names_only_icons_that_still_lack_a_source():
    stale = sorted(n for n in SOURCELESS_ICONS if (SOURCES / f"{n}.svg").is_file())
    assert not stale, f"these now have a source; drop them from SOURCELESS_ICONS: {stale}"
