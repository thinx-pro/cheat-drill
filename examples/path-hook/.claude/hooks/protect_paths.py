#!/usr/bin/env python3
"""The common "protected files" hook pattern: block Edit/MultiEdit/Write on a list of paths.

Written for this comparison as a representative of the pattern, not copied from any project.
"""
import json
import os
import sys

PROTECTED = ("tests/", "scripts/compare.py", ".github/", ".claude/", ".pre-commit-config.yaml")

event = json.load(sys.stdin)
path = event.get("tool_input", {}).get("file_path", "")
root = os.environ.get("CLAUDE_PROJECT_DIR", event.get("cwd", ""))
rel = os.path.relpath(path, root) if path else ""
if any(rel == p or rel.startswith(p) for p in PROTECTED):
    print(f"{rel} is protected", file=sys.stderr)
    sys.exit(2)
