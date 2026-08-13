bubtrsnap
=====

Based on my previous [btrbu][] project, bubtrsnap is a rewrite of it using Grok.  It is a python3 based project and is an enhanced version of btrbu with some minor differences.

More formally, bubtrsnap is another btrfs snapshot and backup script.  Like btrbu before it, bubtrsnap will bootstrap itself if no backups exist and uses a keep policy similar to that of borg (ie- specifying a number of keeps at different time intervals).  For the simplest cases, it can be used straight from the command line but for more sophisticated needs it uses a TOML formatted configuration file with a default location in a users `~/.config/`.

The information bubtrsnap needs is minimal- a snapshot directory, an archive name and a subvolume that the archive name is associated with.  If a backup is desired, then a backup path must also be specified.  It is a btrfs specific utility and takes advantage of the `send` and `receive` commands to aid with making backups.  From a configuration file, hooks are availabe at different stages to enhance it's capabilities via scripts or other command line utilities.

As with btrbu, bubtrsnap remains largely dependency free.  It uses no data files or databases in order to perform its duties.

[btrbu][https://github.com/lama7/btrbu]

Usage
-----

For simple snapshot and backup needs, a command can be as simple as:

    bubtrsnap --snapshot-dir=/pool/snapshots --backup-dir=/backup archive1=/path/to/subvolume

Assuming a start from nothing, this will take a snapshot of `/path/to/subvolume` and place it in `/pool/snapshots` with a timestamp suffix.  So the snapshot name will be of the form `archive.YYYYMMddhhmm`.  This snapshot will then be used to send a full backup to `/backup`. 

Subsequent use of this same command will result in incremental backups which will just advance the timestamp associated with the archive.  It defaults to keeping the most recent snapshot and backup.  See [keep policy][] below for how to change the keep behavior.

[keep policy]: #keep-policy

More than 1 archive can be specified on the command line:

    bubtrsnap --snapshot-dir=/pool/snapshots --backup-dir=/backup archive1=/path/to/subvolume archive2=/path/to/subvolume2

These archives will share the same snapshot and backup destination, but obviously have different names qualified with a timestamp.

For a full list of options and their explanations, `bubtrsnap --help` is useful.

Configuration Files
-------------------

At some point, if backing up several different subvolumes for instance, a configuration file might become desirable to make the command line more manageable.  bubtrsnap will look for a configuration file in `~/.config/bubtrsnap.toml` if no file is specified on the command line.  Alternatively:

    bubtrsnap --config=/path/to/myconfig

Configuration files use TOML formatting and look like:

```
    # a comment...
    snapshot_dir = "/pool/snapshots",
    backup_dir = "/backup",

    keep_daily = 5

    # archives are under a header
    [archives]
    archive1="/path/to/subvolume1",
    archive2="/path/to/subvolume2",
    archive3="/path/to/subvolume3",

```

If an option can be specified on the command line, it can also be specified in the configuration file.  Be sure to susbstitute a `'_'` for any `'-'` characters in the command line option.  So the `--snapshot-dir` option would be specified as `snapshot_dir` in a configuration file.

Configuration files and command line options can be mixed and matched as well.  The rule is that the command line overrides any configuration file settings.  For archive name/ subvolume pairs, anything specified on the command line overrides ALL of the subvolumes in the configuration file.  So if there are 5 subvolumes specified in the configuration file, but a single achive/subvolume is specified on the command line, only the command line archive/subvolume is dealt with.  Basically, bubtrsnap assumes the user knows what they are doing and tries not to get in the way.

Keep Policy
-----------

The keep policy for bubtrsnap is similar to [borgbackup][].  It uses hourly, daily, weekly, monthly and yearly timeframes to determine what to keep.  The relevant options are `keep-hourly`, `keep-daily`, `keep-weekly`, `keep-monthly` and `keep-yearly` and the value assigned is the number of snapshots and backups to keep at that particular timeframe. The timeframes are applied from shortest to longest and there is no overlap, meaning a snapshot/backup kept because of a daily keep doesn't count towards a weekly or monthly keep.  The keep is ALWAYS the most recent available for a given timeframe.

When first starting up, a given keep timeframe will not apply until that timeframe becomes relevant as keeps accumulate.  So monthly keeps will not apply until snapshots and backups have filled all hourly, daily or weekly keep requirements.

If there is an existing set of backups, then the keep policy will be applied to all those and only those snapshot and backups that meet the keep policy criteria will be kept.  Note that the keep policy applies to BOTH snapshots AND backups.

An example of a configuration file with a keep policy:

```
    snapshot_dir = "/pool/snapshots"
    backup_dir = "/backup"
    
    keep_daily = 4
    keep_weekly = 2
    keep_monthly = 1

    [archives]
    archive1="/path/to/subvolume1"
    archive2="/path/to/subvolume2"
    archive3="/path/to/subvolume3"

```

bubtrsnap does NOT have a keep policy below a 1 hour timeframe.  So multiple backups within the same hour will be subject to pruning by any keep policy.  The most recent snapshot/backups from that hour will be kept in those cases.

[borgbackup]: https://borgbackup.org

Hooks
-----

Hooks allow for external programs to be coordinated with the creation of snapshots and backups.  There are 3 types of hooks:  pre-snapshot, post-snapshot and backup.  All referring to the timing when the hooks are run.  The idea was to facilitate creating an all-in-one-place backup solution so that snapshot or backup creation could be paired with backing up to a remote server.  Or some kind of pre-processing or massaging could be done prior to taking snapshots.

Hooks can only be configured in a configuration file.  They are not availabe on the command line.  The configuration values for the file are as follows:
+ `presnaphooks` - a list of commands to run
+ `postsnaphooks` - a table of commands where the key is an archive name
+ `backuphooks` - a table of commands where the key is an archive name

To make the hooks more useful, it is possible to use substitution strings when creating a hook command.  bubtrsnap will parse the command and swap in the appropriate value for the substitution string.  Following are lists of the substitution strings availabe for each type of hook.

presnapshot:
+ `{archive}` - the name of the current archive is substituted
+ `{subvol}` - the name of the subvolume associated with the archive substituted
+ `{timestamp}` - the timestamp for the current run of bubtrsnap is substituted
+ `{backupdir}` - the configured backup directory is substituted
+ `{snapshotdir}` - the configured snapshot directory is substituted

postsnapshot:
+ `{snapshot}` - the full path and name of the snapshot is substituted
+ `{archive}` - just the name of the archive, no path, is substituted
+ `{timestamp}`
+ `{backupdir}`
+ `{snapshotdir}`

backup:
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

    [archives]
    archive1 = "/home/user1",
    archive2 = "/usr/local/cloud",

    [pre-snapshot-hooks]
    archive1 = echo "Starting snapshot and backup process- {timestamp}" > ~/backuplog
    archive2 = /home/user/myspecialprebackupscript

    [post-snapshot-hooks]
    archive1 = "borg create --verbose --list --filter AME user@server:repo::{archive} {snapshot} 2>~/borglog"

    [post-backup-hooks]
    archive1 = 'echo "Can't think of anything more original to show here."'
    archive2 = "borg create user@server:repo::{archive} {backup} 2>>~/borglog"
    .
    .
    .
    .
}
```

The `pre-snapshot-hooks` in the above example is trivial.  It does show a substitution usage. It invokes a timestamp substitution, so the output of the echo would actually be something like `Starting snapshot and backup process- 202006122148.`  The second entry would run the named script.

The `post-snapshot-hooks` shows how a potential `borgback` could be launched, using the just taken snapshot as the source for a backup to a remote server.  bubtrsnap will make sure that it does not exit until the borg process is completed.  The association with `archive1` gives the user access to the extra substitutions such as `{snapshot}`. In this case, `{archive}` would become `archive1` in the actual command and `{snapshot}` would become `/pool/snapshots/archive1.202006122148`. (Note I just made up the timestamp value.  Obviously this would be different when actually run.)

Finally, the `post-backup-hooks` shows multiple hooks, one associated with each archive.  The `{backup}` substitution would work out to be `/backups/archive2.202006122148` with the same caveat as before applying to the timestamp portion of the name.

All hooks are run on a per-archive basis.  So each archive is completely processed, including any associated hooks, prior to the moving on to the next archive.
