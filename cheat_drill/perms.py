"""Approximate evaluation of Claude Code permission rules (permissions.deny / ask / allow).

This mirrors the documented rule shapes, not Claude Code's exact implementation:
  Tool                      every use of the tool
  Bash(npm run test:*)      command prefix
  Bash(git commit *)        wildcard
  Bash(exact command)       exact match
  Edit(path-glob)           Edit rules cover Edit / MultiEdit / Write / NotebookEdit
  Write(path-glob)          Write only
Path globs: //abs, ~/home, /relative-to-project, ./relative or bare relative.
Compound shell commands are checked segment by segment.
"""
import fnmatch
import os
import re

FILE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
RULE_RX = re.compile(r"^([A-Za-z_][\w-]*)(?:\((.*)\))?$", re.S)


def glob_to_regex(pat):
    """gitignore-ish glob -> regex. ** spans directories, * stays inside one."""
    i, out = 0, []
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def path_matches(spec, abs_path, project, home):
    spec = spec.strip()
    if spec.startswith("//"):
        pat = spec[1:]
    elif spec.startswith("~/"):
        pat = os.path.join(home, spec[2:])
    elif spec.startswith("/"):
        pat = project.rstrip("/") + spec
    else:
        pat = os.path.join(project, spec[2:] if spec.startswith("./") else spec)
    rx = glob_to_regex(pat)
    if rx.match(abs_path):
        return True
    # a bare directory rule like Edit(tests/) or Edit(tests) covers what is inside it
    base = pat.rstrip("/")
    return "*" not in base and abs_path.startswith(base + "/")


def split_segments(cmd):
    """Split a shell command on ; && || | & and newlines, respecting quotes."""
    segs, cur, q, i = [], [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if q:
            cur.append(c)
            if c == q:
                q = None
            elif c == "\\" and q == '"' and i + 1 < len(cmd):
                cur.append(cmd[i + 1])
                i += 1
        elif c in "'\"":
            q = c
            cur.append(c)
        elif c in ";|&\n":
            segs.append("".join(cur))
            cur = []
            while i + 1 < len(cmd) and cmd[i + 1] in "|&":
                i += 1
        else:
            cur.append(c)
        i += 1
    segs.append("".join(cur))
    return [s.strip() for s in segs if s.strip()]


def bash_matches(spec, command):
    spec = spec.strip()
    cands = [command.strip()] + split_segments(command)
    for c in cands:
        if spec.endswith(":*"):
            p = spec[:-2]
            if c == p or c.startswith(p + " "):
                return True
        elif "*" in spec:
            if fnmatch.fnmatchcase(c, spec):
                return True
        elif c == spec:
            return True
    return False


def rule_matches(rule, tool, tool_input, project, home):
    m = RULE_RX.match(rule.strip())
    if not m:
        return False
    rtool, spec = m.group(1), m.group(2)
    if rtool == "Edit":
        applies = tool in FILE_TOOLS
    else:
        applies = rtool == tool
    if not applies:
        return False
    if spec is None or spec.strip() in ("", "*"):
        return True
    if tool == "Bash":
        return bash_matches(spec, tool_input.get("command", ""))
    if tool in FILE_TOOLS:
        fp = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        return bool(fp) and path_matches(spec, fp, project, home)
    return False


def evaluate(rules, tool, tool_input, project, home):
    """rules: {"deny": [(rule, source)], "ask": [...], "allow": [...]} -> (decision, rule, source)."""
    for decision in ("deny", "ask", "allow"):
        for rule, src in rules.get(decision, []):
            if rule_matches(rule, tool, tool_input, project, home):
                return decision, rule, src
    return None, None, None
