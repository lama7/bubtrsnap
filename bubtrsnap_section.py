if (cli_export_file or cli_import_file or cli_stage_file) and (
        cli_export_dir or cli_import_dir or cli_stage_dir
    ):
        print(
            "Error: cannot mix file options with directory options on the CLI",
            file=sys.stderr,
        )
        sys.exit(1)

    if cli_stage_file and (
        cli_export_file or cli_import_file
        or cli_export_dir or cli_import_dir or cli_stage_dir
    ):
        print(
            "Error: --stage-file is mutually exclusive with other export/import/stage options",
            file=sys.stderr,
        )
        sys.exit(1)
    if cli_stage_dir and (
        cli_export_file or cli_import_file or cli_stage_file
        or cli_export_dir or cli_import_dir
    ):
        print(
            "Error: --stage-dir is mutually exclusive with other export/import/stage options",
            file=sys.stderr,
        )
        sys.exit(1)

    # CLI keep options
    cli_keeps = {}
    for key in ("keep_hourly", "keep_daily", "keep_weekly", "keep_monthly", "keep_yearly"):
        val = getattr(cli, key, None)
        if val is not None:
            cli_keeps[key] = val

    # CLI keep options (--keep) - requires exactly one archive total
    cli_keep = getattr(cli, "keep", None)
    if cli_keep:
        # Count total archives that would be processed
        cli_archives = parse_cli_archives(cli.archives) if cli.archives else {}
        n_cli = len(cli_archives)
        n_cfg = len(raw_archives)
        total_archives = n_cli if n_cli > 0 else n_cfg
        if total_archives != 1:
            print(
                "Error: --keep requires exactly one archive to process "
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

    # now process any archives from the CLI
    cli_archives = parse_cli_archives(cli.archives) if cli.archives else {}