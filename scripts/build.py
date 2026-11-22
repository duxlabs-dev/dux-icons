#!/usr/bin/env python3
"""Dux Icons build: frames vendor and own icons into a freedesktop icon theme of SVG files.

Standard library only. The theme is assembled in memory, written to a temporary sibling of the
output and renamed into place. The checkout is never written or deleted.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import fnmatch
import hashlib
import json
import math
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
INDEX_TEMPLATE = ROOT / "index.theme.in"

PRODUCT = "Dux Icons"
DEFAULT_OUT_NAME = "dux-icons"
MARKER = ".dux-icons-build"
MARKER_TEXT = "dux-icons 1\n"

CANVAS = 64.0
SCALABLE = "scalable"
SCALABLE_SIZE = 64
SCALABLE_MIN = 16
SCALABLE_MAX = 512

ID_PREFIX = "c-"
SCOPE_ID = "dx-content"
CLIP_ID = "dx-clip"
MONO_ID = "dx-mono"
OUR_APPS = "dev.duxlabs."
FILL_ID = "dx-fill"
SHAPE_ID = "dx-shape"

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"

MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_RASTER_SIDE = 4096
PREFERRED_RASTER_SIDE = 256

PATHS_FILE = "icon-paths.json"
MIME_FILES = ("generic-icons", "types", "aliases")
# Bare app icons for panels too small for the tile; outside the theme dirs, so no lookup finds them.
BARE = "bare"
BARE_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 64 64">'
    "@content</svg>\n"
)
# Tones the tile gradient is lighter at the top and darker at the bottom.
GRADIENT_TONES = (8.0, 10.0)
# Art of our own that replaces an installed app's icon in this theme only: src/art/apps/<icon name>.svg.
ART_DIR = Path("art") / "apps"
# The CIELAB L* range --own-tone keeps recolored art inside.
OWN_TONE_RANGE = (4.0, 98.0)
# Our art is drawn for a deep tile and washes out on a lighter one, so in the color style it takes the hue at a
# light scheme's (tile, glyph) L* instead.
OWN_LIGHT_TILE = 55.0
OWN_DEEP_TONES = (40.0, 100.0)
# A light primary holds less chroma than its scheme's deep one (Material's T80 against T40 runs about 1.2 to 2.5
# times less), so the deep tile takes back some of it.
OWN_DEEP_CHROMA = 1.4
# Width of a tile's ring inside the tile outline; the stroke is twice that, its outer half clipped away.
RING_WIDTH = 3.0
# Room a tile's `reach` leaves between the icon and the tile edge (or its ring), and how far it may grow it.
REACH_MARGIN = 2.0
REACH_SCALE = (1.0, 2.0)
# Radius of the largest circle about the canvas center inside each shipped tile shape, rounded down.
INSET_RADII = {
    "circle": 32.0,
    "clover": 25.6,
    "cookie": 28.8,
    "hexagon": 28.3,
    "rounded": 32.0,
    "square": 32.0,
    "squircle": 32.0,
}
INSET_DEFAULT = 28.0

# App icon styles: color keeps own colors; tinted repaints in tones between tile and glyph; themed and
# monochrome draw one-color glyphs on a uniform tile, in the theme hue or near-neutral. The tiles file sets the
# last two apart.
APP_STYLES = ("color", "tinted", "themed", "monochrome")
GLYPH_STYLES = ("themed", "monochrome")
GLYPH_ID = "dx-glyph"
CUT_ID = "dx-cut"
# Symbolic glyphs of our own apps, solid shapes after their full-color art in ART_DIR: <icon name>-symbolic.svg.
OWN_SYMBOLIC_DIR = Path("art") / "symbolic"
# One-color glyphs for apps whose icon reads badly as a glyph and that ship no symbolic icon. Our own drawings
# only, no brand logos; the names each one stands for are in src/mono/aliases.json.
MONO_DIR = Path("mono") / "apps"
MONO_ALIASES = Path("mono") / "aliases.json"
# Installed icon themes the symbolic lookup never reads: our own packs and the folder overlays DMS writes.
OWN_THEME_GLOBS = ("dux-icons*", "*-dms-*")
# Share of the canvas the longer side of every glyph spans on its tile, and off the tile in the bare copies.
GLYPH_SIZE = 0.58
BARE_GLYPH_SIZE = 0.84
# Share of its own canvas a glyph is taken to fill when its drawn extent cannot be told.
GLYPH_GUESS = 0.85
# Margin each side of a cut-out PNG, around the tight box of its mark.
CUTOUT_MARGIN = 0.06
# feColorMatrix alpha row (R, G, B, A, offset) of a solid tint: every opaque pixel in the glyph color. The offset
# is never positive: Qt SVG would paint it onto transparent pixels.
SOLID_ALPHA = (0.0, 0.0, 0.0, 1.0, 0.0)
# A first shape in one solid color that covers this share of an SVG's canvas is its plate.
PLATE_COVER = 0.85
# Luminance spread below which an SVG's colors are one tone and it is tinted solid.
TONE_SPREAD = 0.12
# Least slope of an SVG tint's alpha across the luminance it cuts at: a gradient that spans the cut gets a short
# edge instead of fading through it.
TINT_RAMP_MIN = 30.0
# The fallback chain of the themed and monochrome styles, in the order its stages are tried.
GLYPH_STAGES = (
    "own",
    "curated",
    "symbolic",
    "theme-symbolic",
    "plate",
    "cutout",
    "luma-holes",
    "depth",
    "solid",
    "duotone",
)

# How own files, folders and places are drawn: on the tile like everything else, or in color without it.
FILES_STYLES = ("standard", "color")
# The own-icon contexts the color style redraws; own app icons keep the tile.
COLOR_CONTEXTS = ("places", "mimetypes", "status")
# CIELAB (L*, C*) of each color-style role at category chroma 1, after the Dux Files art. `back` is a folder's
# back plate, `fold` a sheet's turned corner.
COLOR_TONES = {
    "top": (95.5, 8.0),
    "bottom": (80.0, 36.0),
    "back": (70.0, 46.0),
    "fold": (72.0, 40.0),
    "rim": (59.0, 64.0),
    "mark": (41.5, 63.0),
}
# Share of the canvas the art's box spans in the color style, and its rim width there in canvas units.
COLOR_FILL = 0.9
COLOR_RIM = RING_WIDTH
# Theme chroma (CIELAB C*) at which color-style places get the full COLOR_TONES chroma; grayer themes get less.
COLOR_THEME_CHROMA = 40.0
# Below this tile chroma the theme has no hue to harmonize file categories toward.
COLOR_HUELESS = 4.0
# Material Blend.harmonize: a category hue turns toward the theme hue by half the difference, at most this far.
HARMONIZE_MAX = 15.0
# Color-style folder parts, drawn on the 64-unit canvas rather than from the art. CIELAB (L*, C*) of each role
# at category chroma 1, like COLOR_TONES.
FOLDER_TONES = {
    "back_top": (55.0, 50.0),
    "back_bottom": (41.0, 54.0),
    "back_rim": (43.0, 56.0),
    "paper_top": (99.0, 2.0),
    "paper_bottom": (92.0, 7.5),
    "paper2_top": (93.5, 7.0),
    "paper2_bottom": (85.0, 14.0),
    "shadow": (38.0, 48.0),
    "cavity_top": (32.0, 48.0),
    "cavity_bottom": (43.0, 52.0),
    "front_top": (90.0, 15.0),
    "front_bottom": (74.0, 44.0),
    "rim_top": (72.0, 38.0),
    "rim_mid": (55.0, 56.0),
    "rim_bottom": (45.0, 58.0),
    "shine": (98.5, 3.5),
    "mark": (35.5, 56.0),
}
# Per folder kind: outlines, gradients (id, x1, y1, x2, y2, stops of (offset, role, opacity)) and layers back to
# front. In a layer, {role} is a FOLDER_TONES color; m-back and m-front mask the rim inside each outline.
FOLDER_BACK = (
    "M8.5 8H22.6C26 8 27.1 13.5 31 13.5H55.5A4.5 4.5 0 0 1 60 18V49.5A5.5 5.5 0 0 1 54.5 55H9.5"
    "A5.5 5.5 0 0 1 4 49.5V12.5A4.5 4.5 0 0 1 8.5 8Z"
)
FOLDER_FRONT = "M8 21.5H56A4 4 0 0 1 60 25.5V50.5A5.5 5.5 0 0 1 54.5 56H9.5A5.5 5.5 0 0 1 4 50.5V25.5A4 4 0 0 1 8 21.5Z"
FOLDER_OPEN_BACK = (
    "M8.5 8H22.2C25.5 8 26.6 13.2 30.3 13.2H52.5A4.5 4.5 0 0 1 57 17.7V50.5A5.5 5.5 0 0 1 51.5 56H9.5"
    "A5.5 5.5 0 0 1 4 50.5V12.5A4.5 4.5 0 0 1 8.5 8Z"
)
FOLDER_OPEN_FRONT = (
    "M12.2 28.5H57.8C60.5 28.5 62 30.8 61.2 33.4L55.8 51.4C55 54.1 52.5 56 49.7 56H9.5C6.3 56 3.9 53.3 4.5 50.2"
    "L8.5 32C9 29.9 10.5 28.5 12.2 28.5Z"
)
_FOLDER_BACK_LAYER = (
    '<path d="{back}" fill="url(#back)" stroke="{back_rim}" stroke-width="4" stroke-linejoin="round" '
    'mask="url(#m-back)"/>'
)
_FOLDER_FRONT_LAYER = (
    '<path d="{front}" fill="url(#front)" stroke="url(#rim)" stroke-width="5.6" stroke-linejoin="round" '
    'mask="url(#m-front)"/>'
)
_FOLDER_SHINE = (
    '<path d="M{x0} {y}H{x1}" fill="none" stroke="{{shine}}" stroke-width="1.3" stroke-linecap="round" '
    'opacity="0.58" mask="url(#m-front)"/>'
)
_FOLDER_SHADOW = ((0, "shadow", 0.0), (1, "shadow", 0.34))
FOLDERS = {
    "closed": {
        "back": FOLDER_BACK,
        "front": FOLDER_FRONT,
        "gradients": (
            ("back", 32, 8, 32, 55, ((0, "back_top", None), (1, "back_bottom", None))),
            ("paper", 32, 16.8, 32, 22.5, ((0, "paper_top", None), (1, "paper_bottom", None))),
            ("shadow", 32, 19.3, 32, 21.8, _FOLDER_SHADOW),
            ("front", 10, 21.5, 54, 56, ((0, "front_top", None), (1, "front_bottom", None))),
            ("rim", 32, 21.5, 32, 56, ((0, "rim_top", None), (0.18, "rim_mid", None), (1, "rim_bottom", None))),
        ),
        "layers": (
            _FOLDER_BACK_LAYER,
            '<path d="M11 16.8H53A2.5 2.5 0 0 1 55.5 19.3V30H8.5V19.3A2.5 2.5 0 0 1 11 16.8Z" fill="url(#paper)"/>',
            '<rect x="8.5" y="19.3" width="47" height="2.6" fill="url(#shadow)"/>',
            _FOLDER_FRONT_LAYER,
            _FOLDER_SHINE.format(x0=9.5, y=22.6, x1=54.5),
        ),
    },
    "open": {
        "back": FOLDER_OPEN_BACK,
        "front": FOLDER_OPEN_FRONT,
        "gradients": (
            ("back", 30, 8, 30, 56, ((0, "back_top", None), (1, "back_bottom", None))),
            ("cavity", 30, 15.8, 30, 52, ((0, "cavity_top", None), (1, "cavity_bottom", None))),
            ("paper2", 30, 16.2, 30, 33, ((0, "paper2_top", None), (1, "paper2_bottom", None))),
            ("paper", 32, 20.4, 32, 34, ((0, "paper_top", None), (1, "paper_bottom", None))),
            ("shadow", 32, 25.5, 32, 29, _FOLDER_SHADOW),
            ("front", 14, 28.5, 54, 56, ((0, "front_top", None), (1, "front_bottom", None))),
            ("rim", 32, 28.5, 32, 56, ((0, "rim_top", None), (0.22, "rim_mid", None), (1, "rim_bottom", None))),
        ),
        "layers": (
            _FOLDER_BACK_LAYER,
            '<path d="M10 15.8H51A3 3 0 0 1 54 18.8V52H7V18.8A3 3 0 0 1 10 15.8Z" fill="url(#cavity)"/>',
            '<path d="M12 16.2H47.5A2.5 2.5 0 0 1 50 18.7V35H9.5V18.7A2.5 2.5 0 0 1 12 16.2Z" fill="url(#paper2)"/>',
            '<path d="M14.5 20.4H50.5A2.5 2.5 0 0 1 53 22.9V38H12V22.9A2.5 2.5 0 0 1 14.5 20.4Z" fill="url(#paper)"/>',
            '<rect x="12" y="25.5" width="41" height="3.8" fill="url(#shadow)"/>',
            _FOLDER_FRONT_LAYER,
            _FOLDER_SHINE.format(x0=12.8, y=29.6, x1=57.4),
        ),
    },
}
# Where a closed folder's type marks land on the front: (scale, from (x, y) in the 24-unit grid, to (x, y) on the
# canvas).
FOLDER_MARK = (2.65, (12.0, 13.75), (32.0, 39.0))
# Color-style drawing of own icons; the first matching glob on `context/name` wins.
#   folder: FOLDERS kind replacing the art; its type marks go on the front
#   frame: roles of the leading elements; later elements are type marks
#          body: gradient with a rim; back: back plate with a rim; fold: flat tone;
#          solid: rim tone, for art too small to hold a rim; None: dropped
#   split: the first element's subpaths (each starting with an absolute M) count as elements
#   box: art extent (x0, y0, x1, y1) in the 24-unit grid, fitted to COLOR_FILL of the canvas
# A type mark in the body element's token is a cut-out and takes the body gradient; one in @back takes `fold`.
COLOR_RULES = (
    (("status/folder-open",), {"folder": "open"}),
    (("places/folder", "places/folder-*", "places/user-home", "places/user-desktop"), {"folder": "closed"}),
    # The handle and lid are too thin for a rim on both sides.
    (("places/user-trash",), {"split": True, "frame": ("solid", "solid", "body", None), "box": (4.0, 3.0, 20.0, 21.0)}),
    (("status/user-trash-full",), {"split": True, "frame": ("solid", "solid", "body"), "box": (4.0, 3.0, 20.0, 21.0)}),
    (
        ("places/network-workgroup",),
        {"split": True, "frame": ("body", "body", "body", "solid", "solid"), "box": (3.0, 3.0, 21.0, 21.0)},
    ),
    (("places/start-here",), {"frame": ("solid",), "box": (3.0, 3.0, 21.0, 21.0)}),
    (("places/user-bookmarks",), {"frame": ("back", "body"), "box": (3.0, 3.0, 21.0, 21.25)}),
    (("mimetypes/*",), {"frame": ("body", "fold"), "box": (4.5, 2.0, 19.5, 22.0)}),
    (("*",), {"frame": ("body",), "box": (3.0, 3.0, 21.0, 21.0)}),
)
# File categories of the color style: CIELAB hue and chroma relative to COLOR_TONES, after Papirus. Hues harmonize
# toward the theme hue; None is the theme's own, chroma scaled so a gray theme stays gray. Unlisted own mimetypes
# are `text`.
FILE_CATEGORIES = {
    "text": {"hue": None, "chroma": 0.12, "icons": ["text-x-generic"]},
    "pdf": {"hue": 32.0, "chroma": 1.0, "icons": ["application-pdf"]},
    "presentation": {"hue": 58.0, "chroma": 1.0, "icons": ["x-office-presentation"]},
    "audio": {"hue": 84.0, "chroma": 1.0, "icons": ["audio-x-generic"]},
    "font": {"hue": 85.0, "chroma": 0.3, "icons": ["font-x-generic"]},
    "archive": {"hue": 110.0, "chroma": 1.0, "icons": ["package-x-generic"]},
    "spreadsheet": {"hue": 140.0, "chroma": 1.0, "icons": ["x-office-spreadsheet"]},
    "script": {"hue": 168.0, "chroma": 0.9, "icons": ["text-x-script"]},
    "image": {"hue": 196.0, "chroma": 1.0, "icons": ["image-x-generic"]},
    "optical": {"hue": 226.0, "chroma": 0.85, "icons": ["media-optical"]},
    "html": {"hue": 250.0, "chroma": 1.0, "icons": ["text-html"]},
    "executable": {"hue": 250.0, "chroma": 0.32, "icons": ["application-x-executable"]},
    "document": {"hue": 278.0, "chroma": 1.0, "icons": ["x-office-document"]},
    "video": {"hue": 310.0, "chroma": 1.0, "icons": ["video-x-generic"]},
}

CONTEXTS = {
    "apps": "Applications",
    "mimetypes": "MimeTypes",
}

TOKEN_RE = re.compile(r"@(tile|glyph|back)\b")
STRAY_TOKEN_RE = re.compile(r"(?<![\w&])@[A-Za-z][\w-]*")
# A color written as an attribute or style value, up to a rule's closing brace; an id like url(#add) is left alone.
COLOR_RE = re.compile(r"(?<=[\"':\s])#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?=[\"';\s)}])")
NAME_RE = re.compile(r"[A-Za-z0-9_+][A-Za-z0-9_.+@-]*\Z")
# A whole <mask> element: its colors are luminance that says how much shows, not paint.
MASK_RE = re.compile(r"(<mask\b[^>]*?/>|<mask\b[^>]*>.*?</mask>)", re.DOTALL)
# The template elements that draw the tile: self-closing, filled with @tile.
TILE_SHAPE_RE = re.compile(r'<[A-Za-z][^<>]*\sfill="@tile"[^<>]*/>')


class BuildError(Exception):
    """The build cannot continue. Nothing has been published."""


class Skip(Exception):
    """One icon cannot be built. Counted under `reason` and reported."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def parse_hex(value: str) -> tuple[int, int, int]:
    text = value.strip()
    if not re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", text):
        raise ValueError(value)
    digits = text[1:]
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def normalize_color(value, what: str) -> str:
    try:
        return to_hex(parse_hex(str(value)))
    except ValueError:
        raise BuildError(f"{what} is not a #rgb or #rrggbb color: {value!r}") from None


