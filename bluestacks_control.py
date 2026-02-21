"""
bluestacks_control.py
---------------------
Script 1: Programmatic control of the BlueStacks window for the Clash Royale RL agent.
Purpose: Execute a card placement action (tap card → tap arena) automatically,
         establishing the basic emulator control loop.

Requirements:
    pip install pywin32 pyautogui pillow

Usage:
    python bluestacks_control.py
"""

import time
import ctypes
import win32gui
import win32con
import win32api
import pyautogui
from PIL import ImageGrab

# ──────────────────────────────────────────────
# CONFIG — tweak these to match your setup
# ──────────────────────────────────────────────

# Partial window title to search for (BlueStacks 5 uses "BlueStacks App Player")
BLUESTACKS_TITLE_SUBSTRING = "BlueStacks"

# How long (seconds) to hold a "tap" before releasing (simulate a short touch)
TAP_DURATION = 0.05

# Action delay between card tap and arena tap
ACTION_DELAY = 0.15

# ──────────────────────────────────────────────
# WINDOW UTILITIES
# ──────────────────────────────────────────────

def find_bluestacks_window(title_substring: str = BLUESTACKS_TITLE_SUBSTRING):
    """
    Search all open windows and return the HWND of the first one whose
    title contains `title_substring` (case-insensitive).
    Raises RuntimeError if not found.
    """
    found = []

    def enum_callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title_substring.lower() in title.lower():
                found.append((hwnd, title))

    win32gui.EnumWindows(enum_callback, None)

    if not found:
        raise RuntimeError(
            f"No visible window with '{title_substring}' in title found. "
            "Is BlueStacks running?"
        )

    hwnd, title = found[0]
    print(f"[Window] Found: '{title}'  (HWND={hwnd})")
    return hwnd


def get_window_rect(hwnd) -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) of the window's client area in screen coords."""
    # GetClientRect gives size relative to client origin; ClientToScreen gives screen pos
    client_rect = win32gui.GetClientRect(hwnd)          # (0, 0, width, height)
    pt_origin = win32gui.ClientToScreen(hwnd, (0, 0))   # top-left of client in screen
    left, top = pt_origin
    right  = left + client_rect[2]
    bottom = top  + client_rect[3]
    return left, top, right, bottom


def focus_window(hwnd):
    """Bring the BlueStacks window to the foreground."""
    # Restore if minimized
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.3)  # give OS a moment to switch focus


def screenshot_window(hwnd) -> "PIL.Image.Image":
    """Capture a screenshot of the BlueStacks client area and return a PIL Image."""
    left, top, right, bottom = get_window_rect(hwnd)
    img = ImageGrab.grab(bbox=(left, top, right, bottom))
    return img


# ──────────────────────────────────────────────
# TAP / CLICK UTILITIES
# ──────────────────────────────────────────────

def tap_screen_coord(x: int, y: int, duration: float = TAP_DURATION):
    """
    Perform a single tap at absolute screen coordinates (x, y).
    Simulates a left mouse button press + release.
    """
    pyautogui.moveTo(x, y, duration=0.05)
    pyautogui.mouseDown(button="left")
    time.sleep(duration)
    pyautogui.mouseUp(button="left")


def relative_to_screen(hwnd, rel_x: float, rel_y: float) -> tuple[int, int]:
    """
    Convert a (rel_x, rel_y) position expressed as a fraction of the client area
    [0.0 – 1.0] into absolute screen pixel coordinates.

    Example:
        relative_to_screen(hwnd, 0.5, 0.9)  →  centre-bottom of the BlueStacks window
    """
    left, top, right, bottom = get_window_rect(hwnd)
    width  = right  - left
    height = bottom - top
    screen_x = left + int(rel_x * width)
    screen_y = top  + int(rel_y * height)
    return screen_x, screen_y


# ──────────────────────────────────────────────
# CARD & ARENA LAYOUT
# ──────────────────────────────────────────────

# Card bar: four card slots at the bottom of the screen.
# These relative positions work for a standard portrait BlueStacks layout.
# Adjust if your window resolution differs.
CARD_SLOTS = {
    1: (0.285, 0.925),   # leftmost card
    2: (0.415, 0.925),
    3: (0.545, 0.925),
    4: (0.675, 0.925),   # rightmost card
}

# Predefined arena drop zones (9 regions: 3 cols × 3 rows, player's half only).
# Format: { zone_name: (rel_x, rel_y) }
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
    """
    Execute a full card placement action:
        1. Tap the card in `card_slot`   (1–4)
        2. Wait briefly
        3. Tap the `arena_zone` to deploy

    Args:
        hwnd       : BlueStacks window handle
        card_slot  : Card index 1–4 (left to right in hand)
        arena_zone : One of the keys in ARENA_ZONES
    """
    if card_slot not in CARD_SLOTS:
        raise ValueError(f"card_slot must be 1–4, got {card_slot}")
    if arena_zone not in ARENA_ZONES:
        raise ValueError(f"arena_zone '{arena_zone}' not recognised. "
                         f"Valid zones: {list(ARENA_ZONES.keys())}")

    # --- Step 1: tap card ---
    cx, cy = relative_to_screen(hwnd, *CARD_SLOTS[card_slot])
    print(f"[Action] Tapping card slot {card_slot}  → screen ({cx}, {cy})")
    tap_screen_coord(cx, cy)

    time.sleep(ACTION_DELAY)

    # --- Step 2: tap arena zone ---
    ax, ay = relative_to_screen(hwnd, *ARENA_ZONES[arena_zone])
    print(f"[Action] Placing at zone '{arena_zone}'  → screen ({ax}, {ay})")
    tap_screen_coord(ax, ay)

    print("[Action] Card placement complete.\n")


def wait_action(hwnd):
    """Do nothing (the agent chooses not to play a card this step)."""
    print("[Action] Wait — no card played this step.\n")


# ──────────────────────────────────────────────
# DEMO / SMOKE TEST
# ──────────────────────────────────────────────

def demo():
    """
    Quick smoke test:
        - Finds the BlueStacks window
        - Takes a screenshot and saves it
        - Cycles through placing each card at center_mid
          (useful for verifying coordinate mapping)

    Run this BEFORE the RL loop to confirm control works.
    """
    print("=== BlueStacks Control — Smoke Test ===\n")

    hwnd = find_bluestacks_window()
    focus_window(hwnd)

    # Save a reference screenshot so you can verify the coordinate layout
    img = screenshot_window(hwnd)
    img.save("bluestacks_screenshot.png")
    print("[Screenshot] Saved to bluestacks_screenshot.png\n")

    # Give yourself 3 seconds to switch to the emulator / start a match
    print("Starting card placement demo in 3 seconds ...")
    time.sleep(3)

    # Place card 1 at center_mid as a connectivity test
    place_card(hwnd, card_slot=1, arena_zone="center_mid")
    time.sleep(1)

    print("=== Smoke test complete ===")


# ──────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────

if __name__ == "__main__":
    demo()