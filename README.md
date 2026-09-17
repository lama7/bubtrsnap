# bubtrsnap

A btrfs snapshot and backup management tool built on btrfs-native incremental
`send`/`receive`.  It bootstraps from nothing, uses a borg-style keep policy
for retention, and can send backups to local paths, remote SSH hosts, or both
in a single run.  When you need resilience across unreliable transfers, use
`rsync` as a transport to handle partial-transfer recovery.

Key features:

- Incremental snapshots and backups using `btrfs send`/`receive`
- Borg-style keep policy: hourly, daily, weekly, monthly, yearly
- Local backups (`backup_dir`), SSH remote backups (`remote_host`/`remote_path`),
  or both destinations in one run
- Optional `rsync` transport with `--rsync-opts` for partial-transfer recovery
  on interrupted sends
- Stream files (`export_file`/`export_dir`, `import_file`/`import_dir`) for
  large subvolumes or staged workflows
- Pre-snapshot, post-snapshot, and post-backup hooks with substitution strings
- Has a `--dry-run` mode that validates remote hosts and subvolumes without writing
- Minimal dependencies: `btrfs-progs` and `python3`; no databases or state files

Like the original [btrbu][] project it rewrites, bubtrsnap remains functionally
simple — give it a subvolume to snapshot and a destination to back it up to, and
it does the rest.

[btrbu]: https://github.com/lama7/btrbu

## Quick start

```bash
bubtrsnap --snapshot-dir=/pool/snapshots --backup-dir=/backup \
    archive1=/path/to/subvolume
```

Assuming a clean start, this creates a read-only snapshot in `/pool/snapshots`
and sends it to the backup location.  On subsequent runs, bubtrsnap handles
finding parents for all additional archives so only the incremental
information is sent. All paths **must** be valid btrfs subvolumes.

More than one archive can be specified:

```bash
bubtrsnap --snapshot-dir=/pool/snapshots --backup-dir=/backup \
    archive1=/var/lib data2=/home/user
```

Each archive is processed fully (pre-hook -> snapshot -> post-snapshot-hook ->
backup/send -> post-backup-hook -> keep policy) before the next begins.

## Configuration files

For anything beyond the simplest cases, a TOML configuration file keeps the
command line manageable.  The default location is
`~/.config/bubtrsnap.toml`:

```bash
bubtrsnap                     # loads ~/.config/bubtrsnap.toml
bubtrsnap --config=/path/to/configfile.toml # or use a custom file
```

The configuration file format is TOML:

```toml
# global options
snapshot_dir = "/pool/snapshots"
backup_dir = "/backup"
keep_daily = 5

[archive1]
subvolume = "/var/lib"

[archive2]
subvolume = "/home/user"
```

CLI options map to configuration keys by replacing `-` with `_` (so
`--snapshot-dir` becomes `snapshot_dir`).  Precedence is **CLI > archive >
global** — the CLI always wins, then archive-specific settings, then global.

```bash
bubtrsnap --keep-daily 3            # override keep policy for one run only
bubtrsnap archive1                # process only archive1
bubtrsnap archive2 archive3       # process multiple archives setup in a
                                  # configuration file
```

## Local and remote backups

Backups to the local system, a DAS for example, or a remote backup host
on the network can be coordinated as one, the other or both in a single
run with bubtrsnap.

### Local backup only

For a local only backup, specify `--backup-dir`:

```bash
bubtrsnap --snapshot-dir=/pool/snapshots --backup-dir=/backup \
    archive1=/path/to/subvolume
```

This takes a snapshot of the archive's subvolume and uses it as a source for
sending it to the backup location.

### Snapshots only (`--snaps-only`)

If you only want snapshots and no backups, use `--snaps-only` (config:
`snaps_only`, also per-archive):

```bash
bubtrsnap --snapshot-dir=/pool/snapshots --snaps-only \
    archive1=/path/to/subvolume
```

