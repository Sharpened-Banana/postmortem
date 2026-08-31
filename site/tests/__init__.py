"""Makes site/tests a proper package.

Without this, pytest's default "prepend" import mode imports
site/tests/conftest.py as a bare top-level module named "conftest" --
which collides with the top-level tests/conftest.py (also imported as
bare "conftest") when both directories are collected in the same pytest
session (e.g. a plain `pytest` invocation with no path argument, now
that both are listed in pyproject.toml's `testpaths`). Whichever one
sys.modules["conftest"] ends up pointing to "wins", silently breaking
`from conftest import ...` imports in the top-level suite's test files.

Giving this directory an __init__.py makes pytest import its conftest.py
(and test_api.py) under the dotted name `tests.conftest` instead of the
bare `conftest`, since it walks up to `site/` (which deliberately has no
__init__.py of its own -- see mythic_site/__init__.py's docstring for
why) and uses that as the insertion/base point. That dotted name doesn't
collide with anything: the top-level `tests/` directory is never
registered as a package itself (it also has no __init__.py), so nothing
else claims `sys.modules["tests"]`.
"""
