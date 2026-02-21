"""
match_lifecycle.py
------------------
Handles the full match lifecycle for the Clash Royale RL agent:
  1. Detect match end via the "Winner!" banner
  2. Determine win/loss by the banner colour (blue = we won, pink = they won)
  3. Click OK / Continue
  4. Click Battle again
  5. Wait for the next match to start (elixir bar reappears)

No reference screenshots needed — everything is colour-based.

Standalone usage:
    python match_lifecycle.py --test      # print current screen state
    python match_lifecycle.py --capture   # save OK / Battle button refs
"""

import sys
import time
import cv2
import numpy as np
import pyautogui
from pathlib import Path
from PIL import ImageGrab
import win32gui

# ── Paths ─────────────────────────────────────────────────────────────────────
REF_DIR = Path("clash_bot/lifecycle_refs")
REF_DIR.mkdir(parents=True, exist_ok=True)

# ══════════════════════════════════════════════════════════════════════════════
#  LOW-LEVEL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_client_rect(hwnd):
    r   = win32gui.GetClientRect(hwnd)
    org = win32gui.ClientToScreen(hwnd, (0, 0))
    return org[0], org[1], org[0] + r[2], org[1] + r[3]


def grab_full(hwnd) -> np.ndarray:
    """Capture the BlueStacks client area as an RGB numpy array."""
    left, top, right, bottom = get_client_rect(hwnd)
    return np.array(ImageGrab.grab(bbox=(left, top, right, bottom)))


def crop(arr: np.ndarray, roi: tuple) -> np.ndarray:
    """
    Crop a region from a full-window RGB array.
    roi = (x1_frac, y1_frac, x2_frac, y2_frac)  — all 0.0 to 1.0
    """
    h, w = arr.shape[:2]
    x1, y1 = int(roi[0] * w), int(roi[1] * h)
    x2, y2 = int(roi[2] * w), int(roi[3] * h)
    return arr[y1:y2, x1:x2]


