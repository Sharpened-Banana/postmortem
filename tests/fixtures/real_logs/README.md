# Real combat log fixtures

Real WoWCombatLog.txt excerpts, not synthetic `LogBuilder()` output -- kept
separate from the rest of `tests/` (all hand-built fixtures) because they're
large and exist for a different reason: exercising the real parser/tokenizer
against genuine WoW-formatted data at scale, not just one specific code path.

Every file here has had every real player name (`"Name-Realm-Region"`) replaced
with a synthetic placeholder (`Tank-TestRealm-US`, `DPS1-TestRealm-US`, ...)
before being committed -- these came from a real group, not just the
repo owner, and this is a public repository. Player GUIDs
(`Player-162-0C1ECF61`) are left as-is; they carry no identifying information
on their own. Anonymization is a straight string substitution done once at
capture time -- it doesn't change line counts, event ordering, or timestamps,
so the file is otherwise byte-for-byte what the game wrote.

## `abandoned_key_instance_mismatch.txt`

Captured 2026-08-30 (`WoWCombatLog-083026_220430.txt`, lines 330-32090).
A real Voidscar Arena +10 that was genuinely abandoned, immediately followed
by the start of a real Blinding Vale +10 (not included past its own opening
lines here -- that run continues for ~189k more lines in the original log,
well beyond what this fixture needs).

This is the exact real data that surfaced a real bug (2026-09-01, see
`TestMismatchedEndDoesNotCloseAWrongRun` in `tests/test_combatlog.py` for the
fast synthetic reproduction): WoW fires a `CHALLENGE_MODE_END` with all-zero
stats immediately before *every* `CHALLENGE_MODE_START`, always carrying that
upcoming key's own instance id -- confirmed 100% consistent across 13 real
keys spanning 7 real logs from one account. Harmless when no run is open
between keys, but a genuinely-abandoned key immediately followed by a new one
meant `segment_runs()` used to close the abandoned run with that mismatched
event, misreporting it as a failed/depleted completion instead of correctly
flagging it as abandoned.
