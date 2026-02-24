"""
mumu_control.py
---------------
Programmatic control of the MuMu Player emulator window for the Clash Royale RL agent.
Purpose: Execute card placement (tap card → tap arena), screenshots, and window focus.
Replaces the previous BlueStacks-specific module.

Requirements:
    pip install pywin32 pyautogui pillow

Usage:
    python mumu_control.py
"""

import time
import win32gui
import win32con
import pyautogui
from PIL import ImageGrab

# ──────────────────────────────────────────────
# CONFIG — tweak to match your MuMu Player window
# ──────────────────────────────────────────────

# Window title (case-insensitive). Set to the emulator/game window title.
ANDROID_WINDOW_TITLE = "Android Device"

TAP_DURATION = 0.08
ACTION_DELAY = 0.25

# ──────────────────────────────────────────────
# WINDOW UTILITIES
# ──────────────────────────────────────────────

def find_mumu_window(title_substring: str | None = None):
    """
    Return HWND of the first visible window whose title contains the substring
    (default: "Android Device"). Raises RuntimeError if not found.
    """
    search = (title_substring or ANDROID_WINDOW_TITLE).lower()
    found = []

    def enum_callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            if search in win32gui.GetWindowText(hwnd).lower():
                found.append(hwnd)

    win32gui.EnumWindows(enum_callback, None)
    if not found:
        raise RuntimeError(
            f"No visible window with '{search}' in title. Is the emulator running?"
        )

    hwnd = found[0]
    print(f"[Window] Found: '{win32gui.GetWindowText(hwnd)}'  (HWND={hwnd})")
    return hwnd


def get_window_rect(hwnd) -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) of the window's client area in screen coords."""
    client_rect = win32gui.GetClientRect(hwnd)
    pt_origin = win32gui.ClientToScreen(hwnd, (0, 0))
    left, top = pt_origin
    right  = left + client_rect[2]
    bottom = top  + client_rect[3]
    return left, top, right, bottom


def focus_window(hwnd):
    """Bring the emulator window to the foreground."""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.3)


def screenshot_window(hwnd) -> "PIL.Image.Image":
    """Capture the emulator client area and return a PIL Image."""
    left, top, right, bottom = get_window_rect(hwnd)
    return ImageGrab.grab(bbox=(left, top, right, bottom))


# ──────────────────────────────────────────────
# TAP / CLICK
# ──────────────────────────────────────────────

def tap_screen_coord(x: int, y: int, duration: float = TAP_DURATION):
    """Tap at absolute screen coordinates (left mouse down/up)."""
    pyautogui.moveTo(x, y, duration=0.05)
    pyautogui.mouseDown(button="left")
    time.sleep(duration)
    pyautogui.mouseUp(button="left")


def relative_to_screen(hwnd, rel_x: float, rel_y: float) -> tuple[int, int]:
    """Convert (rel_x, rel_y) in [0,1] to absolute screen pixel coords within the emulator client area."""
    left, top, right, bottom = get_window_rect(hwnd)
    w, h = right - left, bottom - top
    return left + int(rel_x * w), top + int(rel_y * h)


# ──────────────────────────────────────────────
# CARD & ARENA LAYOUT (portrait game view)
# Centers must match where the card icons are; same as hand-detection ROIs in rl_agent.
# ──────────────────────────────────────────────

# Slot centers = center of each card slot (aligned with CARD_SLOT_ROIS in rl_agent)
CARD_SLOTS = {
    1: (0.265, 0.905),   # center of (0.22, 0.87, 0.31, 0.94)
    2: (0.400, 0.905),
    3: (0.535, 0.905),
    4: (0.670, 0.905),
}

ARENA_ZONES = {
    "left_back":    (0.20, 0.72),
    "center_back":  (0.50, 0.72),
    "right_back":   (0.80, 0.72),
    "left_mid":     (0.20, 0.62),
    "center_mid":   (0.50, 0.62),
    "right_mid":    (0.80, 0.62),
    "left_front":   (0.20, 0.52),
    "center_front": (0.50, 0.52),
    "right_front":  (0.80, 0.52),
}


# ──────────────────────────────────────────────
# HIGH-LEVEL ACTION
# ──────────────────────────────────────────────

def place_card(hwnd, card_slot: int, arena_zone: str):
    """Tap the card in card_slot (1–4), then tap arena_zone to deploy. Focuses window first."""
    if card_slot not in CARD_SLOTS:
        raise ValueError(f"card_slot must be 1–4, got {card_slot}")
    if arena_zone not in ARENA_ZONES:
        raise ValueError(f"arena_zone '{arena_zone}' not in ARENA_ZONES")

    focus_window(hwnd)
    cx, cy = relative_to_screen(hwnd, *CARD_SLOTS[card_slot])
    print(f"[Action] Tapping card slot {card_slot}  → screen ({cx}, {cy})")
    tap_screen_coord(cx, cy)
    time.sleep(ACTION_DELAY)
    ax, ay = relative_to_screen(hwnd, *ARENA_ZONES[arena_zone])
    print(f"[Action] Placing at zone '{arena_zone}'  → screen ({ax}, {ay})")
    tap_screen_coord(ax, ay)
    print("[Action] Card placement complete.\n")


def wait_action(hwnd):
    """No card played this step."""
    print("[Action] Wait — no card played this step.\n")


# ──────────────────────────────────────────────
# DEMO
# ──────────────────────────────────────────────

def demo():
    """Smoke test: find window, focus, place one card."""
    print("=== Emulator Control — Smoke Test ===\n")
    hwnd = find_mumu_window()
    focus_window(hwnd)
    print("Starting card placement in 3 seconds ...")
    time.sleep(3)
    place_card(hwnd, card_slot=1, arena_zone="center_mid")
    time.sleep(1)
    print("=== Smoke test complete ===")


if __name__ == "__main__":
    demo()
