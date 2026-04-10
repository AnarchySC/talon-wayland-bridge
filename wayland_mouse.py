"""
Wayland input bridge for Talon eye mouse.

Intercepts ctrl.mouse_move() calls and forwards coordinates via unix
socket to the bridge daemon, which creates a virtual uinput pointer
device. This works around Wayland blocking X11 XTest cursor warping.
"""
import socket
import time
from talon import ctrl, actions, ui

SOCK_PATH = "/tmp/talon-bridge.sock"

# Smoothing & outlier rejection — tunable
SMOOTH_ALPHA = 0.75      # 0.0 = no movement, 1.0 = no smoothing (raw)
JUMP_PX = 99999          # effectively disabled — trust the raw data
OUTLIER_RESET_S = 0.0
DEADZONE_PX = 0          # no deadzone — responsiveness over stillness

# Manual takeover behavior
MANUAL_DETECT_PX = 50    # actual cursor differing from last sent by this much = manual move
MANUAL_YIELD_S = 1.2     # pause gaze tracking this long after detecting manual move
GAP_RESET_S = 0.3        # if no gaze samples for this long, snap to fresh position on next one

LOG_FIRST_N = 10
LOG_GAPS = True          # log whenever >100ms elapses between patched_move calls

_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

_orig_mouse_move = ctrl.mouse_move
_orig_mouse_click = ctrl.mouse_click

_state = {
    "smoothed": None,        # (x, y) — internal smoothed position
    "last_sent": None,       # (x, y) — last position actually forwarded to bridge
    "last_sample_t": 0.0,    # monotonic time of last patched_move call
    "outlier_since": None,
    "manual_until": 0.0,     # monotonic time at which manual-yield mode ends
    "log_n": 0,
}


def _bridge_send(msg: str) -> None:
    try:
        _sock.sendto(msg.encode(), SOCK_PATH)
    except FileNotFoundError:
        print("[wayland_mouse] bridge socket missing — is bridge.py running?")
    except Exception as ex:
        print(f"[wayland_mouse] send error: {ex}")


def _current_cursor_pos():
    """Best-effort read of the actual current cursor position."""
    try:
        return ctrl.mouse_pos()
    except Exception:
        return None


def _reset_to(pos):
    """Reset all smoothing/tracking state to the given (x, y) position."""
    _state["smoothed"] = (float(pos[0]), float(pos[1]))
    _state["last_sent"] = (float(pos[0]), float(pos[1]))
    _state["outlier_since"] = None


def _patched_move(x, y, dx=None, dy=None):
    raw = (float(x), float(y))
    now = time.monotonic()
    if LOG_GAPS and _state["last_sample_t"]:
        gap = now - _state["last_sample_t"]
        if gap > 0.1:
            print(f"[wayland_mouse] GAP {gap*1000:.0f}ms — Talon stopped sending moves")

    # 1) Manual takeover detection: if the real cursor has moved away from
    #    where we last put it, the user grabbed the mouse. Yield for a beat.
    last_sent = _state["last_sent"]
    if last_sent is not None:
        cur = _current_cursor_pos()
        if cur is not None:
            cdx = cur[0] - last_sent[0]
            cdy = cur[1] - last_sent[1]
            if (cdx * cdx + cdy * cdy) > (MANUAL_DETECT_PX * MANUAL_DETECT_PX):
                _state["manual_until"] = now + MANUAL_YIELD_S
                _reset_to(cur)

    # 2) Active manual-yield window: drop the sample.
    if now < _state["manual_until"]:
        _state["last_sample_t"] = now
        return

    # 3) Long gap: if gaze data has been absent for a while, snap fresh on
    #    the next sample to avoid whipping back from a stale position.
    if _state["last_sample_t"] and (now - _state["last_sample_t"]) > GAP_RESET_S:
        cur = _current_cursor_pos()
        if cur is not None:
            _reset_to(cur)
        else:
            _state["smoothed"] = None
    _state["last_sample_t"] = now

    sm = _state["smoothed"]

    if sm is None:
        sm_new = raw
        _state["outlier_since"] = None
    else:
        dx_p = raw[0] - sm[0]
        dy_p = raw[1] - sm[1]
        dist = (dx_p * dx_p + dy_p * dy_p) ** 0.5
        if dist > JUMP_PX:
            if _state["outlier_since"] is None:
                _state["outlier_since"] = now
                return
            if now - _state["outlier_since"] < OUTLIER_RESET_S:
                return
            sm_new = raw
            _state["outlier_since"] = None
        else:
            _state["outlier_since"] = None
            sm_new = (
                sm[0] + (raw[0] - sm[0]) * SMOOTH_ALPHA,
                sm[1] + (raw[1] - sm[1]) * SMOOTH_ALPHA,
            )

    _state["smoothed"] = sm_new

    # Deadzone — filter fixation microsaccades
    last = _state["last_sent"]
    if last is not None:
        ddx = sm_new[0] - last[0]
        ddy = sm_new[1] - last[1]
        if (ddx * ddx + ddy * ddy) < DEADZONE_PX * DEADZONE_PX:
            return

    _state["last_sent"] = sm_new

    if _state["log_n"] < LOG_FIRST_N:
        print(f"[wayland_mouse] raw=({raw[0]:.0f},{raw[1]:.0f}) sent=({sm_new[0]:.0f},{sm_new[1]:.0f})")
        _state["log_n"] += 1

    _bridge_send(f"m {int(sm_new[0])} {int(sm_new[1])}")
    try:
        _orig_mouse_move(x, y, dx=dx, dy=dy)
    except Exception:
        pass

def _patched_click(button=0, down=False, up=False, times=1):
    name = {0: "left", 1: "right", 2: "middle"}.get(button, "left")
    if not down and not up:
        for _ in range(times):
            _bridge_send(f"c {name}")
    try:
        _orig_mouse_click(button=button, down=down, up=up, times=times)
    except Exception:
        pass

ctrl.mouse_move = _patched_move
ctrl.mouse_click = _patched_click

# Neutralize Talon's "break_force" anti-fight mechanism. It stops sending
# mouse_move when the gaze gets too far from the current cursor, which
# manifests as the cursor freezing during large saccades. Replacing
# break_force with a property that always reads 0 makes the threshold
# check (> 6) always false, so mouse_move is always called.
try:
    from talon_plugins import eye_mouse as _em
    def _get_zero(self): return 0.0
    def _set_noop(self, value): pass
    _em.EyeMouse.break_force = property(_get_zero, _set_noop)
    # Reset any existing stale value on the live instance
    try:
        object.__setattr__(_em.mouse, "__dict__", {**_em.mouse.__dict__})
        if "break_force" in _em.mouse.__dict__:
            del _em.mouse.__dict__["break_force"]
    except Exception:
        pass
    print("[wayland_mouse] neutralized eye_mouse.break_force")
except Exception as ex:
    print(f"[wayland_mouse] could not neutralize break_force: {ex}")

print("[wayland_mouse] ctrl.mouse_move / mouse_click patched to use bridge")
