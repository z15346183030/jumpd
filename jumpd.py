#!/usr/bin/env python3
"""jumpd — a directory jumper with explainable scoring.

Remembers which directories you visit, then jumps to the best match
for your query. The score is frecency (frequency x recency) plus
match quality, and every point is printable with --explain.

No shell integration required. Use it like:

    eval "$(jumpd jump foo)"     # cd to the best match for "foo"
    jumpd add /path/to/dir       # record a directory
    jumpd list                    # show all entries, ranked
    jumpd jump foo --explain      # show why a match won

Database: a plain text file at ~/.local/share/jumpd/db.tsv
Format: one entry per line: score<TAB>last_seen_epoch<TAB>path
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "1.0.0"
DEFAULT_MAXAGE = 10_000
RECENCY_MULTIPLIERS = [
    # (max_age_seconds, multiplier)
    (3600, 4),        # within the last hour
    (86400, 2),       # within the last day
    (604800, 0.5),    # within the last week
    (float("inf"), 0.25),  # older
]
PRUNE_AGE_SECONDS = 90 * 86400  # 90 days


def _default_data_dir() -> str:
    """Return the default data directory."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.join(xdg, "jumpd")
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".local", "share", "jumpd")


def _data_dir() -> str:
    return os.environ.get("JUMPD_DATA_DIR") or _default_data_dir()


def _db_path() -> str:
    return os.path.join(_data_dir(), "db.tsv")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


@dataclass
class Entry:
    """A single directory record."""

    path: str
    score: float
    last_seen: float  # epoch seconds

    @property
    def frecency(self) -> float:
        """Frecency = score * recency_multiplier."""
        age = time.time() - self.last_seen
        for max_age, mult in RECENCY_MULTIPLIERS:
            if age <= max_age:
                return self.score * mult
        return self.score * 0.25  # unreachable, but safe

    def to_line(self) -> str:
        return f"{self.score}\t{self.last_seen}\t{self.path}"

    @classmethod
    def from_line(cls, line: str) -> Optional["Entry"]:
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 3:
            return None
        try:
            score = float(parts[0])
            last_seen = float(parts[1])
            path = parts[2]
        except (ValueError, IndexError):
            return None
        if not path:
            return None
        return cls(path=path, score=score, last_seen=last_seen)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


class Database:
    """In-memory database backed by a TSV file."""

    def __init__(self, entries: List[Entry]):
        self._entries: dict[str, Entry] = {}
        for e in entries:
            self._entries[e.path] = e

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Database":
        """Load the database from disk. Returns empty db if file missing."""
        path = path or _db_path()
        entries: List[Entry] = []
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    e = Entry.from_line(line)
                    if e is not None:
                        entries.append(e)
        return cls(entries)

    def save(self, path: Optional[str] = None) -> None:
        """Save the database to disk."""
        path = path or _db_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for e in self._entries.values():
                f.write(e.to_line() + "\n")
        os.replace(tmp, path)

    @property
    def entries(self) -> List[Entry]:
        return list(self._entries.values())

    def add(self, path: str, now: Optional[float] = None) -> None:
        """Add or increment a directory."""
        now = now if now is not None else time.time()
        path = os.path.abspath(path)
        existing = self._entries.get(path)
        if existing:
            existing.score += 1
            existing.last_seen = now
        else:
            self._entries[path] = Entry(path=path, score=1.0, last_seen=now)

    def remove(self, path: str) -> bool:
        """Remove a directory. Returns True if it was present."""
        path = os.path.abspath(path)
        if path in self._entries:
            del self._entries[path]
            return True
        return False

    def age(self, maxage: int = DEFAULT_MAXAGE) -> int:
        """Apply aging: scale scores so total ~= 90% of maxage.

        Returns the number of entries removed (score < 1 after scaling).
        """
        total = sum(e.score for e in self._entries.values())
        if total <= maxage:
            return 0
        k = total / (0.9 * maxage)
        removed = 0
        to_delete = []
        for path, e in self._entries.items():
            e.score = e.score / k
            if e.score < 1:
                to_delete.append(path)
        for path in to_delete:
            del self._entries[path]
            removed += 1
        return removed

    def prune_stale(self, now: Optional[float] = None) -> int:
        """Remove entries for non-existent paths older than 90 days."""
        now = now if now is not None else time.time()
        removed = 0
        to_delete = []
        for path, e in self._entries.items():
            if (now - e.last_seen) > PRUNE_AGE_SECONDS and not os.path.isdir(path):
                to_delete.append(path)
        for path in to_delete:
            del self._entries[path]
            removed += 1
        return removed


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@dataclass
class MatchResult:
    """A matched directory with its score breakdown."""

    entry: Entry
    frecency: float
    match_quality: float  # 0..1, higher = closer match
    exact_bonus: float  # bonus for exact last-component match
    total: float
    matched_components: List[str] = field(default_factory=list)

    def breakdown_lines(self) -> List[str]:
        lines = []
        lines.append(f"  path:      {self.entry.path}")
        lines.append(f"  frecency:  {self.frecency:.2f}  (score={self.entry.score}, last_seen={_time_ago(self.entry.last_seen)})")
        lines.append(f"  match_q:   {self.match_quality:.3f}")
        if self.exact_bonus > 0:
            lines.append(f"  exact:     +TIER  (exact last-component match, always ranked first)")
        lines.append(f"  total:     {self.total:.2f}")
        return lines