_D65 = (0.95047, 1.0, 1.08883)


def _to_linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _from_linear(channel: float) -> float:
    return channel * 12.92 if channel <= 0.0031308 else 1.055 * channel ** (1 / 2.4) - 0.055


def rgb_to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    r, g, b = (_to_linear(c / 255) for c in rgb)
    xyz = (
        0.4124564 * r + 0.3575761 * g + 0.1804375 * b,
        0.2126729 * r + 0.7151522 * g + 0.0721750 * b,
        0.0193339 * r + 0.1191920 * g + 0.9503041 * b,
    )

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116

    fx, fy, fz = (f(v / n) for v, n in zip(xyz, _D65))
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def lab_to_rgb_float(lab: tuple[float, float, float]) -> tuple[float, float, float]:
    lightness, a, b = lab
    fy = (lightness + 16) / 116
    fx = fy + a / 500
    fz = fy - b / 200

    def inv(t: float) -> float:
        return t**3 if t**3 > 216 / 24389 else (116 * t - 16) / (24389 / 27)

    x, y, z = (inv(v) * n for v, n in zip((fx, fy, fz), _D65))
    return (
        _from_linear(3.2404542 * x - 1.5371385 * y - 0.4985314 * z),
        _from_linear(-0.9692660 * x + 1.8760108 * y + 0.0415560 * z),
        _from_linear(0.0556434 * x - 0.2040259 * y + 1.0572252 * z),
    )


def darker(color: str, tones: float = 12.0, floor: float = 10.0, ceiling: float = 98.0) -> str:
    """CIELAB L* lowered by `tones` (raised when negative), keeping hue and as much chroma as sRGB allows."""
    lightness = rgb_to_lab(parse_hex(color))[0]
    return toned(color, min(ceiling, max(floor, lightness - tones)))


def toned(color: str, target: float) -> str:
    """`color` at CIELAB L* `target`, keeping hue and as much chroma as sRGB allows."""
    _, chroma, hue = lch_of(color)
    return lch(target, chroma, hue)


def lch(lightness: float, chroma: float, hue: float) -> str:
    """The CIELAB color of this L*, C* and hue in degrees, its chroma lowered as far as sRGB needs."""
    angle = math.radians(hue)

    def at(c: float) -> tuple[float, float, float]:
        return lab_to_rgb_float((lightness, c * math.cos(angle), c * math.sin(angle)))

    def in_gamut(rgb: tuple[float, float, float]) -> bool:
        return all(-0.0005 <= v <= 1.0005 for v in rgb)

    rgb = at(chroma)
    if not in_gamut(rgb):
        low, high = 0.0, chroma
        for _ in range(24):
            mid = (low + high) / 2
            if in_gamut(at(mid)):
                low = mid
            else:
                high = mid
        rgb = at(low)
    return to_hex(tuple(min(255, max(0, round(v * 255))) for v in rgb))


def lch_of(color: str) -> tuple[float, float, float]:
    """(L*, C*, hue in degrees) of a hex color."""
    lightness, a, b = rgb_to_lab(parse_hex(color))
    return lightness, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360


def harmonize(hue: float, source: float) -> float:
    """Material Blend.harmonize on hue angles: `hue` turned toward `source` by min(difference / 2, 15 degrees)."""
    difference = (source - hue + 180) % 360 - 180
    turn = min(abs(difference) / 2, HARMONIZE_MAX)
    return (hue + math.copysign(turn, difference)) % 360


def retone(content: str, tile: str, lightest: float | None = None) -> str:
    """Every color in `content` outside its masks moved onto the tile hue, each keeping its own lightness.

    With `lightest`, every lightness moves by the same amount so the lightest color lands on that L*,
    kept within OWN_TONE_RANGE.
    """
    # Mask colors are luminance, not paint: leave them, and keep them out of the lightest.
    parts = MASK_RE.split(content)
    art = parts[::2]
    shift = 0.0
    floor, ceiling = (-math.inf, math.inf)
    if lightest is not None:
        tones = [rgb_to_lab(parse_hex(color))[0] for part in art for color in COLOR_RE.findall(part)]
        shift = lightest - max(tones) if tones else 0.0
        floor, ceiling = OWN_TONE_RANGE

    def shifted(match: re.Match) -> str:
        tone = rgb_to_lab(parse_hex(match.group()))[0] + shift
        return toned(tile, min(ceiling, max(floor, tone)))

    parts[::2] = [COLOR_RE.sub(shifted, part) for part in art]
    return "".join(parts)


def mono_matrix(tile: str, glyph: str) -> str:
    """feColorMatrix values mapping content luminance onto the tile-to-glyph color line; alpha kept."""
    b = [c / 255 for c in parse_hex(tile)]
    g = [c / 255 for c in parse_hex(glyph)]

    def luma(c: list[float]) -> float:
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    dark, light = (g, b) if luma(g) <= luma(b) else (b, g)
    rows = []
    for i in range(3):
        span = light[i] - dark[i]
        rows.append(f"{fmt(span * 0.2126, 5)} {fmt(span * 0.7152, 5)} {fmt(span * 0.0722, 5)} 0 {fmt(dark[i], 5)}")
    rows.append("0 0 0 1 0")
    return " ".join(rows)


def contrast_matrix(contrast: float) -> str:
    """feColorMatrix values turning content into gray with its luminance pushed away from the middle."""
    row = (
        f"{fmt(contrast * 0.2126, 5)} {fmt(contrast * 0.7152, 5)} {fmt(contrast * 0.0722, 5)} 0 "
        f"{fmt(0.5 - contrast / 2, 5)}"
    )
    return f"{row} {row} {row} 0 0 0 1 0"


def fmt(value: float, places: int = 4) -> str:
    text = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def esc_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def esc_attr(text: str) -> str:
    return esc_text(text).replace('"', "&quot;")


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BuildError(f"missing file: {path}") from None
    except (OSError, ValueError) as exc:
        raise BuildError(f"cannot read {path}: {exc}") from None


def paint_tokens(text: str, palette: dict[str, str]) -> str:
    return TOKEN_RE.sub(lambda m: palette[m.group(1)], text)


_LENGTH_RE = re.compile(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(px|pt|pc|mm|cm|in)?\s*\Z")
_UNIT_PX = {None: 1.0, "px": 1.0, "pt": 96 / 72, "pc": 16.0, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0}


def parse_length(value: str | None) -> float | None:
    """Length in px, or None for missing, relative (`%`, `em`) or bad values."""
    if value is None:
        return None
    match = _LENGTH_RE.match(value)
    if not match:
        return None
    return float(match.group(1)) * _UNIT_PX[match.group(2)]


def parse_viewbox(value: str | None) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    try:
        parts = [float(p) for p in value.replace(",", " ").split()]
    except ValueError:
        return None
    if len(parts) != 4 or parts[2] < 0 or parts[3] < 0:
        return None
    return parts[0], parts[1], parts[2], parts[3]


def parse_aspect(value: str | None) -> tuple[str, bool]:
    """(align, slice). align is "none" or like "xMidYMid"."""
    align, slice_ = "xMidYMid", False
    for word in (value or "").split():
        if word == "none" or re.fullmatch(r"x(Min|Mid|Max)Y(Min|Mid|Max)", word):
            align = word
        elif word == "slice":
            slice_ = True
    return align, slice_


class Slot:
    """The square of the canvas that content is fitted into."""

    def __init__(self, padding: float, offset: tuple[float, float]):
        inset = CANVAS * padding
        self.size = CANVAS - 2 * inset
        self.x = inset + offset[0]
        self.y = inset + offset[1]
        if self.size <= 0:
            raise BuildError(f"padding {padding} leaves no room for content")

    def fit_box(self, width: float, height: float) -> tuple[float, float, float, float]:
        """Largest centered rect of the given aspect ratio: x, y, w, h."""
        k = self.size / max(width, height)
        w, h = width * k, height * k
        return self.x + (self.size - w) / 2, self.y + (self.size - h) / 2, w, h


def place_viewport(root: ET.Element, slot: Slot) -> tuple[str, tuple[float, float, float, float]]:
    """Transform from document user space into the slot, and the viewport rect on the canvas."""
    tx, ty, sx, sy, viewport = viewport_map(root, slot)
    scale_text = fmt(sx, 6) if sx == sy else f"{fmt(sx, 6)} {fmt(sy, 6)}"
    return f"translate({fmt(tx)} {fmt(ty)}) scale({scale_text})", viewport


def viewport_map(root: ET.Element, slot: Slot) -> tuple[float, float, float, float, tuple[float, float, float, float]]:
    """(tx, ty, sx, sy, viewport) of place_viewport: canvas x = tx + sx * user x, likewise y."""
    viewbox = parse_viewbox(root.get("viewBox"))
    width = parse_length(root.get("width"))
    height = parse_length(root.get("height"))
    if viewbox is None:
        if width is None or height is None:
            raise Skip("no-size")
        viewbox = (0.0, 0.0, width, height)
    else:
        if viewbox[2] == 0 or viewbox[3] == 0:
            raise Skip("empty-viewbox")
        if width is None and height is None:
            width, height = viewbox[2], viewbox[3]
        elif width is None:
            width = height * viewbox[2] / viewbox[3]
        elif height is None:
            height = width * viewbox[3] / viewbox[2]
    if width <= 0 or height <= 0 or viewbox[2] <= 0 or viewbox[3] <= 0:
        raise Skip("no-size")

    vx, vy, vw, vh = slot.fit_box(width, height)
    sx, sy = vw / viewbox[2], vh / viewbox[3]
    align, slice_ = parse_aspect(root.get("preserveAspectRatio"))
    tx, ty = vx, vy
    if align != "none":
        scale = max(sx, sy) if slice_ else min(sx, sy)
        ax = {"xMin": 0.0, "xMid": 0.5, "xMax": 1.0}[align[:4]]
        ay = {"YMin": 0.0, "YMid": 0.5, "YMax": 1.0}[align[4:]]
        tx += (vw - viewbox[2] * scale) * ax
        ty += (vh - viewbox[3] * scale) * ay
        sx = sy = scale
    tx -= viewbox[0] * sx
    ty -= viewbox[1] * sy
    return tx, ty, sx, sy, (vx, vy, vw, vh)


_CSS_SKIP_RE = re.compile(r"/\*.*?\*/|<!--|-->", re.DOTALL)
_CSS_PROTECTED_RE = re.compile(r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|\[[^\]]*\])")
_CSS_NAME_RE = re.compile(r"([.#])(-?[A-Za-z_\u0080-￿][\w\u0080-￿-]*)")
_CSS_ROOT_RE = re.compile(r"(?:svg(?![\w-])|:root)", re.IGNORECASE)
_URL_REF_RE = re.compile(r"url\(\s*(['\"]?)#([^'\")]*?)\1\s*\)")


def _css_find(css: str, start: int, stops: str) -> int:
    """Index of the first char of `stops` at nesting depth 0, or -1."""
    depth = 0
    i = start
    while i < len(css):
        ch = css[i]
        if ch in "\"'":
            end = i + 1
            while end < len(css) and css[end] != ch:
                end += 2 if css[end] == "\\" else 1
            i = end
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif depth == 0 and ch in stops:
            return i
        i += 1
    return -1


def _css_block_end(css: str, open_brace: int) -> int:
    depth = 0
    i = open_brace
    while i < len(css):
        ch = css[i]
        if ch in "\"'":
            end = i + 1
            while end < len(css) and css[end] != ch:
                end += 2 if css[end] == "\\" else 1
            i = end
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise Skip("css")


def _css_selector(selector: str, scope: str, prefix: str) -> str:
    selector = " ".join(selector.split())
    if not selector:
        raise Skip("css")
    parts = _CSS_PROTECTED_RE.split(selector)
    for i in range(0, len(parts), 2):
        parts[i] = _CSS_NAME_RE.sub(lambda m: m.group(1) + prefix + m.group(2), parts[i])
    selector = "".join(parts)
    root = _CSS_ROOT_RE.match(selector)
    if root:
        return f"#{scope}{selector[root.end() :]}"
    return f"#{scope} {selector}"


def scope_css(css: str, scope: str, prefix: str) -> str:
    """Rewrite a stylesheet so its rules apply only inside `#scope`, with names prefixed."""
    css = _CSS_SKIP_RE.sub(" ", css)
    out: list[str] = []
    i = 0
    while i < len(css):
        if css[i].isspace() or css[i] == ";":
            i += 1
            continue
        stop = _css_find(css, i, "{;}")
        if stop < 0:
            if css[i:].strip():
                raise Skip("css")
            break
        prelude = css[i:stop].strip()
        if css[stop] == "}":
            raise Skip("css")
        if css[stop] == ";":
            if not prelude.startswith("@"):
                raise Skip("css")
            i = stop + 1
            continue
        end = _css_block_end(css, stop)
        body = css[stop + 1 : end]
        if prelude.startswith("@"):
            keyword = re.match(r"@[\w-]*", prelude).group(0).lower()
            if keyword in ("@media", "@supports"):
                out.append(f"{prelude}{{{scope_css(body, scope, prefix)}}}")
            else:
                out.append(f"{prelude}{{{body.strip()}}}")
        else:
            selectors = []
            start = 0
            while True:
                comma = _css_find(prelude, start, ",")
                piece = prelude[start:] if comma < 0 else prelude[start:comma]
                selectors.append(_css_selector(piece, scope, prefix))
                if comma < 0:
                    break
                start = comma + 1
            declarations = _URL_REF_RE.sub(lambda m: f"url(#{prefix}{m.group(2)})", " ".join(body.split()))
            out.append(f"{','.join(selectors)}{{{declarations}}}")
        i = end + 1
    return "".join(out)


_DROP_ELEMENTS = {"metadata", "title", "desc", "script", "foreignObject", "style"}
_TEXT_ELEMENTS = {"text", "tspan", "textPath", "tref", "altGlyph"}
_ROOT_DROP_ATTRS = {
    "viewBox",
    "width",
    "height",
    "x",
    "y",
    "preserveAspectRatio",
    "version",
    "baseProfile",
    "zoomAndPan",
    "contentScriptType",
    "contentStyleType",
    "transform",
    "id",
    "overflow",
}
_LOCAL_HREF_ELEMENTS = {
    "use",
    "image",
    "feImage",
    "tref",
    "textPath",
    "mpath",
    "linearGradient",
    "radialGradient",
    "pattern",
    "filter",
}


