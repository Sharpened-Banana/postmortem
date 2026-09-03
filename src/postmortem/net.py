"""Shared HTTPS plumbing for this project's stdlib-only HTTP clients
(upload.py, raiderio.py, keystoneguru.py).

Real bug (2026-09-03): a packaged desktop build hit
``[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to
get local issuer certificate`` on every "Upload to site" click. A frozen
PyInstaller interpreter can't reliably fall back on OS-level CA trust
store discovery the way a normal system Python does, so
``urllib.request.urlopen()``'s default SSL context found no issuer to
verify against. ``certifi`` ships its own trusted CA bundle as package
data, and PyInstaller's own bundled hook only pulls that data file into
a frozen build when the package is actually imported somewhere -- it was
listed as a ``desktop`` extra dependency but never imported, so it never
shipped. Using it explicitly here fixes both: the import makes
PyInstaller bundle the CA file, and the explicit context sidesteps OS
trust-store discovery entirely.

Optional import: the core package stays usable without the ``desktop``
extra installed (see pyproject.toml) -- when ``certifi`` isn't present,
callers just get ``None`` and ``urlopen`` falls back to its own default
context, unchanged from today's behavior.
"""

from __future__ import annotations

import ssl
from typing import Optional


def https_context() -> Optional[ssl.SSLContext]:
    """A verified SSL context built from certifi's CA bundle, or ``None``
    when certifi isn't installed (pass straight through to ``urlopen``'s
    ``context=`` kwarg -- it accepts ``None`` and uses its own default)."""
    try:
        import certifi
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())
