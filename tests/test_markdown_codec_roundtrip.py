from __future__ import annotations

from qsync.markdown_codec import html_to_md, md_to_html


def test_html_to_md_drops_empty_strong_tag() -> None:
    assert html_to_md("<strong> </strong>") == ""


def test_html_to_md_does_not_create_blank_line_after_list_br() -> None:
    md = html_to_md("<ul><li>One</li><li>Two</li></ul><br><strong>After</strong>")
    assert md == "- One\n- Two\n**After**"


def test_md_to_html_does_not_turn_four_stars_into_emphasis() -> None:
    html = md_to_html("****")
    assert "<em>" not in html
    assert html == "****"


def test_html_to_md_caps_excess_consecutive_newlines() -> None:
    md = html_to_md("<p>Text</p><br><br><p>Text</p>")
    assert md == "Text\n\nText"
    assert "\n\n\n" not in md