def _split_tag(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace, local
    return "", tag


def parse_svg(data: bytes) -> ET.Element:
    """Root element of an SVG document. Accepts gzip (`.svgz`) data."""
    if data[:2] == b"\x1f\x8b":
        try:
            inflater = zlib.decompressobj(31)
            data = inflater.decompress(data, MAX_SOURCE_BYTES + 1)
        except zlib.error:
            raise Skip("bad-gzip") from None
        if len(data) > MAX_SOURCE_BYTES:
            raise Skip("too-large")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise Skip("invalid-xml") from None
    namespace, local = _split_tag(root.tag)
    if local != "svg" or namespace not in (SVG_NS, ""):
        raise Skip("not-svg")
    return root


class _Inliner:
    def __init__(self, prefix: str):
        self.prefix = prefix
        self.css: list[str] = []
        self.out: list[str] = []
        self.drawn = 0

    def attrs(self, element: ET.Element, local: str, skip: set[str] | frozenset = frozenset()) -> list[tuple[str, str]]:
        result = []
        for key, value in element.attrib.items():
            namespace, name = _split_tag(key)
            if namespace == XLINK_NS:
                qualified = f"xlink:{name}"
            elif namespace == XML_NS:
                qualified = f"xml:{name}"
            elif namespace == "":
                qualified = name
            else:
                continue
            if namespace == "" and (name in skip or name.startswith("on")):
                continue
            if qualified in ("href", "xlink:href"):
                value = value.strip()
                if value.startswith("#"):
                    value = f"#{self.prefix}{value[1:]}"
                elif local in _LOCAL_HREF_ELEMENTS and not (
                    local in ("image", "feImage") and value[:5].lower() == "data:"
                ):
                    raise Skip("external-ref")
            elif qualified == "id":
                value = self.prefix + value
            elif qualified == "class":
                value = " ".join(self.prefix + name_ for name_ in value.split())
            elif "url(" in value:
                value = _URL_REF_RE.sub(lambda m: f"url(#{self.prefix}{m.group(2)})", value)
            result.append((qualified, value))
        return result

    def children(self, element: ET.Element, in_text: bool, in_defs: bool) -> None:
        if in_text and element.text:
            self.out.append(esc_text(element.text))
        for child in element:
            self.element(child, in_text, in_defs)
            if in_text and child.tail:
                self.out.append(esc_text(child.tail))

    def element(self, element: ET.Element, in_text: bool, in_defs: bool) -> None:
        if not isinstance(element.tag, str):
            return
        namespace, local = _split_tag(element.tag)
        if namespace not in (SVG_NS, ""):
            return
        if local == "style":
            kind = (element.get("type") or "text/css").strip().lower()
            if kind == "text/css":
                self.css.append("".join(element.itertext()))
            return
        if local in _DROP_ELEMENTS:
            return
        self.drawn += 1
        # Qt SVG draws a <clipPath>'s children as shapes unless they sit in <defs>.
        wrap = local == "clipPath" and not in_defs
        self.out.append(f"<defs><{local}" if wrap else f"<{local}")
        for name, value in self.attrs(element, local):
            self.out.append(f' {name}="{esc_attr(value)}"')
        mark = len(self.out)
        self.out.append(">")
        self.children(element, in_text or local in _TEXT_ELEMENTS, in_defs or wrap or local == "defs")
        if len(self.out) == mark + 1:
            self.out[mark] = "/>"
        else:
            self.out.append(f"</{local}>")
        if wrap:
            self.out.append("</defs>")


def _overflow_visible(root: ET.Element) -> bool:
    if (root.get("overflow") or "").strip() in ("visible", "auto"):
        return True
    return bool(re.search(r"(?:^|;)\s*overflow\s*:\s*(?:visible|auto)", root.get("style") or ""))


def inline_svg(root: ET.Element, slot: Slot, clip: bool) -> str:
    """Markup that draws the document inside the slot."""
    transform, viewport = place_viewport(root, slot)
    inliner = _Inliner(ID_PREFIX)
    inliner.children(root, False, False)
    if not inliner.drawn:
        raise Skip("empty")

    root_id = root.get("id")
    scope = ID_PREFIX + root_id if root_id else SCOPE_ID
    group = [f'<g id="{esc_attr(scope)}" transform="{transform}"']
    for name, value in inliner.attrs(root, "svg", _ROOT_DROP_ATTRS):
        group.append(f' {name}="{esc_attr(value)}"')
    group.append(">")

    parts: list[str] = []
    css = "".join(scope_css(sheet, scope, ID_PREFIX) for sheet in inliner.css)
    if css:
        parts.append(f'<style type="text/css">{esc_text(css)}</style>')
    body = "".join(group) + "".join(inliner.out) + "</g>"
    if clip and not _overflow_visible(root):
        x, y, w, h = (fmt(v) for v in viewport)
        parts.append(
            f'<mask id="{CLIP_ID}" maskUnits="userSpaceOnUse" x="{x}" y="{y}" width="{w}" height="{h}">'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#fff"/></mask>'
        )
        body = f'<g mask="url(#{CLIP_ID})">{body}</g>'
    parts.append(body)
    return "".join(parts)


def mono_wrap(content: str, palette: dict[str, str], contrast: float = 1.0, near: str = "tile") -> str:
    """Content in tones from the glyph color to palette[near], the end that sits closest to the tile."""
    size = fmt(CANVAS)
    # Each primitive clamps its result, which is what makes the first one a contrast curve.
    stretch = f'<feColorMatrix type="matrix" values="{contrast_matrix(contrast)}"/>' if contrast != 1.0 else ""
    return (
        f'<filter id="{MONO_ID}" filterUnits="userSpaceOnUse" x="0" y="0" width="{size}" height="{size}" '
        f'color-interpolation-filters="sRGB">{stretch}<feColorMatrix type="matrix" '
        f'values="{mono_matrix(palette[near], palette["glyph"])}"/></filter>'
        f'<g filter="url(#{MONO_ID})">{content}</g>'
    )


def glyph_wrap(content: str, color: str, alpha_row: tuple[float, ...] = SOLID_ALPHA) -> str:
    """Content painted in `color`, its alpha taken from the content by feColorMatrix `alpha_row`."""
    size = fmt(CANVAS)
    rgb = " ".join(f"0 0 0 0 {fmt(c / 255, 5)}" for c in parse_hex(color))
    alpha = " ".join(fmt(v, 5) for v in alpha_row)
    # A row that reads color is steep and gives a faint anti-aliased edge full alpha; masking with the source
    # alpha puts the edge back.
    edge = '<feComposite in2="SourceGraphic" operator="in"/>' if alpha_row != SOLID_ALPHA else ""
    return (
        f'<filter id="{GLYPH_ID}" filterUnits="userSpaceOnUse" x="0" y="0" width="{size}" height="{size}" '
        f'color-interpolation-filters="sRGB"><feColorMatrix type="matrix" values="{rgb} {alpha}"/>{edge}</filter>'
        f'<g filter="url(#{GLYPH_ID})">{content}</g>'
    )


Matrix = tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_NUMBER_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_TRANSFORM_RE = re.compile(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)")
# Elements whose children never draw where they stand.
_NO_RENDER = {
    "defs",
    "clipPath",
    "mask",
    "pattern",
    "symbol",
    "marker",
    "linearGradient",
    "radialGradient",
    "filter",
    *_DROP_ELEMENTS,
}
_SHAPES = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "image"}
_INHERITED = ("fill", "stroke", "stroke-width", "visibility", "fill-opacity", "stroke-opacity")


def _mul(m: Matrix, n: Matrix) -> Matrix:
    return (
        m[0] * n[0] + m[2] * n[1],
        m[1] * n[0] + m[3] * n[1],
        m[0] * n[2] + m[2] * n[3],
        m[1] * n[2] + m[3] * n[3],
        m[0] * n[4] + m[2] * n[5] + m[4],
        m[1] * n[4] + m[3] * n[5] + m[5],
    )


def parse_transform(text: str | None) -> Matrix:
    matrix = _IDENTITY
    for kind, raw in _TRANSFORM_RE.findall(text or ""):
        v = [float(n) for n in _NUMBER_RE.findall(raw)]
        if kind == "matrix" and len(v) == 6:
            step = tuple(v)
        elif kind == "translate" and v:
            step = (1.0, 0.0, 0.0, 1.0, v[0], v[1] if len(v) > 1 else 0.0)
        elif kind == "scale" and v:
            step = (v[0], 0.0, 0.0, v[1] if len(v) > 1 else v[0], 0.0, 0.0)
        elif kind == "rotate" and v:
            a = math.radians(v[0])
            cx, cy = (v[1], v[2]) if len(v) > 2 else (0.0, 0.0)
            c, s = math.cos(a), math.sin(a)
            step = (c, s, -s, c, cx - c * cx + s * cy, cy - s * cx - c * cy)
        elif kind in ("skewX", "skewY") and v:
            t = math.tan(math.radians(v[0]))
            step = (1.0, 0.0, t, 1.0, 0.0, 0.0) if kind == "skewX" else (1.0, t, 0.0, 1.0, 0.0, 0.0)
        else:
            continue
        matrix = _mul(matrix, step)
    return matrix


def _props(element: ET.Element) -> dict[str, str]:
    """Presentation attributes of one element, its style attribute winning."""
    props = {key: element.get(key) for key in (*_INHERITED, "opacity", "display") if element.get(key) is not None}
    for declaration in (element.get("style") or "").split(";"):
        key, sep, value = declaration.partition(":")
        if sep:
            props[key.strip()] = value.strip()
    return props


def _shapes(root: ET.Element) -> list[tuple[ET.Element, str, Matrix, dict[str, str]]]:
    """(element, local name, transform to root user space, inherited paint) of every shape drawn, in paint order.

    Content of unknown extent (text, nested documents) comes as is, for callers to give up on.
    """
    ids = {el.get("id"): el for el in root.iter() if el.get("id")}
    found: list[tuple[ET.Element, str, Matrix, dict[str, str]]] = []

    def walk(element: ET.Element, ctm: Matrix, state: dict[str, str], depth: int) -> None:
        if not isinstance(element.tag, str):
            return
        namespace, local = _split_tag(element.tag)
        if namespace not in (SVG_NS, "") or local in _NO_RENDER:
            return
        props = _props(element)
        if props.get("display") == "none" or _opacity(props.get("opacity")) <= 0:
            return
        state = {**state, **{key: props[key] for key in _INHERITED if key in props}}
        # Group opacity multiplies down the tree, unlike the paint properties, which a child replaces.
        state["opacity"] = str(_opacity(state.get("opacity")) * _opacity(props.get("opacity")))
        if element is not root:
            ctm = _mul(ctm, parse_transform(element.get("transform")))
        if element is root or local in ("g", "a", "switch"):
            for child in element:
                walk(child, ctm, state, depth)
        elif local == "use":
            href = (element.get("href") or element.get(f"{{{XLINK_NS}}}href") or "").strip()
            target = ids.get(href[1:]) if href.startswith("#") else None
            if target is not None and depth < 8:
                x, y = parse_length(element.get("x")) or 0.0, parse_length(element.get("y")) or 0.0
                walk(target, _mul(ctm, (1.0, 0.0, 0.0, 1.0, x, y)), state, depth + 1)
        else:
            found.append((element, local, ctm, state))

    walk(root, _IDENTITY, {"fill": "#000", "stroke": "none", "stroke-width": "1", "visibility": "visible"}, 0)
    return found


def _opacity(value: str | None) -> float:
    """An opacity property as 0 to 1, a percentage included; 1 when unset or unreadable."""
    text = (value or "").strip()
    try:
        number = float(text[:-1]) / 100 if text.endswith("%") else float(text)
    except ValueError:
        return 1.0
    return max(0.0, min(1.0, number)) if math.isfinite(number) else 1.0


def _paints(state: dict[str, str]) -> tuple[bool, bool]:
    """(filled, stroked) for a shape's inherited paint; a paint at no opacity draws nothing."""
    if state.get("visibility") in ("hidden", "collapse") or _opacity(state.get("opacity")) <= 0:
        return False, False
    return tuple(
        state.get(paint, default).strip().lower() not in ("none", "transparent")
        and _opacity(state.get(f"{paint}-opacity")) > 0
        for paint, default in (("fill", "#000"), ("stroke", "none"))
    )


class _PathScanner:
    def __init__(self, d: str):
        self.d, self.i = d, 0

    def _skip(self) -> None:
        while self.i < len(self.d) and self.d[self.i] in " \t\r\n,":
            self.i += 1

    def command(self) -> str | None:
        self._skip()
        if self.i < len(self.d) and self.d[self.i].isalpha():
            self.i += 1
            return self.d[self.i - 1]
        return None

    def more(self) -> bool:
        self._skip()
        return self.i < len(self.d) and (self.d[self.i].isdigit() or self.d[self.i] in "+-.")

    def number(self) -> float:
        self._skip()
        match = _NUMBER_RE.match(self.d, self.i)
        if not match:
            raise ValueError(self.d[self.i :])
        self.i = match.end()
        return float(match.group())

    def flag(self) -> bool:
        self._skip()
        if self.i >= len(self.d) or self.d[self.i] not in "01":
            raise ValueError(self.d[self.i :])
        self.i += 1
        return self.d[self.i - 1] == "1"


