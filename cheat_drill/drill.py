"""Load Claude Code settings, build a throwaway fixture repo, and run every scenario through
the configured PreToolUse hooks (for real) and permission rules (approximated)."""
import json
import os
import re
import shutil
import subprocess
import tempfile

from . import perms
from .scenarios import FIXTURE_FILES, all_scenarios, expand

TEST_DIR_CANDIDATES = ("tests", "test", "spec", "__tests__")


def detect_test_dir(project):
    for d in TEST_DIR_CANDIDATES:
        if os.path.isdir(os.path.join(project, d)):
            return d
    return "tests"


def settings_files(project, include_user=True, extra=()):
    home = os.path.expanduser("~")
    files = []
    if include_user:
        files.append(os.path.join(home, ".claude", "settings.json"))
    files += [os.path.join(project, ".claude", "settings.json"),
              os.path.join(project, ".claude", "settings.local.json")]
    files += [os.path.abspath(e) for e in extra]
    return [f for f in files if os.path.isfile(f)]


def load_config(files):
    """-> rules {deny/ask/allow: [(rule, file)]}, hooks [(matcher, command, timeout, file)], skipped [str]."""
    rules = {"deny": [], "ask": [], "allow": []}
    hooks, skipped = [], []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            skipped.append(f"{f}: unreadable ({e.__class__.__name__})")
            continue
        p = data.get("permissions") or {}
        for k in rules:
            for r in p.get(k) or []:
                if isinstance(r, str):
                    rules[k].append((r, f))
        for group in (data.get("hooks") or {}).get("PreToolUse") or []:
            matcher = group.get("matcher") or ""
            for h in group.get("hooks") or []:
                if h.get("type", "command") == "command" and h.get("command"):
                    hooks.append((matcher, h["command"], h.get("timeout"), f))
                else:
                    skipped.append(f"{f}: {h.get('type')} hook not executed (only command hooks are drilled)")
    return rules, hooks, skipped


def matcher_hits(matcher, tool):
    if matcher in ("", "*"):
        return True
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return matcher == tool


