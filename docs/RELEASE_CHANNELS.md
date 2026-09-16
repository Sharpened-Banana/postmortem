# Release channels: beta first, stable when told

Two kinds of desktop tag, one workflow (`release-desktop.yml`):

| tag | published as | who gets it |
|---|---|---|
| `beta-desktop-N.M` | GitHub **pre-release** | apps whose Settings > Update channel is **Beta** |
| `alpha-desktop-N` | full release, marked Latest | everyone (the **Stable** channel) |

`N` is the upcoming stable build number and `M` counts test builds on the
way to it: `beta-desktop-49.1`, `beta-desktop-49.2`, then `alpha-desktop-49`
when the tester says go. The updater orders builds by `(N, M)` with the
stable `N` ranking above every `N.M`, so a tester on 49.2 is offered 49
when it ships and is never offered anything older than what they run.

Both channels are built and signed identically; a beta is the same app a
few days early. Apps on the stable channel never see beta tags: the
stable updater only matches `alpha-desktop-N`, and apps older than
alpha-desktop-45 poll `/releases/latest`, which a pre-release never
becomes.

## Routine

1. Merge to `main` as usual.
2. `git tag beta-desktop-N.M && git push origin beta-desktop-N.M` -- testers
   are offered it on their next launch.
3. Test. Fix, merge, tag `N.M+1` as needed.
4. On the go-ahead: `git tag alpha-desktop-N && git push origin alpha-desktop-N`.

The addon has no update channel of its own (CurseForge takes the
`addon-v*` tags); testers copy `addon/Postmortem` into their AddOns
folder, as the README's in-game setup describes.

## Bootstrap

An app on the stable channel has to be switched to Beta once (Settings >
Update channel) and restarted; from then on it follows beta tags through
the normal in-app updater. The very first beta build after this feature
shipped predates the channel setting in installed apps, so that one is a
manual install from its GitHub release page.
