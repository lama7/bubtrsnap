bubtrsnap
=========

Based on my previous [btrbu][] project, bubtrsnap is a rewrite of it using
Grok.  It is a python3 based project and is a superior implementation, imho.

More formally, bubtrsnap is a(nother) btrfs snapshot and backup management
script.  Like btrbu before it, bubtrsnap will bootstrap itself if no backups
exist and uses a keep policy similar to that of borg (ie- specifying a number
of keeps at different time intervals).  For the simplest cases, it can be used
straight from the command line but for more sophisticated needs it uses a TOML
formatted configuration file with a default location in a users `~/.config/`.

The information bubtrsnap needs is minimal- a snapshot directory, an archive
name and a subvolume that the archive name is associated with.  If a backup is
desired, then a backup path must also be specified.  It is a btrfs specific
utility and takes advantage of the `send` and `receive` commands to aid with
making backups.  From a configuration file, hooks are available at different
stages to enhance it's capabilities via scripts or other command line
utilities.  For instance, a snapshot can be taken and then a hook used to
invoke a [borgbackup][] command using the just created snapshot as a source.

As with btrbu, bubtrsnap remains largely dependency free.  It uses no data
files or databases in order to perform its duties.  It's main dependency is a
python3 installation.

[btrbu]: https://github.com/lama7/btrbu

## Usage

For simple snapshot and backup needs, a command can be as simple as:

    bubtrsnap --snapshot-dir=/pool/snapshots --backup-dir=/backup archive1=/path/to/subvolume

Assuming a start from nothing, this will take a snapshot of
`/path/to/subvolume` and place it in `/pool/snapshots` with a timestamp suffix.
So the snapshot name will be of the form `archive.YYYYMMddhhmm`.  This snapshot
will then be used to send a full backup to `/backup`. All of these locations **must**
be valid btrfs subvolumes.

Subsequent use of this same command will result in incremental backups which
will just advance the timestamp associated with the archive.  By default,
previous snapshots and backups are retained so that bubtrsnap can take
advantage of btrfs' incremental send and receive capabilities.  Each prior
snapshot and backup becomes either a parent or source.  See [keep policy][]
below for how to change the keep behavior.

[keep policy]: #keep-policy

More than 1 archive can be specified on the command line:

    bubtrsnap --snapshot-dir=/pool/snapshots --backup-dir=/backup archive1=/path/to/subvolume archive2=/path/to/subvolume2

These archives will share the same snapshot and backup destination, but
obviously have different names qualified with a timestamp.  

All archive related processing is completed before the next one is processed.
Normal archive processing start with pre-snapshot hooks followed by the
snapshot, post-snapshot hooks, backup, post-backup hooks and then any keep
policy is applied.

For a full list of options and their explanations, `bubtrsnap --help` is
useful.

## Configuration Files

At some point, if backing up several different subvolumes for instance, a
configuration file will make the command line more manageable.  bubtrsnap will
look for a configuration file in `~/.config/bubtrsnap.toml` if no file is
specified on the command line.  Alternatively:

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
the configuration file.  CLI options map to configuration settings by
susbstituting a `'_'` for any `'-'` characters in the command line option.  So
the `--snapshot-dir` option would be specified as `snapshot_dir` in a
configuration file.

Configuration files for bubtrsnap have a global section and then archive
sections.  Global settings in a configuration file apply to all archives
declared in the file and any archive declared on the command line.  So setting
default `snapshot_dir` and `backup_dir` settings is a good way to go to keep
CLI entry to a minimum.  Any configuration file setting is over-ridden by a CLI
option specification.  So if setting up a new archive, setting a new
`backup_dir` on the command line can be done without having to comment out
things in the configuration file.  The order of precedencee is CLI > archive
specific > global.

Using the above configuration snippet as an reference, the following command is
now possible:

    bubtrsnap archive1

The above command would cause only archive1 to be processed.  Normal
configuration settings still apply, except because the CLI has highest
precedence, bubtrsnap will only process the named archive in the commmand.  To
process all archives in the configuration file:

    bubtrsnap

