"""
A shop's public palette from one colour: its principal colour in, every token
of the booking page out, in light and dark, each pair of text and ground at a
WCAG contrast the page can promise (CONTRAST, test_theme.py sweeps every hue).

The panel's preview runs the same maths (amandu-frontend src/lib/shopPalette.ts);
this one is the authoritative copy, since it is the one that is served. The
colours are worked in OKLCH, so a step in lightness looks like the same step
whatever the hue: a yellow and a blue at one L read equally light.
"""

import math

# Kyo's own achromatic palette: what a shop without a colour, or with a grey
# one, keeps. Mirrors the defaults at the top of booking.html.
KYO = {
    'light': {
        'canvas': '#fafaf9', 'surface': '#ffffff', 'sunken': '#f5f5f4', 'hover': '#f5f5f4',
        'border': '#e7e5e4', 'border-strong': '#d6d3d1',
        'ink': '#1c1917', 'ink-secondary': '#44403c', 'ink-subtle': '#756e69',
        'accent': '#1c1917', 'accent-ink': '#ffffff', 'accent-line': '#1c1917',
        'soft': '#ece9e6', 'soft-ink': '#1c1917', 'signal-ink': '#be123c',
    },
    'dark': {
        'canvas': '#1a1715', 'surface': '#211d1a', 'sunken': '#14110f', 'hover': '#2a2521',
        'border': '#3a332e', 'border-strong': '#4d4540',
        'ink': '#f5f3f1', 'ink-secondary': '#c9c2bc', 'ink-subtle': '#948b84',
        'accent': '#f5f3f1', 'accent-ink': '#1a1715', 'accent-line': '#f5f3f1',
        'soft': '#3a332e', 'soft-ink': '#f5f3f1', 'signal-ink': '#fb7185',
    },
}

# (text, ground, minimum ratio): what each mode guarantees.
CONTRAST = (
    ('ink', 'canvas', 7), ('ink-secondary', 'canvas', 7), ('ink-subtle', 'canvas', 4.5),
    ('accent-ink', 'accent', 4.5), ('soft-ink', 'soft', 4.5), ('accent-line', 'canvas', 3),
    ('band-ink', 'band', 4.5), ('signal-ink', 'canvas', 4.5),
)

# Below this chroma a colour is a grey, and a grey shop gets Kyo's own.
GREY = 0.03


def derive_theme(seed, intensity='sutil'):
    """
    {'light': tokens, 'dark': tokens} for a principal colour ('#rrggbb', or
    None for Kyo's own). 'marcada' tints the grounds more and paints the band
    in the principal colour; 'sutil' paints it in a pale wash of it.
    """
    return {mode: _derive(seed, intensity, mode) for mode in ('light', 'dark')}


