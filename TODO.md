# bubtrsnap bug backlog — reconciled audit findings (2026-10-16)

Consolidates FINDINGS.md (2026-10-14 audit of branch `maintain` @ 4dedd35)
and HANDOFF.md (2026-10-16 status). Open items were re-verified against
`maintain` (v0.4.2.dev0). Items reference function names, not line numbers,
since lines shift as fixes land.

Original audit evidence traces: ~/.hermes/cache/delegation/live/deleg_d5fc1199/task-{0,1,2}.log
Full audit report: ~/.hermes/cache/scratch/bubtrsnap-audit-2026-10-15.md

## Branch workflow

- `main` — shipping branch; currently at the v0.4.1 tag. FF from `maintain`
  only at ship time, then tag, GitHub release, PyPI publish.
- `maintain` — working branch (tip 29ed2cf, VERSION 0.4.2.dev0). All DONE
  items below are committed here and pushed to the `stryder` remote; stryder
  runs this build via an editable pipx install from /home/lama7/src/bubtrsnap.
  `bubtrsnap --version` now also prints the branch/commit it was built from,
  so a dogfooded build identifies itself (see item 26).
- Nothing reaches GitHub until a release is declared.

## SHIPPED in v0.4.1 (tag pushed, GitHub release + PyPI published)

- 353270b  `--stage-file` alone was rejected by its own mutual-exclusion check
- 32a2f01  store_true flags in _MAINT_VALUE_TAKERS swallowed the subcommand
- 8d5b76e  per-archive week_startday accepted but never used
- 6453708  rebuild planned a restore into literal 'None' when backup_dir unset
- e767bd1  CLI --week-startday now beats per-archive value (CLI > archive > global)

## REMAINING — functional bugs (all fixed on maintain, with tests)

1. DONE (b954c6d): piped_run now captures both stderr streams, waits on both
   processes, and returns the first non-zero rc with labeled errors; display
   line uses _quote_for_display. Tests in unittests/test_piped_run.py (7).
   Live-verified: failed send reports "send failed: ERROR: unable to resolve ...".

2. DONE (b836c3c): _receive_local now parses "At subvol" from the real
   receive output via shared _parse_at_subvol() (stdout+stderr, all three
   receive paths); stem is a warned fallback only; receive_stream warns when
   the received name belongs to a different archive using the formerly-dead
   archive_name_from_received(). Tests in unittests/test_receive_names.py (14).

3. DONE (02fc1b0): _run_backup now catches SystemExit as well as Exception
   per archive, logs the failed archive, continues with the rest, and returns
   1 with a summary of failed names. _run_backup coverage gap addressed by
   unittests/test_run_backup_errors.py (4).

4. DONE (61fbefa): _receive_and_post now gates only the hook on `received`;
   keep policies always run (idempotent pruning). Tests:
   TestReceiveAndPostGate in unittests/test_keep_policy.py (3).

5. DONE (7b2ba5d): partial-dir is now per-archive
   (.bubtrsnap-partial-<name>, sanitized); detection and rsync both use it.
   Tests: unittests/test_partial_dir.py (5). Orphan /tmp/.bubtrsnap-partial
   dirs from older versions remain on remotes but are inert.

6. DONE (7b75d30): _run_prune now counts actual successful deletes and
   reports that; logs a note when it differs from the preview.
   Test: TestPruneActualCount in unittests/test_keep_policy.py.

7. DONE (26f3047): archive-level file+dir mixing now hard-errors, mirroring
   the CLI blanket rule; README documents it. Tests: TestFileDirMixing in
   unittests/test_file_options.py (6).

8. DONE (6f713e9): dead `(send_file or send_dir) and remote and remote_dir`
   gate inside the plain-backup elif (the elif chain guarantees both are None
   there) and the no-op `if recv_file and send_file and not staged:
   recv_path = ...` reassignment removed; dead `dest =` stores in the export
   branches dropped. Guarded by TestPlainBackupBranch in
   unittests/test_runtime.py (2).

9. DONE (4afd32a): run() docstring now states read_only dry-run returns the
   CompletedProcess (matches the code).

## REMAINING — validation / precedence gaps (10, 11 fixed)

10. DONE (97e934a): negative keep counts rejected. Probe first, as requested:
    `build_keep_set` treats a negative count exactly like 0 (30 daily
    snapshots, `keep_daily: -5` vs `keep_daily: 0` produce identical keep and
    prune sets), so the FINDINGS "over-prune" claim does not hold and the keep
    algorithm was left untouched. `_validate_keep_count` now refuses negatives
    in global config, archive sections, and CLI `--keep-*`; `_KEEP_COUNT_KEYS`
    is the single list of the five keys. Tests:
    unittests/test_config_validation.py `TestKeepCountValidation` (5).

