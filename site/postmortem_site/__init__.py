"""postmortem_site: the public FastAPI service for browsing/uploading
Postmortem run reports (Warcraft Logs / Raider.io style, fully
public reads, no accounts).

Named ``postmortem_site`` rather than ``site`` because a top-level module
literally named ``site`` would shadow Python's own stdlib ``site``
module -- the ``site/`` directory this package lives in is a plain
(non-package) directory, not itself importable.
"""
