#!/usr/bin/env python3
"""
Head tracker for talon-wayland-bridge.

Uses Mediapipe's face landmarker (Tasks API) to compute head pose
(yaw/pitch) from a webcam and forwards it to the bridge daemon via the
same unix datagram socket as eye gaze updates.

Protocol: "h <yaw_rad> <pitch_rad>" on /tmp/talon-bridge.sock.
Values are relative to a baseline captured at startup ("head centered").
"""
import os
import socket
import sys
import time
import math
from pathlib import Path

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision

_cursor_pos = [None]  # shared: latest cursor position from background thread

try:
    from Xlib import display as _xdisplay
    import threading as _cthreading

    def _cursor_watcher():
        # Single dedicated display connection, not shared with any other thread.
        d = _xdisplay.Display()
        root = d.screen().root
        while True:
            try:
                p = root.query_pointer()
                _cursor_pos[0] = (p.root_x, p.root_y)
            except Exception:
                _cursor_pos[0] = None
            time.sleep(0.05)  # 20Hz is plenty

    _t = _cthreading.Thread(target=_cursor_watcher, daemon=True)
    _t.start()

    def _query_cursor():
        return _cursor_pos[0]

except Exception:
    def _query_cursor():
        return None

SOCK_PATH = "/tmp/talon-bridge.sock"
CTL_PATH = "/tmp/head-tracker.ctl"
CAMERA_DEVICE = int(os.environ.get("HEAD_CAM", "0"))
MODEL_PATH = str(Path(__file__).parent / "face_landmarker.task")

# Head-pose-to-screen mapping
SCREEN_W = 3840
SCREEN_H = 2160
# Pixels of cursor movement per radian of head pose.
# 0.35 rad ≈ 20° head turn → full screen width
YAW_GAIN = SCREEN_W / 0.7    # 20° each way spans full width
PITCH_GAIN = SCREEN_H / 0.5  # 14° each way spans full height

# Simple exponential smoothing on the output position
POS_ALPHA = 0.35

# Manual mouse takeover — when real cursor diverges from last-sent by this
# much, treat it as manual override and stop sending for a while.
MANUAL_DETECT_PX = 60
MANUAL_YIELD_S = 1.5

# 3D reference points on a generic face for solvePnP (arbitrary units).
# Indices match Mediapipe face mesh landmark numbers.
_FACE_3D = np.array([
    [  0.0,   0.0,   0.0],   # nose tip          (1)
    [  0.0, -63.6, -12.5],   # chin              (152)
    [-43.3,  32.7, -26.0],   # left eye corner   (263)
    [ 43.3,  32.7, -26.0],   # right eye corner  (33)
    [-28.9, -28.9, -24.1],   # left mouth corner (287)
    [ 28.9, -28.9, -24.1],   # right mouth corner(57)
], dtype=np.float64)

_LM_IDX = [1, 152, 263, 33, 287, 57]


def _send(sock, msg: str):
    try:
        sock.sendto(msg.encode(), SOCK_PATH)
    except FileNotFoundError:
        pass


def _rot_to_yaw_pitch(rvec):
    R, _ = cv2.Rodrigues(rvec)
    pitch = math.atan2(-R[2, 0], math.sqrt(R[2, 1] ** 2 + R[2, 2] ** 2))
    yaw = math.atan2(R[1, 0], R[0, 0])
    return yaw, pitch


def _start_ctl_listener(state):
    """Listen for control commands on a unix datagram socket."""
    import threading as _t
    if os.path.exists(CTL_PATH):
        os.unlink(CTL_PATH)
    ctl = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    ctl.bind(CTL_PATH)
    os.chmod(CTL_PATH, 0o666)
    print(f"[head] control socket ready at {CTL_PATH}", flush=True)

    def _run():
        while True:
            try:
                data, _ = ctl.recvfrom(256)
                cmd = data.decode().strip()
                if cmd == "baseline":
                    state["reset_baseline"] = True
                    print("[head] baseline reset requested", flush=True)
                elif cmd.startswith("calib "):
                    # format: "calib yaw_range pitch_range" (in radians)
                    try:
                        _, yr, pr = cmd.split()
                        state["yaw_range"] = float(yr)
                        state["pitch_range"] = float(pr)
                        print(f"[head] calibration updated: yaw={yr} pitch={pr}", flush=True)
                    except Exception as ex:
                        print(f"[head] bad calib: {ex}", flush=True)
            except Exception as ex:
                print(f"[head] ctl error: {ex}", flush=True)

    t = _t.Thread(target=_run, daemon=True)
    t.start()


