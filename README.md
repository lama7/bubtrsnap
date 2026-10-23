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

An archive name (the TOML section name, or the `ARCHIVE` in
`bubtrsnap ARCHIVE=/path`) is used directly as a snapshot and subvolume name,
so it may contain only letters, digits, `_` and `-` and must start with a
letter or digit.

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

- The rsync options `-a` (archive mode) and `--partial-dir .bubtrsnap-partial-<archive>`
    are always added — the partial-dir is per archive, so a stale partial from
    one archive never makes another archive look interrupted
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

### When a destination runs out of space

A `btrfs receive` that dies on a full destination pool still creates the
subvolume it was writing and leaves it half-written.  bubtrsnap renames that
partial to `.bubtrsnap-diskfull-<archive>-<timestamp>` in the destination that
failed and prints the one command that clears it:

```
Disk full: the partial subvolume /backup/home.202601010000 is blocking /backup.
Remove it to free the space and unblock later runs:
    btrfs subvolume delete /backup/.bubtrsnap-diskfull-home-202601010000
```

The renamed subvolume is invisible to the keep policy, `list` and `rebuild`:
its name cannot match the `archive.YYYYMMDDhhmm` pattern.  It is also the space
the failed receive ate, so the command above removes both the garbage and the
flag.  Only a real ENOSPC triggers this — a dropped SSH pipe, an
already-existing target, or a permission error leaves the partial where it is.
`--dry-run` never renames anything.

While the sentinel is there, **nothing is sent to or received from that
destination**.  A `btrfs send` into a full pool fails the same way and leaves
another orphan, and an incremental stream's size cannot be measured in advance,
so a partial already in the pool is the only reliable sign that the pool is
broken.  The block is per destination: a full remote never cancels a local
backup, and a full local backup never cancels the remote leg.  It is a skip,
not an abort — the snapshot is still taken, the other destination is still
written, and keep-policy pruning still runs, which is how the pool gets space
back.  The run still exits 1 so cron sees it at `-v 0`, and the reason is
printed once per destination per run:

```
Disk full: /backup holds 1 partial subvolume(s) left by a failed receive:
    .bubtrsnap-diskfull-home-202601010000
Nothing is sent to /backup while they are there.  They are also the space the
failed receive used, so this clears both the block and the garbage:
    btrfs subvolume delete /backup/.bubtrsnap-diskfull-home-202601010000
Error: 1 destination(s) skipped because they are disk-full: /backup
```

Detection costs nothing on the paths that already list the destination (the
keep policy, `rebuild` and the parent lookups), because those walks record what
they see.  A remote receive that has not listed the pool yet issues one
read-only `btrfs subvolume list`; it also runs under `--dry-run`, so a dry run
reports a block a live run left behind.  Metadata-only snapshot restores are not
blocked, since they write no data.

### When a receive dies for another reason

A receive can also die without the pool being full: the stream is truncated, the
SSH pipe is dropped, the process is killed.  btrfs then still creates the
subvolume, and the only thing that distinguishes it from a real backup is that
the Received UUID was never written:

```
$ btrfs subvolume list -R /backup
ID 256 gen 9 top level 257 received_uuid -  path lama7.202601010000
```

bubtrsnap calls such a subvolume a **partial receive**.  It is detected from the
listing the run already takes (`btrfs subvolume list -u -R -p` on a remote, one
`subvolume list` plus the directory walk it already does locally), reported once
per destination per run on stderr at `-v 0`, and treated as absent everywhere it
matters:

- `list` prints `partial` in that column instead of `yes`.
- The keep policy does not count it in a slot, so a corrupt newest entry cannot
  push the last real backup out of the policy and get it pruned.
- `rebuild` plans to restore over it and then refuses the leg — `btrfs receive`
  cannot land on an existing subvolume — and counts it as a failed action, so
  `rebuild` exits 1.

Nothing is deleted by default.  To make the run clean them up as part of keep
policy, opt in with `--prune-partial-subvolumes` (or
`prune_partial_subvolumes = true` in the config, globally or per archive):

```
Partial receive: /backup holds 1 subvolume(s) left by a receive that died
mid-stream:
    lama7.202601010000
btrfs records no Received UUID for them, so they are not usable backups: the
keep policy, `list` and `rebuild` all ignore them, and nothing is restored over
them.  They still hold the space the failed receive used, so removing them frees
it and clears the flag:
    btrfs subvolume delete /backup/lama7.202601010000
  or: bubtrsnap prune lama7 --ts 202601010000
Run with --prune-partial-subvolumes to remove such leftovers automatically.
```

