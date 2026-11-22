"""Tests for scripts/build.py; fixtures only, so no installed apps, themes or network."""

import contextlib
import io
import json
import math
import os
import re
import shutil
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "scripts"))

import build

SVG = "{http://www.w3.org/2000/svg}"
XLINK = "{http://www.w3.org/1999/xlink}"

try:
    import cairo
    import gi

    gi.require_version("Rsvg", "2.0")
    from gi.repository import Rsvg

    HAVE_RSVG = True
except (ImportError, ValueError):
    HAVE_RSVG = False


def slot(padding=0.16, offset=(0.0, 0.0)):
    return build.Slot(padding, offset)


def svg_root(markup):
    return build.parse_svg(markup.encode("utf-8"))


def wrap(content):
    """Parse inlined content the way it sits in a published icon."""
    return ET.fromstring(
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">' + content + "</svg>"
    )


def decode_png(data):
    """RGBA rows of an 8-bit RGBA PNG whose rows all use filter 0."""
    width, height = struct.unpack(">II", data[16:24])
    idat = b""
    pos = 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        if data[pos + 4 : pos + 8] == b"IDAT":
            idat += data[pos + 8 : pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 4 + 1
    return [raw[y * stride + 1 : (y + 1) * stride] for y in range(height)]


class GeometryTests(unittest.TestCase):
    def test_lengths_convert_to_px(self):
        self.assertEqual(build.parse_length("48"), 48)
        self.assertEqual(build.parse_length(" 48px "), 48)
        self.assertAlmostEqual(build.parse_length("36pt"), 48)
        self.assertAlmostEqual(build.parse_length("25.4mm"), 96)
        self.assertAlmostEqual(build.parse_length("2.54cm"), 96)
        self.assertAlmostEqual(build.parse_length("1in"), 96)
        self.assertAlmostEqual(build.parse_length("3pc"), 48)
        self.assertAlmostEqual(build.parse_length("1.5e1"), 15)

    def test_relative_and_bad_lengths_are_unknown(self):
        for value in (None, "", "100%", "2em", "abc", "12 px px"):
            self.assertIsNone(build.parse_length(value), value)

    def test_viewbox_maps_onto_the_slot(self):
        transform, viewport = build.place_viewport(
            svg_root('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"/>'), slot(0.2)
        )
        self.assertEqual(transform, "translate(12.8 12.8) scale(1.6)")
        self.assertEqual([round(v, 4) for v in viewport], [12.8, 12.8, 38.4, 38.4])

    def test_viewbox_origin_and_offset(self):
        root = svg_root('<svg xmlns="http://www.w3.org/2000/svg" viewBox="-8,-8 16 16"/>')
        transform, _ = build.place_viewport(root, slot(0.25, (0.0, -2.0)))
        self.assertEqual(transform, "translate(32 30) scale(2)")

    def test_missing_viewbox_falls_back_to_width_and_height_with_units(self):
        root = svg_root('<svg xmlns="http://www.w3.org/2000/svg" width="32pt" height="16pt"/>')
        transform, viewport = build.place_viewport(root, slot(0.16))
        self.assertEqual(transform, "translate(10.24 21.12) scale(1.02)")
        self.assertEqual([round(v, 4) for v in viewport], [10.24, 21.12, 43.52, 21.76])

    def test_width_only_takes_the_viewbox_ratio(self):
        root = svg_root('<svg xmlns="http://www.w3.org/2000/svg" width="10mm" viewBox="0 0 20 10"/>')
        _, viewport = build.place_viewport(root, slot(0.25))
        self.assertEqual([round(v, 4) for v in viewport], [16, 24, 32, 16])

    def test_preserve_aspect_ratio_none_stretches(self):
        root = svg_root(
            '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10" viewBox="0 0 10 10" preserveAspectRatio="none"/>'
        )
        transform, _ = build.place_viewport(root, slot(0.25))
        self.assertEqual(transform, "translate(16 24) scale(3.2 1.6)")

    def test_preserve_aspect_ratio_meet_and_slice(self):
        base = '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10" viewBox="0 0 10 10" preserveAspectRatio="%s"/>'
        cases = {
            "": "translate(24 24) scale(1.6)",
            "xMinYMid meet": "translate(16 24) scale(1.6)",
            "xMaxYMax": "translate(32 24) scale(1.6)",
            "xMinYMin slice": "translate(16 24) scale(3.2)",
            "xMidYMax slice": "translate(16 8) scale(3.2)",
        }
        for aspect, expected in cases.items():
            transform, viewport = build.place_viewport(svg_root(base % aspect), slot(0.25))
            self.assertEqual(transform, expected, aspect)
            self.assertEqual([round(v, 4) for v in viewport], [16, 24, 32, 16])

    def test_documents_without_a_usable_size_are_skipped(self):
        cases = {
            '<svg xmlns="http://www.w3.org/2000/svg"/>': "no-size",
            '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%"/>': "no-size",
            '<svg xmlns="http://www.w3.org/2000/svg" width="24"/>': "no-size",
            '<svg xmlns="http://www.w3.org/2000/svg" width="0" height="24"/>': "no-size",
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 0 24"/>': "empty-viewbox",
        }
        for markup, reason in cases.items():
            with self.assertRaises(build.Skip) as caught:
                build.place_viewport(svg_root(markup), slot())
            self.assertEqual(caught.exception.reason, reason, markup)

    def test_a_malformed_viewbox_is_ignored(self):
        root = svg_root('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 -5 x"/>')
        transform, _ = build.place_viewport(root, slot(0.25))
        self.assertEqual(transform, "translate(16 16) scale(1.333333)")


class CssTests(unittest.TestCase):
    def scoped(self, css):
        return build.scope_css(css, "S", "c-")

    def test_selectors_are_scoped_and_names_prefixed(self):
        self.assertEqual(self.scoped(".a{fill:red}"), "#S .c-a{fill:red}")
        self.assertEqual(self.scoped("circle { fill: #ffcc00 }"), "#S circle{fill: #ffcc00}")
        self.assertEqual(self.scoped("#mark, g > .dot.big{x:1}"), "#S #c-mark,#S g > .c-dot.c-big{x:1}")
        self.assertEqual(self.scoped("*{y:2}"), "#S *{y:2}")

    def test_root_selectors_become_the_scope_element(self):
        self.assertEqual(self.scoped("svg{fill:red}"), "#S{fill:red}")
        self.assertEqual(self.scoped(":root .a{fill:red}"), "#S .c-a{fill:red}")
        self.assertEqual(self.scoped("svg.dark path{fill:red}"), "#S.c-dark path{fill:red}")
        self.assertEqual(self.scoped("svgfoo{fill:red}"), "#S svgfoo{fill:red}")

    def test_declarations_keep_colors_and_numbers_but_follow_id_renames(self):
        self.assertEqual(
            self.scoped(".a{fill:#fff;opacity:.5;stroke:url(#g);mask:url('#m')}"),
            "#S .c-a{fill:#fff;opacity:.5;stroke:url(#c-g);mask:url(#c-m)}",
        )

    def test_strings_and_attribute_selectors_are_left_alone(self):
        self.assertEqual(self.scoped('[class~="a.b"] .x{c:d}'), '#S [class~="a.b"] .c-x{c:d}')
        self.assertEqual(self.scoped(".a:not(.b){c:d}"), "#S .c-a:not(.c-b){c:d}")

    def test_comments_cdata_markers_and_at_rules(self):
        css = """<!-- /* note { */ @charset "utf-8"; @import url("x.css");
            .a{b:c} @media (min-width: 1px) { .d , e {f:g} } @font-face{font-family:"X.y"} -->"""
        self.assertEqual(
            self.scoped(css),
            '#S .c-a{b:c}@media (min-width: 1px){#S .c-d,#S e{f:g}}@font-face{font-family:"X.y"}',
        )

    def test_broken_sheets_are_reported(self):
        for css in (".a{b:c", ".a}", "garbage", "{a:b}"):
            with self.assertRaises(build.Skip) as caught:
                self.scoped(css)
            self.assertEqual(caught.exception.reason, "css", css)


class InlineTests(unittest.TestCase):
    def inline(self, markup, clip=True):
        return build.inline_svg(svg_root(markup), slot(), clip)

    def test_fixture_with_editor_cruft_is_cleaned(self):
        data = (FIXTURES / "data/icons/hicolor/scalable/apps/alpha.svg").read_bytes()
        content = build.inline_svg(build.parse_svg(data), slot(), True)
        for gone in (
            "inkscape",
            "sodipodi",
            "rdf",
            "metadata",
            "namedview",
            "<title",
            "<script",
            "onload",
            "DOCTYPE",
            "<?xml",
            "<!--",
        ):
            self.assertNotIn(gone, content)
        tree = wrap(content)
        ids = [el.get("id") for el in tree.iter() if el.get("id")]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(i.startswith(("c-", "dx-")) for i in ids), ids)
        self.assertIn('stop-color="#ff8800"', content)
        self.assertIn('fill="url(#c-grad)"', content)
        self.assertIn("stroke:url(#c-grad2)", content)
        self.assertIn('clip-path="url(#c-clip)"', content)
        hrefs = [
            el.get("href") or el.get(XLINK + "href") for el in tree.iter() if el.get("href") or el.get(XLINK + "href")
        ]
        self.assertEqual(sorted(hrefs), ["#c-grad", "#c-tile", "#c-tile"])
        for ref in re.findall(r"url\(#([^)]+)\)", content) + [h[1:] for h in hrefs]:
            self.assertIn(ref, ids)
        self.assertIn('>a <tspan font-weight="bold">b</tspan> &lt;c&gt;</text>', content)

    def test_root_becomes_a_group_that_keeps_presentation_attributes(self):
        content = self.inline(
            '<svg xmlns="http://www.w3.org/2000/svg" id="top" class="a b" fill="none" stroke="#000" '
            'width="8" height="8" viewBox="0 0 8 8" version="1.1" transform="rotate(5)"><path d="M0 0h8"/></svg>',
            clip=False,
        )
        self.assertEqual(
            content,
            '<g id="c-top" transform="translate(10.24 10.24) scale(5.44)" class="c-a c-b" fill="none" stroke="#000">'
            '<path d="M0 0h8"/></g>',
        )

    def test_elements_without_a_namespace_are_treated_as_svg(self):
        content = self.inline('<svg viewBox="0 0 8 8"><rect width="8" height="8"/></svg>', clip=False)
        self.assertEqual(
            content,
            '<g id="dx-content" transform="translate(10.24 10.24) scale(5.44)"><rect width="8" height="8"/></g>',
        )

    def test_viewport_is_clipped_with_a_mask(self):
        content = self.inline(
            '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10" viewBox="0 0 10 10"><rect width="99" height="99"/></svg>'
        )
        tree = wrap(content)
        mask = tree.find(SVG + "mask")
        self.assertEqual(
            mask.attrib,
            {
                "id": "dx-clip",
                "maskUnits": "userSpaceOnUse",
                "x": "10.24",
                "y": "21.12",
                "width": "43.52",
                "height": "21.76",
            },
        )
        self.assertEqual(
            mask[0].attrib, {"x": "10.24", "y": "21.12", "width": "43.52", "height": "21.76", "fill": "#fff"}
        )
        self.assertEqual(tree.find(SVG + "g").get("mask"), "url(#dx-clip)")
        self.assertNotIn("clip-path", content)

    def test_no_mask_when_clipping_is_off_or_overflow_is_visible(self):
        base = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"%s><rect width="8" height="8"/></svg>'
        self.assertNotIn("<mask", self.inline(base % "", clip=False))
        self.assertNotIn("<mask", self.inline(base % ' overflow="visible"'))
        self.assertNotIn("<mask", self.inline(base % ' style="fill:red;overflow:visible"'))
        self.assertIn("<mask", self.inline(base % ""))

    def test_styles_are_hoisted_before_the_content_and_scoped(self):
        data = (FIXTURES / "data/icons/hicolor/scalable/apps/styled.svg").read_bytes()
        content = build.inline_svg(build.parse_svg(data), slot(), True)
        self.assertEqual(content.count("<style"), 1)
        self.assertTrue(content.startswith('<style type="text/css">'))
        css = wrap(content).find(SVG + "style").text
        self.assertTrue(css.startswith("#dx-content{stroke-linecap: round;}#dx-content .c-plate{fill: #20304a;}"), css)
        self.assertIn("#dx-content circle{fill: #ffcc00;}", css)
        self.assertIn("#dx-content #c-mark{stroke: url(#c-ink); stroke-width: 2; fill: none;}", css)
        self.assertIn('class="c-dot c-big"', content)
        self.assertIn('id="c-ink"', content)

    def test_a_bare_clip_path_is_wrapped_in_defs(self):
        doc = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8">%s<rect width="8" height="8" clip-path="url(#k)"/></svg>'
        bare = self.inline(doc % '<clipPath id="k"><rect width="4" height="4"/></clipPath>', clip=False)
        self.assertIn('<defs><clipPath id="c-k"><rect width="4" height="4"/></clipPath></defs>', bare)
        nested = self.inline(
            doc % '<defs><g><clipPath id="k"><rect width="4" height="4"/></clipPath></g></defs>', clip=False
        )
        self.assertEqual(nested.count("<defs>"), 1)

    def test_references_outside_the_document_skip_the_icon(self):
        doc = '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 8 8">%s</svg>'
        for body in (
            '<image xlink:href="photo.png" width="8" height="8"/>',
            '<image href="file:///tmp/photo.png" width="8" height="8"/>',
            '<use xlink:href="sprites.svg#a"/>',
            '<linearGradient id="g" href="https://example.invalid/g.svg#g"/><rect fill="url(#g)" width="8" height="8"/>',
        ):
            with self.assertRaises(build.Skip) as caught:
                self.inline(doc % body)
            self.assertEqual(caught.exception.reason, "external-ref", body)

    def test_data_images_and_links_are_kept(self):
        doc = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8">%s</svg>'
        self.assertIn(
            'href="data:image/png;base64,AAAA"',
            self.inline(doc % '<image href=" data:image/png;base64,AAAA" width="8" height="8"/>'),
        )
        self.assertIn(
            'href="https://example.invalid/"',
            self.inline(doc % '<a href="https://example.invalid/"><rect width="8" height="8"/></a>'),
        )

    def test_documents_that_cannot_be_drawn_are_skipped(self):
        cases = {
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect></svg>': "invalid-xml",
            b"not xml at all": "invalid-xml",
            b'<html xmlns="http://www.w3.org/1999/xhtml"/>': "not-svg",
            b'<svg xmlns="http://example.invalid/other"/>': "not-svg",
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><title>t</title></svg>': "empty",
            b"\x1f\x8bnot really gzip": "bad-gzip",
        }
        for data, reason in cases.items():
            with self.assertRaises(build.Skip) as caught:
                build.inline_svg(build.parse_svg(data), slot(), True)
            self.assertEqual(caught.exception.reason, reason, data)

    def test_gzip_data_is_read(self):
        data = (FIXTURES / "data/icons/hicolor/scalable/apps/zipped.svgz").read_bytes()
        self.assertEqual(data[:2], b"\x1f\x8b")
        self.assertIn('<path id="c-z"', build.inline_svg(build.parse_svg(data), slot(), False))


class ColorTests(unittest.TestCase):
    def test_colors_are_validated_and_normalized(self):
        self.assertEqual(build.normalize_color("#ABC", "x"), "#aabbcc")
        self.assertEqual(build.normalize_color(" #6750A4 ", "x"), "#6750a4")
        for bad in ("red", "#12345", "#gggggg", '#fff"/><script>', "", None):
            with self.assertRaises(build.BuildError):
                build.normalize_color(bad, "x")

    def test_darker_lowers_the_tone_by_twelve_and_keeps_the_hue(self):
        for color in ("#6750a4", "#9ecaff", "#0061a4", "#ffb4ab", "#69b2ff"):
            before = build.rgb_to_lab(build.parse_hex(color))
            after = build.rgb_to_lab(build.parse_hex(build.darker(color)))
            self.assertAlmostEqual(before[0] - after[0], 12, delta=0.6, msg=color)
            hue_before = build.math.degrees(build.math.atan2(before[2], before[1]))
            hue_after = build.math.degrees(build.math.atan2(after[2], after[1]))
            self.assertAlmostEqual(hue_before, hue_after, delta=3, msg=color)

    def test_darker_stops_at_tone_ten(self):
        self.assertAlmostEqual(build.rgb_to_lab(build.parse_hex(build.darker("#2b2b40")))[0], 10, delta=0.6)
        self.assertEqual(build.darker("#000000"), build.darker("#000000"))

    def test_lab_round_trip(self):
        for color in ("#000000", "#ffffff", "#6750a4", "#00ff00"):
            rgb = build.lab_to_rgb_float(build.rgb_to_lab(build.parse_hex(color)))
            self.assertEqual(build.to_hex(tuple(round(v * 255) for v in rgb)), color)

    def test_mono_matrix_maps_black_and_white_to_the_two_palette_colors(self):
        for tile, glyph in (("#9ecaff", "#003258"), ("#003258", "#9ecaff")):
            values = [float(v) for v in build.mono_matrix(tile, glyph).split()]
            rows = [values[i * 5 : i * 5 + 5] for i in range(4)]
            black = [row[4] for row in rows[:3]]
            white = [sum(row[:3]) + row[4] for row in rows[:3]]
            self.assertEqual([round(v * 255) for v in black], [0x00, 0x32, 0x58])
            self.assertEqual([round(v * 255) for v in white], [0x9E, 0xCA, 0xFF])
            self.assertEqual(rows[3], [0, 0, 0, 1, 0])

    def test_retone_leaves_the_viewport_mask_white(self):
        art = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect fill="#553C9A"/></svg>'
        content = build.inline_svg(svg_root(art), slot(), True)
        for lightest in (None, 70.0):
            toned = build.retone(content, "#9ecaff", lightest)
            self.assertIn('fill="#fff"/></mask>', toned, lightest)
            self.assertNotIn("#553C9A", toned, lightest)
        lightness = build.rgb_to_lab(build.parse_hex("#553C9A"))[0]
        self.assertIn(f'<rect fill="{build.toned("#9ecaff", lightness)}"/>', build.retone(content, "#9ecaff"))

    def test_retone_reaches_a_css_color_that_ends_its_rule(self):
        # The last declaration of a rule needs no semicolon: its color ends at the closing brace.
        art = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><style>.a{fill:#553C9A}.b{fill:#F5F0FF;}</style>'
            '<rect class="a" width="8" height="8"/><rect class="b" width="4" height="4"/></svg>'
        )
        content = build.inline_svg(svg_root(art), slot(), True)
        self.assertIn("fill:#553C9A}", content)
        light = build.rgb_to_lab(build.parse_hex("#F5F0FF"))[0]
        dark = build.rgb_to_lab(build.parse_hex("#553C9A"))[0]
        toned = build.retone(content, "#9ecaff", 70.0)
        self.assertNotIn("#553C9A", toned)
        self.assertIn(f"fill:{build.toned('#9ecaff', dark + 70 - light)}}}", toned)
        self.assertIn(f"fill:{build.toned('#9ecaff', 70)};", toned)
        # Ids stay ids, before a brace too.
        self.assertEqual(build.COLOR_RE.findall('fill="url(#abc)" x{fill:url(#add)}'), [])

    def test_retone_leaves_every_mask_alone(self):
        # An inner rim in a mask: its tones say how much shows, not paint.
        art = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><defs><mask id="rim">'
            '<rect width="8" height="8" fill="#fff"/><rect x="1" y="1" width="6" height="6" fill="#333"/></mask></defs>'
            '<rect width="8" height="8" fill="#553C9A"/><rect width="8" height="8" fill="#F5F0FF" mask="url(#rim)"/></svg>'
        )
        content = build.inline_svg(svg_root(art), slot(), True)
        masks = build.MASK_RE.findall(content)
        self.assertEqual(len(masks), 2)
        light = build.rgb_to_lab(build.parse_hex("#F5F0FF"))[0]
        for lightest in (None, 70.0):
            toned = build.retone(content, "#9ecaff", lightest)
            self.assertEqual(build.MASK_RE.findall(toned), masks, lightest)
            self.assertNotIn("#553C9A", toned, lightest)
            # The mask's white is no candidate for the lightest: the art's own lightest lands on the tone.
            self.assertIn(f'fill="{build.toned("#9ecaff", lightest or light)}" mask=', toned, lightest)
        # A self-closing mask ends where it closes, so the color after it is still the art's.
        toned = build.retone('<mask id="m"/><rect fill="#553C9A"/><mask id="n"><rect fill="#000"/></mask>', "#9ecaff")
        self.assertNotIn("#553C9A", toned)
        self.assertIn('<mask id="n"><rect fill="#000"/></mask>', toned)


