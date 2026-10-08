#!/usr/bin/env python3
"""
bubtrsnap— Simple btrfs snapshot & backup tool
A new implementation inspired by the goals of btrbu (minimal deps, single file,
sensible config, hooks, keep policy, incremental send/receive).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
import re
import struct
import calendar
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

VERSION = "0.3.0"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "snapshot_dir": None,
    "backup_dir": None,
    "local_sudo": False,
    "snaps_only": False,
    "export_dir": None,
    "import_dir": None,
    "stage_dir": None,
    "remote_host": None,
    "remote_path": None,
    "remote_sudo": False,
    # Global default keep policy
    "keep_hourly": 0,
    "keep_daily": 0,
    "keep_weekly": 0,
    "keep_monthly": 0,
    "keep_yearly": 0,
    "week_startday": "sunday",
    "verbose": 0,
    "dry_run": False,
    "rsync": False,
    "rsync_opts": None,
}

# Day name to weekday (Mon=0 ... Sun=6)
_DAY_TO_WEEKDAY = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# ---------------------------------------------------------------------------
# rsync options blocked from user-specified --rsync-opts
# (dangerous or incompatible with bubtrsnap's stream-file workflow)
# ---------------------------------------------------------------------------
_BLOCKED_RSYNC_OPTS = frozenset({
    # user must never control partial-dir — bubtrsnap manages it
    "--partial-dir",
    # destructive / file-removing options
    "--remove-source-files",
    "--delete", "--delete-excluded", "--delete-after", "--delete-before",
    "--delete-during", "--delete-delay",
    "--backup", "--backup-dir", "--suffix",
    "--inplace", "--delay-updates",
    # options that produce stdout / progress output
    "-v", "--verbose", "--progress", "--info", "--stats",
    "--human-readable", "--out-format", "--itemize-changes",
    # dry-run / daemon / batch / server modes (incompatible)
    "--dry-run", "--daemon", "--server", "--read-batch", "--write-batch",
    "--only-write-batch",
    # file-list and connection options (bubtrsnap controls the file)
    "--files-from", "--files-from0", "--from0", "--no-from0",
    "--files-from-dir",
    "--bwlimit", "--port", "--address", "--bind-address", "--contimeout",
    "--timeout", "--blocking-io",
    "--rsh", "--rsync-path", "--password-file",
    # path / content manipulation
    "--filter", "--filter-from",
    "--exclude", "--exclude-from",
    "--include", "--include-from",
    "--no-implied-dirs", "--no-dirs",
    "--link-dest", "--hard-link",
    "--copy-links", "--copy-unsafe-links", "--copy-dirlinks",
    "--no-times", "--no-perms", "--no-owner", "--no-group",
    "--no-links", "--no-devices", "--no-specials",
    "--acls", "--xattrs", "--extended-attributes",
    "--atimes", "--no-atimes",
    "--numeric-ids", "--fake-super", "--sparse",
    "--relative", "--time-dir", "--stop-at",
    "--chmod", "--chown", "--checksum", "--checksum-seed",
    "--no-whole-file", "--old-dirs",
})


def _filter_rsync_opts(user_opts: str | None) -> list[str]:
    """
    Parse user-supplied rsync options string, filter out blocked options,
    and return a list of individual option tokens.
    Blocked options are reported via stderr to the user.
    """
    if not user_opts:
        return []
    import shlex
    tokens = shlex.split(user_opts)
    filtered: list[str] = []
    for tok in tokens:
        # Strip '--option=value' to just '--option' for comparison
        opt_base = tok.split("=", 1)[0]
        # Expand combined short options: -avz → -a, -v, -z
        if opt_base.startswith("-") and not opt_base.startswith("--"):
            short_flags = opt_base[1:]
            blocked_shorts = set()
            kept_parts = []
            for flag in short_flags:
                short_opt = f"-{flag}"
                if short_opt in _BLOCKED_RSYNC_OPTS:
                    blocked_shorts.add(flag)
                else:
                    kept_parts.append(flag)
            if blocked_shorts:
                for flag in sorted(blocked_shorts):
                    print(f"Warning: blocking unsafe rsync option -{flag} from --rsync-opts",
                          file=sys.stderr)
            if kept_parts:
                filtered.append(f"-{''.join(kept_parts)}")
            continue
        # Long option
        if opt_base in _BLOCKED_RSYNC_OPTS:
            print(f"Warning: blocking unsafe rsync option {opt_base} from --rsync-opts",
                  file=sys.stderr)
            continue
        filtered.append(tok)
    return filtered


def _quote_for_display(s: str) -> str:
    """Quote a string for shell display, using double quotes for args with spaces.

    Unlike shlex.quote (which defaults to single quotes), this preserves the
    double-quote style that users typically write in config files (e.g.
    ``-e "ssh -p 2222"``).  Falls back to shlex.quote for strings that contain
    double quotes or other shell metacharacters.
    """
    s = str(s)
    if " " not in s:
        return s
    if '"' in s or any(c in s for c in "\\$`"):
        import shlex
        return shlex.quote(s)
    return f'"{s}"'


def log(msg: str, level: int, verbosity: int) -> None:
    """
    level 1 = flow / progress
    level 2 = commands
    level 3 = debug (UUIDs, command output, internal decisions)
    """
    if verbosity >= level:
        if level >= 3:
            print(f"[debug] {msg}")
        else:
            print(msg)
    
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def run(cmd: list[str] | str,
        dry_run: bool = False,
        read_only: bool = False,
        verbosity: int = 0,
        **kwargs) -> subprocess.CompletedProcess | None:
    """
    Run a command, with optional dry-run and logging support.
    
    Extra keyword arguments are passed directly to subprocess.run().
    Common ones: check=True, capture_output=True, text=True, etc.
    
    Args:
        dry_run: If True, normally skips execution and logs [dry-run] prefix.
        read_only: If True AND dry_run is True, still executes the command
                   (read-only operations like list/show/validate should run
                   during dry-run to give the user a realistic preview).
                   Returns None so callers that check for None don't proceed
                   with write operations.
    """
    pretty = cmd if isinstance(cmd, str) else " ".join(_quote_for_display(c) for c in cmd)

    if dry_run:
        if read_only:
            # Read-only operations execute during dry-run for realistic preview
            log(pretty, 2, verbosity)
            try:
                return subprocess.run(cmd, **kwargs)
            except subprocess.CalledProcessError:
                raise
            except Exception as e:
                cmd_name = cmd[0] if isinstance(cmd, list) else cmd
                log(f"[dry-run] {cmd_name} failed: {e}", 2, verbosity)
                return None
        log(f"[dry-run] {pretty}", 2, verbosity)
        return None

    log(pretty, 2, verbosity)
    return subprocess.run(cmd, **kwargs)

def piped_run(send_cmd: list[str],
              recv_cmd: list[str],
              dry_run: bool = False,
              verbosity: int = 0,
              **kwargs) -> tuple[int, str]:
    """
    Pipe the stdout of `send_cmd` into the stdin of `recv_cmd`.

    Returns (returncode, stderr_text) of the receiver process.
    Logs both commands at verbosity 2, honours dry-run by logging
    and returning (0, "") without executing.
    """
    pretty = f"{' '.join(send_cmd)} | {' '.join(recv_cmd)}"
    if dry_run:
        log(f"[dry-run] {pretty}", 2, verbosity)
        return 0, ""
    log(pretty, 2, verbosity)
    try:
        send_proc = subprocess.Popen(send_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        recv_proc = subprocess.Popen(recv_cmd, stdin=send_proc.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        send_proc.stdout.close()
        _, stderr_data = recv_proc.communicate()
        stderr_text = stderr_data.decode() if isinstance(stderr_data, bytes) else (stderr_data or "")
        return recv_proc.returncode, stderr_text
    except FileNotFoundError as e:
        log(f"Pipe failed: {e}", 2, verbosity)
        return 1, str(e)

def _timed_run(cmd: list[str] | str, label: str, cfg: dict, **kwargs) -> subprocess.CompletedProcess | None:
    """Run a command with timing and return the result.

    Wraps run() to log elapsed time at verbosity level 1.
    Logs timing even in dry-run mode to show expected operation duration.
    """
    start = datetime.now()
    result = run(cmd, dry_run=cfg.get("dry_run", False),
                 verbosity=cfg["verbose"], **kwargs)
    elapsed = (datetime.now() - start).total_seconds()
    log(f"{label} completed in {format_duration(elapsed)}", 1, cfg["verbose"])
    return result


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d%H%M")

def _merge_keep(config_val: str | None, cli_val: list[str] | None) -> list[str]:
    """Merge keep from config (comma-separated string) and CLI (list of strings).
    
    Returns a list of timestamp strings (12-digit format).
    """
    result = []
    # Config value: comma-separated string
    if config_val:
        for ts in config_val.split(","):
            ts = ts.strip()
            if ts:
                result.append(ts)
    # CLI value: list from append action
    if cli_val:
        for item in cli_val:
            if item:
                # Also support comma-separated in CLI
                for ts in item.split(","):
                    ts = ts.strip()
                    if ts:
                        result.append(ts)
    return result

def parse_ts(name: str) -> datetime | None:
    try:
        # expected form archive.YYYYMMDDHHMM
        ts = name.rsplit(".", 1)[-1]
        return datetime.strptime(ts, "%Y%m%d%H%M")
    except Exception:
        return None

def format_keep_policy(keep: dict) -> str:
    """Return a short human-readable summary of the keep policy."""
    parts = []
    mapping = [
        ("keep_hourly", "hourly"),
        ("keep_daily", "daily"),
        ("keep_weekly", "weekly"),
        ("keep_monthly", "monthly"),
        ("keep_yearly", "yearly"),
    ]
    for key, label in mapping:
        value = keep.get(key, 0)
        if value > 0:
            parts.append(f"{value} {label}")
    return ", ".join(parts) if parts else "none"

def format_duration(seconds: float) -> str:
    """Return a human-readable duration string."""
    if seconds < 60:
        return f"{seconds:.1f} seconds"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = seconds % 60
        return f"{minutes}m {secs:.1f}s"
    else:
        hours = int(seconds // 3600)
        remaining = seconds % 3600
        minutes = int(remaining // 60)
        secs = remaining % 60
        return f"{hours}h {minutes}m {secs:.1f}s"

def expand_path(path: str | None) -> str | None:
    if path is None:
        return None
    return str(Path(path).expanduser())

def chk_is_dir(path: str, label: str = "directory") -> None:
    if not path or not str(path).strip():
        print(f"Error: {label} value not set", file=sys.stderr)
        sys.exit(1)
    p = Path(path)
    if p.is_file() or not p.is_dir():
        print(f"Error: {label} '{path}' is not an existing directory",
              file=sys.stderr)
        sys.exit(1)

# ---------------------------------------------------------------------------
# Helpers for dealing with stream files
# ---------------------------------------------------------------------------
BTRFS_STREAM_MAGIC = b"btrfs-stream\x00"
BTRFS_SEND_C_SUBVOL = 0x0001
BTRFS_SEND_C_SNAPSHOT = 0x0002
BTRFS_SEND_A_PATH = 0x000F

# First command is tiny (path + UUIDs); 64 KiB is plenty
_STREAM_PREFIX_SIZE = 64 * 1024

# CRC32C (Castagnoli) — polynomial 0x1EDC6F41 reflected
def _crc32c_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x82F63B78  # reflected Castagnoli
            else:
                crc >>= 1
        table.append(crc)
    return table

_CRC32C_TAB = _crc32c_table()

def crc32c(data: bytes, crc: int = 0) -> int:
    """CRC32C with initial seed 0 (btrfs send stream convention)."""
    for b in data:
        crc = _CRC32C_TAB[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc & 0xFFFFFFFF

def _read_file_prefix(path: Path, cfg: dict, size: int = _STREAM_PREFIX_SIZE) -> bytes | None:
    """Read the first `size` bytes of path, using sudo -n when configured."""
    path = Path(path)
    if cfg.get("local_sudo"):
        # dd: portable, no partial-line issues; status=none keeps stderr quiet
        cmd = [
            "sudo", "-n", "dd",
            f"if={path}",
            f"bs={size}",
            "count=1",
            "status=none",
        ]
        result = run(cmd, dry_run=cfg.get("dry_run", False), read_only=True,
                     verbosity=cfg.get("verbose", 0),
                     capture_output=True, check=False)
        if result is None:
            return None
        if result.returncode != 0:
            return None
        return result.stdout or b""
    try:
        with path.open("rb") as f:
            return f.read(size)
    except OSError:
        return None

def _parse_stream_prefix(data: bytes) -> str | None:
    """
    Parse stream header + first SUBVOL/SNAPSHOT command from a byte buffer.
    Returns the PATH attribute value, or None if invalid.
    """
    if len(data) < 13 + 4 + 10:
        return None
    if data[0:13] != BTRFS_STREAM_MAGIC:
        return None

    # version at offset 13 (u32 le) — optional: require version >= 1
    offset = 17  # magic + version

    if offset + 10 > len(data):
        return None
    len_data, cmd, stored_crc = struct.unpack_from("<IHI", data, offset)

    if cmd not in (BTRFS_SEND_C_SUBVOL, BTRFS_SEND_C_SNAPSHOT):
        return None
    if len_data <= 0 or offset + 10 + len_data > len(data):
        # Command body not fully in prefix — treat as invalid / too large
        return None

    # CRC covers command header + body, with CRC field set to 0
    cmd_region = bytearray(data[offset:offset + 10 + len_data])
    struct.pack_into("<I", cmd_region, 6, 0)  # zero checksum field
    calc_crc = crc32c(bytes(cmd_region), 0)
    if calc_crc != stored_crc:
        return None

    body = data[offset + 10:offset + 10 + len_data]
    pos = 0
    while pos + 4 <= len(body):
        tlv_type, tlv_len = struct.unpack_from("<HH", body, pos)
        pos += 4
        if tlv_len < 0 or pos + tlv_len > len(body):
            return None
        value = body[pos:pos + tlv_len]
        pos += tlv_len
        if tlv_type == BTRFS_SEND_A_PATH:
            name = value.decode("utf-8", errors="replace").rstrip("\x00")
            return name.lstrip("./") or None
    return None

def _read_stream_header_and_path(path: Path, cfg: dict) -> str | None:
    data = _read_file_prefix(Path(path), cfg)
    if not data:
        return None
    return _parse_stream_prefix(data)

def is_btrfs_stream(path: Path, cfg: dict | None = None) -> bool:
    cfg = cfg or {}
    return _read_stream_header_and_path(Path(path), cfg) is not None

def stream_snapshot_name(path: Path, cfg: dict | None = None) -> str | None:
    cfg = cfg or {}
    return _read_stream_header_and_path(Path(path), cfg)
   
def find_newest_stream_for_archive(directory: Path, archive: str, cfg: dict) -> Path | None:
    """
    In directory, find the newest valid stream whose embedded snapshot name
    belongs to the given archive (name starts with '{archive}.').
    """
    candidates: list[tuple[datetime, Path]] = []
    if not directory.is_dir():
        return None
    for p in directory.iterdir():
        if not p.is_file():
            continue
        snap_name = stream_snapshot_name(p, cfg)
        if not snap_name:
            continue
        # Match archive by conventional prefix archive.TIMESTAMP
        base = snap_name.split("/")[-1]
        if not (base == archive or base.startswith(archive + ".")):
            continue
        ts = parse_ts(base)  # existing helper
        if ts is None:
            # fall back to file mtime
            ts = datetime.fromtimestamp(p.stat().st_mtime)
        candidates.append((ts, p))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]

def remove_stage_file(path: Path, cfg: dict) -> None:
    """Delete a staging stream file after receive (honours dry-run / sudo)."""
    if cfg.get("dry_run"):
        log(f"[dry-run] remove staged stream {path}", 2, cfg["verbose"])
        return
    if not path.exists():
        return
    try:
        if cfg.get("local_sudo"):
            run(["sudo", "-n", "rm", "-f", str(path)],
                dry_run=cfg.get("dry_run", False),
                verbosity=cfg["verbose"],
                capture_output=True, check=False)
        else:
            path.unlink(missing_ok=True)
        log(f"Removed staged stream file: {path}", 1, cfg["verbose"])
    except OSError as e:
        print(f"Warning: could not remove staged stream '{path}': {e}", file=sys.stderr)

# ---------------------------------------------------------------------------
# Configuration Processing
# ---------------------------------------------------------------------------
ALLOWED_GLOBAL_OPTIONS = {
    "snapshot_dir": str,
    "backup_dir": str,
    "local_sudo": bool,
    "snaps_only": bool,
    "export_dir": str,
    "import_dir": str,
    "stage_dir": str,
    "remote_host": str,
    "remote_path": str,
    "remote_sudo": bool,
    "keep_hourly": int,
    "keep_daily": int,
    "keep_weekly": int,
    "keep_monthly": int,
    "keep_yearly": int,
    "week_startday": str,
    "verbose": int,
    "dry_run": bool,
    "rsync": bool,
    "rsync_opts": str,
}

ALLOWED_ARCHIVE_OPTIONS = {
    "subvolume": str,
    "snaps_only": bool,
    "export_file": str,
    "import_file": str,
    "stage_file": str,
    "pre_snapshot_hook": str,
    "post_snapshot_hook": str,
    "post_backup_hook": str,
    "keep_hourly": int,
    "keep_daily": int,
    "keep_weekly": int,
    "keep_monthly": int,
    "keep_yearly": int,
    "week_startday": str,
    "forced_keep": str,
    "remote_host": str,
    "remote_path": str,
    "remote_sudo": bool,
    "backup_dir": str,
    "export_dir": str,
    "import_dir": str,
    "stage_dir": str,
    "rsync": bool,
    "rsync_opts": str,
}

def _check_type(name: str, value: Any, expected_type: type, context: str) -> None:
    """Helper to check a single value's type."""
    if expected_type is str:
        if not isinstance(value, str):
            print(f"Error: '{name}' in {context} must be a string, got {type(value).__name__}",
                  file=sys.stderr)
            sys.exit(1)
        # Special validation for week_startday
        if name == "week_startday":
            if value.lower() not in _DAY_TO_WEEKDAY:
                print(f"Error: '{name}' in {context} must be one of: monday, tuesday, wednesday, thursday, friday, saturday, sunday",
                      file=sys.stderr)
                sys.exit(1)
        # Special validation for forced_keep (timestamp format YYYYMMDDhhmm)
        if name == "forced_keep":
            # Split by comma and validate each timestamp
            timestamps = [t.strip() for t in value.split(",")]
            for ts in timestamps:
                if len(ts) != 12 or not ts.isdigit():
                    print(f"Error: '{name}' in {context} must be a comma-separated list of timestamps in YYYYMMDDhhmm format",
                          file=sys.stderr)
                    sys.exit(1)
    elif expected_type is int:
        if not isinstance(value, int) or isinstance(value, bool):  # bool is subclass of int
            print(f"Error: '{name}' in {context} must be an integer, got {type(value).__name__}",
                  file=sys.stderr)
            sys.exit(1)
    elif expected_type is bool:
        if not isinstance(value, bool):
            print(f"Error: '{name}' in {context} must be a boolean (true/false), got {type(value).__name__}",
                  file=sys.stderr)
            sys.exit(1)