def _split_components(path: str) -> List[str]:
    """Split a path into its components, lowercased for matching."""
    # Normalize and split
    parts = []
    for p in path.replace("\\", "/").split("/"):
        if p:
            parts.append(p)
    return parts


def _has_uppercase(s: str) -> bool:
    return any(c.isupper() for c in s)


def _smart_case_eq(candidate: str, query: str) -> bool:
    """Case-sensitive if query has uppercase, else case-insensitive."""
    if _has_uppercase(query):
        return candidate == query
    return candidate.lower() == query.lower()


def _smart_case_contains(haystack: str, needle: str) -> bool:
    if _has_uppercase(needle):
        return needle in haystack
    return needle.lower() in haystack.lower()


def _match_query(
    path: str, keywords: List[str]
) -> Tuple[bool, float, bool, List[str]]:
    """Try to match a path against keywords.

    Returns (matched, match_quality, exact_last, matched_components).

    Matching rules:
    1. Smart case: uppercase in keyword -> case-sensitive.
    2. All keywords must appear in the path, in order.
    3. The last keyword's final component must match the last component
       of the path (unless the keyword ends with '/', which means
       "last component must end here" rather than "be exactly this").
    4. If the last keyword is exactly the last path component, it's
       an exact match.
    """
    components = _split_components(path)
    if not components:
        return False, 0.0, False, []

    path_normalized = "/".join(components)

    # Track position in the path string for ordered matching
    search_pos = 0
    matched_components: List[str] = []

    for i, kw in enumerate(keywords):
        is_last = (i == len(keywords) - 1)
        kw_clean = kw.rstrip("/") if kw.endswith("/") else kw
        kw_is_slash_terminated = kw.endswith("/")

        if not kw_clean:
            # Bare slash keyword: enforce a component boundary.
            # The next keyword must start at a new component, i.e.
            # the character at search_pos must be '/' or the end.
            if search_pos > 0 and search_pos < len(path_normalized):
                if path_normalized[search_pos] != "/":
                    # The previous match ended inside a component, not at a boundary
                    return False, 0.0, False, []
            # Move past the boundary slash if present
            if search_pos < len(path_normalized) and path_normalized[search_pos] == "/":
                search_pos += 1
            continue

        # Find the keyword in the path after the current search position
        path_remaining = path_normalized[search_pos:]
        if _has_uppercase(kw_clean):
            found_pos = path_remaining.find(kw_clean)
        else:
            found_pos = path_remaining.lower().find(kw_clean.lower())

        if found_pos < 0:
            return False, 0.0, False, []

        # If the previous keyword was a bare slash, this keyword must
        # start at the beginning of a component (found_pos must be 0,
        # or preceded by '/')
        # This is already enforced by advancing search_pos past the slash.
        abs_pos = search_pos + found_pos
        matched_components.append(kw_clean)
        search_pos = abs_pos + len(kw_clean)

        if is_last:
            # Check last-component rule
            last_comp = components[-1]

            if kw_is_slash_terminated:
                # Trailing slash: last component must BE this keyword
                # (kw_clean must match last_comp exactly)
                if not _smart_case_eq(last_comp, kw_clean):
                    return False, 0.0, False, []
                # Exact match
                return True, 1.0, True, matched_components

            # Normal last keyword: last component must contain the keyword
            if not _smart_case_contains(last_comp, kw_clean):
                return False, 0.0, False, []

            # Check if it's an exact match with the last component
            exact = _smart_case_eq(last_comp, kw_clean)

            # Match quality: how much of the last component does the
            # keyword cover? Closer to 1.0 = better match.
            if exact:
                quality = 1.0
            else:
                if _has_uppercase(kw_clean):
                    quality = len(kw_clean) / len(last_comp) if last_comp else 0.0
                else:
                    quality = len(kw_clean) / len(last_comp) if last_comp else 0.0

            return True, quality, exact, matched_components

    return True, 0.5, False, matched_components


