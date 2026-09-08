"""The Windows .NET binding in postmortem.desktop.app: try the modern
runtime first, fall back to the built-in .NET Framework, and only give
up (with a dialog, not a traceback) when neither loads."""

from __future__ import annotations

import pytest

pytest.importorskip("webview")

from postmortem.desktop import app  # noqa: E402


class TestLoadDotnet:
    def test_uses_coreclr_with_the_desktop_runtime_config(self):
        tried = []
        assert app.load_dotnet(lambda n, **kw: tried.append((n, kw)), "/x/cfg.json") == "coreclr"
        assert tried == [("coreclr", {"runtime_config": "/x/cfg.json"})]

    def test_falls_back_to_netfx_when_no_modern_runtime_is_installed(self):
        tried = []

        def loader(name, **kw):
            tried.append(name)
            if name == "coreclr":
                raise RuntimeError("Failed to create a .NET runtime (coreclr): Can not determine dotnet root")

        assert app.load_dotnet(loader, "/x/cfg.json") == "netfx"
        assert tried == ["coreclr", "netfx"]

    def test_raises_the_last_failure_when_nothing_loads(self):
        def loader(name, **kw):
            raise RuntimeError(f"{name} unavailable")

        with pytest.raises(RuntimeError, match="netfx unavailable"):
            app.load_dotnet(loader, "/x/cfg.json")

    def test_runtime_config_asks_for_the_windows_desktop_flavour(self, tmp_path, monkeypatch):
        # without it CoreCLR is the console flavour, which has no
        # System.Windows.Forms and pywebview can't open a window
        import json
        from postmortem import appdirs
        monkeypatch.setattr(appdirs, "config_dir", lambda: tmp_path)
        path = app.coreclr_runtime_config_path()
        cfg = json.loads(open(path, encoding="utf-8").read())["runtimeOptions"]
        assert cfg["framework"]["name"] == "Microsoft.WindowsDesktop.App"
        assert cfg["rollForward"] == "LatestMajor"  # an installed 8.x satisfies the 6.0.0 floor

    def test_the_explanation_points_at_the_desktop_runtime(self):
        assert "Desktop Runtime" in app.DOTNET_MESSAGE
        assert app.DOTNET_DOWNLOAD_URL.startswith("https://dotnet.microsoft.com/")


class TestPreloadAssemblies:
    def test_loads_what_exists_and_skips_what_does_not(self):
        class FakeClr:
            missing = {"Microsoft.Win32.Registry", "System.Drawing.Common"}
            def __init__(self): self.refs = []
            def AddReference(self, name):
                if name in self.missing:
                    raise Exception(f"Could not load file or assembly '{name}'")
                self.refs.append(name)

        clr = FakeClr()
        loaded = app.preload_desktop_assemblies(clr)
        assert "Microsoft.Win32.SystemEvents" in loaded  # the one that bit on 2026-09-08
        assert "System.Windows.Forms" in loaded
        assert not (set(loaded) & clr.missing)
        assert loaded == clr.refs