def validate_config(file_cfg: dict) -> None:
    """
    Validate that the loaded TOML only contains known options
    and that their types are correct.
    """
    for key, value in file_cfg.items():
        if isinstance(value, dict) and "subvolume" in value:
            # ----- Archive section -----
            archive_name = key
            context = f"archive section [{archive_name}]"

            for opt, opt_value in value.items():
                if opt not in ALLOWED_ARCHIVE_OPTIONS:
                    print(f"Error: unknown option '{opt}' in {context}", file=sys.stderr)
                    sys.exit(1)
                _check_type(opt, opt_value, ALLOWED_ARCHIVE_OPTIONS[opt], context)

            # Extra sanity: subvolume should not be empty
            v = value.get("subvolume", "")
            if not v.strip():
                print(f"Error: 'subvolume' in {context} cannot be empty", file=sys.stderr)
                sys.exit(1)

            # remote and remote_path - remote_path requires remote_host, but remote_host can be used alone
            has_remote = "remote_host" in value
            has_remote_path = "remote_path" in value
            if has_remote_path and not has_remote:
                # remote_path at archive level needs a remote_host (will check global/CLI later)
                pass  # We'll validate after resolution

        else:
            # ----- Global option -----
            if key not in ALLOWED_GLOBAL_OPTIONS:
                print(f"Error: unknown global option '{key}' in configuration file",
                      file=sys.stderr)
                sys.exit(1)
            _check_type(key, value, ALLOWED_GLOBAL_OPTIONS[key], "global configuration")

    # Global remote_path requires remote_host (but remote_host can be used alone)
    has_global_remote = "remote_host" in file_cfg
    has_global_remote_path = "remote_path" in file_cfg
    if has_global_remote_path and not has_global_remote:
        # global remote_path needs a remote_host (will check CLI later)
        pass  # We'll validate after resolution


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as f:
        return tomllib.load(f)

def resolve_keep_policy(archive_cfg: dict, global_cfg: dict, cli_keeps: dict) -> dict:
    """
    Resolve the effective keep policy for an archive.
    
    Priority:
    1. CLI keeps (if any were specified) → missing ones become 0
    2. Archive-specific keeps → missing ones become 0
    3. Global keeps
    """
    keep_keys = ["keep_hourly", "keep_daily", "keep_weekly", "keep_monthly", "keep_yearly"]

    # 1. CLI takes full control if any keep was given
    if cli_keeps:
        policy = {k: 0 for k in keep_keys}
        policy.update(cli_keeps)
        return policy

    # 2. Archive-specific keeps (complete override)
    archive_has_keeps = any(k in archive_cfg for k in keep_keys)
    if archive_has_keeps:
        policy = {k: 0 for k in keep_keys}
        for k in keep_keys:
            if k in archive_cfg:
                policy[k] = archive_cfg[k]
        return policy

    # 3. Fall back to global
    return {k: global_cfg.get(k, 0) for k in keep_keys}

def parse_cli_archives(raw_args: list[str]) -> dict[str, str | None]:
    """
    Parse CLI archive arguments.
    
    Returns:
        dict of {archive_name: subvolume_path_or_None}
        
    Examples:
        "home"              → {"home": None}
        "home=/home"        → {"home": "/home"}
        "home maildir=/mail"→ {"home": None, "maildir": "/mail"}
    """
    result = {}
    for item in raw_args:
        if "=" in item:
            name, path = item.split("=", 1)
            if not name:
                print(f"Invalid archive specification: {item}", file=sys.stderr)
                sys.exit(1)
            result[name] = path
        else:
            if not item:
                print("Invalid empty archive name", file=sys.stderr)
                sys.exit(1)
            result[item] = None
    return result