class TileShapeTests(unittest.TestCase):
    """Rings and reach, from the template and the slot."""

    @staticmethod
    def palette(**extra):
        return {"tile": "#112233", "glyph": "#ffffff", "back": "#000000", **extra}

    def test_a_ring_is_drawn_over_the_content_masked_to_a_circle(self):
        templates = build.Templates(FIXTURES / "src", "circle", False)
        painted = templates.get(self.palette(ring="#ff0000"))
        # A mask, which Qt SVG honors, never a clip-path, which it ignores.
        self.assertIn(
            '@content<mask id="dx-shape" maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64">'
            '<circle cx="32" cy="32" r="32" fill="#fff"/></mask>'
            '<g mask="url(#dx-shape)"><circle cx="32" cy="32" r="32" fill="none" stroke="#ff0000" '
            'stroke-width="6"/></g>',
            painted,
        )
        self.assertNotIn("clip", painted)
        ET.fromstring(painted.replace("@content", ""))
        self.assertNotIn("dx-shape", templates.get(self.palette()))

    def test_a_ring_follows_a_path_shape_and_keeps_the_gradient(self):
        templates = build.Templates(build.SRC, "hexagon", True)
        painted = templates.get(self.palette(ring="#ff0000"))
        d = re.search(r'<path d="([^"]+)" fill="url\(#dx-fill\)"/>', painted).group(1)
        self.assertIn(f'height="64"><path d="{d}" fill="#fff"/></mask>', painted)
        self.assertIn(
            f'<g mask="url(#dx-shape)"><path d="{d}" fill="none" stroke="#ff0000" stroke-width="6"/></g>',
            painted,
        )
        self.assertNotIn("clip", painted)
        self.assertLess(painted.index("@content"), painted.index("dx-shape"))
        ET.fromstring(painted.replace("@content", ""))

    def test_a_ring_needs_the_tile_shape(self):
        src = Path(tempfile.mkdtemp(prefix="dux-icons-test-"))
        self.addCleanup(build.shutil.rmtree, src)
        (src / "templates").mkdir()
        (src / "templates/odd.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 64 64">'
            '<circle cx="32" cy="32" r="32" fill="@tile"></circle>@content</svg>'
        )
        templates = build.Templates(src, "odd", False)
        self.assertEqual(templates.inset, build.INSET_DEFAULT)
        templates.get(self.palette())
        with self.assertRaises(build.BuildError):
            templates.get(self.palette(ring="#ff0000"))

    def test_reach_drops_the_gradient(self):
        templates = build.Templates(FIXTURES / "src", "circle", True)
        self.assertIn("dx-fill", templates.get(self.palette()))
        flat = templates.get(self.palette(reach=0.75))
        self.assertNotIn("dx-fill", flat)
        self.assertIn('fill="#112233"', flat)

    def test_every_shipped_shape_has_an_inset_radius(self):
        self.assertLessEqual(set(build.shapes(build.SRC)), set(build.INSET_RADII))
        for name, radius in build.INSET_RADII.items():
            self.assertTrue(0 < radius <= 32, name)
        self.assertEqual(build.Templates(build.SRC, "hexagon", False).inset, 28.3)

    def test_reach_grows_the_farthest_pixel_onto_the_inset_less_the_margin(self):
        recipe = build.load_json(FIXTURES / "src" / "recipe.json")
        app = build.layout_for("apps", "alpha", recipe, own=False)
        half = 32 * (1 - 2 * 0.16)
        self.assertAlmostEqual(app.size / 2, half)
        self.assertAlmostEqual(build.reach_scale(0.75, app, 32.0, False), (32 - 2) / (0.75 * half))
        self.assertAlmostEqual(build.reach_scale(0.75, app, 32.0, True), (32 - 5) / (0.75 * half))
        self.assertAlmostEqual(build.reach_scale(0.75, app, 28.3, False), (28.3 - 2) / (0.75 * half))
        self.assertEqual(build.reach_scale(1.5, app, 32.0, False), 1.0)
        self.assertEqual(build.reach_scale(0.1, app, 32.0, False), 2.0)
        # An offset slot counts its distance from the center.
        moved = build.Slot(0.16, (0.0, -1.6))
        self.assertAlmostEqual(build.reach_scale(0.75, moved, 32.0, False), 30 / (0.75 * half + 1.6))

    def test_grow_scales_about_the_center(self):
        self.assertEqual(build.grow("<g/>", 1.0), "<g/>")
        self.assertEqual(build.grow("<g/>", 1.5), '<g transform="translate(-16 -16) scale(1.5)"><g/></g>')


class RasterTests(unittest.TestCase):
    def test_png_and_jpeg_sizes(self):
        self.assertEqual(build.png_size((FIXTURES / "data/icons/hicolor/256x256/apps/raster.png").read_bytes()), (8, 4))
        self.assertEqual(build.jpeg_size((FIXTURES / "data/pixmaps/photo.png").read_bytes()), (10, 6))
        with self.assertRaises(build.Skip):
            build.png_size(b"\x89PNG\r\n\x1a\n")
        with self.assertRaises(build.Skip):
            build.jpeg_size(b"\xff\xd8\xff\xd9")

    def test_content_is_recognized_by_its_bytes_not_its_name(self):
        self.assertEqual(build.sniff((FIXTURES / "data/pixmaps/photo.png").read_bytes()), "jpeg")
        self.assertEqual(build.sniff((FIXTURES / "data/pixmaps/pix.xpm").read_bytes()), "xpm")
        self.assertEqual(build.sniff((FIXTURES / "data/pixmaps/weird.png").read_bytes()), "other")
        self.assertEqual(build.sniff(b"\xef\xbb\xbf\n <?xml version='1.0'?><svg/>"), "svg")
        self.assertEqual(build.sniff((FIXTURES / "data/icons/hicolor/scalable/apps/zipped.svgz").read_bytes()), "svg")

    def test_xpm_is_decoded_to_png(self):
        png, width, height = build.xpm_to_png((FIXTURES / "data/pixmaps/pix.xpm").read_bytes())
        self.assertEqual((width, height), (4, 2))
        self.assertEqual(build.png_size(png), (4, 2))
        clear, red, black, gray = b"\0\0\0\0", b"\xff\0\0\xff", b"\0\0\0\xff", b"\x80\x80\x80\xff"
        self.assertEqual(decode_png(png), [clear + red + black + clear, red + black + black + gray])

    def test_xpm_with_unknown_color_names_or_bad_shape_is_skipped(self):
        cases = {
            b'/* XPM */ static char *x[] = {"1 1 1 1", "a c PapayaWhip", "a"};': "xpm-color",
            b'/* XPM */ static char *x[] = {"2 1 1 1", "a c #fff", "a"};': "bad-xpm",
            b'/* XPM */ static char *x[] = {"1 1 1 1", "a c #fff", "b"};': "bad-xpm",
            b"/* XPM */ static char *x[] = {};": "bad-xpm",
        }
        for data, reason in cases.items():
            with self.assertRaises(build.Skip) as caught:
                build.xpm_to_png(data)
            self.assertEqual(caught.exception.reason, reason, data)

    def test_image_box_has_the_image_aspect_ratio(self):
        markup = build.image_markup("image/png", b"abc", 8, 4, slot(0.25))
        self.assertEqual(
            markup,
            '<image x="16" y="24" width="32" height="16" preserveAspectRatio="none" xlink:href="data:image/png;base64,YWJj"/>',
        )
        with self.assertRaises(build.Skip):
            build.image_markup("image/png", b"abc", 0, 4, slot())


class LayoutTests(unittest.TestCase):
    def place(self, recipe, context, name, own=True):
        found = build.layout_for(context, name, recipe, own)
        return round(found.x, 4), round(found.y, 4), round(found.size, 4)

    def test_defaults_come_from_layout(self):
        recipe = {"layout": {"vendorPadding": 0.25, "ownPadding": 0.125, "offset": [1, 2]}}
        self.assertEqual(self.place(recipe, "places", "folder"), (9, 10, 48))
        self.assertEqual(self.place(recipe, "apps", "firefox", own=False), (17, 18, 32))
        self.assertEqual(self.place({}, "places", "folder"), (12.8, 12.8, 38.4))

    def test_override_rules_match_globs_on_name_or_context_and_name(self):
        recipe = {
            "overrides": [
                {"match": ["places/folder", "places/folder-*", "status/folder-open"], "offset": [0, -1.6]},
                {"match": "*-x-generic", "padding": 0.25},
                {"match": "places/folder-remote", "offset": [0, 3]},
            ]
        }
        self.assertEqual(self.place(recipe, "places", "folder"), (12.8, 11.2, 38.4))
        self.assertEqual(self.place(recipe, "places", "folder-music"), (12.8, 11.2, 38.4))
        self.assertEqual(self.place(recipe, "status", "folder-open"), (12.8, 11.2, 38.4))
        self.assertEqual(self.place(recipe, "status", "folder-music"), (12.8, 12.8, 38.4))
        self.assertEqual(self.place(recipe, "places", "folders"), (12.8, 12.8, 38.4))
        self.assertEqual(self.place(recipe, "mimetypes", "text-x-generic"), (16, 16, 32))
        self.assertEqual(self.place(recipe, "places", "folder-remote"), (12.8, 15.8, 38.4))

    def test_bad_overrides_stop_the_build(self):
        for overrides in (
            [{"offset": [0, 1]}],
            [{"match": 3}],
            [{"match": []}],
            ["folder"],
            {"folder": {"offset": [0, 1]}},
            [{"match": "folder", "offset": [1]}],
            [{"match": "folder", "padding": 0.5}],
        ):
            with self.assertRaises(build.BuildError, msg=overrides):
                build.layout_for("places", "folder", {"overrides": overrides}, True)