def build_fixture(project, t):
    root = tempfile.mkdtemp(prefix="cheat-drill-")
    for rel, body in FIXTURE_FILES.items():
        p = os.path.join(root, expand(rel, t))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
    src = os.path.join(project, ".claude")
    if os.path.isdir(src):
        shutil.copytree(src, os.path.join(root, ".claude"), symlinks=True,
                        ignore=shutil.ignore_patterns("*.jsonl", "logs", "todos", "projects"))
    sp = os.path.join(root, ".claude", "settings.json")
    if not os.path.exists(sp):
        os.makedirs(os.path.dirname(sp), exist_ok=True)
        with open(sp, "w") as fh:
            fh.write("{\n  \"hooks\": {\"PreToolUse\": []}\n}\n")
    try:
        subprocess.run(["git", "init", "-q", root], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass
    hk = os.path.join(root, ".git", "hooks", "pre-commit")
    os.makedirs(os.path.dirname(hk), exist_ok=True)
    with open(hk, "w") as fh:
        fh.write("#!/bin/sh\npytest -q\n")
    os.chmod(hk, 0o755)
    return root


def absolutize(tool_input, root):
    out = dict(tool_input)
    for k in ("file_path", "notebook_path"):
        if k in out and not os.path.isabs(out[k]):
            out[k] = os.path.join(root, out[k])
    return out


def run_hook(command, event, root, timeout, transcript):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=root)
    try:
        r = subprocess.run(command, shell=True, input=json.dumps(event), cwd=root, env=env,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "error", f"timed out after {timeout}s"
    first = lambda s: (s.strip().splitlines() or [""])[0][:160]
    if r.returncode == 2:
        return "deny", first(r.stderr) or "exit 2"
    if r.returncode != 0:
        return "error", f"exit {r.returncode}: {first(r.stderr)}"
    out = r.stdout.strip()
    if out.startswith("{"):
        try:
            data = json.loads(out)
        except ValueError:
            return None, ""
        hso = data.get("hookSpecificOutput") or {}
        d = hso.get("permissionDecision")
        reason = hso.get("permissionDecisionReason") or data.get("reason") or data.get("stopReason") or ""
        if d in ("deny", "ask", "allow"):
            return d, reason[:160]
        if data.get("decision") == "block":
            return "deny", reason[:160]
        if data.get("decision") == "approve":
            return "allow", reason[:160]
        if data.get("continue") is False:
            return "deny", reason[:160]
    return None, ""


def drill(project, include_user=True, extra_settings=(), unattended=False, in_place=False,
          timeout=10, test_dir=None, only=None):
    project = os.path.abspath(project)
    t = test_dir or detect_test_dir(project)
    files = settings_files(project, include_user, extra_settings)
    rules, hooks, skipped = load_config(files)
    root = project if in_place else build_fixture(project, t)
    home = os.path.expanduser("~")
    transcript = os.path.join(tempfile.mkdtemp(prefix="cheat-drill-t-"), "transcript.jsonl")
    open(transcript, "w").close()
    results = []
    try:
        for sc in all_scenarios(t):
            if only and sc["category"] not in only and sc["id"] not in only:
                continue
            ti = absolutize(sc["input"], root)
            event = {"session_id": "cheat-drill", "transcript_path": transcript, "cwd": root,
                     "permission_mode": "bypassPermissions" if unattended else "default",
                     "hook_event_name": "PreToolUse", "tool_name": sc["tool"], "tool_input": ti}
            by = []
            for matcher, cmd, hto, src in hooks:
                if not matcher_hits(matcher, sc["tool"]):
                    continue
                d, reason = run_hook(cmd, event, root, min(timeout, hto or timeout), transcript)
                if d:
                    by.append({"kind": "hook", "decision": d, "what": cmd, "file": src, "reason": reason})
            d, rule, src = perms.evaluate(rules, sc["tool"], ti, root, home)
            if d:
                by.append({"kind": "rule", "decision": d, "what": rule, "file": src, "reason": ""})
            decisions = {b["decision"] for b in by}
            if "deny" in decisions:
                verdict = "BLOCK"
            elif "ask" in decisions:
                verdict = "ALLOW" if unattended else "ASK"
            else:
                verdict = "ALLOW"
            errors = [b for b in by if b["decision"] == "error"]
            results.append(dict(sc, input=sc["input"], verdict=verdict, by=by, hook_errors=len(errors)))
    finally:
        if not in_place:
            shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(os.path.dirname(transcript), ignore_errors=True)
    return {"project": project, "test_dir": t, "settings": files, "rules": {k: len(v) for k, v in rules.items()},
            "hooks": [{"matcher": m, "command": c, "file": f} for m, c, _, f in hooks],
            "skipped": skipped, "unattended": unattended, "in_place": in_place, "results": results,
            "summary": summarize(results)}


def summarize(results):
    cheats = [r for r in results if not r["control"]]
    controls = [r for r in results if r["control"]]
    cats = {}
    for r in cheats:
        c = cats.setdefault(r["category"], {"total": 0, "blocked": 0, "ask": 0})
        c["total"] += 1
        c["blocked"] += r["verdict"] == "BLOCK"
        c["ask"] += r["verdict"] == "ASK"
    return {
        "cheats": len(cheats),
        "blocked": sum(r["verdict"] == "BLOCK" for r in cheats),
        "ask": sum(r["verdict"] == "ASK" for r in cheats),
        "allowed": sum(r["verdict"] == "ALLOW" for r in cheats),
        "controls": len(controls),
        "false_block": sum(r["verdict"] == "BLOCK" for r in controls),
        "false_ask": sum(r["verdict"] == "ASK" for r in controls),
        "categories": cats,
        "hook_errors": sum(r["hook_errors"] for r in results),
    }