11. DONE (307b4cf): CLI --forced-keep values are format-validated. Config
    values were checked by validate_config, but `_merge_keep` accepted any
    string and malformed CLI timestamps were warn-and-ignored in
    `_apply_keep_policy_core`. Shared `_is_valid_forced_keep()` /
    `_validate_forced_keep()` now check every timestamp from either source, so
    a bad `--forced-keep` is a hard error naming the offending value. Tests:
    unittests/test_config_validation.py `TestForcedKeepValidation` (6).

32. DONE (d78d51f): a blocked rsync option left its argument orphaned.
    `_filter_rsync_opts` stripped `--exclude` but kept the following token, and
    rsync read it as a positional source:
    `rsync -a --partial-dir .p /orphan_token …` →
    `link_stat "/orphan_token" failed`. `_RSYNC_OPTS_WITH_VALUE` lists the
    blocked long options that take a separate argument, and the filter now
    consumes the value too (the `--opt=value` form is self-contained, and a
    value that is itself an option is not swallowed). Tests: 7 added to
    `TestRsyncOptionsFilter` in unittests/test_ssh.py. Related but still open:
    item 14 (`-e` and `--config` are not blocked at all — verified,
    `--rsync-opts '-e "ssh -oProxyCommand=id" --config=/tmp/x'` passes through).

## REMAINING — security hardening (15 and 16 fixed on 2026-10-22; 12, 13, 14
still deferred by decision)

12. Untrusted remote output into shell: 'At subvol' parsed via
    _parse_at_subvol() from remote stdout/stderr enters the hook mapping
    built in process_archive and is interpolated raw into a shell=True
    command in run_hook(). A compromised remote can inject local shell
    commands. (shell=True for user-configured hook templates is accepted
    design; the remote-output path is the risk.) Overlaps improvement:
    run_hook() substitutions are unquoted, so paths with spaces break.

13. Unquoted SSH remote command strings: build_ssh_cmd() joins args into one
    string re-parsed by the remote shell — config paths with metacharacters
    execute remotely (_run_prune deletes, _apply_keep_policy_core deletes,
    _remote_disk_free/_remote_size_of df/du). remote_host starting with '-'
    is parsed as an ssh option (e.g. -oProxyCommand=). Remote listing output
    is re-injected into later ssh commands in prune/rebuild.
    Fix: shlex.quote in build_ssh_cmd + reject remote_host starting with '-'.

14. rsync blocklist bypass: _filter_rsync_opts blocks --rsh but not the short
    form -e, and --config is not blocked. Verified: --rsync-opts
    '-e "ssh -oProxyCommand=id" --config=...' passes. Add -e/--config or
    switch to an allow-list.

15. DONE (4cf896f): the remote stream temp file is no longer predictable.
    `_remote_tmp_path()` builds `/tmp/bubtrsnap-<sanitized stream name>-<16 hex>`
    from `secrets.token_hex(8)`, so a remote user cannot pre-create the path as a
    symlink (scp wrote through it and `rm -f` removed only the link) or as a
    directory, and two archives whose streams share a basename no longer collide.
    `_receive_on_remote()` now runs `chmod 600` on the remote temp file before
    `btrfs receive` — scp inherits the remote's default mode and rsync copies the
    local file's — and a chmod failure only warns rather than aborting. Residual
    window: the file is still at the remote's default mode *during* the transfer;
    the unpredictable name is what makes it unguessable. A run killed between
    transfer and receive leaves one orphan file per run in /tmp rather than
    reusing the same name. Tests: unittests/test_remote_tmp.py (11); the existing
    SCP/rsync tests in test_ssh.py now count the four-call sequence
    (transfer, chmod, receive, cleanup), and rsynctestbed mirrors the naming.

16. DONE (bc692e9): archive names are validated where they are read.
    `_validate_archive_name()` requires `[A-Za-z0-9][A-Za-z0-9_-]*` for every TOML
    archive section and every `ARCHIVE[=SUBVOLUME]` CLI argument, killing the
    path-escape (`/`, `..`) and glob-metacharacter exposures at the boundary;
    `_archive_snapshots()` and the `_received_uuids_local()` fallback now compare
    names literally through `_archive_items()` instead of `glob(f"{archive}.*")`,
    so a name carrying metacharacters matches nothing even if validation were
    bypassed. README documents the rule. Tests: unittests/test_archive_names.py
    (16).