# ---------------------------------------------------------------------------
# Main entry to setup archives for processing
# ---------------------------------------------------------------------------
def load_and_resolve_archives(cli: argparse.Namespace, config_path: Path | None) -> tuple[dict, list[dict]]:
    global_cfg = DEFAULT_CONFIG.copy()
    raw_archives = {}  # name -> raw section from TOML

    # Load TOML
    if config_path and config_path.exists():
        try:
            file_cfg = load_toml(config_path)
        except Exception as e:
            print(f"Error in config file {config_path}: {e}", file=sys.stderr)
            sys.exit(1)

        validate_config(file_cfg)

        for key, value in file_cfg.items():
            if isinstance(value, dict) and "subvolume" in value:
                raw_archives[key] = value
            else:
                global_cfg[key] = value

    # Apply global CLI overrides
    if cli.snapshot_dir:
        global_cfg["snapshot_dir"] = cli.snapshot_dir
    if cli.backup_dir:
        global_cfg["backup_dir"] = cli.backup_dir
    if cli.local_sudo:
        global_cfg["local_sudo"] = True
    if getattr(cli, "snaps_only", False):
        global_cfg["snaps_only"] = True
    if getattr(cli, "week_startday", None):
        ws = cli.week_startday.lower()
        if ws not in _DAY_TO_WEEKDAY:
            print(f"Error: --week-startday must be one of: monday, tuesday, wednesday, thursday, friday, saturday, sunday",
                  file=sys.stderr)
            sys.exit(1)
        global_cfg["week_startday"] = ws

    # Track if CLI explicitly set remote/remote_path/remote_sudo (for precedence over archive-specific config)
    cli_remote_host = getattr(cli, "remote_host", None)
    cli_remote_path = getattr(cli, "remote_path", None)
    cli_remote_sudo = getattr(cli, "remote_sudo", False)
    if cli_remote_host is not None:
        global_cfg["remote_host"] = cli_remote_host
    if cli_remote_path is not None:
        global_cfg["remote_path"] = cli_remote_path
    if cli_remote_sudo:
        global_cfg["remote_sudo"] = True

    # rsync / rsync_opts CLI overrides (global level)
    cli_rsync = getattr(cli, "rsync", False)
    if cli_rsync:
        global_cfg["rsync"] = True
    cli_rsync_opts = getattr(cli, "rsync_opts", None)
    if cli_rsync_opts:
        global_cfg["rsync_opts"] = cli_rsync_opts

    # Track if CLI explicitly set backup_dir for precedence over archive-specific config
    cli_backup_dir = getattr(cli, "backup_dir", None)
    if cli_backup_dir is not None:
        global_cfg["backup_dir"] = cli_backup_dir

    # -------------------------------------------------------------------------
    # Six file-related options resolution
    #   CLI  >  archive-specific  >  global
    # If ANY of the six appear on the CLI, config file settings for all six
    # are ignored (as if they were not present in the config).
    # -------------------------------------------------------------------------
    FILE_RELATED_CLI = (
        "export_file",
        "import_file",
        "stage_file",
        "export_dir",
        "import_dir",
        "stage_dir",
    )

    cli_export_file = expand_path(cli.export_file) if getattr(cli, "export_file", None) else None
    cli_import_file = expand_path(cli.import_file) if getattr(cli, "import_file", None) else None
    cli_stage_file = expand_path(cli.stage_file) if getattr(cli, "stage_file", None) else None
    cli_export_dir = expand_path(cli.export_dir) if getattr(cli, "export_dir", None) else None
    cli_import_dir = expand_path(cli.import_dir) if getattr(cli, "import_dir", None) else None
    cli_stage_dir = expand_path(cli.stage_dir) if getattr(cli, "stage_dir", None) else None

    cli_has_file_related = any(
        getattr(cli, name, None) for name in FILE_RELATED_CLI
    )

    # Validate dirs when provided on CLI
    if cli_export_dir:
        chk_is_dir(cli_export_dir, "export-dir")
    if cli_import_dir:
        chk_is_dir(cli_import_dir, "import-from-dir")
    if cli_stage_dir:
        chk_is_dir(cli_stage_dir, "stage-dir")

    # Expand config global dirs only when CLI is not driving file-related opts
    if not cli_has_file_related:
        if global_cfg.get("export_dir"):
            global_cfg["export_dir"] = expand_path(global_cfg["export_dir"])
        if global_cfg.get("import_dir"):
            global_cfg["import_dir"] = expand_path(global_cfg["import_dir"])
        if global_cfg.get("stage_dir"):
            global_cfg["stage_dir"] = expand_path(global_cfg["stage_dir"])
        if global_cfg.get("stage_dir") and (
            global_cfg.get("export_dir") or global_cfg.get("import_dir")
        ):
            print(
                "Error: stage_dir cannot be combined with export_dir or import_dir",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        # CLI owns this layer: strip config globals so they cannot leak through
        global_cfg["export_dir"] = None
        global_cfg["import_dir"] = None
        global_cfg["stage_dir"] = None

    # --- mutual exclusion on CLI (unchanged rules) ---
    file_related = [
        ("--export-file", cli_export_file),
        ("--import-file", cli_import_file),
        ("--stage-file", cli_stage_file),
        ("--export-dir", cli_export_dir),
        ("--import-dir", cli_import_dir),
        ("--stage-dir", cli_stage_dir),
    ]
    if cli_stage_file:
        others = [n for n, v in file_related if n != "--stage-file" and v]
        if others:
            print(
                f"Error: --stage-file is mutually exclusive with {', '.join(others)}",
                file=sys.stderr,
            )
            sys.exit(1)
    if cli_stage_dir:
        others = [n for n, v in file_related if n != "--stage-dir" and v]
        if others:
            print(
                f"Error: --stage-dir is mutually exclusive with {', '.join(others)}",
                file=sys.stderr,
            )
            sys.exit(1)

    if (cli_export_file or cli_import_file or cli_stage_file) and (
        cli_export_dir or cli_import_dir or cli_stage_dir
    ):
        print(
            "Error: cannot mix file options with directory options on the CLI",
            file=sys.stderr,
        )
        sys.exit(1)

    # CLI keep options
    cli_keeps = {}
    for key in ("keep_hourly", "keep_daily", "keep_weekly", "keep_monthly", "keep_yearly"):
        val = getattr(cli, key, None)
        if val is not None:
            cli_keeps[key] = val

    # CLI forced-keep options (--forced-keep) - requires exactly one archive total
    cli_keep = getattr(cli, "forced_keep", None)
    if cli_keep:
        # Count total archives that would be processed
        cli_archives = parse_cli_archives(cli.archives) if cli.archives else {}
        n_cli = len(cli_archives)
        n_cfg = len(raw_archives)
        total_archives = n_cli if n_cli > 0 else n_cfg
        if total_archives != 1:
            print(
                "Error: --forced-keep requires exactly one archive to process "
                f"(found {total_archives})",
                file=sys.stderr,
            )
            sys.exit(1)

    # now process any archives from the CLI
    cli_archives = parse_cli_archives(cli.archives) if cli.archives else {}

    # Single-archive requirement for file options
    single_archive_opts = cli_export_file or cli_import_file or cli_stage_file
    if single_archive_opts:
        n_cli = len(cli_archives) if cli_archives else 0
        n_cfg = len(raw_archives)
        if n_cli > 1:
            print(
                "Error: --export-file/--import-file/--stage-file require exactly one archive",
                file=sys.stderr,
            )
            sys.exit(1)
        if n_cli == 0 and n_cfg != 1:
            print(
                "Error: --export-file/--import-file/--stage-file with no CLI archive "
                "require exactly one archive in the configuration file",
                file=sys.stderr,
            )
            sys.exit(1)


    # iterate through archives from CLI or config file and set them up
    archives_to_process = []

    if cli_archives:
        # Only process archives listed on the CLI
        for name, cli_subvol in cli_archives.items():
            raw = raw_archives.get(name, {})

            if cli_subvol is None:
                # No subvolume given on CLI → must exist in config
                if name not in raw_archives:
                    print(f"Error: archive '{name}' not found in configuration and no subvolume specified.",
                          file=sys.stderr)
                    sys.exit(1)
                subvol = raw.get("subvolume")
            else:
                # Explicit subvolume on CLI wins
                subvol = cli_subvol

            if not subvol:
                print(f"Error: archive '{name}' has no subvolume defined.", file=sys.stderr)
                sys.exit(1)

            archive_cfg = {
                "name": name,
                "subvolume": subvol,
                **raw,
            }
            # Ensure CLI subvolume takes precedence
            archive_cfg["subvolume"] = subvol
            archives_to_process.append(archive_cfg)
    else:
        # No archives on CLI → process all from config
        for name, raw in raw_archives.items():
            archive_cfg = {"name": name, **raw}
            archives_to_process.append(archive_cfg)

    # Resolve final settings for each archive
    resolved = []
    for raw in archives_to_process:
        name = raw["name"]
        subvol = raw.get("subvolume")

        # archive subvolume must exist and be a valid btrfs subvolume
        if not subvol:
            print(f"Error: archive '{name}' has no subvolume defined.", file=sys.stderr)
            sys.exit(1)

        subvol_path = Path(subvol)
        chk_btrfs_subvolume(subvol_path, global_cfg)

        if cli_has_file_related:
            # Rule 1: CLI only — ignore all config file-related options
            send_file = cli_export_file
            recv_file = cli_import_file
            stage_file = cli_stage_file
            send_dir = cli_export_dir
            recv_dir = cli_import_dir
            stage_dir = cli_stage_dir
            # CLI remote/remote_path override everything
            remote_host = cli_remote_host
            remote_path = cli_remote_path
            remote_sudo = cli_remote_sudo
            # Initialize export/import dirs for CLI mode
            export_dir = None
            import_dir = None
            # rsync settings: CLI global value always wins
            archive_rsync = global_cfg["rsync"]
            archive_rsync_opts = global_cfg.get("rsync_opts")
        else:
            # Rules 2 & 3: archive-specific first; global only if none
            send_file = raw.get("export_file")
            recv_file = raw.get("import_file")
            stage_file = raw.get("stage_file")
            if send_file:
                send_file = expand_path(send_file)
            if recv_file:
                recv_file = expand_path(recv_file)
            if stage_file:
                stage_file = expand_path(stage_file)

            # Also read export_dir, import_dir and stage_dir from archive config
            export_dir = raw.get("export_dir")
            import_dir = raw.get("import_dir")
            stage_dir = raw.get("stage_dir")
            if export_dir:
                export_dir = expand_path(export_dir)
            if import_dir:
                import_dir = expand_path(import_dir)
            if stage_dir:
                stage_dir = expand_path(stage_dir)

            if stage_file and (send_file or recv_file):
                print(
                    f"Error in archive '{name}': stage_file cannot be combined with "
                    f"export_file or import_file",
                    file=sys.stderr,
                )
                sys.exit(1)

            # Initialize send_dir/recv_dir to None (dir vars not yet set);
            # stage_dir/export_dir/import_dir were set from raw above.
            send_dir = None
            recv_dir = None
            
            # Only file options (send_file, recv_file, stage_file) count as "file opts"
            # export_dir/import_dir/stage_dir are directory options that should be preserved
            archive_has_file_opts = bool(send_file or recv_file or stage_file)
            if archive_has_file_opts:
                # Rule 2: archive-specific only — no global dirs
                send_dir = None
                recv_dir = None
                stage_dir = None
                export_dir = None
                import_dir = None
            else:
                # Rule 3: globals apply only when archive has no file opts
                send_dir = None
                recv_dir = None
                # Preserve archive's export_dir/import_dir/stage_dir if set;
                # fall back to global if not
                if export_dir is None:
                    export_dir = global_cfg.get("export_dir")
                if import_dir is None:
                    import_dir = global_cfg.get("import_dir")
                if stage_dir is None:
                    stage_dir = global_cfg.get("stage_dir")
                send_dir = export_dir
                recv_dir = import_dir

        snaps_only = (
            True
            if getattr(cli, "snaps_only", False)
            else raw.get("snaps_only", global_cfg.get("snaps_only", False))
        )
        # Resolve rsync with precedence: CLI > archive-specific > global
        if cli_rsync:
            archive_rsync = True
        else:
            archive_rsync = bool(raw.get("rsync", global_cfg.get("rsync", False)))
        if cli_rsync_opts:
            archive_rsync_opts = cli_rsync_opts
        else:
            archive_rsync_opts = raw.get("rsync_opts", global_cfg.get("rsync_opts"))
        if snaps_only and (
            send_file or recv_file or stage_file or send_dir or recv_dir or stage_dir
        ):
            print(
                f"Error in archive '{name}': snaps_only is mutually exclusive "
                f"with send/receive/stage file and directory options",
                file=sys.stderr,
            )
            sys.exit(1)

        # Resolve remote_host/remote_path/remote_sudo with precedence: CLI > archive-specific > global
        if cli_remote_host is not None:
            remote_host = cli_remote_host
        else:
            remote_host = raw.get("remote_host", global_cfg.get("remote_host"))
        
        if cli_remote_path is not None:
            remote_path = cli_remote_path
        else:
            remote_path = raw.get("remote_path", global_cfg.get("remote_path"))
        
        if cli_remote_sudo:
            remote_sudo = True
        else:
            remote_sudo = raw.get("remote_sudo", global_cfg.get("remote_sudo", False))

        # Validate: if remote_path is set, remote_host must also be set (from any level)
        if remote_path and not remote_host:
            print(f"Error in archive '{name}': 'remote_path' requires 'remote_host' to be set (global, per-archive, or CLI)", file=sys.stderr)
            sys.exit(1)

        # Validate external tool availability when they'll be needed.
        if archive_rsync and remote_path and shutil.which("rsync") is None:
            print(f"Error in archive '{name}': rsync is enabled but rsync was not found on the system PATH", file=sys.stderr)
            sys.exit(1)
        if remote_host and shutil.which("ssh") is None:
            print(f"Error in archive '{name}': remote_host is set but ssh was not found on the system PATH", file=sys.stderr)
            sys.exit(1)
        
        # Resolve backup_dir with precedence: CLI > archive-specific > global
        if cli_backup_dir is not None:
            backup_dir = cli_backup_dir
        else:
            backup_dir = raw.get("backup_dir", global_cfg.get("backup_dir"))

        entry = {
                    "name": name,
                    "subvolume": str(subvol_path),
                    "snaps_only": snaps_only,
                    "export_file": send_file,
                    "import_file": recv_file,
                    "stage_file": stage_file,
                    "export_dir": export_dir or send_dir,
                    "import_dir": import_dir or recv_dir,
                    "stage_dir": stage_dir,
                    "remote_host": remote_host,
                    "remote_path": remote_path,
                    "remote_sudo": remote_sudo,
                    "backup_dir": backup_dir,
                    "pre_snapshot_hook": raw.get("pre_snapshot_hook"),
                    "post_snapshot_hook": raw.get("post_snapshot_hook"),
                    "post_backup_hook": raw.get("post_backup_hook"),
                    "keep": resolve_keep_policy(raw, global_cfg, cli_keeps),
                    "week_startday": raw.get("week_startday", global_cfg.get("week_startday", "sunday")),
                    "forced_keep": _merge_keep(raw.get("forced_keep"), getattr(cli, "forced_keep", None)),
                    "rsync": archive_rsync,
                    "rsync_opts": archive_rsync_opts,
                }
        resolved.append(entry)

    return global_cfg, resolved

# ---------------------------------------------------------------------------
# Functions that use btrfs commands
# ---------------------------------------------------------------------------
def btrfs_cmd(cfg: dict, *args: str, sudo: bool | None = None) -> list[str]:
    cmd = ["btrfs"]
    use_sudo = sudo if sudo is not None else cfg.get("local_sudo", False)
    if use_sudo:
        cmd = ["sudo", "-n"] + cmd
    return cmd + list(args)


# Core validation: cache check → log L1 → execute → log L2 / error exit.
# Each public wrapper builds its command and delegates here.
def _validate_subvol_path(
    cache_key: str,
    location: str,
    success_msg: str,
    verbose: int,
    cfg: dict,
    cmd: list[str],
    *,
    is_ssh: bool = False,
    timeout: int | None = None,
    **kwargs,
) -> None:
    """Execute *cmd* and verify it succeeds (exit 0).

    Caching lives in ``cfg["_validated_paths"]`` so callers only supply the
    unique cache key, not the dedup logic.
    """
    validated = cfg.setdefault("_validated_paths", set())
    if cache_key in validated:
        return

    log(f"Checking {location} is a valid btrfs subvolume...", 1, verbose)

    try:
        run(cmd, dry_run=False, verbosity=verbose, timeout=timeout, **kwargs)
        log(success_msg, 2, verbose)
        validated.add(cache_key)
    except subprocess.CalledProcessError as exc:
        print(
            f"Error: '{location}' is not a btrfs subvolume "
            "(required for snapshot/backup/receive operations)",
            file=sys.stderr,
        )
        if exc.stderr and is_ssh:
            print(f"  SSH error: {exc.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f"Error: validation of '{location}' timed out", file=sys.stderr)
        sys.exit(1)


# makes sure the dir is a subvolume, errors out if not
def chk_btrfs_subvolume(dir: Path, cfg: dict):
    # Cache: avoid redundant btrfs subvolume show for the same path across
    # multiple archives in a single run (e.g. shared snapshot_dir/backup_dir).
    cache_key = f"local:{dir}"
    cmd = btrfs_cmd(cfg, "subvolume", "show", str(dir))
    _validate_subvol_path(
        cache_key,
        str(dir),
        f"\t{dir} is a valid btrfs subvolume",
        cfg["verbose"],
        cfg,
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

def chk_btrfs_subvolume_ssh(remote: str, remote_dir: str, cfg: dict, remote_sudo: bool = False):
    """Check if a remote directory is a btrfs subvolume via SSH."""
    cache_key = f"ssh:{remote}:{remote_dir}"
    sudo_prefix = ["sudo", "-n"] if remote_sudo else []
    ssh_cmd = ["ssh", remote] + sudo_prefix + ["btrfs", "subvolume", "show", remote_dir]
    _validate_subvol_path(
        cache_key,
        f"{remote}:{remote_dir}",
        f"\t{remote}:{remote_dir} is a valid btrfs subvolume",
        cfg["verbose"],
        cfg,
        ssh_cmd,
        is_ssh=True,
        timeout=30,
        capture_output=True,
        text=True,
    )

def create_snapshot(archive: str, subvol: str, snap_dir: Path, cfg: dict) -> Path:
    name = f"{archive}.{timestamp()}"
    dest = snap_dir / name
    cmd = btrfs_cmd(cfg, "subvolume", "snapshot", "-r", subvol, str(dest))
    try:
        run(cmd, 
            dry_run=cfg.get("dry_run", False),
            verbosity=cfg["verbose"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        print(f"Error executing `{cmd}`", file=sys.stderr)
        sys.exit(1)

    return dest

def get_subvol_uuids(path: Path, cfg: dict) -> tuple[str | None, str | None]:
    """
    Returns (UUID, Received UUID) for a btrfs subvolume.
    Returns (None, None) on failure.
    """
    try:
        result = run(btrfs_cmd(cfg, "subvolume", "show", str(path)),
                     dry_run=False, # this allows code to be executed properly during a dry-run
                     verbosity=cfg["verbose"],
                     check=True,  
                     capture_output=True, 
                     text=True)
        uuid = None
        received_uuid = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("UUID:"):
                uuid = line.split(":", 1)[1].strip()
            elif line.startswith("Received UUID:"):
                val = line.split(":", 1)[1].strip()
                received_uuid = None if val == "-" else val

        # Debug level only
        log(f"{path}: UUID={uuid}  Received UUID={received_uuid}", 3, cfg["verbose"])
        return uuid, received_uuid
    except subprocess.CalledProcessError:
        log(f"Failed to get UUIDs for {path}", 3, cfg["verbose"])
        return None, None


def get_subvol_uuids_ssh(remote: str, remote_path: str, cfg: dict, remote_sudo: bool = False) -> tuple[str | None, str | None]:
    """
    Returns (UUID, Received UUID) for a btrfs subvolume on a remote host via SSH.
    Returns (None, None) on failure.
    """
    cmd = build_ssh_cmd(remote, btrfs_cmd(cfg, "subvolume", "show", remote_path, sudo=False), remote_sudo)
    try:
        result = run(cmd, dry_run=False, verbosity=cfg["verbose"],
                     check=True, capture_output=True, text=True)
        uuid = None
        received_uuid = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("UUID:"):
                uuid = line.split(":", 1)[1].strip()
            elif line.startswith("Received UUID:"):
                val = line.split(":", 1)[1].strip()
                received_uuid = None if val == "-" else val

        log(f"{remote}:{remote_path}: UUID={uuid}  Received UUID={received_uuid}", 3, cfg["verbose"])
        return uuid, received_uuid
    except subprocess.CalledProcessError:
        log(f"Failed to get UUIDs for {remote}:{remote_path}", 3, cfg["verbose"])
        return None, None


def find_parents(archive: str, snapshot_dir: Path, backup_dir: Path, cfg: dict) -> list[Path]:
    """
    Find previous snapshots in snapshot_dir that have already been
    successfully received to backup_dir (matched by UUID ↔ Received UUID).
    Returns them newest-first.
    """
    log(f"Finding parent subvolumes for {archive}", 1, cfg["verbose"])
    # Collect snapshots
    snapshots = []
    for p in snapshot_dir.glob(f"{archive}.*"):
        if not p.is_dir():
            continue
        uuid, _ = get_subvol_uuids(p, cfg)
        if uuid:
            ts = parse_ts(p.name)
            if ts:
                snapshots.append((ts, p, uuid))

    # Collect received UUIDs from backup side
    received_uuids = set()
    for p in backup_dir.glob(f"{archive}.*"):
        if not p.is_dir():
            continue
        _, received_uuid = get_subvol_uuids(p, cfg)
        if received_uuid:
            received_uuids.add(received_uuid)

    # Match
    parents = []
    for ts, path, uuid in snapshots:
        if uuid in received_uuids:
            parents.append((ts, path))
            log(f"Usable parent: {path.name}", 3, cfg["verbose"])

    parents.sort(key=lambda x: x[0], reverse=True)
    result = [path for _, path in parents]

    if result:
        log(f"Found {len(result)} usable parent(s) for {archive}", 3, cfg["verbose"])
    else:
        log("No usable parents found", 3, cfg["verbose"])

    return result


def find_parents_ssh(archive: str, snapshot_dir: Path, remote: str, remote_dir: str, cfg: dict, remote_sudo: bool = False) -> list[Path]:
    """
    Find previous snapshots in snapshot_dir that have already been
    successfully received to remote_dir via SSH (matched by UUID ↔ Received UUID).
    Returns them newest-first.
    """
    log(f"Finding parent subvolumes for {archive} on SSH remote {remote}:{remote_dir}", 1, cfg["verbose"])
    # Collect snapshots
    snapshots = []
    for p in snapshot_dir.glob(f"{archive}.*"):
        if not p.is_dir():
            continue
        uuid, _ = get_subvol_uuids(p, cfg)
        if uuid:
            ts = parse_ts(p.name)
            if ts:
                snapshots.append((ts, p, uuid))

    # Collect received UUIDs from remote backup side
    # Need to run `btrfs subvolume show` on each subvolume to get Received UUID
    received_uuids = set()
    cmd = build_ssh_cmd(remote, btrfs_cmd(cfg, "subvolume", "list", remote_dir, sudo=False), remote_sudo)
    try:
        # Always execute read-only listing during dry-run
        result = run(cmd,
                     dry_run=False,
                     verbosity=cfg["verbose"],
                     check=True, capture_output=True, text=True)
        if result is None:
            return []
        for line in result.stdout.splitlines():
            # Parse: ID 256 gen 1234 top level 5 path lama7.20260831...
            # Extract the path (subvolume name)
            parts = line.split()
            path_idx = -1
            for i, part in enumerate(parts):
                if part == "path" and i + 1 < len(parts):
                    path_idx = i + 1
                    break
            if path_idx >= 0:
                remote_subvol = parts[path_idx]
                # Only process subvolumes matching our archive pattern
                if remote_subvol.startswith(f"{archive}."):
                    # Run btrfs subvolume show on this specific subvolume to get Received UUID
                    show_cmd = build_ssh_cmd(remote, btrfs_cmd(cfg, "subvolume", "show", f"{remote_dir}/{remote_subvol}", sudo=False), remote_sudo)
                    try:
                        # Always execute read-only show during dry-run
                        show_result = run(show_cmd,
                                          dry_run=False,
                                          verbosity=cfg["verbose"],
                                          check=True, capture_output=True, text=True)
                        if show_result is None:
                            continue
                        # Parse output for Received UUID
                        for show_line in show_result.stdout.splitlines():
                            show_line = show_line.strip()
                            if show_line.startswith("Received UUID:"):
                                received_uuid = show_line.split(":", 1)[1].strip()
                                if received_uuid and received_uuid != "-":
                                    received_uuids.add(received_uuid)
                                break
                    except subprocess.CalledProcessError:
                        log(f"Failed to show subvolume {remote_subvol} on {remote}:{remote_dir}", 3, cfg["verbose"])
    except subprocess.CalledProcessError:
        log(f"Failed to list subvolumes on {remote}:{remote_dir}", 3, cfg["verbose"])

    # Match
    parents = []
    for ts, path, uuid in snapshots:
        if uuid in received_uuids:
            parents.append((ts, path))
            log(f"Usable parent (SSH): {path.name}", 3, cfg["verbose"])

    parents.sort(key=lambda x: x[0], reverse=True)
    result = [path for _, path in parents]

    if result:
        log(f"Found {len(result)} usable parent(s) for {archive} on SSH remote", 3, cfg["verbose"])
    else:
        log("No usable parents found on SSH remote", 3, cfg["verbose"])

    return result


# ---------------------------------------------------------------------------
# SSH Helpers
# ---------------------------------------------------------------------------
def build_ssh_cmd(remote: str, remote_cmd: list[str], remote_sudo: bool = False) -> list[str]:
    """Build an SSH command to run remote_cmd on the remote host."""
    sudo_prefix = ["sudo", "-n"] if remote_sudo else []
    return ["ssh", remote] + sudo_prefix + remote_cmd


def build_ssh_receive_cmd(remote: str, remote_dir: str, cfg: dict, remote_sudo: bool = False) -> list[str]:
    """Build the remote btrfs receive command via SSH."""
    recv_cmd = btrfs_cmd(cfg, "receive", remote_dir, sudo=False)
    return build_ssh_cmd(remote, recv_cmd, remote_sudo)


def send_backup(snap: Path, snapshot_dir: Path, backup_dir: Path | None, cfg: dict, archive: str, archive_cfg: dict | None = None) -> Path:
    # Check if SSH remote is configured
    remote = archive_cfg.get("remote_host") if archive_cfg else cfg.get("remote_host")
    remote_dir = archive_cfg.get("remote_path") if archive_cfg else cfg.get("remote_path")
    remote_sudo = archive_cfg.get("remote_sudo") if archive_cfg else cfg.get("remote_sudo", False)

    dest = backup_dir / snap.name if backup_dir else Path(snap.name)

    has_local = backup_dir is not None
    has_remote = remote and remote_dir

    if not has_local and not has_remote:
        log("No backup destination configured (no backup_dir or remote/remote_dir)", 1, cfg["verbose"])
        return dest

    if has_local:
        start_time = datetime.now()
        local_parents = find_parents(archive, snapshot_dir, backup_dir, cfg)
        log(f"Found {len(local_parents)} local parent(s) for {archive}", 1, cfg["verbose"])

        local_send_cmd = btrfs_cmd(cfg, "send")
        for i, p in enumerate(local_parents):
            local_send_cmd += ["-p" if i == 0 else "-c", str(p)]
        local_send_cmd.append(str(snap))
        recv_cmd = btrfs_cmd(cfg, "receive", str(backup_dir))

        log("Sending to local backup...", 1, cfg["verbose"])
        rc, stderr = piped_run(local_send_cmd, recv_cmd, dry_run=cfg.get("dry_run", False), verbosity=cfg["verbose"])
        elapsed = (datetime.now() - start_time).total_seconds()
        log(f"btrfs send/receive completed in {format_duration(elapsed)}", 1, cfg["verbose"])
        if rc != 0:
            raise RuntimeError(f"btrfs receive (local) failed: {stderr}")

    if has_remote:
        start_time = datetime.now()
        ssh_parents = find_parents_ssh(archive, snapshot_dir, remote, remote_dir, cfg, remote_sudo)
        log(f"Found {len(ssh_parents)} SSH parent(s) for {archive}", 1, cfg["verbose"])

        ssh_send_cmd = btrfs_cmd(cfg, "send")
        for i, p in enumerate(ssh_parents):
            ssh_send_cmd += ["-p" if i == 0 else "-c", str(p)]
        ssh_send_cmd.append(str(snap))
        ssh_recv = build_ssh_receive_cmd(remote, remote_dir, cfg, remote_sudo)

        log(f"Sending to SSH remote {remote}...", 1, cfg["verbose"])
        rc, stderr = piped_run(ssh_send_cmd, ssh_recv, dry_run=cfg.get("dry_run", False), verbosity=cfg["verbose"])
        elapsed = (datetime.now() - start_time).total_seconds()
        log(f"btrfs send/receive (SSH) completed in {format_duration(elapsed)}", 1, cfg["verbose"])
        if rc != 0:
            raise RuntimeError(f"btrfs receive via SSH failed: {stderr}")

    return dest


def send_backup_tofile(snap: Path, snapshot_dir: Path, backup_dir: Path | None,
                       stream_file: Path, cfg: dict, archive: str, archive_cfg: dict | None = None) -> Path:
    """
    btrfs send -f <stream_file>.  If stream_file already exists, it is
    removed first (overwritten).  Writes the stream to a local file.
    Remote transfer and receive are handled separately by process_archive
    via receive_stream/_scp_and_receive. This supports the export_file-only
    configuration (send to file, no receive back), as well as the
    export_file + remote case (SCP file to remote).
    """
    # Check if SSH remote is configured
    remote = archive_cfg.get("remote_host") if archive_cfg else cfg.get("remote_host")
    remote_dir = archive_cfg.get("remote_path") if archive_cfg else cfg.get("remote_path")
    remote_sudo = archive_cfg.get("remote_sudo") if archive_cfg else cfg.get("remote_sudo", False)

    # Check if this is a staged file operation (stage_file sets send_file=recv_file)
    is_staged = archive_cfg.get("stage_file") is not None if archive_cfg else False

    has_local = backup_dir is not None
    has_remote = remote and remote_dir

    # When only stream_file is configured (export_file-only, no backup_dir
    # or remote), create a full send stream with no parents.

    # Find parents for the stream file.
    # When sending to a remote destination (via SCP + receive), parents must
    # be those already received on the remote, not on the local backup_dir.
    if has_remote:
        parents = find_parents_ssh(archive, snapshot_dir, remote, remote_dir, cfg, remote_sudo)
        log(f"Found {len(parents)} SSH parent(s) for {archive}", 1, cfg["verbose"])
    elif has_local:
        parents = find_parents(archive, snapshot_dir, backup_dir, cfg)
        log(f"Found {len(parents)} local parent(s) for {archive}", 1, cfg["verbose"])
    else:
        parents = []

    # Build send command for the stream file
    local_send_cmd = btrfs_cmd(cfg, "send")
    for i, p in enumerate(parents):
        local_send_cmd += ["-p" if i == 0 else "-c", str(p)]
    local_send_cmd += ["-f", str(stream_file), str(snap)]

    if stream_file.exists():
        log(f"Overwriting existing stream file: {stream_file}", 1, cfg["verbose"])
        if not cfg["dry_run"]:
            stream_file.unlink()

    # Send to file. Receiving (if needed) is handled separately by process_archive
    # via receive_stream or _scp_and_receive. This supports:
    # - export_file-only (send to file, no receive back)
    # - export_file + remote (SCP file to remote for receive)
    # - stage_file (send to file, receive happens separately)
    label = "Sending to staged file..." if is_staged else "Sending to file..."
    log(label, 1, cfg["verbose"])
    result = _timed_run(local_send_cmd, "btrfs send to file", cfg,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if result is None:
        return stream_file
    if result.returncode != 0:
        raise RuntimeError("btrfs send to file failed")

    return stream_file

def _scp_and_receive(stream: Path, remote: str, remote_dir: str, cfg: dict, remote_sudo: bool = False) -> str | None:
    """Copy stream file to remote via SCP, then receive locally on remote."""
    # SCP the stream file to a temporary location on remote
    remote_tmp = f"/tmp/bubtrsnap-{stream.name}"
    
    scp_cmd = ["scp", str(stream), f"{remote}:{remote_tmp}"]
    if cfg.get("local_sudo"):
        scp_cmd = ["sudo", "-n"] + scp_cmd
    recv_cmd = build_ssh_receive_cmd(remote, remote_dir, cfg, remote_sudo)
    recv_cmd = recv_cmd[:-1] + ["-f", remote_tmp, remote_dir]

    # Overall timing for the complete SCP + SSH receive sequence
    seq_start = datetime.now()

    log(f"Copying stream to remote via SCP...", 1, cfg["verbose"])
    # Use run helper for SCP so it respects dry-run and logging
    scp_start = datetime.now()
    result = run(scp_cmd, dry_run=cfg.get("dry_run", False), verbosity=cfg["verbose"], capture_output=True, text=True, check=False)
    scp_elapsed = (datetime.now() - scp_start).total_seconds()
    if result is not None:
        log(f"SCP transfer completed in {format_duration(scp_elapsed)}", 1, cfg["verbose"])
    if result is not None and result.returncode != 0:
        seq_elapsed = (datetime.now() - seq_start).total_seconds()
        log(f"SCP + receive sequence failed after {format_duration(seq_elapsed)}", 1, cfg["verbose"])
        print(f"Error copying stream to remote via SCP:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    # Now receive on remote using the copied file
    start = datetime.now()

    ssh_result = run(recv_cmd, dry_run=cfg.get("dry_run", False),
                     verbosity=cfg["verbose"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    
    elapsed = (datetime.now() - start).total_seconds()
    if ssh_result is not None:
        log(f"btrfs receive via SSH completed in {format_duration(elapsed)}", 1, cfg["verbose"])
    seq_elapsed = (datetime.now() - seq_start).total_seconds()
    log(f"SCP + receive sequence completed in {format_duration(seq_elapsed)}", 1, cfg["verbose"])
    stderr = (ssh_result.stderr if ssh_result else "") or ""
    stderr = stderr.strip()

    # Clean up remote temp file
    cleanup_cmd = build_ssh_cmd(remote, ["rm", "-f", remote_tmp], remote_sudo)
    run(cleanup_cmd, dry_run=cfg.get("dry_run", False),
        verbosity=cfg["verbose"],
        capture_output=True, check=False)

    if ssh_result is not None and ssh_result.returncode != 0:
        if "already exists" in stderr.lower():
            print(f"Warning: subvolume from '{stream.name}' already exists in "
                  f"{remote_dir} – skipping", file=sys.stderr)
            return None
        print(f"Error receiving '{stream}' via SSH:\n{stderr}", file=sys.stderr)
        sys.exit(1)
    
    # Extract subvolume name reported by btrfs
    subvol_name = None
    for line in stderr.splitlines():
        line = line.strip()
        if "At subvol" in line or "Receiving subvol" in line:
            parts = line.split()
            for i, tok in enumerate(parts):
                if tok in ("subvol", "subvolume") and i + 1 < len(parts):
                    subvol_name = parts[i + 1].strip("'\"")
                    break
            if subvol_name:
                break
    
    if not subvol_name:
        subvol_name = stream.stem
    
    log(f"Received subvolume: {subvol_name}", 1, cfg["verbose"])
    return subvol_name

def _check_interrupted_rsync(remote: str, cfg: dict) -> bool:
    """
    Check if a previous rsync transfer was interrupted by looking for
    the --partial-dir on the remote.
    """
    remote_partial_dir = "/tmp/.bubtrsnap-partial"
    check_cmd = ["ssh", remote, "test", "-d", remote_partial_dir]

    result = run(check_cmd,
                  dry_run=False,  # always execute — read-only check, should run even during dry-run
                  verbosity=cfg["verbose"],
                  capture_output=True, text=True, check=False)

    if result is not None and result.returncode == 0:
        return True
    return False


def _rsync_and_receive(stream: Path, remote: str, remote_dir: str, cfg: dict,
                       remote_sudo: bool = False, rsync_opts: str | None = None) -> str | None:
    """
    Copy stream file to remote via rsync (with --partial-dir for recovery),
    then receive locally on remote via SSH.

    Uses --partial-dir=DIR for interrupted transfer recovery. If a partial
    transfer is detected (a partial-dir exists from a previous interrupted run),
    rsync completes the transfer before the receive step proceeds.

    Returns the received subvolume basename on success, None on 'already exists'.
    Aborts the whole program on any other error.
    """
    # The remote temp file that rsync will place (same as SCP path for consistency)
    remote_tmp = f"/tmp/bubtrsnap-{stream.name}"
    partial_dir_name = ".bubtrsnap-partial"
    remote_partial_dir = f"/tmp/{partial_dir_name}"

    # Build the rsync command
    rsync_cmd = ["rsync", "-a", "--partial-dir", partial_dir_name]

    # Add user-specified options (filtered through blocklist)
    user_opts = _filter_rsync_opts(rsync_opts)
    rsync_cmd += user_opts

    # rsync source -> dest. rsync uses host:path syntax (same as scp)
    if cfg.get("local_sudo"):
        rsync_cmd = ["sudo", "-n"] + rsync_cmd
    rsync_cmd += [str(stream), f"{remote}:{remote_tmp}"]

    # Receive command (same as SCP path)
    recv_cmd = build_ssh_receive_cmd(remote, remote_dir, cfg, remote_sudo)
    recv_cmd = recv_cmd[:-1] + ["-f", remote_tmp, remote_dir]

    # Overall timing for the complete rsync + SSH receive sequence
    seq_start = datetime.now()

    # rsync with --partial-dir will resume any interrupted transfer automatically
    # (the partial-dir check is done earlier in process_archive via
    # _check_interrupted_rsync, before the stream file is created/reused)
    log("Copying stream to remote via rsync...", 1, cfg["verbose"])

    # Execute rsync
    rsync_start = datetime.now()
    result = run(rsync_cmd, dry_run=cfg.get("dry_run", False),
                 verbosity=cfg["verbose"], capture_output=True, text=True, check=False)
    rsync_elapsed = (datetime.now() - rsync_start).total_seconds()
    if result is not None:
        log(f"rsync transfer completed in {format_duration(rsync_elapsed)}", 1, cfg["verbose"])
    if result is not None and result.returncode != 0:
        seq_elapsed = (datetime.now() - seq_start).total_seconds()
        log(f"rsync + receive sequence failed after {format_duration(seq_elapsed)}", 1, cfg["verbose"])
        print(f"Error copying stream to remote via rsync:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    # Now receive on remote using the transferred file
    start = datetime.now()
    ssh_result = run(recv_cmd, dry_run=cfg.get("dry_run", False),
                     verbosity=cfg["verbose"],
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    elapsed = (datetime.now() - start).total_seconds()
    if ssh_result is not None:
        log(f"btrfs receive via SSH completed in {format_duration(elapsed)}", 1, cfg["verbose"])
    seq_elapsed = (datetime.now() - seq_start).total_seconds()
    log(f"rsync + receive sequence completed in {format_duration(seq_elapsed)}", 1, cfg["verbose"])
    stderr = (ssh_result.stderr if ssh_result else "") or ""
    stderr = stderr.strip()

    # Clean up remote temp file
    cleanup_cmd = build_ssh_cmd(remote, ["rm", "-f", remote_tmp], remote_sudo)
    run(cleanup_cmd, dry_run=cfg.get("dry_run", False),
        verbosity=cfg["verbose"],
        capture_output=True, check=False)

    if ssh_result is not None and ssh_result.returncode != 0:
        if "already exists" in stderr.lower():
            print(f"Warning: subvolume from '{stream.name}' already exists in "
                  f"{remote_dir} – skipping", file=sys.stderr)
            return None
        print(f"Error receiving '{stream}' via SSH:\n{stderr}", file=sys.stderr)
        sys.exit(1)

    # Extract subvolume name reported by btrfs
    subvol_name = None
    for line in stderr.splitlines():
        line = line.strip()
        if "At subvol" in line or "Receiving subvol" in line:
            parts = line.split()
            for i, tok in enumerate(parts):
                if tok in ("subvol", "subvolume") and i + 1 < len(parts):
                    subvol_name = parts[i + 1].strip("'\"")
                    break
            if subvol_name:
                break

    if not subvol_name:
        subvol_name = stream.stem

    log(f"Received subvolume: {subvol_name}", 1, cfg["verbose"])
    return subvol_name


def _receive_local(stream: Path, backup_dir: Path, cfg: dict) -> str | None:
    """Execute `btrfs receive -f <stream> <backup_dir>` locally.
    Returns the received subvolume basename on success, None on 'already exists'.
    Aborts the whole program on any other error.
    """
    recv_cmd = btrfs_cmd(cfg, "receive", "-f", str(stream), str(backup_dir))

    result = _timed_run(recv_cmd, "btrfs receive", cfg,
                        capture_output=True, text=True, check=False)

    if result is None:
        return stream.stem

    stderr = (result.stderr or "").strip()

    if result.returncode != 0:
        if "already exists" in stderr.lower():
            print(f"Warning: subvolume from '{stream.name}' already exists in "
                  f"{backup_dir} – skipping", file=sys.stderr)
            return None
        print(f"Error receiving '{stream}' locally:\n{stderr}", file=sys.stderr)
        sys.exit(1)

    return stream.stem


def _receive_remote(stream: Path, remote: str, remote_dir: str, cfg: dict,
                    remote_sudo: bool = False,
                    use_rsync: bool = False, rsync_opts: str | None = None) -> str | None:
    """
    Copy stream file to remote, then receive locally on remote.

    Uses rsync (with --partial-dir for interrupted transfer recovery) when
    ``use_rsync`` is True, otherwise falls back to SCP.
    Returns the received subvolume basename on success, None on 'already exists'.
    Aborts the whole program on any other error.
    """
    if use_rsync:
        return _rsync_and_receive(stream, remote, remote_dir, cfg, remote_sudo, rsync_opts)
    return _scp_and_receive(stream, remote, remote_dir, cfg, remote_sudo)


def receive_stream(stream: Path, backup_dir: Path | None, cfg: dict,
                   remote: str | None = None, remote_dir: str | None = None,
                   remote_sudo: bool = False,
                   use_rsync: bool = False, rsync_opts: str | None = None) -> str | None:
    """
    Dispatch to `_receive_local` and/or `_receive_remote` based on which
    destinations are configured.  When both are set, both are invoked.
    Returns the received subvolume basename on success, None on 'already exists'.
    Aborts the whole program on any other error.
    """
    received = None
    if backup_dir:
        received = _receive_local(stream, backup_dir, cfg)
    if remote and remote_dir:
        remote_received = _receive_remote(stream, remote, remote_dir, cfg, remote_sudo,
                                          use_rsync, rsync_opts)
        # Prefer the local result if it succeeded, else use remote result
        if not received:
            received = remote_received
    if received is None and not backup_dir and not (remote and remote_dir):
        print("Error: receive_stream called without backup_dir or remote/remote_dir",
              file=sys.stderr)
        sys.exit(1)
    return received

def archive_name_from_received(name: str) -> str:
    """Extract archive portion from conventional {archive}.{timestamp} name."""
    if "." in name:
        return name.rsplit(".", 1)[0]
    return name

def run_hook(cmd_template: str | None, mapping: dict[str, str], cfg: dict) -> None:
    if not cmd_template:
        log(f"\tNo hook to process", 1, cfg["verbose"])
        return
    cmd = cmd_template
    for key, val in mapping.items():
        cmd = cmd.replace(f"{{{key}}}", val)
    log(f"\tRunning hook: {cmd}", 1, cfg["verbose"])
    if not cfg.get("dry_run"):
        run(cmd, dry_run=False, verbosity=cfg["verbose"], shell=True, check=False)

# ---------------------------------------------------------------------------
# Keep Policy- (simple Borg-inspired, ported from btrbu
#  The implementation walks backwards using interval boundaries to help
#  determine what gets kept after a particular keep interval is completed.
#  The hourly boundary is the previous hour, the day is 23:59 of the previous 
#  day, the week is Saturday 11:59:59, the month is the last day of the month
#  the year is 12/31@23:59:59.
# ---------------------------------------------------------------------------
#
# keep policy helpers
_KEEP_REASON = {
    "h": "hourly",
    "d": "daily",
    "w": "weekly",
    "m": "monthly",
    "y": "yearly",
    "1": "newest",
}

def _ts_str(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M")

def _parse_ts_str(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y%m%d%H%M")

def _end_of_hour(dt: datetime) -> datetime:
    return dt.replace(minute=59, second=0, microsecond=0)

def _end_of_day(dt: datetime) -> datetime:
    return dt.replace(hour=23, minute=59, second=0, microsecond=0)

def _prev_hour(ts: str, hrs_cnt: int) -> str:
    t = _parse_ts_str(ts) - timedelta(hours=hrs_cnt)
    return _ts_str(_end_of_hour(t))

def _prev_day(ts: str, days_cnt: int) -> str:
    t = _parse_ts_str(ts) - timedelta(days=days_cnt)
    return _ts_str(_end_of_day(t))

def _prev_week(ts: str, week_cnt: int, week_start_day: int = 6) -> str:
    """Most recent week-end day at or before ts, then week_cnt weeks further back.
    
    week_start_day: 0=Monday ... 6=Sunday (the day the week STARTS).
    The week END day is (week_start_day - 1) % 7.
    """
    t = _parse_ts_str(ts)
    # Week end day = day before week start day
    week_end_day = (week_start_day - 1) % 7
    days_since_end = (t.weekday() - week_end_day) % 7
    if days_since_end != 0:
        t = t - timedelta(days=days_since_end)
    t = t - timedelta(weeks=week_cnt)
    return _ts_str(_end_of_day(t))

def _last_day_of_month(year: int, month: int) -> datetime:
    last = calendar.monthrange(year, month)[1]
    return datetime(year, month, last, 23, 59, 0, 0)

def _prev_month(ts: str, months_cnt: int) -> str:
    """Last day of the target month (23:59), matching btrbu semantics."""
    t = _parse_ts_str(ts)
    last_this = _last_day_of_month(t.year, t.month)

    if months_cnt != 0:
        y, m = t.year, t.month - months_cnt
        while m <= 0:
            m += 12
            y -= 1
        return _ts_str(_last_day_of_month(y, m))

    if t.day != last_this.day:
        # not last day of month → last day of previous month
        if t.month == 1:
            return _ts_str(_last_day_of_month(t.year - 1, 12))
        return _ts_str(_last_day_of_month(t.year, t.month - 1))

    return _ts_str(last_this)

def _prev_year(ts: str, years_cnt: int) -> str:
    t = _parse_ts_str(ts)
    if years_cnt != 0:
        return _ts_str(datetime(t.year - years_cnt, 12, 31, 23, 59, 0, 0))
    if t.month != 12 or t.day != 31:
        return _ts_str(datetime(t.year - 1, 12, 31, 23, 59, 0, 0))
    return _ts_str(datetime(t.year, 12, 31, 23, 59, 0, 0))

_ARCHIVE_TS_RE = re.compile(r"^(.+)\.(\d{12})$")
def iter_archive_items(directory: Path, archive: str):
    """Yield (timestamp_str, path) for exact archive.YYYYMMDDHHMM entries."""
    if not directory.is_dir():
        return
    for p in directory.iterdir():
        if not p.is_dir() and not p.is_symlink():
            # snapshots/backups are subvolumes (dirs); skip plain files
            # remove this guard if you also prune stream files later
            continue
        m = _ARCHIVE_TS_RE.match(p.name)
        if not m:
            continue
        if m.group(1) != archive:
            continue
        ts = m.group(2)
        # validate it's a real datetime
        if parse_ts(p.name) is None:
            continue
        yield ts, p


def iter_archive_items_ssh(remote: str, remote_dir: str, archive: str, cfg: dict, remote_sudo: bool = False):
    """
    Yield (timestamp_str, remote_subvol_path) for exact archive.YYYYMMDDHHMM entries
    on a remote SSH host. Mirrors iter_archive_items but queries via SSH.
    """
    # Build SSH command to list subvolumes
    cmd = build_ssh_cmd(remote, btrfs_cmd(cfg, "subvolume", "list", remote_dir, sudo=False), remote_sudo)
    try:
        # Always execute read-only listing during dry-run
        result = run(cmd,
                     dry_run=False,
                     verbosity=cfg["verbose"],
                     check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError:
        log(f"Failed to list subvolumes on {remote}:{remote_dir}", 3, cfg["verbose"])
        return

    if result is None:
        return

    for line in result.stdout.splitlines():
        # Parse: ID 256 gen 1234 top level 5 path lama7.20260831...
        parts = line.split()
        path_idx = -1
        for i, part in enumerate(parts):
            if part == "path" and i + 1 < len(parts):
                path_idx = i + 1
                break
        if path_idx >= 0:
            remote_subvol = parts[path_idx]
            # Match archive pattern
            m = _ARCHIVE_TS_RE.match(remote_subvol)
            if not m:
                continue
            if m.group(1) != archive:
                continue
            ts = m.group(2)
            if parse_ts(remote_subvol) is None:
                continue
            # Yield timestamp and full remote path
            yield ts, f"{remote_dir}/{remote_subvol}"

def _get_keeps(
    timestamps: list[str],
    keep_tbl: dict[str, str],
    max_keeps: int,
    ts_start: str,
    interval_label: str,
    interval_f,
) -> str | None:
    """
    Walk timestamps (ascending) from the newest entry <= ts_start backward,
    keeping one per interval.  Returns the oldest timestamp kept in this pass.
    """
    i = len(timestamps) - 1
    while i >= 0 and timestamps[i] > ts_start:
        i -= 1
    if i < 0:
        return None

    keep_cnt = 0
    oldest_keep = None
    while i >= 0:
        if timestamps[i] not in keep_tbl:
            keep_tbl[timestamps[i]] = interval_label
            oldest_keep = timestamps[i]
            keep_cnt += 1
            if keep_cnt == max_keeps:
                return oldest_keep
        tsref = interval_f(timestamps[i], 1)
        i -= 1
        while i >= 0 and timestamps[i] > tsref:
            i -= 1
    return oldest_keep

def build_keep_set(ts_list: list[str], keep: dict, week_start_day: int = 6) -> dict[str, str]:
    """
    Port of btrbu bldKeepList (table-driven interval loop).

    ts_list: sorted ascending list of 'YYYYMMDDHHMM' strings.
    Returns dict { timestamp_str: reason } for timestamps to keep.

    When every keep_* is 0, returns {newest: "d"} for pure-function callers;
    apply_keep_policy skips pruning entirely when all counts are 0.
    """
    counts = {
        "h": keep.get("keep_hourly", 0),
        "d": keep.get("keep_daily", 0),
        "w": keep.get("keep_weekly", 0),
        "m": keep.get("keep_monthly", 0),
        "y": keep.get("keep_yearly", 0),
    }
    total = sum(counts.values())

    if not ts_list:
        return {}

    keeps: dict[str, str] = {}
    newest = ts_list[-1]

    if total == 0:
        keeps[newest] = "d"
        return keeps

    # Always keep the most recent (reason filled by first active interval)
    keeps[newest] = "1"

    if total == 1:
        for label, n in counts.items():
            if n:
                keeps[newest] = label
                break
        return keeps

    # (label, count, step_fn, start_fn(newest, oldest_keep) -> ts_start)
    # start_fn encodes btrbu's per-interval seed boundary.
    intervals = [
        (
            "h",
            counts["h"],
            _prev_hour,
            lambda newest, _old: _prev_hour(newest, 1),
        ),
        (
            "d",
            counts["d"],
            _prev_day,
            lambda newest, _old: _prev_day(newest, 1),
        ),
        (
            "w",
            counts["w"],
            lambda ts, cnt: _prev_week(ts, cnt, week_start_day),
            lambda newest, old: _prev_week(_prev_day(old or newest, 1), 0, week_start_day),
        ),
        (
            "m",
            counts["m"],
            _prev_month,
            lambda newest, old: _prev_month(_prev_day(old or newest, 1), 0),
        ),
        (
            "y",
            counts["y"],
            _prev_year,
            lambda newest, old: _prev_year(_prev_day(old or newest, 1), 0),
        ),
    ]

    oldest_keep: str | None = None
    prior_total = 0  # sum of counts for shorter intervals already considered

    for label, count, step_fn, start_fn in intervals:
        if count <= 0:
            continue

        if keeps.get(newest) == "1":
            keeps[newest] = label

        # Newest consumes one slot of the shortest active interval only
        n = count
        if prior_total == 0:
            n = count - 1

        if n <= 0:
            oldest_keep = newest
            prior_total += count
            continue

        # Hourly: btrbu always treats newest as the first hourly when hourly > 0
        if label == "h":
            if count == 1:
                oldest_keep = newest
                prior_total += count
                continue
            n = count - 1

        _prev_oldest = oldest_keep
        oldest_keep = _get_keeps(
            ts_list,
            keeps,
            n,
            start_fn(newest, oldest_keep),
            label,
            step_fn,
        )
        # Boundary fallback for w/m/y: when old (oldest keep of prior
        # interval) is on the interval boundary day (week-end / month-end
        # / year-end), the start_fn _prev_X(_prev_day(old,1),0,...) overshoots
        # to the PREVIOUS boundary, skipping archives in the same period.
        # Retry with _prev_day(old, 1) — the day before the oldest keep —
        # which lets _get_keeps find the archive that crosses into the
        # prior boundary period.
        if oldest_keep is None and label in ("w", "m", "y") and _prev_oldest is not None:
            oldest_keep = _get_keeps(
                ts_list,
                keeps,
                n,
                _prev_day(_prev_oldest, 1),
                label,
                step_fn,
            )
        prior_total += count

    return keeps

def _apply_keep_policy_core(
    archive: str,
    keep: dict,
    cfg: dict,
    location: str,
    items: list[tuple[str, str, list[str]]],
    no_policy_msg: str,
    no_items_msg: str,
    forced_keep: list[str] | None = None,
) -> None:
    """Shared keep-policy engine used by apply_keep_policy and apply_keep_policy_ssh.

    *items* is a pre-materialised list of (timestamp, display_name, delete_cmd)
    triples produced by the caller (who knows how to enumerate its destination).

    *forced_keep* is the archive-specific list of timestamps to preserve; it is
    passed explicitly rather than read from *cfg* so per-archive values do not
    leak across archives in the same run.
    """
    keep_values = [
        keep.get("keep_hourly", 0),
        keep.get("keep_daily", 0),
        keep.get("keep_weekly", 0),
        keep.get("keep_monthly", 0),
        keep.get("keep_yearly", 0),
    ]
    if not any(v > 0 for v in keep_values):
        log(no_policy_msg, 1, cfg["verbose"])
        return

    log(f"Keep policy for {archive} in {location}: {format_keep_policy(keep)}",
        1, cfg["verbose"])
    log(f"Applying to {archive} in {location}:", 1, cfg["verbose"])

    if not items:
        log(f"\t{no_items_msg}", 1, cfg["verbose"])
        return

    # ascending timestamp order (btrbu style)
    items.sort(key=lambda x: x[0])
    ts_list = [t for t, _, _ in items]

    # Parse week_startday from config (shared — both local and SSH honour it)
    week_start_day = _DAY_TO_WEEKDAY.get(
        cfg.get("week_startday", "sunday").lower(), 6)

    # Handle forced_keep: validate timestamps exist for this archive
    forced_keep_list = forced_keep or []
    if forced_keep_list:
        valid_forced_keep = []
        for ts in forced_keep_list:
            if ts not in ts_list:
                log(f"Warning: --forced-keep timestamp '{ts}' not found for archive '{archive}' – ignoring", 1, cfg["verbose"])
            else:
                valid_forced_keep.append(ts)
        forced_keep_list = valid_forced_keep

    # Apply forced keeps first — these are guaranteed to be kept
    keeps = {}
    for ts in forced_keep_list:
        keeps[ts] = "forced"

    # Now apply keep policy to remaining timestamps
    policy_keeps = build_keep_set(ts_list, keep, week_start_day)
    # Add policy keeps, but don't overwrite forced ones
    for ts, reason in policy_keeps.items():
        if ts not in keeps:
            keeps[ts] = reason

    for ts, display_name, delete_cmd in items:
        if ts in keeps:
            reason = _KEEP_REASON.get(keeps[ts], keeps[ts])
            log(f"\tKeeping {display_name} ({reason})", 1, cfg["verbose"])
        else:
            log(f"\tPruning {display_name}", 1, cfg["verbose"])
            try:
                run(delete_cmd,
                    dry_run=cfg.get("dry_run", False),
                    verbosity=cfg["verbose"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True)
            except subprocess.CalledProcessError:
                print(f"Error during prune: `{delete_cmd}`", file=sys.stderr)
                sys.exit(1)

def apply_keep_policy_ssh(remote: str, remote_dir: str, archive: str, keep: dict, cfg: dict, remote_sudo: bool = False, forced_keep: list[str] | None = None) -> None:
    """Apply the given keep policy to backups of an archive on a remote SSH host.

    Mirrors apply_keep_policy but operates via SSH.  Delegates the shared
    algorithm to _apply_keep_policy_core so fixes are applied in one place.
    """
    raw_items = list(iter_archive_items_ssh(remote, remote_dir, archive, cfg, remote_sudo))
    items = [
        (ts, remote_path,
         build_ssh_cmd(remote,
                       btrfs_cmd(cfg, "subvolume", "delete", remote_path, sudo=False),
                       remote_sudo))
        for ts, remote_path in raw_items
    ]
    _apply_keep_policy_core(
        archive, keep, cfg,
        f"{remote}:{remote_dir}",
        items,
        f"No keep policy specified for {archive} on SSH remote – skipping pruning",
        f"No prior archives for {archive} found in {remote}:{remote_dir}",
        forced_keep,
    )

def apply_keep_policy(directory: Path, archive: str, keep: dict, cfg: dict, forced_keep: list[str] | None = None) -> None:
    """Apply the given keep policy to snapshots or backups of an archive.

    Uses the btrbu algorithm (non-overlapping interval slots from newest
    backward).  Delegates the shared algorithm to _apply_keep_policy_core so
    fixes are applied in one place.
    """
    if not directory.is_dir():
        log(
            f"Directory '{directory}' does not exist – skipping keep policy for {archive}",
            1,
            cfg["verbose"],
        )
        return

    raw_items = list(iter_archive_items(directory, archive))
    items = [
        (ts, p.name, btrfs_cmd(cfg, "subvolume", "delete", str(p)))
        for ts, p in raw_items
    ]
    _apply_keep_policy_core(
        archive, keep, cfg,
        str(directory),
        items,
        f"No keep policy specified for {archive} – skipping pruning (default: leave existing snapshots/backups untouched)",
        f"No prior archives for {archive} found in {directory}",
        forced_keep,
    )

#-----------------------------------------------------------------
# Does all archive related actions
#-----------------------------------------------------------------
def _validate_destinations(
    archive: dict, cfg: dict, backup_dir: Path | None, remote: str | None,
    remote_dir: str | None, remote_sudo: bool,
) -> None:
    """Check that backup_dir and/or remote/remote_dir are valid btrfs subvolumes."""
    if backup_dir:
        chk_btrfs_subvolume(backup_dir, cfg)
    if remote and remote_dir:
        chk_btrfs_subvolume_ssh(remote, remote_dir, cfg, remote_sudo)


def _build_hook_context(
    archive_name: str, snap: Path | None, received: str | None,
    backup_dir: Path | None, remote: str | None, remote_dir: str | None,
    cfg: dict,
) -> dict:
    """Build the variable-substitution mapping for a post_backup_hook."""
    if received and backup_dir:
        backup = str(backup_dir / received)
    elif received and remote and remote_dir:
        backup = f"{remote}:{remote_dir}/{received}"
    elif received:
        backup = received
    else:
        backup = ""
    return {
        "backup": backup,
        "snapshot": str(snap) if snap else "",
        "archive": archive_name,
        "timestamp": timestamp(),
        "snapshotdir": str(cfg.get("snapshot_dir", "")) if cfg.get("snapshot_dir") else "",
        "backupdir": str(backup_dir) if backup_dir else "",
    }


def _apply_all_keep_policies(
    snap_dir: Path | None, backup_dir: Path | None, remote: str | None,
    remote_dir: str | None, remote_sudo: bool, name: str, keep: dict, cfg: dict,
    forced_keep: list[str] | None = None,
) -> None:
    """Apply keep policy to snap_dir, backup_dir, and remote in one place."""
    if snap_dir:
        apply_keep_policy(snap_dir, name, keep, cfg, forced_keep)
    if backup_dir:
        apply_keep_policy(backup_dir, name, keep, cfg, forced_keep)
    if remote and remote_dir:
        apply_keep_policy_ssh(remote, remote_dir, name, keep, cfg, remote_sudo, forced_keep)


def _piped_send_to_local(snap: Path, snap_dir: Path, backup_dir: Path, cfg: dict, name: str) -> None:
    """Perform a local piped btrfs send -> btrfs receive to backup_dir with timing."""
    log("Finding parents for local piped send...", 1, cfg["verbose"])
    local_send_cmd = btrfs_cmd(cfg, "send")
    local_parents = find_parents(name, snap_dir, backup_dir, cfg)
    log(f"Found {len(local_parents)} local parent(s) for {name}", 1, cfg["verbose"])
    for i, p in enumerate(local_parents):
        local_send_cmd += ["-p" if i == 0 else "-c", str(p)]
    local_send_cmd.append(str(snap))
    recv_cmd = btrfs_cmd(cfg, "receive", str(backup_dir))

    log("Sending to local backup...", 1, cfg["verbose"])
    start_time = datetime.now()
    rc, stderr = piped_run(local_send_cmd, recv_cmd,
                           dry_run=cfg.get("dry_run", False), verbosity=cfg["verbose"])
    elapsed = (datetime.now() - start_time).total_seconds()
    log(f"btrfs send/receive completed in {format_duration(elapsed)}", 1, cfg["verbose"])
    if rc != 0:
        raise RuntimeError(f"btrfs receive (local) failed: {stderr}")


def _receive_and_post(received: str | None, archive: dict, name: str, snap: Path | None,
                      backup_dir: Path | None, remote: str | None, remote_dir: str | None,
                      snap_dir: Path | None, keep: dict, cfg: dict,
                      remote_sudo: bool = False,
                      forced_keep: list[str] | None = None) -> None:
    """After a receive, run post-backup hook and all keep policies."""
    if received:
        run_hook(archive.get("post_backup_hook"),
                 _build_hook_context(name, snap, received, backup_dir, remote, remote_dir, cfg), cfg)
        _apply_all_keep_policies(snap_dir or None, backup_dir, remote, remote_dir, remote_sudo,
                                 name, keep, cfg, forced_keep)


def process_archive(archive: dict, cfg: dict) -> str | None:
    """
    Normal processing for a single archive:
    pre-hook -> snapshot -> post-snapshot-hook -> backup -> post-backup-hook -> keep policy
    If just export_file:
    pre-hook -> snapshot -> post-snapshot-hook -> send backup to file -> keep for snapshots
    If just import_file:
    backup receive from file -> post-backup-hook -> keep for backups
    If both export_file and import_file then same as Normal above except the backup uses a file
    as a staging step.
    Processing for export_dir and import_dir uses same processing flow except it will
    handle multiple archives versus a single archive for the file variant
    """
    name = archive["name"]
    subvol = archive["subvolume"]
    keep = archive["keep"]
    forced_keep_list = archive.get("forced_keep", [])
    remote_sudo = archive.get("remote_sudo", False)
    use_rsync = archive.get("rsync", False)
    archive_rsync_opts = archive.get("rsync_opts")

    log(f"=== Processing archive: {name} ({subvol}) ===", 1, cfg["verbose"])

    # Resolve file/dir options; stage_file is sugar for send+recv on same path,
    # stage_dir is sugar for export_dir + import_dir on same dir.
    send_file = archive.get("export_file") or archive.get("stage_file")
    recv_file = archive.get("import_file") or archive.get("stage_file")
    send_dir = archive.get("export_dir")
    recv_dir = archive.get("import_dir")
    stage_dir = archive.get("stage_dir")
    snaps_only = archive.get("snaps_only", False)

    if stage_dir and not (send_file or recv_file):
        send_dir = stage_dir
        recv_dir = stage_dir

    # Resolve destination paths.
    archive_backup_dir = archive.get("backup_dir")
    backup_dir = (
        Path(archive_backup_dir) if archive_backup_dir
        else (Path(cfg["backup_dir"]) if cfg.get("backup_dir") else None)
    )
    snap_dir = Path(cfg["snapshot_dir"]) if cfg.get("snapshot_dir") else None
    remote = archive.get("remote_host")
    remote_dir = archive.get("remote_path")
    staged = bool(archive.get("stage_file") or stage_dir)

    # Validate all archive paths early — fail fast before any mutation.
    # Caching inside chk_btrfs_subvolume/chk_btrfs_subvolume_ssh avoids
    # redundant validation when the same path is checked across archives
    # (e.g. a shared snapshot_dir or backup_dir).
    chk_btrfs_subvolume(Path(subvol), cfg)
    if snap_dir:
        chk_btrfs_subvolume(snap_dir, cfg)
    _validate_destinations(archive, cfg, backup_dir, remote, remote_dir, remote_sudo)

    # ---- receive-only (file) ----
    if recv_file and not send_file:
        if not backup_dir and not (remote and remote_dir):
            print(f"Error: archive '{name}' import_file requires backup_dir or remote_dir (with remote)",
                  file=sys.stderr)
            sys.exit(1)
        p = Path(recv_file)
        if not p.is_file() or not is_btrfs_stream(p, cfg):
            print(f"Error: '{p}' is not a valid btrfs stream file", file=sys.stderr)
            sys.exit(1)
        received = receive_stream(p, backup_dir, cfg, remote, remote_dir, remote_sudo,
                                  use_rsync, archive_rsync_opts)
        _receive_and_post(received, archive, name, None, backup_dir, remote, remote_dir,
                          snap_dir, keep, cfg, remote_sudo, forced_keep=forced_keep_list)
        log(f"Finished archive: {name}\n", 1, cfg["verbose"])
        return

    # ---- receive-only (dir) ----
    if recv_dir and not send_dir and not send_file:
        if not backup_dir and not (remote and remote_dir):
            print("Error: import_dir requires backup_dir or remote_dir (with remote)", file=sys.stderr)
            sys.exit(1)
        stream = find_newest_stream_for_archive(Path(recv_dir), name, cfg)
        if not stream:
            log(f"No matching stream for archive '{name}' in {recv_dir}", 1, cfg["verbose"])
            log(f"Finished archive: {name}\n", 1, cfg["verbose"])
            return
        log(f"Using stream {stream} for archive {name}", 1, cfg["verbose"])
        received = receive_stream(stream, backup_dir, cfg, remote, remote_dir, remote_sudo,
                                  use_rsync, archive_rsync_opts)
        _receive_and_post(received, archive, name, None, backup_dir, remote, remote_dir,
                          snap_dir, keep, cfg, remote_sudo, forced_keep=forced_keep_list)
        log(f"Finished archive: {name}\n", 1, cfg["verbose"])
        return

    # ---- paths that create a snapshot ----
    if not snap_dir:
        print("Error: snapshot_dir is required for snapshot/send operations",
              file=sys.stderr)
        sys.exit(1)

    # Check for interrupted rsync transfer before taking a snapshot
    # so we can reuse the existing stream file instead of creating a new one
    interrupted_rsync = False
    interrupted_stream: Path | None = None
    if use_rsync and remote and remote_dir:
        log(f"Checking for interrupted rsync transfer to {remote}...", 1, cfg["verbose"])
        interrupted_rsync = _check_interrupted_rsync(remote, cfg)
        if interrupted_rsync:
            # Determine which stream file was being transferred
            if send_file:
                interrupted_stream = Path(send_file)
            elif send_dir:
                interrupted_stream = find_newest_stream_for_archive(Path(send_dir), name, cfg)
            if interrupted_stream and interrupted_stream.is_file():
                log(f"Found interrupted rsync transfer with stream file: {interrupted_stream}", 1, cfg["verbose"])
            else:
                log(f"Partial-dir found but no matching stream file; proceeding with new transfer", 1, cfg["verbose"])
                interrupted_rsync = False

    if interrupted_rsync and interrupted_stream and interrupted_stream.is_file():
        log(f"Resuming interrupted rsync transfer (stream file already exists)", 1, cfg["verbose"])
        snap = interrupted_stream  # use the existing stream file as 'snap' placeholder
        post_hook_snap_name = interrupted_stream.stem
    else:
        log("Running pre-snapshot hook...", 1, cfg["verbose"])
        run_hook(archive.get("pre_snapshot_hook"), {
            "archive": name, "subvol": subvol, "timestamp": timestamp(),
            "snapshotdir": str(snap_dir),
            "backupdir": str(backup_dir) if backup_dir else "",
        }, cfg)

        log("Creating snapshot...", 1, cfg["verbose"])
        snap = create_snapshot(name, subvol, snap_dir, cfg)
        post_hook_snap_name = snap.name

        log("Running post-snapshot hook...", 1, cfg["verbose"])
        run_hook(archive.get("post_snapshot_hook"), {
            "snapshot": str(snap), "archive": name, "timestamp": timestamp(),
            "snapshotdir": str(snap_dir),
            "backupdir": str(backup_dir) if backup_dir else "",
        }, cfg)

    stream_path: Path | None = None
    dest: Path | None = None
    received: str | None = None

    if send_file:
        stream_path = Path(send_file)
        if not interrupted_rsync:
            send_backup_tofile(snap, snap_dir, backup_dir, stream_path, cfg, name, archive)
        # When export_file is set with a local backup_dir and no import_file,
        # also do a normal piped send|receive to the local backup_dir (in
        # addition to any file-based remote transfer).
        if backup_dir and not recv_file:
            if not interrupted_rsync:
                _piped_send_to_local(snap, snap_dir, backup_dir, cfg, name)
        dest = backup_dir / snap.name if backup_dir else stream_path
    elif send_dir:
        chk_is_dir(send_dir, "export-dir" if not staged else "stage-dir")
        stream_path = Path(send_dir) / f"{post_hook_snap_name}.btrfs"
        if not interrupted_rsync:
            send_backup_tofile(snap, snap_dir, backup_dir, stream_path, cfg, name, archive)
        # When export_dir is set with a local backup_dir and no import_dir,
        # also do a normal piped send|receive to the local backup_dir (in
        # addition to any file-based remote transfer). Mirrors the
        # _piped_send_to_local block for export_file.
        if backup_dir and not recv_file and not recv_dir:
            if not interrupted_rsync:
                _piped_send_to_local(snap, snap_dir, backup_dir, cfg, name)
        dest = backup_dir / snap.name if backup_dir else stream_path
    elif not snaps_only and (backup_dir or (remote and remote_dir)):
        log("Starting backup...", 1, cfg["verbose"])

        # Space check before writing the backup (portable du/df-style).
        # Hard-stop (non-zero exit) if the estimate won't fit; warn when tight.
        if backup_dir:
            needed = _size_of(snap)
            if not _space_check(backup_dir, needed, "backup", cfg):
                raise SystemExit(1)

        if (send_file or send_dir) and remote and remote_dir:
            if not interrupted_rsync:
                dest = send_backup_tofile(snap, snap_dir, backup_dir, stream_path, cfg, name, archive)
            else:
                dest = stream_path
        else:
            dest = send_backup(snap, snap_dir, backup_dir, cfg, name, archive)

        run_hook(archive.get("post_backup_hook"),
                 _build_hook_context(name, snap, dest, backup_dir, remote, remote_dir, cfg), cfg)
        _apply_all_keep_policies(snap_dir, backup_dir, remote, remote_dir, remote_sudo, name, keep, cfg, forced_keep=forced_keep_list)
        log(f"Finished archive: {name}\n", 1, cfg["verbose"])
        return

    # snaps_only or no backup destination
    if snaps_only or not (backup_dir or (remote and remote_dir)):
        _apply_all_keep_policies(snap_dir, None, None, None, False, name, keep, cfg, forced_keep=forced_keep_list)
        log(f"Finished archive: {name}\n", 1, cfg["verbose"])
        return

    # ---- receive after export-file/export-dir when import_* or staging is set,
    # OR when export_file/export_dir is used with remote/remote_dir (auto-receive to SSH) ----
    auto_ssh_receive = (
        (send_file or send_dir)
        and remote
        and remote_dir
        and not recv_file
        and not recv_dir
        and not staged
    )
    do_receive = bool(
        stream_path
        and (recv_file or recv_dir or staged or auto_ssh_receive)
    )
    if do_receive:
        if not backup_dir and not (remote and remote_dir):
            print("Error: receive requires backup_dir or remote_dir (with remote)", file=sys.stderr)
            sys.exit(1)

        if staged:
            recv_path = stream_path
        elif recv_file:
            recv_path = Path(recv_file)
        elif recv_dir:
            if recv_dir == send_dir:
                recv_path = stream_path
            else:
                recv_path = find_newest_stream_for_archive(Path(recv_dir), name, cfg)
                if not recv_path:
                    log(f"No matching stream for archive '{name}' in {recv_dir}", 1, cfg["verbose"])
                    log(f"Finished archive: {name}\n", 1, cfg["verbose"])
                    return
        elif auto_ssh_receive:
            recv_path = stream_path
        else:
            print("Should never get here...", file=sys.stderr)
            sys.exit(1)

        # Non-staged: import_file may differ from export_file
        if recv_file and send_file and not staged:
            recv_path = Path(recv_file)

        # When both backup_dir AND remote are targets in a stage/import/export
        # flow (auto_ssh_receive is False), the single exported stream carries
        # parents matched only to whichever side was checked first by
        # send_backup_tofile().  Using that same stream for local receive risks
        # mismatched incrementals where histories diverge.  Route backup_dir
        # through an independent piped send (fresh parents from
        # find_parents(backup_dir)) and let receive_stream handle only the remote.
        # Guard against duplicate runs (auto_ssh_receive fires above, skipping
        # this block because the send_file branch already ran piped send) and
        # against interrupted-rsync recovery (snap points to a .btrfs file).
        if backup_dir and remote and remote_dir and snap_dir and not interrupted_rsync and not auto_ssh_receive:
            log("Finding parents for local backup directory...", 1, cfg["verbose"])
            _piped_send_to_local(snap, snap_dir, backup_dir, cfg, name)
            recv_dest_dir = None
        else:
            # auto_ssh_receive: send_file branch already piped-sent to backup_dir,
            # so receive_stream must not re-receive locally.  Otherwise (pure local
            # import/export, no remote) receive_stream is the only path that
            # populates backup_dir, so it must receive locally.
            recv_dest_dir = None if auto_ssh_receive else backup_dir
        received = receive_stream(recv_path, recv_dest_dir, cfg, remote, remote_dir, remote_sudo,
                                  use_rsync, archive_rsync_opts)
        # snap_dir keep policy is handled by the final apply_keep_policy call
        _receive_and_post(received, archive, name, snap, backup_dir, remote, remote_dir,
                          None, keep, cfg, remote_sudo, forced_keep=forced_keep_list)

        if staged and stream_path is not None:
            remove_stage_file(stream_path, cfg)

    apply_keep_policy(snap_dir, name, keep, cfg, forced_keep_list)
    log(f"Finished archive: {name}\n", 1, cfg["verbose"])
    if interrupted_rsync:
        log(f"Archive {name} recovered from interrupted rsync; will re-process for complete backup", 1, cfg["verbose"])
        return "RECOVERED"



# ---------------------------------------------------------------------------
# Maintenance mode: argparse subcommand CLI (backup / list / prune / rebuild)
#
# The legacy invocation `bubtrsnap <archive>...` (no subcommand) is preserved
# as the implicit `backup` default, so every existing invocation keeps working
# unchanged.  Because argparse subparsers consume the first positional token as
# the subcommand name (which would break the legacy `bubtrsnap home=/home`
# form), a pre-dispatch function scans argv to find the first positional token;
# if it is a known subcommand we parse with that sub-parser (dropping only the
# command token), otherwise we fall through to the legacy backup parser with
# all positionals treated as archives.
# ---------------------------------------------------------------------------

MAINTENANCE_SUBCOMMANDS = ("backup", "list", "prune", "rebuild")

# Options that consume the following token as a VALUE when scanning argv.
_MAINT_VALUE_TAKERS = {
    "--config", "--snapshot-dir", "--backup-dir", "--snaps-only",
    "--export-file", "--import-file", "--export-dir", "--import-dir",
    "--stage-file", "--stage-dir",
    "--remote-host", "--remote-path", "--remote-sudo", "--local-sudo",
    "--rsync", "--rsync-opts",
    "--keep-hourly", "--keep-daily", "--keep-weekly",
    "--keep-monthly", "--keep-yearly", "--week-startday",
    "--forced-keep", "--ts",
}
# Short flags that take NO value.
_MAINT_NO_VALUE_SHORT = {"-h"}
# Flags that take an OPTIONAL value (-v / --verbose).
_MAINT_OPT_VTAKERS = {"-v", "--verbose"}


def _maint_first_positional(tokens) -> int | None:
    """Return the index of the first positional token in `tokens` (argv[1:])
    that names a known subcommand, otherwise None.

    Skips options and their values.  An optional-value flag (-v/--verbose)
    consumes the following token only when that token is present and is not
    itself a flag or subcommand — mirroring argparse's own parsing, so legacy
    forms such as `bubtrsnap -v 2 list` and `bubtrsnap -v2 home=/home` resolve
    as they do today.
    """
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "--":
            nxt = tokens[i + 1] if i + 1 < n else None
            return None if nxt is None else (i + 1 if nxt in MAINTENANCE_SUBCOMMANDS else None)
        if tok.startswith("--"):
            name = tok.split("=", 1)[0]
            # --opt=value is one token; --opt takes the next token as its value.
            if name == "--version" or "=" in tok:
                i += 1
            elif name in _MAINT_VALUE_TAKERS:
                i += 2
            else:
                i += 1
        elif tok.startswith("-"):
            if tok in _MAINT_NO_VALUE_SHORT:
                i += 1
            elif tok in _MAINT_OPT_VTAKERS:
                if i + 1 < n:
                    nxt = tokens[i + 1]
                    if not nxt.startswith("-") and nxt not in MAINTENANCE_SUBCOMMANDS:
                        i += 2
                    else:
                        i += 1
                else:
                    i += 1
            else:
                i += 1
        else:
            return i if tok in MAINTENANCE_SUBCOMMANDS else None
    return None


def _add_maint_global_options(p: argparse.ArgumentParser) -> None:
    """Register the options shared by every sub-parser.  These mirror the
    legacy main() definitions verbatim (types, metavar, help, defaults)."""
    p.add_argument("--version", action="version", version=f"bubtrsnap {VERSION}")
    p.add_argument("--config", type=Path,
        help="Path to TOML configuration file (default: ~/.config/bubtrsnap.toml)")
    p.add_argument("--snapshot-dir", type=str,
        help="Directory where read-only snapshots will be created")
    p.add_argument("--backup-dir", type=str,
        help="Directory where backups will be sent via btrfs send/receive")
    p.add_argument("--snaps-only", action="store_true",
        help="Only create snapshots; skip the backup step")
    p.add_argument("--export-file", type=str, metavar="FILE",
        help="Send stream to FILE (archive-specific). Exactly one archive required. "
             "Mutually exclusive with --snaps-only and the *-dir options.")
    p.add_argument("--import-file", type=str, metavar="FILE",
        help="Receive stream from FILE (archive-specific). Exactly one archive required. "
             "If used with --export-file, both must name the same file.")
    p.add_argument("--export-dir", type=str, metavar="DIR",
        help="Write per-archive streams into DIR as {archive}.{timestamp}.btrfs. "
             "Global; applies to CLI archives or all config archives.")
    p.add_argument("--import-dir", type=str, metavar="DIR",
        help="Receive newest matching stream for each archive from DIR.")
    p.add_argument("--stage-file", type=str, metavar="FILE",
        help="Stage via FILE: btrfs send -f FILE then receive -f FILE, then delete FILE. "
             "Exactly one archive. Mutually exclusive with other export/import/stage options "
             "and with --snaps-only.")
    p.add_argument("--stage-dir", type=str, metavar="DIR",
        help="Stage via DIR: write {archive}.{timestamp}.btrfs, receive it, then delete it. "
             "Applies to listed or all archives. Mutually exclusive with other export/import/stage "
             "options and with --snaps-only.")
    p.add_argument("--remote-host", type=str, metavar="USER@HOST",
        help="SSH remote host in user@host format for sending backups over SSH. "
             "Can be set globally or per-archive in config file.")
    p.add_argument("--remote-path", type=str, metavar="DIR",
        help="Target directory on the SSH remote for receiving backups. "
             "Can be set globally or per-archive in config file.")
    p.add_argument("--remote-sudo", action="store_true",
        help="Run btrfs commands on SSH remote with 'sudo -n'. "
             "Can be set globally or per-archive in config file.")
    p.add_argument("--local-sudo", action="store_true",
        help="Run local btrfs commands with 'sudo -n'")
    p.add_argument("--rsync", action="store_true",
        help="Use rsync instead of scp for stream file transfer to SSH remote. "
             "Provides partial-transfer recovery via --partial-dir. "
             "Can be set globally or per-archive in config file. "
             "Implies rsync uses -a and --partial-dir by default.")
    p.add_argument("--rsync-opts", type=str, metavar="OPTS",
        help="Additional rsync options (space-separated). Applies when --rsync is enabled. "
             "Blocked: --partial-dir, --delete, --backup, -v/--verbose, --progress, "
             "--stats, --dry-run, --daemon, --files-from, --bwlimit, --rsh, "
             "--rsync-path, --exclude, --include, --filter, and other potentially "
             "dangerous options. Blocked options are reported via stderr.")
    p.add_argument("--keep-hourly", type=int, metavar="N", help="Number of hourly snapshots/backups to retain")
    p.add_argument("--keep-daily", type=int, metavar="N", help="Number of daily snapshots/backups to retain")
    p.add_argument("--keep-weekly", type=int, metavar="N", help="Number of weekly snapshots/backups to retain")
    p.add_argument("--keep-monthly", type=int, metavar="N", help="Number of monthly snapshots/backups to retain")
    p.add_argument("--keep-yearly", type=int, metavar="N", help="Number of yearly snapshots/backups to retain")
    p.add_argument("--week-startday", type=str, metavar="DAY",
        help="Day that starts the week: monday..sunday (default: sunday). Affects keep policy weekly boundary.")
    p.add_argument("--forced-keep", type=str, metavar="TIMESTAMP", action="append",
        help="Force keep of a specific timestamp for the archive (format: YYYYMMDDhhmm). "
             "Can be specified multiple times. Comma-separated list also accepted. "
             "Must match an existing snapshot/backup for the archive. Archive-specific only.")
    p.add_argument("-v", "--verbose", type=int, nargs="?", const=1, default=0, metavar="LEVEL",
        help="Verbosity level: 0=quiet (default), 1=progress, 2=commands. "
             "-v alone means level 1, --verbose 2 means level 2")
    p.add_argument("--debug", action="store_true", help="Enable debug output (highest level)")
    p.add_argument("--dry-run", action="store_true",
        help="Show what would be done without making changes (implies -vv)")


def _build_backup_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bubtrsnap",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=f"bubtrsnap v{VERSION} — btrfs snapshot & backup tool",
        epilog=(
            "Examples:\n"
            "  bubtrsnap --snapshot-dir=/pool/snapshots --backup-dir=/backup home=/home\n"
            "  bubtrsnap --config=~/.config/bubtrsnap.toml\n"
            "  bubtrsnap -v --snaps-only root=/\n"
            "  bubtrsnap backup --snapshot-dir=/pool/snap --backup-dir=/backup home=/home\n"
            "  bubtrsnap list home\n"
            "  bubtrsnap prune --ts 202601010000 home --yes\n"
            "  bubtrsnap rebuild home\n"
            "With no subcommand the legacy backup behaviour applies (all positionals "
            "are archive names); with a subcommand the command token is consumed and "
            "the remaining options/archives are parsed for that subcommand.\n"
        ),
    )
    _add_maint_global_options(p)
    p.add_argument("archives", nargs="*", metavar="ARCHIVE[=SUBVOLUME]",
        help="Archives to process. Can be just the archive name (looked up in the "
             "config file) or name=/path/to/subvolume. If none are given, all "
             "archives from the config file are processed.")
    return p


def _build_list_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bubtrsnap list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="List snapshots/backups present for the given archive(s). Read-only.",
    )
    _add_maint_global_options(p)
    p.add_argument("archives", nargs="*", metavar="ARCHIVE[=SUBVOLUME]",
        help="Archives to list (all config archives if none given). Can be just the "
             "archive name or name=/path/to/subvolume.")
    return p


def _build_prune_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bubtrsnap prune",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Delete snapshot/backup subvolumes matching the given timestamp(s) "
                    "or inclusive dotted range(s). Requires at least one archive.",
    )
    _add_maint_global_options(p)
    p.add_argument("--ts", action="append", type=str,
        help="Timestamp (YYYYMMDDhhmm) or dotted inclusive range '<from>..<to>' "
             "(e.g. 202601010000..202601050000) to delete. Bounds are inclusive. "
             "Specify multiple --ts to match any of them.")
    p.add_argument("archives", nargs="+", metavar="ARCHIVE[=SUBVOLUME]",
        help="Archives to prune. At least one required (prune never operates on "
             "'all' archives). Can be just the archive name or "
             "name=/path/to/subvolume.")
    p.add_argument("--yes", action="store_true",
        help="Confirm and perform deletions without an interactive prompt. "
             "Without --yes (and without --dry-run) the user is asked to confirm.")
    return p


def _build_rebuild_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bubtrsnap rebuild",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Reconcile snapshot/backup locations: restore a lost local snapshot "
                    "from a local backup (btrfs subvolume snapshot), restore a lost "
                    "local backup from a local snapshot (btrfs send/receive), and "
                    "re-send to a remote when it is missing. Never deletes; only restores.",
    )
    _add_maint_global_options(p)
    p.add_argument("archives", nargs="*", metavar="ARCHIVE[=SUBVOLUME]",
        help="Archives to rebuild (all config archives if none given). Can be just the "
             "archive name or name=/path/to/subvolume.")
    return p


_SUB_BUILDERS = {
    "backup": _build_backup_parser,
    "list": _build_list_parser,
    "prune": _build_prune_parser,
    "rebuild": _build_rebuild_parser,
}


# Top-level "menu" help shown for a bare `bubtrsnap --help` / `-h`.
# Printed before sub-dispatch so a no-subcommand -h gives an overview of all
# subcommands rather than (only) the legacy backup option list.
_TOP_LEVEL_HELP = f"""\
bubtrsnap v{VERSION} — btrfs snapshot & backup tool

Usage:
  bubtrsnap <subcommand> [options]
  bubtrsnap [options] [ARCHIVE[=SUBVOLUME] ...]   legacy backup (no subcommand)
                                           use 'bubtrsnap backup --help' for the option list

Subcommands:
  backup     Take snapshots and send backups.  Default when no subcommand is given.
             Full option list: 'bubtrsnap backup --help'.
  list       Read-only table of snapshot / backup / remote timestamps.
             Use 'bubtrsnap list --help' for options.
  prune      Delete snapshots/backups matching a timestamp (or a dotted inclusive
             range '<from>..<to>').  Requires at least one archive and --yes.
  rebuild    Reconcile snapshot/backup/remote locations: restore lost local
             snapshots/backups, and recover a snapshot lost only on the remote.
             Never deletes.

Global options are shared by every subcommand (e.g. --config, --snapshot-dir,
--backup-dir, -v/--verbose, --dry-run, --rsync, and the keep policies).
Run 'bubtrsnap <subcommand> --help' for that command's full option list.

Examples:
  bubtrsnap --config=~/.config/bubtrsnap.toml
  bubtrsnap backup --snapshot-dir=/snap --backup-dir=/bck home=/home
  bubtrsnap list home
  bubtrsnap prune --ts 202601010000..202601050000 home --yes
  bubtrsnap rebuild home
"""


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------

def _run_backup(args: argparse.Namespace) -> int:
    # various pre-checks for certain options and option combinations
    if args.snaps_only and (
        args.export_file or args.import_file or args.stage_file
        or args.export_dir or args.import_dir or args.stage_dir
    ):
        print("Error: --snaps-only is mutually exclusive with send/receive/stage "
              "file and directory options", file=sys.stderr)
        return 1
    if (args.export_file or args.import_file or args.stage_file) and (
        args.export_dir or args.import_dir or args.stage_dir
    ):
        print("Error: cannot mix file options with directory options", file=sys.stderr)
        return 1
    if args.stage_file and (
        args.export_file or args.import_file or args.stage_file
        or args.export_dir or args.import_dir or args.stage_dir
    ):
        print("Error: --stage-file is mutually exclusive with other send/receive/stage options",
              file=sys.stderr)
        return 1
    if args.stage_dir and (
        args.export_file or args.import_file or args.stage_file
        or args.export_dir or args.import_dir
    ):
        print("Error: --stage-dir is mutually exclusive with other send/receive/stage options",
              file=sys.stderr)
        return 1
    if (args.export_file or args.import_file) and (args.export_dir or args.import_dir):
        print("Error: cannot mix --export-file/--import-file with "
              "--export-dir/--import-dir", file=sys.stderr)
        return 1
    if (args.remote_host and not args.remote_path) or (args.remote_path and not args.remote_host):
        print("Error: --remote-host and --remote-path must be specified together",
              file=sys.stderr)
        return 1

    # Verbosity handling
    verbosity = args.verbose
    if args.debug:
        verbosity = 3
    if args.dry_run:
        verbosity = max(verbosity, 2)

    config_path = args.config or Path.home() / ".config" / "bubtrsnap.toml"

    try:
        global_cfg, archives = load_and_resolve_archives(
            args, config_path if config_path.exists() else None
        )
    except SystemExit:
        return 1

    global_cfg["verbose"] = verbosity
    global_cfg["dry_run"] = args.dry_run

    # see if we're doing a recv only operation....
    receive_only = all(
        (a.get("import_file") or a.get("import_dir"))
        and not a.get("export_file")
        and not a.get("export_dir")
        and not a.get("stage_file")
        and not a.get("stage_dir")
        and not a.get("snaps_only")
        for a in archives
    ) if archives else False

    # if not doing just a recv, then taking snapshots and snapshot_dir is needed
    if not receive_only and not global_cfg.get("snapshot_dir"):
        print("--snapshot-dir is required", file=sys.stderr)
        return 1

    if not archives:
        print("No archives to process.", file=sys.stderr)
        return 1

    # Process each archive fully
    recovered_archives = []
    for archive in archives:
        try:
            result = process_archive(archive, global_cfg)
            if result == "RECOVERED":
                recovered_archives.append(archive)
        except Exception as e:
            log(f"Error processing {archive['name']}: {e}", 1, global_cfg["verbose"])
            return 1

    # Re-process archives that were recovered from interrupted rsync transfers.
    if recovered_archives:
        log(f"Re-processing {len(recovered_archives)} recovered archive(s)...", 1, global_cfg["verbose"])
        for archive in recovered_archives:
            try:
                process_archive(archive, global_cfg)
            except Exception as e:
                log(f"Error re-processing {archive['name']}: {e}", 1, global_cfg["verbose"])
                return 1

    log("All archives processed.", 1, global_cfg["verbose"])
    return 0


def _run_maintenance(args: argparse.Namespace) -> int:
    cmd = getattr(args, "subcommand", "backup")
    if cmd not in ("list", "prune", "rebuild"):
        print(f"Error: unknown command {cmd!r}", file=sys.stderr)
        return 1
    verbosity = args.verbose
    if args.debug:
        verbosity = 3
    if args.dry_run:
        verbosity = max(verbosity, 2)

    config_path = args.config or Path.home() / ".config" / "bubtrsnap.toml"
    try:
        global_cfg, archives = load_and_resolve_archives(
            args, config_path if config_path.exists() else None
        )
    except SystemExit:
        return 1

    global_cfg["verbose"] = verbosity
    global_cfg["dry_run"] = args.dry_run
    if not archives:
        print("No archives to process.", file=sys.stderr)
        return 1

    if cmd == "list":
        return _run_list(archives, cfg=global_cfg, verbosity=verbosity)
    if cmd == "prune":
        return _run_prune(archives, cfg=global_cfg, verbosity=verbosity, args=args)
    if cmd == "rebuild":
        return _run_rebuild(archives, cfg=global_cfg, verbosity=verbosity)
    return 1


# ---------------------------------------------------------------------------
# Maintenance mode: real command bodies (list / prune / rebuild)
# ---------------------------------------------------------------------------
# Space estimation (portable du/df-style)

def _disk_free(path: Path) -> int:
    """Free bytes on the filesystem containing `path` (df-style)."""
    return shutil.disk_usage(str(path)).free


def _size_of(path: Path) -> int:
    """Approximate on-disk size of a directory (du-style, recursive)."""
    if not path.is_dir():
        return 0
    total = 0
    try:
        for p in path.iterdir():
            if p.is_dir() and not p.is_symlink():
                total += _size_of(p)
            elif p.is_file() and not p.is_symlink():
                total += p.stat().st_size
    except OSError:
        pass
    return total


def _human_size(nbytes: int) -> str:
    """Human-readable byte size (portable du-style)."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if nbytes < 1024 or unit == "PiB":
            return f"{nbytes:.0f} {unit}" if unit == "B" else f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.0f} B"


def _space_check(target: Path, needed: int, label: str, cfg: dict) -> bool:
    """Hard-stop (False) if `needed` won't fit in free space at `target`;
    otherwise warn when it would consume >=25% of free space; return True."""
    if needed <= 0:
        return True
    try:
        free = _disk_free(target)
    except OSError:
        free = 0
    if free <= 0:
        log(f"[space] cannot measure free space at {target}", 1, cfg["verbose"])
        return True
    if needed > free:
        log(f"[space] {label}: need {_human_size(needed)} but only {_human_size(free)} "
            f"free at {target} — will not fit. Aborting.", 1, cfg["verbose"])
        return False
    if needed / free >= 0.25:
        log(f"[space] {label}: need {_human_size(needed)} of {_human_size(free)} free "
            f"({needed / free * 100:.0f}%) — low space, proceeding.", 1, cfg["verbose"])
    return True


def _remote_disk_free(remote: str, remote_dir, cfg: dict, remote_sudo: bool = False) -> int | None:
    """Free bytes on the filesystem backing `remote_dir` on the SSH `remote`
    host, via 'df -P -B 1' (free space in bytes).  Returns None if the query
    could not be run or parsed (ssh not reachable, no df, or it timed out).
    Read-only."""
    try:
        sudo = ["sudo", "-n"] if remote_sudo else []
        cmd = ["ssh", remote] + sudo + ["df", "-P", "-B", "1", str(remote_dir)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        log(f"[space] remote df timed out on {remote}", 1, cfg["verbose"])
        return None
    except Exception as e:
        log(f"[space] could not query remote free space on {remote}: {e}", 1, cfg["verbose"])
        return None
    if proc.returncode != 0:
        return None
    # df -P -B 1 header: Filesystem  1-blocks  Used  Available  Use%  Mounted on
    # With -B 1 the columns are raw bytes (no unit suffix).  Avail/Available
    # is column 4 (index 3).
    for line in proc.stdout.splitlines():
        parts = line.split()
        if not parts or parts[0] == "Filesystem":
            continue
        if len(parts) >= 4:
            avail = parts[3]
            if avail.endswith("B"):
                avail = avail[:-1]
            try:
                return int(avail)
            except ValueError:
                continue
    return None



def _remote_size_of(remote: str, subvol_path: str, cfg: dict,
                    remote_sudo: bool = False) -> int | None:
    """Approximate on-disk size (bytes) of a subvolume on the SSH remote, via
    'du -sb'.  Returns None if the query could not be run or parsed (ssh not
    reachable, no du, or it timed out).  Read-only.  Consistent with the
    local _size_of() (both use logical file-size sums)."""
    try:
        sudo = ["sudo", "-n"] if remote_sudo else []
        cmd = ["ssh", remote] + sudo + ["du", "-sb", subvol_path]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        log(f"[space] remote du timed out on {remote}", 1, cfg["verbose"])
        return None
    except Exception as e:
        log(f"[space] could not measure remote subvolume size on {remote}: {e}", 1, cfg["verbose"])
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        try:
            return int(parts[0])
        except ValueError:
            continue
    return None


def _space_check_remote(remote, remote_dir, needed, label, cfg,
                        remote_sudo: bool = False, free: int | None = None) -> bool:
    """Guard for remote re-sends: return False if `needed` won't fit on
    `remote`, True otherwise (warning emitted when >=25% of free space would
    be used).  `free` may be pre-fetched to avoid a second SSH query.
    Returns True (proceed) if the remote's free space can't be measured,
    since we can't know it won't fit."""
    if needed <= 0:
        return True
    if free is None:
        free = _remote_disk_free(remote, remote_dir, cfg, remote_sudo)
    if free is None:
        log(f"[space] {label}: cannot measure free space on {remote}; "
            f"proceeding.", 1, cfg["verbose"])
        return True
    if free <= 0:
        log(f"[space] {label}: need {_human_size(needed)} but no free space "
            f"on {remote} — will not fit. Skipping remote part.",
            1, cfg["verbose"])
        return False
    if needed > free:
        log(f"[space] {label}: need {_human_size(needed)} but only "
            f"{_human_size(free)} free on {remote} — will not fit. "
            f"Skipping remote part.", 1, cfg["verbose"])
        return False
    if needed / free >= 0.25:
        log(f"[space] {label}: need {_human_size(needed)} of "
            f"{_human_size(free)} free ({needed / free * 100:.0f}%) — low "
            f"space, proceeding.", 1, cfg["verbose"])
    return True


def _ts_match(ts: str, spec: str) -> bool:
    """Match a 12-digit timestamp against a spec: exact, or dotted inclusive range."""
    if ".." in spec:
        a, b = spec.split("..", 1)
        a, b = a.strip(), b.strip()
        return len(a) == 12 and len(b) == 12 and a.isdigit() and b.isdigit() and a <= ts <= b
    return spec == ts


def _validate_ts_specs(specs: list[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in specs:
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if ".." in part:
                lo, hi = part.split("..", 1)
                lo, hi = lo.strip(), hi.strip()
                if not (len(lo) == 12 and lo.isdigit() and len(hi) == 12 and hi.isdigit()):
                    raise ValueError(f"bad timestamp range {part!r} (expected <YYYYMMDDhhmm>..<YYYYMMDDhhmm>)")
                if lo > hi:
                    raise ValueError(f"timestamp range {part!r} has from > to")
                cleaned.append(f"{lo}..{hi}")
            else:
                if not (len(part) == 12 and part.isdigit()):
                    raise ValueError(f"bad timestamp {part!r} (expected YYYYMMDDhhmm)")
                cleaned.append(part)
    if not cleaned:
        raise ValueError("no valid --ts value(s) given")
    return cleaned


def _iter_location(loc: dict, label: str, name: str, cfg: dict):
    """Yield (ts, path_or_remote_path) for an archive in one location."""
    if label == "remote":
        remote, remote_dir = loc["remote"], loc["remote_dir"]
        if remote and remote_dir:
            yield from iter_archive_items_ssh(remote, remote_dir, name, cfg, loc.get("remote_sudo", False))
    else:
        d = loc["snap_dir"] if label == "snap" else loc["backup_dir"]
        if d is not None:
            yield from iter_archive_items(d, name)


def _loc_of(archive: dict, cfg: dict) -> dict:
    bd = archive.get("backup_dir") or cfg.get("backup_dir")
    return {
        "snap_dir": Path(cfg["snapshot_dir"]) if cfg.get("snapshot_dir") else None,
        "backup_dir": Path(bd) if bd else None,
        "remote": archive.get("remote_host"),
        "remote_dir": archive.get("remote_path"),
        "remote_sudo": archive.get("remote_sudo", False),
    }


def _run_list(archives: list[dict], cfg: dict, verbosity: int) -> int:
    """Print a read-only table of timestamps across snapshot / backup / remote."""
    for archive in archives:
        name = archive["name"]
        loc = _loc_of(archive, cfg)
        snap_d = loc["snap_dir"]
        bak_d = loc["backup_dir"]
        remote = loc["remote"]
        remote_dir = loc["remote_dir"]
        remote_sudo = loc["remote_sudo"]

        snap_ts = {ts for ts, _ in iter_archive_items(snap_d, name)} if snap_d and snap_d.is_dir() else set()
        bak_ts = {ts for ts, _ in iter_archive_items(bak_d, name)} if bak_d and bak_d.is_dir() else set()
        rem_ts = set()
        if remote and remote_dir:
            rem_ts = {ts for ts, _ in iter_archive_items_ssh(remote, remote_dir, name, cfg, remote_sudo)}

        all_ts = sorted(snap_ts | bak_ts | rem_ts)
        log(f"\n=== {name} ===", 0, verbosity)
        if not all_ts:
            log("    (no snapshots/backups found)", 0, verbosity)
            continue
        # Only show columns that are actually configured for this archive.
        # snapshot_dir is global, so the snapshot column is always present.
        cols = [("snapshot", snap_ts)]
        if bak_d:
            cols.append(("backup", bak_ts))
        if remote and remote_dir:
            cols.append(("remote", rem_ts))
        hdr = "    " + f"{'timestamp':<20}" + " ".join(f"{label:<10}" for label, _ in cols)
        log(hdr, 0, verbosity)
        for ts in all_ts:
            cells = [f"{ts:<20}"] + [f"{'yes' if ts in tset else 'no':<10}" for tset in (_x[1] for _x in cols)]
            log("    " + "".join(cells), 0, verbosity)
    log("List complete.", 0, verbosity)
    return 0


def _run_prune(archives: list[dict], cfg: dict, verbosity: int, args: argparse.Namespace) -> int:
    specs = getattr(args, "ts", None) or []
    if not specs:
        print("Error: prune requires at least one --ts", file=sys.stderr)
        return 1
    try:
        specs = _validate_ts_specs(specs)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    total_to_del = 0
    for archive in archives:
        name = archive["name"]
        loc = _loc_of(archive, cfg)
        for label in ("snap", "backup", "remote"):
            items = [(ts, p) for ts, p in _iter_location(loc, label, name, cfg)
                     if any(_ts_match(ts, s) for s in specs)]
            if items:
                total_to_del += len(items)
                for ts, p in items:
                    disp = p.name if hasattr(p, "name") else str(p).rsplit("/", 1)[-1]
                    log(f"    [{label}] would delete {ts} ({disp})", 1, cfg["verbose"])
    if not total_to_del:
        log(f"Nothing to prune (no matches for {specs}).", 1, cfg["verbose"])
        return 0

    if args.dry_run:
        log(f"[dry-run] would delete {total_to_del} subvolume(s).", 1, cfg["verbose"])
        return 0

    if not args.yes:
        try:
            ans = input(f"Delete {total_to_del} snapshot/backup subvolume(s)? [y/N]: ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            return 1
        if ans.strip().lower() not in ("y", "yes", "1"):
            print("Aborted.", file=sys.stderr)
            return 1

    for archive in archives:
        name = archive["name"]
        loc = _loc_of(archive, cfg)
        for label in ("snap", "backup", "remote"):
            for ts, p in [(ts, p) for ts, p in _iter_location(loc, label, name, cfg)
                          if any(_ts_match(ts, s) for s in specs)]:
                if label == "remote":
                    cmd = build_ssh_cmd(loc["remote"],
                                        btrfs_cmd(cfg, "subvolume", "delete", str(p), sudo=False),
                                        loc.get("remote_sudo", False))
                else:
                    cmd = btrfs_cmd(cfg, "subvolume", "delete", str(p))
                try:
                    run(cmd, dry_run=False, verbosity=cfg["verbose"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                except subprocess.CalledProcessError as e:
                    print(f"Error pruning: {e}", file=sys.stderr)
                    return 1
    log(f"Prune complete: deleted {total_to_del} subvolume(s).", 1, cfg["verbose"])
    return 0


def _run_rebuild(archives: list[dict], cfg: dict, verbosity: int) -> int:
    """Reconcile snapshot/backup locations: preview the work, check space,
    then restore what is missing (lost local snapshot <- backup via a
    metadata-only subvolume snapshot, lost local backup <- snapshot via
    send/receive, lost remote <- local re-send, and a snapshot that exists
    only on the remote recovered back locally via an SSH send/receive).
    Never deletes; proceeds automatically (no --yes gate)."""
    dry = cfg.get("dry_run", False)
    restored = 0
    any_actionable = False
    for archive in archives:
        name = archive["name"]
        loc = _loc_of(archive, cfg)
        snap_d = loc["snap_dir"]
        bak_d = loc["backup_dir"]
        remote = loc["remote"]
        remote_dir = loc["remote_dir"]
        remote_sudo = loc["remote_sudo"]

        log(f"\n=== Rebuild: {name} ===", 1, cfg["verbose"])
        if not (snap_d and snap_d.is_dir()) and not bak_d and not (remote and remote_dir):
            log("  no destination configured — nothing to rebuild", 1, cfg["verbose"])
            continue

        snap_ts = {ts for ts, _ in iter_archive_items(snap_d, name)} if snap_d and snap_d.is_dir() else set()
        bak_ts = {ts for ts, _ in iter_archive_items(bak_d, name)} if bak_d and bak_d.is_dir() else set()
        rem_ts = set()
        if remote and remote_dir:
            rem_ts = {ts for ts, _ in iter_archive_items_ssh(remote, remote_dir, name, cfg, remote_sudo)}

        all_ts = sorted(snap_ts | bak_ts | rem_ts)
        if not all_ts:
            log("  no archives found anywhere — nothing to rebuild", 1, cfg["verbose"])
            continue

        # Phase 1: determine the plan (no side effects) and the local space it
        # would need.  Only new local backups consume local disk.
        items = []
        needed = 0
        remote_needed = 0
        snap_needed = 0
        for ts in all_ts:
            in_s, in_b, in_r = ts in snap_ts, ts in bak_ts, ts in rem_ts
            snap_p = snap_d / f"{name}.{ts}" if snap_d else None
            bak_p = bak_d / f"{name}.{ts}" if bak_d else None
            base = {
                "name": name, "ts": ts, "snap_dir": snap_d,
                "remote": remote, "remote_dir": remote_dir, "remote_sudo": remote_sudo,
            }
            if in_s and not in_b:
                if snap_p and snap_p.exists():
                    needed += _size_of(snap_p)
                base.update({"kind": "backup", "action": "restore local backup",
                             "from": "snapshot", "source": snap_p, "dest": bak_d})
                items.append(base)
            elif in_b and not in_s and bak_p and bak_p.exists():
                base.update({"kind": "snapshot",
                             "action": "restore snapshot (metadata-only)",
                             "from": "backup", "source": bak_p, "dest": snap_d})
                items.append(base)
            elif in_r and not in_s and not in_b and snap_d and remote and remote_dir:
                # Lost snapshot, only present on the remote: recover the full
                # subvolume by sending it from the remote and receiving it
                # into snapshot_dir.  The received subvolume lands as a
                # top-level readonly snapshot (btrfs receive default), matching
                # the normal snapshot convention.  The remote side is a plain
                # (non-incremental) send — the remote snapshot's own parent
                # subvolumes already live there, so no parent flags are needed.
                base.update({"kind": "from_remote",
                             "action": "restore snapshot (from remote)",
                             "from": "remote",
                             "source": f"{remote_dir}/{name}.{ts}",
                             "dest": snap_d})
                items.append(base)
                if snap_d.is_dir():
                    snap_needed += _remote_size_of(remote, f"{remote_dir}/{name}.{ts}",
                                                    cfg, remote_sudo) or 0
            elif remote and remote_dir and not in_r:
                src = snap_p if (in_s and snap_p and snap_p.exists()) else (
                    bak_p if (bak_p and bak_p.exists()) else None)
                if src is None:
                    base.update({"kind": "skip",
                                 "action": "skip (remote missing, no local source)",
                                 "from": "", "source": None, "dest": remote_dir})
                else:
                    src_kind = "snapshot" if (snap_p and snap_p.exists()) else "backup"
                    if src:
                        remote_needed += _size_of(src)
                    base.update({"kind": "remote",
                                 "action": f"re-send to remote ({src_kind})",
                                 "from": f"local {src_kind}", "source": src, "dest": remote_dir})
                items.append(base)
            else:
                if not in_s and not in_b and not in_r:
                    base.update({"kind": "skip", "action": "skip (not present anywhere)",
                                 "from": "", "source": None, "dest": None})
                else:
                    base.update({"kind": "ok", "action": "ok (nothing to do)",
                                 "from": "", "source": None, "dest": None})
                items.append(base)

        # Phase 2: preview the work to be done.
        log(f"    {'timestamp':<20} {'action':<38} source -> destination", 1, cfg["verbose"])
        for it in items:
            ts = it["ts"]
            if it["kind"] in ("backup", "snapshot", "remote", "from_remote"):
                src = it["source"]
                src_disp = (src.name if (src and hasattr(src, "name")) else str(src))
                dest = it["dest"]
                dest_disp = (dest.name if (dest and hasattr(dest, "name"))
                             else (str(dest).rsplit("/", 1)[-1] if dest else "-"))
                arrow = f"{src_disp} -> {dest_disp}"
            else:
                arrow = "-"
            log(f"    {ts:<20} {it['action']:<38} {arrow}", 1, cfg["verbose"])

        # Phase 3: report the memory/space requirement, then hard-stop if it
        # won't fit (warning emitted by _space_check when >=25% of free is used).
        if needed > 0:
            target = bak_d or (snap_d or Path("."))
            try:
                free = _disk_free(target)
            except OSError:
                free = 0
            log(f"    memory required: {_human_size(needed)} "
                f"(free at {target.name or target}: {_human_size(free)})", 1, cfg["verbose"])
            if not _space_check(target, needed, f"backup for {name}", cfg):
                continue  # _space_check already logged the reason; preview above is shown

        # Snapshot-space guard: restoring a lost snapshot from the remote writes
        # a full subvolume into snapshot_dir (a different filesystem from
        # backup_dir on this system), so check free space there.  We never
        # hard-stop here on the *remote* — only on the local target — so a
        # missing snapshot won't block the re-send to remote of other archives.
        if snap_needed > 0 and snap_d:
            target = snap_d
            try:
                free = _disk_free(target)
            except OSError:
                free = 0
            log(f"    memory required (snapshot): {_human_size(snap_needed)} "
                f"(free at {target.name or target}: {_human_size(free)})", 1, cfg["verbose"])
            if not _space_check(target, snap_needed, f"snapshot restore for {name}", cfg):
                # Downgrade the from_remote items for this archive to skip so the
                # remaining local/remote work still runs.
                for it in items:
                    if it["kind"] == "from_remote":
                        it["kind"] = "skip"

        # Report the remote requirement (if any) and guard the remote re-sends:
        # if it won't fit we skip just the remote part, so local work still runs.
        if remote_needed > 0 and remote and remote_dir:
            rem_free = _remote_disk_free(remote, remote_dir, cfg, remote_sudo)
            if rem_free is None:
                log(f"    [space] cannot measure free space on {remote} — "
                    f"proceeding (remote size not checked).", 1, cfg["verbose"])
            else:
                log(f"    memory required (remote {remote}): "
                    f"{_human_size(remote_needed)} "
                    f"(free on {remote}: {_human_size(rem_free)})",
                    1, cfg["verbose"])
                if not _space_check_remote(remote, remote_dir, remote_needed,
                                           f"re-send for {name}", cfg,
                                           remote_sudo, rem_free):
                    for it in items:
                        if it["kind"] == "remote":
                            it["kind"] = "skip"

        # Phase 4: execute the plan.
        for it in items:
            kind = it["kind"]
            if kind not in ("backup", "snapshot", "remote", "from_remote"):
                continue
            any_actionable = True
            ts = it["ts"]
            label = f"{name}.{ts}"
            if kind == "backup":
                log(f"    {label}: restoring local backup from snapshot", 1, cfg["verbose"])
                try:
                    _piped_send_to_local(it["source"], it["snap_dir"], it["dest"], cfg, name)
                    log(f"    {label}: local backup restored", 1, cfg["verbose"])
                    restored += 1
                except Exception as e:
                    log(f"    {label}: local backup restore failed: {e}", 1, cfg["verbose"])
            elif kind == "snapshot":
                log(f"    {label}: restoring snapshot (metadata-only) from backup", 1, cfg["verbose"])
                try:
                    cmd = btrfs_cmd(cfg, "subvolume", "snapshot", "-r",
                                    str(it["source"]), str(it["dest"]))
                    run(cmd, dry_run=dry, verbosity=cfg["verbose"],
                        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    log(f"    {label}: snapshot restored", 1, cfg["verbose"])
                    restored += 1
                except Exception as e:
                    log(f"    {label}: snapshot restore failed: {e}", 1, cfg["verbose"])
            elif kind == "remote":
                log(f"    {label}: re-sending to remote {remote}", 1, cfg["verbose"])
                try:
                    ssh_parents = find_parents_ssh(name, it["snap_dir"], remote,
                                                   remote_dir, cfg, remote_sudo)
                    ssh_send = btrfs_cmd(cfg, "send")
                    for i, pp in enumerate(ssh_parents):
                        ssh_send += ["-p" if i == 0 else "-c", str(pp)]
                    ssh_send.append(str(it["source"]))
                    rc, stderr = piped_run(ssh_send,
                                           build_ssh_receive_cmd(remote, remote_dir, cfg,
                                                                remote_sudo),
                                           dry_run=dry, verbosity=cfg["verbose"])
                    if rc == 0:
                        log(f"    {label}: remote restored", 1, cfg["verbose"])
                        restored += 1
                    else:
                        log(f"    {label}: remote re-send failed: {stderr.strip()}", 3, cfg["verbose"])
                except Exception as e:
                    log(f"    {label}: remote re-send error: {e}", 1, cfg["verbose"])
            elif kind == "from_remote":
                log(f"    {label}: restoring snapshot from remote {remote}", 1, cfg["verbose"])
                try:
                    # Remote → local: SSH into the remote and run `btrfs send`
                    # on the remote subvolume (plain send — the snapshot is
                    # top-level, so no parent flags are needed), then pipe
                    # the stream into a local `btrfs receive` into snapshot_dir.
                    # The received subvolume is created top-level and readonly,
                    # matching the normal snapshot convention.
                    send_cmd = btrfs_cmd(cfg, "send", sudo=False)
                    send_cmd.append(str(it["source"]))
                    send_cmd = build_ssh_cmd(remote, send_cmd, remote_sudo)
                    recv_cmd = btrfs_cmd(cfg, "receive", str(it["dest"]))
                    rc, stderr = piped_run(send_cmd, recv_cmd,
                                           dry_run=dry, verbosity=cfg["verbose"])
                    if rc == 0:
                        log(f"    {label}: snapshot restored from remote", 1, cfg["verbose"])
                        restored += 1
                    else:
                        log(f"    {label}: snapshot restore from remote failed: {stderr.strip()}", 3, cfg["verbose"])
                except Exception as e:
                    log(f"    {label}: snapshot restore from remote error: {e}", 1, cfg["verbose"])

    dry_prefix = "[dry-run] " if dry else ""
    if not any_actionable:
        log(f"{dry_prefix}Rebuild complete: nothing to do.", 1, cfg["verbose"])
    else:
        log(f"{dry_prefix}Rebuild complete: {restored} restored.", 1, cfg["verbose"])
    return 0


# ---------------------------------------------------------------------------
# main() — pre-dispatch between legacy backup and subcommand parsing.
# ---------------------------------------------------------------------------
def main() -> int:
    tokens = sys.argv[1:]
    idx = _maint_first_positional(tokens)
    if idx is None:
        # Bare -h / --help with no subcommand: show the top-level overview
        # (all subcommands + the legacy usage) rather than only the
        # backup option list.  Per-subcommand help still comes from
        # `bubtrsnap <subcommand> --help` below.
        if any(tok in ("-h", "--help") for tok in tokens):
            print(_TOP_LEVEL_HELP)
            return 0
        # Legacy backup: parse everything (all positionals are archives).
        parser = _build_backup_parser()
        args = parser.parse_args(tokens)
    else:
        # Maintenance: drop the command token and parse with its sub-parser.
        cmd = tokens[idx]
        args = _SUB_BUILDERS[cmd]().parse_args(tokens[:idx] + tokens[idx + 1:])
        args.subcommand = cmd
    if getattr(args, "subcommand", "backup") != "backup":
        return _run_maintenance(args)
    return _run_backup(args)

if __name__ == "__main__":
    sys.exit(main())