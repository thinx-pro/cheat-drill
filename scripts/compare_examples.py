"""Run the drill against every config in examples/ and print the README comparison table.

usage: python3 scripts/compare_examples.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from cheat_drill.drill import drill  # noqa: E402
from cheat_drill.scenarios import CATEGORY_LABEL  # noqa: E402

CONFIGS = [("no-guard", "No hooks, no rules"),
           ("deny-rules", "Permission deny rules on tests/CI paths + `--no-verify`"),
           ("path-hook", "Typical path-based \"protected files\" hook"),
           ("verifier-guard", "Reference `verifier_guard.py` (in-sample, see below)")]


def main():
    reps = [(name, label, drill(os.path.join(ROOT, "examples", name), include_user=False)) for name, label in CONFIGS]
    cats = list(reps[0][2]["summary"]["categories"])
    print("| Config | Cheats blocked | Ask | Got through | Legit actions blocked |")
    print("|---|---|---|---|---|")
    for name, label, r in reps:
        s = r["summary"]
        print(f"| {label} (`examples/{name}`) | **{s['blocked']}/{s['cheats']}** | {s['ask']} | {s['allowed']} | "
              f"{s['false_block']}/{s['controls']} |")
    print()
    print("| Category | " + " | ".join(n for n, _, _ in reps) + " |")
    print("|---|" + "---|" * len(reps))
    for c in cats:
        row = []
        for _, _, r in reps:
            x = r["summary"]["categories"][c]
            row.append(f"{x['blocked']}/{x['total']}")
        print(f"| {CATEGORY_LABEL[c]} | " + " | ".join(row) + " |")


if __name__ == "__main__":
    main()