def main():
    if not os.path.exists(MODEL_PATH):
        print(f"[head] model missing: {MODEL_PATH}", file=sys.stderr)
        sys.exit(1)

    cap = cv2.VideoCapture(CAMERA_DEVICE, cv2.CAP_V4L2)
    if not cap.isOpened():
        print(f"[head] cannot open camera {CAMERA_DEVICE}", file=sys.stderr)
        sys.exit(1)

    # Prefer MJPG for faster USB transfer + larger resolution the camera
    # actually likes. MediaPipe will downscale as needed.
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    print(f"[head] capture: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}@{cap.get(cv2.CAP_PROP_FPS):.0f}fps", flush=True)

    base_options = mp_python.BaseOptions(model_asset_path=MODEL_PATH)
    options = mp_vision.FaceLandmarkerOptions(
        base_options=base_options,
        running_mode=mp_vision.RunningMode.IMAGE,
        num_faces=1,
        output_facial_transformation_matrixes=True,
    )
    landmarker = mp_vision.FaceLandmarker.create_from_options(options)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

    # State shared with the control thread
    state = {"reset_baseline": False, "yaw_range": 0.7, "pitch_range": 0.5}
    _start_ctl_listener(state)

    baseline_yaw = None
    baseline_pitch = None
    baseline_samples = 0
    BASELINE_NEEDED = 30

    print(f"[head] streaming from camera {CAMERA_DEVICE}. Hold still ~1s for baseline.", flush=True)

    frame_count = 0
    last_log = time.monotonic()
    t0 = time.monotonic()

    while True:
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.05)
            continue

        h, w = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = landmarker.detect(mp_image)

        if not result.face_landmarks or not result.facial_transformation_matrixes:
            continue

        # Use Mediapipe's native 4x4 facial transformation matrix — much
        # more stable than solvePnP on a handful of hand-picked points.
        M = np.asarray(result.facial_transformation_matrixes[0])
        R = M[:3, :3]
        # Decompose to Euler angles (YXZ order): yaw around Y, pitch around X
        pitch = math.atan2(-R[1, 2], R[1, 1])
        yaw = math.atan2(-R[2, 0], R[0, 0])

        # Handle baseline reset requested from control socket
        if state["reset_baseline"]:
            baseline_yaw = yaw
            baseline_pitch = pitch
            state["reset_baseline"] = False
            print(f"[head] baseline reset: yaw={yaw:.3f} pitch={pitch:.3f}", flush=True)

        if baseline_samples < BASELINE_NEEDED:
            if baseline_yaw is None:
                baseline_yaw = yaw
                baseline_pitch = pitch
            else:
                baseline_yaw = 0.9 * baseline_yaw + 0.1 * yaw
                baseline_pitch = 0.9 * baseline_pitch + 0.1 * pitch
            baseline_samples += 1
            if baseline_samples == BASELINE_NEEDED:
                print(f"[head] baseline set: yaw={baseline_yaw:.3f} pitch={baseline_pitch:.3f}", flush=True)
            continue

        rel_yaw = yaw - baseline_yaw
        rel_pitch = pitch - baseline_pitch

        # Compute absolute cursor position on screen, using dynamic ranges.
        yaw_range = state["yaw_range"]
        pitch_range = state["pitch_range"]
        yaw_gain = SCREEN_W / max(yaw_range, 0.01)
        pitch_gain = SCREEN_H / max(pitch_range, 0.01)
        raw_x = SCREEN_W / 2 - rel_yaw * yaw_gain
        raw_y = SCREEN_H / 2 + rel_pitch * pitch_gain
        raw_x = max(0, min(SCREEN_W - 1, raw_x))
        raw_y = max(0, min(SCREEN_H - 1, raw_y))

        if not hasattr(main, "_smoothed_x"):
            main._smoothed_x = raw_x
            main._smoothed_y = raw_y
            main._last_sent = (int(raw_x), int(raw_y))
            main._manual_until = 0.0

        now_m = time.monotonic()

        # Manual takeover detection: compare the real cursor position to
        # what we last sent. If they diverge, something else (a mouse)
        # moved the cursor. Pause head tracking for a window, then resume
        # from the current cursor position.
        cur = _query_cursor()
        if cur is not None and main._last_sent is not None:
            cdx = cur[0] - main._last_sent[0]
            cdy = cur[1] - main._last_sent[1]
            if (cdx * cdx + cdy * cdy) > (MANUAL_DETECT_PX * MANUAL_DETECT_PX):
                main._manual_until = now_m + MANUAL_YIELD_S
                main._smoothed_x = cur[0]
                main._smoothed_y = cur[1]
                main._last_sent = cur

        if now_m < main._manual_until:
            continue

        main._smoothed_x += (raw_x - main._smoothed_x) * POS_ALPHA
        main._smoothed_y += (raw_y - main._smoothed_y) * POS_ALPHA

        tx, ty = int(main._smoothed_x), int(main._smoothed_y)
        main._last_sent = (tx, ty)
        _send(sock, f"m {tx} {ty}")

        frame_count += 1
        now = time.monotonic()
        if now - last_log >= 5.0:
            print(f"[head] {frame_count/(now-last_log):.1f} fps  yaw={math.degrees(rel_yaw):+.1f}° pitch={math.degrees(rel_pitch):+.1f}°", flush=True)
            frame_count = 0
            last_log = now


if __name__ == "__main__":
    main()