def query(
    db: Database,
    keywords: List[str],
    cwd: Optional[str] = None,
    exclude_dirs: Optional[List[str]] = None,
    cwd_only: bool = False,
) -> List[MatchResult]:
    """Query the database for matching directories.

    Returns results sorted by total score descending.
    """
    results: List[MatchResult] = []
    now = time.time()
    cwd = cwd or os.getcwd()

    # Normalize exclude dirs — compare both the raw string and the
    # abspath, since db entries may store paths in either form.
    exclude_set = set()
    if exclude_dirs:
        for d in exclude_dirs:
            exclude_set.add(d)
            exclude_set.add(os.path.abspath(d))

    for entry in db.entries:
        path = entry.path

        # Skip excluded
        if path in exclude_set:
            continue

        # Only check existence for entries old enough to be pruned
        # (filesystem isdir() is expensive — skip it for recent entries)
        if (now - entry.last_seen) > PRUNE_AGE_SECONDS and not os.path.isdir(path):
            continue

        # cwd-only filter
        if cwd_only:
            if not path.startswith(os.path.abspath(cwd)):
                continue

        matched, quality, exact, _ = _match_query(path, keywords)
        if not matched:
            continue

        frec = entry.frecency
        # Exact match always wins over any partial match (issue #247).
        # We use a tier system: exact matches get a large flat bonus
        # that guarantees they rank above non-exact matches, regardless
        # of frecency differences. The bonus is 1e9 + frecency so that
        # among exact matches, frecency still determines order.
        if exact:
            exact_bonus = 1_000_000_000.0  # tier separator
            total = exact_bonus + frec + quality * frec * 0.3
        else:
            exact_bonus = 0.0
            total = frec + quality * frec * 0.3

        results.append(
            MatchResult(
                entry=entry,
                frecency=frec,
                match_quality=quality,
                exact_bonus=exact_bonus,
                total=total,
                matched_components=[],
            )
        )

    # Sort by total descending, then by path for stability
    results.sort(key=lambda r: (-r.total, r.entry.path))
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _time_ago(epoch: float) -> str:
    """Human-readable time-ago string."""
    delta = time.time() - epoch
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    if delta < 604800:
        return f"{int(delta / 86400)}d ago"
    return f"{int(delta / 604800)}w ago"


def _format_score(score: float) -> str:
    if score == int(score):
        return str(int(score))
    return f"{score:.2f}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_add(args: argparse.Namespace) -> int:
    """Add a directory to the database."""
    target = args.path
    if not os.path.isdir(target):
        print(f"jumpd: not a directory: {target}", file=sys.stderr)
        return 1

    db = Database.load()
    db.add(target)
    removed = db.age(args.maxage)
    db.save()
    abs_path = os.path.abspath(target)
    entry = db._entries.get(abs_path)
    if entry:
        print(f"jumpd: added {abs_path} (score={_format_score(entry.score)}, {_time_ago(entry.last_seen)})")
    if removed:
        print(f"jumpd: aged out {removed} stale entries", file=sys.stderr)
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    """Remove a directory from the database."""
    db = Database.load()
    target = os.path.abspath(args.path)
    if db.remove(target):
        db.save()
        print(f"jumpd: removed {target}")
        return 0
    else:
        print(f"jumpd: not in database: {target}", file=sys.stderr)
        return 1


