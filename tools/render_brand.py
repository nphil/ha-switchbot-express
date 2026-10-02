"""Deterministic Pillow renderer for the SwitchBot Express brand kit.

Writes the eight images Home Assistant serves straight from
``custom_components/switchbot_express/brand/`` (no brands-repo submission needed):

    icon.png        256x256      dark_icon.png        256x256
    icon@2x.png     512x512      dark_icon@2x.png     512x512
    logo.png        864x256      dark_logo.png        864x256
    logo@2x.png     1728x512     dark_logo@2x.png     1728x512

The mark: a curtain rail with a small carrier (the SwitchBot device) on it,
and three slanted curtain pleats that shorten toward the trailing edge. The
slant plus the tapering trail is the single "fast" cue. Flat colour only, five
shapes, no gradients. ``../icon.svg`` is the same mark as hand-authored vector
art; this script does not read it, so keep the numbers below in sync with it.

Light variant ("icon", "logo"): blue plate, white mark, amber carrier.
Dark variant ("dark_icon", "dark_logo"): pale plate, blue mark, so it keeps
its contrast on the dark Home Assistant header.

Pillow only, 4x supersampling, no randomness: re-running produces
byte-identical PNGs.

Usage: python3 tools/render_brand.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = REPO_ROOT / "custom_components" / "switchbot_express" / "brand"

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def load_font(size: int) -> ImageFont.FreeTypeFont:
    """Liberation Sans Bold, then DejaVu Bold, then Pillow's default font."""
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


# ---------------------------------------------------------------------------
# Geometry, in a 256x256 reference space (identical numbers to ../icon.svg).
# Capsules are ((x0, y0), (x1, y1), radius): a round-capped stroke.
# ---------------------------------------------------------------------------

PLATE_RX = 56.0

RAIL = ((48.0, 58.0), (200.0, 58.0), 7.0)
PLEATS = (  # slanted capsules hanging from the rail; hems share one line
    ((166.0, 86.0), (138.0, 206.0), 12.0),
    ((124.0, 86.0), (96.0, 206.0), 12.0),
    ((82.0, 86.0), (54.0, 206.0), 12.0),
)
CARRIER = (152.0, 36.0, 220.0, 82.0, 16.0)  # x0, y0, x1, y1, corner radius

# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------

LIGHT_VARIANT = {
    "plate": (0x25, 0x4B, 0xE8),
    "mark": (0xFF, 0xFF, 0xFF),
    "accent": (0xFF, 0xB8, 0x2E),
}
DARK_VARIANT = {
    "plate": (0xEE, 0xF2, 0xFF),
    "mark": (0x25, 0x4B, 0xE8),
    "accent": (0xF5, 0x8A, 0x00),
}

WORDMARK_ON_LIGHT_BG = (0x12, 0x1E, 0x4A)  # logo.png
SUBMARK_ON_LIGHT_BG = (0x25, 0x4B, 0xE8)
WORDMARK_ON_DARK_BG = (0xF3, 0xF6, 0xFF)  # dark_logo.png
SUBMARK_ON_DARK_BG = (0x8F, 0xA8, 0xFF)

SS = 4  # supersampling factor


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _capsule(draw, capsule, k, fill):
    (x0, y0), (x1, y1), r = capsule
    dx, dy = x1 - x0, y1 - y0
    length = (dx * dx + dy * dy) ** 0.5
    nx, ny = -dy / length * r, dx / length * r
    draw.polygon(
        [((x0 + nx) * k, (y0 + ny) * k), ((x1 + nx) * k, (y1 + ny) * k),
         ((x1 - nx) * k, (y1 - ny) * k), ((x0 - nx) * k, (y0 - ny) * k)],
        fill=fill,
    )
    for cx, cy in ((x0, y0), (x1, y1)):
        draw.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=fill)