def _bezier_extrema(p: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Points where a quadratic or cubic Bezier turns in x or y."""
    ts: list[float] = []
    for axis in (0, 1):
        v = [q[axis] for q in p]
        if len(p) == 3:
            den = v[0] - 2 * v[1] + v[2]
            if den:
                ts.append((v[0] - v[1]) / den)
        else:
            a = -v[0] + 3 * v[1] - 3 * v[2] + v[3]
            b = 2 * (v[0] - 2 * v[1] + v[2])
            c = v[1] - v[0]
            if abs(a) < 1e-12:
                if b:
                    ts.append(-c / b)
            else:
                disc = b * b - 4 * a * c
                if disc >= 0:
                    ts += [(-b + sign * math.sqrt(disc)) / (2 * a) for sign in (1, -1)]
    points = []
    n = len(p) - 1
    for t in ts:
        if 0 < t < 1:
            weights = [math.comb(n, k) * (1 - t) ** (n - k) * t**k for k in range(n + 1)]
            points.append(tuple(sum(w * q[axis] for w, q in zip(weights, p)) for axis in (0, 1)))
    return points


def _arc_points(x1, y1, rx, ry, phi, large, sweep, x2, y2) -> list[tuple[float, float]]:
    """Points along an SVG elliptical arc, after the endpoint-to-center conversion of SVG 1.1 F.6.5."""
    rx, ry = abs(rx), abs(ry)
    if not rx or not ry or (x1, y1) == (x2, y2):
        return [(x2, y2)]
    cos, sin = math.cos(math.radians(phi)), math.sin(math.radians(phi))
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    xp, yp = cos * dx + sin * dy, -sin * dx + cos * dy
    grow_ = xp * xp / (rx * rx) + yp * yp / (ry * ry)
    if grow_ > 1:
        rx, ry = rx * math.sqrt(grow_), ry * math.sqrt(grow_)
    num = rx * rx * ry * ry - rx * rx * yp * yp - ry * ry * xp * xp
    den = rx * rx * yp * yp + ry * ry * xp * xp
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        co = -co
    cxp, cyp = co * rx * yp / ry, -co * ry * xp / rx
    cx, cy = cos * cxp - sin * cyp + (x1 + x2) / 2, sin * cxp + cos * cyp + (y1 + y2) / 2
    start = math.atan2((yp - cyp) / ry, (xp - cxp) / rx)
    turn = math.atan2((-yp - cyp) / ry, (-xp - cxp) / rx) - start
    if sweep and turn < 0:
        turn += 2 * math.pi
    elif not sweep and turn > 0:
        turn -= 2 * math.pi
    points = []
    for i in range(1, 25):
        a = start + turn * i / 24
        ex, ey = rx * math.cos(a), ry * math.sin(a)
        points.append((cx + ex * cos - ey * sin, cy + ex * sin + ey * cos))
    return points


def path_points(d: str) -> list[tuple[float, float]]:
    """Points whose bounding box is the drawn extent of path data `d`: ends, curve extrema and arc samples."""
    scan = _PathScanner(d)
    points: list[tuple[float, float]] = []
    x = y = start_x = start_y = 0.0
    control: tuple[float, float] | None = None
    previous = ""
    while True:
        command = scan.command()
        if command is None:
            if not scan.more():
                break
            raise ValueError(d)
        upper, relative = command.upper(), command.islower()
        if upper == "Z":
            x, y, control, previous = start_x, start_y, None, "Z"
            continue
        first = True
        while first or scan.more():
            ox, oy = (x, y) if relative else (0.0, 0.0)

            def pair(ox=ox, oy=oy):
                return scan.number() + ox, scan.number() + oy

            if upper == "M":
                x, y = pair()
                if first:
                    start_x, start_y = x, y
                    upper = "L"
                points.append((x, y))
                control = None
            elif upper == "L":
                x, y = pair()
                points.append((x, y))
                control = None
            elif upper == "H":
                x = scan.number() + ox
                points.append((x, y))
                control = None
            elif upper == "V":
                y = scan.number() + oy
                points.append((x, y))
                control = None
            elif upper in ("C", "S", "Q", "T"):
                smooth = upper in ("S", "T")
                if smooth:
                    kind = "C" if upper == "S" else "Q"
                    mirrored = control is not None and previous in ((kind, "S") if kind == "C" else (kind, "T"))
                    c1 = (2 * x - control[0], 2 * y - control[1]) if mirrored else (x, y)
                else:
                    c1 = pair()
                if upper in ("C", "S"):
                    c2 = pair()
                    end = pair()
                    curve = [(x, y), c1, c2, end]
                    control = c2
                else:
                    end = pair()
                    curve = [(x, y), c1, end]
                    control = c1
                points += _bezier_extrema(curve) + [end]
                x, y = end
            elif upper == "A":
                rx, ry, phi = scan.number(), scan.number(), scan.number()
                large, sweep = scan.flag(), scan.flag()
                end = pair()
                points += _arc_points(x, y, rx, ry, phi, large, sweep, *end)
                x, y = end
                control = None
            else:
                raise ValueError(command)
            previous = upper
            first = False
    return points


def _shape_points(element: ET.Element, local: str) -> list[tuple[float, float]] | None:
    """Local points spanning one shape's geometry, [] when it draws nothing, None when that is unknown."""

    def length(name: str, default: float | None = 0.0) -> float | None:
        value = element.get(name)
        return default if value is None else parse_length(value)

    if local == "path":
        try:
            return path_points(element.get("d") or "")
        except ValueError:
            return None
    if local in ("rect", "image"):
        x, y, w, h = length("x"), length("y"), length("width", None), length("height", None)
        if None in (x, y, w, h):
            return None
        return [(x, y), (x + w, y + h)] if w > 0 and h > 0 else []
    if local in ("circle", "ellipse"):
        cx, cy = length("cx"), length("cy")
        rx = length("r" if local == "circle" else "rx")
        ry = rx if local == "circle" else length("ry")
        if None in (cx, cy, rx, ry):
            return None
        if rx <= 0 or ry <= 0:
            return []
        return [(cx + rx * math.cos(i * math.pi / 8), cy + ry * math.sin(i * math.pi / 8)) for i in range(16)]
    if local == "line":
        values = [length(name) for name in ("x1", "y1", "x2", "y2")]
        return None if None in values else [(values[0], values[1]), (values[2], values[3])]
    if local in ("polyline", "polygon"):
        numbers = [float(n) for n in _NUMBER_RE.findall(element.get("points") or "")]
        return list(zip(numbers[::2], numbers[1::2]))
    return None


def shape_box(element: ET.Element, local: str, ctm: Matrix, state: dict[str, str]):
    """(x0, y0, x1, y1) a shape covers in root user space, stroke included; () if it draws nothing, None if unknown."""
    filled, stroked = _paints(state)
    if not (filled or stroked or local == "image"):
        return ()
    points = _shape_points(element, local)
    if points is None:
        return None
    if not points:
        return ()
    placed = [(ctm[0] * px + ctm[2] * py + ctm[4], ctm[1] * px + ctm[3] * py + ctm[5]) for px, py in points]
    xs, ys = [p[0] for p in placed], [p[1] for p in placed]
    pad = 0.0
    if stroked and local != "image":
        width = parse_length(state.get("stroke-width")) or 0.0
        pad = width / 2 * math.sqrt(abs(ctm[0] * ctm[3] - ctm[1] * ctm[2]))
    return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad


def svg_extent(root: ET.Element) -> tuple[float, float, float, float] | None:
    """(x0, y0, x1, y1) the document draws over in its user space, or None when that cannot be told."""
    boxes = []
    for element, local, ctm, state in _shapes(root):
        box = shape_box(element, local, ctm, state)
        if box is None:
            return None
        if box:
            boxes.append(box)
    if not boxes:
        return None
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


def document_box(root: ET.Element) -> tuple[float, float, float, float] | None:
    """The viewBox as (x0, y0, x1, y1), else the width and height from the origin."""
    viewbox = parse_viewbox(root.get("viewBox"))
    if viewbox is None:
        width, height = parse_length(root.get("width")), parse_length(root.get("height"))
        return (0.0, 0.0, width, height) if width and height else None
    return viewbox[0], viewbox[1], viewbox[0] + viewbox[2], viewbox[1] + viewbox[3]


def fit_glyph(root: ET.Element, size: float, clip: bool = True) -> str:
    """The document drawn centered on the canvas, the longer side of what it draws spanning `size` of it."""
    full = Slot(0.0, (0.0, 0.0))
    tx, ty, sx, sy, viewport = viewport_map(root, full)
    vx0, vy0, vx1, vy1 = viewport[0], viewport[1], viewport[0] + viewport[2], viewport[1] + viewport[3]
    extent = svg_extent(root)
    box, share = (vx0, vy0, vx1, vy1), GLYPH_GUESS
    if extent is not None:
        x0, y0, x1, y1 = (
            max(vx0, tx + sx * extent[0]),
            max(vy0, ty + sy * extent[1]),
            min(vx1, tx + sx * extent[2]),
            min(vy1, ty + sy * extent[3]),
        )
        if x1 > x0 and y1 > y0:
            box, share = (x0, y0, x1, y1), 1.0
    scale = CANVAS * size / (max(box[2] - box[0], box[3] - box[1]) * share)
    shift_x = CANVAS / 2 - scale * (box[0] + box[2]) / 2
    shift_y = CANVAS / 2 - scale * (box[1] + box[3]) / 2
    content = inline_svg(root, full, clip)
    return f'<g transform="translate({fmt(shift_x)} {fmt(shift_y)}) scale({fmt(scale, 6)})">{content}</g>'


_CSS_PAINT_RE = re.compile(
    r"((?<![\w-])(?:fill|stroke|color|stop-color|flood-color|lighting-color)\s*:\s*)([^;}!]*)", re.IGNORECASE
)


def clean_symbolic(root: ET.Element) -> ET.Element:
    """A symbolic icon with currentColor and its stylesheet's colors made plain black, for a solid tint.

    The stylesheet stays: its fill:none, display:none and stroke rules decide what the icon draws."""

    def plain(match: re.Match) -> str:
        value = match.group(2).strip()
        if value.lower() == "currentcolor" or _solid_rgb(value) is not None:
            return f"{match.group(1)}#000"
        return match.group(0)

    for element in root.iter():
        if isinstance(element.tag, str) and _split_tag(element.tag)[1] == "style":
            css = "".join(element.itertext())
            for child in list(element):
                element.remove(child)
            element.text = _CSS_PAINT_RE.sub(plain, css)
        for key, value in element.attrib.items():
            if "currentcolor" in value.lower():
                element.set(key, re.sub("currentcolor", "#000", value, flags=re.IGNORECASE))
    return root


def _solid_rgb(value: str | None) -> tuple[int, int, int] | None:
    """The sRGB color of a paint that is one plain color, else None."""
    text = (value or "").strip().lower()
    if text.startswith("rgb("):
        numbers = _NUMBER_RE.findall(text)
        if len(numbers) >= 3:
            scale = 2.55 if "%" in text else 1.0
            return tuple(min(255, max(0, round(float(n) * scale))) for n in numbers[:3])
        return None
    if text in _XPM_NAMED:
        return _XPM_NAMED[text]
    try:
        return parse_hex(text)
    except ValueError:
        return None


def _referenced_ids(root: ET.Element) -> set[str]:
    """Every id the document points at, by href or url(#id), stylesheets included."""
    found = set()
    for element in root.iter():
        href = (element.get("href") or element.get(f"{{{XLINK_NS}}}href") or "").strip()
        if href.startswith("#"):
            found.add(href[1:])
        values = list(element.attrib.values())
        if isinstance(element.tag, str) and _split_tag(element.tag)[1] == "style":
            values.append("".join(element.itertext()))
        for value in values:
            found.update(match.group(2) for match in _URL_REF_RE.finditer(value))
    return found


def strip_svg_plate(root: ET.Element) -> bool:
    """Drop the backing plate, if any: the first drawn shape, a solid rect, circle or ellipse covering PLATE_COVER
    of the canvas, with more drawn after it. True when dropped."""
    canvas = document_box(root)
    shapes = _shapes(root)
    if canvas is None:
        return False
    drawn = [(shape, box) for shape in shapes if (box := shape_box(*shape)) != ()]
    # A lone plate is the whole icon.
    if len(drawn) < 2:
        return False
    (element, local, _, state), box = drawn[0]
    if local not in ("rect", "circle", "ellipse") or box is None or _solid_rgb(state.get("fill")) is None:
        return False
    width = min(box[2], canvas[2]) - max(box[0], canvas[0])
    height = min(box[3], canvas[3]) - max(box[1], canvas[1])
    area = (canvas[2] - canvas[0]) * (canvas[3] - canvas[1])
    if width <= 0 or height <= 0 or width * height < PLATE_COVER * area:
        return False
    # A plate in <defs> or drawn through <use> may also shape a mask or clip, which removing it would break.
    parents = {child: parent for parent in root.iter() for child in parent}
    referenced = _referenced_ids(root)
    node = element
    while node is not root:
        parent = parents.get(node)
        if parent is None or node.get("id") in referenced or _split_tag(parent.tag)[1] in _NO_RENDER:
            return False
        node = parent
    parents[element].remove(element)
    return True


def _luma(rgb: tuple[int, int, int]) -> float:
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255


def _paint_colors(value: str | None, ids: dict[str, ET.Element]) -> list[tuple[int, int, int]]:
    """The plain colors of one paint: itself, or a gradient's stops or a pattern's tiles."""
    match = _URL_REF_RE.search(value or "")
    if not match:
        rgb = _solid_rgb(value)
        return [] if rgb is None else [rgb]
    colors = []
    target = ids.get(match.group(2))
    seen: set[int] = set()
    # A gradient or pattern without children of its own takes them from the one its href names.
    while target is not None and len(target) == 0 and id(target) not in seen:
        seen.add(id(target))
        href = (target.get("href") or target.get(f"{{{XLINK_NS}}}href") or "").strip()
        target = ids.get(href[1:]) if href.startswith("#") else None
    for el in target.iter() if target is not None else ():
        for paint in (el.get("stop-color"), _props(el).get("stop-color"), _props(el).get("fill")):
            rgb = _solid_rgb(paint)
            if rgb is not None:
                colors.append(rgb)
    return colors


def svg_tones(root: ET.Element) -> list[tuple[float, float]]:
    """(luminance, weight) of each plain paint color: the painted area on the canvas times opacity, split across
    a gradient's or pattern's colors."""
    ids = {el.get("id"): el for el in root.iter() if el.get("id")}
    canvas = document_box(root)
    tones: list[tuple[float, float]] = []

    for element, local, ctm, state in _shapes(root):
        box = shape_box(element, local, ctm, state)
        area = 0.0
        if box and canvas:
            width = min(box[2], canvas[2]) - max(box[0], canvas[0])
            height = min(box[3], canvas[3]) - max(box[1], canvas[1])
            area = max(0.0, width) * max(0.0, height)
        filled, stroked = _paints(state)
        for paint, on, opacity in (("fill", filled, "fill-opacity"), ("stroke", stroked, "stroke-opacity")):
            colors = _paint_colors(state.get(paint), ids) if on else []
            weight = area * _opacity(state.get("opacity")) * _opacity(state.get(opacity)) / max(1, len(colors))
            tones += [(_luma(rgb), weight) for rgb in colors]
    return tones


def svg_tint(root: ET.Element) -> tuple[str, tuple[float, ...]]:
    """(mode, alpha row) of the glyph tint for an SVG without a plate.

    Its luminances split at their widest gap. The side whose shapes cover more of the canvas is the art and
    drawn solid; the other side becomes holes the tile shows through:

    luma-holes  dark art, light parts holes (kitty's terminal and face, its light backing gone)
    depth       light art, dark parts holes (btop's striped B, its near-black shadow gone)
    solid       every color about as light as the rest
    """
    tones = svg_tones(root)
    lumas = sorted({luma for luma, _ in tones})
    if not lumas or lumas[-1] - lumas[0] < TONE_SPREAD:
        return "solid", SOLID_ALPHA
    gap = max(range(len(lumas) - 1), key=lambda i: lumas[i + 1] - lumas[i])
    low, high = lumas[gap], lumas[gap + 1]
    cut, k = (low + high) / 2, max(TINT_RAMP_MIN, 2 / (high - low))
    dark = sum(weight for luma, weight in tones if luma < cut)
    light = sum(weight for luma, weight in tones if luma > cut)
    w = (0.2126 * k, 0.7152 * k, 0.0722 * k)
    # alpha = k * (cut - luminance) or k * (luminance - cut); no offset needed.
    if dark >= light:
        return "luma-holes", (-w[0], -w[1], -w[2], k * cut, 0.0)
    return "depth", (w[0], w[1], w[2], -k * cut, 0.0)


def cutout_markup(path: str, size: float, color: str) -> str:
    """A cut-out PNG (white, the glyph in its alpha) as a mask over a `color` rect, its mark spanning `size`."""
    try:
        file = Path(path)
        if not file.is_absolute() or file.stat().st_size > MAX_SOURCE_BYTES:
            raise Skip("cutout-unreadable")
        data = file.read_bytes()
    except OSError:
        raise Skip("cutout-unreadable") from None
    width, height = png_size(data)
    if not (0 < width <= MAX_RASTER_SIDE and 0 < height <= MAX_RASTER_SIDE):
        raise Skip("bad-image-size")
    side = CANVAS * size / (1 - 2 * CUTOUT_MARGIN)
    w, h = side * width / max(width, height), side * height / max(width, height)
    x, y, w, h = (fmt(v) for v in ((CANVAS - w) / 2, (CANVAS - h) / 2, w, h))
    encoded = base64.b64encode(data).decode("ascii")
    return (
        f'<mask id="{CUT_ID}" maskUnits="userSpaceOnUse" x="{x}" y="{y}" width="{w}" height="{h}">'
        f'<image x="{x}" y="{y}" width="{w}" height="{h}" preserveAspectRatio="none" '
        f'xlink:href="data:image/png;base64,{encoded}"/></mask>'
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{color}" mask="url(#{CUT_ID})"/>'
    )


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_XPM_NAMED = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "gray": (190, 190, 190),
    "grey": (190, 190, 190),
}


def png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 33 or data[:8] != _PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise Skip("bad-png")
    return struct.unpack(">II", data[16:24])


def jpeg_size(data: bytes) -> tuple[int, int]:
    i = 2
    while i + 9 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0x00, 0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + struct.unpack(">H", data[i + 2 : i + 4])[0]
    raise Skip("bad-jpeg")


def encode_png(width: int, height: int, rgba: bytes) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    stride = width * 4
    raw = b"".join(b"\x00" + rgba[y * stride : (y + 1) * stride] for y in range(height))
    return (
        _PNG_SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _xpm_strings(text: str) -> list[str]:
    strings = []
    i = 0
    while i < len(text):
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
        elif text[i] == '"':
            end = i + 1
            buf = []
            while end < len(text) and text[end] != '"':
                if text[end] == "\\" and end + 1 < len(text):
                    end += 1
                buf.append(text[end])
                end += 1
            strings.append("".join(buf))
            i = end + 1
        else:
            i += 1
    return strings


def _xpm_color(spec: str) -> tuple[int, int, int, int]:
    name = spec.strip()
    if name.lower() == "none":
        return 0, 0, 0, 0
    if name.startswith("#"):
        digits = name[1:]
        if len(digits) in (3, 6, 12) and re.fullmatch(r"[0-9a-fA-F]+", digits):
            step = len(digits) // 3
            channels = [digits[i * step : (i + 1) * step] for i in range(3)]
            return tuple(int((c * 2)[:2], 16) for c in channels) + (255,)
        raise Skip("xpm-color")
    lowered = name.lower().replace(" ", "")
    gray = re.fullmatch(r"gr[ae]y(\d{1,3})", lowered)
    if gray and int(gray.group(1)) <= 100:
        level = round(int(gray.group(1)) * 255 / 100)
        return level, level, level, 255
    if lowered in _XPM_NAMED:
        return _XPM_NAMED[lowered] + (255,)
    raise Skip("xpm-color")


def xpm_to_png(data: bytes) -> tuple[bytes, int, int]:
    """Decode an XPM3 pixmap; color names outside a small X11 set skip the icon."""
    strings = _xpm_strings(data.decode("latin-1"))
    try:
        width, height, colors, cpp = (int(v) for v in strings[0].split()[:4])
    except (IndexError, ValueError):
        raise Skip("bad-xpm") from None
    if not (0 < width <= MAX_RASTER_SIDE and 0 < height <= MAX_RASTER_SIDE and 0 < cpp <= 4 and colors > 0):
        raise Skip("bad-xpm")
    if len(strings) < 1 + colors + height:
        raise Skip("bad-xpm")
    table: dict[str, bytes] = {}
    for line in strings[1 : 1 + colors]:
        key, words = line[:cpp], line[cpp:].split()
        values: dict[str, str] = {}
        current = None
        for word in words:
            if word in ("c", "m", "g", "g4", "s") and (current is None or current in values):
                current = word
            elif current is not None:
                values[current] = f"{values[current]} {word}" if current in values else word
        spec = values.get("c") or values.get("g") or values.get("m")
        if spec is None:
            raise Skip("bad-xpm")
        table[key] = bytes(_xpm_color(spec))
    rows = []
    for line in strings[1 + colors : 1 + colors + height]:
        if len(line) < width * cpp:
            raise Skip("bad-xpm")
        try:
            rows.append(b"".join(table[line[x : x + cpp]] for x in range(0, width * cpp, cpp)))
        except KeyError:
            raise Skip("bad-xpm") from None
    return encode_png(width, height, b"".join(rows)), width, height


def image_markup(mime: str, data: bytes, width: int, height: int, slot: Slot) -> str:
    if not (0 < width <= MAX_RASTER_SIDE and 0 < height <= MAX_RASTER_SIDE):
        raise Skip("bad-image-size")
    x, y, w, h = (fmt(v) for v in slot.fit_box(width, height))
    encoded = base64.b64encode(data).decode("ascii")
    # Qt SVG always stretches <image>, so the box takes the image's aspect ratio and "none" is exact everywhere.
    return (
        f'<image x="{x}" y="{y}" width="{w}" height="{h}" preserveAspectRatio="none" '
        f'xlink:href="data:{mime};base64,{encoded}"/>'
    )


def sniff(data: bytes) -> str:
    if data[:8] == _PNG_SIGNATURE:
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:2] == b"\x1f\x8b":
        return "svg"
    head = data[:256].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"/* XPM */"):
        return "xpm"
    if head.startswith(b"<"):
        return "svg"
    return "other"


def vendor_content(path: Path, slot: Slot) -> tuple[str, str]:
    """("svg" | "raster", markup) for one vendor icon file."""
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise Skip("too-large")
        data = path.read_bytes()
    except OSError:
        raise Skip("unreadable") from None
    kind = sniff(data)
    if kind == "svg":
        return "svg", inline_svg(parse_svg(data), slot, True)
    if kind == "png":
        return "raster", image_markup("image/png", data, *png_size(data), slot)
    if kind == "jpeg":
        return "raster", image_markup("image/jpeg", data, *jpeg_size(data), slot)
    if kind == "xpm":
        return "raster", image_markup("image/png", *xpm_to_png(data), slot)
    raise Skip("unsupported-format")


class Dirs:
    """Search paths, derived from the environment like the XDG base dir spec."""

    def __init__(self, env: dict[str, str]):
        self.home = Path(env.get("HOME") or Path.home())
        self.data_home = self._abs(env.get("XDG_DATA_HOME")) or self.home / ".local/share"
        self.cache_home = self._abs(env.get("XDG_CACHE_HOME")) or self.home / ".cache"
        raw = [Path(p) for p in (env.get("XDG_DATA_DIRS") or "").split(":") if p.startswith("/")]
        if not raw:
            raw = [
                Path("/usr/local/share"),
                Path("/usr/share"),
                self.data_home / "flatpak/exports/share",
                Path("/var/lib/flatpak/exports/share"),
            ]
        self.data: list[Path] = []
        for path in [self.data_home, *raw]:
            if path not in self.data:
                self.data.append(path)
        # Set by run: vendor icon lookups cached from an earlier run, valid while their inputs are unchanged.
        self.lookups: Lookups | None = None

    @staticmethod
    def _abs(value: str | None) -> Path | None:
        return Path(value) if value and value.startswith("/") else None

    @property
    def icon_bases(self) -> list[Path]:
        return [self.data[0] / "icons", self.home / ".icons", *(d / "icons" for d in self.data[1:])]


def read_desktop_entry(path: Path) -> dict[str, str] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    entry: dict[str, str] = {}
    in_group = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_group = line == "[Desktop Entry]"
            continue
        if not in_group or "=" not in line:
            continue
        key, _, value = line.partition("=")
        entry.setdefault(key.strip(), value.strip())
    return entry


def desktop_entries(dirs: Dirs) -> list[tuple[str, dict[str, str]]]:
    """(desktop file id without `.desktop`, keys), first data dir wins."""
    seen: dict[str, dict[str, str]] = {}
    for base in dirs.data:
        apps = base / "applications"
        if not apps.is_dir():
            continue
        for folder, subdirs, files in os.walk(apps):
            subdirs.sort()
            for name in sorted(files):
                if not name.endswith(".desktop"):
                    continue
                path = Path(folder) / name
                entry_id = str(path.relative_to(apps)).replace("/", "-")[: -len(".desktop")]
                if entry_id in seen:
                    continue
                entry = read_desktop_entry(path)
                if entry is not None:
                    seen[entry_id] = entry
    return sorted(seen.items())


