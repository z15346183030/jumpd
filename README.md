# jumpd

A directory jumper with explainable scoring.

Remembers which directories you visit, then jumps to the best match for your query. The score is frecency (frequency × recency) plus match quality — and every point is printable with `--explain`.

**Zero dependencies. Single file. Pure Python stdlib.**

## Why

Directory jumpers usually rank purely by how often and how recently you visited a place. That answers "where do I go most", not "where did I mean".

jumpd separates the two questions:

- **Exact match is a hard tier.** If the last path component equals your query, it wins outright — even against a directory visited a thousand times. `web` should not lose to `my-web-frontend`.
- **Match quality is part of the score.** A query that covers most of a name ranks higher than one that barely touches it.
- **Case carries intent.** All-lowercase queries are case-insensitive; any uppercase letter makes the query case-sensitive.
- **A trailing slash means exact.** `web/` says "the last component is exactly `web`", not `web-frontend`.
- **Scoring is inspectable.** `--explain` prints every component, so the ranking is a sum you can check rather than a number you have to trust.

## Install

```
pip install jumpd
```

Or just copy `jumpd.py` to your `PATH`. No dependencies beyond Python 3.8+.

## Quick start

```bash
# Record directories (do this as you cd around)
jumpd add /home/user/projects/myapp
jumpd add /home/user/work/legacy-frontend

# Jump to the best match
eval "$(jumpd jump myapp)"       # cd to /home/user/projects/myapp

# See why it chose that one
jumpd jump myapp --explain       # prints score breakdown to stderr

# List all entries, ranked
jumpd list
jumpd list -l 10                 # top 10

# Query for all matches
jumpd query web --explain        # all matches with score breakdown

# Shell integration (optional — records dirs automatically)
eval "$(jumpd init bash)"        # bash/zsh/fish/powershell
```

## How scoring works

Each directory has a **frecency** score:

| Last accessed | Multiplier |
|---|---|
| Within 1 hour | score × 4 |
| Within 1 day | score × 2 |
| Within 1 week | score × 0.5 |
| Older | score × 0.25 |

The **total** score combines frecency with match quality:

- **Exact match** (query == last path component): always ranks first, regardless of frecency. This is a hard rule — an exact match beats a 1000-visit partial match.
- **Match quality** (0.0–1.0): what fraction of the last path component does your query cover? `proj` matching `my-project` scores 0.25; `project` matching `project` scores 1.0.
- **Non-exact total** = `frecency + match_quality × frecency × 0.3`

### `--explain` output

```
$ jumpd jump web --explain
jumpd: match breakdown
  path:      /home/user/projects/web
  frecency:  40.00  (score=10.0, last_seen=2m ago)
  match_q:   1.000
  exact:     +TIER  (exact last-component match, always ranked first)
  total:     1000000052.00
/home/user/projects/web
```

## Smart case

- Query all lowercase → case-insensitive: `jumpd jump web` matches `Web`, `WEB`, `web`
- Query has uppercase → case-sensitive: `jumpd jump Web` matches only `Web`

## Trailing slash

`jumpd jump web/` means "the last path component must be exactly `web`", not `web-frontend` or `my-web-app`.

## Commands

| Command | Description |
|---|---|
| `jumpd add <path>` | Record a directory (increments score if exists) |
| `jumpd jump <keywords>` | Print the best matching directory (for `eval`) |
| `jumpd list [-l N] [--all]` | List entries ranked by frecency |
| `jumpd query <keywords> [--explain]` | Show all matches with optional breakdown |
| `jumpd remove <path>` | Remove a directory from the database |
| `jumpd edit <path> <increment\|decrement\|delete>` | Adjust scores manually |
| `jumpd import <z\|autojump> <file>` | Import from another tool's database |
| `jumpd init <bash\|zsh\|fish\|powershell>` | Print shell integration script |
| `jumpd info` | Show database statistics |

## Options

| Option | Description | Default |
|---|---|---|
| `--explain` | Show score breakdown | off |
| `--exclude <dirs>` | Exclude directories (pathsep-separated) | none |
| `--cwd-only` | Only match within current directory subtree | off |
| `--cmd <name>` | Shell integration command name | `j` |
| `-l, --limit N` | Limit list output to N entries | all |
| `--all` | Include missing directories in list | off |

## Environment variables

| Variable | Description | Default |
|---|---|---|
| `JUMPD_DATA_DIR` | Database directory | `$XDG_DATA_HOME/jumpd` or `~/.local/share/jumpd` |

## Database format

Plain text TSV, one entry per line:

```
<score>\t<last_seen_epoch>\t<path>
```

Human-readable, diff-friendly, and trivially editable.

## Aging

When the total score of all entries exceeds `--maxage` (default 10,000), all scores are divided by a factor *k* so the new total is ~90% of the max. Entries with score < 1 after scaling are removed.

## Testing

```bash
python -m unittest discover -s tests
```

## License

MIT
