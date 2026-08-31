# PyInstaller entry-point wrapper.
#
# PyInstaller needs a script to trace as the frozen process's entry
# point. Pointing it directly at src/postmortem/desktop/app.py would
# fight the repo's src/-layout (that file is meant to be imported as
# ``postmortem.desktop.app``, not run as a loose script with a
# hand-rolled sys.path). CI installs the package for real first (``pip
# install ".[desktop]" pyinstaller``, see
# .github/workflows/release-desktop.yml) -- the exact same precondition
# the ``postmortem-desktop`` console script already relies on -- so
# this wrapper just imports the real entry point normally, sidestepping
# the src-layout entirely instead of fighting PyInstaller's module-path
# inference.
from postmortem.desktop.app import main

main()
