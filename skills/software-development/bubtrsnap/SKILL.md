---
name: bubtrsnap
description: "Test, build, and develop the bubtrsnap btrfs snapshot/backup tool."
version: 0.1.0
author: Ger (github-handle), Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [btrfs, backup, snapshot, testing, ssh]
    related_skills: [requesting-code-review, test-driven-development]
---

# bubtrsnap Skill

Tools and workflows for developing the `bubtrsnap` btrfs snapshot and backup management script.

## When to Use

- Running the test suite for `bubtrsnap`
- Adding new features to `bubtrsnap` (CLI options, config options, keep policies)
- Debugging btrfs send/receive logic
- Verifying config parsing and precedence rules
- Working with SSH remote backups

## Prerequisites

- Linux with btrfs filesystem
- Python 3.11+
- btrfs-progs installed (`btrfs` command available)
- SSH access to remote host for remote backup features
- Run from `/home/lama7/src/bubtrsnap/` directory

## How to Run

```bash
# Run all tests
python3 -m unittest discover -v

# Run specific test module
python3 test_keep_policy.py -v
python3 test_file_options.py -v
python3 test_ssh.py -v

# Run main script with --help
python3 bubtrsnap --help

# Dry-run with config
python3 bubtrsnap --config /path/to/config.toml --dry-run -v
```

## Quick Reference

| Command | Purpose |
|---------|---------|
| `python3 -m unittest discover` | Run all tests |
| `python3 bubtrsnap --help` | Show CLI options |
| `python3 bubtrsnap --config <file> --dry-run -v` | Dry-run with config |
| `git diff` | Show uncommitted changes |
| `git add bubtrsnap test_*.py && git commit -m "msg"` | Commit changes |

### SSH Remote Options

| Option | Config Key | Description |
|--------|------------|-------------|
| `--remote-host user@host` | `remote_host` | SSH target host |
| `--remote-path /path` | `remote_path` | Remote btrfs subvolume directory |
| `--remote-sudo` | `remote_sudo` | Use `sudo -n` on remote |

### Dual-Destination Backup

When both `backup_dir` (local) and `remote_path` + `remote_host` are set, bubtrsnap sends to **both** in a single run. Each destination maintains its own parent snapshot for incremental send.

## Procedure

### Adding a new CLI option

1. Add argument to `argparse` in `main()` (around line 1600)
2. Add to `ALLOWED_GLOBAL_OPTIONS` or `ALLOWED_ARCHIVE_OPTIONS` for config file support
3. Add validation in `_check_type()` if needed
4. Apply CLI override in `load_and_resolve_archives()` (around line 490)
5. Thread through to relevant functions (e.g., `apply_keep_policy()`)
6. Add tests in `test_keep_policy.py` or `test_file_options.py`
7. Run tests: `python3 -m unittest discover`

### Adding a new keep policy interval

1. Add counter to `DEFAULT_CONFIG` and `ALLOWED_*_OPTIONS`
2. Add CLI argument
3. Update `build_keep_set()` interval table
4. Add `_prev_*()` helper function
5. Update `apply_keep_policy()` to pass new parameter
6. Add comprehensive tests

**⚠️ The keep policy algorithm (`build_keep_set`, `_prev_*`, interval table) must not be modified without explicit approval from the user.**

### Modifying config precedence

1. Locate `load_and_resolve_archives()` (around line 450)
2. Follow pattern: CLI > archive-specific > global
3. For "all-or-nothing" options (like keep), check `cli_keeps` dict
4. Update `resolve_keep_policy()` if needed
5. Add precedence tests in `test_file_options.py`

### SSH remote backup workflow

1. Ensure remote host has btrfs and the target directory is a subvolume
2. Configure `remote_host`, `remote_path` (and optionally `remote_sudo`) in config or CLI
3. Run with `--dry-run -v` to verify parent snapshot matching on both sides
4. For staged workflows: `--export-dir` + `--import-dir` on remote

## Pitfalls

- **Tests need real btrfs subvolumes** for integration tests — unit tests mock via `importlib`
- **Config validation** runs before archive resolution — invalid types exit early
- **Timestamp format** is `YYYYMMDDhhmm` (12 digits) — NOT `YYYYMMDDhhmmss`
- **Week boundaries**: default `sunday` start → week ends `saturday` (backward compat)
- **Force keeps** (`--keep` / `keep`) are applied BEFORE policy, logged as `"forced"`
- **`--keep` requires exactly one archive** — validated in `load_and_resolve_archives()`
- **SSH commands use `sudo -n`** when `sudo`/`remote_sudo` set — requires passwordless sudo
- **Remote validation** checks `remote_dir` is a btrfs subvolume via SSH
- **Dual-destination** requires parent snapshot on EACH side for incremental send

## Logging Standards

All new code must use the centralized `log()` helper and `run()` wrapper for consistent output.

### `log()` helper (line ~47)
```python
def log(msg: str, level: int, verbosity: int) -> None:
    """level 1 = flow/progress, level 2 = commands, level 3 = debug"""
    if verbosity >= level:
        if level >= 3:
            print(f"[debug] {msg}")
        else:
            print(msg)
```

**Usage rules:**
- **Level 1** (always shown with `-v`): major flow steps — "Creating snapshot...", "Starting backup...", "Applying keep policy..."
- **Level 2** (shown with `-vv`): every external command via `run()` — shows the full command line
- **Level 3** (`--debug`): internal decisions, UUIDs, command output, dry-run prefixes

### `run()` wrapper (line ~62)
```python
def run(cmd: list[str], dry_run: bool = False, verbosity: int = 0, **kwargs) -> subprocess.CompletedProcess | None:
    pretty = " ".join(str(c) for c in cmd)
    if dry_run:
        log(f"[dry-run] {pretty}", 2, verbosity)
        return None
    log(pretty, 2, verbosity)
    return subprocess.run(cmd, **kwargs)
```

**All external commands MUST go through `run()`** — never call `subprocess.run()` directly.

### Dry-run logging
- Prefix with `[dry-run] ` at level 2
- Shows what WOULD run without executing
- Implied by `--dry-run` (sets verbosity to at least 2)

### SSH command logging
- Remote commands logged at level 2 via `run()`
- Include `sudo -n` prefix when `remote_sudo` is set
- Validation commands (e.g., `btrfs subvolume list`) also go through `run()`

### Adding logging to new features
1. Use `log(f"Step description...", 1, cfg["verbose"])` for user-visible progress
2. Use `run(cmd, dry_run=cfg.get("dry_run"), verbosity=cfg["verbose"], ...)` for all commands
3. Use `log(f"[debug] internal state: {var}", 3, cfg["verbose"])` for debug-only detail
4. Never use bare `print()` for flow/command output — only for errors to stderr

## Verification

- All 32+ tests pass: `python3 -m unittest discover`
- Help shows new options: `python3 bubtrsnap --help | grep -A2 <option>`
- Config validation rejects invalid values
- Dry-run shows expected behavior without modifying filesystem
- SSH dry-run validates remote subvolume without sending data