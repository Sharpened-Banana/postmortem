#!/usr/bin/env bash
#
# Refresh everything that goes stale when WoW ships a new patch or season.
# See docs/PATCH_UPDATE.md for what each step is and how to verify it.
#
# Safe to re-run: every step is idempotent, and nothing is committed,
# pushed or deployed -- this only rebuilds local data files and runs the
# tests. The release/deploy commands are printed at the end for you to
# run deliberately.
#
#   ./scripts/update-for-patch.sh --toc-interface 120200
#   ./scripts/update-for-patch.sh --interrupts-source ~/Downloads/mplus.json
#   ./scripts/update-for-patch.sh --savedvars path/to/SavedVariables/Postmortem.lua
#   ./scripts/update-for-patch.sh --with-spell-damage      # slow, hours
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

DATA_DIR="src/postmortem/data"
MDT_PATH="${MDT_PATH:-/Applications/World of Warcraft/_retail_/Interface/AddOns/MythicDungeonTools}"
LOGS_DIR="${LOGS_DIR:-/Applications/World of Warcraft/_retail_/Logs}"
WTF_DIR="${WTF_DIR:-/Applications/World of Warcraft/_retail_/WTF}"
SAVEDVARS=""
INTERRUPTS_SOURCE=""
TOC_INTERFACE=""
WITH_SPELL_DAMAGE=0

