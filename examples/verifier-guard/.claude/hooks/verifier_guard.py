#!/usr/bin/env python3
"""verifier_guard: a Claude Code PreToolUse hook that protects verification assets.

Stops an agent from weakening the thing that checks its work: tests, golden files,
comparison scripts, CI, git hooks, coverage floors and the agent's own hook config.
Covers file tools (Edit / MultiEdit / Write / NotebookEdit) and the shell (sed -i, perl -i,
redirects, tee, cp, mv, rm, chmod, python -c, git checkout -- path, --no-verify,
core.hooksPath, HUSKY=0, SKIP=, assume-unchanged, base64 | sh, bash -c ...).

Standalone, Python 3.8+, no dependencies. Install:
  .claude/hooks/verifier_guard.py  +  in .claude/settings.json:
  {"hooks": {"PreToolUse": [{"matcher": "Edit|MultiEdit|Write|NotebookEdit|Bash",
     "hooks": [{"type": "command", "command": "python3 \\"$CLAUDE_PROJECT_DIR/.claude/hooks/verifier_guard.py\\""}]}]}}

Optional .claude/verifier-guard.json:
  {"protect": ["extra/glob/**"], "allow": ["tests/scratch/**"], "mode": "deny" | "ask"}
VERIFIER_GUARD_MODE=ask|deny overrides the mode. "ask" hands each hit to a human instead.
Adding a brand-new test file is allowed; changing or removing an existing one is not.
"""
import json
import os
import re
import shlex
import sys

VERSION = "0.1.0"

# Verification assets. Globs are relative to the project root; ** spans directories.
PROTECT = [
    "tests/**", "test/**", "spec/**", "**/__tests__/**", "**/__snapshots__/**", "**/*.snap",
    "**/test_*.py", "**/*_test.py", "**/conftest.py", "**/*_test.go", "**/*.test.*", "**/*.spec.*",
    "**/fixtures/**", "**/golden/**", "**/testdata/**", "**/expected/**",
    "scripts/compare*", "scripts/verify*", "scripts/check*", "tools/compare*", "tools/verify*",
    ".github/workflows/**", ".gitlab-ci.yml", ".circleci/**", "Jenkinsfile", "azure-pipelines.yml",
    ".pre-commit-config.yaml", ".husky/**", ".git/hooks/**", ".githooks/**", "lefthook.yml", ".lefthook.yml",
    ".claude/settings.json", ".claude/settings.local.json", ".claude/hooks/**", ".claude/verifier-guard.json",
    "pytest.ini", "tox.ini", "noxfile.py", ".coveragerc", "codecov.yml", ".nycrc", ".nycrc.json",
    "jest.config.*", "vitest.config.*", "karma.conf.*", "playwright.config.*", "cypress.config.*",
]
# New files under these may be created (adding tests is good); everything else protected is locked.
NEW_FILE_OK = ["tests/**", "test/**", "spec/**", "**/__tests__/**", "**/test_*.py", "**/*_test.py",
               "**/*_test.go", "**/*.test.*", "**/*.spec.*"]
# Shared config files: only edits touching test/coverage settings are blocked.
MIXED_CONFIG = re.compile(r"(^|/)(pyproject\.toml|setup\.cfg|package\.json|Cargo\.toml|\.mocharc\.[a-z]+|"
                          r"tsconfig\.json|Makefile|justfile)$")
TEST_KEYS = re.compile(r"fail_under|fail-under|cov-fail-under|addopts|testpaths|--deselect|--ignore|"
                       r"\bskip|xfail|coverageThreshold|testPathIgnorePatterns|testMatch|\"test\"\s*:|"
                       r"collectCoverage|\"?(branches|functions|lines|statements)\"?\s*:\s*\d|"
                       r"\"strict\"|noImplicitAny|^\s*test\s*:", re.M | re.I)
REVIEWER_TALK = re.compile(
    r"(note (to|for) (the )?review(er)?s?|\breviewers?\s*:|do not flag|don'?t flag|"
    r"(deviation|mismatch|difference|discrepancy) is (intentional|expected|approved|fine)|"
    r"ignore (this|the) (diff|mismatch|failure|check))", re.I)

WRITERS = {"cp", "install", "rsync", "ln", "dd"}           # destination is written
MOVERS = {"mv", "rm", "unlink", "truncate", "shred", "chmod", "chown", "chgrp", "touch"}  # every path arg
INPLACE = {"sed", "gsed", "perl", "ruby"}                  # with -i
INTERP = {"python", "python3", "node", "ruby", "perl", "deno", "bun"}
WRITE_CODE = re.compile(r"(open\([^)]*['\"][wa+]|write_text|write_bytes|writeFile|appendFile|unlink|"
                        r"rmtree|os\.remove|os\.replace|os\.rename|shutil\.(copy|move)|truncate|rmSync|renameSync)")
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
SEP = {";", "&&", "||", "|", "&", "\n"}