def _derive(seed, intensity, mode):
    L, C, H = _hex_to_oklch(seed) if seed else (0, 0, 0)
    strong = intensity == 'marcada'
    if C < GREY:
        t = dict(KYO[mode])
        t['band'], t['band-ink'] = (t['accent'], t['accent-ink']) if strong else (t['soft'], t['soft-ink'])
        return t
    k = 2.2 if strong else 1
    t = {}
    if mode == 'light':
        t['canvas'] = _ok(0.983, min(C * 0.05, 0.008) * k, H)
        t['surface'] = _ok(0.998, min(C * 0.02, 0.004) * k, H)
        t['sunken'] = t['hover'] = _ok(0.958, min(C * 0.1, 0.016) * k, H)
        t['border'] = _ok(0.905, min(C * 0.12, 0.022) * k, H)
        t['border-strong'] = _ok(0.85, min(C * 0.14, 0.03) * k, H)
        t['ink'] = _ok(0.235, min(C * 0.25, 0.035), H)
        t['ink-secondary'] = _fit(0.40, min(C * 0.25, 0.035), H, t['canvas'], 7, -0.02)
        t['ink-subtle'] = _fit(0.54, min(C * 0.2, 0.03), H, t['canvas'], 4.5, -0.02)
        # The colour as given whenever a label can sit on it; else darkened until white can.
        if _contrast(seed, '#ffffff') >= 4.5:
            t['accent'], t['accent-ink'] = seed, '#ffffff'
        elif _contrast(seed, t['ink']) >= 4.5:
            t['accent'], t['accent-ink'] = seed, t['ink']
        else:
            t['accent'], t['accent-ink'] = _fit(L, C, H, '#ffffff', 4.5, -0.02), '#ffffff'
        # A pale accent (a yellow under dark ink) is no line on the canvas.
        t['accent-line'] = (
            t['accent'] if _contrast(t['accent'], t['canvas']) >= 3 else _fit(L, C, H, t['canvas'], 3, -0.02)
        )
        t['soft'] = _ok(0.945, min(C * 0.45, 0.06) * (1.3 if strong else 1), H)
        t['soft-ink'] = _fit(0.42, min(C, 0.16), H, t['soft'], 4.5, -0.02)
        t['signal-ink'] = '#be123c'
    else:
        t['canvas'] = _ok(0.175, min(C * 0.12, 0.02) * k, H)
        t['surface'] = _ok(0.215, min(C * 0.12, 0.02) * k, H)
        t['sunken'] = _ok(0.15, min(C * 0.1, 0.016) * k, H)
        t['hover'] = _ok(0.25, min(C * 0.12, 0.02) * k, H)
        t['border'] = _ok(0.30, min(C * 0.14, 0.026) * k, H)
        t['border-strong'] = _ok(0.37, min(C * 0.16, 0.03) * k, H)
        t['ink'] = _ok(0.955, min(C * 0.08, 0.012), H)
        t['ink-secondary'] = _fit(0.83, min(C * 0.1, 0.02), H, t['canvas'], 7, 0.02)
        t['ink-subtle'] = _fit(0.68, min(C * 0.15, 0.025), H, t['canvas'], 4.5, 0.02)
        # On a dark ground the accent is a light version of the colour, labelled in the ground.
        aL = min(max(L, 0.66), 0.84)
        t['accent'] = _ok(aL, C, H)
        if _contrast(t['accent'], t['canvas']) < 4.5:
            t['accent'] = _fit(aL, C, H, t['canvas'], 4.5, 0.02)
        t['accent-ink'] = t['canvas']
        t['accent-line'] = t['accent']
        t['soft'] = _ok(0.30, min(C * 0.5, 0.07), H)
        t['soft-ink'] = _fit(0.86, min(C, 0.12), H, t['soft'], 4.5, 0.02)
        t['signal-ink'] = '#fb7185'
    t['band'], t['band-ink'] = (t['accent'], t['accent-ink']) if strong else (t['soft'], t['soft-ink'])
    return t


# --- colour maths: sRGB <-> OKLab/OKLCH (Björn Ottosson), WCAG contrast ---

def _to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _to_gamma(c):
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def _rgb(hex_):
    return [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]


def _hex(rgb):
    return '#' + ''.join(f'{round(min(max(c, 0), 1) * 255):02x}' for c in rgb)


def _hex_to_oklch(hex_):
    r, g, b = map(_to_linear, _rgb(hex_))
    l = math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m = math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s = math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    L = 0.2104542553 * l + 0.7936177850 * m - 0.0040720453 * s
    a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    b_ = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return L, math.hypot(a, b_), math.degrees(math.atan2(b_, a)) % 360


def _oklab_to_linear(L, a, b):
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def _ok(L, C, H):
    """An OKLCH colour as hex, its chroma cut until it fits sRGB (hue and lightness kept)."""
    L = min(max(L, 0), 1)
    h = math.radians(H)

    def at(c):
        return _oklab_to_linear(L, c * math.cos(h), c * math.sin(h))

    def fits(c):
        return all(-1e-4 <= x <= 1 + 1e-4 for x in at(c))

    lo, hi = 0, max(C, 0)
    if not fits(hi):
        for _ in range(24):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if fits(mid) else (lo, mid)
        hi = lo
    return _hex([_to_gamma(min(max(c, 0), 1)) for c in at(hi)])


def _luminance(hex_):
    r, g, b = map(_to_linear, _rgb(hex_))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(x, y):
    hi, lo = sorted((_luminance(x), _luminance(y)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _fit(L, C, H, ground, target, step):
    """Walk the lightness by `step` from L until the colour reaches `target` against `ground`."""
    hex_ = _ok(L, C, H)
    for _ in range(80):
        if _contrast(hex_, ground) >= target:
            break
        L += step
        if not 0 <= L <= 1:
            break
        hex_ = _ok(L, C, H)
    return hex_