# Track what actually happened so the summary can't overstate it.
declare -a DONE=() SKIPPED=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mdt) MDT_PATH="$2"; shift 2 ;;
    --logs) LOGS_DIR="$2"; shift 2 ;;
    --savedvars) SAVEDVARS="$2"; shift 2 ;;
    --interrupts-source) INTERRUPTS_SOURCE="$2"; shift 2 ;;
    --toc-interface) TOC_INTERFACE="$2"; shift 2 ;;
    --with-spell-damage) WITH_SPELL_DAMAGE=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32m✓\033[0m %s\n' "$*"; DONE+=("$*"); }
skip() { printf '   \033[33m–\033[0m skipped: %s\n' "$*"; SKIPPED+=("$*"); }

PY="python3"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
pm() { "$PY" -m postmortem.cli "$@"; }

# --- 1. dungeon/enemy data (MDT) -------------------------------------------
# The dungeon pool and every enemy's forces value change every season, and
# route comparison is meaningless without them. Update the MDT addon in
# your WoW install FIRST -- this reads whatever is installed there.
say "Dungeon data (from Mythic Dungeon Tools)"
if [[ -d "$MDT_PATH" ]]; then
  pm extract-data "$MDT_PATH" -o "$DATA_DIR/dungeon_data.json"
  ok "dungeon_data.json rebuilt from $MDT_PATH"
else
  skip "no MDT addon at $MDT_PATH (pass --mdt PATH)"
fi

# --- 2. talent map ----------------------------------------------------------
# Patches reshuffle talent trees; without a refresh, a run's logged picks
# decode to node ids instead of names (see talents.py).
say "Talent data (from Blizzard's Game Data API)"
if [[ -n "${BNET_CLIENT_ID:-}" && -n "${BNET_CLIENT_SECRET:-}" ]]; then
  pm build-talent-data -o "$(mktemp -t talent).json"
  ok "talent_data.json rebuilt (auto-bundled)"
else
  skip "BNET_CLIENT_ID/BNET_CLIENT_SECRET not set -- https://develop.battle.net/access/"
fi

# --- 3. interrupt database --------------------------------------------------
# Needs a community mechanics file downloaded by hand (the command reads a
# local file and never fetches). Resolving ability NAMES against your own
# logs matters: guide ids often point at an ability's damage component
# rather than its interruptible cast.
say "Interrupt database"
if [[ -n "$INTERRUPTS_SOURCE" && -f "$INTERRUPTS_SOURCE" ]]; then
  logs=()
  if [[ -d "$LOGS_DIR" ]]; then
    while IFS= read -r f; do logs+=("$f"); done < <(
      find "$LOGS_DIR" -name 'WoWCombatLog*.txt' -type f 2>/dev/null | tail -5
    )
  fi
  if [[ ${#logs[@]} -gt 0 ]]; then
    pm build-interrupt-data "$INTERRUPTS_SOURCE" --resolve-from "${logs[@]}"
    ok "interrupt_data.json rebuilt, ids resolved from ${#logs[@]} log(s)"
    # same source file, its dispel-* rows: the dispel-efficiency list
    pm build-dispel-data "$INTERRUPTS_SOURCE" -o "$DATA_DIR/dispel_data.json" --resolve-from "${logs[@]}"
    ok "dispel_data.json rebuilt, ids resolved from ${#logs[@]} log(s)"
  else
    pm build-interrupt-data "$INTERRUPTS_SOURCE"
    ok "interrupt_data.json rebuilt (no logs found to resolve ids against)"
    pm build-dispel-data "$INTERRUPTS_SOURCE" -o "$DATA_DIR/dispel_data.json"
    ok "dispel_data.json rebuilt (no logs found to resolve ids against)"
  fi
else
  skip "no --interrupts-source FILE (see docs/PATCH_UPDATE.md for where to get one)"
fi

# --- 3b. avoidable-damage database -----------------------------------------
# Captured in-game: the addon reads Blizzard's own damage meter at the end
# of every key and records which spells it classifies as avoidable
# (PostmortemAvoidableDB in the addon's SavedVariables). Merges into the
# existing list -- it only ever grows -- and bundles it. WoW writes
# SavedVariables on logout or /reload, so play a key and log out first.
say "Avoidable-damage database (from the addon's SavedVariables)"
SV_FILES=()
if [[ -n "$SAVEDVARS" ]]; then
  SV_FILES=("$SAVEDVARS")
else
  while IFS= read -r f; do SV_FILES+=("$f"); done < <(
    find "$WTF_DIR/Account" -path '*/SavedVariables/Postmortem.lua' -type f 2>/dev/null
  )
fi
if [[ ${#SV_FILES[@]} -gt 0 ]]; then
  for sv in "${SV_FILES[@]}"; do
    if grep -q '^PostmortemAvoidableDB' "$sv"; then
      pm extract-avoidable "$sv" -o "$DATA_DIR/avoidable_spells.json"
      ok "avoidable_spells.json merged from $sv"
    else
      skip "$sv has no PostmortemAvoidableDB yet (finish a key with the addon loaded, then log out)"
    fi
  done
else
  skip "no Postmortem.lua under $WTF_DIR/Account (pass --savedvars FILE)"
fi

# --- 4. spell damage (optional, slow) --------------------------------------
# Only the kick-value *fallback*, and it samples a lot of public reports --
# hours, and bounded by Warcraft Logs' hourly points budget. Resumable.
say "Spell damage (Warcraft Logs sampling)"
if [[ "$WITH_SPELL_DAMAGE" -eq 1 ]]; then
  if [[ -n "${WCL_CLIENT_ID:-}" && -n "${WCL_CLIENT_SECRET:-}" ]]; then
    pm build-spell-damage
    ok "spell_damage.json rebuilt"
  else
    skip "WCL_CLIENT_ID/WCL_CLIENT_SECRET not set"
  fi
else
  skip "not requested (pass --with-spell-damage; takes hours)"
fi

# --- 5. addon interface version --------------------------------------------
# A .toc whose Interface doesn't match the live patch makes WoW mark the
# addon out of date. Format is MMmmpp: patch 12.2.0 -> 120200.
say "Addon .toc interface version"
TOC="addon/Postmortem/Postmortem.toc"
CURRENT_TOC="$(grep -m1 '^## Interface:' "$TOC" | tr -dc '0-9')"
if [[ -n "$TOC_INTERFACE" ]]; then
  if [[ "$TOC_INTERFACE" == "$CURRENT_TOC" ]]; then
    ok ".toc already at $CURRENT_TOC"
  else
    "$PY" - "$TOC" "$TOC_INTERFACE" <<'PYEOF'
import re, sys
path, version = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
open(path, "w", encoding="utf-8").write(
    re.sub(r"^## Interface: *\d+", f"## Interface: {version}", text, count=1, flags=re.M)
)
PYEOF
    ok ".toc interface $CURRENT_TOC -> $TOC_INTERFACE"
  fi
else
  skip "no --toc-interface NNNNNN (currently $CURRENT_TOC)"
fi

# --- 6. tests ---------------------------------------------------------------
say "Tests"
if "$PY" -m pytest tests site/tests -q >/tmp/pm-patch-tests.log 2>&1; then
  ok "$(tail -1 /tmp/pm-patch-tests.log)"
else
  printf '   \033[31m✗ tests failed\033[0m -- see /tmp/pm-patch-tests.log\n'
  tail -15 /tmp/pm-patch-tests.log
  exit 1
fi

# --- summary ----------------------------------------------------------------
say "Summary"
for d in "${DONE[@]}"; do printf '   \033[32m✓\033[0m %s\n' "$d"; done
for s in "${SKIPPED[@]}"; do printf '   \033[33m–\033[0m %s\n' "$s"; done

say "Changed data files"
git status --short -- "$DATA_DIR" addon/Postmortem/Postmortem.toc || true

cat <<'NEXT'

== Next steps (deliberate, not automated) ==

  1. Review the diff, especially dungeon_data.json's dungeon list:
       git diff --stat src/postmortem/data/ addon/
  2. Commit on a branch and open a PR.
  3. Deploy the site (it bundles this same data):
       fly deploy
  4. Cut a desktop release so the app ships the new data:
       git tag alpha-desktop-N && git push origin alpha-desktop-N
  5. If the .toc changed, push the addon to your WoW install and /reload:
       rsync -a addon/Postmortem/ "/Applications/World of Warcraft/_retail_/Interface/AddOns/Postmortem/"

NEXT