or to process 2:

    bubtrsnap archive2 archive3

Want to change the keep policy for all archives?

    bubtrsnap keep-daily 5 keep-weekly 2

Note that this policy is only used for that 1 command.  No alterations are made
to the configuration file.  Also, any keep intervals not specified are set to 0
so no archives will be kept at hourly, monthly or yearly intervals in the above
example.

Archive sections are defined by a header which names the archive and then a
`subvolume = "/path/to/subvolume"` entry.  Within an archive section, all key
value pairs are specific to that archive.  Options set in the archive take
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
+ `verbose`
+ `send_to_dir`
+ `receive_from_dir`

The following options can only be used with an archive:

+ `subvolume`  # required for an archive
+ `send_to_file`
+ `receive_from_file`
+ `pre_snapshot_hook`
+ `post_snapshot_hook`
+ `post_backup_hook`

The `verbose` option has several levels for increased messaging on the CLI or
for logging purposes if running bubtrsnap from a cron job.  By default,
bubtrsnap is quiet and will only report errors.  Verbose 1 will report general
flow messages.  Increasing the number will cause `btrfs` commands to be
reported.  To be able to get the maximum information on flow and commands, use
`debug` on the CLI.

Finally, the `dry-run` option is not honored in the configuration file.  Simply
add it the the CLI to see how bubtrsnap will proceed with a configuration.

[keep policy]: #keep-policy

## Stream Files: send_to_file/receive_from_file

It is possible to take advanage of btrfs' ability to send to or to receive from
a file using the appropriately named `send_to_file` and/or `receive_from_file`
options.  This is to facilitate backups of very large archives where a raw
send-receive could get interrupted due to the time it takes for the transfer.

Both of these options take a file name for an argument.  In the case of
`send_to_file` the file names the destination file for the stream data.  This
file cannot be a pre-existing file.  For `receive_from_file` the file names the
source for a `btrfs receive` operation and the destination will be
`backup_dir`.  The options can be used individually or together on the CLI.
When `send_to_file` is specified no backup processing will be performed (no
`btrfs receive`).  Processing will stop when post-snapshot hooks are complete.
In the case of `receive_from_file`, the snapshotting steps are skipped and
processing **STARTS** at the backup step.  Any post-backup hooks will be
processed as well.  In both cases, the keep policy will be applied to the
appropriate area.  If both are used on the CLI, then processing is normal with
the exception that the stream file is used essentially as a staging step.  When
specifying both, the same file **MUST** be named for both options.

An example CLI command (assuming a configuration file is setup):

    bubtrsnap --receive-from-file ~/btrfsstreams/archive.btrfs

or using both:

    bubtrsnap --send-to-file ~/btrfsstreams/archive.btrfs --receive-from-file ~/btrfsstreams/archive.btrfs archive1

Alternatively, the options can be placed in a configuration file and assigned to
an archive like so:

```
    [archive1]
    subvolume = "/home/user/"
    send_to_file = "/home/user/btrfsstreams/archive1stream.btrfs
```

or together:

```
    backup_dir = "/pool/backups"
    snapshot_dir = "/snapshots"
    sudo = true

    [archive1]
    subvolume = "~/another/silly/path"
    send_to_file = "/home/user/btrfsstreams/archive1stream.btrfs
    receive_from_file = "/home/user/btrfsstreams/archive1stream.btrfs"
    .
    .
    .
```

The options are mutually exclusive with the `--snaps-only` option and when used
on the command line, only 1 `archive=subvolume`, or alternatively the name of
an archive section in the configuration file, may be specified.  

## Stream Directories: send_to_dir/receive_from_dir

If you wish for stream files to be used with multiple archives, then
`send_to_dir` and `receive_from_dir` are available.  These are similar to their
file counterparts.  They are available from the CLI or a configuration file.
They are a global only setting in a configuration file.  Also, no mixing and
matching of the `dir` and `file` options are allowed.

