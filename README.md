bubtrsnap
=====

Based on my previous [btrbu][] project, bubtrsnap is a rewrite of it using
Grok.  It is a python3 based project and is a superior implenatation, imho.

More formally, bubtrsnap is a(nother) btrfs snapshot and backup management
script.  Like btrbu before it, bubtrsnap will bootstrap itself if no backups
exist and uses a keep policy similar to that of borg (ie- specifying a number
        of keeps at different time intervals).  For the simplest cases, it can
be used straight from the command line but for more sophisticated needs it uses
a TOML formatted configuration file with a default location in a users
`~/.config/`.

The information bubtrsnap needs is minimal- a snapshot directory, an archive
name and a subvolume that the archive name is associated with.  If a backup is
desired, then a backup path must also be specified.  It is a btrfs specific
utility and takes advantage of the `send` and `receive` commands to aid with
making backups.  From a configuration file, hooks are availabe at different
stages to enhance it's capabilities via scripts or other command line
utilities.  For instance, a snapshot can be taken and then a hook used to
invoke a [borgbackup][] command using the just created snapshot as a source.

As with btrbu, bubtrsnap remains largely dependency free.  It uses no data
files or databases in order to perform its duties.  It's main dependency is a
python3 installation.

[btrbu]: https://github.com/lama7/btrbu

Usage
-----

For simple snapshot and backup needs, a command can be as simple as:

bubtrsnap --snapshot-dir=/pool/snapshots --backup-dir=/backup archive1=/path/to/subvolume


Assuming a start from nothing, this will take a snapshot of
`/path/to/subvolume` and place it in `/pool/snapshots` with a timestamp suffix.
So the snapshot name will be of the form `archive.YYYYMMddhhmm`.  This snapshot
will then be used to send a full backup to `/backup`. 

Subsequent use of this same command will result in incremental backups which
will just advance the timestamp associated with the archive.  By default,
     previous snapshots and backups are retained so that bubtrsnap can take
     advantage of btrfs' incremental send and receive capabilities.  Each prior
     snapshot and backup becomes either a parent or source.  See [keep
     policy][] below for how to change the keep behavior.

[keep policy]: #keep-policy

More than 1 archive can be specified on the command line:

    bubtrsnap --snapshot-dir=/pool/snapshots --backup-dir=/backup archive1=/path/to/subvolume archive2=/path/to/subvolume2

These archives will share the same snapshot and backup destination, but
obviously have different names qualified with a timestamp.

For a full list of options and their explanations, `bubtrsnap --help` is
useful.

Configuration Files
-------------------

At some point, if backing up several different subvolumes for instance, a
configuration file might become desirable to make the command line more
manageable.  bubtrsnap will look for a configuration file in
`~/.config/bubtrsnap.toml` if no file is specified on the command line.
Alternatively:

    bubtrsnap --config=/path/to/myconfig

Configuration files use TOML formatting and look like:

```
    # a comment... these are global options
    snapshot_dir = "/pool/snapshots"
    backup_dir = "/backup"

    keep_daily = 5

    # archives are under a header
    [archive1]
    subvolume = "/path/to/subvolume1"

    [archive2]
    subvolume = "/path/to/subvolume2"
    keep_daily = 7
    keep_monthly = 1

    [archive3]
    subvolume = "/path/to/subvolume3"

```

If an option can be specified on the command line, it can also be specified in
the configuration file.  Be sure to susbstitute a `'_'` for any `'-'`
characters in the command line option.  So the `--snapshot-dir` option would be
specified as `snapshot_dir` in a configuration file.

Configuration files for bubtrsnap have a global section and then archive
sections.  Global settings in a configuration file apply to all archives
declared in the file and and archive declared on the command line.  So setting
default `snapshot_dir` and `backup_dir` settings is a good way to go to keep
CLI entry to a minimum.  Any configuration file setting is over-ridden by a CLI
option specification.  So if setting up a new archive, setting a new
`backup_dir` on the command line can be done without having to comment out
things in the configuration file.  The order of precedencee is CLI > archive
specific > global.

Archive sections are defined be a header which names the archive and then a
`subvolume = "/path/to/subvolume"` entry.  Within an archive section, all key
value paris are specific to that archive.  Options set in the archive take
precedence over global settings.

