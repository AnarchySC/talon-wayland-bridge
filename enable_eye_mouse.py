from talon import actions, cron

def disable_eye_mouse():
    try:
        actions.tracking.control_toggle(False)
        print("[user] eye mouse DISABLED (using head tracking as primary cursor)")
    except Exception as e:
        print(f"[user] disable failed: {e}")

cron.after("2s", disable_eye_mouse)