This creates the read-only snapshot and exits — no `btrfs send`/`receive` or
keep pruning is performed.  The `--snaps-only` option is mutually exclusive
with `--export-file`, `--import-file`, `--stage-file`, `--export-dir`,
`--import-dir`, and `--stage-dir`, and bubtrsnap will error out if
`--snaps-only` is set with any of them.

### Local sudo (`--local-sudo`)

When bubtrsnap needs to run `btrfs` commands on directories it doesn't own,
use `--local-sudo` (config: `local_sudo`).  This prepends `sudo -n` to local
`btrfs` commands (snapshot creation, `btrfs send`/`receive`, `btrfs subvolume
show` for validation) as well as `scp`/`rsync` used for file-based stream
transfers.  The user running bubtrsnap must have their sudo profile configured
for NOPASSWD access to these commands.  The `remote_sudo` option is separate —
it only affects commands run on the remote via SSH.

### Remote backup via SSH

Use the `--remote-host` and `--remote-path` options to backup to another host
over a network:

```bash
bubtrsnap --snapshot-dir=/pool/snapshots \
    --remote-host user@backuphost --remote-path /btrfs/backups \
    archive1=/path/to/subvolume
```

This creates a snapshot, then sends the appropriate incremental or full send
data via SSH to the remote host into a `btrfs receive` which writes the
resulting subvolume to `/btrfs/backups`.  The remote directory is validated as
a btrfs subvolume automatically before receiving.

The `remote_path` option always requires `remote_host`.  If `remote_sudo` is
set, `sudo -n` is prepended to the remote btrfs commands.

### Both local and remote in one run

Specify `backup_dir`, `remote_host`, and `remote_path` together and bubtrsnap
sends to both destinations using the same snapshot as source:

```bash
bubtrsnap --snapshot-dir=/pool/snapshots --backup-dir=/local/backups \
    --remote-host user@backuphost --remote-path /btrfs/backups \
    archive1=/path/to/subvolume
```

### rsync transport (optional)

By default bubtrsnap uses `scp` for file-based stream transfers to a remote.
Enable `rsync` for partial-transfer recovery:

```bash
bubtrsnap --snapshot-dir=/pool/snapshots \
    --export-file /tmp/stream.btrfs \
    --remote-host user@backuphost --remote-path /btrfs/backups \
    --rsync --rsync-opts '-e "ssh -p 2222"' \
    archive1=/path/to/subvolume
```

When `--rsync` is enabled:

- The rsync options `-a` (archive mode) and `--partial-dir .bubtrsnap-partial`
    are always added
- Additional options come from `--rsync-opts` (config: `rsync_opts`)
- Dangerous options (`--delete`, `--partial-dir`, `-v`, `--progress`,
  `--dry-run`, `--daemon`, `--rsh`, `--exclude`, etc.) are blocked and reported
- The rsync option can be set globally, per-archive, or on the CLI

To use a non-standard SSH port or identity file, put `ssh -p PORT` (or
`-i /path/to/key`) with the `-e` rsync option in `rsync_opts`:

```toml
rsync = true
rsync_opts = '-e "ssh -p 2222 -i /home/user/.ssh/id_rsa"'
```

Using rsync affects only file-based transfers (replacing scp) and does not
affect piping `btrfs send | btrfs receive` or local backup operations.

## Stream files

For very large subvolumes or when you want to break the send and receive steps
apart, use stream files:

| Option         | Description                                  |
|----------------|----------------------------------------------|
| `export_file`  | Write the btrfs stream to a file, then stop  |
| `import_file`  | Receive from a stream file into `backup_dir` |
| `export_dir`   | Write stream to `dir/archive.timestamp.btrfs`|
| `import_dir`   | Search `dir/` for the most recent matching stream file |

Local staging can be accomplished by using `export_file` and `import_file`
together: send to a file, then receive from that same file.  If the export file
already exists, it is overwritten. When combined with
`remote_host`/`remote_path`, the stream file is sent to the remote and received
there — a fully automated single-command workflow.