class VendorIcons:
    """Finds the file for an `Icon=` value: absolute path, hicolor, pixmaps, loose icon-base files, then `themes`."""

    EXTENSIONS = (".svg", ".svgz", ".png", ".xpm")

    def __init__(self, dirs: Dirs, themes: tuple[str, ...] = ()):
        self.dirs = dirs
        self.themes = themes
        self._theme_dirs: dict[Path, list[Path]] = {}
        self._index: dict[str, list[tuple[tuple, Path]]] | None = None

    def _hicolor(self) -> dict[str, list[tuple[tuple, Path]]]:
        if self._index is not None:
            return self._index
        index: dict[str, list[tuple[tuple, Path]]] = {}
        for root_order, base in enumerate(self.dirs.icon_bases):
            root = base / "hicolor"
            try:
                size_dirs = sorted(p for p in root.iterdir() if p.is_dir())
            except OSError:
                continue
            for size_dir in size_dirs:
                if size_dir.name == "symbolic":
                    continue
                match = re.match(r"(\d+)x\d+(?:@(\d+))?\Z", size_dir.name)
                pixels = int(match.group(1)) * int(match.group(2) or 1) if match else 0
                scalable = size_dir.name.startswith("scalable")
                try:
                    contexts = sorted(p for p in size_dir.iterdir() if p.is_dir())
                except OSError:
                    continue
                for context in contexts:
                    try:
                        names = sorted(os.listdir(context))
                    except OSError:
                        continue
                    for name in names:
                        stem, ext = os.path.splitext(name)
                        ext = ext.lower()
                        if ext not in self.EXTENSIONS:
                            continue
                        if ext in (".svg", ".svgz"):
                            kind = (0, 0) if scalable else (1, -pixels)
                        elif pixels >= PREFERRED_RASTER_SIDE:
                            kind = (2, pixels)
                        else:
                            kind = (3, -pixels)
                        if ext == ".xpm":
                            kind = (4, -pixels)
                        rank = (0 if context.name == "apps" else 1, kind, root_order, size_dir.name, context.name)
                        index.setdefault(stem, []).append((rank, context / name))
        for candidates in index.values():
            candidates.sort()
        self._index = index
        return index

    def _directories(self, root: Path) -> list[Path]:
        """The icon directories an installed theme lists, largest first, without its symbolic ones."""
        cached = self._theme_dirs.get(root)
        if cached is not None:
            return cached
        try:
            text = (root / "index.theme").read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        sizes: dict[str, dict[str, str]] = {}
        section = ""
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
            elif section and "=" in line:
                key, _, value = line.partition("=")
                sizes.setdefault(section, {})[key.strip()] = value.strip()
        ranked = []
        for rel, keys in sizes.items():
            parts = rel.split("/")
            if "Size" not in keys or rel.startswith("/") or ".." in parts or "symbolic" in rel.lower():
                continue
            try:
                pixels = int(keys.get("MaxSize") or keys["Size"]) * int(keys.get("Scale") or 1)
            except ValueError:
                continue
            ranked.append((-pixels, rel))
        found = [root / rel for _, rel in sorted(ranked)]
        self._theme_dirs[root] = found
        return found

    def _themed(self, name: str) -> Path | None:
        for theme in self.themes:
            for base in self.dirs.icon_bases:
                candidates = [
                    folder / f"{name}{ext}" for folder in self._directories(base / theme) for ext in self.EXTENSIONS
                ]
                found = self._first(candidates)
                if found is not None:
                    return found
        return None

    @staticmethod
    def is_published(path: Path) -> bool:
        """True when the file belongs to a theme this script published."""
        return any((folder / MARKER).exists() for folder in Path(os.path.realpath(path)).parents)

    def _first(self, candidates: list[Path]) -> Path | None:
        for candidate in candidates:
            if candidate.is_file() and not self.is_published(candidate):
                return candidate
        return None

    def expand(self, icon: str) -> str:
        return str(self.dirs.home / icon[2:]) if icon.startswith("~/") else icon

    def resolve(self, icon: str) -> Path:
        lookups = self.dirs.lookups
        # A path, or a name reaching into a subdirectory, lands where lookup_inputs does not look.
        if lookups is None or lookups.themes != self.themes or "/" in icon:
            return self._resolve(icon)
        known = lookups.resolved.get(icon, "")
        if known is None:
            raise Skip("not-found")
        if known and os.path.isfile(known):
            return Path(known)
        try:
            found = self._resolve(icon)
        except Skip:
            lookups.found(icon, None)
            raise
        lookups.found(icon, str(found))
        return found

    def _resolve(self, icon: str) -> Path:
        if not icon or "\n" in icon or icon.startswith("image:"):
            raise Skip("not-found")
        icon = self.expand(icon)
        if icon.startswith("/"):
            found = self._first([Path(icon)])
            if found is None:
                raise Skip("not-found")
            return found
        names = [icon]
        stem, ext = os.path.splitext(icon)
        if ext.lower() in self.EXTENSIONS and stem:
            names.append(stem)
        for name in names:
            candidates = [path for _, path in self._hicolor().get(name, [])]
            for base in self.dirs.data:
                candidates += [base / "pixmaps" / f"{name}{ext}" for ext in self.EXTENSIONS]
            for base in self.dirs.icon_bases:
                candidates += [base / f"{name}{ext}" for ext in self.EXTENSIONS]
            found = self._first(candidates)
            if found is not None:
                return found
        # A name only an icon theme has, as with an app that borrows a stock icon.
        for name in names:
            found = self._themed(name)
            if found is not None:
                return found
        raise Skip("not-found")


class Glyphs:
    """One-color glyphs for an app's icon names: our own, then the `<name>-symbolic.svg` an app ships in hicolor,
    then one an installed icon theme has. Names match without regard to case."""

    SUFFIX = "-symbolic.svg"

    def __init__(self, src: Path, dirs: Dirs):
        self.src = src
        self.dirs = dirs
        self._curated: dict[str, Path] | None = None
        self._symbolic: dict[str, Path] | None = None
        self._themed: dict[str, Path] | None = None

    def _curated_index(self) -> dict[str, Path]:
        if self._curated is not None:
            return self._curated
        index: dict[str, Path] = {}
        folder = self.src / MONO_DIR
        try:
            files = sorted(p for p in folder.iterdir() if p.suffix == ".svg")
        except OSError:
            files = []
        for path in files:
            index.setdefault(path.stem.lower(), path)
        aliases_path = self.src / MONO_ALIASES
        if aliases_path.is_file():
            data = load_json(aliases_path)
            apps = data.get("apps") if isinstance(data, dict) else None
            if not isinstance(apps, dict):
                raise BuildError(f'{aliases_path} must be {{"apps": {{"<glyph>": ["<icon name>", ...]}}}}')
            for stem, names in sorted(apps.items()):
                target = index.get(stem.lower())
                if target is None or not isinstance(names, list):
                    raise BuildError(f"{aliases_path}: '{stem}' must name a file in {MONO_DIR} and list icon names")
                for name in names:
                    index.setdefault(str(name).lower(), target)
        self._curated = index
        return index

    def own(self, name: str) -> Path | None:
        path = self.src / OWN_SYMBOLIC_DIR / f"{name}{self.SUFFIX}"
        return path if path.is_file() else None

    def _index(self, folders: list[Path]) -> dict[str, Path]:
        """Icon name -> first `<name>-symbolic.svg` in `folders`, in their order."""
        index: dict[str, Path] = {}
        for folder in folders:
            try:
                names = sorted(os.listdir(folder))
            except OSError:
                continue
            for name in names:
                if name.endswith(self.SUFFIX) and len(name) > len(self.SUFFIX):
                    index.setdefault(name[: -len(self.SUFFIX)].lower(), folder / name)
        return index

    def _symbolic_index(self) -> dict[str, Path]:
        if self._symbolic is None:
            folders = []
            for base in self.dirs.icon_bases:
                folders += [base / "hicolor/symbolic/apps", base / "hicolor/scalable/apps"]
            self._symbolic = self._index(folders)
        return self._symbolic

    @staticmethod
    def theme_app_dirs(root: Path) -> list[Path]:
        """The application directories an installed theme's index.theme lists: symbolic ones first, then the
        scalable ones, then by size, largest first."""
        try:
            text = (root / "index.theme").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        sections: dict[str, dict[str, str]] = {}
        section = ""
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
            elif section and "=" in line:
                key, _, value = line.partition("=")
                sections.setdefault(section, {}).setdefault(key.strip(), value.strip())
        head = sections.get("Icon Theme", {})
        listed = [d.strip() for key in ("Directories", "ScaledDirectories") for d in head.get(key, "").split(",")]
        ranked = []
        for rel in dict.fromkeys(d for d in listed if d):
            keys = sections.get(rel, {})
            parts = rel.split("/")
            if rel.startswith("/") or ".." in parts:
                continue
            if keys.get("Context") != "Applications" and "apps" not in parts:
                continue
            try:
                pixels = int(keys.get("Size") or 0) * int(keys.get("Scale") or 1)
            except ValueError:
                pixels = 0
            symbolic = "symbolic" in rel.lower()
            ranked.append((not symbolic, keys.get("Type") != "Scalable", -pixels, rel))
        return [root / rel for *_, rel in sorted(ranked)]

    @staticmethod
    def theme_roots(dirs: Dirs) -> list[Path]:
        """Installed themes, except hicolor and our own, whose symbolic app icons may stand in; in name order."""
        roots: dict[str, list[Path]] = {}
        for base in dirs.icon_bases:
            try:
                names = sorted(os.listdir(base))
            except OSError:
                continue
            for name in names:
                root = base / name
                if (
                    name == "hicolor"
                    or any(fnmatch.fnmatchcase(name, glob) for glob in OWN_THEME_GLOBS)
                    or not (root / "index.theme").is_file()
                    or (root / MARKER).exists()
                ):
                    continue
                roots.setdefault(name, []).append(root)
        return [root for name in sorted(roots, key=lambda n: (n.casefold(), n)) for root in roots[name]]

    def _theme_index(self) -> dict[str, Path]:
        """Symbolic app icons of every installed theme but hicolor and our own, themes in name order."""
        if self._themed is None:
            folders = []
            for root in self.theme_roots(self.dirs):
                folders += self.theme_app_dirs(root)
            self._themed = self._index(folders)
        return self._themed

    @staticmethod
    def _first(index: dict[str, Path], names: list[str]) -> Path | None:
        for name in names:
            path = index.get(name.lower())
            if path is not None and path.is_file() and not VendorIcons.is_published(path):
                return path
        return None

    def _lookup(self, kind: str, names: list[str], index) -> Path | None:
        """_first of index(), reusing an earlier run's result while lookup_inputs is unchanged; indexing installed
        themes costs far more than a fingerprint."""
        lookups = self.dirs.lookups
        if lookups is None:
            return self._first(index(), names)
        key = "\n".join((kind, *names))
        known = lookups.glyphs.get(key, "")
        if known is None:
            return None
        if known and os.path.isfile(known):
            return Path(known)
        found = self._first(index(), names)
        lookups.found_glyph(key, None if found is None else str(found))
        return found

    def curated(self, names: list[str]) -> Path | None:
        index = self._curated_index()
        return next((index[n.lower()] for n in names if n.lower() in index), None)

    def symbolic(self, names: list[str]) -> Path | None:
        """The app's own symbolic icon, in hicolor."""
        return self._lookup("symbolic", names, self._symbolic_index)

    def theme_symbolic(self, names: list[str]) -> Path | None:
        """A symbolic icon an installed icon theme draws for the app."""
        return self._lookup("theme-symbolic", names, self._theme_index)


# Bump when lookup_inputs or the fingerprint changes what it covers, so no earlier key can match.
LOOKUPS_VERSION = 3
LOOKUPS_FILE = Path("dux-icons") / "lookups.json"


def file_state(path: str) -> str:
    """Changes whenever the file or directory at `path` is replaced, rewritten or removed; "-" when missing."""
    try:
        st = os.stat(path)
    except OSError:
        return "-"
    return f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}"


def _listing(path: str) -> list[str] | None:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return None


def lookup_inputs(dirs: Dirs, themes: tuple[str, ...], src: Path) -> list:
    """What a build reads besides its arguments and the icons it frames, by cheap stats and listings.

    Covers the script, src, desktop entries, the MIME database and every directory a vendor icon lookup searches.
    Icon bases are covered by their listings, since published themes are written there; other directories by their
    own state. Never resolves an icon.
    """
    items: list = [LOOKUPS_VERSION, sys.version, themes, str(src)]
    script = os.path.realpath(__file__)
    items += [(path, file_state(path)) for path in (script, str(INDEX_TEMPLATE))]
    for folder, subdirs, files in os.walk(src):
        subdirs.sort()
        items += [(path, file_state(path)) for path in (os.path.join(folder, name) for name in sorted(files))]
    for base in map(str, dirs.data):
        # The same walk as desktop_entries; an entry symlinked from elsewhere is stated through the link.
        for folder, subdirs, files in os.walk(os.path.join(base, "applications")):
            subdirs.sort()
            items += [
                (path, file_state(path))
                for path in (os.path.join(folder, name) for name in sorted(files) if name.endswith(".desktop"))
            ]
        items.append((os.path.join(base, "pixmaps"), file_state(os.path.join(base, "pixmaps"))))
        items += [(path, file_state(path)) for path in (os.path.join(base, "mime", n) for n in MIME_FILES)]
    for base in map(str, dirs.icon_bases):
        items.append((base, [n for n in _listing(base) or () if n.endswith(VendorIcons.EXTENSIONS)]))
        root = os.path.join(base, "hicolor")
        for size in _listing(root) or ():
            contexts = _listing(os.path.join(root, size))
            items.append((os.path.join(root, size), contexts))
            items += [(path, file_state(path)) for path in (os.path.join(root, size, c) for c in contexts or ())]
        for theme in themes:
            index = os.path.join(base, theme, "index.theme")
            items.append((index, file_state(index)))
            items += [(path, file_state(path)) for path in theme_directories(index)]
    # The symbolic app icons themed and monochrome builds take from installed themes.
    for root in Glyphs.theme_roots(dirs):
        index = str(root / "index.theme")
        items.append((index, file_state(index)))
        items += [(str(path), file_state(str(path))) for path in Glyphs.theme_app_dirs(root)]
    return items


def theme_directories(index: str) -> list[str]:
    """Every directory an installed theme's index.theme has a section for, symbolic ones included."""
    try:
        with open(index, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    root = os.path.dirname(index)
    found = []
    for raw in lines:
        line = raw.strip()
        if line.startswith("[") and line.endswith("]") and line != "[Icon Theme]":
            found.append(os.path.join(root, line[1:-1]))
    return found


class Lookups:
    """Vendor icon and glyph lookups cached from earlier runs, reused while lookup_inputs is unchanged.

    One JSON file under $XDG_CACHE_HOME, replaced atomically. An unreadable or stale file is ignored.
    """

    def __init__(
        self,
        path: Path,
        key: str,
        themes: tuple[str, ...],
        resolved: dict[str, str | None],
        dirty: bool,
        glyphs: dict[str, str | None] | None = None,
    ):
        self.path = path
        self.key = key
        self.themes = themes
        self.resolved = resolved
        self.dirty = dirty
        self.glyphs = {} if glyphs is None else glyphs

    @classmethod
    def load(cls, dirs: Dirs, recipe: dict, src: Path) -> Lookups:
        themes = tuple(str(v) for v in recipe.get("vendorThemes") or ())
        key = hashlib.sha256(repr(lookup_inputs(dirs, themes, src)).encode("utf-8")).hexdigest()
        path = dirs.cache_home / LOOKUPS_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            resolved, glyphs = data["resolved"], data["glyphs"]
            valid = (
                data.get("version") == LOOKUPS_VERSION
                and data.get("key") == key
                and all(
                    isinstance(found, dict) and all(isinstance(v, str) or v is None for v in found.values())
                    for found in (resolved, glyphs)
                )
            )
        except (OSError, ValueError, TypeError, KeyError):
            valid = False
        if not valid:
            return cls(path, key, themes, {}, True)
        return cls(path, key, themes, resolved, False, glyphs)

    def found(self, icon: str, path: str | None) -> None:
        if self.resolved.get(icon, "") != path:
            self.resolved[icon] = path
            self.dirty = True

    def found_glyph(self, key: str, path: str | None) -> None:
        if self.glyphs.get(key, "") != path:
            self.glyphs[key] = path
            self.dirty = True

    def save(self) -> None:
        """Best effort: a run that cannot write the cache still builds."""
        if not self.dirty:
            return
        data = {"version": LOOKUPS_VERSION, "key": self.key, "resolved": self.resolved, "glyphs": self.glyphs}
        data = json.dumps(data, sort_keys=True)
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".lookups-", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data + "\n")
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)


def fingerprint(dirs: Dirs, src: Path) -> str:
    """Hex digest that changes whenever a build with the same arguments could come out different.

    Adds the state of the files vendor and symbolic icons are drawn from to lookup_inputs. The caller compares
    the arguments and the files they name.
    """
    recipe, _, _ = load_recipe(src, None)
    dirs.lookups = Lookups.load(dirs, recipe, src)
    icons = VendorIcons(dirs, dirs.lookups.themes)
    glyphs = Glyphs(src, dirs)
    sources = []
    for name, record in sorted(vendor_apps(dirs, icons, Report()).items()):
        for icon in sorted(record["icons"]):
            try:
                path = str(icons.resolve(icon))
            except Skip:
                path = icons.expand(icon) if is_icon_path(icon) else ""
            sources.append((name, icon, path, file_state(path) if path else "-"))
        names = glyph_names(name, record)
        for kind, path in (("symbolic", glyphs.symbolic(names)), ("theme-symbolic", glyphs.theme_symbolic(names))):
            if path is not None:
                sources.append((name, kind, str(path), file_state(str(path))))
    dirs.lookups.save()
    return hashlib.sha256(repr((dirs.lookups.key, sources)).encode("utf-8")).hexdigest()


def mime_generic_icons(dirs: Dirs) -> dict[str, str]:
    """MIME type -> generic icon name, from the shared MIME database."""
    generic: dict[str, str] = {}
    types: set[str] = set()
    aliases: dict[str, str] = {}
    for base in dirs.data:
        mime = base / "mime"
        for name in MIME_FILES:
            try:
                lines = (mime / name).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if name == "generic-icons":
                    kind, _, icon = line.partition(":")
                    if icon:
                        generic.setdefault(kind.strip(), icon.strip())
                elif name == "types":
                    types.add(line)
                else:
                    alias, _, target = line.partition(" ")
                    if target:
                        aliases.setdefault(alias.strip(), target.strip())
    result: dict[str, str] = {}
    for kind in sorted(types | set(generic) | set(aliases)):
        canonical = aliases.get(kind, kind)
        media, sep, _ = canonical.partition("/")
        if not sep or "/" not in kind:
            continue
        result[kind] = generic.get(canonical) or f"{media}-x-generic"
    return result


