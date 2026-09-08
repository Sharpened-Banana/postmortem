"""The Windows .NET binding in postmortem.desktop.app: try the modern
runtime first, fall back to the built-in .NET Framework, and only give
up (with a dialog, not a traceback) when neither loads."""

from __future__ import annotations

import pytest

pytest.importorskip("webview")

from postmortem.desktop import app  # noqa: E402


class TestLoadDotnet:
    def test_uses_coreclr_when_it_loads(self):
        tried = []
        assert app.load_dotnet(tried.append) == "coreclr"
        assert tried == ["coreclr"]

    def test_falls_back_to_netfx_when_no_modern_runtime_is_installed(self):
        tried = []

        def loader(name):
            tried.append(name)
            if name == "coreclr":
                raise RuntimeError("Failed to create a .NET runtime (coreclr): Can not determine dotnet root")

        assert app.load_dotnet(loader) == "netfx"
        assert tried == ["coreclr", "netfx"]

    def test_raises_the_last_failure_when_nothing_loads(self):
        def loader(name):
            raise RuntimeError(f"{name} unavailable")

        with pytest.raises(RuntimeError, match="netfx unavailable"):
            app.load_dotnet(loader)

    def test_the_explanation_points_at_the_desktop_runtime(self):
        assert "Desktop Runtime" in app.DOTNET_MESSAGE
        assert app.DOTNET_DOWNLOAD_URL.startswith("https://dotnet.microsoft.com/")
