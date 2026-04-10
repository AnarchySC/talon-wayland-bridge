#!/usr/bin/env python3
"""
Wayland input bridge for Talon eye tracker.

Creates a virtual absolute-position pointer device via uinput and listens
on a unix socket for "x y" coordinate pairs. Writes ABS events so the
Wayland compositor sees it as a real hardware mouse.

Layout dimensions are queried from Mutter via DBus and refreshed on
MonitorsChanged signals.
"""
import os
import sys
import socket
import signal
import threading
from evdev import UInput, ecodes as e, AbsInfo

SOCK_PATH = "/tmp/talon-bridge.sock"

ABS_MAX = 32767  # tablet-style normalized range

# Updated dynamically from Mutter; defaults are a safe single-1080p fallback
_layout = {"w": 1920, "h": 1080}
_layout_lock = threading.Lock()


def _query_mutter_layout():
    """Compute the bounding box of all logical monitors from Mutter."""
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    proxy = Gio.DBusProxy.new_sync(
        bus,
        Gio.DBusProxyFlags.NONE,
        None,
        "org.gnome.Mutter.DisplayConfig",
        "/org/gnome/Mutter/DisplayConfig",
        "org.gnome.Mutter.DisplayConfig",
        None,
    )
    state = proxy.call_sync("GetCurrentState", None, Gio.DBusCallFlags.NONE, -1, None)
    serial, monitors, logical_monitors, props = state.unpack()

    # Build a lookup from monitor connector → current mode (w, h)
    current_modes = {}
    for mon in monitors:
        spec, modes, mon_props = mon
        connector = spec[0]
        for mode in modes:
            mode_id, w, h, refresh, pref_scale, scales, mode_props = mode
            if mode_props.get("is-current"):
                current_modes[connector] = (w, h)
                break

    min_x, min_y = 0, 0
    max_x, max_y = 0, 0
    for lm in logical_monitors:
        x, y, scale, transform, primary, mon_specs, lm_props = lm
        for spec in mon_specs:
            connector = spec[0]
            if connector not in current_modes:
                continue
            mw, mh = current_modes[connector]
            if transform in (1, 3, 5, 7):  # 90/270 rotations
                lw, lh = mh, mw
            else:
                lw, lh = mw, mh
            lw = int(lw / scale)
            lh = int(lh / scale)
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x + lw)
            max_y = max(max_y, y + lh)
            break  # one spec per logical monitor is enough
    return max_x - min_x, max_y - min_y


def _refresh_layout():
    try:
        w, h = _query_mutter_layout()
        with _layout_lock:
            _layout["w"] = w
            _layout["h"] = h
        print(f"[bridge] layout refreshed: {w}x{h}", flush=True)
    except Exception as ex:
        print(f"[bridge] layout query failed: {ex}", flush=True)


def _start_layout_watcher():
    """Run a GLib mainloop in a background thread to listen for MonitorsChanged."""
    def _run():
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        def on_signal(connection, sender, path, iface, signal_name, params):
            if signal_name == "MonitorsChanged":
                _refresh_layout()

        bus.signal_subscribe(
            "org.gnome.Mutter.DisplayConfig",
            "org.gnome.Mutter.DisplayConfig",
            "MonitorsChanged",
            "/org/gnome/Mutter/DisplayConfig",
            None,
            Gio.DBusSignalFlags.NONE,
            on_signal,
        )
        GLib.MainLoop().run()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

def main():
    caps = {
        e.EV_KEY: [e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE],
        e.EV_ABS: [
            (e.ABS_X, AbsInfo(value=0, min=0, max=ABS_MAX, fuzz=0, flat=0, resolution=0)),
            (e.ABS_Y, AbsInfo(value=0, min=0, max=ABS_MAX, fuzz=0, flat=0, resolution=0)),
        ],
    }

    _refresh_layout()
    _start_layout_watcher()

    ui = UInput(caps, name="talon-wayland-bridge", version=0x1)
    with _layout_lock:
        print(f"[bridge] uinput device created, layout={_layout['w']}x{_layout['h']}", flush=True)

    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o666)
    print(f"[bridge] listening on {SOCK_PATH}", flush=True)

    def cleanup(*_):
        try:
            ui.close()
            os.unlink(SOCK_PATH)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    try:
        while True:
            data, _ = srv.recvfrom(256)
            try:
                msg = data.decode().strip()
                if msg.startswith("m "):
                    _, xs, ys = msg.split()
                    with _layout_lock:
                        lw, lh = _layout["w"], _layout["h"]
                    gx = max(0, min(lw - 1, int(float(xs))))
                    gy = max(0, min(lh - 1, int(float(ys))))
                    ax = int(round(gx * ABS_MAX / max(lw - 1, 1)))
                    ay = int(round(gy * ABS_MAX / max(lh - 1, 1)))
                    ui.write(e.EV_ABS, e.ABS_X, ax)
                    ui.write(e.EV_ABS, e.ABS_Y, ay)
                    ui.syn()
                elif msg.startswith("c "):
                    _, btn = msg.split()
                    btn_code = {"left": e.BTN_LEFT, "right": e.BTN_RIGHT, "middle": e.BTN_MIDDLE}.get(btn)
                    if btn_code is not None:
                        ui.write(e.EV_KEY, btn_code, 1)
                        ui.syn()
                        ui.write(e.EV_KEY, btn_code, 0)
                        ui.syn()
            except Exception as ex:
                print(f"[bridge] parse error: {ex} msg={data!r}", flush=True)
    finally:
        cleanup()

if __name__ == "__main__":
    main()
