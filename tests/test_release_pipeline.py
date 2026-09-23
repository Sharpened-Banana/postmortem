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


WORKFLOW = ROOT / ".github" / "workflows" / "release-desktop.yml"


def _jobs() -> dict[str, str]:
    """Each job's raw text, keyed by job id, in file order. The workflow
    is plain block YAML with two-space indentation; stdlib has no YAML
    parser and none is needed to find a job's own lines."""
    text = WORKFLOW.read_text(encoding="utf-8")
    body = text.split("\njobs:\n", 1)[1]
    jobs: dict[str, str] = {}
    current = None
    for line in body.splitlines():
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            current = m.group(1)
            jobs[current] = ""
        elif current is not None:
            jobs[current] += line + "\n"
    return jobs


def _tag_patterns(job_text: str) -> list[re.Pattern]:
    return [re.compile(p) for p in re.findall(r'=~ (\^\S+\$) \]\]', job_text)]


class TestReleaseTagValidation:
    """alpha-desktop-50.1 passed the old glob check and would have been
    published as the Latest stable release that build_key() -- and so
    every installed updater -- can't parse. The check also ran only
    after the release was already created."""

    ACCEPTED = ["alpha-desktop-49", "alpha-desktop-50", "beta-desktop-50.5"]
    REJECTED = ["alpha-desktop-50.1", "beta-desktop-50", "alpha-desktop-",
                "alpha-desktop-5x", 'alpha-desktop-1"; import os #',
                "beta-desktop-1.2.3", "beta-desktop-1.x", "addon-v0.3.4"]

    def test_validation_is_the_first_job(self):
        assert list(_jobs())[0] == "validate-tag"

    def test_the_first_job_accepts_exactly_the_updater_parseable_tags(self):
        from postmortem.desktop.updater import build_key

        patterns = _tag_patterns(_jobs()["validate-tag"])
        assert len(patterns) == 2
        for tag in self.ACCEPTED:
            assert any(p.search(tag) for p in patterns), tag
            assert build_key(tag) is not None
        for tag in self.REJECTED:
            assert not any(p.search(tag) for p in patterns), tag
            # The invariant that matters: nothing the workflow accepts is
            # a tag installed updaters can't order.
            assert build_key(tag) is None

    def test_the_build_jobs_own_check_is_just_as_strict(self):
        build = _jobs()["build"]
        patterns = _tag_patterns(build)
        assert len(patterns) == 2
        for tag in self.REJECTED:
            assert not any(p.search(tag) for p in patterns), tag

    def test_nothing_is_created_before_validation_and_tests(self):
        jobs = _jobs()
        assert re.search(r"needs: \[validate-tag, tests\]", jobs["release"])
        assert "needs: validate-tag" in jobs["tests"]
        assert "needs: release" in jobs["build"]
        for early in ("validate-tag", "tests"):
            assert "gh release" not in jobs[early]

    def test_the_tests_job_runs_the_suite(self):
        assert "python -m pytest tests" in _jobs()["tests"]


def _step(job_text: str, name: str) -> str:
    """One step's text: from its ``- name:`` line to the next step."""
    start = job_text.index(f"- name: {name}")
    nxt = job_text.find("\n      - ", start + 1)
    return job_text[start:] if nxt == -1 else job_text[start:nxt]


class TestCanonicalRepoNeverShipsUnsigned:
    """Missing signing secrets used to downgrade to an unsigned build with
    a notice -- fine for a fork, but in the real repository that build is
    what every installed app auto-updates to."""

    CANONICAL = '[ "$GITHUB_REPOSITORY" = "Sharpened-Banana/postmortem" ]'

    def _guarded(self, step: str, missing_marker: str) -> None:
        # Inside the "secret missing" branch, the canonical-repo check
        # must fail the job before any path that carries on unsigned.
        branch = step[step.index(missing_marker):]
        guard = branch.index(self.CANONICAL)
        assert "exit 1" in branch[guard:guard + 300]
        carry_on = min(i for i in (branch.find("exit 0"), branch.find("::notice::"),
                                   branch.find("::warning::")) if i != -1)
        assert guard < carry_on

    def test_macos_signing_certificate(self):
        step = _step(_jobs()["build"], "Import the Developer ID certificate")
        self._guarded(step, 'if [ -z "$MACOS_CERT_P12" ]')

    def test_macos_notarization(self):
        step = _step(_jobs()["build"], "Notarize and staple")
        self._guarded(step, 'if [ -z "$APPLE_ID" ]')

    def test_windows_signing(self):
        step = _step(_jobs()["build"], "Decide whether Windows signing is configured")
        self._guarded(step, 'echo "WINDOWS_SIGNING=1"')