def load_palette(args: argparse.Namespace, recipe: dict, dirs: Dirs, src: Path) -> dict[str, str]:
    palette: dict[str, str] = {}
    if args.from_dms:
        path = dirs.cache_home / "dms" / "dms-colors.json"
        data = load_json(path)
        mode = args.mode or data.get("mode") or "dark"
        colors = (data.get("colors") or {}).get(mode)
        if not isinstance(colors, dict):
            raise BuildError(f"{path} has no colors for mode '{mode}'")
        tokens = dict(recipe.get("tokens") or {})
        if "badge" in tokens:
            tokens.setdefault("tile", tokens.pop("badge"))
        roles = {"tile": "primary", "glyph": "on_primary", **tokens}
        for token in ("tile", "glyph", "back"):
            role = roles.get(token)
            if role and colors.get(role):
                palette[token] = normalize_color(colors[role], f"{path}: {role}")
            elif token != "back":
                raise BuildError(f"{path} is missing '{role}' for mode '{mode}'")
    else:
        path = args.palette or src / "palette.json"
        data = load_json(path)
        if not isinstance(data, dict):
            raise BuildError(f"{path} is not a JSON object")
        if "tile" not in data and "badge" in data:
            data = {**data, "tile": data["badge"]}
        for token in ("tile", "glyph", "back"):
            if token in data:
                palette[token] = normalize_color(data[token], f"{path}: {token}")
            elif token != "back" and not getattr(args, token):
                raise BuildError(f"{path} is missing '{token}'")
    for token in ("tile", "glyph", "back"):
        value = getattr(args, token)
        if value:
            palette[token] = normalize_color(value, f"--{token}")
    palette.setdefault("back", darker(palette["tile"]))
    return palette


def load_tiles(path: Path, palette: dict[str, str]) -> dict:
    """Palettes that override the theme's for some icons: a JSON object with up to three optional keys.

        "apps": {"<icon name>": TILE}   an installed app's tiled icon; never a dev.duxlabs.* app or own icon
        "shell": TILE                   our own app icons from src/own/apps (the DMS built-ins)
        "dux": TILE + "tint"?: hex      the tiled icons of installed dev.duxlabs.* apps

    TILE is {"tile": hex, "glyph"?: hex, "back"?: hex, "ring"?: hex, "reach"?: number, "cutout"?: path, "bare"?: hex}.
    glyph defaults to the global one, back to the tile 12 tones darker. ring is a line just inside the tile edge.
    reach, above 0, is how far the icon's farthest opaque pixel lies from its center, as a fraction of its half-size;
    the icon then grows to fill a flat tile. cutout is a PNG of the mark (white, mark in alpha), centered with
    CUTOUT_MARGIN; bare is the glyph color of the tile-free copy. Both apply to the themed styles. A dux tile's tint
    (default: its tile color) is the hue our apps' art is recolored to.

    Returns {"apps": {name: tile}} plus "shell" and "dux" when given.
    """
    data = load_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("apps", {}), dict):
        raise BuildError(f'{path} must be a JSON object; its "apps", if any, an object of icon names')
    if not {"apps", "shell", "dux"} & set(data):
        raise BuildError(f'{path} needs at least one of "apps", "shell" or "dux"')
    tiles: dict = {
        "apps": {name: parse_tile(spec, f"{path}: apps.{name}", palette) for name, spec in data.get("apps", {}).items()}
    }
    for key in ("shell", "dux"):
        if key in data:
            tiles[key] = parse_tile(data[key], f"{path}: {key}", palette)
    if "dux" in tiles:
        tint = data["dux"].get("tint", tiles["dux"]["tile"])
        tiles["dux"]["tint"] = normalize_color(tint, f"{path}: dux.tint")
    return tiles


def parse_tile(spec, where: str, palette: dict[str, str]) -> dict:
    """One tile of load_tiles, its defaults filled in; `where` names it in errors."""
    if not isinstance(spec, dict) or "tile" not in spec:
        raise BuildError(f"{where} needs a tile color")
    tile = {"tile": normalize_color(spec["tile"], f"{where}.tile")}
    tile["glyph"] = normalize_color(spec.get("glyph", palette["glyph"]), f"{where}.glyph")
    tile["back"] = normalize_color(spec.get("back", darker(tile["tile"])), f"{where}.back")
    for key in ("ring", "bare"):
        if key in spec:
            tile[key] = normalize_color(spec[key], f"{where}.{key}")
    if "reach" in spec:
        reach = spec["reach"]
        if isinstance(reach, bool) or not isinstance(reach, (int, float)) or not 0 < reach < math.inf:
            raise BuildError(f"{where}.reach must be a number above 0: {reach!r}")
        tile["reach"] = float(reach)
    # Drawn only by the themed and monochrome styles, which fall through when it cannot be read.
    if isinstance(spec.get("cutout"), str):
        tile["cutout"] = spec["cutout"]
    return tile


def shapes(src: Path) -> list[str]:
    try:
        return sorted(p.stem for p in (src / "templates").iterdir() if p.suffix == ".svg")
    except OSError:
        return []


def read_template(src: Path, name: str) -> tuple[Path, str]:
    """(path, text) of a tile shape, checked but with its tokens still in place."""
    if not NAME_RE.match(str(name)):
        raise BuildError(f"bad shape name: {name!r}")
    path = src / "templates" / f"{name}.svg"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise BuildError(f"no shape '{name}'; there is {', '.join(shapes(src)) or 'none'}") from None
    except OSError as exc:
        raise BuildError(f"cannot read template {path}: {exc}") from None
    if text.count("@content") != 1:
        raise BuildError(f"{path} must contain @content exactly once")
    head = re.search(r"<svg\b[^>]*>", text)
    if not head or "xmlns:xlink=" not in head.group(0):
        raise BuildError(f"{path}: the root <svg> must declare xmlns:xlink")
    viewbox = re.search(r'viewBox="([^"]*)"', head.group(0))
    if not viewbox or parse_viewbox(viewbox.group(1)) != (0.0, 0.0, CANVAS, CANVAS):
        raise BuildError(f'{path}: the template canvas must be viewBox="0 0 64 64"')
    if re.search(r'\bid="(?:c-|dx-)', text):
        raise BuildError(f"{path}: ids starting with c- or dx- are reserved for content")
    return path, text


def paint_template(path: Path, text: str, palette: dict[str, str], gradient: bool = False) -> str:
    """A template from read_template in the palette's colors, its gradient made from the palette tile."""
    if gradient:
        head = re.search(r"<svg\b[^>]*>", text)
        up, down = GRADIENT_TONES
        text = (
            f'{text[: head.end()]}<linearGradient id="{FILL_ID}" gradientUnits="userSpaceOnUse" '
            f'x1="0" y1="0" x2="0" y2="{fmt(CANVAS)}"><stop offset="0" stop-color="{darker(palette["tile"], -up)}"/>'
            f'<stop offset="1" stop-color="{darker(palette["tile"], down)}"/></linearGradient>{text[head.end() :]}'
        )
        palette = {**palette, "tile": f"url(#{FILL_ID})"}
    painted = paint_tokens(text.strip(), palette)
    stray = sorted(set(STRAY_TOKEN_RE.findall(painted)) - {"@content"})
    if stray:
        raise BuildError(f"{path}: unresolved tokens: {', '.join(stray)}")
    return painted + "\n"


def load_template(src: Path, name: str, palette: dict[str, str], gradient: bool = False) -> str:
    return paint_template(*read_template(src, name), palette, gradient)


def ring_markup(path: Path, text: str, color: str) -> str:
    """A RING_WIDTH line of `color` just inside the tile shape of template `text`, masked to that shape."""
    shapes = TILE_SHAPE_RE.findall(text)
    if not shapes:
        raise BuildError(f'{path}: a ring needs the tile drawn by self-closing elements with fill="@tile"')
    fill = "".join(shape.replace('fill="@tile"', 'fill="#fff"') for shape in shapes)
    stroke = f'fill="none" stroke="{color}" stroke-width="{fmt(2 * RING_WIDTH)}"'
    lines = "".join(shape.replace('fill="@tile"', stroke) for shape in shapes)
    # A mask, not a clip-path: Qt SVG ignores clip-path, which would leave the outer half of the stroke to the
    # viewBox edge.
    size = fmt(CANVAS)
    return (
        f'<mask id="{SHAPE_ID}" maskUnits="userSpaceOnUse" x="0" y="0" width="{size}" height="{size}">{fill}</mask>'
        f'<g mask="url(#{SHAPE_ID})">{lines}</g>'
    )


class Templates:
    """One tile shape, painted once for each palette that asks for it.

    A palette's `ring` is drawn over the content, and a palette with a `reach` gets a flat tile.
    """

    def __init__(self, src: Path, name: str, gradient: bool):
        self.path, self.text = read_template(src, name)
        self.gradient = gradient
        self.inset = INSET_RADII.get(name, INSET_DEFAULT)
        self._painted: dict[tuple, str] = {}

    def get(self, palette: dict) -> str:
        ring = palette.get("ring")
        gradient = self.gradient and "reach" not in palette
        key = (palette["tile"], palette["glyph"], palette["back"], ring, gradient)
        if key not in self._painted:
            text = self.text
            if ring:
                text = text.replace("@content", "@content" + ring_markup(self.path, text, ring))
            self._painted[key] = paint_template(self.path, text, palette, gradient)
        return self._painted[key]


def reach_scale(reach: float, slot: Slot, inset: float, ring: bool) -> float:
    """Growth about the canvas center that puts content of this `reach` REACH_MARGIN inside the tile or ring."""
    half = slot.size / 2
    # An offset slot moves the content off center, so its farthest pixel can be that much farther out.
    off = math.hypot(slot.x + half - CANVAS / 2, slot.y + half - CANVAS / 2)
    room = inset - REACH_MARGIN - (RING_WIDTH if ring else 0.0)
    low, high = REACH_SCALE
    return min(high, max(low, room / (reach * half + off)))


def grow(content: str, scale: float) -> str:
    """Content scaled about the canvas center."""
    if scale == 1.0:
        return content
    shift = fmt(CANVAS / 2 * (1 - scale))
    return f'<g transform="translate({shift} {shift}) scale({fmt(scale, 6)})">{content}</g>'


def override_rules(recipe: dict) -> list[tuple[list[str], dict]]:
    """Recipe `overrides`, a list of `{"match": pattern or [patterns], ...values}`, as (patterns, values) in file order."""
    raw = recipe.get("overrides") or []
    rules = []
    for rule in raw if isinstance(raw, list) else [raw]:
        match = rule.get("match") if isinstance(rule, dict) else None
        patterns = [match] if isinstance(match, str) else match
        if not isinstance(patterns, list) or not patterns or not all(isinstance(p, str) for p in patterns):
            raise BuildError("recipe 'overrides': each entry needs \"match\": a pattern or a list of patterns")
        rules.append((patterns, {key: value for key, value in rule.items() if key != "match"}))
    return rules


def layout_for(context: str, name: str, recipe: dict, own: bool) -> Slot:
    """Slot for one icon; recipe override globs match the name or `context/name`."""
    layout = recipe.get("layout") or {}
    spec: dict = {}
    for patterns, values in override_rules(recipe):
        if any(fnmatch.fnmatchcase(name, p) or fnmatch.fnmatchcase(f"{context}/{name}", p) for p in patterns):
            spec.update(values)
    try:
        default = layout.get("ownPadding", 0.2) if own else layout.get("vendorPadding", 0.16)
        padding = float(spec.get("padding", default))
        offset = tuple(float(v) for v in spec.get("offset", layout.get("offset") or (0, 0)))
        if len(offset) != 2:
            raise ValueError("offset needs two numbers")
    except (TypeError, ValueError) as exc:
        raise BuildError(f"recipe layout for '{context}/{name}': {exc}") from None
    return Slot(padding, offset)


