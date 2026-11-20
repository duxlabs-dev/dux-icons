#!/usr/bin/env python3
"""Dux Icons build: frames vendor and own icons into a freedesktop icon theme of SVG files.

Standard library only. The theme is assembled in memory, written to a temporary sibling of the
output and renamed into place. The checkout is never written or deleted.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import fnmatch
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

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"

MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_RASTER_SIDE = 4096
PREFERRED_RASTER_SIDE = 256

PATHS_FILE = "icon-paths.json"
# Tones the badge gradient is lighter at the top and darker at the bottom.
GRADIENT_TONES = (8.0, 10.0)

CONTEXTS = {
    "apps": "Applications",
    "mimetypes": "MimeTypes",
}

TOKEN_RE = re.compile(r"@(badge|glyph|back)\b")
STRAY_TOKEN_RE = re.compile(r"(?<![\w&])@[A-Za-z][\w-]*")
# A color written as an attribute or style value; an id like url(#add) is left alone.
COLOR_RE = re.compile(r"(?<=[\"':\s])#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?=[\"';\s)])")
NAME_RE = re.compile(r"[A-Za-z0-9_+][A-Za-z0-9_.+@-]*\Z")


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
    return "#%02x%02x%02x" % rgb


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
        return t ** 3 if t ** 3 > 216 / 24389 else (116 * t - 16) / (24389 / 27)

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
    _, a, b = rgb_to_lab(parse_hex(color))
    chroma = math.hypot(a, b)
    hue = math.atan2(b, a)

    def at(c: float) -> tuple[float, float, float]:
        return lab_to_rgb_float((target, c * math.cos(hue), c * math.sin(hue)))

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


def retone(content: str, badge: str) -> str:
    """Every color in `content` moved onto the badge hue, each keeping its own lightness."""
    return COLOR_RE.sub(lambda m: toned(badge, rgb_to_lab(parse_hex(m.group()))[0]), content)


def mono_matrix(badge: str, glyph: str) -> str:
    """feColorMatrix values mapping content luminance onto the badge-to-glyph color line; alpha kept."""
    b = [c / 255 for c in parse_hex(badge)]
    g = [c / 255 for c in parse_hex(glyph)]

    def luma(c: list[float]) -> float:
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    dark, light = (g, b) if luma(g) <= luma(b) else (b, g)
    rows = []
    for i in range(3):
        span = light[i] - dark[i]
        rows.append(
            f"{fmt(span * 0.2126, 5)} {fmt(span * 0.7152, 5)} {fmt(span * 0.0722, 5)} 0 {fmt(dark[i], 5)}"
        )
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


_LENGTH_RE = re.compile(
    r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(px|pt|pc|mm|cm|in)?\s*\Z"
)
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
    scale_text = fmt(sx, 6) if sx == sy else f"{fmt(sx, 6)} {fmt(sy, 6)}"
    return f"translate({fmt(tx)} {fmt(ty)}) scale({scale_text})", (vx, vy, vw, vh)


_CSS_SKIP_RE = re.compile(r"/\*.*?\*/|<!--|-->", re.S)
_CSS_PROTECTED_RE = re.compile(r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|\[[^\]]*\])")
_CSS_NAME_RE = re.compile(r"([.#])(-?[A-Za-z_\u0080-￿][\w\u0080-￿-]*)")
_CSS_ROOT_RE = re.compile(r"(?:svg(?![\w-])|:root)", re.I)
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
        return f"#{scope}{selector[root.end():]}"
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


def mono_wrap(content: str, palette: dict[str, str], contrast: float = 1.0, near: str = "badge") -> str:
    """Content in tones from the glyph color to palette[near], the end that sits closest to the badge."""
    size = fmt(CANVAS)
    # Each primitive clamps its result, which is what makes the first one a contrast curve.
    stretch = f'<feColorMatrix type="matrix" values="{contrast_matrix(contrast)}"/>' if contrast != 1.0 else ""
    return (
        f'<filter id="{MONO_ID}" filterUnits="userSpaceOnUse" x="0" y="0" width="{size}" height="{size}" '
        f'color-interpolation-filters="sRGB">{stretch}<feColorMatrix type="matrix" '
        f'values="{mono_matrix(palette[near], palette["glyph"])}"/></filter>'
        f'<g filter="url(#{MONO_ID})">{content}</g>'
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
                    folder / f"{name}{ext}"
                    for folder in self._directories(base / theme)
                    for ext in self.EXTENSIONS
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


def mime_generic_icons(dirs: Dirs) -> dict[str, str]:
    """MIME type -> generic icon name, from the shared MIME database."""
    generic: dict[str, str] = {}
    types: set[str] = set()
    aliases: dict[str, str] = {}
    for base in dirs.data:
        mime = base / "mime"
        for name in ("generic-icons", "types", "aliases"):
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
        roles = {"badge": "primary", "glyph": "on_primary", **(recipe.get("tokens") or {})}
        for token in ("badge", "glyph", "back"):
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
        for token in ("badge", "glyph", "back"):
            if token in data:
                palette[token] = normalize_color(data[token], f"{path}: {token}")
            elif token != "back" and not getattr(args, token):
                raise BuildError(f"{path} is missing '{token}'")
    for token in ("badge", "glyph", "back"):
        value = getattr(args, token)
        if value:
            palette[token] = normalize_color(value, f"--{token}")
    palette.setdefault("back", darker(palette["badge"]))
    return palette


def shapes(src: Path) -> list[str]:
    try:
        return sorted(p.stem for p in (src / "templates").iterdir() if p.suffix == ".svg")
    except OSError:
        return []


def load_template(src: Path, name: str, palette: dict[str, str], gradient: bool = False) -> str:
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
        raise BuildError(f"{path}: the template canvas must be viewBox=\"0 0 64 64\"")
    if re.search(r'\bid="(?:c-|dx-)', text):
        raise BuildError(f"{path}: ids starting with c- or dx- are reserved for content")
    if gradient:
        up, down = GRADIENT_TONES
        text = (
            f'{text[: head.end()]}<linearGradient id="{FILL_ID}" gradientUnits="userSpaceOnUse" '
            f'x1="0" y1="0" x2="0" y2="{fmt(CANVAS)}"><stop offset="0" stop-color="{darker(palette["badge"], -up)}"/>'
            f'<stop offset="1" stop-color="{darker(palette["badge"], down)}"/></linearGradient>{text[head.end() :]}'
        )
        palette = {**palette, "badge": f"url(#{FILL_ID})"}
    painted = paint_tokens(text.strip(), palette)
    stray = sorted(set(STRAY_TOKEN_RE.findall(painted)) - {"@content"})
    if stray:
        raise BuildError(f"{path}: unresolved tokens: {', '.join(stray)}")
    return painted + "\n"


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

    def add_icon(self, context: str, name: str, svg: str) -> None:
        rel = f"{SCALABLE}/{context}/{name}.svg"
        self.files[rel] = svg.encode("utf-8")
        self.names[name] = rel

    def add_alias(self, name: str, target: str, context: str | None = None) -> bool:
        """Symlink `name` to icon `target`; False when it cannot or need not be made."""
        if name in self.names or target not in self.names or not NAME_RE.match(name):
            return False
        real = self.names[target]
        folder = f"{SCALABLE}/{context}" if context else os.path.dirname(real)
        self.links[f"{folder}/{name}.svg"] = os.path.relpath(real, folder)
        self.names[name] = real
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

    def add(self, key: str, amount: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + amount

    def get(self, key: str) -> int:
        return self.counts.get(key, 0)

    def skip(self, group: str, reason: str, name: str) -> None:
        self.skips[group].setdefault(reason, []).append(name)


def assemble(template: str, content: str) -> str:
    return template.replace("@content", content)


def build_own(theme: Theme, recipe: dict, src: Path, template: str, palette: dict[str, str], report: Report) -> None:
    for path in sorted((src / "own").glob("*/*.svg")):
        context, name = path.parent.name, path.stem
        rel = f"{context}/{name}"
        try:
            if not (NAME_RE.match(context) and NAME_RE.match(name)):
                raise Skip("bad-name")
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                raise Skip("unreadable") from None
            painted = paint_tokens(text, palette)
            if STRAY_TOKEN_RE.search(painted):
                raise Skip("unknown-token")
            slot = layout_for(context, name, recipe, own=True)
            content = inline_svg(parse_svg(painted.encode("utf-8")), slot, clip=False)
        except Skip as skip:
            report.skip("own", skip.reason, rel)
            continue
        theme.add_icon(context, name, assemble(template, content))
        report.add("own")


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
                    report.warnings.append(f"aliases.json: '{alias}' (alias of '{target}') is already an own icon; remove the alias")
                elif alias in theme.names:
                    report.warnings.append(f"aliases.json: '{alias}' is listed more than once; '{target}' is ignored")
                elif theme.add_alias(alias, target, context):
                    report.add("alias-own")
                else:
                    report.skip("own", "alias-without-target", f"{alias} -> {target}")


def build_vendor(
    theme: Theme,
    recipe: dict,
    dirs: Dirs,
    template: str,
    palette: dict[str, str],
    mono: bool,
    report: Report,
) -> None:
    themes = tuple(str(v) for v in recipe.get("vendorThemes") or ())
    icons = VendorIcons(dirs, themes)
    contrast = float(recipe.get("monoContrast", 1.0))
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
        name = os.path.basename(icon) if icon.startswith(("/", "~/")) else icon
        stem, ext = os.path.splitext(name)
        if ext.lower() in VendorIcons.EXTENSIONS and stem:
            name = stem
        entries.append((name, icon, entry_id))

    def is_path(icon: str) -> bool:
        return icon.startswith(("/", "~/"))

    files: dict[str, set[str]] = {}
    for name, icon, _ in entries:
        if is_path(icon):
            files.setdefault(name, set()).add(os.path.realpath(icons.expand(icon)))
    # Several Icon= values can name one icon (a name and a path).
    wanted: dict[str, dict] = {}
    for name, icon, entry_id in entries:
        # Two files with one basename are two icons; each goes by its desktop entry instead.
        if is_path(icon) and len(files[name]) > 1:
            name = entry_id
        record = wanted.setdefault(name, {"icons": set(), "aliases": set()})
        record["icons"].add(icon)
        if entry_id != name:
            record["aliases"].add(entry_id)

    pending_aliases: list[tuple[str, str]] = []
    for name in sorted(wanted):
        record = wanted[name]
        report.add("vendor-icons")
        built = None
        failure: Skip | None = None
        # Plain names before paths, so the theme lookup wins over a pinned file.
        for icon in sorted(record["icons"], key=lambda value: (is_path(value), value)):
            try:
                if not NAME_RE.match(name):
                    raise Skip("bad-name")
                if name in theme.names:
                    raise Skip("own-icon")
                path = icons.resolve(icon)
                built = vendor_content(path, layout_for("apps", name, recipe, own=False))
                break
            except Skip as skip:
                failure = failure or skip
        if built is None:
            report.skip("vendor", failure.reason, name)
            continue
        kind, content = built
        if mono:
            content = mono_wrap(content, palette, contrast, "back")
        elif name.startswith(OUR_APPS):
            content = retone(content, palette["badge"])
        theme.add_icon("apps", name, assemble(template, content))
        theme.paths.update((icon, name) for icon in record["icons"] if is_path(icon))
        report.add(f"vendor-{kind}")
        pending_aliases += [(alias, name) for alias in sorted(record["aliases"])]
    for alias, name in sorted(pending_aliases):
        if theme.add_alias(alias, name):
            report.add("alias-desktop")


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
        raise BuildError(
            f"refusing --out {out}: it holds files that {PRODUCT} did not build (no {MARKER} marker)"
        )


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
            pass  # tmp now holds the previous build; removed below
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
    parser.add_argument("--palette", type=Path, metavar="FILE", help="JSON with badge, glyph and optionally back colors")
    parser.add_argument("--from-dms", action="store_true", help="read colors from $XDG_CACHE_HOME/dms/dms-colors.json")
    parser.add_argument("--mode", choices=("dark", "light"), help="DMS color mode for --from-dms (default: the file's mode)")
    parser.add_argument("--badge", metavar="HEX", help="badge color, e.g. #9ecaff")
    parser.add_argument("--glyph", metavar="HEX", help="glyph color, e.g. #003258")
    parser.add_argument("--back", metavar="HEX", help="back plate color (default: the badge color 12 tones darker)")
    parser.add_argument(
        "--mono",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="paint vendor content in tones between the badge and glyph colors (default: recipe 'mono')",
    )
    parser.add_argument("--shape", metavar="NAME", help="badge shape, a file in src/templates (default: recipe 'template')")
    parser.add_argument(
        "--gradient",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="shade the badge from lighter at the top to darker at the bottom (default: recipe 'gradient')",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="list every skipped icon")
    return parser.parse_args(argv)


def summarize(report: Report, verbose: bool) -> list[str]:
    get = report.get
    lines = [
        f"own icons: {get('own')} built",
        f"desktop entries: {get('entries')} seen, {get('entries-visible')} visible with an icon "
        f"({get('entries-hidden')} hidden, {get('entries-no-icon')} without Icon=, {get('entries-not-app')} not applications)",
        f"vendor icons: {get('vendor-icons')} distinct, {get('vendor-svg')} built from SVG, "
        f"{get('vendor-raster')} built from raster, "
        f"{sum(len(v) for v in report.skips['vendor'].values())} skipped",
        f"aliases: {get('alias-own')} own, {get('alias-desktop')} desktop entry, {get('alias-mime')} MIME type",
    ]
    def listed(names: list[str]) -> str:
        names = sorted(names)
        shown = names if verbose or len(names) <= 6 else names[:6] + [f"... {len(names) - 6} more (-v lists all)"]
        return f"{len(names)}: {', '.join(shown)}"

    if report.warnings:
        lines.append(f"warnings: {len(report.warnings)} (printed to stderr)")
    for group in ("own", "vendor"):
        for reason in sorted(report.skips[group]):
            lines.append(f"skipped {group} [{reason}] {listed(report.skips[group][reason])}")
    return lines


def run(args: argparse.Namespace, env: dict[str, str], src: Path, stdout) -> None:
    started = time.perf_counter()
    dirs = Dirs(env)
    out = Path(os.path.abspath(args.out)) if args.out else dirs.data_home / "icons" / DEFAULT_OUT_NAME
    check_out_dir(out)

    recipe = load_json(src / "recipe.json")
    if not isinstance(recipe, dict):
        raise BuildError(f"{src / 'recipe.json'} is not a JSON object")
    palette = load_palette(args, recipe, dirs, src)
    mono = bool(recipe.get("mono")) if args.mono is None else args.mono
    gradient = bool(recipe.get("gradient")) if args.gradient is None else args.gradient
    shape = args.shape or recipe.get("template", "circle")
    template = load_template(src, shape, palette, gradient)
    fit = (recipe.get("shapes") or {}).get(shape) or {}
    if not isinstance(fit, dict):
        raise BuildError(f"recipe 'shapes.{shape}' must be an object")
    recipe = {**recipe, "layout": {**(recipe.get("layout") or {}), **fit}}
    try:
        index_template = INDEX_TEMPLATE.read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError(f"cannot read {INDEX_TEMPLATE}: {exc}") from None
    name = args.name if args.name is not None else index_name(index_template)

    theme = Theme()
    report = Report()
    build_own(theme, recipe, src, template, palette, report)
    build_own_aliases(theme, src, report)
    build_vendor(theme, recipe, dirs, template, palette, mono, report)
    build_fallbacks(theme, dirs, report)
    if theme.paths:
        listed = {icon: theme.names[name] for icon, name in sorted(theme.paths.items())}
        theme.files[PATHS_FILE] = (json.dumps(listed, indent=1) + "\n").encode("utf-8")
    inherits = [str(v) for v in recipe.get("inherits") or ["hicolor"]]
    index = render_index(index_template, name, inherits, theme.contexts())
    publish(out, theme, index)
    elapsed = time.perf_counter() - started

    for warning in report.warnings:
        print(f"{PRODUCT}: warning: {warning}", file=sys.stderr)
    print(f"{PRODUCT}: published '{name}' to {out}", file=stdout)
    for line in summarize(report, args.verbose):
        print(f"  {line}", file=stdout)
    print(
        f"  palette: badge={palette['badge']} glyph={palette['glyph']} back={palette['back']} "
        f"mono={'yes' if mono else 'no'} shape={shape} gradient={'yes' if gradient else 'no'}",
        file=stdout,
    )
    print(f"  {len(theme.files)} files, {len(theme.links)} symlinks, built in {elapsed:.2f}s", file=stdout)


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
