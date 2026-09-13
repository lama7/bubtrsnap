# Feature Addition: rsync

Currently, bubtrsnap relies on scp for it's file transport when using a stream
file for backup purposes to a remote host.  The scp program has the advantage
of being available with a majority of ssh installations but it does not handle
an interrupted file transfer.  Thus, I want to take advantage of rsync's
ability to handle an interrupted file transfer using it's `--partial` or
`--partial-dir` options.

## Implementation Details

Note when reading that rsync is solely to replace scp as a way to copy a stream
file to a remote host.  It does not affect piped send/receive operations at
any point.

### An Option not a Replacement

We will maintain the use of scp and implement rsync as an opt-in optional
feature.  If no rsync related options are specified, we will rely on scp as its
currently implemented.

### rsync Option

+ On the CLI and in the configuration file the option will simply be `--rsync`. 

+ If used on the CLI, the usual precedence rules apply and rsync will be used
for all remote file transfers for that run of bubtrnap.

+ In a configuration file, we will allow it to be specified globally or per 
archive.  Again, we will use our usual precedence rules to resolve any conflicts.

+ If the option is specified by itself on the CLI, or as `rsync = true` in the
configuration file we will use default rsync options.

+ We will also allow for an `--rsync-opts` option which will accept a string 
specifying the user's preferred rsync options.

+ The default options for rsync will be `-a` (or `--archive`) and `--partial-dir`

+ For now, we will always add `--partial-dir` to the rsync command.  We will note 
this in the documentation.

### Program Flow

1. Check if a previous bubtrsnap run was interrupted resulting in an incomplete
file transfer by testing for the existence of the remote partial-dir
(`/tmp/.bubtrsnap-partial`) via SSH.

2. If not, then process as normal.

3. If so, rsync with `--partial-dir` automatically resumes the interrupted
transfer (reconstructing the same command from config at resume time — no
stored state).  rsync detects the partial-dir from the previous run and
completes the transfer.

4. When the transfer completes, the remote `btrfs receive -f` command is run
with the now completed file and processing of that archive finishes.

5. Normal archive processing continues for the current run.

### Miscellaneous

- We filter out dangerous rsync options or options that produce STDOUT feedback
like `-v` or `--progress` from the user-specified `--rsync-opts`.  Blocked
options are reported to the user via stderr.
- `--partial-dir` is always added by bubtrsnap (using `.bubtrsnap-partial`)
and cannot be overridden by user options — attempts to set it are blocked
with a warning.
- rsync always uses `-a` (archive mode) as the base option.
- The remote temp file path (`/tmp/bubtrsnap-<streamname>`) is the same as
the SCP path for consistency, so interrupted rsync and SCP transfers use the
same destination.

### Testing

All tests mock `bubtrsnap.run` (the helper itself) and assert on `run()`
call arguments, following the existing test patterns in `test_ssh.py`.
Test categories:
- `TestRsyncConfigPrecedence`: CLI > archive > global precedence
- `TestRsyncOptionsFilter`: blocklist filtering of `--rsync-opts`
- `TestRsyncAndReceive`: `_rsync_and_receive` dry-run, command structure,
  partial-dir detection, failure paths, local_sudo handling
- `TestReceiveRemoteRsyncRouting`: routing from `_receive_remote` to
  `_rsync_and_receive` vs `_scp_and_receive`
