"""A fake build host tree: clones of the scenario's target projects plus a toy `m` and `atest`.

`m` "compiles" every Java file in the slot: a call to a method declared in the tree with an
argument count no declaration has is an error, as javac would report it. That catches the
typical migration break (an upstream signature change the OEM code still calls the old way).
It also rejects module names no Android.bp defines, and logs the modules it built to
`build/m.log`.

`atest` passes unless `build/FAIL_ALWAYS` lists the test, or a line `Test<TAB>text` in
`build/FAIL_IF` names it and `text` occurs in a Java file of the slot. It logs to
`build/atest.log`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.fixtures.scenario import PROJECTS, Scenario

ENVSETUP = """# fake envsetup for tests
_HERE="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
lunch() {{ export TARGET_PRODUCT="$1"; echo "lunch $1"; }}
m() {{ "{python}" "$_HERE/fake_m.py" "$@"; }}
atest() {{ "{python}" "$_HERE/fake_atest.py" "$@"; }}
"""

FAKE_M = r"""
import re, sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
modules = sys.argv[1:]
(root / "build" / "m.log").open("a").write(" ".join(modules) + "\n")
print("building", " ".join(modules))

known = set()
for bp in root.rglob("Android.bp"):
    if ".git" not in bp.parts:
        known |= set(re.findall(r'name:\s*"([^"]+)"', bp.read_text()))
errors = [f"error: unknown module '{m}'" for m in modules if m not in known]

decl = re.compile(r"^\s*(?:public|private|protected)[^=;(]*?\s(\w+)\s*\(([^)]*)\)\s*\{")
call = re.compile(r"(?<![\w.])(?:\w+\.)?(\w+)\s*\(([^()]*)\)")
def arity(args):
    return 0 if not args.strip() else args.count(",") + 1

files = [f for f in root.rglob("*.java") if ".git" not in f.parts]
declared = {}
for f in files:
    for line in f.read_text().splitlines():
        if m := decl.match(line):
            declared.setdefault(m.group(1), set()).add(arity(m.group(2)))
for f in files:
    for n, line in enumerate(f.read_text().splitlines(), 1):
        if decl.match(line) or line.lstrip().startswith(("//", "*")):
            continue
        for m in call.finditer(line):
            name, args = m.group(1), m.group(2)
            if name in declared and arity(args) not in declared[name]:
                rel = f.relative_to(root)
                errors.append(f"{rel}:{n}: error: method {name} cannot be applied to given types")
for e in errors:
    print(e)
print("FAILED: build" if errors else "build completed successfully")
sys.exit(1 if errors else 0)
"""

FAKE_ATEST = r"""
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
tests = sys.argv[1:]
(root / "build" / "atest.log").open("a").write(" ".join(tests) + "\n")
always = root / "build" / "FAIL_ALWAYS"
fail_if = root / "build" / "FAIL_IF"
failing = set(always.read_text().split()) if always.exists() else set()
if fail_if.exists():
    text = "".join(f.read_text() for f in root.rglob("*.java") if ".git" not in f.parts)
    for line in fail_if.read_text().splitlines():
        test, _, needle = line.partition("\t")
        if needle and needle in text:
            failing.add(test)
for t in tests:
    print(f"{t}: {'FAILED' if t in failing else 'PASSED'}")
sys.exit(1 if failing & set(tests) else 0)
"""


def make_build_tree(scenario: Scenario, root: Path, slots: int = 1) -> list[str]:
    """Create `slots` prepared tree roots under `root`; returns their paths."""
    paths = []
    for i in range(1, slots + 1):
        slot = root / f"slot-{i}"
        for project in PROJECTS:
            (slot / project).parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "-q", str(scenario.target / project), str(slot / project)],
                check=True,
            )
        build = slot / "build"
        build.mkdir()
        (build / "envsetup.sh").write_text(ENVSETUP.format(python=sys.executable))
        (build / "fake_m.py").write_text(FAKE_M)
        (build / "fake_atest.py").write_text(FAKE_ATEST)
        paths.append(str(slot))
    return paths
