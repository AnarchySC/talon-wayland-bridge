# talon-wayland-bridge

> A project by [**AnarchySC**](https://anarchygames.org) — built out of
> necessity for an accessibility user who refused to be told "Linux isn't
> supported."

Run [Talon Voice](https://talonvoice.com/) eye tracking on Linux Wayland.

Talon is the de-facto eye-tracking + voice-control framework on Linux, but it
injects cursor and key input through X11 (`XTest`). On Wayland — which most
modern distros now default to — that input gets silently dropped by the
compositor for security reasons. The result: Talon detects your gaze just
fine, but the cursor never moves.

This bridge fixes that. It creates a virtual absolute-pointer device through
`/dev/uinput` and forwards Talon's gaze coordinates to it. The Wayland
compositor (Mutter, KWin, etc.) sees an ordinary hardware pointer and accepts
its input — no patches to Talon, no `Xorg` fallback, no compositor changes.

If you're an accessibility user who needs Talon and has been forced to choose
between Wayland's improvements and a working eye tracker, this is for you.

## Status

| Piece | State |
|---|---|
| Tobii Eye Tracker 5 detection | ✓ Works (firmware 10008+) |
| Cursor movement on Wayland | ✓ Works |
| Multi-monitor (mixed orientation) | ✓ Works (queries Mutter for layout) |
| Left/right click via bridge | ✓ Works |
| Hot-plugging monitors | ✓ Bridge listens for `MonitorsChanged` |
| Scroll | Not yet wired |
| KWin / wlroots compositors | Untested (only verified on GNOME Mutter) |

## How it works

```
Tobii Eye Tracker 5
        │  USB
        ▼
    Talon
        │  ctrl.mouse_move(x, y)   ← monkey-patched by user script
        ▼
~/.talon/user/wayland_mouse.py
        │  unix-dgram /tmp/talon-bridge.sock
        ▼
    bridge.py  ←  queries Mutter via DBus for desktop layout
        │  EV_ABS events
        ▼
    /dev/uinput
        │
        ▼
   libinput → Wayland compositor → cursor moves
```

The bridge advertises a virtual absolute pointer device with a tablet-style
`0..32767` coordinate range. It queries Mutter's `org.gnome.Mutter.DisplayConfig`
for the full logical layout (the bounding box of all monitors, accounting for
rotation and scaling) and normalizes Talon's global X11 pixel coordinates to
that range so the cursor lands where the user is actually looking.

## Requirements

- Linux with a Wayland compositor (tested on Ubuntu GNOME)
- A Tobii eye tracker that Talon supports (verified on the **Eye Tracker 5**, VID:PID `2104:0313`)
- [Talon Voice](https://talonvoice.com/) (free for personal use)
- `python3-evdev` and `python3-gi` from your distro packages
- Read/write access to `/dev/uinput` (provided automatically when you're the
  active session user — `uaccess` ACL on most distros)

## Install

```bash
# 1. System dependencies
sudo apt install python3-evdev python3-gi

# 2. Talon (if not already installed)
mkdir -p ~/talon && cd ~/talon
curl -L -o talon-linux.tar.xz https://talonvoice.com/dl/latest/talon-linux.tar.xz
tar xf talon-linux.tar.xz

# 3. Install Talon's udev rule (gives you uaccess to the Tobii USB device)
sudo cp ~/talon/talon/10-talon.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger

# 4. Clone this repo and copy the pieces into place
git clone https://github.com/AnarchySC/talon-wayland-bridge.git
cd talon-wayland-bridge
mkdir -p ~/talon/wayland-bridge ~/.talon/user ~/.config/systemd/user
cp bridge.py                       ~/talon/wayland-bridge/
cp wayland_mouse.py enable_eye_mouse.py ~/.talon/user/
cp talon-bridge.service talon.service ~/.config/systemd/user/

# 5. Enable both services
systemctl --user daemon-reload
systemctl --user enable --now talon-bridge.service
systemctl --user enable --now talon.service
```

The bridge runs as a normal user — no `sudo`, no setuid binary. The systemd
units start automatically at login.

## First run

1. **Plug in the Tobii** if it isn't already.
2. **Calibrate.** Talon's tray icon is hidden under GNOME Wayland (legacy
   `XEmbed` icons are gone), so calibration must be triggered from a script.
   Edit `~/.talon/user/enable_eye_mouse.py` and add a `cron.after("3s",
   actions.tracking.calibrate)` line, restart Talon (`systemctl --user
   restart talon.service`), do the calibration, then remove the line.
3. **Sit still during calibration.** Move only your eyes. Don't predict the
   next dot — hold gaze on the current one until it disappears.
4. **Look around.** The cursor should follow.

## Troubleshooting

### Talon says `LIBUSB_ERROR_BUSY`

Another process is holding the Tobii USB device — usually leftover services
from a previous Tobii Pro SDK install:

```bash
sudo systemctl stop tobiiusb.service tobii-runtime-IS5LPROENTRY.service
sudo systemctl disable tobiiusb.service tobii-runtime-IS5LPROENTRY.service
```

### Cursor doesn't move at all

- `ls /dev/uinput` — should show `crw-rw----+ root` with the `+` (uaccess ACL)
- `systemctl --user status talon-bridge.service` — should be `active (running)`
- `cat /tmp/bridge.log` — look for `uinput device created` and `layout` lines
- `cat /tmp/talon.log | grep wayland_mouse` — should show `patched to use bridge`

### Cursor lands in the wrong place / hugs an edge

- This bridge normalizes against the **full multi-monitor logical layout**.
  If your gaze coordinates are getting compressed, your layout probably
  changed (you plugged in a new monitor). The bridge listens for
  `MonitorsChanged` from Mutter and refreshes — check `/tmp/bridge.log`
  for a `layout refreshed` line. If you don't see one, restart the
  service.

### Cursor can't reach the edges of a large screen

- The Tobii Eye Tracker 5 is rated for **27" max at 16:9**. Larger displays
  exceed the IR field of view at the edges. Use Talon's **Zoom Mouse** mode
  for precise selection at the edges, or supplement with voice commands.

### Multiple monitors with rotated displays

Supported. The bridge correctly handles transforms (90°/180°/270°) when
computing the logical bounding box. If you have a portrait monitor on
the left and your tracked monitor on the right, Mutter's layout reflects
that and the bridge maps accordingly.

## Known limitations

- **Tobii 5 hardware FOV.** On screens larger than ~27", the edges
  (especially the corner farthest from the device) become unreliable.
  Reposition the Tobii or use Zoom Mouse.
- **GNOME tray icon missing.** Talon assumes an X11 system tray. Under
  Wayland GNOME, the tray icon doesn't appear. Toggle eye mouse from
  `~/.talon/user/enable_eye_mouse.py` or define voice/hotkey commands.
- **Tested only on GNOME Mutter.** KWin and wlroots compositors should work
  in principle (libinput sees the same uinput device) but the
  layout-detection code is Mutter-specific. PRs welcome for KWin's DBus API
  or `wlr-output-management`.
- **Talon-only.** The same approach would work for `xdotool`, AutoKey,
  AutoHotkey-on-Linux, and similar tools — they all need a Wayland
  input bridge — but this repo specifically targets Talon.

## Repository layout

```
talon-wayland-bridge/
├── bridge.py              # uinput bridge daemon
├── wayland_mouse.py       # Talon user script: monkey-patches ctrl.mouse_*
├── enable_eye_mouse.py    # Talon user script: auto-enables eye mouse
├── talon-bridge.service   # systemd user unit for the bridge
├── talon.service          # systemd user unit for Talon itself
├── LICENSE                # MIT
└── README.md
```

## Support the project

This bridge exists because Tobii told us a Linux version "isn't on the
roadmap" — and we needed one anyway. If this project helped you get back
to work, please consider supporting it. Donations keep projects like this
alive and help us build more accessibility tooling.

- **Website & contact:** [anarchygames.org](https://anarchygames.org)
- **GitHub Sponsors:** see the "Sponsor" button at the top of this repo
- **One-time donations:** see the project website

If you can't donate, other equally appreciated ways to help:

- ⭐ Star the repo so other accessibility users can find it
- 🐛 File issues with your compositor / eye tracker details if it doesn't
  work for you
- 📝 Send PRs for KWin / wlroots / Hyprland support, scroll events, or
  additional eye tracker models
- 💬 Tell your accessibility community about it

## License

MIT. See `LICENSE`.

## Acknowledgements

- The Talon team for building the only serious accessibility eye-tracking
  framework for Linux
- The Talon community wiki for documenting Tobii 5 setup at
  https://talon.wiki/Resource%20Hub/Hardware/tobii_5/
- Everyone in r/Linux_Accessibility and the Talon Slack who has been
  asking for Wayland support for years

---

<p align="center">
  Made with stubbornness by <a href="https://anarchygames.org"><b>AnarchySC</b></a><br>
  <sub>Because "not supported" is an unacceptable answer when accessibility is on the line.</sub>
</p>
