"""cheat-drill: how many known agent cheat moves do your Claude Code hooks actually stop?"""
import argparse
import json
import os
import shutil
import sys

from . import __version__
from .drill import drill
from .scenarios import CATEGORY_LABEL, all_scenarios

GUARD_SNIPPET = """{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|MultiEdit|Write|NotebookEdit|Bash",
        "hooks": [
          { "type": "command", "command": "python3 \\"$CLAUDE_PROJECT_DIR/.claude/hooks/verifier_guard.py\\"" }
        ]
      }
    ]
  }
}"""


class C:
    on = False

    @classmethod
    def w(cls, code, s):
        return f"\033[{code}m{s}\033[0m" if cls.on else s


def home_short(p):
    h = os.path.expanduser("~")
    return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


def verdict_cell(v):
    return {"BLOCK": C.w("32;1", "BLOCK"), "ASK": C.w("33;1", "ASK  "), "ALLOW": C.w("31;1", "ALLOW")}[v]


def control_cell(v):
    return {"BLOCK": C.w("31;1", "BLOCK"), "ASK": C.w("33;1", "ASK  "), "ALLOW": C.w("32", "ok   ")}[v]


def by_text(r):
    if not r["by"]:
        return ""
    stop = [b for b in r["by"] if b["decision"] in ("deny", "ask")] or r["by"]
    b = stop[0]
    if b["kind"] == "rule":
        return f"rule {b['what']}"
    name = os.path.basename(b["what"].split()[-1].strip("\"'")) if b["what"].split() else b["what"]
    if b["decision"] == "error":
        return f"hook error ({name}): {b['reason']}"
    return f"hook {name}"


def bar(n, total, width=20):
    if total == 0:
        return ""
    k = round(width * n / total)
    return C.w("32", "█" * k) + C.w("2", "░" * (width - k))


def render(rep, verbose=False):
    s = rep["summary"]
    lines = []
    nrules = sum(rep["rules"].values())
    lines.append(C.w("1", f"cheat-drill {__version__}") + C.w("2", f"  ·  {s['cheats']} cheat moves, {s['controls']} legit controls"))
    lines.append(C.w("2", f"project  {home_short(rep['project'])}   (test dir: {rep['test_dir']}/)"))
    lines.append(C.w("2", f"config   {len(rep['settings'])} settings file(s), {len(rep['hooks'])} PreToolUse command hook(s), "
                          f"{rep['rules']['deny']} deny / {rep['rules']['ask']} ask rules"))
    if not rep["settings"] or (not rep["hooks"] and not nrules):
        lines.append(C.w("33", "         no hooks or permission rules found: every move below goes straight through"))
    for sk in rep["skipped"]:
        lines.append(C.w("33", "         skipped: " + home_short(sk)))
    lines.append("")
    cur = None
    for r in rep["results"]:
        if r["category"] != cur:
            cur = r["category"]
            lines.append(C.w("1;36", CATEGORY_LABEL.get(cur, cur)))
        cell = control_cell(r["verdict"]) if r["control"] else verdict_cell(r["verdict"])
        title = r["title"][:50].ljust(50)
        lines.append(f"  {C.w('2', r['id'])}  {title}  {cell}  {C.w('2', by_text(r))}")
        if verbose:
            shown = r["input"].get("command") or r["input"].get("file_path")
            lines.append(C.w("2", f"        {r['tool']}: {shown}"))
    lines.append("")
    pct = round(100 * s["blocked"] / s["cheats"]) if s["cheats"] else 0
    lines.append(C.w("1", "Score    ") + bar(s["blocked"], s["cheats"]) +
                 C.w("1", f"  {s['blocked']}/{s['cheats']} blocked ({pct}%)") +
                 f"  ·  {s['ask']} ask  ·  " + C.w("31" if s["allowed"] else "32", f"{s['allowed']} got through"))
    fp = s["false_block"]
    lines.append(C.w("1", "Controls ") + C.w("31" if fp else "32", f"{fp}/{s['controls']} legit actions blocked") +
                 (f"  ·  {s['false_ask']} asked" if s["false_ask"] else ""))
    weak = sorted(((c["blocked"] / c["total"], k, c) for k, c in s["categories"].items()), key=lambda x: x[0])
    if weak and weak[0][0] < 1:
        _, k, c = weak[0]
        lines.append(C.w("1", "Weakest  ") + f"{CATEGORY_LABEL.get(k, k)} ({c['blocked']}/{c['total']})")
    if s["hook_errors"]:
        lines.append(C.w("33", f"Note     {s['hook_errors']} hook run(s) errored or timed out (not counted as blocks); see --verbose"))
    if rep["unattended"]:
        lines.append(C.w("2", "         --unattended: 'ask' counted as got-through (nobody is there to answer)"))
    if s["allowed"] and not any("verifier_guard" in h["command"] for h in rep["hooks"]):
        lines.append(C.w("2", "Fix      cheat-drill guard --install   (reference hook; re-run the drill afterwards)"))
    return "\n".join(lines)