An important concept to keep in mind when using bubtrsnap is this idea of
option precedence.  The CLI has top priority as it is assumed the user knows
what they want to do.  After that, any archive specific settings are applied,
then global settings and finally, program default settings as appropriate.  So,
using the above example, a keep policy of 5 days is specified globally.  The
policy is over-ridden for archive2 which uses a keep policy of 7 dailies and 1
monthly.  See [keep policy][] below for more info on keeps.

Several options are global only:

+ `snapshot_dir`
+ `backup_dir`
+ `sudo`

Finally, the `dry-run` option is not honored in the configuration file.  Simply
add it the the CLI to see how bubtrsnap will proceed with a configuration.

[keep policy]: #keep-policy

Option: send-to-file
--------------------

A new feature unique to bubtrsnap is the `send-to-file` option which leverages
the ability of btrfs to send it's data to a file rather than another btrfs
filesystem. This is to facilitate backups of very large archives where a raw
send-receive could get interrupted due to the time it takes for the transfer.
This option is still being developed but as of now, the option takes a
directory destination for the resulting file of the `btrfs send -f` command.
The command will include a parent subvolume or clone sources so the resulting
file can contain incremental information to be used.  Finally, no `btrfs
receive` operation is performed, only the file is created. It is left to the
user to use the resulting file as they deem appropriate.

Alternatively, the option can be placed in a configuration file and assigned to
an archive like so:

```
    [archive1]
    subvolume = "/home/user/"
    send_to_file = "/home/user/btrfsfiles"
```

The option is mutually exclusive with the `--snaps-only` option and when used
on the command line, only 1 `archive=subvolume` may be specified.  If either of
these options is also used in the command line then bubtrsnap exits with an
error.

Option: receive-from-file
-------------------------

The companion to `send-to-file` is the `receive-from-file` option.  It allows
bubtrsnap to restore (or initially populate) a backup location from one or more
btrfs send-stream files previously created with `btrfs send -f` (or with
bubtrsnap’s own `--send-to-file`).

On the command line the option accepts either a single stream file **or** a
directory containing stream files:

```
    bubtrsnap --backup-dir=/backup --receive-from-file=/path/to/stream.btrfs
    bubtrsnap --backup-dir=/backup --receive-from-file=/path/to/stream-directory
```

When a directory is given, bubtrsnap scans it, lists every valid btrfs stream
file it finds (in sorted name order) and receives each one in turn.  Stream
validity is checked with `btrfs receive --dump -f`.

Important behaviours:

* `--backup-dir` (or the corresponding `backup_dir` setting) is mandatory.
* The option is mutually exclusive with both `--snaps-only` and `--send-to-file`.
* When used on the CLI, **only** the receive operation is performed; any archives
  defined in a configuration file are ignored.
* If a received subvolume already exists in the destination, a warning is printed
  to stderr and processing continues with the remaining files.  Any other error
  from `btrfs receive` aborts the run.
* After a successful receive the configured keep policy is applied to the
  newly-created subvolume (the archive name is taken from the conventional
  `archive.YYYYMMddhhmm` naming).

In a configuration file the option may appear either globally or inside an
archive section.  The global setting can be a directory or a specific stream
file.  When set in an archive section, it **must** be a specfic stream file.

* Global Example(with directory setting):
```
    receive_from_file = "/path/to/streams"

    [archive1]
    subvolume = "/home/user"
```

* Archive Example:
```
    receive_from_file = "/path/to/archive1.202608181200.btrfs"
```

When specified globally it is processed first, before any archives.  When
specified on an individual archive, snapshot creation and the associated
pre-/post-snapshot hooks are skipped; only the receive, the post-backup hook
(if any) and the keep policy are executed.

Keep Policy
-----------

The keep policy for bubtrsnap is similar to [borgbackup][].  It uses hourly,
daily, weekly, monthly and yearly timeframes to determine what to keep.  The
relevant options are `keep-hourly`, `keep-daily`, `keep-weekly`, `keep-monthly`
and `keep-yearly` and the value assigned is the number of snapshots and backups
to keep at that particular timeframe. The timeframes are applied from shortest
to longest and there is no overlap, meaning a snapshot/backup kept because of a
daily keep doesn't count towards a weekly or monthly keep.  The keep is ALWAYS
the most recent available for a given timeframe.

When first starting up, a given keep timeframe will not apply until that
timeframe becomes relevant as keeps accumulate.  So monthly keeps will not
apply until snapshots and backups have filled all hourly, daily or weekly keep
requirements.

If there is an existing set of backups, then the keep policy will be applied to
all those and only those snapshot and backups that meet the keep policy
criteria will be kept.  Note that the keep policy applies to BOTH snapshots AND
backups.

