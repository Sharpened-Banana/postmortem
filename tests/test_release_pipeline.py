"""Guards on the release pipeline's non-Python files: the desktop release
workflow and the Windows installer script. Neither can run under pytest,
so these read the files and assert the specific guards stay in place --
the same approach tests/test_update_channels.py takes for release logic.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "build" / "postmortem.iss"


def _iss_setup_directives() -> dict[str, str]:
    """``Key=Value`` lines of the installer's [Setup] section, comments
    stripped."""
    out: dict[str, str] = {}
    section = None
    for raw in ISS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "Setup" and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


class TestInstallerIsPerUser:
    """An "all users" install goes to Program Files, which the in-app
    updater (running as the user) can never replace -- so every
    self-update from one failed. The installer must not offer it."""

    def test_no_all_users_override(self):
        setup = _iss_setup_directives()
        assert setup.get("PrivilegesRequired") == "lowest"
        assert "PrivilegesRequiredOverridesAllowed" not in setup

    def test_default_dir_is_the_per_user_programs_folder(self):
        # {autopf} under lowest privileges is %LOCALAPPDATA%\Programs.
        assert re.match(r"^\{autopf\}\\", _iss_setup_directives()["DefaultDirName"])

    def test_app_id_never_changes(self):
        # Changing it would stack a second install instead of upgrading.
        assert _iss_setup_directives()["AppId"] == "{{8E5F1B42-7C3D-4A9E-9F21-6D0B5A7C4E13}"
