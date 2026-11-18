"""Every icon has its SVG source at the house size, and its PNG is the render of that source.

scripts/render_icons.py rebuilds each PNG from icons/src/<name>.svg. CI has no renderer, so this
file checks what can be checked without one: each source is a 256-unit square, each PNG it renders
into is 256 pixels square, every icon a template names has a source, and every source belongs to
an icon a template or the planned folder holds.
"""

import struct
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ICONS = REPO / "icons"
SOURCES = ICONS / "src"
PLANNED = ICONS / "planned"


def png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", data[16:24])


def rendered_pngs():
    # The PNG each source renders into: icons/<name>.png or icons/planned/<name>.png.
    pngs = {p.stem: p for p in ICONS.glob("*.png")}
    pngs.update({p.stem: p for p in PLANNED.glob("*.png")})
    return pngs


def template_icons():
    named = set()
    for xml in (REPO / "templates").glob("*.xml"):
        icon = (ET.parse(xml).getroot().findtext("Icon") or "").strip()
        if icon:
            named.add(icon.rsplit("/", 1)[-1].removesuffix(".png"))
    return named


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
    missing = sorted(n for n in template_icons() if not (SOURCES / f"{n}.svg").is_file())
    assert not missing, f"icons with no SVG source in icons/src: {missing}"


def test_every_svg_source_belongs_to_a_template_or_the_planned_folder():
    held = template_icons() | {p.stem for p in PLANNED.glob("*.png")}
    orphans = sorted(p.stem for p in SOURCES.glob("*.svg") if p.stem not in held)
    assert not orphans, f"sources for icons nothing names: {orphans}"
