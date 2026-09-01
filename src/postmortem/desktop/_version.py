"""Auto-updater version marker for the desktop app.

Overwritten at release-build time by
``.github/workflows/release-desktop.yml`` (its "Stamp version" step,
which runs before ``pip install`` so the stamped file is what actually
gets bundled) with the git tag being released, e.g.
``"alpha-desktop-8"``. A source checkout / dev run keeps this
placeholder -- ``updater.py``'s ``_current_build_number()`` treats
anything that isn't an ``alpha-desktop-<N>`` tag as "not a release
build," which quietly disables update-checking rather than erroring
(there's nothing to compare a dev run's version against).
"""

VERSION = "dev"
