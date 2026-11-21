"""Tests for scripts/build.py; fixtures only, so no installed apps, themes or network."""

import contextlib
import io
import json
import os
import re
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

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
        for badge, glyph in (("#9ecaff", "#003258"), ("#003258", "#9ecaff")):
            values = [float(v) for v in build.mono_matrix(badge, glyph).split()]
            rows = [values[i * 5 : i * 5 + 5] for i in range(4)]
            black = [row[4] for row in rows[:3]]
            white = [sum(row[:3]) + row[4] for row in rows[:3]]
            self.assertEqual([round(v * 255) for v in black], [0x00, 0x32, 0x58])
            self.assertEqual([round(v * 255) for v in white], [0x9E, 0xCA, 0xFF])
            self.assertEqual(rows[3], [0, 0, 0, 1, 0])


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
        self.env = {
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.data_home),
            "XDG_DATA_DIRS": str(FIXTURES / "data"),
            "XDG_CACHE_HOME": str(FIXTURES / "cache"),
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
            "palette: badge=#6750a4 glyph=#ffffff back=#473484 mono=no shape=circle gradient=no",
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
            self.assertNotRegex(text, r"@(badge|glyph|back|content)", path)
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
        self.built("--badge", "#112233", "--glyph", "#ddeeff", "--back", "#000001")
        folder = self.icon("places/folder.svg")
        self.assertIn('<circle cx="32" cy="32" r="32" fill="#112233"/>', folder)
        self.assertIn('<g id="dx-content" transform="translate(12.8 11.2) scale(1.6)">', folder)
        self.assertIn('<path id="c-plate" fill="#000001"', folder)
        self.assertIn('fill="#ddeeff"', folder)
        self.assertNotIn("<mask", folder)

    def test_back_defaults_to_the_badge_twelve_tones_darker(self):
        stdout = self.built("--badge", "#9ecaff")
        back = re.search(r"back=(#[0-9a-f]{6})", stdout).group(1)
        self.assertEqual(back, build.darker("#9ecaff"))
        self.assertIn(f'fill="{back}"', self.icon("places/folder.svg"))

    def test_palette_sources(self):
        palette = self.tmp / "palette.json"
        palette.write_text('{"badge": "#abc", "glyph": "#010203", "back": "#040506"}')
        self.assertIn("badge=#aabbcc glyph=#010203 back=#040506", self.built("--palette", str(palette)))
        self.assertIn(
            "badge=#aabbcc glyph=#ffffff back=#040506", self.built("--palette", str(palette), "--glyph", "#FFF")
        )
        self.assertIn("badge=#9ecaff glyph=#003258", self.built("--from-dms"))
        self.assertIn("badge=#0061a4 glyph=#ffffff", self.built("--from-dms", "--mode", "light"))
        self.assertIn("badge=#0061a4 glyph=#000000", self.built("--from-dms", "--mode", "light", "--glyph", "#000"))

    def test_bad_palettes_stop_the_build_before_publishing(self):
        palette = self.tmp / "palette.json"
        palette.write_text('{"badge": "#abc"}')
        for args in (["--badge", "blue"], ["--palette", str(palette)], ["--palette", str(self.tmp / "none.json")]):
            code, _, stderr = self.build(*args)
            self.assertEqual(code, 1, args)
            self.assertIn("Dux Icons: error:", stderr)
            self.assertFalse(self.out.exists())
        self.env["XDG_CACHE_HOME"] = str(self.tmp / "no-cache")
        self.assertEqual(self.build("--from-dms")[0], 1)

    def test_bare_app_icons_drop_the_badge_and_keep_mono(self):
        self.built("--mono", "--badge", "#9ecaff", "--glyph", "#003258")
        bare = self.out / "bare" / "apps"
        text = (bare / "alpha.svg").read_text(encoding="utf-8")
        self.assertIn("<circle", self.icon("apps/alpha.svg"))
        self.assertNotIn("<circle", text)
        self.assertIn(f'values="{build.mono_matrix("#9ecaff", "#003258")}"', text)
        ET.fromstring(text)
        self.assertEqual(os.readlink(bare / "twin.svg"), "alpha.svg")
        self.assertNotIn(build.BARE, (self.out / "index.theme").read_text(encoding="utf-8"))

    def test_mono_filters_vendor_content_only(self):
        stdout = self.built("--mono", "--badge", "#9ecaff", "--glyph", "#003258")
        self.assertIn("mono=yes", stdout)
        matrix = build.mono_matrix(build.darker("#9ecaff"), "#003258")
        for rel in ("apps/alpha.svg", "apps/raster.svg", "apps/pix.svg"):
            text = self.icon(rel)
            self.assertIn(
                '<filter id="dx-mono" filterUnits="userSpaceOnUse" x="0" y="0" width="64" height="64" '
                f'color-interpolation-filters="sRGB"><feColorMatrix type="matrix" values="{matrix}"/></filter>'
                '<g filter="url(#dx-mono)">',
                text,
            )
            ET.fromstring(text)
        self.assertNotIn("filter", self.icon("places/folder.svg"))
        self.assertNotIn("dx-mono", self.icon("apps/alpha.svg", out=self.rebuilt("--no-mono")))

    def rebuilt(self, *args):
        self.built(*args)
        return self.out

    def test_dux_labs_apps_take_the_badge_hue_without_mono(self):
        icons = self.data_home / "icons/hicolor/scalable/apps"
        icons.mkdir(parents=True)
        art = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="#553C9A"/><rect width="9" height="9" style="fill:#F5F0FF"/></svg>'
        for name in ("dev.duxlabs.files", "plain-purple"):
            (icons / f"{name}.svg").write_text(art)
        apps = self.data_home / "applications"
        apps.mkdir()
        for name in ("dev.duxlabs.files", "plain-purple"):
            (apps / f"{name}.desktop").write_text(f"[Desktop Entry]\nType=Application\nName={name}\nIcon={name}\n")
        self.built("--no-mono", "--badge", "#9ecaff")
        ours = self.icon("apps/dev.duxlabs.files.svg")
        for color in ("#553C9A", "#F5F0FF"):
            lightness = build.rgb_to_lab(build.parse_hex(color))[0]
            self.assertIn(build.toned("#9ecaff", lightness), ours)
            self.assertNotIn(color, ours)
        self.assertIn("#553C9A", self.icon("apps/plain-purple.svg"))
        self.assertIn('<g filter="url(#dx-mono)">', self.icon("apps/dev.duxlabs.files.svg", out=self.rebuilt("--mono")))

    def test_mono_contrast_adds_a_stretch_before_the_tones(self):
        values = [float(v) for v in build.contrast_matrix(2.0).split()]
        for gray, expected in ((0.5, 0.5), (0.25, 0.0), (0.75, 1.0), (0.6, 0.7)):
            got = sum(values[i] * gray for i in range(3)) + values[4]
            self.assertAlmostEqual(got, expected, places=4)
        self.assertEqual(values[15:], [0, 0, 0, 1, 0])
        palette = {"badge": "#9ecaff", "glyph": "#003258", "back": "#7ba7db"}
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

    def test_gradient_shades_the_badge_only(self):
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
        # The own icon's cutout keeps the flat badge color.
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
        self.built("--badge", "#101010")
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

    def test_vendor_css_does_not_reach_the_badge(self):
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

    def test_mono_maps_tones_between_glyph_and_back(self):
        self.built("--mono", "--badge", "#9ecaff", "--glyph", "#003258")
        # White content stays off the badge color, or a light icon would vanish into it.
        self.assertNear(self.pixel("apps/slice.svg", 32, 32), build.parse_hex(build.darker("#9ecaff")) + (255,))
        self.assertNear(self.pixel("apps/slice.svg", 32, 48), (0x9E, 0xCA, 0xFF, 255))
        red, green, _blue, alpha = self.pixel("apps/slice.svg", 12, 23)
        self.assertEqual(alpha, 255)
        self.assertLess(red, 0x30)
        self.assertLess(green, 0x60)


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
        palette = {"badge": "#6750a4", "glyph": "#ffffff", "back": "#473484"}
        for name in names:
            for gradient in (False, True):
                root = ET.fromstring(build.load_template(build.SRC, name, palette, gradient).replace("@content", ""))
                self.assertEqual(root.get("viewBox"), "0 0 64 64", name)

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

    def test_index_template_names_the_product(self):
        template = build.INDEX_TEMPLATE.read_text()
        self.assertEqual(build.index_name(template), "Dux Icons")
        self.assertIn("Comment=Material icons for Dux Modular Shell\n", template)


if __name__ == "__main__":
    unittest.main()
