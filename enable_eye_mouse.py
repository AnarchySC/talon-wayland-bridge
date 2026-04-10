from talon import actions, cron

def enable_eye_mouse():
    try:
        actions.tracking.control_toggle(True)
        print("[user] eye mouse enabled")
    except Exception as e:
        print(f"[user] enable failed: {e}")

cron.after("2s", enable_eye_mouse)
