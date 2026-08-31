"""Enables ``python -m mythic_analyzer.desktop`` as a way to launch the
desktop app, alongside the ``mythic-analyzer-desktop`` console script
(see app.py and pyproject.toml's ``[project.scripts]``)."""

from __future__ import annotations

from .app import main

if __name__ == "__main__":
    main()
