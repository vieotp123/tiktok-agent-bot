"""
CI guard — no public action without /confirm_action.

Greps source for direct TikTok public-action primitives outside the
single allow-listed worker file (`bot/tiktok_bot.py`). Any other module
that wants to trigger a public action MUST go through the high-risk
pending_action flow in `bot/agent/permissions.py::create_pending` and
require admin `/confirm_action` — never call the primitives directly.

Rationale:
  Customer-facing TikTok send/post is a 🔴 high-risk action per
  `docs/SELF_OPERATING_AGENT.md` §4 and `docs/OPERATING_RULES.md` §3.
  A regression that bypasses /confirm_action would silently DM
  customers from the wrong code path. This guard locks the boundary at
  static-grep time so it shows up in smoke + evals before deploy.

Sentinels (regex, all checked per line):
  1. Imports of TikTok senders:
        `from bot.tiktok_bot import send_message`
        `from bot.tiktok_bot import send_bubbles`
        `from bot.tiktok_bot import send_*`
  2. Qualified calls to those senders:
        `bot.tiktok_bot.send_message(`
        `tiktok_bot.send_bubbles(`
  3. TikTok DM input selector:
        `dm-message-input`
  4. TikTok DM/upload URL paths:
        `tiktok.com/messages`
        `tiktok.com/upload`

Allow-list (files where the sentinels are legitimate):
  - `bot/tiktok_bot.py` — defines the senders, owns the DOM selector.
  - The guard itself (this file) — references sentinels as strings.
  - `docs/` — documentation may quote them.

Escape hatch:
  Append `# ci: public-action ok` on the offending line to whitelist a
  single deliberate use (rare; prefer creating a pending_action).

CLI:
    python scripts/ci_public_action_guard.py
    python scripts/ci_public_action_guard.py bot backend scripts
    python scripts/ci_public_action_guard.py --json

Exit 0 = clean; 1 = at least one violation.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Files allowed to contain the sentinels. Stored as repo-relative POSIX paths.
ALLOWED_FILES: frozenset[str] = frozenset({
    "bot/tiktok_bot.py",
    "scripts/ci_public_action_guard.py",
})

# Directories whose contents are always exempt (docs / data / tests of guard).
ALLOWED_DIR_PREFIXES: tuple[str, ...] = (
    "docs/",
    "research/",
)

# Default paths the CLI scans.
DEFAULT_SCAN_PATHS: tuple[str, ...] = (
    "bot",
    "backend",
    "scripts",
)

# Per-line escape hatch.
ESCAPE_HATCH = "# ci: public-action ok"


@dataclass(frozen=True)
class Sentinel:
    name: str
    pattern: re.Pattern[str]
    message: str


SENTINELS: tuple[Sentinel, ...] = (
    Sentinel(
        "import_tiktok_sender",
        re.compile(
            r"\bfrom\s+bot\.tiktok_bot\s+import\s+[^#\n]*"
            r"\b(send_message|send_bubbles)\b"
        ),
        "imports a TikTok sender from bot.tiktok_bot — route via "
        "permissions.create_pending() + /confirm_action instead.",
    ),
    Sentinel(
        "qualified_tiktok_sender_call",
        re.compile(
            r"\b(?:bot\.)?tiktok_bot\.(send_message|send_bubbles)\s*\("
        ),
        "calls a TikTok sender directly — high-risk, must go through "
        "permissions.create_pending() + /confirm_action.",
    ),
    Sentinel(
        "dm_message_input_selector",
        re.compile(r"dm-message-input"),
        "uses the TikTok DM-input DOM selector — only the worker in "
        "bot/tiktok_bot.py may drive that input.",
    ),
    Sentinel(
        "tiktok_dm_url",
        re.compile(r"tiktok\.com/(?:messages|upload)\b"),
        "references the TikTok DM/upload URL — public-action surface; "
        "must go through /confirm_action.",
    ),
)


@dataclass(frozen=True)
class Violation:
    path: str          # repo-relative POSIX path
    line: int
    sentinel: str
    snippet: str
    message: str

    def render(self) -> str:
        return (f"{self.path}:{self.line}: [{self.sentinel}] "
                f"{self.message}\n    {self.snippet.strip()[:140]}")


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _is_allowed(rel_path: str) -> bool:
    if rel_path in ALLOWED_FILES:
        return True
    return any(rel_path.startswith(p) for p in ALLOWED_DIR_PREFIXES)


def _iter_python_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            if root.suffix == ".py":
                files.append(root)
            continue
        if not root.is_dir():
            continue
        for p in root.rglob("*.py"):
            # Skip __pycache__ and any venv/site-packages inside a
            # subdirectory the user happened to point at.
            parts = p.parts
            if "__pycache__" in parts or "venv" in parts \
                    or "site-packages" in parts:
                continue
            files.append(p)
    return sorted(files)


def scan_text(rel_path: str, text: str) -> list[Violation]:
    """Scan a single file's text. Returns violations (allow-list aware)."""
    if _is_allowed(rel_path):
        return []
    out: list[Violation] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ESCAPE_HATCH in line:
            continue
        for s in SENTINELS:
            if s.pattern.search(line):
                out.append(Violation(
                    path=rel_path, line=lineno,
                    sentinel=s.name, snippet=line, message=s.message,
                ))
    return out


def scan_paths(paths: list[str] | None = None,
                root: Path | None = None) -> list[Violation]:
    """Scan one or more paths (files or directories). Returns violations."""
    base = (root or REPO_ROOT).resolve()
    targets = [base / p for p in (paths or DEFAULT_SCAN_PATHS)]
    out: list[Violation] = []
    for f in _iter_python_files(targets):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = f.resolve().relative_to(base).as_posix() \
            if f.is_absolute() else f.as_posix()
        out.extend(scan_text(rel, text))
    return out


def _cli() -> int:
    ap = argparse.ArgumentParser(
        prog="ci_public_action_guard",
        description="Reject direct TikTok public-action calls outside "
                    "bot/tiktok_bot.py.",
    )
    ap.add_argument("paths", nargs="*", default=list(DEFAULT_SCAN_PATHS),
                    help="paths to scan (default: bot backend scripts)")
    ap.add_argument("--json", action="store_true",
                    help="emit JSON instead of human-readable lines")
    args = ap.parse_args()

    violations = scan_paths(args.paths)

    if args.json:
        print(json.dumps([v.__dict__ for v in violations],
                          ensure_ascii=False, indent=2))
    else:
        if not violations:
            print("✅ public-action guard: no direct send/post calls "
                  "outside the allow-list.")
        else:
            print(f"❌ public-action guard: {len(violations)} "
                  f"violation(s) — must route via /confirm_action:\n")
            for v in violations:
                print(v.render())
            print("\nFix: route the call through "
                  "`bot.agent.permissions.create_pending(...)` and wait "
                  "for `/confirm_action`. If the line is genuinely safe "
                  f"(rare), append `{ESCAPE_HATCH}` to it.")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(_cli())