class Theme:
    """The published theme in memory: files and symlinks by relative path."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.links: dict[str, str] = {}
        self.names: dict[str, str] = {}
        # `Icon=` values that are file paths, which no theme lookup reaches, by the icon built from them.
        self.paths: dict[str, str] = {}

    def add_icon(self, context: str, name: str, svg: str, bare: str = "") -> None:
        rel = f"{SCALABLE}/{context}/{name}.svg"
        self.files[rel] = svg.encode("utf-8")
        self.names[name] = rel
        if bare:
            self.files[f"{BARE}/{context}/{name}.svg"] = assemble(BARE_TEMPLATE, bare).encode("utf-8")

    def add_alias(self, name: str, target: str, context: str | None = None) -> bool:
        """Symlink `name` to icon `target`; False when it cannot or need not be made."""
        if name in self.names or target not in self.names or not NAME_RE.match(name):
            return False
        real = self.names[target]
        folder = f"{SCALABLE}/{context}" if context else os.path.dirname(real)
        self.links[f"{folder}/{name}.svg"] = os.path.relpath(real, folder)
        self.names[name] = real
        bare_real = BARE + real.removeprefix(SCALABLE)
        if bare_real in self.files:
            bare_folder = BARE + folder.removeprefix(SCALABLE)
            self.links[f"{bare_folder}/{name}.svg"] = os.path.relpath(bare_real, bare_folder)
        return True

    def contexts(self) -> list[str]:
        paths = list(self.files) + list(self.links)
        return sorted({p.split("/")[1] for p in paths if p.startswith(f"{SCALABLE}/")})


def render_index(template: str, name: str, inherits: list[str], contexts: list[str]) -> str:
    if not name.strip() or re.search(r"[\x00-\x1f]", name):
        raise BuildError(f"--name cannot be written to index.theme: {name!r}")
    directories = [f"{SCALABLE}/{context}" for context in contexts]
    sections = []
    for context in contexts:
        sections.append(
            f"[{SCALABLE}/{context}]\n"
            f"Context={CONTEXTS.get(context, context.capitalize())}\n"
            f"Size={SCALABLE_SIZE}\nMinSize={SCALABLE_MIN}\nMaxSize={SCALABLE_MAX}\nType=Scalable\n"
        )
    text, count = re.subn(r"(?m)^Name=.*$", lambda m: f"Name={name}", template, count=1)
    if not count:
        raise BuildError("index.theme.in has no Name= line")
    for token, value in (
        ("@inherits", ",".join(inherits)),
        ("@directories", ",".join(directories)),
        ("@sections", "\n".join(sections).rstrip("\n")),
    ):
        if token not in text:
            raise BuildError(f"index.theme.in is missing {token}")
        text = text.replace(token, value)
    return text.rstrip("\n") + "\n"


def index_name(template: str) -> str:
    match = re.search(r"(?m)^Name=(.*)$", template)
    return match.group(1).strip() if match else PRODUCT


class Report:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.skips: dict[str, dict[str, list[str]]] = {"own": {}, "vendor": {}}
        self.warnings: list[str] = []
        # App icons by the GLYPH_STAGES stage that drew them in the themed and monochrome styles.
        self.stages: dict[str, list[str]] = {}

    def stage(self, stage: str, name: str) -> None:
        self.stages.setdefault(stage, []).append(name)

    def add(self, key: str, amount: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + amount

    def get(self, key: str) -> int:
        return self.counts.get(key, 0)

    def skip(self, group: str, reason: str, name: str) -> None:
        self.skips[group].setdefault(reason, []).append(name)


def assemble(template: str, content: str) -> str:
    return template.replace("@content", content)


def color_rule(rel: str) -> dict:
    """The COLOR_RULES entry for own icon `context/name`."""
    for patterns, rule in COLOR_RULES:
        if any(fnmatch.fnmatchcase(rel, pattern) for pattern in patterns):
            return rule
    return {}


def file_category(name: str) -> str:
    """The FILE_CATEGORIES key of own mimetype `name`."""
    for key, category in FILE_CATEGORIES.items():
        if name in category["icons"]:
            return key
    return "text"


def color_tones(context: str, name: str, palette: dict[str, str], table: dict = COLOR_TONES) -> dict[str, str]:
    """Role -> color of `table` for one color-style icon: places in the theme hue, files in their category's
    harmonized hue."""
    _, chroma, hue = lch_of(palette["tile"])
    scale = min(1.0, chroma / COLOR_THEME_CHROMA)
    if context == "mimetypes":
        category = FILE_CATEGORIES[file_category(name)]
        if category["hue"] is None:
            scale *= category["chroma"]
        else:
            scale = category["chroma"]
            hue = category["hue"] if chroma < COLOR_HUELESS else harmonize(category["hue"], hue)
    return {role: lch(lightness, tone_chroma * scale, hue) for role, (lightness, tone_chroma) in table.items()}


def _token_of(element: ET.Element) -> str | None:
    match = TOKEN_RE.fullmatch((element.get("fill") or "").strip())
    return match.group(1) if match else None


def _markup(element: ET.Element, paint, extra: str = "") -> str:
    """`element` as markup without ids, its tokens painted by `paint` (token -> color), `extra` attributes added."""
    namespace, local = _split_tag(element.tag)
    if namespace not in (SVG_NS, ""):
        return ""
    attrs = []
    for key, value in element.attrib.items():
        space, name = _split_tag(key)
        if name == "id" or space not in ("", XLINK_NS):
            continue
        value = TOKEN_RE.sub(lambda m: paint(m.group(1)), value)
        attrs.append(f' {"xlink:" if space else ""}{name}="{esc_attr(value)}"')
    children = "".join(_markup(child, paint) for child in element)
    text = esc_text(element.text or "") if children or (element.text or "").strip() else ""
    head = f"<{local}{''.join(attrs)}{extra}"
    return f"{head}>{text}{children}</{local}>" if children or text else f"{head}/>"


def _linear_gradient(gradient_id: str, x1: float, y1: float, x2: float, y2: float, stops, tones: dict) -> str:
    """A userSpaceOnUse <linearGradient> of (offset, role, opacity or None) stops in `tones`."""
    markup = "".join(
        f'<stop offset="{fmt(offset)}" stop-color="{tones[role]}"'
        + ("" if opacity is None else f' stop-opacity="{fmt(opacity)}"')
        + "/>"
        for offset, role, opacity in stops
    )
    return (
        f'<linearGradient id="{gradient_id}" gradientUnits="userSpaceOnUse" x1="{fmt(x1)}" y1="{fmt(y1)}" '
        f'x2="{fmt(x2)}" y2="{fmt(y2)}">{markup}</linearGradient>'
    )


def color_folder(root: ET.Element, kind: str, context: str, name: str, palette: dict[str, str]) -> str:
    """A color-style folder of FOLDERS `kind`, drawn on the canvas in FOLDER_TONES.

    On a closed folder, the art's elements after its back and front are type marks, moved onto the front by
    FOLDER_MARK: an @tile cut-out shows the front gradient, everything else takes the mark tone.
    """
    folder = FOLDERS[kind]
    tones = color_tones(context, name, palette, FOLDER_TONES)
    gradients = list(folder["gradients"])
    elements = [child for child in root if _split_tag(child.tag)[1] not in _DROP_ELEMENTS | {"defs"}]
    marks = elements[2:] if kind == "closed" else []
    factor, (fx, fy), (tx, ty) = FOLDER_MARK

    def local(x: float, y: float) -> tuple[float, float]:
        return (x - tx) / factor + fx, (y - ty) / factor + fy

    if marks:
        # The front gradient again in the marks' own units, so that a cut-out shows the front lying behind it.
        _, x1, y1, x2, y2, stops = next(gradient for gradient in gradients if gradient[0] == "front")
        gradients.append(("cut", *local(x1, y1), *local(x2, y2), stops))
    size = fmt(CANVAS)
    defs = "".join(_linear_gradient(*gradient, tones) for gradient in gradients) + "".join(
        f'<mask id="m-{part}" maskUnits="userSpaceOnUse" x="0" y="0" width="{size}" height="{size}">'
        f'<path d="{folder[part]}" fill="#fff"/></mask>'
        for part in ("back", "front")
    )
    layers = "".join(layer.format(back=folder["back"], front=folder["front"], **tones) for layer in folder["layers"])
    drawn = "".join(_markup(mark, lambda token: "url(#cut)" if token == "tile" else tones["mark"]) for mark in marks)
    if drawn:
        layers += (
            f'<g transform="translate({fmt(tx)} {fmt(ty)}) scale({fmt(factor)}) translate({fmt(-fx)} {fmt(-fy)})">'
            f"{drawn}</g>"
        )
    document = f'<svg xmlns="{SVG_NS}" viewBox="0 0 {size} {size}"><defs>{defs}</defs>{layers}</svg>'
    return assemble(BARE_TEMPLATE, inline_svg(parse_svg(document.encode("utf-8")), Slot(0.0, (0.0, 0.0)), clip=False))


def color_icon(text: str, context: str, name: str, palette: dict[str, str]) -> str:
    """Own icon art drawn in the color style: no tile, filling the canvas, a rim just inside each outline.

    The art's leading elements take the roles of its COLOR_RULES frame; the rest are type marks.
    """
    root = parse_svg(text.encode("utf-8"))
    rule = color_rule(f"{context}/{name}")
    if "folder" in rule:
        return color_folder(root, rule["folder"], context, name, palette)
    tones = color_tones(context, name, palette)
    viewbox = parse_viewbox(root.get("viewBox")) or (0.0, 0.0, 24.0, 24.0)
    x0, y0, x1, y1 = rule.get("box", (viewbox[0], viewbox[1], viewbox[0] + viewbox[2], viewbox[1] + viewbox[3]))
    scale = CANVAS * COLOR_FILL / max(x1 - x0, y1 - y0)
    size = max(viewbox[2], viewbox[3]) * scale
    inset = (CANVAS - size) / 2
    slot = Slot(
        inset / CANVAS,
        (
            CANVAS / 2 - inset - ((x0 + x1) / 2 - viewbox[0]) * scale,
            CANVAS / 2 - inset - ((y0 + y1) / 2 - viewbox[1]) * scale,
        ),
    )
    rim = fmt(2 * COLOR_RIM / scale)
    span = (fmt(viewbox[0] - viewbox[2]), fmt(viewbox[1] - viewbox[3]), fmt(3 * viewbox[2]), fmt(3 * viewbox[3]))

    elements = [child for child in root if _split_tag(child.tag)[1] not in _DROP_ELEMENTS | {"defs"}]
    if rule.get("split") and elements and elements[0].get("d"):
        first = elements.pop(0)
        for d in reversed(re.split(r"(?=M)", first.get("d").strip())):
            if d.strip():
                piece = ET.Element(first.tag, first.attrib)
                piece.set("d", d.strip())
                elements.insert(0, piece)
    frame = rule.get("frame", ("body",))[: len(elements)]
    body_token = next((_token_of(el) for el, role in zip(elements, frame) if role == "body" and _token_of(el)), "glyph")
    fills = {"body": "url(#body)", "back": tones["back"], "fold": tones["fold"], "solid": tones["rim"]}
    # Across the art's box, so cut-outs in the marks match the body behind them.
    gradient = (
        f'<defs><linearGradient id="body" gradientUnits="userSpaceOnUse" x1="{fmt(x0)}" y1="{fmt(y0)}" '
        f'x2="{fmt(x1)}" y2="{fmt(y1)}"><stop offset="0" stop-color="{tones["top"]}"/>'
        f'<stop offset="1" stop-color="{tones["bottom"]}"/></linearGradient></defs>'
    )
    parts = [gradient]
    for index, (element, role) in enumerate(zip(elements, frame)):
        if role is None:
            continue
        fill = fills[role]
        if role in ("body", "back"):
            # A doubled stroke whose outer half a mask of the same shape cuts away: Qt SVG ignores clip-path.
            parts.append(
                f'<mask id="rim{index}" maskUnits="userSpaceOnUse" x="{span[0]}" y="{span[1]}" width="{span[2]}" '
                f'height="{span[3]}">{_markup(element, lambda _: "#fff")}</mask>'
            )
            extra = f' stroke="{tones["rim"]}" stroke-width="{rim}" stroke-linejoin="round" mask="url(#rim{index})"'
            parts.append(_markup(element, lambda _, fill=fill: fill, extra))
        else:
            parts.append(_markup(element, lambda _, fill=fill: fill))

    marks = {"glyph": tones["mark"], "tile": tones["mark"], "back": tones["fold"], body_token: "url(#body)"}
    rest = "".join(_markup(element, marks.get) for element in elements[len(frame) :])
    body = next((element for element, role in zip(elements, frame) if role == "body"), None)
    if rest and body is not None:
        # Type marks stay inside the body's rim; a zipper running to the top edge would paint over it.
        cut = f' stroke="#000" stroke-width="{rim}" stroke-linejoin="round"'
        parts.append(
            f'<mask id="marks" maskUnits="userSpaceOnUse" x="{span[0]}" y="{span[1]}" width="{span[2]}" '
            f'height="{span[3]}">{_markup(body, lambda _: "#fff", cut)}</mask>'
        )
        rest = f'<g mask="url(#marks)">{rest}</g>'
    parts.append(rest)
    document = f'<svg xmlns="{SVG_NS}" viewBox="{" ".join(fmt(v) for v in viewbox)}">{"".join(parts)}</svg>'
    return assemble(BARE_TEMPLATE, inline_svg(parse_svg(document.encode("utf-8")), slot, clip=False))


def build_own(
    theme: Theme,
    recipe: dict,
    src: Path,
    template: str,
    palette: dict[str, str],
    report: Report,
    shell: dict | None = None,
    templates: Templates | None = None,
    files_style: str = "standard",
    app_style: str = "color",
) -> None:
    """Own icons on `template`; with a `shell` tile, painted from `templates`, the apps in its colors instead.

    In the color `files_style`, places, mimetypes and status icons are drawn by color_icon, without the tile.
    In the themed and monochrome `app_style`, our app icons are sized like every other app glyph.
    """
    for path in sorted((src / "own").glob("*/*.svg")):
        context, name = path.parent.name, path.stem
        rel = f"{context}/{name}"
        tile = shell if context == "apps" else None
        try:
            if not (NAME_RE.match(context) and NAME_RE.match(name)):
                raise Skip("bad-name")
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                raise Skip("unreadable") from None
            painted = paint_tokens(text, tile or palette)
            if STRAY_TOKEN_RE.search(painted):
                raise Skip("unknown-token")
            if files_style == "color" and context in COLOR_CONTEXTS:
                theme.add_icon(context, name, color_icon(text, context, name, palette))
                report.add("own")
                report.add("own-color")
                continue
            slot = layout_for(context, name, recipe, own=True)
            if app_style in GLYPH_STYLES and context == "apps":
                content = fit_glyph(parse_svg(painted.encode("utf-8")), GLYPH_SIZE, clip=False)
                report.stage("own", name)
            else:
                content = inline_svg(parse_svg(painted.encode("utf-8")), slot, clip=False)
            if tile and "reach" in tile and app_style == "color":
                content = grow(content, reach_scale(tile["reach"], slot, templates.inset, "ring" in tile))
            bare = ""
            if context == "apps":
                # Off the tile, the glyph takes the bare color (else the tile color) to read on a panel.
                untiled = paint_tokens(text, {**palette, "glyph": (tile or {}).get("bare", palette["tile"])})
                bare = inline_svg(parse_svg(untiled.encode("utf-8")), Slot(0.0, (0.0, 0.0)), clip=False)
        except Skip as skip:
            report.skip("own", skip.reason, rel)
            continue
        theme.add_icon(context, name, assemble(templates.get(tile) if tile else template, content), bare)
        report.add("own")
        if tile:
            report.add("tiles-shell")
            report_tile(report, tile)


def report_tile(report: Report, tile: dict) -> None:
    for key in ("ring", "reach"):
        if key in tile:
            report.add(f"tiles-{key}")


def build_own_aliases(theme: Theme, src: Path, report: Report) -> None:
    path = src / "aliases.json"
    if not path.is_file():
        return
    data = load_json(path)
    if not isinstance(data, dict):
        raise BuildError(f"{path} is not a JSON object")
    own_icons = set(theme.names)
    for context in sorted(data):
        mapping = data[context]
        if not isinstance(mapping, dict) or not NAME_RE.match(context):
            raise BuildError(f"{path}: '{context}' must map icon names to lists of aliases")
        for target in sorted(mapping):
            for alias in sorted(str(a) for a in mapping[target] or []):
                if alias in own_icons:
                    report.warnings.append(
                        f"aliases.json: '{alias}' (alias of '{target}') is already an own icon; remove the alias"
                    )
                elif alias in theme.names:
                    report.warnings.append(f"aliases.json: '{alias}' is listed more than once; '{target}' is ignored")
                elif theme.add_alias(alias, target, context):
                    report.add("alias-own")
                else:
                    report.skip("own", "alias-without-target", f"{alias} -> {target}")


def is_icon_path(icon: str) -> bool:
    return icon.startswith(("/", "~/"))


def vendor_apps(dirs: Dirs, icons: VendorIcons, report: Report) -> dict[str, dict]:
    """The app icons the desktop entries ask for: icon name -> {"icons": Icon= values, "aliases": entry ids}."""
    entries: list[tuple[str, str, str]] = []
    for entry_id, entry in desktop_entries(dirs):
        report.add("entries")
        if entry.get("Type", "Application") != "Application":
            report.add("entries-not-app")
            continue
        # NoDisplay only keeps an app out of menus; its windows still show the icon in a dock or switcher.
        if entry.get("Hidden", "").lower() == "true":
            report.add("entries-hidden")
            continue
        icon = entry.get("Icon", "")
        if not icon:
            report.add("entries-no-icon")
            continue
        report.add("entries-visible")
        name = os.path.basename(icon) if is_icon_path(icon) else icon
        stem, ext = os.path.splitext(name)
        if ext.lower() in VendorIcons.EXTENSIONS and stem:
            name = stem
        entries.append((name, icon, entry_id))

    files: dict[str, set[str]] = {}
    for name, icon, _ in entries:
        if is_icon_path(icon):
            files.setdefault(name, set()).add(os.path.realpath(icons.expand(icon)))
    # Several Icon= values can name one icon (a name and a path).
    wanted: dict[str, dict] = {}
    for name, icon, entry_id in entries:
        # Two files with one basename are two icons; each goes by its desktop entry instead.
        if is_icon_path(icon) and len(files[name]) > 1:
            name = entry_id
        record = wanted.setdefault(name, {"icons": set(), "aliases": set()})
        record["icons"].add(icon)
        if entry_id != name:
            record["aliases"].add(entry_id)
    return wanted


def vendor_source(name: str, record: dict, icons: VendorIcons, art: Path, taken, load) -> tuple[Path, object]:
    """(path, load(path)) for the first source of app icon `name` that `load` accepts.

    Our art in `art` comes first, then each Icon= value, plain names before paths so the theme lookup wins
    over a pinned file. Raises the first Skip when no source works.
    """
    if not NAME_RE.match(name):
        raise Skip("bad-name")
    if name in taken:
        raise Skip("own-icon")
    override = art / f"{name}.svg"
    sources = [lambda: override] if override.is_file() else []
    sources += [
        lambda icon=icon: icons.resolve(icon)
        for icon in sorted(record["icons"], key=lambda value: (is_icon_path(value), value))
    ]
    failure: Skip | None = None
    for source in sources:
        try:
            path = source()
            return path, load(path)
        except Skip as skip:
            failure = failure or skip
    raise failure


def _read_svg(path: Path) -> ET.Element:
    try:
        if path.stat().st_size > MAX_SOURCE_BYTES:
            raise Skip("too-large")
        data = path.read_bytes()
    except OSError:
        raise Skip("unreadable") from None
    if sniff(data) != "svg":
        raise Skip("not-svg")
    return parse_svg(data)


def glyph_names(name: str, record: dict) -> list[str]:
    """The names an app's glyph may go by, in the order they are tried: its icon name, the name of each Icon=
    value, then its desktop entry ids."""
    names = [name]
    for icon in sorted(record["icons"]):
        stem, ext = os.path.splitext(os.path.basename(icon))
        names.append(stem if ext.lower() in VendorIcons.EXTENSIONS and stem else os.path.basename(icon))
    return names + sorted(record["aliases"])


def app_glyph(
    name: str,
    record: dict,
    path: Path,
    glyphs: Glyphs,
    tile: dict,
    palette: dict[str, str],
    duotone: tuple[str, str],
    contrast: float,
) -> tuple[str, str, str]:
    """(stage, content, bare) of one app icon in the themed styles, from the first GLYPH_STAGES stage that can
    draw it. `path` is the icon's source and `duotone` its framed and bare content, for the last stage."""
    color, bare_color = tile["glyph"], tile.get("bare", palette["tile"])

    def drawn(root: ET.Element, alpha_row: tuple[float, ...] = SOLID_ALPHA) -> tuple[str, str]:
        return (
            glyph_wrap(fit_glyph(root, GLYPH_SIZE), color, alpha_row),
            glyph_wrap(fit_glyph(root, BARE_GLYPH_SIZE), bare_color, alpha_row),
        )

    names = glyph_names(name, record)
    source: ET.Element | None = None
    try:
        source = _read_svg(path)
    except Skip:
        pass
    own = glyphs.own(name) if name.startswith(OUR_APPS) else None
    curated = glyphs.curated(names)
    symbolic = path if path.name.endswith(Glyphs.SUFFIX) else glyphs.symbolic(names)
    for stage, found in (("own", own), ("curated", curated), ("symbolic", symbolic)):
        if found is not None:
            try:
                return (stage, *drawn(clean_symbolic(_read_svg(found))))
            except Skip:
                pass
    # Installed themes are read only once nothing of the app's own or ours drew it.
    themed = glyphs.theme_symbolic(names)
    if themed is not None:
        try:
            return ("theme-symbolic", *drawn(clean_symbolic(_read_svg(themed))))
        except Skip:
            pass
    if source is not None:
        try:
            stripped = _read_svg(path)
            if strip_svg_plate(stripped):
                return ("plate", *drawn(stripped))
        except Skip:
            pass
    if tile.get("cutout"):
        try:
            return (
                "cutout",
                cutout_markup(tile["cutout"], GLYPH_SIZE, color),
                cutout_markup(tile["cutout"], BARE_GLYPH_SIZE, bare_color),
            )
        except Skip:
            pass
    if source is not None:
        try:
            mode, alpha_row = svg_tint(source)
            return (mode, *drawn(source, alpha_row))
        except Skip:
            pass
    content, bare = duotone
    # No tile to keep light content off, so the bare tones span up to the bare glyph color.
    bare = mono_wrap(bare, {**palette, "tile": bare_color}, contrast, "tile")
    return "duotone", mono_wrap(content, tile, contrast, "back"), bare


def deep_tile(tile: dict) -> dict:
    """`tile` for our apps' art: as it is, or above OWN_LIGHT_TILE moved to OWN_DEEP_TONES in its hue, its chroma
    raised by OWN_DEEP_CHROMA and any ring kept. A ringed tile is near-neutral by design, so the ring's color,
    the theme's, gives the deep tile its hue and chroma instead."""
    if rgb_to_lab(parse_hex(tile["tile"]))[0] <= OWN_LIGHT_TILE:
        return tile
    if "ring" in tile:
        _, chroma, hue = lch_of(tile["ring"])
    else:
        _, chroma, hue = lch_of(tile["tile"])
        chroma *= OWN_DEEP_CHROMA
    front = lch(OWN_DEEP_TONES[0], chroma, hue)
    return {**tile, "tile": front, "back": darker(front), "glyph": toned(front, OWN_DEEP_TONES[1])}


