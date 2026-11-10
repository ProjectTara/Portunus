import sys


SELF_TEST_MODULES = (
    "portunus.app",
    "portunus.gateway",
    "portunus.updater",
    "portunus.wifi",
    "pymobiledevice3.bonjour",
    "pymobiledevice3.remote.tunnel_service",
    "pymobiledevice3.pair_records",
    "pymobiledevice3.lockdown",
    "pymobiledevice3.usbmux",
    "pymobiledevice3.remote.rsd_tunnel",
    "pymobiledevice3.remote.userspace_tunnel",
    "pymobiledevice3.remote.core_device.screen_stream",
    "pymobiledevice3.services.mobile_image_mounter",
    "pymobiledevice3.services.amfi",
    "pytun_pmd3",
    "psutil",
    "cryptography.hazmat.primitives.asymmetric.ed25519",
    "webview",
    "clr",
    "winrt.windows.media.ocr",
    "winrt.windows.media.audio",
)


def self_test(report: str) -> int:
    """Import everything the app needs at runtime. Used by CI on the built exe."""
    import importlib
    import traceback

    failures = []
    for name in SELF_TEST_MODULES:
        try:
            importlib.import_module(name)
        except BaseException:  # noqa: BLE001
            failures.append(f"FAIL {name}\n{traceback.format_exc()}")
    with open(report, "w", encoding="utf-8") as out:
        out.write("\n".join(failures) if failures else f"OK {len(SELF_TEST_MODULES)} modules")
    return 1 if failures else 0


def run() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        raise SystemExit(self_test(sys.argv[2]))
    # The updater starts the new build with: --apply-update <install_dir> <old_pid>
    if len(sys.argv) == 4 and sys.argv[1] == "--apply-update" and sys.argv[3].isdigit():
        from pathlib import Path

        from portunus.updater import apply_update

        raise SystemExit(apply_update(Path(sys.argv[2]), int(sys.argv[3])))
    from portunus.app import main

    main()


if __name__ == "__main__":
    run()
