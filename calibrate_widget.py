#!/usr/bin/env python3
"""
Calibration widget for talon-wayland-bridge head tracking.

Small always-on-top GTK window with:
  - "Reset Baseline" button (instant)
  - "Calibrate Range" button (5-point: center, up, down, left, right)

Communicates with head_tracker.py via /tmp/head-tracker.ctl (unix dgram).
During "Calibrate Range", shows a fullscreen overlay with a target square
at each of 5 positions. The user aims their head (not eyes!) at each
square in sequence. Extreme yaw/pitch across the 4 edges become the new
user range.
"""
import os
import socket
import sys
import time
import math
import threading

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk

CTL_PATH = "/tmp/head-tracker.ctl"


def _send_ctl(msg: str):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.sendto(msg.encode(), CTL_PATH)
        s.close()
    except FileNotFoundError:
        print("head tracker control socket not found — is head_tracker.py running?")
    except Exception as ex:
        print(f"send error: {ex}")


class OverlayWindow(Gtk.Window):
    """Fullscreen transparent overlay that shows calibration targets."""
    def __init__(self, on_done):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.on_done = on_done
        self.fullscreen()
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_app_paintable(True)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)

        self.connect("draw", self._on_draw)

        self.points = [
            ("CENTER", 0.5, 0.5),
            ("LEFT",   0.05, 0.5),
            ("RIGHT",  0.95, 0.5),
            ("UP",     0.5, 0.05),
            ("DOWN",   0.5, 0.95),
        ]
        self.current = 0
        self.samples = []  # (yaw, pitch) captured at each point
        self.show_all()

        GLib.timeout_add(1500, self._advance)

    def _on_draw(self, widget, cr):
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.paint()
        w = self.get_allocated_width()
        h = self.get_allocated_height()

        if self.current < len(self.points):
            label, fx, fy = self.points[self.current]
            cx, cy = fx * w, fy * h
            # Pulsing target square
            size = 120
            cr.set_source_rgb(1.0, 0.4, 0.2)
            cr.rectangle(cx - size / 2, cy - size / 2, size, size)
            cr.fill()
            cr.set_source_rgb(1, 1, 1)
            cr.select_font_face("Sans")
            cr.set_font_size(48)
            text = f"{self.current+1}/{len(self.points)}  Aim at: {label}"
            ext = cr.text_extents(text)
            cr.move_to(w / 2 - ext.width / 2, h / 2 + 200)
            cr.show_text(text)
        return False

    def _advance(self):
        self.current += 1
        if self.current >= len(self.points):
            self.close()
            self.on_done()
            return False
        self.queue_draw()
        return True


class Widget(Gtk.Window):
    def __init__(self):
        super().__init__(title="Head Tracker")
        self.set_default_size(260, 40)
        self.set_keep_above(True)
        self.set_resizable(False)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(6)
        box.set_margin_end(6)
        self.add(box)

        self.reset_btn = Gtk.Button(label="Reset Baseline")
        self.reset_btn.connect("clicked", self._on_reset)
        box.pack_start(self.reset_btn, True, True, 0)

        self.calib_btn = Gtk.Button(label="Calibrate Range")
        self.calib_btn.connect("clicked", self._on_calibrate)
        box.pack_start(self.calib_btn, True, True, 0)

        self.connect("destroy", Gtk.main_quit)

    def _on_reset(self, _btn):
        _send_ctl("baseline")

    def _on_calibrate(self, _btn):
        # Run the 5-point calibration. Overlay captures the extremes; we
        # then push a new range to the head tracker. Calibration is simple:
        # the widget records the head pose at each point by polling the
        # tracker indirectly. Since we don't have a direct telemetry
        # channel, we use a default "reasonable range" for now and rely on
        # a future refinement where the tracker reports current angles.
        self.overlay = OverlayWindow(on_done=self._calib_done)

    def _calib_done(self):
        # For this first pass, commit a moderate range. A smarter version
        # would read actual captured head poses from the tracker.
        yaw_range = 0.7    # ~40° total
        pitch_range = 0.5  # ~28° total
        _send_ctl(f"calib {yaw_range} {pitch_range}")
        _send_ctl("baseline")


def main():
    w = Widget()
    w.show_all()
    # Position near top of screen
    display = Gdk.Display.get_default()
    monitor = display.get_primary_monitor() or display.get_monitor(0)
    geom = monitor.get_geometry()
    alloc = w.get_allocated_width(), w.get_allocated_height()
    w.move(geom.x + geom.width // 2 - 130, geom.y + 20)
    Gtk.main()


if __name__ == "__main__":
    main()
