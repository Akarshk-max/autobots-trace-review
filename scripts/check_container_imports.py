"""Fail if src/autobots imports first-party code the image cannot resolve.

Run this INSIDE the built image, before pushing it.

Why this exists
---------------
Locally the project is installed editable, so a bare `autobots...` import
resolves, and the repo root is on sys.path, so `learn...` resolves too. The image
does neither - it runs `uv sync --no-install-project` and copies only
./src/autobots - so those imports raise ModuleNotFoundError at startup and the
container exits before serving anything. That took prod's deploys down for a day
and was invisible to lint, tests and review.

Why not just `python -c "import src.autobots.main"`
---------------------------------------------------
Settings validation runs during that import and fails first when no secrets are
present, so the import never reaches the broken module - the check would pass
while the image is still broken. This instead parses every file for its import
roots and asks whether each resolves, executing no module body and needing no
secrets.

Scope
-----
Only FIRST-PARTY roots are checked: the invariant is that in-repo code is
imported as `src.autobots...` and never from a directory the image does not copy.
Third-party availability is the lockfile's job, and several optional packages
(torch, spacy, ...) are deliberately absent from the image.
"""

import ast
import importlib.util
import pathlib
import sys
from collections import defaultdict

SRC = pathlib.Path("src/autobots")

# Repo directories, plus the bare form of the package. Importing any of these as
# a top-level root means the code will not resolve once it is in the image, where
# only ./src/autobots exists and the project is not installed.
FIRST_PARTY_ROOTS = {
    "autobots",  # must be src.autobots
    "build",
    "db",
    "dist",
    "docs",
    "learn",
    "scripts",
    "tests",
    "tests_e2e",
    "to_del",
}


def import_roots(path: pathlib.Path) -> set[str]:
    """Top-level package name of every absolute import in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is relative, resolved against the package itself
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def main() -> int:
    if not SRC.is_dir():
        print(f"expected to run from the image workdir; {SRC} not found", file=sys.stderr)
        return 2

    # Match how the app is launched: uvicorn imports `src.autobots.main` with the
    # working directory on sys.path.
    sys.path.insert(0, str(pathlib.Path.cwd()))

    offenders: dict[str, list[str]] = defaultdict(list)
    files = sorted(SRC.rglob("*.py"))
    for path in files:
        for root in import_roots(path):
            if root not in FIRST_PARTY_ROOTS:
                continue
            try:
                resolved = importlib.util.find_spec(root) is not None
            except (ImportError, ValueError):
                resolved = False
            if not resolved:
                offenders[root].append(str(path))

    # `src` must resolve, or every first-party import in the image is broken.
    try:
        src_ok = importlib.util.find_spec("src") is not None
    except (ImportError, ValueError):
        src_ok = False
    if not src_ok:
        print("'src' is not importable from the image workdir - first-party imports cannot work")
        return 1

    if offenders:
        print("First-party imports that cannot be resolved inside the image:\n")
        for root, paths in sorted(offenders.items()):
            print(f"  {root!r} - used by {len(paths)} file(s):")
            for p in sorted(paths):
                print(f"      {p}")
        print(
            "\nThe image installs no project package and copies only ./src/autobots.\n"
            "Import in-repo code as `src.autobots...`, and never from a directory\n"
            "that is not copied into the image (learn/, tests/, scripts/, ...)."
        )
        return 1

    print(f"OK - first-party imports in {len(files)} files under {SRC} resolve inside the image")
    return 0


if __name__ == "__main__":
    sys.exit(main())
