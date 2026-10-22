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
- `maintain` — working branch (tip 216bb60, VERSION 0.4.2.dev0). All DONE
  items below are committed here and pushed to the `stryder` remote; stryder
  runs this build via an editable pipx install from /home/lama7/src/bubtrsnap.
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

## REMAINING — validation / precedence gaps

10. Negative keep counts pass validation: validate_config only type-checks
    keep_* values as int, so -5 is accepted. CONFLICT to resolve with one
    probe first: FINDINGS says they distort build_keep_set accounting and
    over-prune; the keep-algorithm audit says negatives are treated as 0.
    Validation fix only — do not touch the keep algorithm itself (needs
    explicit approval).

11. CLI --forced-keep values never format-validated: only config values get
    the 12-digit check (validate_config); _merge_keep accepts any string;
    malformed CLI timestamps are silently warned-and-ignored in
    _apply_keep_policy_core.

## REMAINING — security hardening

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

15. Predictable temp file /tmp/bubtrsnap-<name> on the remote — symlink/TOCTOU
    exposure on multi-user hosts, then unconditional rm -f.

16. Archive names unsanitized in filesystem paths and globs:
    snap_dir / f'{archive}.{ts}' escapes snapshot_dir for names with '/' or
    '..'; glob(f'{archive}.*') treats metacharacters as pattern syntax.

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

## REMAINING — improvements (not bugs)

21. _size_of walks the whole snapshot summing logical sizes, then compares
    against free space before an INCREMENTAL send -> false "insufficient
    space" aborts. Also used by rebuild to report required memory.

22. find_parents / find_parents_ssh spawn one `btrfs subvolume show` (or SSH)
    per candidate; `btrfs subvolume list -u <dir>` gets all UUIDs in one call.

23. DONE (4afd32a): _scp_and_receive and _rsync_and_receive now share a
    single _receive_on_remote() tail (SSH receive, temp cleanup, error
    handling, name extraction); only the transfer command and log label
    differ.

24. FALSE POSITIVE: crc32c is live — _parse_stream_prefix validates the
    stream CRC via it, reached by is_btrfs_stream and stream_snapshot_name.

25. --config only accepted before the subcommand (top-level parser only;
    `bubtrsnap list --config x` fails).

26. SKILL.md version field tracks the latest release; keep in sync at each
    ship (currently 0.4.1).

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

## Docs/testbed staleness (docs-only; 29 fixed)

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

30. `_validate_subvol_path()` never raises: it calls `run(cmd, dry_run=False, ...)`
    without `check=True`, so the `except subprocess.CalledProcessError` block is
    unreachable and a failing `btrfs subvolume show` is logged as success and
    cached in `cfg["_validated_paths"]`. Verified:
    `chk_btrfs_subvolume(Path('/nonexistent-path-xyz'), cfg)` returns silently.
    Fix: pass `check=True` (or test `result.returncode`).

31. Housekeeping outside the repo: `~/.hermes/skills/.usage.json` still lists the
    deleted `btrfs-ssh-backup` skill.

## Keep-algorithm audit notes (no changes made, per instruction)

Correct across 7 scenarios. Observations only: total kept can be less than
sum(keep_*) because _get_keeps does not backfill slots taken by a shorter
interval; partial interval fills are silent; lexicographic ts compare is safe
only because ts are 12-digit padded.

## Verified clean (do not re-flag)

- All tests pass (211 across 13 files); no secrets, eval/exec, pickle; stderr
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

Phase 1 (security): 13, 12, 14, 15+16
Phase 2 (validation/consistency): 10 (probe first), 11, 30
Phase 3 (improvements): 21, 22, 25, 26, 31

Each item = one commit, test written first and proven red.