def cmd_jump(args: argparse.Namespace) -> int:
    """Jump to the best matching directory."""
    keywords = args.keywords

    # Handle special cases: absolute paths, relative paths, ..
    if not keywords:
        print("jumpd: no query given. Try 'jumpd list' to see entries.", file=sys.stderr)
        return 1

    # If the first keyword is an absolute or relative path, just cd there
    first = keywords[0]
    if os.path.isabs(first) or os.path.isdir(first) or first in ("..", "-", "."):
        target = first
        if len(keywords) > 1:
            target = os.path.join(first, *keywords[1:])
        target = os.path.abspath(target)
        if os.path.isdir(target):
            # Record it
            db = Database.load()
            db.add(target)
            db.save()
            print(target)
            return 0
        else:
            print(f"jumpd: not a directory: {target}", file=sys.stderr)
            return 1

    exclude_dirs = []
    if args.exclude:
        exclude_dirs = [d.strip() for d in args.exclude.split(os.pathsep) if d.strip()]

    db = Database.load()
    results = query(
        db,
        keywords,
        exclude_dirs=exclude_dirs,
        cwd_only=args.cwd_only,
    )

    if not results:
        print("jumpd: no match found", file=sys.stderr)
        return 2

    best = results[0]

    if args.explain:
        print("jumpd: match breakdown", file=sys.stderr)
        for line in best.breakdown_lines():
            print(line, file=sys.stderr)
        if len(results) > 1:
            print(f"  (next: {results[1].entry.path} total={results[1].total:.2f})", file=sys.stderr)

    # Record the jump
    db.add(best.entry.path)
    db.save()

    print(best.entry.path)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """List all entries, ranked by frecency."""
    db = Database.load()

    if args.all:
        entries = db.entries
    else:
        # Filter to existing dirs
        entries = [e for e in db.entries if os.path.isdir(e.path)]

    # Sort by frecency descending
    entries.sort(key=lambda e: (-e.frecency, e.path))

    if not entries:
        print("jumpd: database is empty. Use 'jumpd add <path>' to add entries.", file=sys.stderr)
        return 1

    if args.limit:
        entries = entries[: args.limit]

    # Compute max width for score
    max_score_len = max(len(_format_score(e.frecency)) for e in entries)
    max_score_len = max(max_score_len, 4)

    for e in entries:
        score_str = _format_score(e.frecency)
        exists = "" if os.path.isdir(e.path) else " [missing]"
        print(f"{score_str:>{max_score_len}}  {_time_ago(e.last_seen):>8}  {e.path}{exists}")

    return 0


