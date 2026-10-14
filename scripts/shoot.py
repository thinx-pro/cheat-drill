"""Regenerate the README screenshot from real cheat-drill output (macOS, Google Chrome).

usage: python3 scripts/shoot.py
"""
import html
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
COLORS = {"31": "#ff6b6b", "32": "#7bd88f", "33": "#fcd566", "35": "#d49cff", "36": "#5ad4e6"}
SHOTS = [("drill-path-hook.png", "examples/path-hook", 1040, 1490),
         ("drill-verifier-guard.png", "examples/verifier-guard", 1040, 1490)]


def ansi_to_html(text):
    out, depth, pos = [], 0, 0
    for m in re.finditer(r"\033\[([0-9;]*)m", text):
        out.append(html.escape(text[pos:m.start()]))
        pos = m.end()
        codes = m.group(1).split(";")
        if codes in (["0"], [""]):
            out.append("</span>" * depth)
            depth = 0
            continue
        style = ["font-weight:700" if c == "1" else "color:#8a8f98" if c == "2" else f"color:{COLORS[c]}"
                 for c in codes if c in COLORS or c in ("1", "2")]
        out.append(f'<span style="{";".join(style)}">')
        depth += 1
    out.append(html.escape(text[pos:]) + "</span>" * depth)
    return "".join(out)


def main():
    tmp = tempfile.mkdtemp()
    home = os.path.expanduser("~")
    for png, project, w, h in SHOTS:
        r = subprocess.run([sys.executable, "-m", "cheat_drill", "--project", project, "--no-user"], cwd=ROOT,
                           env=dict(os.environ, FORCE_COLOR="1"), capture_output=True, text=True)
        text = r.stdout.replace(os.path.join(ROOT, project).replace(home, "~"), project)
        page = os.path.join(tmp, png + ".html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write("""<!doctype html><meta charset="utf-8"><style>
body{margin:0;background:#0f1115;font:13px/1.42 ui-monospace,SFMono-Regular,Menlo,monospace;color:#e6e6e6}
.win{margin:20px;border-radius:10px;background:#16181d;box-shadow:0 10px 40px #0008;overflow:hidden;width:max-content;min-width:900px}
.bar{background:#22252b;padding:10px 14px}.bar i{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:7px}
.body{padding:14px 22px 18px}pre{margin:6px 0 4px;white-space:pre}.cmd{font-weight:700}.p{color:#7bd88f}
</style><div class="win"><div class="bar"><i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i style="background:#28c840"></i></div>
<div class="body"><div class="cmd"><span class="p">$</span> cheat-drill --project """ + html.escape(project) +
                     " --no-user</div><pre>" + ansi_to_html(text.rstrip()) + "</pre></div></div>")
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
                        f"--window-size={w},{h}", f"--screenshot={os.path.join(ROOT, 'assets', png)}", "file://" + page],
                       capture_output=True, check=True)
        print("wrote assets/" + png)


if __name__ == "__main__":
    main()
