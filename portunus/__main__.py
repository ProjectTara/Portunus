import sys


def run() -> None:
    # The updater starts the new build with: --apply-update <install_dir> <old_pid>
    if len(sys.argv) == 4 and sys.argv[1] == "--apply-update" and sys.argv[3].isdigit():
        from pathlib import Path

        from portunus.updater import apply_update

        raise SystemExit(apply_update(Path(sys.argv[2]), int(sys.argv[3])))
    from portunus.app import main

    main()


if __name__ == "__main__":
    run()
