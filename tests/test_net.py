"""postmortem.net: the shared certifi-backed SSL context used by every
HTTPS client in this project (see net.py's module docstring for the real
bug -- packaged desktop builds hit CERTIFICATE_VERIFY_FAILED because the
default urlopen() context can't discover a CA trust store)."""

from __future__ import annotations

import builtins
import ssl

import postmortem.net as net


class TestHttpsContext:
    def test_returns_a_real_ssl_context_when_certifi_is_installed(self):
        # certifi is a real desktop-extra dependency (pyproject.toml) and
        # is installed in this dev/test environment -- confirmed real,
        # not mocked: the point of this test is that the `import certifi`
        # path actually resolves and produces a usable context.
        context = net.https_context()
        assert isinstance(context, ssl.SSLContext)

    def test_falls_back_to_none_when_certifi_is_missing(self, monkeypatch):
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "certifi":
                raise ImportError("no module named certifi")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert net.https_context() is None
