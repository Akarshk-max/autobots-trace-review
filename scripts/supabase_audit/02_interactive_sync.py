#!/usr/bin/env python3
"""
Interactive wrapper for 01_sync_auth_users.py
============================================

Run:
  python scripts/supabase_audit/02_interactive_sync.py

What it does
------------
- First, it analyzes Autobots (source) and Bolt/Scout (destination) and shows a plan:
  - which users are missing in destination (can be copied)
  - which users already exist in destination (overlap/conflicts)
- Then it gives you a menu to either:
  - Copy missing users (safe: adds users, does NOT modify existing destination users)
  - Update passwords for existing users (dangerous: modifies destination users)

All writes still require explicit confirmation (typing APPLY).

Questions you will be asked
---------------------------
1) "Show detailed technical output? [y/N]"
   - type: y if you want extra details (useful for troubleshooting)
   - press Enter for normal output

2) "How many users do you want to work with?"
   - 1) Test users only (SAFE: only a small pre-approved list)
   - 2) ALL users (DANGEROUS: entire user database)
   If you choose option 2, you must type APPLY to continue.

Menu usage
----------
- You can view the list of users missing in destination and the list of users that already exist.
- When selecting users from a list, use formats like:
    1,2,5-9   (users 1, 2, and 5 through 9)
    3-10      (users 3 through 10)
- Before any write, you must type APPLY to confirm.

Notes
-----
- This script never writes unless you choose an APPLY action and confirm.
- It does not print DB URLs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_01 = REPO_ROOT / "scripts" / "supabase_audit" / "01_sync_auth_users.py"


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_01(args: list[str], report_path: str) -> dict:
    """
    Runs script 01 with provided args and forces --report path. Returns parsed JSON report.
    Raises on failure and prints stdout/stderr for debugging.
    """
    cmd = [sys.executable, str(SCRIPT_01)] + args + ["--report", report_path]
    env = os.environ.copy()

    print("\n$ " + " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if p.stdout.strip():
        print(p.stdout)
    if p.returncode != 0:
        if p.stderr.strip():
            print(p.stderr, file=sys.stderr)
        raise SystemExit(f"01_sync_auth_users.py failed with exit code {p.returncode}")
    return json.loads(Path(report_path).read_text(encoding="utf-8"))


def paginate(items: list[str], page_size: int = 50) -> Iterable[list[str]]:
    for i in range(0, len(items), page_size):
        yield items[i : i + page_size]


def parse_selection(sel: str, n: int) -> list[int]:
    """
    Parse selection like: '1,2,5-9' into 0-based indices.
    """
    sel = sel.strip()
    if not sel:
        return []
    parts = [p.strip() for p in sel.split(",") if p.strip()]
    out: list[int] = []
    for p in parts:
        m = re.match(r"^(\d+)-(\d+)$", p)
        if m:
            a = int(m.group(1))
            b = int(m.group(2))
            if a > b:
                a, b = b, a
            for k in range(a, b + 1):
                out.append(k - 1)
        else:
            out.append(int(p) - 1)
    seen = set()
    final: list[int] = []
    for i in out:
        if 0 <= i < n and i not in seen:
            seen.add(i)
            final.append(i)
    return final


@dataclass
class Scope:
    mode: str
    no_guard: bool


def plan_all_idempotent(scope: Scope, *, debug: bool) -> dict:
    args = ["--mode", "all-idempotent"]
    if scope.no_guard:
        args += ["--no-guard", "--yes-really"]
    if debug:
        args += ["--debug"]
    return run_01(args, report_path=f"tmp/interactive_plan_{now_ts()}.json")


def show_summary(plan: dict) -> None:
    counts = plan.get("counts", {})
    db = plan.get("db", {})
    src_db = db.get("src", {})
    dst_db = db.get("dst", {})

    src_total = int(src_db.get("users_count", counts.get("source_candidates", 0)) or 0)
    dst_total = int(dst_db.get("users_count", 0) or 0)

    overlap = int(counts.get("dest_matched", 0) or 0)
    only_src = int(counts.get("insert", 0) or 0)
    only_dst = max(dst_total - overlap, 0)

    print("\n=== SUMMARY (from plan) ===")
    scope_label = (
        "ALL users (guardrails off)"
        if not plan.get("guardrail_enabled", True)
        else "Test users only (allowlist)"
    )
    print(f"scope: {scope_label}")
    print(f"src_total_users: {src_total}")
    print(f"dst_total_users: {dst_total}")
    print(f"overlap_by_email: {overlap}")
    print(f"only_src_by_email: {only_src}")
    print(f"only_dst_by_email: {only_dst}")
    print(f"plan.insert: {counts.get('insert')}")
    print(f"plan.update: {counts.get('update')}")
    print(f"plan.skip: {counts.get('skip')}")
    print("\nNote: plan.* numbers above are based on the selected scope.")
    print("\nDestination connection user:", dst_db.get("current_user"))
    if dst_db.get("current_user") == "postgres":
        print(
            "WARNING: destination user is postgres (high privilege). Use only during migration window."
        )


def choose_list(plan: dict, which: str) -> list[str]:
    if which == "insert":
        return list(plan.get("insert_emails") or [])
    if which == "skip":
        return list(plan.get("skip_emails") or [])
    if which == "update":
        return list(plan.get("update_emails") or [])
    raise ValueError(which)


def show_list(items: list[str], title: str) -> None:
    print(f"\n=== {title} (count={len(items)}) ===")
    if not items:
        return
    page_size = 50
    for start in range(0, len(items), page_size):
        page = items[start : start + page_size]
        for idx, email in enumerate(page, start=start + 1):
            print(f"{idx:4d}. {email}")
        if page[-1] != items[-1]:
            x = input("\nPress Enter for next page, or type 'q' to stop listing: ").strip().lower()
            if x == "q":
                break


def apply_selected(emails: list[str], *, override: bool, no_guard: bool, debug: bool) -> dict:
    tmp_file = Path(f"tmp/interactive_selected_{now_ts()}.txt")
    tmp_file.write_text("\n".join(emails) + "\n", encoding="utf-8")

    mode = "selected-override" if override else "selected-idempotent"
    args = [
        "--mode",
        mode,
        "--emails-file",
        str(tmp_file),
        "--apply",
        "--yes-apply",
        "--copy-identities",
    ]
    if override:
        args += ["--add-email-provider", "--promote-confirmation"]
    if no_guard:
        args += ["--no-guard", "--yes-really"]
    if debug:
        args += ["--debug"]

    return run_01(args, report_path=f"tmp/interactive_apply_{mode}_{now_ts()}.json")


def apply_all(*, override: bool, no_guard: bool, debug: bool) -> dict:
    mode = "all-override" if override else "all-idempotent"
    args = ["--mode", mode, "--apply", "--yes-apply", "--copy-identities"]
    if override:
        args += ["--add-email-provider", "--promote-confirmation"]
    if no_guard:
        args += ["--no-guard", "--yes-really"]
    if debug:
        args += ["--debug"]

    return run_01(args, report_path=f"tmp/interactive_apply_{mode}_{now_ts()}.json")


def confirm_danger(prompt: str) -> bool:
    print("\n" + prompt)
    x = input("Type APPLY to continue (anything else cancels): ").strip()
    return x == "APPLY"


def main() -> None:
    Path("tmp").mkdir(exist_ok=True)

    print("Interactive Supabase Auth Sync")
    print("=============================")
    print("This tool runs 01_sync_auth_users.py in dry-run first, then lets you choose actions.\n")

    debug = input("Show detailed technical output? [y/N]: ").strip().lower() == "y"

    print("\nHow many users do you want to work with?")
    print("  1) Test users only (SAFE: only a small pre-approved list)")
    print("  2) ALL users (DANGEROUS: entire user database)")
    print("\nRecommended: start with option 1. Use option 2 only during the real migration.")
    scope_choice = input("Select [1/2] (default 1): ").strip() or "1"
    if scope_choice == "2":
        if not confirm_danger("You chose ALL users. This can affect your entire user database."):
            print("Cancelled.")
            return
        scope = Scope(mode="all", no_guard=True)
    else:
        scope = Scope(mode="safe", no_guard=False)

    plan = plan_all_idempotent(scope, debug=debug)
    show_summary(plan)

    while True:
        print("\n=== MENU ===")
        print("  1) Refresh the plan (re-check current status)")
        print("  2) Show users missing in destination (can be copied)")
        print("  3) Show users already in destination (overlap/conflicts)")
        print("  4) Copy selected missing users to destination")
        print("  5) Update passwords for selected existing users        [DANGEROUS]")
        print("  6) Copy ALL missing users to destination")
        print("  7) Update passwords for ALL existing users             [VERY DANGEROUS]")
        print("  0) Exit")

        print("\nLegend:")
        print("  Copy   = add new users (does not modify existing destination users)")
        print("  Update = modify existing destination users (password hash/metadata)")

        choice = input("Select: ").strip()

        if choice == "0":
            return

        if choice == "1":
            plan = plan_all_idempotent(scope, debug=debug)
            show_summary(plan)
            continue

        if choice == "2":
            show_list(choose_list(plan, "insert"), "Users missing in destination (can be copied)")
            continue

        if choice == "3":
            show_list(choose_list(plan, "skip"), "Users already in destination (overlap/conflicts)")
            continue

        if choice == "4":
            inserts = choose_list(plan, "insert")
            show_list(inserts, "Users missing in destination (can be copied)")
            sel = input("\nPick user numbers (e.g. 1,2,5-9) or blank to cancel: ")
            idxs = parse_selection(sel, len(inserts))
            if not idxs:
                print("Cancelled.")
                continue
            emails = [inserts[i] for i in idxs]
            print("\nSelected emails:", emails)
            if not confirm_danger(
                "About to COPY the selected missing users into destination (adds users)."
            ):
                print("Cancelled.")
                continue
            rep = apply_selected(emails, override=False, no_guard=scope.no_guard, debug=debug)
            print("\nAPPLIED:", rep.get("applied"))
            continue

        if choice == "5":
            overlaps = choose_list(plan, "skip")
            show_list(overlaps, "Users already in destination (overlap/conflicts)")
            sel = input("\nPick user numbers to OVERRIDE (e.g. 1,2,5-9) or blank to cancel: ")
            idxs = parse_selection(sel, len(overlaps))
            if not idxs:
                print("Cancelled.")
                continue
            emails = [overlaps[i] for i in idxs]
            print("\nSelected emails:", emails)
            if not confirm_danger(
                "About to UPDATE PASSWORDS for the selected existing users (modifies destination)."
            ):
                print("Cancelled.")
                continue
            rep = apply_selected(emails, override=True, no_guard=scope.no_guard, debug=debug)
            print("\nAPPLIED:", rep.get("applied"))
            continue

        if choice == "6":
            if not confirm_danger("About to COPY ALL missing users into destination (adds users)."):
                print("Cancelled.")
                continue
            rep = apply_all(override=False, no_guard=scope.no_guard, debug=debug)
            print("\nAPPLIED:", rep.get("applied"))
            continue

        if choice == "7":
            if not confirm_danger(
                "About to UPDATE PASSWORDS for ALL existing users (modifies destination users)."
            ):
                print("Cancelled.")
                continue
            rep = apply_all(override=True, no_guard=scope.no_guard, debug=debug)
            print("\nAPPLIED:", rep.get("applied"))
            continue

        print("Unknown choice.")


if __name__ == "__main__":
    main()