Detection is deliberately fail-closed: if the destination cannot be listed, or
the UUID columns are not available (older btrfs-progs on a remote falls back to
the plain listing it used to run), nothing is classified as partial and nothing
is ever deleted on a guess.  `snapshot_dir` is not judged this way, because a
snapshot made with `btrfs subvolume snapshot -r` has no Received UUID either —
the rule would call every snapshot broken.  What a snapshot pool *can* be judged
by is the read-only flag, which is the next section.

### When snapshot_dir holds something bubtrsnap cannot use

Only a read-only subvolume can be sent, and only a read-only subvolume can be
used as an incremental parent with `-p`/`-c`.  `bubtrsnap` snapshots with
`btrfs subvolume snapshot -r`, so every archive it wrote in `snapshot_dir` carries
that flag.  A child with an archive name that does not carry it is not an archive
`bubtrsnap` can use, whatever made it: `snapshot` without `-r`, `btrfs property
set <path> ro false`, or a `btrfs receive` that died mid-stream while `rebuild`
was recovering a snapshot that only exists on the remote.

The flag is read with one extra read-only call per snapshot pool per run,
`btrfs subvolume list -r -p <snapshot_dir>`.  `-r` is the only listing that
reports it — it is a filter rather than a column, so no combination of
`-u -R -p -o -s -c -t -a` shows it — and a listing is filesystem-wide, so `-p`
plus the pool's own subvolume ID keeps nested and other-pool entries out.  A
btrfs-progs that rejects `-r` falls back to one `subvolume show` per candidate and
reads its `Flags:` line; a pool that cannot be scanned at all is left alone.  Only
`snapshot_dir` is scanned, never a remote destination: everything there was
received, so the Received UUID rule above is the right one there.  When
`snapshot_dir` and `backup_dir` are the same directory both rules run on it, and
an entry caught by either is unusable.

```
Unusable snapshot: /snap holds 1 incompat subvolume(s) named like an archive that
btrfs does not mark read-only:
    lama7.202601010000
Only a read-only subvolume can be sent, or used as an incremental parent, so these
are not archives bubtrsnap can use: the keep policy, `list` and `rebuild` all
ignore them.  A receive that died mid-stream leaves exactly this, and so does a
snapshot made without -r.  They still hold the space they used, so removing them
frees it:
    btrfs subvolume delete /snap/lama7.202601010000
  or: bubtrsnap prune lama7 --ts 202601010000
Run with --prune-partial-subvolumes to remove such leftovers automatically.
```

Such an entry is called **incompat**, not partial: nothing received it, so
"half-written" is not the claim being made — only that `bubtrsnap` cannot use it.
Like a partial receive it is kept out of the keep policy's slot counting, so the
corrupt newest entry cannot push a real archive out of a slot, and like a partial
it is removed only if `--prune-partial-subvolumes` was set.  `list` prints
`incompat` in the snapshot column instead of `yes`, and `rebuild` plans around it:
`btrfs subvolume snapshot -r` onto an existing writable directory does not
overwrite it, it nests a new subvolume inside it, so a snapshot leg whose target
name is already such an entry is refused and counted as a failure rather than
attempted.  A failed receive into `snapshot_dir` records its own orphan, so the
legs still queued for that pool in the same run see it.

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

