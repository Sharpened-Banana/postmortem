"""Enables ``python -m postmortem.desktop`` as a way to launch the
desktop app, alongside the ``postmortem-desktop`` console script
(see app.py and pyproject.toml's ``[project.scripts]``)."""

from __future__ import annotations

from .app import main

if __name__ == "__main__":
    main()