def glob_rx(pat):
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


class Guard:
    def __init__(self, root, cfg):
        self.root = os.path.realpath(root)
        self.protect = [glob_rx(p) for p in PROTECT + list(cfg.get("protect") or [])]
        self.allow = [glob_rx(p) for p in cfg.get("allow") or []]
        self.new_ok = [glob_rx(p) for p in NEW_FILE_OK]

    def rel(self, path, cwd):
        if not path or "\n" in path:
            return None
        p = os.path.expanduser(path)
        if not os.path.isabs(p):
            p = os.path.join(cwd, p)
        p = os.path.normpath(p)
        try:
            rp = os.path.realpath(p)
        except OSError:
            rp = p
        for cand in (rp, p):
            if cand == self.root:
                return "."
            if cand.startswith(self.root + os.sep):
                return cand[len(self.root) + 1:]
        if p.startswith(os.path.normpath(self.root) + os.sep):
            return p[len(os.path.normpath(self.root)) + 1:]
        return None  # outside the project

    def protected(self, rel):
        """True if rel is a protected file, or a directory that contains protected files."""
        if rel is None or any(a.match(rel) for a in self.allow):
            return False
        if rel == ".":
            return True
        probe = rel.rstrip("/")
        return any(r.match(probe) or r.match(probe + "/x") or r.match(probe + "/x.py") for r in self.protect)

    def new_file_ok(self, rel, abs_path):
        return not os.path.exists(abs_path) and any(r.match(rel) for r in self.new_ok)


# ---------------------------------------------------------------------------- file tools

def check_file_tool(g, tool, inp, cwd):
    path = inp.get("file_path") or inp.get("notebook_path") or ""
    if not path:
        return None
    abs_path = path if os.path.isabs(path) else os.path.join(cwd, path)
    rel = g.rel(abs_path, cwd)
    new_text = inp.get("new_string") or inp.get("content") or inp.get("new_source") or ""
    old_text = inp.get("old_string") or ""
    for e in inp.get("edits") or []:
        new_text += "\n" + (e.get("new_string") or "")
        old_text += "\n" + (e.get("old_string") or "")
    if rel is not None and MIXED_CONFIG.search(rel):
        if TEST_KEYS.search(old_text) or TEST_KEYS.search(new_text):
            return "deny", f"{rel}: edit touches test/coverage settings"
        return None
    if g.protected(rel):
        if tool == "Write" and g.new_file_ok(rel, abs_path):
            return None
        return "deny", f"{rel} is a verification asset (tests, goldens, verifier scripts, CI, hooks)"
    if REVIEWER_TALK.search(new_text):
        return "ask", "new text addresses the reviewer (\"intentional\", \"do not flag\" ...): a human should read it"
    return None


# ---------------------------------------------------------------------------- shell

def tokenize(cmd):
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError:
        return cmd.split()


def segments(tokens):
    seg, segs, ops = [], [], []
    for tok in tokens:
        if tok in SEP or (set(tok) <= set(";&|") and tok):
            segs.append(seg)
            ops.append(tok)
            seg = []
        else:
            seg.append(tok)
    segs.append(seg)
    return segs, ops


def strip_env(seg):
    env = {}
    i = 0
    while i < len(seg) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[i]):
        k, _, v = seg[i].partition("=")
        env[k] = v
        i += 1
    while i < len(seg) and seg[i] in ("env", "command", "exec", "nohup", "time", "sudo", "xargs"):
        i += 1
        while i < len(seg) and (seg[i].startswith("-") or re.match(r"^[A-Za-z_]\w*=", seg[i])):
            if "=" in seg[i]:
                k, _, v = seg[i].partition("=")
                env[k] = v
            i += 1
    return env, seg[i:]


def redirect_targets(seg):
    out, args = [], []
    i = 0
    while i < len(seg):
        t = seg[i]
        if t in (">", ">>", "&>", ">|", "&>>", ">>&") and i + 1 < len(seg):
            out.append(seg[i + 1])
            i += 2
            continue
        if t in (">&",) and i + 1 < len(seg):
            if not seg[i + 1].isdigit():
                out.append(seg[i + 1])
            i += 2
            continue
        if t == "<" and i + 1 < len(seg):
            i += 2
            continue
        args.append(t)
        i += 1
    # "2" before a redirect operator is a file descriptor, not an argument
    if args and args[-1].isdigit() and out:
        args = args[:-1]
    return out, args