17. Hooks run with check=False in run_hook(): a failing pre-snapshot hook
    (e.g. DB quiesce) is silently ignored and the snapshot is taken anyway.

## REMAINING — convention violations (all done)

18. DONE (6c00a80): _remote_disk_free/_remote_size_of now use
    run(read_only=True) instead of raw subprocess.run — probes are logged at
    level 2, honour dry-run, and inherit any future run() central changes.

19. DONE (cd19b4d): remove_stage_file now checks the sudo rm returncode; a
    non-zero exit warns to stderr instead of logging success.

20. DONE (538cb6e): the ssh `test -d` recovery check in
    _check_interrupted_rsync now has a 10s timeout; timeout/OSError means
    "no interruption detected" rather than an indefinite hang.

## Improvements (not bugs) — 31 and 33 open

21. DONE (62a710a, 5b8fa1a, 29ed2cf): the fit check now runs only for FULL sends. btrfs cannot
    report an incremental stream's size in advance — verified empirically:
    btrfs-progs 6.14 (stryder) and the 7.1 docs have no --dry-run/size flag,
    `btrfs qgroup` 'excl' is ~16 KiB for a snapshot (the new extents are still
    shared with the live subvolume) and the `rfer` delta missed a 50 MiB
    in-place rewrite (38,952,960 B vs the 52,469,526 B stream), and `btrfs
    filesystem du`'s Total delta under-reads (8.2 MiB vs 19.0 MiB actual).
    Measured over-estimates on stryder: lama7Maildir 4,783,448,064 B snapshot
    vs 19,038,629 B stream (251x), lama7 698,740,109,312 B vs 675,157,964 B
    (1035x). Nothing is cached between runs, per the decision.

    process_archive now discovers the local parents first (the list is passed
    to send_backup via `local_parents=`, so the subvolume listing is not
    repeated): parents exist -> incremental -> `_low_space_warning()` only,
    which never aborts and fires when the destination has less than
    LOW_SPACE_FRACTION (0.25, hard-coded module constant; promoting it to a
    config key is a later nicety) of its capacity free. No parents -> full
    send -> the existing `_space_check()` hard stop, unchanged. `_run_rebuild`
    phase 3 applies the same rule to its local backup restores: the estimate
    is only a hard stop when the restores would be full sends, otherwise it is
    logged as an upper bound and the floor warning is applied.

    Tests: unittests/test_space_check_incremental.py (15). Live A/B on a
    loopback btrfs (received parent, 141.7 MiB free, 195.5 MiB snapshot): old
    code aborted with "need 195.5 MiB but only 141.7 MiB free ... Aborting."
    (exit 1, nothing written); new code warned "only 141.7 MiB free ... 14% of
    1.0 GiB" and the incremental send succeeded (exit 0).

    5b8fa1a finishes the same policy for the remote side. `_run_rebuild` now
    lists the SSH parents once per archive (lazily, only when something is
    actually re-sent) instead of once per timestamp, and uses them for the
    guard: parents on the remote -> incremental -> upper bound logged, floor
    warning, never a skip; no parents -> full send -> `_space_check_remote()`
    can still skip the remote part. The cached parents are also reused by
    phase 4, and `find_parents_ssh` is no longer reached with a None
    snapshot_dir (it used to raise AttributeError there, swallowed as "remote
    re-send error"). `_remote_disk_usage()` returns (free, total) so the remote
    can be judged against its capacity; `_remote_disk_free()` is now a thin
    wrapper. Main-path remote destinations get `_low_space_warning_remote()`
    too — nothing about a remote is ever hard-stopped, because a remote that
    fills up must not cancel a local backup that already succeeded. 29ed2cf
    moves that warning up front so it fires for every route to the remote
    (piped send, scp, rsync), not just the piped one, and the local floor
    message no longer says "incremental" since the floor applies to any send.
    Tests: 17 more in the same file (32 there, 332 total).

22. DONE (8cb58ed): `find_parents()` / `find_parents_ssh()` now resolve UUIDs with
    a single `btrfs subvolume list -u -R -p -o <dir>` per directory instead of
    one `btrfs subvolume show` per candidate. `-u` prints the uuid, `-R` the
    received uuid, `-p` the parent ID, `-o` restricts the listing to subvolumes
    below the search path; the directory's own `Subvolume ID` (one extra
    `subvolume show`) is the parent ID that identifies its *direct* children, so
    a nested subvolume that happens to match `{archive}.*` is not mistaken for a
    snapshot. Verified on btrfs-progs 6.14 (stryder) and 7.1 (thorin); if the
    listing or the ID lookup fails the code falls back to the old per-subvolume
    `show` loop, so older builds keep working. Measured on stryder with the live
    config (lama7Maildir): local parents 18 subprocess spawns → 4, SSH parents
    19 spawns / 10 SSH round trips → 4 spawns / 2 round trips, with identical
    parent lists and ordering (9 local parents, 8 SSH parents). Helpers:
    `_parse_subvolume_list()`, `_get_subvolume_id()`, `_child_uuid_map()`,
    `_archive_snapshots()`, `_received_uuids_local()`, `_received_uuids_ssh()`,
    `_match_parents()`. The maps are deliberately *not* cached: a receive during
    the same run would make them stale. Follow-up f5baa50: `_entry_name()` reads
    the path column from the " path " marker rather than whitespace-splitting the
    line, so a subvolume basename containing a space ("My Docs.202601010000") is
    not truncated to "My" and silently dropped (which would have degraded an
    incremental send into a full one). `TestFindParentsSSH` in
    unittests/test_ssh.py was rewritten with a realistic fake listing (the old
    mock was order-dependent and passed only by accident);
    unittests/test_find_parents.py (15 tests) counts invocations so a regression
    back to the per-subvolume loop fails even when the parent list is still
    correct.

