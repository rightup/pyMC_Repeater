from __future__ import annotations

from pathlib import Path

HTML_ROOT = Path(__file__).resolve().parents[1] / "repeater" / "web" / "html"


def test_plugin_catalogue_artwork_uses_bounded_media_region():
    css = (HTML_ROOT / "assets" / "plugin-ui-overrides.css").read_text(encoding="utf-8")

    assert 'img[alt$=" artwork"]' in css
    assert "object-fit: contain" in css
    assert "height: clamp(" in css
    assert "flex: 1 1 auto" in css


def test_plugin_layout_override_loads_after_generated_stylesheet():
    index = (HTML_ROOT / "index.html").read_text(encoding="utf-8")

    generated = index.index("/assets/index-")
    override = index.index("/assets/plugin-ui-overrides.css")
    assert generated < override