class BuildCase(unittest.TestCase):
    """Runs the whole build against the fixtures."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="dux-icons-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        self.data_home = self.home / ".local/share"
        self.data_home.mkdir(parents=True)
        # A copy, since a build writes its lookup cache there.
        shutil.copytree(FIXTURES / "cache", self.tmp / "cache")
        self.env = {
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.data_home),
            "XDG_DATA_DIRS": str(FIXTURES / "data"),
            "XDG_CACHE_HOME": str(self.tmp / "cache"),
        }
        self.out = self.tmp / "published" / "dux-icons"

    def build(self, *args, out=None, src=None):
        """(exit code, stdout, stderr)"""
        argv = ["--out", str(out or self.out), *args]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = build.main(argv, env=self.env, src=src or FIXTURES / "src", stdout=stdout)
        return code, stdout.getvalue(), stderr.getvalue()

    def built(self, *args, **kwargs):
        code, stdout, stderr = self.build(*args, **kwargs)
        self.assertEqual((code, stderr), (0, ""), stdout)
        return stdout

    def icon(self, rel, out=None):
        return ((out or self.out) / "scalable" / rel).read_text(encoding="utf-8")

    @staticmethod
    def snapshot(folder):
        result = {}
        for path in sorted(folder.rglob("*")):
            rel = str(path.relative_to(folder))
            if path.is_symlink():
                result[rel] = ("link", os.readlink(path))
            elif path.is_file():
                result[rel] = ("file", path.read_bytes())
        return result


class PublishedThemeTests(BuildCase):
    def test_counts_and_reasons_are_reported(self):
        stdout = self.built()
        self.assertIn(f"Dux Icons: published 'Dux Icons' to {self.out}", stdout)
        self.assertIn("own icons: 2 built", stdout)
        self.assertIn(
            "desktop entries: 22 seen, 19 visible with an icon (1 hidden, 1 without Icon=, 1 not applications)", stdout
        )
        self.assertIn("vendor icons: 15 distinct, 5 built from SVG, 3 built from raster, 7 skipped", stdout)
        self.assertIn("aliases: 5 own, 4 desktop entry, 4 MIME type", stdout)
        for line in (
            "skipped own [alias-without-target] 1: orphan -> absent",
            "skipped own [unknown-token] 1: places/badtoken",
            "skipped vendor [empty] 1: hollow",
            "skipped vendor [external-ref] 1: external",
            "skipped vendor [invalid-xml] 1: broken",
            "skipped vendor [no-size] 1: nosize",
            "skipped vendor [not-found] 1: ghost",
            "skipped vendor [own-icon] 1: folder",
            "skipped vendor [unsupported-format] 1: weird",
            "palette: tile=#6750a4 glyph=#ffffff back=#473484 apps=color shape=circle gradient=no",
        ):
            self.assertIn(line, stdout)
        self.assertRegex(stdout, r"18 files, 17 symlinks, built in \d+\.\d\ds")

    def test_tree_layout(self):
        self.built()
        tree = self.snapshot(self.out)
        self.assertEqual(
            sorted(tree),
            sorted(
                [".dux-icons-build", "index.theme"]
                + [
                    f"scalable/apps/{n}.svg"
                    for n in ("alpha", "nobox", "photo", "pix", "raster", "slice", "styled", "zipped")
                ]
                + [f"scalable/apps/{n}.svg" for n in ("actions", "hidden", "kde-nested", "twin")]
                + [
                    f"bare/apps/{n}.svg"
                    for n in ("alpha", "nobox", "photo", "pix", "raster", "slice", "styled", "zipped")
                    + ("actions", "hidden", "kde-nested", "twin")
                ]
                + [f"scalable/mimetypes/{n}.svg" for n in ("text-x-generic", "text-x-script", "x-office-document")]
                + [
                    f"scalable/mimetypes/{n}.svg"
                    for n in ("application-pdf", "application-x-pdf", "text-plain", "text-x-python")
                ]
                + [f"scalable/places/{n}.svg" for n in ("folder", "inode-directory", "folder-documents", "folder-docs")]
            ),
        )
        self.assertFalse(self.out.is_symlink())
        self.assertEqual(sorted(p.name for p in self.out.parent.iterdir()), ["dux-icons"])

    def test_aliases_are_relative_symlinks_to_real_files(self):
        self.built()
        expected = {
            "apps/twin.svg": "alpha.svg",
            "apps/actions.svg": "zipped.svg",
            "apps/kde-nested.svg": "nobox.svg",
            "places/inode-directory.svg": "folder.svg",
            "places/folder-documents.svg": "folder.svg",
            "places/folder-docs.svg": "folder.svg",
            "mimetypes/text-x-script.svg": "text-x-generic.svg",
            "mimetypes/x-office-document.svg": "text-x-generic.svg",
            "mimetypes/text-plain.svg": "text-x-generic.svg",
            "mimetypes/text-x-python.svg": "text-x-generic.svg",
            "mimetypes/application-pdf.svg": "text-x-generic.svg",
            "mimetypes/application-x-pdf.svg": "text-x-generic.svg",
        }
        for rel, target in expected.items():
            link = self.out / "scalable" / rel
            self.assertTrue(link.is_symlink(), rel)
            self.assertEqual(os.readlink(link), target, rel)
            self.assertTrue(link.resolve().is_file(), rel)
        for missing in ("mimetypes/image-png.svg", "mimetypes/application-x-foo.svg", "places/orphan.svg"):
            self.assertFalse(os.path.lexists(self.out / "scalable" / missing), missing)

    def test_index_theme(self):
        self.built()
        index = (self.out / "index.theme").read_text()
        self.assertTrue(
            index.startswith(
                "[Icon Theme]\nName=Dux Icons\nComment=Material icons for Dux Modular Shell\nInherits=hicolor\n"
            )
        )
        self.assertIn("\nDirectories=scalable/apps,scalable/mimetypes,scalable/places\n", index)
        self.assertIn(
            "\n[scalable/apps]\nContext=Applications\nSize=64\nMinSize=16\nMaxSize=512\nType=Scalable\n", index
        )
        self.assertIn(
            "\n[scalable/mimetypes]\nContext=MimeTypes\nSize=64\nMinSize=16\nMaxSize=512\nType=Scalable\n", index
        )
        self.assertIn("\n[scalable/places]\nContext=Places\nSize=64\nMinSize=16\nMaxSize=512\nType=Scalable\n", index)
        self.assertNotIn("@", index)
        self.assertNotRegex(index, r"\d+x\d+")
        self.assertEqual((self.out / ".dux-icons-build").read_text(), "dux-icons 1\n")

    def test_name_option(self):
        self.built("--name", "Dux Icons (slot b)")
        self.assertIn("\nName=Dux Icons (slot b)\n", (self.out / "index.theme").read_text())
        code, _, stderr = self.build("--name", "two\nlines")
        self.assertEqual(code, 1)
        self.assertIn("Dux Icons: error:", stderr)

    def test_every_icon_is_well_formed_and_self_contained(self):
        self.built()
        files = [p for p in (self.out / "scalable").rglob("*.svg") if not p.is_symlink()]
        self.assertEqual(len(files), 10)
        for path in files:
            text = path.read_text(encoding="utf-8")
            root = ET.fromstring(text)
            self.assertEqual(root.tag, SVG + "svg", path)
            self.assertEqual(root.get("viewBox"), "0 0 64 64", path)
            self.assertEqual(root[0].attrib, {"cx": "32", "cy": "32", "r": "32", "fill": "#6750a4"}, path)
            self.assertNotRegex(text, r"@(tile|glyph|back|content)", path)
            ids = [el.get("id") for el in root.iter() if el.get("id")]
            self.assertEqual(len(ids), len(set(ids)), path)
            refs = re.findall(r"url\(#([^)]+)\)", text)
            refs += [
                v[1:] for el in root.iter() for k, v in el.attrib.items() if k.endswith("href") and v.startswith("#")
            ]
            for ref in refs:
                self.assertIn(ref, ids, path)
            for el in root.iter():
                href = el.get(XLINK + "href") or el.get("href") or ""
                self.assertTrue(href == "" or href.startswith(("#", "data:")), path)

    def test_vendor_svg_is_inlined_with_the_computed_transform(self):
        self.built()
        self.assertIn(
            '<g id="c-svg1" transform="translate(10.24 10.24) scale(0.906667)" fill="#102030">',
            self.icon("apps/alpha.svg"),
        )
        self.assertIn('<g id="dx-content" transform="translate(16 16) scale(2)">', self.icon("apps/zipped.svg"))
        self.assertIn("<mask", self.icon("apps/alpha.svg"))

    def test_scalable_svg_beats_png_and_the_256_px_raster_is_preferred(self):
        self.built()
        self.assertNotIn("<image", self.icon("apps/alpha.svg"))
        raster = self.icon("apps/raster.svg")
        payload = (FIXTURES / "data/icons/hicolor/256x256/apps/raster.png").read_bytes()
        self.assertIn(
            '<image x="10.24" y="21.12" width="43.52" height="21.76" preserveAspectRatio="none" '
            f'xlink:href="data:image/png;base64,{build.base64.b64encode(payload).decode()}"/>',
            raster,
        )

    def test_jpeg_and_xpm_content(self):
        self.built()
        self.assertIn(
            'width="43.52" height="26.112" preserveAspectRatio="none" xlink:href="data:image/jpeg;base64,',
            self.icon("apps/photo.svg"),
        )
        pix = self.icon("apps/pix.svg")
        match = re.search(r'xlink:href="data:image/png;base64,([^"]+)"', pix)
        self.assertEqual(build.png_size(build.base64.b64decode(match.group(1))), (4, 2))

    def test_own_icons_take_the_three_tokens(self):
        self.built("--tile", "#112233", "--glyph", "#ddeeff", "--back", "#000001")
        folder = self.icon("places/folder.svg")
        self.assertIn('<circle cx="32" cy="32" r="32" fill="#112233"/>', folder)
        self.assertIn('<g id="dx-content" transform="translate(12.8 11.2) scale(1.6)">', folder)
        self.assertIn('<path id="c-plate" fill="#000001"', folder)
        self.assertIn('fill="#ddeeff"', folder)
        self.assertNotIn("<mask", folder)

    def test_back_defaults_to_the_tile_twelve_tones_darker(self):
        stdout = self.built("--tile", "#9ecaff")
        back = re.search(r"back=(#[0-9a-f]{6})", stdout).group(1)
        self.assertEqual(back, build.darker("#9ecaff"))
        self.assertIn(f'fill="{back}"', self.icon("places/folder.svg"))

    def test_palette_sources(self):
        palette = self.tmp / "palette.json"
        palette.write_text('{"tile": "#abc", "glyph": "#010203", "back": "#040506"}')
        self.assertIn("tile=#aabbcc glyph=#010203 back=#040506", self.built("--palette", str(palette)))
        self.assertIn(
            "tile=#aabbcc glyph=#ffffff back=#040506", self.built("--palette", str(palette), "--glyph", "#FFF")
        )
        self.assertIn("tile=#9ecaff glyph=#003258", self.built("--from-dms"))
        self.assertIn("tile=#0061a4 glyph=#ffffff", self.built("--from-dms", "--mode", "light"))
        self.assertIn("tile=#0061a4 glyph=#000000", self.built("--from-dms", "--mode", "light", "--glyph", "#000"))

    def test_bad_palettes_stop_the_build_before_publishing(self):
        palette = self.tmp / "palette.json"
        palette.write_text('{"tile": "#abc"}')
        for args in (["--tile", "blue"], ["--palette", str(palette)], ["--palette", str(self.tmp / "none.json")]):
            code, _, stderr = self.build(*args)
            self.assertEqual(code, 1, args)
            self.assertIn("Dux Icons: error:", stderr)
            self.assertFalse(self.out.exists())
        self.env["XDG_CACHE_HOME"] = str(self.tmp / "no-cache")
        self.assertEqual(self.build("--from-dms")[0], 1)

    def test_badge_is_a_hidden_alias_of_tile(self):
        # Older DMS still passes --badge.
        tile = self.tmp / "tile"
        self.built("--tile", "#9ecaff", "--glyph", "#003258", "--mono", out=tile)
        stdout = self.built("--badge", "#9ecaff", "--glyph", "#003258", "--mono")
        self.assertIn("palette: tile=#9ecaff glyph=#003258", stdout)
        self.assertEqual(self.snapshot(self.out), self.snapshot(tile))
        self.assertEqual(build.parse_args(["--tile", "#000"]).tile, "#000")
        self.assertEqual(build.parse_args(["--badge", "#123456"]).tile, "#123456")
        usage = io.StringIO()
        with contextlib.redirect_stdout(usage), self.assertRaises(SystemExit):
            build.parse_args(["--help"])
        self.assertIn("--tile HEX", usage.getvalue())
        self.assertNotIn("--badge", usage.getvalue())

    def test_legacy_badge_keys_still_name_the_tile(self):
        palette = self.tmp / "palette.json"
        palette.write_text('{"badge": "#abc", "glyph": "#010203"}')
        self.assertIn("tile=#aabbcc glyph=#010203", self.built("--palette", str(palette)))
        palette.write_text('{"badge": "#abc", "tile": "#123456", "glyph": "#010203"}')
        self.assertIn("tile=#123456 glyph=#010203", self.built("--palette", str(palette)))
        src = self.tmp / "src"
        shutil.copytree(FIXTURES / "src", src)
        recipe = json.loads((src / "recipe.json").read_text())
        recipe["tokens"] = {"badge": "on_primary", "glyph": "primary"}
        (src / "recipe.json").write_text(json.dumps(recipe))
        self.assertIn("tile=#003258 glyph=#9ecaff", self.built("--from-dms", src=src))

    def test_bare_app_icons_drop_the_tile_and_keep_mono(self):
        self.built("--app-style", "themed", "--tile", "#9ecaff", "--glyph", "#003258")
        bare = self.out / "bare" / "apps"
        text = (bare / "alpha.svg").read_text(encoding="utf-8")
        self.assertIn("<circle", self.icon("apps/alpha.svg"))
        self.assertNotIn("<circle", text)
        # Off the tile, the glyph takes the tile color.
        self.assertIn('values="0 0 0 0 0.61961 0 0 0 0 0.79216 0 0 0 0 1 ', text)
        raster = (bare / "raster.svg").read_text(encoding="utf-8")
        self.assertIn(f'values="{build.mono_matrix("#9ecaff", "#003258")}"', raster)
        ET.fromstring(text)
        self.assertEqual(os.readlink(bare / "twin.svg"), "alpha.svg")
        self.assertNotIn(build.BARE, (self.out / "index.theme").read_text(encoding="utf-8"))

    def test_mono_filters_vendor_content_only(self):
        stdout = self.built("--app-style", "themed", "--tile", "#9ecaff", "--glyph", "#003258")
        self.assertIn("apps=themed", stdout)
        matrix = build.mono_matrix(build.darker("#9ecaff"), "#003258")
        self.assertIn('<g filter="url(#dx-glyph)">', self.icon("apps/alpha.svg"))
        # Rasters without a cut-out take the duotone.
        for rel in ("apps/raster.svg", "apps/pix.svg"):
            text = self.icon(rel)
            self.assertIn(
                '<filter id="dx-mono" filterUnits="userSpaceOnUse" x="0" y="0" width="64" height="64" '
                f'color-interpolation-filters="sRGB"><feColorMatrix type="matrix" values="{matrix}"/></filter>'
                '<g filter="url(#dx-mono)">',
                text,
            )
            ET.fromstring(text)
        self.assertNotIn("filter", self.icon("places/folder.svg"))
        self.assertNotIn("dx-mono", self.icon("apps/raster.svg", out=self.rebuilt("--no-mono")))

    def test_tinted_repaints_every_logo_in_its_tile_tones(self):
        tiles = self.tmp / "tiles.json"
        tiles.write_text(json.dumps({"apps": {"alpha": {"tile": "#112233", "glyph": "#ddeeff", "back": "#000011"}}}))
        args = ("--tile", "#9ecaff", "--glyph", "#003258", "--tiles", str(tiles))
        stdout = self.built("--app-style", "tinted", *args)
        self.assertIn("apps=tinted", stdout)
        self.assertNotIn("dx-glyph", self.icon("apps/alpha.svg"))
        self.assertIn(f'values="{build.mono_matrix("#000011", "#ddeeff")}"', self.icon("apps/alpha.svg"))
        self.assertIn(f'values="{build.mono_matrix(build.darker("#9ecaff"), "#003258")}"', self.icon("apps/raster.svg"))
        bare = (self.out / "bare/apps/alpha.svg").read_text(encoding="utf-8")
        self.assertIn(f'values="{build.mono_matrix("#9ecaff", "#003258")}"', bare)
        self.assertNotIn("filter", self.icon("places/folder.svg"))
        tinted = self.snapshot(self.out)
        self.assertEqual(self.snapshot(self.rebuilt("--mono", *args)), tinted)

    def rebuilt(self, *args):
        self.built(*args)
        return self.out

    def test_dux_labs_apps_take_the_tile_hue_without_mono(self):
        icons = self.data_home / "icons/hicolor/scalable/apps"
        icons.mkdir(parents=True)
        art = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="#553C9A"/><rect width="9" height="9" style="fill:#F5F0FF"/></svg>'
        for name in ("dev.duxlabs.files", "plain-purple"):
            (icons / f"{name}.svg").write_text(art)
        apps = self.data_home / "applications"
        apps.mkdir()
        for name in ("dev.duxlabs.files", "plain-purple"):
            (apps / f"{name}.desktop").write_text(f"[Desktop Entry]\nType=Application\nName={name}\nIcon={name}\n")
        self.built("--no-mono", "--tile", "#9ecaff")
        ours = self.icon("apps/dev.duxlabs.files.svg")
        for color in ("#553C9A", "#F5F0FF"):
            lightness = build.rgb_to_lab(build.parse_hex(color))[0]
            self.assertIn(build.toned("#9ecaff", lightness), ours)
            self.assertNotIn(color, ours)
        self.assertIn("#553C9A", self.icon("apps/plain-purple.svg"))
        self.assertIn(
            '<g filter="url(#dx-glyph)">',
            self.icon("apps/dev.duxlabs.files.svg", out=self.rebuilt("--app-style", "themed")),
        )

    def test_mono_contrast_adds_a_stretch_before_the_tones(self):
        values = [float(v) for v in build.contrast_matrix(2.0).split()]
        for gray, expected in ((0.5, 0.5), (0.25, 0.0), (0.75, 1.0), (0.6, 0.7)):
            got = sum(values[i] * gray for i in range(3)) + values[4]
            self.assertAlmostEqual(got, expected, places=4)
        self.assertEqual(values[15:], [0, 0, 0, 1, 0])
        palette = {"tile": "#9ecaff", "glyph": "#003258", "back": "#7ba7db"}
        self.assertEqual(build.mono_wrap("<g/>", palette).count("<feColorMatrix"), 1)
        wrapped = build.mono_wrap("<g/>", palette, 2.0, "back")
        self.assertLess(
            wrapped.index(build.contrast_matrix(2.0)), wrapped.index(build.mono_matrix("#7ba7db", "#003258"))
        )

    def test_shape_picks_the_template_and_its_fit(self):
        stdout = self.built("--shape", "square")
        self.assertIn("shape=square", stdout)
        self.assertIn('<rect width="64" height="64" fill="#6750a4"/>', self.icon("apps/alpha.svg"))
        self.assertIn('transform="translate(6.4 6.4) scale(1.066667)"', self.icon("apps/alpha.svg"))
        self.assertIn("<circle", self.icon("apps/alpha.svg", out=self.rebuilt()))

    def test_an_unknown_shape_stops_the_build_and_names_the_shapes(self):
        code, _, stderr = self.build("--shape", "blob")
        self.assertEqual(code, 1)
        self.assertIn("no shape 'blob'; there is circle, square", stderr)
        self.assertFalse(self.out.exists())
        self.assertEqual(self.build("--shape", "../recipe")[0], 1)

    def test_gradient_shades_the_tile_only(self):
        stdout = self.built("--gradient")
        self.assertIn("gradient=yes", stdout)
        text = self.icon("places/folder.svg")
        self.assertIn(
            '<linearGradient id="dx-fill" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="64">'
            f'<stop offset="0" stop-color="{build.darker("#6750a4", -8)}"/>'
            f'<stop offset="1" stop-color="{build.darker("#6750a4", 10)}"/></linearGradient>',
            text,
        )
        self.assertIn('<circle cx="32" cy="32" r="32" fill="url(#dx-fill)"/>', text)
        # The own icon's cutout keeps the flat tile color.
        self.assertIn('fill="#6750a4" d="M5 11h6v2H5z"', text)
        ET.fromstring(text)
        self.assertNotIn("dx-fill", self.icon("places/folder.svg", out=self.rebuilt("--no-gradient")))

    def test_output_is_deterministic(self):
        self.built()
        first = self.snapshot(self.out)
        self.built()
        self.assertEqual(self.snapshot(self.out), first)
        other = self.tmp / "elsewhere" / "theme"
        self.built(out=other)
        self.assertEqual(self.snapshot(other), first)

    def test_rebuild_replaces_the_previous_build_and_leaves_no_temp_dirs(self):
        self.built()
        stale = self.out / "scalable" / "apps" / "stale.svg"
        stale.write_text("<svg/>")
        self.built("--tile", "#101010")
        self.assertFalse(stale.exists())
        self.assertIn('fill="#101010"', self.icon("places/folder.svg"))
        self.assertEqual(sorted(p.name for p in self.out.parent.iterdir()), ["dux-icons"])

    def test_default_out_is_the_xdg_icon_directory(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = build.main([], env=self.env, src=FIXTURES / "src", stdout=stdout)
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        out = self.data_home / "icons" / "dux-icons"
        self.assertTrue((out / "index.theme").is_file())
        self.assertFalse(out.is_symlink())

    def test_an_empty_directory_is_accepted(self):
        self.out.mkdir(parents=True)
        self.built()
        self.assertTrue((self.out / ".dux-icons-build").is_file())


class FilesStyleTests(BuildCase):
    """--files-style color: own places, mimetypes and status icons drawn without the tile."""

    def test_standard_is_the_default_and_the_recipe_can_pick_color(self):
        self.built()
        default = self.snapshot(self.out)
        self.assertIn("files=standard", self.built("--files-style", "standard"))
        self.assertEqual(self.snapshot(self.out), default)
        src = self.tmp / "src"
        shutil.copytree(FIXTURES / "src", src)
        recipe = json.loads((src / "recipe.json").read_text())
        (src / "recipe.json").write_text(json.dumps({**recipe, "filesStyle": "color"}))
        self.assertIn("files=color", self.built(src=src))
        self.assertNotIn("<circle", self.icon("places/folder.svg"))
        self.assertIn("files=standard", self.built("--files-style", "standard", src=src))
        self.assertIn("<circle", self.icon("places/folder.svg"))

    def test_a_bad_recipe_style_stops_the_build(self):
        src = self.tmp / "src"
        shutil.copytree(FIXTURES / "src", src)
        recipe = json.loads((src / "recipe.json").read_text())
        (src / "recipe.json").write_text(json.dumps({**recipe, "filesStyle": "neon"}))
        code, _, stderr = self.build(src=src)
        self.assertEqual(code, 1)
        self.assertIn("recipe 'filesStyle' must be one of standard, color: 'neon'", stderr)
        self.assertFalse(self.out.exists())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build.parse_args(["--files-style", "neon"])

    def test_color_drops_the_tile_for_files_and_folders_only(self):
        stdout = self.built("--files-style", "color")
        self.assertIn("own icons: 2 built (2 in color)", stdout)
        self.assertIn("files=color", stdout)
        for rel in ("places/folder.svg", "mimetypes/text-x-generic.svg"):
            text = self.icon(rel)
            root = ET.fromstring(text)
            self.assertEqual(root.get("viewBox"), "0 0 64 64", rel)
            self.assertNotIn("<circle", text, rel)
            self.assertNotIn("#6750a4", text, rel)
            self.assertNotRegex(text, r"@[a-z]", rel)
        # Filling the canvas: the 24-unit art grows past 64 units, and a folder is drawn on the canvas itself.
        page = self.icon("mimetypes/text-x-generic.svg")
        scale = float(re.search(r'<g id="dx-content" transform="[^"]*scale\(([\d.]+)\)', page).group(1))
        self.assertGreater(scale * 24, 64)
        self.assertIn('<g id="dx-content" transform="translate(0 0) scale(1)">', self.icon("places/folder.svg"))
        self.assertIn('<circle cx="32" cy="32" r="32" fill="#6750a4"/>', self.icon("apps/alpha.svg"))

    def test_the_rim_is_a_masked_doubled_stroke_never_a_clip_path(self):
        self.built("--files-style", "color")
        page = self.icon("mimetypes/text-x-generic.svg")
        self.assertNotIn("clip-path", page)
        self.assertNotIn("clipPath", page)
        masks = re.findall(r'<mask id="(c-rim\d+)" maskUnits="userSpaceOnUse"', page)
        self.assertEqual(masks, ["c-rim0"])
        scale = float(re.search(r'<g id="dx-content" transform="[^"]*scale\(([\d.]+)\)', page).group(1))
        stroke = re.search(r'<path [^>]*stroke-width="([\d.]+)"[^>]*mask="url\(#c-rim0\)"', page)
        # Twice the rim, so the half the mask keeps is the rim width on the canvas.
        self.assertAlmostEqual(float(stroke.group(1)) * scale, 2 * build.COLOR_RIM, delta=0.01)

    def test_a_folder_is_the_layered_template_with_the_marks_on_its_front(self):
        self.built("--files-style", "color", "--tile", "#69b2ff")
        folder = self.icon("places/folder.svg")
        ET.fromstring(folder)
        for banned in ("clip-path", "clipPath", "<filter", "<style", "<text"):
            self.assertNotIn(banned, folder)
        tones = build.color_tones("places", "folder", {"tile": "#69b2ff"}, build.FOLDER_TONES)
        back, front = build.FOLDER_BACK, build.FOLDER_FRONT
        # Back plate and front, each with its rim held inside it by a mask of its own outline.
        for part, d in (("back", back), ("front", front)):
            self.assertIn(
                f'<mask id="c-m-{part}" maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64">'
                f'<path d="{d}" fill="#fff"/></mask>',
                folder,
            )
        self.assertIn(
            f'<path d="{back}" fill="url(#c-back)" stroke="{tones["back_rim"]}" stroke-width="4" '
            'stroke-linejoin="round" mask="url(#c-m-back)"/>',
            folder,
        )
        self.assertIn(
            f'<path d="{front}" fill="url(#c-front)" stroke="url(#c-rim)" stroke-width="5.6" '
            'stroke-linejoin="round" mask="url(#c-m-front)"/>',
            folder,
        )
        # Back to front: back, paper, the lip's shadow, front, shine, marks.
        order = [
            folder.index(f'<path d="{back}" fill="url(#c-back)"'),
            folder.index('fill="url(#c-paper)"/>'),
            folder.index('fill="url(#c-shadow)"/>'),
            folder.index(f'<path d="{front}" fill="url(#c-front)"'),
            folder.index(f'stroke="{tones["shine"]}"'),
            folder.index('<g transform="translate(32 39) scale(2.65) translate(-12 -13.75)">'),
        ]
        self.assertEqual(order, sorted(order))
        for role in ("rim_top", "rim_mid", "rim_bottom", "paper_top", "front_top", "back_bottom"):
            self.assertIn(f'stop-color="{tones[role]}"', folder, role)
        self.assertIn(f'stop-color="{tones["shadow"]}" stop-opacity="0.34"', folder)
        # The fixture's third element is an @tile cut-out: the front gradient, mapped into the marks' units.
        self.assertIn('<path fill="url(#c-cut)" d="M5 11h6v2H5z"/>', folder)

        def ends(gradient):
            found = re.search(
                rf'<linearGradient id="{gradient}" [^>]*x1="(.+?)" y1="(.+?)" x2="(.+?)" y2="(.+?)"', folder
            )
            return [float(v) for v in found.groups()]

        factor, (fx, fy), (tx, ty) = build.FOLDER_MARK
        cut = ends("c-cut")
        moved = [(v - (fx, fy)[i % 2]) * factor + (tx, ty)[i % 2] for i, v in enumerate(cut)]
        for got, want in zip(moved, ends("c-front")):
            self.assertAlmostEqual(got, want, delta=0.001)
        ids = re.findall(r'\sid="([^"]+)"', folder)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertLessEqual(set(re.findall(r"url\(#([^)]+)\)", folder)), set(ids))

    def test_folders_take_the_theme_hue_and_files_their_category_hue(self):
        palette = {"tile": "#69b2ff"}
        _, _, theme_hue = build.lch_of(palette["tile"])
        folder = build.color_tones("places", "folder", palette)
        self.assertAlmostEqual(build.lch_of(folder["rim"])[2], theme_hue, delta=4)
        rims = {}
        for key, category in build.FILE_CATEGORIES.items():
            tones = build.color_tones("mimetypes", category["icons"][0], palette)
            rims[key] = tones["rim"]
            lightness = [build.lch_of(tones[role])[0] for role in ("top", "bottom", "rim", "mark")]
            self.assertEqual(lightness, sorted(lightness, reverse=True), key)
        self.assertEqual(len(set(rims.values())), len(rims), rims)
        # Unlisted mimetypes are text, which is the theme hue at low chroma.
        self.assertEqual(build.color_tones("mimetypes", "x-unknown", palette)["rim"], rims["text"])
        self.assertLess(build.lch_of(rims["text"])[1], 12)
        # Category hues lean toward the theme: blue turns teal image (196) up 15 degrees, green turns it down.
        for tile, hue in (("#69b2ff", 211), ("#8cd790", 181)):
            rim = build.color_tones("mimetypes", "image-x-generic", {"tile": tile})["rim"]
            turned = (build.lch_of(rim)[2] - hue + 180) % 360 - 180
            self.assertAlmostEqual(turned, 0, delta=3, msg=tile)

    def test_a_gray_theme_keeps_files_gray_and_category_hues_unturned(self):
        gray = {"tile": "#c6c6c6"}
        self.assertLess(build.lch_of(build.color_tones("places", "folder", gray)["rim"])[1], 3)
        self.assertLess(build.lch_of(build.color_tones("mimetypes", "text-x-generic", gray)["rim"])[1], 3)
        video = build.color_tones("mimetypes", "video-x-generic", gray)["rim"]
        self.assertAlmostEqual(build.lch_of(video)[2], build.FILE_CATEGORIES["video"]["hue"], delta=4)

    def test_harmonize_turns_half_the_way_up_to_fifteen_degrees(self):
        self.assertAlmostEqual(build.harmonize(100, 120), 110)
        self.assertAlmostEqual(build.harmonize(120, 100), 110)
        self.assertAlmostEqual(build.harmonize(0, 100), 15)
        self.assertAlmostEqual(build.harmonize(350, 10), 0)
        self.assertAlmostEqual(build.harmonize(10, 350), 0)
        self.assertAlmostEqual(build.harmonize(300, 100), 315)

    def test_files_style_is_accepted_with_dump_sources(self):
        code, stdout, stderr = self.build("--dump-sources", "--files-style", "color")
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn('"apps"', stdout)


class RefusalTests(BuildCase):
    def refused(self, out, needle):
        before = self.snapshot(out) if out.is_dir() else None
        code, stdout, stderr = self.build(out=out)
        self.assertEqual(code, 1, stdout)
        self.assertIn("Dux Icons: error: refusing --out", stderr)
        self.assertIn(needle, stderr)
        if before is not None:
            self.assertEqual(self.snapshot(out), before)
        self.assertEqual([p.name for p in out.parent.iterdir() if ".tmp-" in p.name or ".old-" in p.name], [])

    def test_refuses_a_directory_it_did_not_build(self):
        foreign = self.tmp / "foreign"
        (foreign / "scalable").mkdir(parents=True)
        (foreign / "index.theme").write_text("[Icon Theme]\nName=Someone Else\n")
        self.refused(foreign, "did not build")

    def test_refuses_a_symlink_even_to_its_own_build(self):
        self.built()
        link = self.tmp / "link"
        link.symlink_to(self.out)
        self.refused(link, "symlink")
        self.assertTrue(link.is_symlink())

    def test_refuses_a_file(self):
        target = self.tmp / "file"
        target.write_text("x")
        self.refused(target, "not a directory")

    def test_refuses_a_marker_that_is_a_symlink(self):
        foreign = self.tmp / "foreign"
        foreign.mkdir()
        (foreign / "keep.txt").write_text("keep")
        (foreign / ".dux-icons-build").symlink_to(foreign / "keep.txt")
        self.refused(foreign, "did not build")

    def test_an_unwritable_parent_is_an_error_not_a_traceback(self):
        parent = self.tmp / "locked"
        parent.mkdir()
        parent.chmod(0o555)
        self.addCleanup(parent.chmod, 0o755)
        if os.access(parent, os.W_OK):
            self.skipTest("running with privileges that ignore directory modes")
        code, _, stderr = self.build(out=parent / "theme")
        self.assertEqual(code, 1)
        self.assertIn("Dux Icons: error: cannot write to", stderr)

    def test_refuses_the_checkout(self):
        for out in (ROOT, ROOT / "scalable", ROOT / "build" / "theme", ROOT / "tests" / "out"):
            code, _, stderr = self.build(out=out)
            self.assertEqual(code, 1, out)
            self.assertIn("inside the checkout", stderr)
        self.assertFalse((ROOT / "build").exists())
        self.assertFalse((ROOT / "tests" / "out").exists())


class VendorSourceTests(BuildCase):
    def desktop(self, name, icon):
        apps = self.data_home / "applications"
        apps.mkdir(parents=True, exist_ok=True)
        (apps / f"{name}.desktop").write_text(
            f"[Desktop Entry]\nType=Application\nName={name}\nExec={name}\nIcon={icon}\n"
        )

    def test_absolute_icon_paths(self):
        source = FIXTURES / "data/icons/hicolor/scalable/apps/slice.svg"
        self.desktop("abs-app", source)
        self.desktop("gone-app", self.tmp / "nowhere.png")
        stdout = self.built()
        self.assertTrue((self.out / "scalable/apps/slice.svg").is_file())
        self.assertEqual(os.readlink(self.out / "scalable/apps/abs-app.svg"), "slice.svg")
        self.assertIn("skipped vendor [not-found] 2: ghost, nowhere", stdout)
        listed = json.loads((self.out / "icon-paths.json").read_text())
        self.assertEqual(listed, {str(source): "scalable/apps/slice.svg"})

    def test_two_files_with_one_basename_are_two_icons(self):
        first, second = self.tmp / "one/icon.svg", self.tmp / "two/icon.svg"
        for path, color in ((first, "#111111"), (second, "#eeeeee")):
            path.parent.mkdir()
            path.write_text(
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect width="8" height="8" fill="{color}"/></svg>'
            )
        self.desktop("one-app", first)
        self.desktop("two-app", second)
        self.built()
        self.assertFalse((self.out / "scalable/apps/icon.svg").exists())
        self.assertIn("#111111", self.icon("apps/one-app.svg"))
        self.assertIn("#eeeeee", self.icon("apps/two-app.svg"))
        listed = json.loads((self.out / "icon-paths.json").read_text())
        self.assertEqual(listed, {str(first): "scalable/apps/one-app.svg", str(second): "scalable/apps/two-app.svg"})

    def test_a_name_only_an_icon_theme_has_comes_from_the_listed_themes(self):
        stock = self.data_home / "icons/Stock"
        for folder, color in (("16x16/apps", "#000016"), ("64x64/apps", "#000064"), ("symbolic/apps", "#5b5b5b")):
            (stock / folder).mkdir(parents=True)
            for name in ("stock-thing", "alpha"):
                (stock / folder / f"{name}.svg").write_text(
                    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect width="8" height="8" fill="{color}"/></svg>'
                )
        (stock / "index.theme").write_text(
            "[Icon Theme]\nName=Stock\nDirectories=16x16/apps,64x64/apps,symbolic/apps,../escape\n"
            "[16x16/apps]\nSize=16\n[64x64/apps]\nSize=64\n[symbolic/apps]\nSize=512\n[../escape]\nSize=999\n"
        )
        unlisted = self.data_home / "icons/Unlisted/64x64/apps"
        unlisted.mkdir(parents=True)
        (unlisted / "other-thing.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect width="8" height="8"/></svg>'
        )
        (unlisted.parent.parent / "index.theme").write_text(
            "[Icon Theme]\nName=Unlisted\nDirectories=64x64/apps\n[64x64/apps]\nSize=64\n"
        )
        self.desktop("stock-app", "stock-thing")
        self.desktop("other-app", "other-thing")
        stdout = self.built()
        self.assertIn("#000064", self.icon("apps/stock-thing.svg"))
        self.assertNotIn("#0000", self.icon("apps/alpha.svg"), "the app's own icon comes first")
        self.assertIn("skipped vendor [not-found] 2: ghost, other-thing", stdout)

    def test_user_entries_shadow_system_entries(self):
        apps = self.data_home / "applications"
        apps.mkdir(parents=True)
        (apps / "styled.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=styled\nIcon=styled\nHidden=true\n"
        )
        stdout = self.built()
        self.assertIn("2 hidden", stdout)
        self.assertFalse((self.out / "scalable/apps/styled.svg").exists())

    def test_published_themes_are_never_vendor_icons(self):
        self.built()
        copy = self.tmp / "copy"
        self.built(out=copy)
        self.desktop("one", self.out / "scalable/apps/alpha.svg")
        self.desktop("two", copy / "scalable/apps/styled.svg")
        (self.data_home / "icons").mkdir(parents=True)
        (self.data_home / "icons" / "loose.svg").symlink_to(copy / "scalable/apps/nobox.svg")
        self.desktop("six", "loose")
        stdout = self.built("-v")
        self.assertRegex(stdout, r"skipped vendor \[not-found\] .*\bloose\b")
        self.assertEqual(os.readlink(self.out / "scalable/apps/one.svg"), "alpha.svg")
        self.assertIn('transform="translate(10.24 10.24) scale(0.906667)"', self.icon("apps/alpha.svg"))
        self.assertEqual(self.icon("apps/alpha.svg").count("<circle"), 1)
        self.assertEqual(self.icon("apps/styled.svg").count('r="32"'), 1)
        for name in ("loose", "six"):
            self.assertFalse(os.path.lexists(self.out / f"scalable/apps/{name}.svg"), name)

    def test_aliases_that_an_own_icon_already_provides_are_warned_about(self):
        src = self.tmp / "src"
        build.shutil.copytree(FIXTURES / "src", src)
        (src / "aliases.json").write_text(
            '{"places": {"folder": ["inode-directory", "text-x-generic"]}, "mimetypes": {"text-x-generic": ["inode-directory"]}}'
        )
        code, stdout, stderr = self.build(src=src)
        self.assertEqual(code, 0)
        self.assertEqual(
            stderr.splitlines(),
            [
                "Dux Icons: warning: aliases.json: 'inode-directory' is listed more than once; 'folder' is ignored",
                "Dux Icons: warning: aliases.json: 'text-x-generic' (alias of 'folder') is already an own icon; remove the alias",
            ],
        )
        self.assertIn("warnings: 2 (printed to stderr)", stdout)
        self.assertEqual(os.readlink(self.out / "scalable/mimetypes/inode-directory.svg"), "text-x-generic.svg")
        self.assertFalse((self.out / "scalable/mimetypes/text-x-generic.svg").is_symlink())


class PerAppTests(BuildCase):
    """Tiles, --dump-sources, --own-tone and the art in src/art."""

    OURS = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="#553C9A"/>'
        '<rect width="9" height="9" style="fill:#F5F0FF"/></svg>'
    )

    def install(self, name, art):
        icons = self.data_home / "icons/hicolor/scalable/apps"
        icons.mkdir(parents=True, exist_ok=True)
        (icons / f"{name}.svg").write_text(art)
        apps = self.data_home / "applications"
        apps.mkdir(exist_ok=True)
        (apps / f"{name}.desktop").write_text(f"[Desktop Entry]\nType=Application\nName={name}\nIcon={name}\n")

    def tiles(self, data):
        path = self.tmp / "tiles.json"
        path.write_text(json.dumps(data))
        return str(path)

    def tile(self, rel, out=None):
        return ET.fromstring(self.icon(rel, out))[0].get("fill")

    @staticmethod
    def tone(color):
        return build.rgb_to_lab(build.parse_hex(color))[0]

    def test_a_tile_colors_only_its_own_app(self):
        self.install("dev.duxlabs.files", self.OURS)
        self.built()
        bare = self.snapshot(self.out / "bare")
        tiles = {
            "apps": {
                "alpha": {"tile": "#112233"},
                "folder": {"tile": "#445566"},
                "dev.duxlabs.files": {"tile": "#778899"},
                "not-installed": {"tile": "#000000"},
            }
        }
        stdout = self.built("--tiles", self.tiles(tiles))
        self.assertIn("apps=color shape=circle gradient=no files=standard tiles=1\n", stdout)
        self.assertEqual(self.tile("apps/alpha.svg"), "#112233")
        self.assertEqual(os.readlink(self.out / "scalable/apps/twin.svg"), "alpha.svg")
        for rel in ("apps/raster.svg", "apps/dev.duxlabs.files.svg", "places/folder.svg"):
            self.assertEqual(self.tile(rel), "#6750a4", rel)
        self.assertEqual(self.snapshot(self.out / "bare"), bare)

    def test_tile_defaults_and_mono_tones(self):
        tiles = {
            "apps": {
                "pix": {"tile": "#9ecaff"},
                "raster": {"tile": "#ffb4ab", "glyph": "#690005", "back": "#93000a"},
            }
        }
        self.built("--mono", "--tiles", self.tiles(tiles))
        self.assertIn(f'values="{build.mono_matrix(build.darker("#9ecaff"), "#ffffff")}"', self.icon("apps/pix.svg"))
        self.assertIn(f'values="{build.mono_matrix("#93000a", "#690005")}"', self.icon("apps/raster.svg"))
        self.assertEqual(self.tile("apps/raster.svg"), "#ffb4ab")
        bare = (self.out / "bare/apps/pix.svg").read_text()
        self.assertIn(f'values="{build.mono_matrix("#6750a4", "#ffffff")}"', bare)

    def test_a_tile_shades_its_own_gradient(self):
        self.built("--gradient", "--tiles", self.tiles({"apps": {"alpha": {"tile": "#112233"}}}))
        self.assertIn(f'stop-color="{build.darker("#112233", -8)}"', self.icon("apps/alpha.svg"))
        self.assertNotIn(build.darker("#6750a4", -8), self.icon("apps/alpha.svg"))
        self.assertIn(f'stop-color="{build.darker("#6750a4", -8)}"', self.icon("apps/raster.svg"))

    def test_bad_tiles_stop_the_build_before_publishing(self):
        for data in (
            [],
            {"alpha": {"tile": "#112233"}},
            {"apps": []},
            {"apps": {"alpha": "#112233"}},
            {"apps": {"alpha": {"glyph": "#112233"}}},
            {"apps": {"alpha": {"tile": "blue"}}},
            {"apps": {"alpha": {"tile": "#112233", "back": "#12"}}},
            {"apps": {"alpha": {"tile": "#112233", "ring": "red"}}},
            {"apps": {"alpha": {"tile": "#112233", "ring": None}}},
        ) + tuple(
            {"apps": {"alpha": {"tile": "#112233", "reach": bad}}}
            for bad in (0, -0.5, "0.8", True, None, [1], float("nan"), float("inf"))
        ):
            code, _, stderr = self.build("--tiles", self.tiles(data))
            self.assertEqual(code, 1, data)
            self.assertIn("Dux Icons: error:", stderr)
            self.assertFalse(self.out.exists())
        self.assertEqual(self.build("--tiles", str(self.tmp / "none.json"))[0], 1)

    def test_tiles_without_ring_or_reach_render_as_before(self):
        for args in ((), ("--gradient",)):
            self.built(*args)
            before = self.snapshot(self.out)
            self.built(*args, "--tiles", self.tiles({"apps": {"alpha": {"tile": "#112233"}}}))
            after = self.snapshot(self.out)
            rel = "scalable/apps/alpha.svg"
            tiled, plain = after.pop(rel)[1].decode(), before.pop(rel)[1].decode()
            self.assertEqual(after, before)
            if args:
                self.assertEqual(
                    tiled,
                    plain.replace(build.darker("#6750a4", -8), build.darker("#112233", -8), 1).replace(
                        build.darker("#6750a4", 10), build.darker("#112233", 10), 1
                    ),
                )
            else:
                self.assertEqual(tiled, plain.replace('fill="#6750a4"/>', 'fill="#112233"/>', 1))
            self.assertNotIn("dx-shape", tiled)

    def test_a_ring_is_drawn_after_the_content_and_never_bare(self):
        tiles = {"apps": {"alpha": {"tile": "#112233", "ring": "#FF0000"}}}
        stdout = self.built("--app-style", "themed", "--tiles", self.tiles(tiles))
        self.assertIn(" tiles=1 ring=1\n", stdout)
        text = self.icon("apps/alpha.svg")
        ET.fromstring(text)
        self.assertLess(text.index('<g filter="url(#dx-glyph)">'), text.index('<mask id="dx-shape"'))
        self.assertIn('stroke="#ff0000" stroke-width="6"/></g>\n</svg>', text)
        self.assertNotIn("dx-shape", (self.out / "bare/apps/alpha.svg").read_text())
        self.assertNotIn("dx-shape", self.icon("apps/raster.svg"))

    def test_reach_grows_the_content_on_a_flat_tile(self):
        tiles = {
            "apps": {
                "alpha": {"tile": "#112233", "reach": 0.75},
                "raster": {"tile": "#445566", "reach": 0.75, "ring": "#ff0000"},
                "pix": {"tile": "#778899", "reach": 3},
            }
        }
        self.built("--gradient")
        bare = self.snapshot(self.out / "bare")
        stdout = self.built("--gradient", "--tiles", self.tiles(tiles))
        self.assertIn(" tiles=3 ring=1 reach=3\n", stdout)
        half = 32 * (1 - 2 * 0.16)
        for rel, room in (("apps/alpha.svg", 30), ("apps/raster.svg", 27)):
            text = self.icon(rel)
            scale = room / (0.75 * half)
            shift = build.fmt(32 * (1 - scale))
            self.assertIn(f'<g transform="translate({shift} {shift}) scale({build.fmt(scale, 6)})">', text, rel)
            self.assertNotIn("dx-fill", text, rel)
            ET.fromstring(text)
        # Already past the tile: kept as it is, never shrunk.
        pix = self.icon("apps/pix.svg")
        self.assertNotIn('<g transform="translate(', pix)
        self.assertIn('<circle cx="32" cy="32" r="32" fill="#778899"/>', pix)
        self.assertIn("dx-fill", self.icon("apps/styled.svg"))
        self.assertEqual(self.snapshot(self.out / "bare"), bare)

    SHELL = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="@back" d="M2 2h20v20H2z"/>'
        '<path fill="@glyph" d="M6 6h12v12H6z"/><path fill="@tile" d="M9 9h6v6H9z"/></svg>'
    )

    def shell_src(self):
        """The fixture sources plus one of our own app icons, as src/own/apps holds them."""
        src = self.tmp / "src"
        build.shutil.copytree(FIXTURES / "src", src)
        (src / "own/apps").mkdir()
        (src / "own/apps/dev.duxlabs.shell.qr.svg").write_text(self.SHELL)
        return src

    def test_a_shell_palette_paints_our_app_icons_only(self):
        src = self.shell_src()
        self.install("dev.duxlabs.files", self.OURS)
        self.built("--no-mono", src=src)
        before = self.snapshot(self.out)
        tiles = {"shell": {"tile": "#112233", "glyph": "#eeeeee", "ring": "#ff0000"}}
        stdout = self.built("--no-mono", "--tiles", self.tiles(tiles), src=src)
        self.assertIn("gradient=no files=standard tiles=0 shell=1 ring=1\n", stdout)
        after = self.snapshot(self.out)
        rel = "scalable/apps/dev.duxlabs.shell.qr.svg"
        icon = after.pop(rel)[1].decode()
        plain = before.pop(rel)[1].decode()
        # The folder, the vendor apps (dev.duxlabs.files among them) and every bare copy stay as they were.
        self.assertEqual(after, before)
        ET.fromstring(icon)
        self.assertEqual(self.tile("apps/dev.duxlabs.shell.qr.svg"), "#112233")
        for color in (build.darker("#112233"), "#eeeeee", "#112233"):
            self.assertIn(f'fill="{color}" d=', icon, color)
        self.assertIn('<g mask="url(#dx-shape)">', icon)
        self.assertNotIn("dx-shape", plain)
        self.assertEqual(self.tile("places/folder.svg"), "#6750a4")
        # Defaults: glyph from the theme, back from the shell tile.
        self.built("--no-mono", "--tiles", self.tiles({"shell": {"tile": "#112233"}}), src=src)
        self.assertIn('fill="#ffffff" d=', self.icon("apps/dev.duxlabs.shell.qr.svg"))
        self.assertNotIn("dx-shape", self.icon("apps/dev.duxlabs.shell.qr.svg"))

    def test_a_shell_reach_grows_our_app_icons_on_a_flat_tile(self):
        src = self.shell_src()
        self.built("--no-mono", "--gradient", src=src)
        before = self.snapshot(self.out)
        for ring, room in ((None, 30), ("#ff0000", 27)):
            shell = {"tile": "#112233", "reach": 1.0, **({"ring": ring} if ring else {})}
            stdout = self.built("--no-mono", "--gradient", "--tiles", self.tiles({"shell": shell}), src=src)
            self.assertIn(f" shell=1{' ring=1' if ring else ''} reach=1\n", stdout)
            after = self.snapshot(self.out)
            rel = "scalable/apps/dev.duxlabs.shell.qr.svg"
            icon = after.pop(rel)[1].decode()
            plain = before[rel][1].decode()
            # Everything else, the bare copy of this icon among it, stays as it was.
            self.assertEqual(after, {k: v for k, v in before.items() if k != rel})
            ET.fromstring(icon)
            half = 32 * (1 - 2 * 0.2)  # the fixture's ownPadding
            scale = room / half
            shift = build.fmt(32 * (1 - scale))
            self.assertIn(f'<g transform="translate({shift} {shift}) scale({build.fmt(scale, 6)})">', icon)
            self.assertNotIn("dx-fill", icon)
            self.assertIn("dx-fill", plain)
            self.assertEqual(self.tile("apps/dev.duxlabs.shell.qr.svg"), "#112233")
            self.assertEqual("dx-shape" in icon, bool(ring))
            if ring:
                self.assertLess(icon.index("scale("), icon.index('<mask id="dx-shape"'))

    def test_a_dux_palette_tiles_our_apps_and_retones_them_to_its_tint(self):
        self.install("dev.duxlabs.files", self.OURS)
        self.install("plain-purple", self.OURS)
        dark, light = self.tone("#553C9A"), self.tone("#F5F0FF")
        self.built("--no-mono", "--tile", "#9ecaff")
        before = self.snapshot(self.out)
        tiles = {
            "dux": {"tile": "#112233", "tint": "#FF8800", "ring": "#ff0000"},
            "apps": {"alpha": {"tile": "#445566"}},
        }
        stdout = self.built("--no-mono", "--tile", "#9ecaff", "--tiles", self.tiles(tiles))
        self.assertIn(" tiles=1 dux=1 ring=1\n", stdout)
        ours = self.icon("apps/dev.duxlabs.files.svg")
        ET.fromstring(ours)
        self.assertEqual(self.tile("apps/dev.duxlabs.files.svg"), "#112233")
        self.assertIn(f'fill="{build.toned("#ff8800", dark)}"', ours)
        self.assertIn(f'style="fill:{build.toned("#ff8800", light)}"', ours)
        self.assertIn('fill="#fff"/></mask>', ours)
        self.assertIn('<g mask="url(#dx-shape)">', ours)
        after = self.snapshot(self.out)
        for rel in ("scalable/apps/dev.duxlabs.files.svg", "scalable/apps/alpha.svg"):
            after.pop(rel), before.pop(rel)
        # The bare copy keeps the theme hue; someone else's purple app is never retoned.
        self.assertEqual(after, before)
        self.assertIn("#553C9A", self.icon("apps/plain-purple.svg"))
        # Without a tint the art takes the dux tile's hue, and --own-tone still sets its lightest color.
        self.built(
            "--no-mono",
            "--tile",
            "#9ecaff",
            "--own-tone",
            "70",
            "--tiles",
            self.tiles({"dux": {"tile": "#112233"}}),
        )
        ours = self.icon("apps/dev.duxlabs.files.svg")
        self.assertIn(f'style="fill:{build.toned("#112233", 70)}"', ours)
        self.assertIn(f'fill="{build.toned("#112233", dark + 70 - light)}"', ours)
        self.assertNotIn("dx-shape", ours)

    def test_our_light_art_moves_off_a_light_tile_to_a_deep_one_in_its_hue(self):
        self.install("dev.duxlabs.files", self.OURS)
        self.install("plain-purple", self.OURS)
        # A dark scheme's light primary: our apps take its hue at a light scheme's primary tone, other apps keep it.
        self.built("--app-style", "color", "--tile", "#d0bcff", "--glyph", "#381e72")
        deep = self.tile("apps/dev.duxlabs.files.svg")
        self.assertAlmostEqual(self.tone(deep), build.OWN_DEEP_TONES[0], delta=0.5)
        (_, chroma, hue), (_, light_chroma, light_hue) = build.lch_of(deep), build.lch_of("#d0bcff")
        self.assertAlmostEqual(hue, light_hue, delta=1)
        self.assertAlmostEqual(chroma, light_chroma * build.OWN_DEEP_CHROMA, delta=1)
        # About the light scheme's own primary.
        self.assertLess(
            math.dist(build.rgb_to_lab(build.parse_hex(deep)), build.rgb_to_lab(build.parse_hex("#6750a4"))), 6
        )
        self.assertEqual(self.tile("apps/plain-purple.svg"), "#d0bcff")
        # A light ringed dux tile, as Ring gives in a light scheme, is near-neutral: the deep tile takes the ring's
        # theme color, not gray.
        light = {"dux": {"tile": "#f9f9f9", "ring": "#7f67be"}}
        self.built("--app-style", "color", "--tile", "#6750a4", "--glyph", "#ffffff", "--tiles", self.tiles(light))
        ours = self.icon("apps/dev.duxlabs.files.svg")
        ringed = self.tile("apps/dev.duxlabs.files.svg")
        self.assertEqual(ringed, build.toned("#7f67be", build.OWN_DEEP_TONES[0]))
        self.assertGreater(build.lch_of(ringed)[1], 30)
        self.assertIn('stroke="#7f67be"', ours)
        # A deep tile is already right, and only the color style moves it.
        self.built("--app-style", "color", "--tile", "#6750a4", "--glyph", "#ffffff")
        self.assertEqual(self.tile("apps/dev.duxlabs.files.svg"), "#6750a4")
        self.built("--app-style", "tinted", "--tile", "#d0bcff", "--glyph", "#381e72")
        self.assertEqual(self.tile("apps/dev.duxlabs.files.svg"), "#d0bcff")

    def test_without_shell_or_dux_a_tiles_file_changes_nothing(self):
        src = self.shell_src()
        self.install("dev.duxlabs.files", self.OURS)
        for args in (("--no-mono",), ("--mono", "--gradient"), ("--no-mono", "--own-tone", "70")):
            self.built(*args, src=src)
            before = self.snapshot(self.out)
            stdout = self.built(*args, "--tiles", self.tiles({"apps": {}}), src=src)
            self.assertNotIn("shell=", stdout)
            self.assertNotIn("dux=", stdout)
            self.assertEqual(self.snapshot(self.out), before, args)

    def test_bad_shell_or_dux_palettes_stop_the_build(self):
        for data in (
            {},
            {"shell": "#112233"},
            {"shell": {"glyph": "#112233"}},
            {"dux": {"tile": "#112233", "tint": "orange"}},
            {"dux": {"tile": "#112233", "ring": None}},
            {"shell": {"tile": "#112233", "reach": 0}},
        ):
            code, _, stderr = self.build("--tiles", self.tiles(data))
            self.assertEqual(code, 1, data)
            self.assertIn("Dux Icons: error:", stderr)
            self.assertFalse(self.out.exists())

    def test_dump_sources_lists_what_a_build_frames_and_needs_no_colors(self):
        self.install("dev.duxlabs.files", self.OURS)
        cache = self.env["XDG_CACHE_HOME"]
        self.env["XDG_CACHE_HOME"] = str(self.tmp / "no-cache")
        code, stdout, stderr = self.build("--dump-sources", "--from-dms", "--tile", "nonsense")
        self.assertEqual((code, stderr), (0, ""))
        self.assertFalse(self.out.exists())
        sources = json.loads(stdout)["apps"]
        self.assertEqual(sources["alpha"], str(FIXTURES / "data/icons/hicolor/scalable/apps/alpha.svg"))
        self.assertEqual(sources["raster"], str(FIXTURES / "data/icons/hicolor/256x256/apps/raster.png"))
        for name in ("folder", "ghost", "broken", "twin", "dev.duxlabs.files"):
            self.assertNotIn(name, sources)
        self.env["XDG_CACHE_HOME"] = cache
        self.built()
        framed = {p.stem for p in (self.out / "scalable/apps").glob("*.svg") if not p.is_symlink()}
        self.assertEqual(set(sources), framed - {"dev.duxlabs.files"})

    def test_own_tone_moves_our_lightest_color_onto_the_tone(self):
        self.install("dev.duxlabs.files", self.OURS)
        self.install("plain-purple", self.OURS)
        dark, light = self.tone("#553C9A"), self.tone("#F5F0FF")
        stdout = self.built("--no-mono", "--tile", "#9ecaff", "--own-tone", "70")
        self.assertIn("own-tone=70", stdout)
        ours = self.icon("apps/dev.duxlabs.files.svg")
        self.assertIn(f'style="fill:{build.toned("#9ecaff", 70)}"', ours)
        self.assertIn(f'fill="{build.toned("#9ecaff", dark + 70 - light)}"', ours)
        self.assertIn('fill="#fff"/></mask>', ours)
        bare = (self.out / "bare/apps/dev.duxlabs.files.svg").read_text()
        self.assertIn(f"fill:{build.toned('#9ecaff', light)}", bare)
        self.assertIn(f'fill="{build.toned("#9ecaff", dark)}"', bare)
        self.assertIn("#553C9A", self.icon("apps/plain-purple.svg"))
        # Shifted below the floor, the darkest color stops at it.
        self.built("--no-mono", "--tile", "#9ecaff", "--own-tone", "20")
        self.assertIn(f'fill="{build.toned("#9ecaff", 4)}"', self.icon("apps/dev.duxlabs.files.svg"))

    def test_own_tone_at_the_arts_own_lightest_keeps_the_art_and_leaves_the_mask_white(self):
        self.install("dev.duxlabs.files", self.OURS)
        self.built("--no-mono", "--tile", "#9ecaff")
        before = self.snapshot(self.out)
        self.built("--no-mono", "--tile", "#9ecaff", "--own-tone", repr(self.tone("#F5F0FF")))
        # With or without --own-tone the viewport mask stays white, so the art draws fully opaque.
        for rel in ("scalable/apps/dev.duxlabs.files.svg", "bare/apps/dev.duxlabs.files.svg"):
            self.assertIn('fill="#fff"/></mask>', before[rel][1].decode(), rel)
        self.assertEqual(self.snapshot(self.out), before)
        for bad in ("-1", "100.5", "nan", "dark"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                build.parse_args(["--own-tone", bad])

    def test_art_replaces_the_icon_of_an_installed_app_only(self):
        src = self.tmp / "src"
        build.shutil.copytree(FIXTURES / "src", src)
        art = src / "art/apps"
        art.mkdir(parents=True)
        for name in ("alpha", "not-installed"):
            (art / f"{name}.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect width="8" height="8" fill="#0a0b0c"/></svg>'
            )
        stdout = self.built(src=src)
        self.assertIn("5 built from SVG, 3 built from raster, 7 skipped (1 from src/art)", stdout)
        self.assertIn("#0a0b0c", self.icon("apps/alpha.svg"))
        self.assertNotIn("#102030", self.icon("apps/alpha.svg"))
        self.assertIn("#0a0b0c", (self.out / "bare/apps/alpha.svg").read_text())
        self.assertFalse(os.path.lexists(self.out / "scalable/apps/not-installed.svg"))
        code, stdout, _ = self.build("--dump-sources", src=src)
        self.assertEqual(code, 0)
        sources = json.loads(stdout)["apps"]
        self.assertEqual(sources["alpha"], str(art / "alpha.svg"))
        self.assertNotIn("not-installed", sources)


@unittest.skipUnless(HAVE_RSVG, "needs gi Rsvg and cairo")
class RenderTests(BuildCase):
    """librsvg renders of published icons, sampled at a few pixels."""

    def pixel(self, rel, x, y, size=64):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        handle = Rsvg.Handle.new_from_file(str(self.out / "scalable" / rel))
        rect = Rsvg.Rectangle()
        rect.x, rect.y, rect.width, rect.height = 0, 0, size, size
        handle.render_document(cairo.Context(surface), rect)
        offset = y * surface.get_stride() + x * 4
        blue, green, red, alpha = surface.get_data()[offset : offset + 4]
        return red, green, blue, alpha

    def assertNear(self, actual, expected, delta=3):
        for a, e in zip(actual, expected):
            self.assertAlmostEqual(a, e, delta=delta, msg=f"{actual} != {expected}")

    def test_vendor_css_does_not_reach_the_tile(self):
        self.built()
        self.assertNear(self.pixel("apps/styled.svg", 32, 3), (0x67, 0x50, 0xA4, 255))
        self.assertNear(self.pixel("apps/styled.svg", 32, 26), (0xFF, 0xCC, 0x00, 255))
        self.assertNear(self.pixel("apps/styled.svg", 14, 14), (0x20, 0x30, 0x4A, 255))
        self.assertEqual(self.pixel("apps/styled.svg", 1, 1)[3], 0)

    def test_slice_content_is_clipped_to_its_viewport(self):
        self.built()
        self.assertNear(self.pixel("apps/slice.svg", 32, 32), (255, 255, 255, 255))
        self.assertNear(self.pixel("apps/slice.svg", 12, 23), (0xAA, 0, 0, 255))
        self.assertNear(self.pixel("apps/slice.svg", 32, 48), (0x67, 0x50, 0xA4, 255))

    def test_raster_and_own_content_render(self):
        self.built()
        self.assertNotEqual(self.pixel("apps/raster.svg", 32, 32)[:3], (0x67, 0x50, 0xA4))
        self.assertNear(self.pixel("apps/pix.svg", 26, 26), (255, 0, 0, 255))
        self.assertNear(self.pixel("places/folder.svg", 32, 36), (255, 255, 255, 255))
        self.assertNear(self.pixel("places/folder.svg", 22, 20), build.parse_hex("#473484") + (255,))

    def test_a_ring_shows_three_units_inside_the_edge(self):
        tiles = self.tmp / "tiles.json"
        tiles.write_text(json.dumps({"apps": {"alpha": {"tile": "#112233", "ring": "#ff0000"}}}))
        for shape in ("circle", "square"):
            self.built("--shape", shape, "--tiles", str(tiles))
            for x, y in ((32, 1), (32, 2), (1, 32), (62, 32)):
                self.assertNear(self.pixel("apps/alpha.svg", x, y), (255, 0, 0, 255))
            self.assertNear(self.pixel("apps/alpha.svg", 32, 4), (0x11, 0x22, 0x33, 255))

    def test_color_style_draws_the_rim_over_the_marks_and_no_tile(self):
        self.built("--files-style", "color", "--tile", "#69b2ff", src=build.SRC)
        tones = build.color_tones("mimetypes", "package-x-generic", {"tile": "#69b2ff"})
        rel = "mimetypes/package-x-generic.svg"
        # The zipper starts under the top rim (24-unit y 2 to 3.04) and shows below it.
        self.assertNear(self.pixel(rel, 29, 4), build.parse_hex(tones["rim"]) + (255,), delta=6)
        self.assertNear(self.pixel(rel, 29, 7), build.parse_hex(tones["mark"]) + (255,), delta=6)
        # Off the art there is no tile.
        self.assertEqual(self.pixel(rel, 1, 1)[3], 0)
        self.assertEqual(self.pixel("places/folder.svg", 1, 62)[3], 0)
        self.assertEqual(self.pixel("places/folder.svg", 32, 40)[3], 255)

    def test_themed_drops_the_plate_and_draws_the_rest_in_the_glyph_color(self):
        self.built("--app-style", "themed", "--tile", "#9ecaff", "--glyph", "#003258")
        # slice.svg: a red full-bleed rect under a white disc; only the disc is drawn, on the tile.
        self.assertNear(self.pixel("apps/slice.svg", 32, 32), (0x00, 0x32, 0x58, 255))
        self.assertNear(self.pixel("apps/slice.svg", 32, 6), (0x9E, 0xCA, 0xFF, 255))
        self.assertNear(self.pixel("apps/slice.svg", 32, 59), (0x9E, 0xCA, 0xFF, 255))


class GlyphGeometryTests(unittest.TestCase):
    """The pieces of the themed fallback chain, one at a time."""

    @staticmethod
    def box(points):
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        return min(xs), min(ys), max(xs), max(ys)

    def test_path_extent_follows_curves_arcs_and_relative_commands(self):
        self.assertEqual(self.box(build.path_points("M10 10C10 0 30 0 30 10")), (10, 2.5, 30, 10))
        self.assertEqual(self.box(build.path_points("M10 10Q20-10 30 10")), (10, 0, 30, 10))
        for arc in ("M0 10a10 10 0 0 1 20 0", "M0 10a10 10 0 0120 0"):
            x0, y0, x1, y1 = self.box(build.path_points(arc))
            self.assertAlmostEqual(y0, 0, places=6, msg=arc)
            self.assertEqual((x0, x1, y1), (0, 20, 10))
        self.assertEqual(self.box(build.path_points("m5 5h10v10h-10z m20 0l1 1")), (5, 5, 26, 15))
        # A smooth curve mirrors the previous control point.
        self.assertEqual(self.box(build.path_points("M0 10C0 0 10 0 10 10S20 20 20 10")), (0, 2.5, 20, 17.5))

    def test_svg_extent_takes_transforms_strokes_and_skips_what_does_not_draw(self):
        root = svg_root(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            '<defs><rect width="100" height="100"/></defs><path d="M0 0h100v100H0z" fill="none"/>'
            '<g transform="translate(10 0) scale(2)"><rect x="0" y="10" width="5" height="5"/></g>'
            '<line x1="40" y1="50" x2="60" y2="50" stroke="#000" stroke-width="4"/>'
            '<circle cx="50" cy="90" r="2" style="display:none"/></svg>'
        )
        self.assertEqual(build.svg_extent(root), (10, 20, 62, 52))
        self.assertIsNone(build.svg_extent(svg_root('<svg xmlns="http://www.w3.org/2000/svg"><text>a</text></svg>')))

    def test_fit_glyph_spans_the_glyph_size_of_what_it_draws(self):
        root = svg_root(
            '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect x="2" y="4" width="12" height="6"/></svg>'
        )
        markup = build.fit_glyph(root, build.GLYPH_SIZE)
        (a, b, k), (t1, t2, s) = (
            [float(v) for v in m]
            for m in re.findall(r"translate\(([-\d.]+) ([-\d.]+)\) scale\(([-\d.]+)\)", markup)[:2]
        )
        half = build.CANVAS * build.GLYPH_SIZE / 2
        self.assertAlmostEqual(k * (t1 + s * 2) + a, 32 - half, places=2)
        self.assertAlmostEqual(k * (t1 + s * 14) + a, 32 + half, places=2)
        self.assertAlmostEqual(k * (t2 + s * 7) + b, 32, places=2)

    def test_clean_symbolic_keeps_the_stylesheet_rules_and_drops_its_colors(self):
        root = build.clean_symbolic(
            svg_root(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><defs><style>.a{color:#f00}'
                ".r{fill:none;stroke:CurrentColor!important;stroke-width:2}.h{display:none}.g{fill:url(#g)}"
                '</style></defs><path class="a" style="fill:currentColor" d="M0 0h4v4z"/><rect fill="CurrentColor"/>'
                "</svg>"
            )
        )
        markup = ET.tostring(root, encoding="unicode")
        self.assertIn(
            ".a{color:#000}.r{fill:none;stroke:#000!important;stroke-width:2}.h{display:none}.g{fill:url(#g)}", markup
        )
        self.assertNotIn("urrent", markup)
        self.assertNotIn("#f00", markup)
        self.assertIn('style="fill:#000"', markup)
        self.assertIn('fill="#000"', markup)

    def test_a_full_bleed_solid_first_shape_is_the_plate(self):
        zen = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024" fill="none">'
            '<g clip-path="url(#c)"><path d="M0 0h1024v1024H0z" fill="none"/>'
            '<rect width="1024" height="1024" rx="185" fill="#202020"/>'
            '<circle cx="512" cy="512" r="134" stroke="#F2F0E3" stroke-width="30"/></g>'
            '<defs><clipPath id="c"><rect width="1024" height="1024" fill="white"/></clipPath></defs></svg>'
        )
        root = svg_root(zen)
        self.assertTrue(build.strip_svg_plate(root))
        markup = ET.tostring(root, encoding="unicode")
        self.assertNotIn("#202020", markup)
        self.assertIn("<ns0:circle", markup)
        self.assertIn('fill="white"', markup)
        for plate in (
            '<rect width="1024" height="1024" fill="url(#g)"/>',
            '<rect width="700" height="700" fill="#202020"/>',
            '<path d="M0 0h1024v1024H0z" fill="#202020"/>',
        ):
            root = svg_root(zen.replace('<rect width="1024" height="1024" rx="185" fill="#202020"/>', plate))
            self.assertFalse(build.strip_svg_plate(root), plate)

    def test_an_unpainted_first_shape_is_no_plate(self):
        for artboard in (
            '<rect width="64" height="64" fill="#fff" fill-opacity="0"/>',
            '<rect width="64" height="64" style="fill:#fff;fill-opacity:0"/>',
            '<rect width="64" height="64" fill="#fff" opacity="0%"/>',
            '<rect width="64" height="64" fill="transparent"/>',
        ):
            root = svg_root(
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">{artboard}'
                '<rect width="64" height="64" fill="#202020"/><circle cx="32" cy="32" r="9" fill="#fff"/></svg>'
            )
            self.assertTrue(build.strip_svg_plate(root), artboard)
            markup = ET.tostring(root, encoding="unicode")
            self.assertNotIn("#202020", markup, artboard)
            self.assertIn('width="64" height="64" ', markup, artboard)
            self.assertEqual(build.svg_extent(root), (23, 23, 41, 41), artboard)

    def test_a_plate_is_kept_when_removing_it_would_break_or_empty_the_icon(self):
        head = '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 64 64">'
        dot = '<circle cx="32" cy="32" r="9" fill="#fff"/>'
        for body in (
            # A shared plate in <defs>, drawn through <use> and also the shape of a mask.
            (
                '<defs><rect id="bg" width="64" height="64" fill="#202020"/><mask id="m"><use href="#bg"/></mask></defs>'
                f'<use xlink:href="#bg"/><g mask="url(#m)">{dot}</g>'
            ),
            # Drawn where it stands, and reused by a clip.
            f'<rect id="bg" width="64" height="64" fill="#202020"/><clipPath id="c"><use href="#bg"/></clipPath>{dot}',
            # The plate is all there is.
            (
                '<defs><linearGradient id="g"><stop stop-color="#fff"/></linearGradient></defs>'
                '<rect width="64" height="64" fill="#202020"/>'
            ),
            '<rect width="64" height="64" fill="#202020"/><circle cx="32" cy="32" r="9" fill="#fff" fill-opacity="0"/>',
        ):
            root = svg_root(head + body + "</svg>")
            before = ET.tostring(root)
            self.assertFalse(build.strip_svg_plate(root), body)
            self.assertEqual(ET.tostring(root), before, body)

    def test_paint_colors_follow_a_gradients_href_to_its_stops(self):
        root = svg_root(
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><defs>'
            '<linearGradient id="g1"><stop stop-color="#ff0000"/><stop offset="1" style="stop-color:#0000ff"/>'
            '</linearGradient><linearGradient id="g2" xlink:href="#g1" x2="1"/>'
            '<radialGradient id="g3" href="#g2"/><linearGradient id="own" href="#g1"><stop stop-color="#00ff00"/>'
            '</linearGradient><linearGradient id="a" href="#b"/><linearGradient id="b" href="#a"/></defs></svg>'
        )
        ids = {el.get("id"): el for el in root.iter() if el.get("id")}
        for paint in ("url(#g2)", "url(#g3)"):
            self.assertEqual(build._paint_colors(paint, ids), [(255, 0, 0), (0, 0, 255)], paint)
        self.assertEqual(build._paint_colors("url(#own)", ids), [(0, 255, 0)])
        self.assertEqual(build._paint_colors("url(#a)", ids), [])

    def test_svg_tint_keeps_the_larger_side_of_the_widest_tone_gap(self):
        def tint(body):
            mode, row = build.svg_tint(
                svg_root(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">{body}</svg>')
            )
            self.assertLessEqual(row[4], 0, body)

            def alpha(color):
                rgb = [c / 255 for c in build.parse_hex(color)]
                return round(max(0.0, min(1.0, sum(r * c for r, c in zip(row, rgb)) + row[3])), 6)

            return mode, alpha

        # kitty: dark art over a smaller light backing; the backing becomes holes.
        mode, alpha = tint(
            '<rect width="5" height="5" style="fill:#ddd"/><path d="M0 0h9v9H0z"/>'
            '<circle cx="5" cy="5" r="2" fill="#784421"/>'
        )
        self.assertEqual(mode, "luma-holes")
        self.assertEqual((alpha("#000000"), alpha("#784421"), alpha("#dddddd")), (1, 1, 0))
        # btop: a light striped mark over a smaller near-black shadow, from patterns; the shadow becomes holes.
        mode, alpha = tint(
            '<defs><pattern id="p"><rect fill="#f00"/><rect fill="#d70000"/><rect fill="#af0000"/>'
            '<rect fill="#870000"/><rect fill="#5f0000"/></pattern></defs>'
            '<path d="M3 3h6v6H3z" style="fill:#080808"/><path d="M1 1h7v7H1z" style="fill:url(#p)"/>'
        )
        self.assertEqual(mode, "depth")
        self.assertEqual((alpha("#080808"), alpha("#5f0000"), alpha("#ff0000")), (0, 1, 1))
        # A light shield over a faint black shadow: the shadow's opacity makes it the smaller side.
        mode, _ = tint('<path d="M0 0h10v10H0z" opacity="0.2"/><path d="M1 1h8v8H1z" fill="#ff9f05"/>')
        self.assertEqual(mode, "depth")
        mode, _ = tint('<path d="M0 0h9v9z" fill="#3050a0"/><circle r="2" fill="rgb(40,70,150)"/>')
        self.assertEqual(mode, "solid")
        # A gradient across the cut gets a steep ramp, not a fade.
        _, alpha = tint(
            '<linearGradient id="g"><stop stop-color="#000"/><stop offset="1" stop-color="#fff"/></linearGradient>'
            '<path d="M0 0h9v9H0z" fill="url(#g)"/><path d="M0 0h2v2H0z" fill="#fff"/>'
        )
        ramp = [alpha(build.to_hex((v, v, v))) for v in range(0, 256, 8)]
        self.assertLessEqual(sum(1 for a in ramp if 0 < a < 1), 2)

    def test_glyph_tints_never_lift_alpha_off_transparent_pixels(self):
        self.assertEqual(build.SOLID_ALPHA[4], 0)
        solid = build.glyph_wrap("<g/>", "#e5efff")
        self.assertIn('values="0 0 0 0 0.89804 0 0 0 0 0.93725 0 0 0 0 1 0 0 0 1 0"', solid)
        self.assertNotIn("feComposite", solid)
        steep = build.glyph_wrap("<g/>", "#e5efff", (3.0, 3.0, 3.0, -1.5, 0.0))
        self.assertIn('values="0 0 0 0 0.89804 0 0 0 0 0.93725 0 0 0 0 1 3 3 3 -1.5 0"', steep)
        self.assertIn('<feComposite in2="SourceGraphic" operator="in"/>', steep)
        for markup in (solid, steep):
            wrap(markup)

    def test_an_installed_theme_lists_its_application_dirs_symbolic_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Papirus"
            root.mkdir()
            (root / "index.theme").write_text(
                "[Icon Theme]\nName=P\nDirectories=16x16/apps,64x64/apps,16x16/symbolic/apps,22x22/symbolic/apps,"
                "16x16/places,scalable/apps,../up/apps\n"
                "[16x16/apps]\nSize=16\nContext=Applications\n[64x64/apps]\nSize=64\nContext=Applications\n"
                "[16x16/symbolic/apps]\nSize=16\nContext=Applications\n[22x22/symbolic/apps]\nSize=22\n"
                "[16x16/places]\nSize=16\nContext=Places\n[scalable/apps]\nSize=64\nType=Scalable\n"
            )
            self.assertEqual(
                [str(p.relative_to(root)) for p in build.Glyphs.theme_app_dirs(root)],
                ["22x22/symbolic/apps", "16x16/symbolic/apps", "scalable/apps", "64x64/apps", "16x16/apps"],
            )
            self.assertEqual(build.Glyphs.theme_app_dirs(Path(tmp) / "missing"), [])

    def test_a_cutout_is_a_mask_over_a_glyph_colored_rect(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cut.png"
            path.write_bytes(build.encode_png(2, 2, b"\xff\xff\xff\xff" * 4))
            markup = build.cutout_markup(str(path), build.GLYPH_SIZE, "#e5efff")
            self.assertIn('<mask id="dx-cut" maskUnits="userSpaceOnUse"', markup)
            self.assertIn('xlink:href="data:image/png;base64,', markup)
            self.assertIn('fill="#e5efff" mask="url(#dx-cut)"/>', markup)
            side = build.CANVAS * build.GLYPH_SIZE / (1 - 2 * build.CUTOUT_MARGIN)
            self.assertIn(f'width="{build.fmt(side)}"', markup)
            for bad in (Path(tmp) / "missing.png", "relative.png"):
                with self.assertRaises(build.Skip):
                    build.cutout_markup(str(bad), build.GLYPH_SIZE, "#e5efff")
            (Path(tmp) / "text.png").write_text("not a png")
            with self.assertRaises(build.Skip):
                build.cutout_markup(str(Path(tmp) / "text.png"), build.GLYPH_SIZE, "#e5efff")


class AppStyleTests(BuildCase):
    """--app-style: color as before; themed and monochrome through the fallback chain."""

    SYMBOLIC = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><style>.x{color:#f00}</style>'
        '<path class="x" style="fill:currentColor" d="M2 2h12v12H2z"/></svg>'
    )
    PLATED = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" '
        'fill="#202020"/><circle cx="32" cy="32" r="16" fill="none" stroke="#f2f0e3" stroke-width="4"/></svg>'
    )
    COLORS = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect x="8" y="8" width="48" height="48" fill="#202020"/>{}</svg>'

    def install(self, name, data, folder="scalable/apps", ext="svg", entry=True):
        """Icon file `name` in hicolor and, with `entry`, an app whose Icon= is `name`."""
        icons = self.data_home / "icons/hicolor" / folder
        icons.mkdir(parents=True, exist_ok=True)
        (icons / f"{name}.{ext}").write_bytes(data.encode() if isinstance(data, str) else data)
        if entry:
            apps = self.data_home / "applications"
            apps.mkdir(exist_ok=True)
            (apps / f"{name}.desktop").write_text(f"[Desktop Entry]\nType=Application\nName={name}\nIcon={name}\n")

    def png(self, name):
        return build.encode_png(2, 2, b"\x20\x40\x60\xff" * 4)

    def test_help_lists_the_options_dms_probes_for(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            build.parse_args(["--help"])
        text = out.getvalue()
        for option in ("--app-style {color,tinted,themed,monochrome}", "--tiles", "--files-style {standard,color}"):
            self.assertIn(option, text)
        self.assertNotIn("--mono", text)

    def test_the_style_comes_from_the_flag_its_aliases_or_the_recipe(self):
        for args, style in (
            ((), "color"),
            (("--app-style", "monochrome"), "monochrome"),
            (("--mono",), "tinted"),
            (("--no-mono",), "color"),
            (("--mono", "--app-style", "monochrome"), "monochrome"),
        ):
            self.assertIn(f" apps={style} ", self.built(*args), args)
        src = self.tmp / "src"
        shutil.copytree(FIXTURES / "src", src)
        recipe = json.loads((src / "recipe.json").read_text())
        for values, args, style in (
            ({"mono": True}, (), "tinted"),
            ({"mono": True, "appStyle": "monochrome"}, (), "monochrome"),
            ({"appStyle": "themed"}, ("--no-mono",), "color"),
        ):
            (src / "recipe.json").write_text(json.dumps({**recipe, **values}))
            self.assertIn(f" apps={style} ", self.built(*args, src=src), values)
        (src / "recipe.json").write_text(json.dumps({**recipe, "appStyle": "mono"}))
        code, _, stderr = self.build(src=src)
        self.assertEqual(code, 1)
        self.assertIn("recipe 'appStyle' must be one of color, tinted, themed, monochrome: 'mono'", stderr)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build.parse_args(["--app-style", "mono"])

    def test_color_is_the_same_build_by_every_name(self):
        self.built()
        default = self.snapshot(self.out)
        for args in (("--app-style", "color"), ("--no-mono",)):
            self.built(*args)
            self.assertEqual(self.snapshot(self.out), default, args)

    def test_base_is_no_longer_a_files_style(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build.parse_args(["--files-style", "base"])
        src = self.tmp / "src"
        shutil.copytree(FIXTURES / "src", src)
        recipe = json.loads((src / "recipe.json").read_text())
        (src / "recipe.json").write_text(json.dumps({**recipe, "filesStyle": "base"}))
        code, _, stderr = self.build(src=src)
        self.assertEqual(code, 1)
        self.assertIn("recipe 'filesStyle' must be one of standard, color: 'base'", stderr)

    def chain_src(self):
        src = self.tmp / "src"
        shutil.copytree(FIXTURES / "src", src)
        (src / "mono/apps").mkdir(parents=True)
        (src / "mono/apps/curio.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="192" height="192" fill="none" stroke="#000" '
            'stroke-width="12"><path d="M40 40h112v112H40z"/></svg>'
        )
        (src / "mono/aliases.json").write_text(json.dumps({"apps": {"curio": ["Curio-App"]}}))
        (src / "art/symbolic").mkdir(parents=True)
        (src / "art/symbolic/dev.duxlabs.files-symbolic.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M1 2h14v12H1z"/></svg>'
        )
        (src / "own/apps").mkdir()
        (src / "own/apps/dev.duxlabs.shell.qr.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path fill="@glyph" d="M2 2h20v20H2z"/></svg>'
        )
        return src

    def install_chain(self):
        cut = self.tmp / "cut.png"
        cut.write_bytes(build.encode_png(2, 2, b"\xff\xff\xff\xff" * 4))
        self.install("dev.duxlabs.files", PerAppTests.OURS)
        # Found by the alias its glyph lists.
        self.install("Curio-App", self.png("curio"), "64x64/apps", "png")
        self.install("symby", self.COLORS.format(""))
        self.install("symby-symbolic", self.SYMBOLIC, "symbolic/apps", entry=False)
        self.install("plated", self.PLATED)
        # Installed themes: read in name order, never our own packs, DMS overlays or anything we published.
        icons = self.data_home / "icons"
        for theme, name, mark in (
            ("aaa-dms-1", "themy", "M0 0h1v1z"),
            ("Aardvark", "themy", "M0 0h2v2z"),
            ("Beta", "themy", "M0 0h3v3z"),
            ("Zeta", "themy", "M0 0h4v4z"),
            ("dux-icons", "onlyours", "M0 0h5v5z"),
        ):
            folder = icons / theme / "16x16/symbolic/apps"
            folder.mkdir(parents=True)
            (icons / theme / "index.theme").write_text(
                "[Icon Theme]\nName=T\nDirectories=16x16/symbolic/apps\n"
                "[16x16/symbolic/apps]\nSize=16\nContext=Applications\n"
            )
            (folder / f"{name}-symbolic.svg").write_text(
                f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="{mark}"/></svg>'
            )
        (icons / "Aardvark" / build.MARKER).write_text(build.MARKER_TEXT)
        self.install("themy", self.png("themy"), "64x64/apps", "png")
        self.install("onlyours", self.png("onlyours"), "64x64/apps", "png")
        self.install("cutty", self.png("cutty"), "64x64/apps", "png")
        self.install("cutless", self.png("cutless"), "64x64/apps", "png")
        self.install(
            "holes",
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="6" height="6" fill="#ddd"/><path d="M2 2h7v7z"/></svg>',
        )
        self.install(
            "deep",
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path d="M3 3h6v6H3z" fill="#080808"/><path d="M1 1h7v7H1z" fill="#d70000"/></svg>',
        )
        tiles = {
            "apps": {
                name: {"tile": "#1f578a", "glyph": "#e5efff", "back": "#0a4170", **extra}
                for name, extra in (
                    ("Curio-App", {}),
                    ("symby", {"cutout": str(cut)}),
                    ("cutty", {"cutout": str(cut)}),
                    ("cutless", {"cutout": str(self.tmp / "gone.png")}),
                    ("raster", {"unknown": 1}),
                    ("plated", {}),
                    ("holes", {}),
                    ("deep", {}),
                    ("themy", {}),
                )
            },
            "shell": {"tile": "#1f578a", "glyph": "#e5efff"},
            "dux": {"tile": "#1f578a", "glyph": "#e5efff", "tint": "#e5efff"},
        }
        path = self.tmp / "tiles.json"
        path.write_text(json.dumps(tiles))
        return str(path)

    def test_each_app_takes_the_first_stage_of_the_chain_that_draws_it(self):
        src, tiles = self.chain_src(), self.install_chain()
        for style in ("themed", "monochrome"):
            stdout = self.built("--app-style", style, "--tiles", tiles, "-v", src=src)
            self.assertIn(
                "app glyphs: 2 own, 1 curated, 1 symbolic, 1 theme-symbolic, 3 plate, 1 cutout, 1 luma-holes, 2 depth, 2 solid, "
                "5 duotone",
                stdout,
            )
            for line in (
                "glyph [own] 2: dev.duxlabs.files, dev.duxlabs.shell.qr",
                "glyph [curated] 1: Curio-App",
                "glyph [symbolic] 1: symby",
                "glyph [theme-symbolic] 1: themy",
                "glyph [plate] 3: plated, slice, styled",
                "glyph [cutout] 1: cutty",
                "glyph [luma-holes] 1: holes",
                "glyph [depth] 2: alpha, deep",
                "glyph [duotone] 5: cutless, onlyours, photo, pix, raster",
            ):
                self.assertIn(line, stdout)
        glyph = "0 0 0 0 0.89804 0 0 0 0 0.93725 0 0 0 0 1"
        for name in ("Curio-App", "symby", "themy", "plated", "holes", "deep", "dev.duxlabs.files"):
            text = self.icon(f"apps/{name}.svg")
            ET.fromstring(text)
            self.assertIn(f'values="{glyph} ', text, name)
            self.assertIn('fill="#1f578a"', text, name)
            # Solid glyphs keep their alpha; the SVG tints cut theirs from the art's tones.
            self.assertEqual("feComposite" in text, name in ("holes", "deep"), name)
        # The first installed theme by name that is not ours.
        self.assertIn('d="M0 0h3v3z"', self.icon("apps/themy.svg"))
        self.assertNotIn("#202020", self.icon("apps/plated.svg"))
        self.assertNotIn("currentColor", self.icon("apps/symby.svg"))
        self.assertIn('fill="#e5efff" mask="url(#dx-cut)"', self.icon("apps/cutty.svg"))
        self.assertIn(build.mono_matrix("#0a4170", "#e5efff"), self.icon("apps/cutless.svg"))
        # Bare copies draw the same glyph in the theme's tile color, no tile.
        bare = (self.out / "bare/apps/symby.svg").read_text()
        self.assertIn('values="0 0 0 0 0.40392 0 0 0 0 0.31373 0 0 0 0 0.64314 0 0 0 1 0"', bare)
        self.assertNotIn("<circle", bare)
        # Our app icons take the glyph size like every other glyph.
        self.assertIn("translate(9.728 9.728) scale(0.696)", self.icon("apps/dev.duxlabs.shell.qr.svg"))

    @staticmethod
    def glyph_rows(color):
        return " ".join(f"0 0 0 0 {build.fmt(c / 255, 5)}" for c in build.parse_hex(color))

    def test_bare_copies_take_the_tiles_bare_color(self):
        src, path = self.chain_src(), self.install_chain()
        tiles = json.loads(Path(path).read_text())
        for name in ("symby", "cutty", "cutless"):
            tiles["apps"][name]["bare"] = "#ff8800"
        tiles["shell"]["bare"] = "#00ff88"
        tiles["dux"]["bare"] = "#8800ff"
        Path(path).write_text(json.dumps(tiles))
        palette = ("--tile", "#9ecaff", "--glyph", "#003258")
        for style in ("themed", "monochrome"):
            self.built("--app-style", style, "--tiles", path, *palette, src=src)

            def bare(name):
                return (self.out / f"bare/apps/{name}.svg").read_text()

            self.assertIn(f'values="{self.glyph_rows("#ff8800")} ', bare("symby"))
            self.assertIn('fill="#ff8800" mask="url(#dx-cut)"', bare("cutty"))
            self.assertIn(f'values="{build.mono_matrix("#ff8800", "#003258")}"', bare("cutless"))
            self.assertIn(f'values="{self.glyph_rows("#8800ff")} ', bare("dev.duxlabs.files"))
            self.assertIn('fill="#00ff88"', bare("dev.duxlabs.shell.qr"))
            # Without a bare color, the theme tile color.
            self.assertIn(f'values="{self.glyph_rows("#9ecaff")} ', bare("holes"))
            self.assertIn(f'values="{build.mono_matrix("#9ecaff", "#003258")}"', bare("raster"))
            # The tiled icons keep their glyph color.
            self.assertIn(f'values="{self.glyph_rows("#e5efff")} ', self.icon("apps/symby.svg"))
        Path(path).write_text(json.dumps({"apps": {"symby": {"tile": "#1f578a", "bare": "orange"}}}))
        code, _, stderr = self.build("--app-style", "themed", "--tiles", path, src=src)
        self.assertEqual(code, 1)
        self.assertIn("apps.symby.bare", stderr)

    def test_glyph_styles_put_apps_the_tiles_leave_out_on_the_uniform_tile(self):
        tiles = self.tmp / "tiles.json"
        tiles.write_text(json.dumps({"apps": {}, "shell": {"tile": "#1f578a", "glyph": "#e5efff", "bare": "#ff8800"}}))
        palette = ("--tile", "#9ecaff", "--glyph", "#003258")
        for style in ("themed", "monochrome"):
            self.built("--app-style", style, "--tiles", str(tiles), *palette)
            text = self.icon("apps/alpha.svg")
            self.assertIn('fill="#1f578a"', text, style)
            self.assertNotIn('fill="#9ecaff"', text, style)
            self.assertIn(f'values="{self.glyph_rows("#e5efff")} ', text, style)
            bare = (self.out / "bare/apps/alpha.svg").read_text()
            self.assertIn(f'values="{self.glyph_rows("#ff8800")} ', bare, style)
        # The other styles keep an app's own tile, else the theme's.
        for style in ("color", "tinted"):
            self.built("--app-style", style, "--tiles", str(tiles), *palette)
            self.assertNotIn('fill="#1f578a"', self.icon("apps/alpha.svg"), style)

    @unittest.skipUnless(HAVE_RSVG, "needs librsvg and pycairo")
    def test_a_symbolic_glyph_keeps_its_stylesheet_rules(self):
        self.install("ringy", self.COLORS.format(""))
        self.install(
            "ringy-symbolic",
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><style>.h{display:none}'
            ".r{fill:none;stroke:currentColor;stroke-width:2}</style>"
            '<rect class="h" width="16" height="16"/><circle class="r" cx="8" cy="8" r="6"/></svg>',
            "symbolic/apps",
            entry=False,
        )
        tiles = self.tmp / "tiles.json"
        tiles.write_text(json.dumps({"apps": {"ringy": {"tile": "#1f578a", "glyph": "#e5efff"}}}))
        stdout = self.built("--app-style", "themed", "--tiles", str(tiles), "-v")
        self.assertIn("glyph [symbolic] 1: ringy", stdout)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 64, 64)
        handle = Rsvg.Handle.new_from_file(str(self.out / "scalable/apps/ringy.svg"))
        rect = Rsvg.Rectangle()
        rect.x, rect.y, rect.width, rect.height = 0, 0, 64, 64
        handle.render_document(cairo.Context(surface), rect)
        data, stride = surface.get_data(), surface.get_stride()

        def pixel(x, y):
            blue, green, red, alpha = data[y * stride + x * 4 : y * stride + x * 4 + 4]
            return red, green, blue, alpha

        # The ring only: its hollow middle and the hidden square around it show the tile.
        for x, y, color in ((32, 32, "#1f578a"), (15, 15, "#1f578a"), (32, 18, "#e5efff")):
            for got, want in zip(pixel(x, y), build.parse_hex(color) + (255,)):
                self.assertAlmostEqual(got, want, delta=3, msg=(x, y))

    def test_color_ignores_the_chain_and_cutouts(self):
        src, tiles = self.chain_src(), self.install_chain()
        self.built("--app-style", "themed", "--tiles", tiles, src=src)
        self.assertIn("dx-cut", self.icon("apps/cutty.svg"))
        stdout = self.built("--app-style", "color", "--tiles", tiles, "-v", src=src)
        self.assertNotIn("app glyphs", stdout)
        self.assertNotIn("dx-glyph", self.icon("apps/symby.svg"))
        self.assertNotIn("dx-cut", self.icon("apps/cutty.svg"))

    def test_dump_sources_takes_the_style_options(self):
        code, stdout, stderr = self.build("--dump-sources", "--app-style", "themed", "--files-style", "standard")
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn('"alpha"', stdout)

    @unittest.skipUnless(HAVE_RSVG, "needs librsvg and pycairo")
    def test_a_symbolic_glyph_renders_in_the_glyph_color_on_its_tile(self):
        src, tiles = self.chain_src(), self.install_chain()
        self.built("--app-style", "themed", "--tiles", tiles, src=src)
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 64, 64)
        handle = Rsvg.Handle.new_from_file(str(self.out / "scalable/apps/symby.svg"))
        rect = Rsvg.Rectangle()
        rect.x, rect.y, rect.width, rect.height = 0, 0, 64, 64
        handle.render_document(cairo.Context(surface), rect)
        data, stride = surface.get_data(), surface.get_stride()

        def pixel(x, y):
            blue, green, red, alpha = data[y * stride + x * 4 : y * stride + x * 4 + 4]
            return red, green, blue, alpha

        # The 12-unit square spans 58% of the canvas: 13.4 to 50.6.
        for x, y, color in ((32, 32, "#e5efff"), (15, 15, "#e5efff"), (11, 32, "#1f578a"), (53, 32, "#1f578a")):
            for got, want in zip(pixel(x, y), build.parse_hex(color) + (255,)):
                self.assertAlmostEqual(got, want, delta=3, msg=(x, y))


class SourceTreeTests(BuildCase):
    """The real recipe and own icons of this checkout."""

    def own(self):
        folder = build.SRC / "own"
        return [p.relative_to(folder).with_suffix("").as_posix() for p in sorted(folder.glob("*/*.svg"))]

    def test_recipe_template_and_own_icons_build(self):
        stdout = self.built(src=build.SRC)
        self.assertIn(f"own icons: {len(self.own())} built", stdout)
        self.assertNotIn("skipped own", stdout)
        self.assertIn("places/folder", self.own())
        for rel in self.own():
            text = self.icon(f"{rel}.svg")
            self.assertEqual(ET.fromstring(text).get("viewBox"), "0 0 64 64", rel)
            self.assertNotRegex(text, r"@[a-z]", rel)

    def test_every_shape_loads_flat_and_shaded(self):
        recipe = build.load_json(build.SRC / "recipe.json")
        names = build.shapes(build.SRC)
        self.assertIn(recipe["template"], names)
        self.assertLessEqual(set(recipe["shapes"]), set(names))
        palette = {"tile": "#6750a4", "glyph": "#ffffff", "back": "#473484"}
        for name in names:
            for gradient in (False, True):
                root = ET.fromstring(build.load_template(build.SRC, name, palette, gradient).replace("@content", ""))
                self.assertEqual(root.get("viewBox"), "0 0 64 64", name)

    def test_every_shapes_fit_clears_its_ring_by_the_reach_margin(self):
        # Each slot's circle stays REACH_MARGIN inside a ring, as reach_scale keeps grown content. The clover's
        # waists are its inset where the slot's edges come closest to the center.
        recipe = build.load_json(build.SRC / "recipe.json")
        for name in build.shapes(build.SRC):
            _, _, fitted = build.load_recipe(build.SRC, name)
            room = build.INSET_RADII[name] - build.RING_WIDTH - build.REACH_MARGIN
            for own in (False, True):
                half = build.layout_for("apps", "app", fitted, own).size / 2
                self.assertLessEqual(half, room, (name, own))
        self.assertIn("clover", recipe["shapes"])

    def test_no_alias_repeats_an_own_icon(self):
        code, _, stderr = self.build(src=build.SRC)
        self.assertEqual((code, stderr), (0, ""))

    def test_the_folder_family_shares_one_placement(self):
        self.built(src=build.SRC)
        family = [
            rel
            for rel in self.own()
            if re.fullmatch(r"places/(folder(-.+)?|user-home|user-desktop)|status/folder-open", rel)
        ]
        self.assertIn("places/folder", family)
        placements = {}
        for rel in family:
            placements[rel] = re.search(r'<g id="dx-content" transform="([^"]+)"', self.icon(f"{rel}.svg")).group(1)
        self.assertEqual(set(placements.values()), {placements["places/folder"]}, placements)

    def test_color_style_redraws_files_and_folders_and_keeps_apps_on_the_tile(self):
        stdout = self.built("--files-style", "color", "--shape", "circle", src=build.SRC)
        self.assertNotIn("skipped own", stdout)
        colored = [rel for rel in self.own() if rel.split("/")[0] in build.COLOR_CONTEXTS]
        self.assertIn(f"({len(colored)} in color)", stdout)
        for rel in self.own():
            text = self.icon(f"{rel}.svg")
            self.assertNotRegex(text, r"@[a-z]", rel)
            self.assertNotIn("clip-path", text, rel)
            ET.fromstring(text)
            if rel in colored:
                self.assertNotIn("<circle", text, rel)
                scale = float(re.search(r'<g id="dx-content" transform="[^"]*scale\(([\d.]+)\)', text).group(1))
                # Folders are drawn on the canvas, the rest is the 24-unit art filling it.
                if "folder" in build.color_rule(rel):
                    self.assertEqual(scale, 1.0, rel)
                else:
                    self.assertGreater(scale * 24, 64, rel)
                if rel != "places/start-here":
                    self.assertIn('maskUnits="userSpaceOnUse"', text, rel)
            else:
                self.assertIn('<circle cx="32" cy="32" r="32"', text, rel)

    def test_color_style_ids_are_prefixed_unique_and_resolve(self):
        # Inlined ids carry the prefix and are defined once; each reference finds its target in the same icon.
        self.built("--files-style", "color", "--tile", "#69b2ff", src=build.SRC)
        for rel in self.own():
            if rel.split("/")[0] not in build.COLOR_CONTEXTS:
                continue
            text = self.icon(f"{rel}.svg")
            ids = re.findall(r'\sid="([^"]+)"', text)
            self.assertEqual(len(ids), len(set(ids)), rel)
            self.assertTrue(all(i == "dx-content" or i.startswith("c-") for i in ids), (rel, ids))
            self.assertLessEqual(set(re.findall(r"url\(#([^)]+)\)", text)), set(ids), rel)
            self.assertNotIn("clip-path", text, rel)
            if "folder" in build.color_rule(rel):
                self.assertIn('<mask id="c-m-front" maskUnits="userSpaceOnUse"', text, rel)
            elif rel != "places/start-here":  # solid only: too small for a rim
                self.assertRegex(text, r'<mask id="c-rim\d+" maskUnits="userSpaceOnUse"', rel)

    def test_color_style_folders_take_the_layered_template_and_other_places_their_art(self):
        self.built("--files-style", "color", "--tile", "#9F7AEA", src=build.SRC)
        family = [
            rel
            for rel in self.own()
            if re.fullmatch(r"places/(folder(-.+)?|user-home|user-desktop)|status/folder-open", rel)
        ]
        self.assertEqual(len(family), 13)
        for rel in family:
            text = self.icon(f"{rel}.svg")
            kind = "open" if rel == "status/folder-open" else "closed"
            self.assertEqual(build.color_rule(rel), {"folder": kind}, rel)
            self.assertIn(f'<path d="{build.FOLDERS[kind]["front"]}" fill="url(#c-front)"', text, rel)
            self.assertIn(f'<path d="{build.FOLDERS[kind]["back"]}" fill="url(#c-back)"', text, rel)
            self.assertIn('fill="url(#c-paper)"', text, rel)
            # Type marks only where the art has them, and only on a closed front.
            self.assertEqual("scale(2.65)" in text, rel not in ("places/folder", "status/folder-open"), rel)
        # The open folder: the leaning front, the dark cavity and a second sheet behind the first.
        opened = self.icon("status/folder-open.svg")
        self.assertIn(build.FOLDER_OPEN_FRONT, opened)
        for gradient in ("c-cavity", "c-paper2"):
            self.assertIn(f'fill="url(#{gradient})"', opened)
        self.assertNotIn(build.FOLDER_FRONT, opened)
        self.assertNotIn("c-cavity", self.icon("places/folder.svg"))
        # Every other place keeps its own art and rule.
        for rel in self.own():
            if rel.startswith("places/") and rel not in family:
                text = self.icon(f"{rel}.svg")
                self.assertNotIn("folder", build.color_rule(rel), rel)
                self.assertNotIn(build.FOLDER_BACK, text, rel)
                self.assertNotIn("c-m-front", text, rel)
                if rel != "places/start-here":  # solid only
                    self.assertIn('fill="url(#c-body)"', text, rel)

    def test_color_style_marks_stay_inside_the_rim(self):
        self.built("--files-style", "color", src=build.SRC)
        for rel in ("mimetypes/package-x-generic", "mimetypes/text-x-generic"):
            text = self.icon(f"{rel}.svg")
            body = re.search(r'<mask id="c-rim0"[^>]*><path fill="#fff" d="([^"]+)"/></mask>', text).group(1)
            rim = re.search(r'stroke-width="([\d.]+)" stroke-linejoin="round" mask="url\(#c-rim0\)"', text).group(1)
            self.assertIn(
                f'<path fill="#fff" d="{body}" stroke="#000" stroke-width="{rim}" stroke-linejoin="round"/></mask>'
                '<g mask="url(#c-marks)">',
                text,
                rel,
            )

    def test_disc_images_and_unknown_files_have_own_icons(self):
        self.assertIn("mimetypes/media-optical", self.own())
        self.built(src=build.SRC)
        mimetypes = self.out / "scalable" / "mimetypes"
        for name in (
            "application-x-cd-image",
            "application-vnd.efi.iso",
            "application-x-iso9660-image",
            "application-x-compressed-iso",
            "application-x-raw-disk-image",
        ):
            self.assertEqual(os.readlink(mimetypes / f"{name}.svg"), "media-optical.svg", name)
        for name in ("application-x-generic", "unknown", "application-octet-stream"):
            self.assertEqual(os.readlink(mimetypes / f"{name}.svg"), "text-x-generic.svg", name)
        self.assertIn("a4.75 4.75 0 1 1", self.icon("mimetypes/media-optical.svg"))

    def test_office_files_and_pdfs_take_their_own_icons_before_any_other_theme(self):
        # Named outright, so a hicolor icon an office suite installs under the specific name never wins over ours.
        self.built(src=build.SRC)
        mimetypes = self.out / "scalable" / "mimetypes"
        expected = {
            "x-office-document": (
                "application-msword",
                "application-rtf",
                "application-vnd.oasis.opendocument.text",
                "application-vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            "x-office-spreadsheet": (
                "application-vnd.ms-excel",
                "application-vnd.oasis.opendocument.spreadsheet",
                "application-vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "text-csv",
            ),
            "x-office-presentation": (
                "application-vnd.ms-powerpoint",
                "application-vnd.oasis.opendocument.presentation",
                "application-vnd.openxmlformats-officedocument.presentationml.presentation",
            ),
            "application-pdf": ("application-x-bzpdf", "application-x-gzpdf", "application-x-xzpdf"),
        }
        for icon, names in expected.items():
            self.assertTrue((mimetypes / f"{icon}.svg").is_file(), icon)
            for name in names:
                self.assertEqual(os.readlink(mimetypes / f"{name}.svg"), f"{icon}.svg", name)
        self.assertEqual(build.file_category("application-pdf"), "pdf")

    def test_every_own_mimetype_has_a_category_or_is_text(self):
        names = {rel.split("/")[1] for rel in self.own() if rel.startswith("mimetypes/")}
        listed = [icon for category in build.FILE_CATEGORIES.values() for icon in category["icons"]]
        self.assertEqual(len(listed), len(set(listed)))
        self.assertLessEqual(set(listed), names)
        self.assertEqual(build.file_category("media-optical"), "optical")

    def test_art_overrides_are_buildable_svg(self):
        for path in sorted((build.SRC / build.ART_DIR).glob("*")):
            self.assertEqual(path.suffix, ".svg", path)
            self.assertRegex(path.stem, build.NAME_RE, path)
            kind, _ = build.vendor_content(path, build.Slot(0.0, (0.0, 0.0)))
            self.assertEqual(kind, "svg", path)

    def test_curated_glyphs_are_our_own_drawings_and_resolve(self):
        folder = build.SRC / build.MONO_DIR
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["com.mitchellh.ghostty.svg"])
        lookup = build.Glyphs(build.SRC, build.Dirs({"HOME": str(self.home), "XDG_DATA_DIRS": "/nonexistent"}))
        for name in ("com.mitchellh.ghostty", "Ghostty"):
            self.assertEqual(lookup.curated(["nothing", name]), folder / "com.mitchellh.ghostty.svg", name)
        self.assertIsNone(lookup.curated(["google-chrome", "discord"]))

    def test_our_apps_have_solid_symbolic_glyphs(self):
        art = sorted(p.stem for p in (build.SRC / build.ART_DIR).glob("dev.duxlabs.*.svg"))
        symbolic = build.SRC / build.OWN_SYMBOLIC_DIR
        self.assertEqual(sorted(p.name for p in symbolic.iterdir()), [f"{name}-symbolic.svg" for name in art])
        lookup = build.Glyphs(build.SRC, build.Dirs({"HOME": str(self.home), "XDG_DATA_DIRS": "/nonexistent"}))
        for name in art:
            path = lookup.own(name)
            text = path.read_text()
            # One flat color: no gradients, opacity or color of their own, for the solid tint to paint.
            for word in ("Gradient", "opacity", 'fill="#', "stroke", "<style"):
                self.assertNotIn(word, text, path.name)
            root = build.parse_svg(path.read_bytes())
            self.assertIsNotNone(build.svg_extent(root), path.name)
            wrap(build.glyph_wrap(build.fit_glyph(root, build.GLYPH_SIZE), "#ffffff"))

    def test_index_template_names_the_product(self):
        template = build.INDEX_TEMPLATE.read_text()
        self.assertEqual(build.index_name(template), "Dux Icons")
        self.assertIn("Comment=Material icons for Dux Modular Shell\n", template)


class LookupTests(BuildCase):
    """--fingerprint, and the vendor icon lookups a run reuses from the one before."""

    ART = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" fill="#c33"/></svg>'

    def fingerprint(self, src=None):
        code, stdout, stderr = self.build("--fingerprint", src=src)
        self.assertEqual((code, stderr), (0, ""))
        self.assertRegex(stdout, r"\A[0-9a-f]{64}\n\Z")
        self.assertFalse(self.out.exists(), "a fingerprint builds nothing")
        return stdout.strip()

    def install(self, name, art=ART, icon=None):
        icons = self.data_home / "icons/hicolor/scalable/apps"
        icons.mkdir(parents=True, exist_ok=True)
        (icons / f"{name}.svg").write_text(art)
        apps = self.data_home / "applications"
        apps.mkdir(exist_ok=True)
        (apps / f"{name}.desktop").write_text(f"[Desktop Entry]\nType=Application\nName={name}\nIcon={icon or name}\n")

    def cache(self):
        return Path(self.env["XDG_CACHE_HOME"]) / build.LOOKUPS_FILE

    def resolving(self):
        """Counts the lookups a run makes past the cache."""
        return mock.patch.object(build.VendorIcons, "_resolve", autospec=True, side_effect=build.VendorIcons._resolve)

    def test_fingerprint_is_stable_while_nothing_changes(self):
        first = self.fingerprint()
        self.assertEqual(first, self.fingerprint())
        self.built(out=self.data_home / "icons/dux-icons-dms-0123abcd")
        self.assertEqual(first, self.fingerprint(), "publishing a theme next to the icons changes nothing")

    def test_a_build_and_the_source_listing_report_the_fingerprint(self):
        self.install("newapp")
        digest = self.fingerprint()
        self.assertTrue(self.built().endswith(f"\n  fingerprint: {digest}\n"))
        code, stdout, _ = self.build("--dump-sources")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["fingerprint"], digest)
        self.assertIn("newapp", json.loads(stdout)["apps"])

    def test_fingerprint_follows_installed_apps_and_their_icons(self):
        seen = {self.fingerprint()}
        self.install("newapp")
        seen.add(self.fingerprint())
        # Same size, rewritten in place: only the file's own state tells.
        icon = self.data_home / "icons/hicolor/scalable/apps/newapp.svg"
        icon.write_text(self.ART.replace("#c33", "#3c3"))
        seen.add(self.fingerprint())
        desktop = self.data_home / "applications/newapp.desktop"
        desktop.write_text(desktop.read_text().replace("Icon=newapp", "Icon=alpha"))
        seen.add(self.fingerprint())
        desktop.unlink()
        seen.add(self.fingerprint())
        self.assertEqual(len(seen), 5)

    def test_fingerprint_follows_icon_dirs_and_the_generator(self):
        src = self.tmp / "src"
        shutil.copytree(FIXTURES / "src", src)
        seen = {self.fingerprint(src)}
        # A new hicolor size can hold a better icon for an app that already has one.
        (self.data_home / "icons/hicolor/128x128/apps").mkdir(parents=True)
        seen.add(self.fingerprint(src))
        (self.data_home / "icons/hicolor/128x128/apps/alpha.png").write_bytes(b"")
        seen.add(self.fingerprint(src))
        (self.data_home / "icons/loose.png").write_bytes(b"")
        seen.add(self.fingerprint(src))
        template = src / "templates/circle.svg"
        template.write_text(template.read_text() + "\n")
        seen.add(self.fingerprint(src))
        (src / "art/apps").mkdir(parents=True, exist_ok=True)
        (src / "art/apps/alpha.svg").write_text(self.ART)
        seen.add(self.fingerprint(src))
        self.assertEqual(len(seen), 6)

    def test_fingerprint_follows_symbolic_icons_of_installed_themes(self):
        seen = {self.fingerprint()}
        theme = self.data_home / "icons/Papyrus"
        (theme / "symbolic/apps").mkdir(parents=True)
        (theme / "index.theme").write_text(
            "[Icon Theme]\nName=Papyrus\nDirectories=symbolic/apps\n\n[symbolic/apps]\nContext=Applications\nSize=16\nType=Scalable\n"
        )
        seen.add(self.fingerprint())
        (theme / "symbolic/apps/newapp-symbolic.svg").write_text(self.ART)
        seen.add(self.fingerprint())
        self.assertEqual(len(seen), 3)
        # Our own packs are never read for glyphs, so building one changes nothing.
        own = self.data_home / "icons/dux-icons-dms-0123abcd/symbolic/apps"
        own.mkdir(parents=True)
        (own.parent.parent / "index.theme").write_text("[Icon Theme]\nName=Dux Icons\nDirectories=symbolic/apps\n")
        (own / "newapp-symbolic.svg").write_text(self.ART)
        self.assertIn(self.fingerprint(), seen)

    def test_fingerprint_follows_symbolic_icons_rewritten_in_place(self):
        self.install("newapp")
        own = self.data_home / "icons/hicolor/symbolic/apps/newapp-symbolic.svg"
        own.parent.mkdir(parents=True)
        own.write_text(self.ART)
        theme = self.data_home / "icons/Papyrus"
        (theme / "symbolic/apps").mkdir(parents=True)
        (theme / "index.theme").write_text(
            "[Icon Theme]\nName=Papyrus\nDirectories=symbolic/apps\n\n[symbolic/apps]\nContext=Applications\nSize=16\n"
        )
        # A theme linked in from a store, its current generation a link of its own.
        store = self.tmp / "store"
        for generation in ("1", "2"):
            (store / generation).mkdir(parents=True)
            (store / generation / "newapp-symbolic.svg").write_text(self.ART.replace("#c33", f"#{generation * 3}"))
        (store / "current").symlink_to(store / "1")
        (theme / "symbolic/apps/newapp-symbolic.svg").symlink_to(store / "current/newapp-symbolic.svg")
        seen = {self.fingerprint()}
        for edit in (own, store / "1/newapp-symbolic.svg"):
            # Same size, rewritten in place: only the file's own state tells.
            stat = edit.stat()
            edit.write_text(edit.read_text().replace("#", "@"))
            os.utime(edit, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
            seen.add(self.fingerprint())
        (store / "next").symlink_to(store / "2")
        (store / "next").replace(store / "current")
        seen.add(self.fingerprint())
        self.assertEqual(len(seen), 4)

    def test_fingerprint_reuses_the_glyph_lookups(self):
        self.install("newapp")
        theme = self.data_home / "icons/Papyrus"
        (theme / "symbolic/apps").mkdir(parents=True)
        (theme / "index.theme").write_text(
            "[Icon Theme]\nName=Papyrus\nDirectories=symbolic/apps\n\n[symbolic/apps]\nContext=Applications\nSize=16\n"
        )
        (theme / "symbolic/apps/newapp-symbolic.svg").write_text(self.ART)
        indexing = mock.patch.object(build.Glyphs, "_theme_index", autospec=True, side_effect=build.Glyphs._theme_index)
        with indexing as index:
            self.fingerprint()
        self.assertGreater(index.call_count, 0)
        with indexing as index:
            self.fingerprint()
            stdout = self.built("--app-style", "themed", "-v", out=self.tmp / "themed")
        self.assertEqual(index.call_count, 0, "nothing changed, so no theme is indexed again")
        self.assertIn("glyph [theme-symbolic] 1: newapp", stdout)
        (theme / "symbolic/apps/other-symbolic.svg").write_text(self.ART)
        with indexing as index:
            self.fingerprint()
        self.assertGreater(index.call_count, 0)

    def test_every_install_of_a_theme_lends_its_symbolic_icons(self):
        system = self.tmp / "system"
        self.env["XDG_DATA_DIRS"] = f"{FIXTURES / 'data'}:{system}"
        index = (
            "[Icon Theme]\nName=Papyrus\nDirectories=symbolic/apps\n\n[symbolic/apps]\nContext=Applications\nSize=16\n"
        )
        # A user's partial copy of the theme, ahead of the system one in the search path.
        roots = [self.data_home / "icons/Papyrus", system / "icons/Papyrus"]
        for root in roots:
            (root / "symbolic/apps").mkdir(parents=True)
            (root / "index.theme").write_text(index)
        glyph = roots[1] / "symbolic/apps/newapp-symbolic.svg"
        glyph.write_text(self.ART)
        dirs = build.Dirs(self.env)
        self.assertEqual(build.Glyphs.theme_roots(dirs), roots)
        self.assertEqual(build.Glyphs(FIXTURES / "src", dirs).theme_symbolic(["NewApp"]), glyph)
        (roots[0] / "symbolic/apps/newapp-symbolic.svg").write_text(self.ART)
        self.assertEqual(
            build.Glyphs(FIXTURES / "src", dirs).theme_symbolic(["newapp"]), roots[0] / "symbolic/apps" / glyph.name
        )
        seen = {self.fingerprint()}
        (roots[1] / "symbolic/apps/other-symbolic.svg").write_text(self.ART)
        seen.add(self.fingerprint())
        self.assertEqual(len(seen), 2)

    def test_a_rebuild_reuses_the_lookups_and_builds_the_same_theme(self):
        self.install("newapp")
        with self.resolving() as resolve:
            self.built()
        self.assertGreater(resolve.call_count, 0)
        self.assertTrue(self.cache().is_file())
        first = self.snapshot(self.out)
        with self.resolving() as resolve:
            self.built("--files-style", "color")
            self.built()
        self.assertEqual(resolve.call_count, 0, "a color or style change looks nothing up again")
        self.assertEqual(first, self.snapshot(self.out))

        with self.resolving() as resolve:
            self.build("--dump-sources")
        self.assertEqual(resolve.call_count, 0, "the source listing reuses them as well")

    def test_lookups_are_dropped_when_an_icon_dir_changes(self):
        self.install("newapp", icon="shared")
        self.built()
        self.assertNotIn("newapp", {p.stem for p in (self.out / "scalable/apps").glob("*.svg")})
        # The icon the entry asks for turns up in a directory the last run searched and found nothing in.
        (self.data_home / "icons/hicolor/scalable/apps/shared.svg").write_text(self.ART)
        uncached = self.tmp / "uncached"
        self.env["XDG_CACHE_HOME"], cache = str(self.tmp / "other-cache"), self.env["XDG_CACHE_HOME"]
        self.built(out=uncached)
        self.env["XDG_CACHE_HOME"] = cache
        self.built()
        self.assertIn("shared", {p.stem for p in (self.out / "scalable/apps").glob("*.svg")})
        self.assertEqual(self.snapshot(uncached), self.snapshot(self.out))

    def test_a_cached_icon_that_is_gone_is_looked_up_again(self):
        self.built()
        data = json.loads(self.cache().read_text())
        self.assertIn("alpha", data["resolved"])
        data["resolved"]["alpha"] = str(self.tmp / "gone.svg")
        self.cache().write_text(json.dumps(data))
        with self.resolving() as resolve:
            self.built()
        self.assertEqual([call.args[1] for call in resolve.call_args_list], ["alpha"])
        alpha = str(FIXTURES / "data/icons/hicolor/scalable/apps/alpha.svg")
        self.assertEqual(json.loads(self.cache().read_text())["resolved"]["alpha"], alpha)

    def test_a_broken_or_unwritable_cache_falls_back_to_looking_up(self):
        reference = self.tmp / "reference"
        self.built(out=reference)
        for broken in ("", "{", "[]", '{"version": 1, "key": "x", "resolved": {}}', '{"resolved": {"a": 1}}'):
            self.cache().write_text(broken)
            with self.resolving() as resolve:
                self.built()
            self.assertGreater(resolve.call_count, 0, broken)
            self.assertEqual(self.snapshot(reference), self.snapshot(self.out), broken)
            self.assertEqual(json.loads(self.cache().read_text())["version"], build.LOOKUPS_VERSION)
        # A file in the way of the cache directory: the build still publishes, and leaves no temporary file.
        self.cache().unlink()
        self.cache().parent.rmdir()
        self.cache().parent.write_text("")
        self.built()
        self.assertEqual(self.snapshot(reference), self.snapshot(self.out))


if __name__ == "__main__":
    unittest.main()