23. DONE (4afd32a): _scp_and_receive and _rsync_and_receive now share a
    single _receive_on_remote() tail (SSH receive, temp cleanup, error
    handling, name extraction); only the transfer command and log label
    differ.

24. FALSE POSITIVE: crc32c is live — _parse_stream_prefix validates the
    stream CRC via it, reached by is_btrfs_stream and stream_snapshot_name.

25. DONE (59c7d16): `--config` is accepted after a subcommand — the
    maintenance sub-parsers share the global option set. Re-verified
    2026-10-22: `bubtrsnap list --config examples/basic.toml` parses the
    config (it only fails on this machine's non-btrfs `/var/lib`). The
    original note was stale.

26. DONE (56ad416) for the dev-build identification half: `build_identity()`
    reads branch, short commit and dirty state from the git checkout the script
    lives in, cached once per process, and `version_string()` renders
    `bubtrsnap 0.4.2.dev0 (maintain 28c32ba)` — printed by `--version` and
    logged at verbosity 2 at the start of every backup or maintenance run, so a
    dogfooded build names itself in bug reports. The hash is deliberately kept
    out of VERSION: PEP 440 rejects `0.4.2.dev28c32ba`, and pyproject.toml reads
    VERSION as the project version. An installed wheel has no git metadata and
    falls back to VERSION alone. Tests: unittests/test_build_identity.py (10).
    Still open as housekeeping: the `version:` field in
    ~/.hermes/skills/software-development/bubtrsnap/SKILL.md needs a policy — it
    reads 0.4.2.dev0 (the dev tip) rather than the latest release; settle it at
    ship time.

## Receive-output parsing (verified 2026-10-21 empirically; both fixed)

27. DONE (09bc88a): `_received_subvol_name()` now takes the name from the
    CRC-validated stream header via `stream_snapshot_name()` (which already
    handles both SUBVOL and SNAPSHOT commands) and uses the receive banner only
    as a cross-check, warning "received-name mismatch" when the two disagree.
    `_parse_at_subvol()` also accepts `At snapshot`. Context: btrfs-progs prints
    `At subvol <name>` on stderr for a FULL send and `At snapshot <name>` on
    stdout for an incremental (parent-bearing) send — confirmed on thorin v7.1
    and stryder v6.14 with real bubtrsnap streams. The old stem fallback was
    cosmetic only because `{archive}.{ts}.btrfs` mirrors the snapshot name; it
    silently reported the wrong name for renamed streams (`export_file =
    "~/lama7Maildir.btrfs"`) and staged streams, which then fed the `{snapshot}`
    hook mapping (item 12 — the header is local, so this also shrinks the
    remote-output injection surface). `"Receiving subvol"` in the parser is still
    a dead pattern (verbose btrfs-progs prints lowercase); left in place as
    tolerance. Tests: synthetic SUBVOL/SNAPSHOT stream fixtures in
    unittests/test_receive_names.py.

28. DONE (09bc88a): `_is_duplicate_receive()` now matches btrfs-progs' actual
    wording — `ERROR: creating snapshot/subvolume ... failed: File exists` —
    as well as the legacy "already exists", so a repeat receive warns and skips
    instead of exiting 1. Verified against a real duplicate receive (rc=1,
    "File exists").

## Docs/testbed staleness and validation gaps (29, 30 fixed)

29. DONE (8fbf984): README's "When `--rsync` is enabled" bullet and
    `examples/single-rsync.toml` now document the per-archive
    `--partial-dir .bubtrsnap-partial-{sanitized archive}` that
    `_partial_dir_name()` actually emits. `rsynctestbed/test_rsync_interrupted.py`
    hardcoded the old shared `/tmp/.bubtrsnap-partial` in both its rsync
    invocation and its recovery assertion, so it exercised a directory the
    current code never creates; it now mirrors `_partial_dir_name()` and also
    asserts an unrelated archive's partial dir is absent after an interruption.
    Not executed here — the Docker socket is not reachable from this shell
    (`permission denied` on `/var/run/docker.sock`); the change is verified by
    `py_compile` and by comparing `partial_dir_name()` against
    `_partial_dir_name()` for several archive names. Run
    `python3 rsynctestbed/test_rsync_interrupted.py` in an environment with the
    `bubtrsnap-test` container to confirm end to end.

30. DONE (84afd70): `_validate_subvol_path()` now calls
    `run(..., check=True)`, so the `except subprocess.CalledProcessError` branch
    is reachable and a failing `btrfs subvolume show` prints
    "Error: '<path>' is not a btrfs subvolume" and exits 1 instead of logging
    success and caching the path as validated. Verified empirically on thorin:
    `chk_btrfs_subvolume(Path('/run/media/gerry/backup/_no_such_dir'), cfg)`
    used to return silently, now exits 1. Safe for real configs — `btrfs
    subvolume show` on a plain directory exits 1 and `btrfs subvolume snapshot`
    into that same directory fails too ("Could not create subvolume: Invalid
    cross-device link"), so nothing that previously worked is now rejected.
    Tests: unittests/test_validate_subvol_path.py (8 tests; the fake
    `subprocess.run` honours `check=`, which is what makes the old false-green
    style of test impossible). Note the older
    `TestSSHValidation.test_chk_btrfs_subvolume_ssh_failure` in
    unittests/test_ssh.py only passed because its mock raised the exception by
    hand — it now reflects real behaviour.

31. Housekeeping outside the repo: `~/.hermes/skills/.usage.json` still lists the
    deleted `btrfs-ssh-backup` skill.

33. Open (found while probing TODO 21 on 2026-10-22): a receive that dies
    half-way — the send stream truncated, the remote full, the ssh pipe cut —
    leaves the destination subvolume `{archive}.{ts}` behind with
    `Received UUID: -`.  It is never chosen as a parent (`_match_parents`
    compares UUID against received UUID, so a partial subvolume cannot match),
    which is why the guard still works, but it is writable, useless, and it
    blocks the next attempt with the same name: "creating subvolume ... failed:
    File exists".  Nothing cleans it up.  Fix shape: on a failed receive,
    delete the subvolume the receive was about to create (the name is known
    before the pipe runs) or at least report it with a `btrfs subvolume delete`
    hint; a test would need a loopback btrfs with a truncated stream.

## Keep-algorithm audit notes (no changes made, per instruction)

Correct across 7 scenarios. Observations only: total kept can be less than
sum(keep_*) because _get_keeps does not backfill slots taken by a shorter
interval; partial interval fills are silent; lexicographic ts compare is safe
only because ts are 12-digit padded.

## Verified clean (do not re-flag)

- All tests pass (332 across 20 files, re-run 2026-10-22); no secrets, eval/exec, pickle; stderr
  discipline correct on error paths.
- forced_keep threading intact (read from archive dict in
  _apply_all_keep_policies, threaded through to local and SSH paths).
- Sudo scoping correct: local_sudo local-only, remote_sudo only via
  build_ssh_cmd, ssh client never root.
- Prune safety nets sound: _confirm fail-safes on EOF/Ctrl-C, forced-keep
  targets require explicit confirmation even with --yes, no partial deletes.
- Staged dual-destination guard is correct (false positive in review).
- 'At subvol' does go to stderr — but only for FULL sends; incremental sends
  print 'At snapshot' on stdout. See item 27 (this bullet was a false positive).

## Suggested sequencing (open items only)

Done 2026-10-22: 10, 11, 32 (plus 25 verified already fixed and 26's build
identity), then the two easy security items 15 and 16.

Phase 1 (security, deferred by decision on 2026-10-22): 13, 12, 14
Phase 2 (improvements): 31, 33

Each item = one commit, test written first and proven red.
