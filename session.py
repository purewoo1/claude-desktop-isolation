"""Run the GUI and login prompts on the private display and session bus."""
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import time

from guard import PROXY_PORT, process


def desktop_command():
    return [
        "/opt/claude-desktop/claude-desktop",
        f"--proxy-server=http://127.0.0.1:{PROXY_PORT}",
        "--proxy-bypass-list=localhost;127.0.0.1;[::1]",
        "--disable-quic",
        "--password-store=gnome-libsecret",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--ozone-platform=x11",
        f"--force-device-scale-factor={os.environ['CLAUDE_GUARD_SCALE']}",
    ]


def browser_command(profile):
    return [
        "/opt/google/chrome/chrome", "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--disable-sync", "--disable-quic", "--disable-dev-shm-usage",
        "--disable-gpu", "--ozone-platform=x11", "--password-store=gnome-libsecret",
        f"--proxy-server=http://127.0.0.1:{PROXY_PORT}",
        "--proxy-bypass-list=localhost;127.0.0.1;[::1]",
        f"--user-data-dir={Path.home() / '.config' / profile}",
    ]


def start_rime(stack):
    from gi.repository import Gio

    general = Gio.Settings.new("org.freedesktop.ibus.general")
    general.set_strv("preload-engines", ["rime", "xkb:us::eng"])
    general.set_strv("engines-order", ["rime", "xkb:us::eng"])
    general.set_boolean("use-global-engine", True)
    hotkey = Gio.Settings.new("org.freedesktop.ibus.general.hotkey")
    hotkey.set_strv("triggers", ["<Control><Shift>space"])
    Gio.Settings.sync()
    daemon = stack.enter_context(process([
        "/usr/bin/ibus-daemon", "--xim", "--desktop=claude-isolated",
        "--address=" + os.environ["IBUS_ADDRESS"], "--cache=refresh",
        "--panel=disable", "--config=/usr/libexec/ibus-dconf", "--emoji-extension=disable",
    ]))
    for _ in range(60):
        if daemon.poll() is not None:
            break
        result = subprocess.run(["/usr/bin/ibus", "engine", "rime"], capture_output=True, timeout=3)
        if result.returncode == 0:
            # Spawn the panel only after a usable engine is registered.
            stack.enter_context(process(["/usr/libexec/ibus-ui-gtk3"]))
            return
        time.sleep(0.1)
    raise RuntimeError("The private Rime engine did not start. Install ibus-rime.")


def unlock_keyring():
    import secretstorage

    connection = secretstorage.dbus_init()
    try:
        store = secretstorage.get_default_collection(connection)
    except secretstorage.exceptions.ItemNotFoundException:
        store = secretstorage.create_collection(connection, "Claude Desktop (Isolated)", "default")
    if store.is_locked():
        store.unlock()
    files = list((Path.home() / ".local/share/keyrings").glob("*.keyring"))
    for path in files:
        with path.open("rb") as handle:
            if not handle.read(16).startswith(b"GnomeKeyring\n\r\0"):
                raise RuntimeError("The private keyring must use a non-empty password.")
    if store.is_locked() or not files:
        raise RuntimeError("Unlock or create the private, encrypted keyring before launching.")


def main(mode):
    home = Path.home()
    for path in (".config", ".cache", ".local/share"):
        (home / path).mkdir(mode=0o700, parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(process(["/usr/bin/openbox", "--sm-disable"]))
        for _ in range(40):
            check = subprocess.run(["/usr/bin/xprop", "-root", "_NET_SUPPORTING_WM_CHECK"],
                                   capture_output=True, text=True, timeout=2)
            if "window id #" in check.stdout:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("The private window manager did not start.")
        unlock_keyring()
        if os.environ.get("CLAUDE_GUARD_RIME") == "1":
            start_rime(stack)
        if mode == "browser":
            return subprocess.call([*browser_command("browser"), "https://claude.ai/"])
        return subprocess.call(desktop_command())


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1]))
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"private Claude session: {error}")
