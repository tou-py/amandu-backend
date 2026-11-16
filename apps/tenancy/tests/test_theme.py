import colorsys

import pytest

from apps.tenancy.theme import CONTRAST, KYO, _contrast, derive_theme


def _seeds():
    """Every 10° of hue, at four lightnesses and three saturations, plus the edges."""
    for h in range(0, 360, 10):
        for light in (0.2, 0.4, 0.6, 0.8):
            for sat in (0.3, 0.65, 1):
                yield '#' + ''.join(f'{round(c * 255):02x}' for c in colorsys.hls_to_rgb(h / 360, light, sat))
    yield from ('#000000', '#ffffff', '#ffff00', '#00ffff', '#0000ff', '#ff0000', '#808080')


def test_every_colour_keeps_every_pair_readable():
    failures = []
    for seed in _seeds():
        for intensity in ('sutil', 'marcada'):
            for mode, t in derive_theme(seed, intensity).items():
                for text, ground, target in CONTRAST:
                    ratio = _contrast(t[text], t[ground])
                    if ratio < target:
                        failures.append(f'{seed} {intensity} {mode}: {text} on {ground} {ratio:.2f} < {target}')
    assert not failures, '\n'.join(failures[:20])


@pytest.mark.parametrize('seed', [None, '#777777', '#7a7570'])
def test_no_colour_or_a_grey_is_kyos_own(seed):
    theme = derive_theme(seed)
    assert {k: v for k, v in theme['light'].items() if k in KYO['light']} == KYO['light']
    assert theme['light']['band'] == KYO['light']['soft']
    assert derive_theme(seed, 'marcada')['light']['band'] == KYO['light']['accent']


def test_a_colour_that_can_carry_white_is_used_as_given():
    light = derive_theme('#1f3a5f')['light']
    assert (light['accent'], light['accent-ink']) == ('#1f3a5f', '#ffffff')


def test_marcada_paints_the_band_in_the_accent():
    theme = derive_theme('#c8102e', 'marcada')
    assert theme['light']['band'] == theme['light']['accent']
    assert derive_theme('#c8102e')['light']['band'] == theme['light']['soft'] != theme['light']['accent']
