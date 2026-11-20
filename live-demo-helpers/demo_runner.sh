# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# demo_runner.sh — a tiny sourceable helper for presenting the live demos.
#
# This file is meant to be *sourced* by each live-demo-helpers/demoN.sh, so it
# deliberately does NOT set `set -euo pipefail` (that would change the caller's
# shell). The sourcing demoN.sh owns `set -euo pipefail`; the functions here are
# written defensively.
#
# Four helpers:
#   banner "<title>"      — bold divider announcing a section.
#   step   "<narration>"  — a dim presenter sentence; waits for Enter.
#   note   "<text>"       — an inline aside; no pause.
#   run    "<command>"    — prints the command in bright cyan, waits for Enter,
#                           then runs it with `eval` in the CURRENT shell so
#                           cd/export/source persist across sequential run
#                           calls. Prints a green ok on exit 0; on non-zero
#                           prints a red FAILURE line and returns 1 (the caller
#                           under `set -e` then exits).
#   quiet  "<label>" "<command>"
#                         — like `run`, but for plumbing the audience shouldn't
#                           watch (fetching the gateway URL, cd, sourcing env).
#                           Prints only a dim "· <label>…" line — no command
#                           reveal, no Enter pause — then evals the command in
#                           the CURRENT shell (export/cd/source persist) and
#                           stops on failure with the same red block as `run`.
#                           Use `step`+`run` for teaching moments; `quiet` for
#                           the necessary noise between them.
#
# --auto contract: each demoN.sh exports DEMO_AUTO=1 when invoked with --auto.
# When DEMO_AUTO is set, step/run skip the pre-Enter pause but still print, and
# run still executes and still stops on the first failure.
#
# Color degrades gracefully: ANSI is emitted only when stdout is a TTY
# (`[ -t 1 ]`) and NO_COLOR is unset; otherwise every color var is empty.

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  _DR_CYAN=$'\033[1;36m'   # bright cyan — the command line
  _DR_DIM=$'\033[2m'       # dim — step narration
  _DR_GREEN=$'\033[0;32m'  # green — ok
  _DR_RED=$'\033[0;31m'    # red — failure
  _DR_BOLD=$'\033[1m'      # bold — banner
  _DR_RESET=$'\033[0m'
else
  _DR_CYAN=""
  _DR_DIM=""
  _DR_GREEN=""
  _DR_RED=""
  _DR_BOLD=""
  _DR_RESET=""
fi

# Wait for the presenter to press Enter, unless --auto (DEMO_AUTO) is set.
_dr_pause() {
  if [ -z "${DEMO_AUTO:-}" ]; then
    # Read from the terminal; ignore EOF so --auto-less piping doesn't wedge.
    read -r _dr_discard || true
  fi
}

banner() {
  printf '\n%s============================================================%s\n' "$_DR_BOLD" "$_DR_RESET"
  printf '%s  %s%s\n' "$_DR_BOLD" "$1" "$_DR_RESET"
  printf '%s============================================================%s\n' "$_DR_BOLD" "$_DR_RESET"
}

step() {
  printf '\n%s# %s%s\n' "$_DR_DIM" "$1" "$_DR_RESET"
  _dr_pause
}

note() {
  printf '%s  (%s)%s\n' "$_DR_DIM" "$1" "$_DR_RESET"
}

run() {
  local _dr_cmd="$1"
  printf '\n%s$ %s%s\n' "$_DR_CYAN" "$_dr_cmd" "$_DR_RESET"
  _dr_pause
  # Test the eval directly in the `if` so the caller's `set -e` is suspended for
  # this evaluation; otherwise a non-zero exit would abort the function here
  # (before the status line) and the red [FAILED] line would never render.
  local _dr_rc=0
  if eval "$_dr_cmd"; then
    printf '%s[ok]%s\n' "$_DR_GREEN" "$_DR_RESET"
  else
    _dr_rc=$?
    printf '%s[FAILED] exit %d — stopping the demo here.%s\n' "$_DR_RED" "$_dr_rc" "$_DR_RESET"
    return 1
  fi
}

# Plumbing the audience shouldn't watch: prints only a dim label, no command and
# no pause, then runs it. Same eval-in-current-shell and stop-on-failure as run.
quiet() {
  local _dr_label="$1"
  local _dr_cmd="$2"
  printf '%s· %s…%s\n' "$_DR_DIM" "$_dr_label" "$_DR_RESET"
  # Same `if eval` pattern as run() so the caller's `set -e` is suspended for the
  # evaluation and the red [FAILED] line still renders on a non-zero exit.
  local _dr_rc=0
  if eval "$_dr_cmd"; then
    :
  else
    _dr_rc=$?
    printf '%s[FAILED] exit %d — stopping the demo here.%s\n' "$_DR_RED" "$_dr_rc" "$_DR_RESET"
    return 1
  fi
}