From a usage standpoint, they result in generally the same processing except
that all send and receive operations will be through stream files in the
specified directories.  The `send_to_dir` will write a file with a name like
`archivename.YYYYMMDDHHMMSS.btrfs`.  The `receive_from_dir` will scan the
directory for the most recent stream file that matches the current archive
being worked on.  The received file will go into `backup_dir`.  Again, they can
be specified individually or together.  If both are specified, the file
resulting from the send will be used for the ensuing receive operation.  These
options are also mutually exclusive with the `snaps-only` option.

An examples for the CLI:

    bubtrsnap --send-to-dir ~/btrfsstreams/ archive1 archive2 archive3=/some/subvolume

So archive1, archive2 and archive3 (which isn't setup in the configuration
file) will all have stream files put into `~/btrfsstreams/` which must
pre-exist.  

In a configuration file:

```
    backup_dir = "/pool/backups"
    snapshot_dir = "/snapshots"
    sudo = true

    receive_from_dir = "~/btrfsstreams/"

    [archive1]
    subvolume = "~/another/silly/path"
    keep_daily = 7
    .
    .
    .
```

In this instance, any archives in the configuration file will skip snapshot
processing and a btrfs stream file will be searched for in the specified directory. 
If a stream file is not found, processing for that archive completes and the
next archive is dealt with.

## Keep Policy

The keep policy for bubtrsnap is a direct port from [btrbu][] which was
inspired by the [borgbackup][] policy.  It uses hourly, daily, weekly, monthly
and yearly timeframes to determine what to keep.  The relevant options are
`keep-hourly`, `keep-daily`, `keep-weekly`, `keep-monthly` and `keep-yearly`
and the value assigned is the number of snapshots and backups to keep at that
particular timeframe. The timeframes are applied from shortest to longest and
there is no overlap, meaning a snapshot/backup kept because of a daily keep
doesn't count towards a weekly or monthly keep.  The keep is ALWAYS the most
recent available for a given timeframe.

When first starting up, a given keep timeframe will not apply until that
timeframe becomes relevant as keeps accumulate.  So monthly keeps will not
apply until snapshots and backups have filled all hourly, daily or weekly keep
requirements.

If there is an existing set of backups, then the keep policy will be applied to
all those and only those snapshot and backups that meet the keep policy
criteria will be kept.  Note that the keep policy applies to BOTH snapshots AND
backups.

Keeps can be specified on the CLI or via a configuration file.  Keeps are dealt
with as all or nothing.  Keeps specified on the CLI, in the global section or
for a specific archive are independent of each other.  Keeps from the CLI take
precedence over global and archive settings while archive specific keeps take
precedence over global settings.  Any unspecified keep is set to 0 at that
interval. As an example, consider the following CLI command:

    bubtrsnap --keep-daily 5 myarchive=/some/subvolume

Assuming that a snapshot directory and backup directory are properly configured
in a configuration file, this command will only keep 5 daily snapshots for
`myarchive`.  Any previous weekly, hourly, monthly and yearly snapshots and
backups will be pruned.  Always perform a `--dry-run` to test the results of a
new keep policy.
    
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

The shortest keep policy time interval is 1 hour for bubtrsnap.  So multiple
backups within the same hour will be subject to pruning by any keep policy.
The most recent snapshot/backups from that hour will be kept in those cases.

### Default (no keep options set)

If no `keep_*` values are set on the CLI or in the config file (all remain
`0`), **pruning is skipped**. Existing snapshots and backups are left as they
are.

### Weekly boundary

**Weekly keeps are aligned to Saturday** (23:59), matching btrbu. The week
boundary used when selecting weeklies is the most recent Saturday at or before
a candidate timestamp.

### Other boundaries

| Interval | Boundary used when walking backward |
|----------|-------------------------------------|
| Hourly   | Previous hour at minute 59          |
| Daily    | Previous calendar day at 23:59      |
| Weekly   | Saturday 23:59                      |
| Monthly  | Last day of the target month 23:59  |
| Yearly   | December 31 23:59                   |

[borgbackup]: https://borgbackup.org

## Hooks

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

