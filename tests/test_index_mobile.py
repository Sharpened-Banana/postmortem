"""Phase 2 of the site's mobile plan: the history page's leaderboard rows
become cards under 720px on the same DOM. The desktop markup and rules
are untouched, so the CSS switch is what these pin."""
from __future__ import annotations

from postmortem.report.index import render_index


def _css(page: str) -> str:
    return page[page.index("<style>"):page.index("</style>")]


def test_phone_rules_live_under_one_media_query():
    css = _css(render_index([]))
    at = css.index("@media (max-width:720px)")
    # every card rule is after the query; the desktop grid template is before it
    assert css.index("grid-template-columns:22px 70px minmax(120px,180px)") < at
    for rule in (
        '.run-row { grid-template-columns:36px minmax(0,1fr) auto;',
        '.run-head > div.static { display:none; }',
        '.run-row .score:nth-last-child(2)::before { content:"Kick eff."; }',
        '.run-row .score:last-child::before { content:"Route"; }',
        '.run-row .dungeon .dn-full { white-space:normal; overflow:visible; max-width:none; }',
        '.run-row .result { grid-area:result; justify-self:end; }',
        '.run-row .party { grid-column:1 / -1;',
        '.board { min-width:0; }',
    ):
        assert css.index(rule) > at, rule


def test_desktop_board_keeps_its_minimum_width_rule():
    css = _css(render_index([]))
    assert ".board { min-width:1040px; }" in css
    # full dungeon names everywhere; the abbreviation span is never shown
    assert css.index(".run-row .dungeon .dn-abbr { display:none; }") < css.index("@media (max-width:720px)")
    assert ".dn-full { display:none; }" not in css


def test_phone_body_size_and_viewport_fit():
    page = render_index([])
    assert "viewport-fit=cover" in page
    css = _css(page)
    assert css.index("body { padding:16px; font-size:15px; }") > css.index("@media (max-width:720px)")


def test_class_colours_are_readable_on_the_dark_panels():
    """Blizzard's Death Knight, Demon Hunter and Shaman colours fail WCAG AA
    (4.5:1) as text on the page's dark surfaces; the page uses lightened
    variants of the same hues -- the same values as the run report."""
    import re
    page = render_index([])
    colours = dict(re.findall(r'(\w+): "(#[0-9A-Fa-f]{6})"', page[page.index("deathknight:"):]))

    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    for surface in ("#0E0D0C", "#141210", "#1A1714"):
        for cls in ("deathknight", "demonhunter", "shaman", "druid", "priest", "warlock"):
            a, b = lum(colours[cls]), lum(surface)
            assert (max(a, b) + 0.05) / (min(a, b) + 0.05) >= 4.5, (cls, surface)