An example of a configuration file with a keep policy:

```
    snapshot_dir = "/pool/snapshots"
    backup_dir = "/backup"
    
    keep_daily = 4
    keep_weekly = 2
    keep_monthly = 1

    [archive1]
    subvolume = "/path/to/subvolume1"

```

The shortest keep policy time interval is 1 hour fo bubtrsnap.  So multiple
backups within the same hour will be subject to pruning by any keep policy.
The most recent snapshot/backups from that hour will be kept in those cases.

[borgbackup]: https://borgbackup.org

Hooks
-----

Hooks allow for external programs to be coordinated with the creation of
snapshots and backups.  There are 3 types of hooks:  pre-snapshot,
post-snapshot and backup.  All referring to the timing when the hooks are run.
The idea was to facilitate creating an all-in-one-place backup solution so that
snapshot or backup creation could be paired with backing up to a remote server.
Or some kind of pre-processing or massaging could be done prior to taking
snapshots.

Hooks can only be configured in a configuration file.  They are not availabe on
the command line.  The configuration values for the file are as follows:

+ `pre-snapshot-hooks` - a list of commands to run
+ `post-snapshot-hooks` - a table of commands where the key is an archive name
+ `post-backup-hooks` - a table of commands where the key is an archive name

To make the hooks more useful, it is possible to use substitution strings when
creating a hook command.  bubtrsnap will parse the command and swap in the
appropriate value for the substitution string.  Following are lists of the
substitution strings availabe for each type of hook.

pre-snapshot-hook:
+ `{archive}` - the name of the current archive is substituted
+ `{subvol}` - the name of the subvolume associated with the archive substituted
+ `{timestamp}` - the timestamp for the current run of bubtrsnap is substituted
+ `{backupdir}` - the configured backup directory is substituted
+ `{snapshotdir}` - the configured snapshot directory is substituted

post-snapshot-hook:
+ `{snapshot}` - the full path and name of the snapshot is substituted
+ `{archive}` - just the name of the archive, no path, is substituted
+ `{timestamp}`
+ `{backupdir}`
+ `{snapshotdir}`

post-backup-hook:
+ `{backup}` - the full path and name of the backup is substituted
+ `{snapshot}`
+ `{archive}`
+ `{timestamp}`
+ `{backupdir}`
+ `{snapshotdir}`

An example of a configuration file with some hooks in it:

```
    snapshot_dir = "/pool/snapshots",
    backup_dir = "/backups/,

    [archive1]
    subvolume = "/home/user1",
    pre_snapshot_hook = echo "Starting snapshot and backup process- {timestamp}" > ~/backuplog
    post_snapshot_hook = "borg create --verbose --list --filter AME user@server:repo::{archive} {snapshot} 2>~/borglog"
    post_backup_hook = 'echo "Can't think of anything more original to show here."'

    [archive2]
    subvolume = "/usr/local/cloud",
    pre_snapshot_hook = /home/user/myspecialprebackupscript
    post_backup_hook = "borg create user@server:repo::{archive} {backup} 2>>~/borglog"
    .
    .
    .
    .
}
```

The `pre_snapshot_hook` is run prior to any snapshots and can be thought of
like preprocessing for the archive.  In the above example, it is trivial just
posting a message to a log file.  Regardless, it does show a substitution usage
for `archive1`. It invokes a timestamp substitution, so the output of the echo
would actually be something like `Starting snapshot and backup process-
202608122148.`  The `archive2` entry would run the named script.

A `post_snapshot_hook` is executed after the archive snapshot is done.  In the
example above, the hook for `archive1` shows how a potential `borgback` could
be launched, using the just taken snapshot as the source for a backup to a
remote server.  bubtrsnap will make sure that it does not exit until the borg
process is completed.  The association with `archive1` gives the user access to
the extra substitutions such as `{snapshot}`. In this case, `{archive}` would
become `archive1` in the actual command and `{snapshot}` would become
`/pool/snapshots/archive1.202006122148`. (Note I just made up the timestamp
value.  Obviously this would be different when actually run.)

Finally, the `post_backup_hook` executes the given hook after the backup
(`btrfs send`) is completed.  The `{backup}` substitution undeer `archive2`
above would work out to be `/backups/archive2.202006122148` with the same
caveat as before applying to the timestamp portion of the name.

All hooks are run on a per-archive basis.  So each archive is completely
processed, including any associated hooks, prior to the processing of the next
archive.