def path_args(args):
    return [a for a in args[1:] if a and not a.startswith("-")]


def inplace_files(args):
    """File operands of sed/perl/ruby: drop options, -e/-f scripts and the bare script."""
    files, scripted, skip = [], False, False
    for a in args[1:]:
        if skip:
            skip = False
            continue
        if a in ("-e", "-f", "--expression", "--file") or re.match(r"^-[a-zA-Z]*e$", a):
            scripted, skip = True, True
            continue
        if a and not a.startswith("-"):
            files.append(a)
    return files if scripted else files[1:]


def git_parts(args):
    """git [-c k=v] [-C dir] sub ... -> (configs, sub, rest)"""
    i, configs = 1, []
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-c", "-C", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(args):
            if args[i] == "-c":
                configs.append(args[i + 1])
            i += 2
        else:
            if args[i].startswith("-c") and len(args[i]) > 2:
                configs.append(args[i][2:])
            i += 1
    sub = args[i] if i < len(args) else ""
    return configs, sub, args[i + 1:]


def check_git(g, args, env, cwd):
    configs, sub, rest = git_parts(args)
    if any(c.lower().startswith("core.hookspath") for c in configs):
        return "deny", "git -c core.hooksPath bypasses the repository's git hooks"
    if sub == "config" and any(a.lower() == "core.hookspath" or a.lower().startswith("core.hookspath=") for a in rest):
        if not any(a in ("--get", "--list", "-l", "--get-all") for a in rest):
            return "deny", "changing core.hooksPath disables the repository's git hooks"
    if sub in ("commit", "push", "merge", "am", "rebase", "cherry-pick", "revert", "pull"):
        if "--no-verify" in rest:
            return "deny", f"git {sub} --no-verify skips the hooks that check this change"
        if sub == "commit":
            skip_next = False
            for a in rest:
                if skip_next:
                    skip_next = False
                    continue
                if a in ("-m", "-F", "-C", "-c", "--author", "--date", "--message", "--file", "--fixup", "--squash"):
                    skip_next = True
                    continue
                if re.match(r"^-[a-zA-Z]+$", a):
                    # bundled short flags: -n is --no-verify; m/F/C/c take a value, which ends the bundle
                    for ch in a[1:]:
                        if ch == "n":
                            return "deny", "git commit -n is --no-verify: it skips the hooks"
                        if ch in "mFCc":
                            skip_next = a.endswith(ch)
                            break
        for k in ("HUSKY", "HUSKY_SKIP_HOOKS", "SKIP", "PRE_COMMIT_ALLOW_NO_CONFIG", "LEFTHOOK", "LEFTHOOK_EXCLUDE"):
            if k in env and (k != "HUSKY" or env[k] == "0") and (k != "LEFTHOOK" or env[k] == "0"):
                return "deny", f"{k}= turns off commit hooks"
    if sub == "update-index" and any(a in ("--assume-unchanged", "--skip-worktree") for a in rest):
        return "deny", "assume-unchanged / skip-worktree hides changes from git status and review"
    if sub in ("checkout", "restore", "rm", "mv", "apply") or (sub == "reset" and "--" in rest):
        paths = rest[rest.index("--") + 1:] if "--" in rest else [a for a in rest if not a.startswith("-")]
        if sub == "checkout" and "--" not in rest:
            paths = paths[1:] if len(paths) > 1 else []  # first is a branch / rev
        for p in paths:
            if g.protected(g.rel(p, cwd)):
                return "deny", f"git {sub} would change {p}, a verification asset"
    return None