def render_mark(size, palette):
    """(size, size) RGBA icon: flat rounded plate with the rail/pleats mark."""
    big = size * SS
    k = big / 256.0
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)

    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=PLATE_RX * k, fill=palette["plate"] + (255,))

    mark = palette["mark"] + (255,)
    _capsule(d, RAIL, k, mark)
    for pleat in PLEATS:
        _capsule(d, pleat, k, mark)
    x0, y0, x1, y1, r = CARRIER
    d.rounded_rectangle([x0 * k, y0 * k, x1 * k, y1 * k], radius=r * k, fill=palette["accent"] + (255,))

    return canvas.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------------------
# Wordmark lockup
# ---------------------------------------------------------------------------

LOGO_ASPECT = 864 / 256  # width / height (about 3.4x)
LOGO_MARK_FRAC = 0.86  # mark size as a fraction of canvas height
LOGO_LEFT_FRAC = 0.05
LOGO_GAP_FRAC = 0.11
LOGO_NAME_FRAC = 0.34  # "SwitchBot" font size / canvas height
LOGO_SUB_FRAC = 0.30  # "Express" font size / canvas height
LOGO_TRACK_FRAC = 0.004
LOGO_LINE_GAP_FRAC = 0.09


def _tracked_width(draw, text, font, tracking):
    return sum(draw.textlength(ch, font=font) for ch in text) + tracking * (len(text) - 1)


def _draw_tracked(draw, x, y, text, font, fill, tracking):
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def render_logo(height, palette, name_rgb, sub_rgb):
    """RGBA lockup: mark on the left, "SwitchBot" over "Express" on the right,
    transparent background, exactly `height` tall."""
    width = round(height * LOGO_ASPECT)
    mark_size = round(height * LOGO_MARK_FRAC)
    mark = render_mark(mark_size, palette)

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    left = round(height * LOGO_LEFT_FRAC)
    canvas.alpha_composite(mark, (left, (height - mark_size) // 2))

    name_font = load_font(round(height * LOGO_NAME_FRAC))
    sub_font = load_font(round(height * LOGO_SUB_FRAC))
    tracking = height * LOGO_TRACK_FRAC
    draw = ImageDraw.Draw(canvas)

    # Vertically centre the two-line block on ink bounds, not font ascent.
    n_l, n_t, n_r, n_b = draw.textbbox((0, 0), "SwitchBot", font=name_font)
    s_l, s_t, s_r, s_b = draw.textbbox((0, 0), "Express", font=sub_font)
    line_gap = height * LOGO_LINE_GAP_FRAC
    block_h = (n_b - n_t) + line_gap + (s_b - s_t)
    top = (height - block_h) / 2
    text_x = left + mark_size + height * LOGO_GAP_FRAC

    name_w = _tracked_width(draw, "SwitchBot", name_font, tracking)
    _draw_tracked(draw, text_x - n_l, top - n_t, "SwitchBot", name_font, name_rgb + (255,), tracking)
    sub_y = top + (n_b - n_t) + line_gap
    _draw_tracked(draw, text_x - s_l, sub_y - s_t, "Express", sub_font, sub_rgb + (255,), tracking)

    sub_w = _tracked_width(draw, "Express", sub_font, tracking)
    right_edge = text_x + max(name_w, sub_w)
    assert right_edge <= width - height * 0.03, f"wordmark clipped at height {height}: {right_edge} > {width}"
    return canvas


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _save_pair(master, stem):
    """<stem>@2x.png from the 512-based master, <stem>.png as an exact half."""
    master.save(BRAND_DIR / f"{stem}@2x.png")
    half = (master.width // 2, master.height // 2)
    master.resize(half, Image.LANCZOS).save(BRAND_DIR / f"{stem}.png")


def main():
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    _save_pair(render_mark(512, LIGHT_VARIANT), "icon")
    _save_pair(render_mark(512, DARK_VARIANT), "dark_icon")
    _save_pair(render_logo(512, LIGHT_VARIANT, WORDMARK_ON_LIGHT_BG, SUBMARK_ON_LIGHT_BG), "logo")
    _save_pair(render_logo(512, DARK_VARIANT, WORDMARK_ON_DARK_BG, SUBMARK_ON_DARK_BG), "dark_logo")


if __name__ == "__main__":
    main()
