# Vendored library provenance

These four libraries are third-party code committed into this repo so the
packaged addon is self-contained (LibDBIcon and LibDataBroker both assert
without LibStub and CallbackHandler present, so all four are required).

They were copied byte-for-byte out of MythicDungeonTools' own `Libs/`
folder — the same build this machine had installed — rather than fetched
from each project separately. Nothing here is modified.

| Library | Upstream | Copied from |
| --- | --- | --- |
| LibStub | https://repos.curseforge.com/wow/libstub | MythicDungeonTools 6.2.16 (`Libs/LibStub/`) |
| CallbackHandler-1.0 | https://repos.curseforge.com/wow/callbackhandler | MythicDungeonTools 6.2.16 (`Libs/CallbackHandler-1.0/`) |
| LibDataBroker-1.1 | https://github.com/tekkub/libdatabroker-1-1 | MythicDungeonTools 6.2.16 (`Libs/LibDataBroker-1.1/`) |
| LibDBIcon-1.0 | https://github.com/Nevcairiel/LibDBIcon-1.0 | MythicDungeonTools 6.2.16 (`Libs/LibDBIcon-1.0/`) |

## SHA-256 of every vendored file, as committed

```
d8a175f5823a999d8a188ee5e8e1133c393fd8c25b71931ab929d4691eff750b  CallbackHandler-1.0/CallbackHandler-1.0.lua
d79b3eada649684543bbbc3c511c2515e16d644f198fa991a201ee64d045b0f2  LibDataBroker-1.1/LibDataBroker-1.1.lua
bd9249c720b511f1ca23f45ac1117c079b054060d764550efe04875d8d9e26bc  LibDBIcon-1.0/LibDBIcon-1.0.lua
d34235c85f869505fdd9ef321d2420f69db7d2eb873d2b3bbf57584a314060fa  LibStub/LibStub.lua
```

Regenerate with:

```sh
cd addon/Postmortem/libs && shasum -a 256 */*.lua
```

## Updating one of these

Copy the new file in, then update its row and its hash above in the same
commit, so a future reader can diff what is here against a known upstream
point instead of guessing. `.pkgmeta` does not fetch these — what is in
this folder is exactly what ships.
