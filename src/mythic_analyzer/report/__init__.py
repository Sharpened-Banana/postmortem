"""Report rendering: text, JSON and self-contained HTML."""

from .text import render_text
from .html import render_html

__all__ = ["render_text", "render_html"]