def build_vendor(
    theme: Theme,
    recipe: dict,
    dirs: Dirs,
    templates: Templates,
    palette: dict[str, str],
    app_style: str,
    report: Report,
    art: Path,
    tiles: dict | None = None,
    own_tone: float | None = None,
) -> None:
    """Installed apps' icons; `tiles` as load_tiles returns them. `art` is ART_DIR under the source tree."""
    themes = tuple(str(v) for v in recipe.get("vendorThemes") or ())
    icons = VendorIcons(dirs, themes)
    glyphs = Glyphs(art.parent.parent, dirs)
    contrast = float(recipe.get("monoContrast", 1.0))
    wanted = vendor_apps(dirs, icons, report)
    tiles = tiles or {}
    apps, dux = tiles.get("apps") or {}, tiles.get("dux")
    tint = dux["tint"] if dux else palette["tile"]

    pending_aliases: list[tuple[str, str]] = []
    for name in sorted(wanted):
        record = wanted[name]
        report.add("vendor-icons")

        def load(path: Path, name: str = name) -> tuple[tuple[str, str], str]:
            built = vendor_content(path, layout_for("apps", name, recipe, own=False))
            return built, vendor_content(path, Slot(0.0, (0.0, 0.0)))[1]

        try:
            path, ((kind, content), bare) = vendor_source(name, record, icons, art, theme.names, load)
        except Skip as skip:
            report.skip("vendor", skip.reason, name)
            continue
        ours = name.startswith(OUR_APPS)
        # Our own apps take the dux tile, if any, never an app's; no tile colors a bare copy.
        tile = dux if ours else apps.get(name)
        # The glyph styles put every app on one tile, so an app the tiles leave out still wears the uniform one.
        tiled = tile or (tiles.get("shell") if app_style in GLYPH_STYLES else None) or palette
        if app_style in GLYPH_STYLES:
            stage, content, bare = app_glyph(name, record, path, glyphs, tiled, palette, (content, bare), contrast)
            report.stage(stage, name)
        elif tile and "reach" in tile:
            slot = layout_for("apps", name, recipe, own=False)
            content = grow(content, reach_scale(tile["reach"], slot, templates.inset, "ring" in tile))
        if app_style == "tinted":
            content = mono_wrap(content, tiled, contrast, "back")
            # No tile to keep light content off, so the tones span up to the tile color.
            bare = mono_wrap(bare, palette, contrast, "tile")
        elif app_style == "color" and ours:
            tiled = deep_tile(tiled)
            content = retone(content, tint, own_tone)
            bare = retone(bare, palette["tile"])
        theme.add_icon("apps", name, assemble(templates.get(tiled), content), bare)
        theme.paths.update((icon, name) for icon in record["icons"] if is_icon_path(icon))
        report.add(f"vendor-{kind}")
        if path.parent == art:
            report.add("vendor-art")
        if tile:
            report.add("tiles-dux" if ours else "tiles")
            report_tile(report, tile)
        pending_aliases += [(alias, name) for alias in sorted(record["aliases"])]
    for alias, name in sorted(pending_aliases):
        if theme.add_alias(alias, name):
            report.add("alias-desktop")


def vendor_sources(recipe: dict, dirs: Dirs, src: Path) -> dict[str, str]:
    """Icon name -> absolute source file, for every app icon a build would frame and a tile could color.

    Same scan, names and resolution order as build_vendor; own icons and our own apps are left out.
    """
    # The names own icons and their aliases take, found by building them in a throwaway palette.
    own = Theme()
    neutral = {"tile": "#000000", "glyph": "#ffffff", "back": "#000000"}
    build_own(own, recipe, src, "@content", neutral, Report())
    build_own_aliases(own, src, Report())
    icons = VendorIcons(dirs, tuple(str(v) for v in recipe.get("vendorThemes") or ()))
    art = src / ART_DIR
    sources: dict[str, str] = {}
    for name, record in sorted(vendor_apps(dirs, icons, Report()).items()):
        if name.startswith(OUR_APPS):
            continue
        try:
            path, _ = vendor_source(
                name, record, icons, art, own.names, lambda path: vendor_content(path, Slot(0.0, (0.0, 0.0)))
            )
        except Skip:
            continue
        sources[name] = os.path.abspath(path)
    return sources


def build_fallbacks(theme: Theme, dirs: Dirs, report: Report) -> None:
    """Symlink the MIME types nothing provided to their generic icon, when we have it."""
    for kind, generic in mime_generic_icons(dirs).items():
        if theme.add_alias(kind.replace("/", "-"), generic):
            report.add("alias-mime")


def check_out_dir(out: Path) -> None:
    """Refuse an output directory this script must not replace."""
    if os.path.islink(out):
        raise BuildError(f"refusing --out {out}: it is a symlink")
    resolved = Path(os.path.realpath(out))
    if resolved == ROOT or ROOT in resolved.parents:
        raise BuildError(f"refusing --out {out}: it is inside the checkout {ROOT}")
    if resolved.parent == resolved:
        raise BuildError(f"refusing --out {out}")
    if not os.path.lexists(out):
        return
    if not out.is_dir():
        raise BuildError(f"refusing --out {out}: it exists and is not a directory")
    if (out / MARKER).is_file() and not (out / MARKER).is_symlink():
        return
    if any(out.iterdir()):
        raise BuildError(f"refusing --out {out}: it holds files that {PRODUCT} did not build (no {MARKER} marker)")


def _exchange(a: Path, b: Path) -> bool:
    """Atomically swap two paths (Linux renameat2). False if unavailable."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError):
        return False
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    return renameat2(-100, os.fsencode(a), -100, os.fsencode(b), 2) == 0


def publish(out: Path, theme: Theme, index: str) -> None:
    check_out_dir(out)
    parent = out.parent
    umask = os.umask(0)
    os.umask(umask)
    try:
        parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=f".{out.name}.tmp-", dir=parent))
    except OSError as exc:
        raise BuildError(f"cannot write to {parent}: {exc}") from None
    try:
        os.chmod(tmp, 0o777 & ~umask)
        for rel in sorted(theme.files):
            path = tmp / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(theme.files[rel])
        for rel in sorted(theme.links):
            path = tmp / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(theme.links[rel], path)
        (tmp / MARKER).write_text(MARKER_TEXT, encoding="utf-8")
        (tmp / "index.theme").write_text(index, encoding="utf-8")
        # The destination may have changed while we were building.
        check_out_dir(out)
        if not os.path.lexists(out):
            os.rename(tmp, out)
        elif _exchange(tmp, out):
            pass  # tmp holds the previous build; removed below
        else:
            old = Path(tempfile.mkdtemp(prefix=f".{out.name}.old-", dir=parent))
            os.rmdir(old)
            os.rename(out, old)
            try:
                os.rename(tmp, out)
            except OSError:
                os.rename(old, out)
                raise
            shutil.rmtree(old, ignore_errors=True)
    except OSError as exc:
        raise BuildError(f"cannot publish to {out}: {exc}") from None
    finally:
        if os.path.lexists(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=f"Build the {PRODUCT} theme from the template, own icons and the vendor icons of installed apps.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        metavar="DIR",
        help=f"directory to publish the theme to (default: $XDG_DATA_HOME/icons/{DEFAULT_OUT_NAME})",
    )
    parser.add_argument("--name", help="theme name written to index.theme (default: the Name in index.theme.in)")
    parser.add_argument("--palette", type=Path, metavar="FILE", help="JSON with tile, glyph and optionally back colors")
    parser.add_argument("--from-dms", action="store_true", help="read colors from $XDG_CACHE_HOME/dms/dms-colors.json")
    parser.add_argument(
        "--mode", choices=("dark", "light"), help="DMS color mode for --from-dms (default: the file's mode)"
    )
    parser.add_argument("--tile", metavar="HEX", help="tile color, e.g. #9ecaff")
    # The tile's name in released builds, which older DMS still passes.
    parser.add_argument("--badge", dest="tile", metavar="HEX", help=argparse.SUPPRESS)
    parser.add_argument("--glyph", metavar="HEX", help="glyph color, e.g. #003258")
    parser.add_argument("--back", metavar="HEX", help="back plate color (default: the tile color 12 tones darker)")
    parser.add_argument(
        "--app-style",
        choices=APP_STYLES,
        help="how app icons are drawn: 'color' keeps each app's own artwork; 'tinted' repaints it in tones between "
        "the tile and glyph colors; 'themed' and 'monochrome' draw each "
        "as a one-color glyph on its tile, from our own symbolic art, a glyph in src/mono/apps, the app's "
        "-symbolic icon, one from an installed icon theme, its SVG without the plate, a --tiles cutout or its SVG "
        "tinted, else in tones between the "
        "tile and glyph colors; the --tiles colors tell themed and monochrome apart (default: recipe 'appStyle', "
        "else tinted when recipe 'mono' is true, else color)",
    )
    # The released generator's switch, which DMS still passes to older packs.
    parser.add_argument("--mono", dest="app_style", action="store_const", const="tinted", help=argparse.SUPPRESS)
    parser.add_argument("--no-mono", dest="app_style", action="store_const", const="color", help=argparse.SUPPRESS)
    parser.add_argument(
        "--shape", metavar="NAME", help="tile shape, a file in src/templates (default: recipe 'template')"
    )
    parser.add_argument(
        "--gradient",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="shade the tile from lighter at the top to darker at the bottom (default: recipe 'gradient')",
    )
    parser.add_argument(
        "--tiles",
        type=Path,
        metavar="FILE",
        help='JSON {"apps": {"<icon name>": TILE}, "shell": TILE, "dux": TILE} (each key optional), TILE being '
        '{"tile": HEX, "glyph": HEX, "back": HEX, "ring": HEX, "reach": N, "cutout": PNG} with only tile required: "apps" '
        'colors listed installed apps, "shell" our own app icons in src/own/apps and "dux" installed '
        'dev.duxlabs.* apps, whose art it also recolors to the hue of its "tint": HEX (default: its tile); glyph '
        f"defaults to the theme glyph, back to the tile color 12 tones darker; ring draws a {fmt(RING_WIDTH)}-unit "
        "line of that color inside the tile edge; reach, above 0, is the icon's farthest opaque pixel from its "
        "center as a fraction of its half-size, and grows the icon (up to 2x) to fill the tile, which is then left "
        "flat; cutout is the app's mark as a white PNG with the mark in its alpha, drawn by the themed app styles; "
        "other own icons and the tile-free copies keep the theme colors",
    )
    parser.add_argument(
        "--own-tone",
        type=parse_tone,
        metavar="L",
        help="CIELAB lightness 0-100 the lightest color of a recolored dev.duxlabs.* app lands on, every other "
        "color moving with it (default: each color keeps its own lightness)",
    )
    parser.add_argument(
        "--files-style",
        choices=FILES_STYLES,
        help="how own files, folders and places are drawn: 'standard' on the tile like every other icon, or 'color' "
        "without it, filling the icon: folders and places in the theme hue, each file type in its own category "
        "hue (archives green, video purple, ...) turned toward the theme hue, with a rim that keeps them apart "
        "from dark and light panels; own app icons keep the tile in both (default: recipe 'filesStyle', else "
        "standard)",
    )
    parser.add_argument(
        "--dump-sources",
        action="store_true",
        help='print JSON {"apps": {"<icon name>": "<source file>"}} for the app icons a build would frame '
        "and a tile could color, then exit without building; needs no colors and takes the style options too",
    )
    parser.add_argument(
        "--fingerprint",
        action="store_true",
        help="print a digest of this script, its src and the installed apps and icons a build reads, which "
        "changes whenever a build with the same arguments could come out different, then exit without building; "
        "a build prints it last and --dump-sources as its 'fingerprint', each taken before reading anything else",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="list every skipped icon")
    return parser.parse_args(argv)


def parse_tone(value: str) -> float:
    try:
        tone = float(value)
    except ValueError:
        tone = math.nan
    if not 0 <= tone <= 100:
        raise argparse.ArgumentTypeError(f"needs a lightness from 0 to 100, not {value!r}")
    return tone


def summarize(report: Report, verbose: bool) -> list[str]:
    get = report.get
    lines = [
        f"own icons: {get('own')} built" + (f" ({get('own-color')} in color)" if get("own-color") else ""),
        (
            f"desktop entries: {get('entries')} seen, {get('entries-visible')} visible with an icon "
            f"({get('entries-hidden')} hidden, {get('entries-no-icon')} without Icon=, "
            f"{get('entries-not-app')} not applications)"
        ),
        (
            f"vendor icons: {get('vendor-icons')} distinct, {get('vendor-svg')} built from SVG, "
            f"{get('vendor-raster')} built from raster, "
            f"{sum(len(v) for v in report.skips['vendor'].values())} skipped"
            + (f" ({get('vendor-art')} from src/art)" if get("vendor-art") else "")
        ),
        f"aliases: {get('alias-own')} own, {get('alias-desktop')} desktop entry, {get('alias-mime')} MIME type",
    ]
    if report.stages:
        lines.append(
            "app glyphs: "
            + ", ".join(f"{len(report.stages[stage])} {stage}" for stage in GLYPH_STAGES if stage in report.stages)
        )

    def listed(names: list[str]) -> str:
        names = sorted(names)
        shown = names if verbose or len(names) <= 6 else names[:6] + [f"... {len(names) - 6} more (-v lists all)"]
        return f"{len(names)}: {', '.join(shown)}"

    if report.warnings:
        lines.append(f"warnings: {len(report.warnings)} (printed to stderr)")
    for group in ("own", "vendor"):
        for reason in sorted(report.skips[group]):
            lines.append(f"skipped {group} [{reason}] {listed(report.skips[group][reason])}")
    if verbose:
        lines += [f"glyph [{stage}] {listed(report.stages[stage])}" for stage in GLYPH_STAGES if stage in report.stages]
    return lines


def recipe_app_style(recipe: dict) -> str:
    """The recipe's app style: 'appStyle', else tinted for the older `"mono": true`."""
    style = recipe.get("appStyle")
    if style is None:
        return "tinted" if recipe.get("mono") else "color"
    if style not in APP_STYLES:
        raise BuildError(f"recipe 'appStyle' must be one of {', '.join(APP_STYLES)}: {style!r}")
    return style


def load_recipe(src: Path, shape: str | None) -> tuple[dict, str, dict]:
    """(recipe, shape, recipe with the shape's fit merged into its layout)."""
    recipe = load_json(src / "recipe.json")
    if not isinstance(recipe, dict):
        raise BuildError(f"{src / 'recipe.json'} is not a JSON object")
    shape = shape or recipe.get("template", "circle")
    fit = (recipe.get("shapes") or {}).get(shape) or {}
    if not isinstance(fit, dict):
        raise BuildError(f"recipe 'shapes.{shape}' must be an object")
    return recipe, shape, {**recipe, "layout": {**(recipe.get("layout") or {}), **fit}}


def run(args: argparse.Namespace, env: dict[str, str], src: Path, stdout) -> None:
    started = time.perf_counter()
    dirs = Dirs(env)
    # Taken before anything else is read, so whatever changes during the run shows in the next one.
    digest = fingerprint(dirs, src)
    if args.fingerprint:
        print(digest, file=stdout)
        return
    if args.dump_sources:
        _, _, fitted = load_recipe(src, args.shape)
        listing = {"apps": vendor_sources(fitted, dirs, src), "fingerprint": digest}
        print(json.dumps(listing, indent=1, sort_keys=True), file=stdout)
        dirs.lookups.save()
        return
    out = Path(os.path.abspath(args.out)) if args.out else dirs.data_home / "icons" / DEFAULT_OUT_NAME
    check_out_dir(out)

    recipe, shape, fitted = load_recipe(src, args.shape)
    palette = load_palette(args, recipe, dirs, src)
    tiles = load_tiles(args.tiles, palette) if args.tiles else {}
    app_style = args.app_style or recipe_app_style(recipe)
    gradient = bool(recipe.get("gradient")) if args.gradient is None else args.gradient
    files_style = args.files_style or recipe.get("filesStyle", "standard")
    if files_style not in FILES_STYLES:
        raise BuildError(f"recipe 'filesStyle' must be one of {', '.join(FILES_STYLES)}: {files_style!r}")
    templates = Templates(src, shape, gradient)
    template = templates.get(palette)
    recipe = fitted
    try:
        index_template = INDEX_TEMPLATE.read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError(f"cannot read {INDEX_TEMPLATE}: {exc}") from None
    name = args.name if args.name is not None else index_name(index_template)

    theme = Theme()
    report = Report()
    build_own(theme, recipe, src, template, palette, report, tiles.get("shell"), templates, files_style, app_style)
    build_own_aliases(theme, src, report)
    build_vendor(theme, recipe, dirs, templates, palette, app_style, report, src / ART_DIR, tiles, args.own_tone)
    build_fallbacks(theme, dirs, report)
    if theme.paths:
        listed = {icon: theme.names[name] for icon, name in sorted(theme.paths.items())}
        theme.files[PATHS_FILE] = (json.dumps(listed, indent=1) + "\n").encode("utf-8")
    inherits = [str(v) for v in recipe.get("inherits") or ["hicolor"]]
    index = render_index(index_template, name, inherits, theme.contexts())
    publish(out, theme, index)
    dirs.lookups.save()
    elapsed = time.perf_counter() - started

    for warning in report.warnings:
        print(f"{PRODUCT}: warning: {warning}", file=sys.stderr)
    print(f"{PRODUCT}: published '{name}' to {out}", file=stdout)
    for line in summarize(report, args.verbose):
        print(f"  {line}", file=stdout)
    extra = f" tiles={report.get('tiles')}" if args.tiles else ""
    extra += "".join(f" {key}={report.get(f'tiles-{key}')}" for key in ("shell", "dux") if key in tiles)
    extra += "".join(f" {key}={report.get(f'tiles-{key}')}" for key in ("ring", "reach") if report.get(f"tiles-{key}"))
    extra += f" own-tone={fmt(args.own_tone, 2)}" if args.own_tone is not None else ""
    print(
        f"  palette: tile={palette['tile']} glyph={palette['glyph']} back={palette['back']} "
        f"apps={app_style} shape={shape} gradient={'yes' if gradient else 'no'} files={files_style}{extra}",
        file=stdout,
    )
    print(f"  {len(theme.files)} files, {len(theme.links)} symlinks, built in {elapsed:.2f}s", file=stdout)
    print(f"  fingerprint: {digest}", file=stdout)


def main(argv: list[str] | None = None, env: dict[str, str] | None = None, src: Path | None = None, stdout=None) -> int:
    args = parse_args(argv)
    try:
        run(args, dict(os.environ if env is None else env), src or SRC, stdout or sys.stdout)
    except BuildError as exc:
        print(f"{PRODUCT}: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
