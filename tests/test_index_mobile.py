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
    assert css.index("grid-template-columns:22px 44px 70px") < at
    for rule in (
        '.run-row { grid-template-columns:36px minmax(0,1fr) auto;',
        '.run-head > div.static { display:none; }',
        '.run-row .score:nth-last-child(2)::before { content:"Kicks"; }',
        '.run-row .score:last-child::before { content:"Route"; }',
        '.run-row .party { grid-column:1 / -1;',
        '.board { min-width:0; }',
    ):
        assert css.index(rule) > at, rule


def test_desktop_board_keeps_its_minimum_width_rule():
    css = _css(render_index([]))
    assert ".board { min-width:860px; }" in css


def test_phone_body_size_and_viewport_fit():
    page = render_index([])
    assert "viewport-fit=cover" in page
    css = _css(page)
    assert css.index("body { padding:16px; font-size:15px; }") > css.index("@media (max-width:720px)")