def cmd_guard(args):
    from . import guard as gmod
    src = gmod.__file__
    if args.print:
        sys.stdout.write(open(src, encoding="utf-8").read())
        return 0
    if not args.install:
        print(f"Reference hook: {src}\n\nInstall into this project:\n  cheat-drill guard --install\n\nThen add to .claude/settings.json:\n{GUARD_SNIPPET}")
        return 0
    dest = os.path.join(os.path.abspath(args.project), ".claude", "hooks", "verifier_guard.py")
    if os.path.exists(dest) and not args.force:
        print(f"{dest} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(src, dest)
    os.chmod(dest, 0o755)
    print(f"wrote {home_short(dest)}\n\nNow add this to {home_short(os.path.join(os.path.abspath(args.project), '.claude', 'settings.json'))} "
          f"(merge with any existing \"hooks\"):\n{GUARD_SNIPPET}\n\nThen re-run: cheat-drill")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["guard"]:
        p = argparse.ArgumentParser(prog="cheat-drill guard", description="Print or install the reference verifier_guard hook.")
        p.add_argument("--install", action="store_true", help="copy it to <project>/.claude/hooks/verifier_guard.py")
        p.add_argument("--print", action="store_true", help="print the hook source")
        p.add_argument("--force", action="store_true", help="overwrite an existing copy")
        p.add_argument("--project", default=".", help="project root (default: .)")
        return cmd_guard(p.parse_args(argv[1:]))

    p = argparse.ArgumentParser(
        prog="cheat-drill",
        description="Feed known agent cheat moves (sed -i on tests, --no-verify, core.hooksPath, golden-file "
                    "overwrites, ...) to your Claude Code PreToolUse hooks and permission rules, and score what they stop. "
                    "Hooks run for real against a throwaway fixture repo; no scenario is ever executed.",
        epilog="subcommand: cheat-drill guard [--install|--print]   reference hook that blocks these moves")
    p.add_argument("--project", default=".", help="project whose .claude/ settings and hooks are drilled (default: .)")
    p.add_argument("--settings", action="append", default=[], metavar="FILE", help="extra settings.json to include (repeatable)")
    p.add_argument("--no-user", action="store_true", help="ignore ~/.claude/settings.json")
    p.add_argument("--unattended", action="store_true", help="count 'ask' as got-through (no human to answer)")
    p.add_argument("--in-place", action="store_true", help="run hooks against the real project dir instead of a fixture copy")
    p.add_argument("--test-dir", help="name of the test directory (default: auto-detect, else tests)")
    p.add_argument("--only", help="comma-separated categories or scenario ids")
    p.add_argument("--timeout", type=float, default=10, help="seconds per hook run (default 10)")
    p.add_argument("--min-score", type=int, metavar="PCT", help="exit 1 if fewer than PCT%% of cheats are blocked or any control is blocked")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--list", action="store_true", help="list the scenarios and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="show each synthetic tool call")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--version", action="version", version=f"cheat-drill {__version__}")
    a = p.parse_args(argv)
    C.on = not a.no_color and not a.json and (sys.stdout.isatty() or os.environ.get("FORCE_COLOR") == "1") \
        and "NO_COLOR" not in os.environ

    if a.list:
        for sc in all_scenarios(a.test_dir or "tests"):
            shown = sc["input"].get("command") or sc["input"].get("file_path")
            print(f"{sc['id']}  {sc['category']:<13} {sc['tool']:<9} {sc['title']:<48} {shown}")
        return 0

    only = {x.strip() for x in a.only.split(",")} if a.only else None
    rep = drill(a.project, include_user=not a.no_user, extra_settings=a.settings, unattended=a.unattended,
                in_place=a.in_place, timeout=a.timeout, test_dir=a.test_dir, only=only)
    if a.json:
        text = json.dumps(rep, indent=2)
        home = os.path.expanduser("~")
        print(text.replace(json.dumps(home)[1:-1] + "/", "~/").replace(json.dumps(home), "\"~\""))
    else:
        print(render(rep, a.verbose))
    s = rep["summary"]
    if a.min_score is not None and s["cheats"]:
        if 100 * s["blocked"] / s["cheats"] < a.min_score or s["false_block"]:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