When `import_file` is used on its own (no snapshotting needed), the snapshot
step is skipped entirely — processing starts at the receive step.

### Remote receive with stream files

When `export_file` or `export_dir` is combined with remote settings (and no
explicit `import_file`/`import_dir`/`stage_file`), bubtrsnap:

1. Creates the snapshot
2. Writes the btrfs stream to a local file
3. Copies the stream file to the remote via `scp`
4. Runs `btrfs receive -f <temp_file> <remote_path>` on the remote
5. Cleans up the temporary file on the remote
6. Applies keep policy on the remote

For `rsync` transport, the stream file is transferred via `rsync` instead of
`scp`, and interrupted transfers can be resumed.

### Combining local and remote stream receive

An archive configured with `backup_dir` + `export_file`/`export_dir` +
`remote_host`/`remote_path` sends to **both** destinations:

1. Local piped `btrfs send | btrfs receive` to `backup_dir`
2. Stream file written, transferred, and received on remote

Keep policy is applied independently to each destination.

A CLI usage example:

```bash
bubtrsnap --snapshot-dir /snapshots \
    --export-file /tmp/stream.btrfs \
    --remote-host user@backuphost --remote-path /btrfs/backups \
    archive1=/path/to/subvol
```

This command would create a readonly snapshot of archive1, create stream file
/tmp/stream.btrfs with appropriate incremental (or not) information, use scp
to transfer the file to the remote host where it will then execute a btrfs
receive command to create the subvolume on the remote host.

In a config file:

```toml
snapshot_dir = "/snapshots"
export_dir = "/local/streams"
backup_dir = "/pool/backups"
remote_host = "user@backuphost"
remote_path = "/btrfs/backups"

[archive1]
subvolume = "/home/user/data"
keep_daily = 7
keep_weekly = 4
```

Given the above in a configuration file, a simple `bubtrsnap` CLI command
would perform the equivalent operations to the above CLI example, plus perform
a local piped send -> receive to /pool/backups and apply the specified keeps to
the archive snapshots, local backup and remote backup locations.

## Staging: stage-file / stage-dir

The `--stage-file FILE` and `--stage-dir DIR` options are a convenience that
combine send-to and receive-from using the same path, then delete the stream
file:

```bash
bubtrsnap --stage-file /tmp/archive.btrfs archive1=/path/to/subvolume
```

When combined with `--remote-host`/`--remote-path`, staging also transfers the
stream to the remote (via `scp` or `rsync`) and receives it there, just like
`export_file`/`import_file`.

## Keep policy

The keep policy is inspired by [borgbackup][].  It specifies how many
snapshots/backups to retain at each time interval:

| Option        | CLI flag         | Config key      |
|---------------|------------------|-----------------|
| `keep_hourly` | `--keep-hourly`  | `keep_hourly`   |
| `keep_daily`  | `--keep-daily`   | `keep_daily`    |
| `keep_weekly` | `--keep-weekly`  | `keep_weekly`   |
| `keep_monthly`| `--keep-monthly` | `keep_monthly`  |
| `keep_yearly` | `--keep-yearly`  | `keep_yearly`   |

Intervals are applied shortest to longest with no overlap — a snapshot kept
because of a daily keep does not count towards a weekly or monthly keep.  The
keep policy applies to **both** snapshots and backups.

When no keep values are set (all `0`), pruning is skipped — existing snapshots
and backups are left untouched.  By default, all keep values are set to 0.

```toml
snapshot_dir = "/pool/snapshots"
backup_dir = "/backup"
keep_daily = 4
keep_weekly = 2
keep_monthly = 1

[archive1]
subvolume = "/path/to/subvolume1"
```

### Forced keeps