def check_segment(g, seg, cwd, depth):
    env, seg = strip_env(seg)
    if not seg:
        return None, cwd
    redirs, args = redirect_targets(seg)
    for t in redirs:
        if t != "/dev/null" and g.protected(g.rel(t, cwd)):
            return ("deny", f"redirect writes to {t}, a verification asset"), cwd
    if not args:
        return None, cwd
    prog = os.path.basename(args[0])
    if prog == "cd":
        target = args[1] if len(args) > 1 else os.path.expanduser("~")
        return None, os.path.normpath(os.path.join(cwd, os.path.expanduser(target)))
    if prog in SHELLS and "-c" in args:
        i = args.index("-c")
        if i + 1 < len(args):
            return check_command(g, args[i + 1], cwd, depth + 1), cwd
    if prog == "eval" and len(args) > 1:
        return check_command(g, " ".join(args[1:]), cwd, depth + 1), cwd
    if prog == "git":
        return check_git(g, args, env, cwd), cwd
    if prog == "tee":
        for p in path_args(args):
            if g.protected(g.rel(p, cwd)):
                return ("deny", f"tee writes to {p}, a verification asset"), cwd
    if prog in INPLACE and any(a.startswith("-i") or a == "--in-place" or re.match(r"^-[a-z]*i", a) for a in args[1:]):
        for p in inplace_files(args):
            if g.protected(g.rel(p, cwd)):
                return ("deny", f"{prog} -i edits {p} in place, a verification asset"), cwd
    if prog in WRITERS:
        ps = path_args(args)
        if ps and g.protected(g.rel(ps[-1], cwd)):
            return ("deny", f"{prog} overwrites {ps[-1]}, a verification asset"), cwd
    if prog in MOVERS:
        for p in path_args(args):
            if re.match(r"^[ugoa]*[+-=][rwxXst]+$", p) or re.match(r"^\d{3,4}$", p):
                continue  # chmod mode argument
            if g.protected(g.rel(p, cwd)):
                return ("deny", f"{prog} {p} changes a verification asset"), cwd
    if prog == "find" and ("-delete" in args or "-exec" in args or "-execdir" in args):
        tail = args[args.index("-exec") + 1:] if "-exec" in args else args
        writes = "-delete" in args or any(os.path.basename(a) in MOVERS | INPLACE | WRITERS for a in tail)
        starts = [a for a in args[1:] if not a.startswith("-")][:1]
        if writes and starts and g.protected(g.rel(starts[0], cwd)):
            return ("deny", f"find over {starts[0]} rewrites or deletes verification assets"), cwd
    if prog in INTERP or re.match(r"^python3?(\.\d+)?$", prog):
        code = ""
        for flag in ("-c", "-e", "--eval", "-pe", "-pi", "-ne"):
            if flag in args and args.index(flag) + 1 < len(args):
                code = args[args.index(flag) + 1]
        if code and WRITE_CODE.search(code):
            for lit in re.findall(r"['\"]([^'\"]+)['\"]", code):
                if "/" in lit or "." in lit:
                    if g.protected(g.rel(lit, cwd)):
                        return ("deny", f"inline {prog} code writes {lit}, a verification asset"), cwd
    # blanket snapshot acceptance
    joined = " ".join(args)
    if re.search(r"\b(jest|vitest)\b", joined) and re.search(r"(^|\s)(-u|--updateSnapshot|--update)(\s|$)", joined):
        return ("ask", "accepting every snapshot rewrites expected outputs: a human should confirm"), cwd
    if re.search(r"--snapshot-update|--update-snapshots|--force-regen|--regen-all|\binsta\s+accept\b", joined):
        return ("ask", "regenerating expected outputs: a human should confirm"), cwd
    return None, cwd


def check_command(g, cmd, cwd, depth=0):
    if depth > 3:
        return "ask", "deeply nested shell command"
    tokens = tokenize(cmd)
    segs, ops = segments(tokens)
    for i, seg in enumerate(segs):
        if seg and os.path.basename(seg[0]) in ("base64", "xxd", "openssl") and \
                any(a in ("-d", "-D", "--decode", "-r") for a in seg) and i < len(ops) and ops[i] == "|":
            nxt = segs[i + 1] if i + 1 < len(segs) else []
            _, nxt = strip_env(nxt)
            if nxt and os.path.basename(nxt[0]) in SHELLS | INTERP:
                return "deny", "decoded payload piped into a shell: the command cannot be checked"
    for seg in segs:
        hit, cwd = check_segment(g, seg, cwd, depth)
        if hit:
            return hit
    return None


# ---------------------------------------------------------------------------- main

def decide(event, environ=None):
    environ = environ if environ is not None else os.environ
    tool = event.get("tool_name") or ""
    inp = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()
    root = environ.get("CLAUDE_PROJECT_DIR") or cwd
    cfg = {}
    try:
        with open(os.path.join(root, ".claude", "verifier-guard.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        pass
    g = Guard(root, cfg)
    if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        hit = check_file_tool(g, tool, inp, cwd)
    elif tool == "Bash":
        hit = check_command(g, inp.get("command") or "", cwd)
    else:
        hit = None
    if not hit:
        return None
    mode = (environ.get("VERIFIER_GUARD_MODE") or cfg.get("mode") or "deny").lower()
    decision, reason = hit
    if decision == "deny" and mode == "ask":
        decision = "ask"
    return decision, reason


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    out = decide(event)
    if out:
        decision, reason = out
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": decision,
            "permissionDecisionReason": "verifier_guard: " + reason +
            ". Fix the code, not the check. If the check itself is wrong, say so and let the human change it."}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