def cmd_query(args: argparse.Namespace) -> int:
    """Query for matching directories, list all matches."""
    keywords = args.keywords
    if not keywords:
        print("jumpd: no query given.", file=sys.stderr)
        return 1

    exclude_dirs = []
    if args.exclude:
        exclude_dirs = [d.strip() for d in args.exclude.split(os.pathsep) if d.strip()]

    db = Database.load()
    results = query(
        db,
        keywords,
        exclude_dirs=exclude_dirs,
        cwd_only=args.cwd_only,
    )

    if not results:
        print("jumpd: no match found", file=sys.stderr)
        return 2

    if args.explain:
        for r in results:
            print(f"  total={r.total:.2f}  {r.entry.path}")
            for line in r.breakdown_lines():
                print(line)
            print()
    else:
        for r in results:
            print(r.entry.path)

    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    """Edit the database: increment, decrement, or delete entries."""
    db = Database.load()
    target = os.path.abspath(args.path)

    if target not in db._entries:
        print(f"jumpd: not in database: {target}", file=sys.stderr)
        return 1

    entry = db._entries[target]
    if args.action == "increment":
        entry.score += 1
        entry.last_seen = time.time()
        print(f"jumpd: incremented {target} (score={_format_score(entry.score)})")
    elif args.action == "decrement":
        entry.score = max(0, entry.score - 1)
        entry.last_seen = time.time()
        print(f"jumpd: decremented {target} (score={_format_score(entry.score)})")
    elif args.action == "delete":
        del db._entries[target]
        print(f"jumpd: deleted {target}")

    db.save()
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """Import from another tool's database."""
    source = args.source
    db = Database.load()
    count = 0

    if source == "z":
        # z format: path|score
        z_path = args.file
        if not os.path.isfile(z_path):
            print(f"jumpd: file not found: {z_path}", file=sys.stderr)
            return 1
        with open(z_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|")
                if len(parts) >= 2:
                    path = parts[0]
                    score = float(parts[1])
                    db._entries[os.path.abspath(path)] = Entry(
                        path=os.path.abspath(path),
                        score=score,
                        last_seen=time.time(),
                    )
                    count += 1
    elif source == "autojump":
        # autojump format: score\tpath
        aj_path = args.file
        if not os.path.isfile(aj_path):
            print(f"jumpd: file not found: {aj_path}", file=sys.stderr)
            return 1
        with open(aj_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 2:
                    score = float(parts[0])
                    path = parts[-1]
                    db._entries[os.path.abspath(path)] = Entry(
                        path=os.path.abspath(path),
                        score=score,
                        last_seen=time.time(),
                    )
                    count += 1
    else:
        print(f"jumpd: unknown source '{source}'. Supported: z, autojump", file=sys.stderr)
        return 1

    db.save()
    print(f"jumpd: imported {count} entries from {source}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Print a shell function for integration."""
    shell = args.shell
    cmd_name = args.cmd or "j"

    if shell == "bash":
        print(f"""# jumpd shell integration for bash
# Add this to your ~/.bashrc:
#   eval "$(jumpd init bash)"

__jumpd_jump() {{
    local result
    result=$(jumpd jump "$@" 2>/dev/null) && cd "$result"
}}

{cmd_name}() {{
    __jumpd_jump "$@"
}}

__jumpd_precmd() {{
    jumpd add "$PWD" 2>/dev/null
}}

# Hook: record directory on each prompt
__jumpd_prev_dir="$PWD"
__jumpd_prompt_cmd() {{
    if [[ "$PWD" != "$__jumpd_prev_dir" ]]; then
        jumpd add "$PWD" 2>/dev/null
        __jumpd_prev_dir="$PWD"
    fi
}}
PROMPT_COMMAND="__jumpd_prompt_cmd;${{PROMPT_COMMAND:-:}}\"""")
    elif shell == "zsh":
        print(f"""# jumpd shell integration for zsh
# Add this to your ~/.zshrc:
#   eval "$(jumpd init zsh)"

__jumpd_jump() {{
    local result
    result=$(jumpd jump "$@" 2>/dev/null) && cd "$result"
}}

{cmd_name}() {{
    __jumpd_jump "$@"
}}

# Hook: record directory when it changes
__jumpd_prev_dir="$PWD"
__jumpd_chpwd() {{
    if [[ "$PWD" != "$__jumpd_prev_dir" ]]; then
        jumpd add "$PWD" 2>/dev/null
        __jumpd_prev_dir="$PWD"
    fi
}}
chpwd_functions+=(__jumpd_chpwd)""")
    elif shell == "fish":
        print(f"""# jumpd shell integration for fish
# Add this to your ~/.config/fish/config.fish:
#   jumpd init fish | source

function {cmd_name}
    set result (jumpd jump $argv 2>/dev/null)
    and cd $result
end

function __jumpd_track --on-variable PWD
    jumpd add $PWD 2>/dev/null
end""")
    elif shell == "powershell":
        print(f"""# jumpd shell integration for PowerShell
# Add this to your PowerShell profile:
#   Invoke-Expression (& {{ (jumpd init powershell | Out-String) }})

function global:{cmd_name} {{
    $result = jumpd jump $args 2>$null
    if ($LASTEXITCODE -eq 0 -and $result) {{
        Set-Location $result
    }}
}}

$__jumpd_prev = $PWD
function global:__jumpd_track {{
    if ($PWD -ne $__jumpd_prev) {{
        jumpd add $PWD 2>$null
        $__jumpd_prev = $PWD
    }}
}}
# Add to prompt
$global:__originalPrompt = $function:prompt
function global:prompt {{
    __jumpd_track
    & $global:__originalPrompt
}}""")
    else:
        print(f"jumpd: unknown shell '{shell}'. Supported: bash, zsh, fish, powershell", file=sys.stderr)
        return 1

    return 0


def cmd_info(args: argparse.Namespace) -> int:
    """Show database info."""
    db = Database.load()
    entries = db.entries
    existing = [e for e in entries if os.path.isdir(e.path)]
    total_score = sum(e.score for e in entries)

    print(f"database:    {_db_path()}")
    print(f"entries:     {len(entries)} ({len(existing)} exist, {len(entries) - len(existing)} missing)")
    print(f"total score: {_format_score(total_score)}")
    print(f"maxage:      {args.maxage}")
    if entries:
        newest = max(entries, key=lambda e: e.last_seen)
        oldest = min(entries, key=lambda e: e.last_seen)
        print(f"newest:      {newest.path} ({_time_ago(newest.last_seen)})")
        print(f"oldest:      {oldest.path} ({_time_ago(oldest.last_seen)})")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jumpd",
        description="A directory jumper with explainable scoring.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  jumpd add /path/to/project        # record a directory
  eval "$(jumpd jump proj)"          # cd to the best match for "proj"
  jumpd jump proj --explain          # show why that match won
  jumpd list                          # show all entries, ranked
  jumpd list -l 10                    # top 10
  jumpd query src --explain           # all matches for "src", with scores
  jumpd init bash                     # print bash integration script
""",
    )
    parser.add_argument("--version", action="version", version=f"jumpd {VERSION}")

    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add", help="Record a directory")
    p_add.add_argument("path", help="Directory to record")
    p_add.add_argument("--maxage", type=int, default=DEFAULT_MAXAGE, help=f"Max total score before aging (default {DEFAULT_MAXAGE})")
    p_add.set_defaults(func=cmd_add)

    # remove
    p_remove = sub.add_parser("remove", help="Remove a directory from the database")
    p_remove.add_argument("path", help="Directory to remove")
    p_remove.set_defaults(func=cmd_remove)

    # jump
    p_jump = sub.add_parser("jump", help="Jump to the best matching directory")
    p_jump.add_argument("keywords", nargs="+", help="Query keywords")
    p_jump.add_argument("--explain", action="store_true", help="Show score breakdown")
    p_jump.add_argument("--exclude", type=str, default=None, help=f"Directories to exclude (separated by {os.pathsep})")
    p_jump.add_argument("--cwd-only", action="store_true", help="Only match within current directory subtree")
    p_jump.add_argument("--maxage", type=int, default=DEFAULT_MAXAGE, help=f"Max total score before aging (default {DEFAULT_MAXAGE})")
    p_jump.set_defaults(func=cmd_jump)

    # list
    p_list = sub.add_parser("list", help="List all entries, ranked by frecency")
    p_list.add_argument("-l", "--limit", type=int, default=None, help="Limit to N entries")
    p_list.add_argument("--all", action="store_true", help="Include missing directories")
    p_list.set_defaults(func=cmd_list)

    # query
    p_query = sub.add_parser("query", help="Show all matches for a query")
    p_query.add_argument("keywords", nargs="+", help="Query keywords")
    p_query.add_argument("--explain", action="store_true", help="Show score breakdown")
    p_query.add_argument("--exclude", type=str, default=None, help=f"Directories to exclude (separated by {os.pathsep})")
    p_query.add_argument("--cwd-only", action="store_true", help="Only match within current directory subtree")
    p_query.set_defaults(func=cmd_query)

    # edit
    p_edit = sub.add_parser("edit", help="Edit an entry's score")
    p_edit.add_argument("path", help="Directory to edit")
    p_edit.add_argument("action", choices=["increment", "decrement", "delete"], help="Action to perform")
    p_edit.set_defaults(func=cmd_edit)

    # import
    p_import = sub.add_parser("import", help="Import from another tool's database")
    p_import.add_argument("source", choices=["z", "autojump"], help="Source format")
    p_import.add_argument("file", help="Path to source database file")
    p_import.set_defaults(func=cmd_import)

    # init
    p_init = sub.add_parser("init", help="Print shell integration script")
    p_init.add_argument("shell", choices=["bash", "zsh", "fish", "powershell"], help="Target shell")
    p_init.add_argument("--cmd", type=str, default=None, help="Command name (default: j)")
    p_init.set_defaults(func=cmd_init)

    # info
    p_info = sub.add_parser("info", help="Show database info")
    p_info.add_argument("--maxage", type=int, default=DEFAULT_MAXAGE, help=f"Max total score (default {DEFAULT_MAXAGE})")
    p_info.set_defaults(func=cmd_info)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