If there is a particular archive you don't want bubtrsnap to remove, the
`--forced-keep TIMESTAMP` (config: `forced_keep`) option forces retention of
specific timestamps that would otherwise be pruned.  Format: `YYYYMMDDhhmm` (12
digits).

```bash
bubtrsnap --forced-keep 202601011200 --forced-keep 202601021200 archive1=/path/to/subvolume
```

In config:
```toml
[archive1]
subvolume = "/path/to/subvol"
forced_keep = "202601011200,202601021200"
```

Note that on the CLI, a forced keep doesn't last beyond the command itself.  A
subsequent CLI command without the forced keep specified again might result in
the archive getting pruned.  To keep a specific archive over many iterations, use `forced_keep` in a configuration file so it is specified on every run.

### Weekly boundary

Weekly keeps are aligned to the day before `week_startday` (default:
`sunday`, meaning the week ends on Saturday).  This can be set globally or
per-archive.

## Hooks

Hooks coordinate external programs at three stages:

| Hook               | Timing                           |
|--------------------|----------------------------------|
| `pre_snapshot_hook`| Before the snapshot is taken     |
| `post_snapshot_hook`| After the snapshot, before backup |
| `post_backup_hook` | After the backup is complete     |

Hooks use substitution strings that are replaced at run time:

| Placeholder   | Available in              |
|---------------|---------------------------|
| `{archive}`   | all                       |
| `{subvol}`    | pre-snapshot              |
| `{timestamp}` | all                       |
| `{snapshotdir}`| all                       |
| `{backupdir}` | all (may be empty)        |
| `{snapshot}`  | post-snapshot, post-backup |
| `{backup}`    | post-backup               |

Example:

```toml
snapshot_dir = "/pool/snapshots"
backup_dir = "/backups"

[archive1]
subvolume = "/home/user1"
pre_snapshot_hook = echo "Starting backup {timestamp}" >> ~/backuplog
post_snapshot_hook = "borg create --verbose user@server:repo::{archive} {snapshot}"
post_backup_hook = 'echo "Backup of {archive} complete" >> ~/backuplog'
```

## Dry-run and debugging

To test a particular configuration or set of options, the `--dry-run` option
shows what bubtrsnap would do without making changes:

- Read-only validation (SSH checks, `btrfs subvolume show`/`list`, stream
  header inspection) is still executed
- Write operations (snapshot creation, send/receive, delete/prune) are
  logged with a `[dry-run]` prefix and skipped
- Stream file creation and deletion are also skipped

Additionally, `--dry-run` raises verbosity to level 2 (commands) so all the
commands that would execute are visible.  Use `--debug` (equivalent to
`--verbose 3`) for maximum verbosity including UUIDs and internal decision
details.

Always use `--dry-run` to preview a new keep policy or remote configuration.

## Configuration reference

**Global-only options:**
`snapshot_dir`, `local_sudo`, `stage_dir`, `verbose`, `dry_run`

**Global or per-archive (archive overrides global):**
`backup_dir`, `snaps_only`, `export_dir`, `import_dir`, `remote_host`,
`remote_path`, `remote_sudo`, `rsync`, `rsync_opts`, `week_startday`,
`keep_hourly`, `keep_daily`, `keep_weekly`, `keep_monthly`, `keep_yearly`

**Per-archive only:**
`subvolume` (required), `export_file`, `import_file`, `stage_file`,
`pre_snapshot_hook`, `post_snapshot_hook`, `post_backup_hook`, `forced_keep`

## Dependencies

- `btrfs-progs` (for `btrfs` commands)
- `python3`
- SSH client (only for remote backup, at least on the client side)
- `rsync` (only when `--rsync` is enabled)

## Unit tests

```bash
python3 unittests/test_keep_policy.py -v
python3 unittests/test_ssh.py -v
python3 unittests/test_file_options.py -v
python3 unittests/test_runtime.py -v
```

[btrbu]: https://github.com/lama7/btrbu
[borgbackup]: https://borgbackup.org