If `backup_dir` is configured alongside this setup, an **additional** local
piped `btrfs send | btrfs receive` writes directly to `backup_dir`. This uses
parents matched to `backup_dir`'s history — independent from the parents used
for the exported stream going to the remote. See [Combining local and remote
stream receive](#combining-local-and-remote-stream-receive) below.

### Combining local and remote stream receive

An archive configured with `backup_dir` + `export_file`/`export_dir` +
`remote_host`/`remote_path` sends to **both** destinations:

1. Local piped `btrfs send | btrfs receive` to `backup_dir`
2. Stream file written, transferred, and received on remote

Keep policy is applied independently to each destination.

Each destination receives its data through an **independent pipeline**:
`backup_dir` gets a fresh piped send from the new snapshot with parents
matched to `backup_dir`'s actual history, while the exported stream file
goes to the remote with parents matched to the remote's history. This
ensures correct incrementals even when the two backup locations have
divergent snapshots.

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
`export_file`/`import_file`. If `backup_dir` is set alongside remote staging,
bubtrsnap uses an independent piped send for the local backup (with correct
parents from `backup_dir`'s history) and sends the stream file exclusively to
the remote — two separate pipelines with matching parents for each destination.

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

## Maintenance subcommands

Beyond the legacy `bubtrsnap ARCHIVE[=SUBVOLUME] ...` invocation, bubtrsnap
offers maintenance subcommands. When you give a subcommand name on the command
line it is parsed with its own option set; with no subcommand the legacy backup
behaviour applies (all positionals are archive names). The global options
(`--config`, `--snapshot-dir`, `--backup-dir`, `-v/--verbose`, `--dry-run`,
`--rsync`, and the `keep_*` policies) are shared by every subcommand. Run
`bubtrsnap <subcommand> --help` for that command's full option list.

**`bubtrsnap backup`** — take snapshots and send backups. This is the default
behaviour when no subcommand is given.

**`bubtrsnap list [ARCHIVE ...]`** — read-only table of the snapshot / backup /
remote timestamps that exist for each archive. All config archives are shown if
none are named. A backup or remote entry left by a receive that never finished
is labelled `partial` rather than `yes`, and a `snapshot_dir` entry that btrfs
does not mark read-only is labelled `incompat` (see *When a receive dies for
another reason* and *When snapshot_dir holds something bubtrsnap cannot use*).

**`bubtrsnap prune --ts TS [ARCHIVE ...]`** — delete snapshot/backup subvolumes
matching the given timestamp(s). `--ts` takes a single timestamp
`YYYYMMDDhhmm` or a dotted inclusive range `<from>..<to>`; multiple `--ts`
options match any of them. Prune never operates on "all" archives — name at
least one. Without `--yes` (and without `--dry-run`) it asks for confirmation
before deleting. If any timestamp in the target set is marked `forced_keep`,
the preview labels it `[forced-keep]` and an *additional* confirmation is
required even when `--yes` is set (a hand-picked range can capture a
protected timestamp by accident); declining aborts the whole prune.

**`bubtrsnap rebuild [ARCHIVE ...]`** — reconcile snapshot/backup/remote
locations. It restores a lost local snapshot from a local backup, restores a
lost local backup from a local snapshot (via `btrfs send`/`receive`), and
re-sends to a remote when it is missing there; it can also recover a snapshot
that exists only on the remote. It never deletes — it only restores. All
config archives are rebuilt if none are named. A rebuild is plan-first: its
action list is computed once from the state at the start of the run, and each
action fills only one of the three locations. As a result, a snapshot missing
in *two* locations needs two runs. For example, an archive that only exists as
a local snapshot gets its local backup restored on the first run and is only
re-sent to the remote on the second; likewise an archive that only exists on the
remote is recovered as a local snapshot on the first run and gets its local
backup filled in on the second.  An archive with no `backup_dir` has no local
backup to restore, so its remote re-send happens in the same run. Run
`bubtrsnap list` to confirm all three locations line up before calling it done.
A restore that was attempted and failed is reported at verbosity 1 and `rebuild`
exits 1 when any action failed; an action the space guard chose not to attempt
is a skip, not a failure, and does not change the exit code.  A partial receive
(see above) does not count as a backup, and an `incompat` entry does not count as
a snapshot, so a leg that would have to receive over one, or snapshot into one, is
refused and counted as a failure.

Examples:

```bash
bubtrsnap --config=~/.config/bubtrsnap.toml
bubtrsnap backup --snapshot-dir=/snap --backup-dir=/bck home=/home
bubtrsnap list home
bubtrsnap prune --ts 202601010000..202601050000 home --yes
bubtrsnap rebuild home
```

## Configuration reference

**Global-only options:**
`snapshot_dir`, `local_sudo`, `verbose`, `dry_run`

**Global or per-archive (archive overrides global):**
`backup_dir`, `snaps_only`, `export_dir`, `import_dir`, `stage_dir`,
`remote_host`, `remote_path`, `remote_sudo`, `rsync`, `rsync_opts`,
`week_startday`, `keep_hourly`, `keep_daily`, `keep_weekly`,
`keep_monthly`, `keep_yearly`, `prune_partial_subvolumes`

**Per-archive only:**
`subvolume` (required), `export_file`, `import_file`, `stage_file`,
`pre_snapshot_hook`, `post_snapshot_hook`, `post_backup_hook`, `forced_keep`

An archive may not mix file options (`export_file`/`import_file`/`stage_file`)
with directory options (`export_dir`/`import_dir`/`stage_dir`) — the same
blanket rule the CLI enforces.  Global directory options apply to an archive
only when that archive has no file options of its own.

### Keep-policy precedence (archive-specific keeps)

If an archive defines **any** `keep_*` value, it fully controls its own keep
policy — the other intervals it does **not** set default to `0` (they are not
inherited from the global section). The global keep values are used only when
the archive sets none of them. So an archive-specific policy is complete as
written; to keep a yearly as well you must set `keep_yearly` too.

## Configuration examples

A set of ready-to-use configuration files ship in `examples/`. Copy one into
`~/.config/bubtrsnap.toml` (or pass it to `--config`) and edit the paths.

- `examples/basic.toml` — snapshots and local backups for **two archives** with
  a single **global** keep policy, plus `local_sudo` (with a comment explaining
  when it's needed).
- `examples/single-remote.toml` — **one archive** that snapshots, keeps a
  **local** backup, and also keeps a **remote** backup; uses an
  **archive-specific** keep policy.
- `examples/single-rsync.toml` — **one archive** that snapshots, keeps a local
  backup, and sends the **stream to the remote over rsync** (resumable
  transfers); uses an **archive-specific** keep policy.
- `examples/remote-only.toml` — **one archive** that keeps only local
  snapshots and sends backups to a **remote** (no local backup destination);
  uses an **archive-specific** keep policy.

Example — basic local snapshots and backups, two archives:

```toml
snapshot_dir = "/pool/snapshots"
backup_dir   = "/backup"

# Only needed when bubtrsnap doesn't own the subvolumes/directories (e.g.
# backing up system subvolumes or a root-owned /pool). Prepends `sudo -n`.
local_sudo = true

# Global keep policy: applies to snapshots and backups of every archive that
# does not define its own keep values.
keep_daily   = 7
keep_weekly  = 4
keep_monthly = 2

[home]
subvolume = "/home"

[varlib]
subvolume = "/var/lib"
```

Example — one archive, local + remote, archive-specific keep policy:

```toml
snapshot_dir = "/pool/snapshots"
backup_dir   = "/backup"
remote_host  = "backup@example.com"
remote_path  = "/btrfs/backup"

[server]
subvolume = "/var/lib"
# Archive-specific: fully controls this archive's policy. Intervals omitted
# here are 0 (not inherited from any global section).
keep_daily   = 14
keep_weekly  = 4
keep_monthly = 4
```

Example — one archive with rsync remote transfer (resumable stream files):

```toml
snapshot_dir = "/pool/snapshots"
backup_dir   = "/backup"
remote_host  = "backup@example.com"
remote_path  = "/btrfs/backup"

# Stream directory: incremental streams are written here as
# <archive>.<timestamp>.btrfs and rsync'd to the remote (resumable).
export_dir = "/pool/streams"

rsync      = true
rsync_opts = '-e "ssh -p 2222"'   # optional: non-standard port / key

[server]
subvolume = "/var/lib"
keep_daily   = 14
keep_weekly  = 4
keep_monthly = 4
```

**Note:** rsync only affects the *file-based* stream transfer. A plain
`btrfs send | btrfs receive` over SSH is never rsync'd, so a rsync config must
also produce a stream file (`export_file`/`export_dir`/`stage_file`).

Example — let keep policy clean up the leftovers of a receive that died
mid-stream instead of only reporting them:

```toml
snapshot_dir = "/pool/snapshots"
backup_dir   = "/backup"

# Off by default: a run only reports such subvolumes and prints the delete
# command.  Covers both a receive that died mid-stream in a backup or remote
# pool and a snapshot_dir entry btrfs does not mark read-only.  Set per archive
# too, if only one archive should be pruned this way.
prune_partial_subvolumes = true

[home]
subvolume = "/home"
```

## Installing

```bash
pipx install bubtrsnap     # recommended: puts `bubtrsnap` on your PATH, isolated from system python
pip install bubtrsnap      # or into a virtualenv (see note below)
```

**Note for Arch / other "externally-managed" distros:** on Arch (and similar distros that ship the PEP 668 "externally managed" marker), `pip install` / `pip3 install` fails with `externally-managed-environment` because it would write into distro-managed site-packages. The fix is to install into an isolated environment instead:

```bash
pipx install bubtrsnap             # simplest; pipx creates its own venv
# or
python3 -m venv ~/venvs/bubtrsnap && ~/venvs/bubtrsnap/bin/pip install bubtrsnap
```

On Arch, `pipx` itself is the `python-pipx` package: `pacman -S python-pipx` (not `pipx` or `pip3`). Avoid `--break-system-packages` unless you specifically want to override the distro guard.

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
python3 unittests/test_rebuild.py -v
```

[btrbu]: https://github.com/lama7/btrbu
[borgbackup]: https://borgbackup.org
