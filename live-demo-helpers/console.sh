#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# live-demo-helpers/console.sh — start the shared web console in staged mode.
#
# The console itself lives at assets/console/console.py and works without this
# script. CONSOLE_STAGES=1 makes it follow the DEMO_STAGE that demo1.sh,
# demo2.sh, demo3.sh and reset_demo.sh keep in demo.env, so the page reveals
# only what the walkthrough has reached. It serves http://localhost:8001 on
# 127.0.0.1 only (override with CONSOLE_PORT).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [ -f "$REPO_ROOT/demo.env" ]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/demo.env"
fi
if [ -f "$REPO_ROOT/.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.venv/bin/activate"
fi
export CONSOLE_STAGES=1
exec python3 "$REPO_ROOT/assets/console/console.py"