def color_frac(region: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> float:
    """Fraction of pixels in region whose RGB falls within [lo, hi]."""
    mask = np.all((region >= lo) & (region <= hi), axis=2)
    return mask.sum() / max(mask.size, 1)


def click_rel(hwnd, rel_x: float, rel_y: float, delay: float = 0.2):
    """Click at a fractional position within the BlueStacks client area."""
    left, top, right, bottom = get_client_rect(hwnd)
    w, h = right - left, bottom - top
    pyautogui.click(left + int(rel_x * w), top + int(rel_y * h))
    time.sleep(delay)


def click_roi_centre(hwnd, roi: tuple, delay: float = 0.3):
    click_rel(hwnd, (roi[0] + roi[2]) / 2, (roi[1] + roi[3]) / 2, delay)


# ══════════════════════════════════════════════════════════════════════════════
#  SCREEN COLOUR SIGNATURES
# ══════════════════════════════════════════════════════════════════════════════

# "Winner!" banner — wide strip covering the top portion of the screen.
# We scan broadly so it doesn't matter exactly where the banner appears.
WINNER_ROI = (0.10, 0.03, 0.90, 0.30)

# WE win  -> "Winner!" is BLUE
WIN_BLUE_LO     = np.array([ 30,  80, 160], dtype=np.uint8)
WIN_BLUE_HI     = np.array([130, 180, 255], dtype=np.uint8)
WIN_BLUE_THRESH = 0.04

# THEY win -> "Winner!" is PINK / MAGENTA
WIN_PINK_LO     = np.array([180,  30, 120], dtype=np.uint8)
WIN_PINK_HI     = np.array([255, 130, 220], dtype=np.uint8)
WIN_PINK_THRESH = 0.04

# End-screen golden background — present on ALL result screens
END_ROI      = (0.10, 0.05, 0.90, 0.35)
END_GOLD_LO  = np.array([160, 110,  10], dtype=np.uint8)
END_GOLD_HI  = np.array([255, 220, 110], dtype=np.uint8)
END_THRESH   = 0.08

# OK / Continue button — blue-purple, centre-bottom of result screen
OK_ROI    = (0.28, 0.68, 0.72, 0.86)
OK_LO     = np.array([ 30,  60, 160], dtype=np.uint8)
OK_HI     = np.array([120, 160, 255], dtype=np.uint8)
OK_THRESH = 0.06

# Battle / Play Again button — green, main lobby
BATTLE_ROI    = (0.20, 0.72, 0.80, 0.90)
BATTLE_LO     = np.array([ 40, 150,  30], dtype=np.uint8)
BATTLE_HI     = np.array([130, 255, 120], dtype=np.uint8)
BATTLE_THRESH = 0.06

# Elixir bar — pink, ONLY visible during a live match
ELIXIR_ROI    = (0.5498, 0.9625, 0.9788, 0.9883)   # calibrated
ELIXIR_LO     = np.array([200,  50, 150], dtype=np.uint8)
ELIXIR_HI     = np.array([255, 160, 255], dtype=np.uint8)
ELIXIR_THRESH = 0.05


# ══════════════════════════════════════════════════════════════════════════════
#  STATE DETECTORS
# ══════════════════════════════════════════════════════════════════════════════

def is_match_live(hwnd) -> bool:
    """True while inside a live match (elixir bar is visible)."""
    arr = grab_full(hwnd)
    return color_frac(crop(arr, ELIXIR_ROI), ELIXIR_LO, ELIXIR_HI) >= ELIXIR_THRESH


def is_end_screen(hwnd) -> bool:
    """
    True when the post-match results overlay is on screen.
    Checks for the golden background OR either Winner banner colour.
    """
    arr       = grab_full(hwnd)
    gold      = color_frac(crop(arr, END_ROI),    END_GOLD_LO, END_GOLD_HI)
    blue      = color_frac(crop(arr, WINNER_ROI), WIN_BLUE_LO, WIN_BLUE_HI)
    pink      = color_frac(crop(arr, WINNER_ROI), WIN_PINK_LO, WIN_PINK_HI)
    return gold >= END_THRESH or blue >= WIN_BLUE_THRESH or pink >= WIN_PINK_THRESH


def parse_result(hwnd) -> dict:
    """
    Determine win/loss from the colour of the Winner! banner.
      Blue  Winner! -> WE won
      Pink  Winner! -> THEY won
    Returns { "won": bool }
    """
    arr       = grab_full(hwnd)
    region    = crop(arr, WINNER_ROI)
    blue_frac = color_frac(region, WIN_BLUE_LO, WIN_BLUE_HI)
    pink_frac = color_frac(region, WIN_PINK_LO, WIN_PINK_HI)

    print(f"[Lifecycle] Winner banner — blue={blue_frac:.3f}  pink={pink_frac:.3f}")

    if blue_frac >= WIN_BLUE_THRESH and blue_frac > pink_frac:
        print("[Lifecycle] -> VICTORY (blue banner)")
        return {"won": True}
    elif pink_frac >= WIN_PINK_THRESH and pink_frac > blue_frac:
        print("[Lifecycle] -> DEFEAT (pink banner)")
        return {"won": False}
    else:
        print("[Lifecycle] -> Could not read banner clearly — defaulting to DEFEAT")
        print("             Tweak WIN_BLUE_THRESH / WIN_PINK_THRESH if this recurs.")
        return {"won": False}


# ══════════════════════════════════════════════════════════════════════════════
#  BUTTON INTERACTIONS
# ══════════════════════════════════════════════════════════════════════════════

def _template_score(arr: np.ndarray, ref_path: Path, roi: tuple) -> float:
    """Optional template-match boost on top of colour. Returns 0 if no ref."""
    if not ref_path.exists():
        return 0.0
    ref = cv2.imread(str(ref_path), cv2.IMREAD_GRAYSCALE)
    if ref is None:
        return 0.0
    region = crop(arr, roi)
    gray   = cv2.cvtColor(region, cv2.COLOR_RGB2GRAY)
    ref_r  = cv2.resize(ref, (gray.shape[1], gray.shape[0]))
    return float(cv2.matchTemplate(gray, ref_r, cv2.TM_CCOEFF_NORMED).max())


def click_ok(hwnd, timeout: float = 12.0) -> bool:
    """Wait for and click the OK / Continue button. Returns True if clicked."""
    print("[Lifecycle] Waiting for OK button...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        arr = grab_full(hwnd)
        if (color_frac(crop(arr, OK_ROI), OK_LO, OK_HI) >= OK_THRESH or
                _template_score(arr, REF_DIR / "ok_button.png", OK_ROI) > 0.70):
            click_roi_centre(hwnd, OK_ROI)
            print("[Lifecycle] OK clicked.")
            return True
        time.sleep(0.3)
    print("[Lifecycle] WARN: OK button not found within timeout.")
    return False


def click_battle(hwnd, timeout: float = 25.0) -> bool:
    """Wait for and click the Battle / Play Again button. Returns True if clicked."""
    print("[Lifecycle] Waiting for Battle button...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        arr = grab_full(hwnd)
        if (color_frac(crop(arr, BATTLE_ROI), BATTLE_LO, BATTLE_HI) >= BATTLE_THRESH or
                _template_score(arr, REF_DIR / "battle_button.png", BATTLE_ROI) > 0.70):
            click_roi_centre(hwnd, BATTLE_ROI)
            print("[Lifecycle] Battle clicked.")
            return True
        time.sleep(0.3)
    print("[Lifecycle] WARN: Battle button not found within timeout.")
    return False


def wait_for_match_start(hwnd, timeout: float = 60.0) -> bool:
    """Block until the elixir bar reappears. Returns True when match starts."""
    print("[Lifecycle] Waiting for next match to start...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_match_live(hwnd):
            print("[Lifecycle] Match started!")
            return True
        time.sleep(0.5)
    print("[Lifecycle] WARN: Match did not start within timeout.")
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  FULL POST-MATCH SEQUENCE  (called by rl_agent.py)
# ══════════════════════════════════════════════════════════════════════════════

def handle_match_end(hwnd,
                     timeout_result: float = 10.0,
                     timeout_ok:     float = 12.0,
                     timeout_battle: float = 25.0,
                     timeout_start:  float = 60.0) -> dict:
    """
    Full post-match automation:
        1. Wait for end screen
        2. Parse win/loss from Winner! banner colour
        3. Click OK (and any secondary screens e.g. chest unlock)
        4. Click Battle
        5. Wait for next match to start
    Returns { "won": bool }
    """
    print("\n[Lifecycle] Post-match sequence starting...")

    # 1. Confirm end screen
    deadline = time.time() + timeout_result
    while not is_end_screen(hwnd):
        if time.time() > deadline:
            print("[Lifecycle] End screen not detected — defaulting to DEFEAT.")
            return {"won": False}
        time.sleep(0.3)

    time.sleep(0.6)   # let screen settle before reading colours

    # 2. Parse result
    result = parse_result(hwnd)

    # 3. Click OK
    click_ok(hwnd, timeout=timeout_ok)
    time.sleep(1.0)

    # Handle secondary OK screen (chest unlock etc.)
    arr = grab_full(hwnd)
    if color_frac(crop(arr, OK_ROI), OK_LO, OK_HI) >= OK_THRESH:
        click_roi_centre(hwnd, OK_ROI)
        print("[Lifecycle] Secondary OK clicked.")
        time.sleep(1.0)

    # 4. Click Battle
    click_battle(hwnd, timeout=timeout_battle)

    # 5. Wait for next match
    wait_for_match_start(hwnd, timeout=timeout_start)

    print(f"[Lifecycle] Sequence complete: {'VICTORY' if result['won'] else 'DEFEAT'}\n")
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  OPTIONAL BUTTON REFERENCE CAPTURE
# ══════════════════════════════════════════════════════════════════════════════

def capture_refs(hwnd):
    """Save greyscale crops of OK and Battle buttons for optional template matching."""
    for name, roi in [("ok_button", OK_ROI), ("battle_button", BATTLE_ROI)]:
        input(f"\n[Capture] Show '{name}' on screen then press ENTER...")
        arr    = grab_full(hwnd)
        region = crop(arr, roi)
        gray   = cv2.cvtColor(region, cv2.COLOR_RGB2GRAY)
        out    = REF_DIR / f"{name}.png"
        cv2.imwrite(str(out), gray)
        print(f"   Saved -> {out}")
    print("\n[Capture] Done. No result screenshots needed — win/loss is colour-based.\n")


# ══════════════════════════════════════════════════════════════════════════════
#  STANDALONE USAGE
# ══════════════════════════════════════════════════════════════════════════════

def _find_bluestacks():
    found = []
    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            if "bluestacks" in win32gui.GetWindowText(hwnd).lower():
                found.append(hwnd)
    win32gui.EnumWindows(cb, None)
    if not found:
        raise RuntimeError("BlueStacks not found!")
    return found[0]


if __name__ == "__main__":
    hwnd = _find_bluestacks()

    if "--capture" in sys.argv:
        capture_refs(hwnd)
    elif "--test" in sys.argv:
        print("\n[Test] Current screen state:")
        print(f"  is_match_live : {is_match_live(hwnd)}")
        print(f"  is_end_screen : {is_end_screen(hwnd)}")
        if is_end_screen(hwnd):
            print(f"  parse_result  : {parse_result(hwnd)}")
    else:
        print("Usage:")
        print("  python match_lifecycle.py --test")
        print("  python match_lifecycle.py --capture")