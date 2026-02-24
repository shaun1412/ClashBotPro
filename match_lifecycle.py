"""
match_lifecycle.py
------------------
Handles the full match lifecycle for the Clash Royale RL agent:
  1. Detect match end via the "Winner!" screen
  2. Determine win/loss by POSITION of "Winner!":
       Top 30% of screen  -> opponent won (DEFEAT)
       Lower 70%          -> we won (VICTORY)
  3. Click OK / Continue
  4. Click Battle again
  5. Wait for the next match to start (elixir bar reappears)

No reference images needed for win/loss — position-based detection only.

Standalone usage:
    python match_lifecycle.py --test      # print current screen state
    python match_lifecycle.py --capture   # save OK / Battle button refs (optional)
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

# ══════════════════════════════════════════════════════════════════════════════
#  LOW-LEVEL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def get_client_rect(hwnd):
    r   = win32gui.GetClientRect(hwnd)
    org = win32gui.ClientToScreen(hwnd, (0, 0))
    return org[0], org[1], org[0] + r[2], org[1] + r[3]


def grab_full(hwnd) -> np.ndarray:
    """Capture the emulator (MuMu) client area as an RGB numpy array."""
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
    """Click at a fractional position within the emulator (MuMu) client area."""
    left, top, right, bottom = get_client_rect(hwnd)
    w, h = right - left, bottom - top
    pyautogui.click(left + int(rel_x * w), top + int(rel_y * h))
    time.sleep(delay)


def click_roi_centre(hwnd, roi: tuple, delay: float = 0.3):
    click_rel(hwnd, (roi[0] + roi[2]) / 2, (roi[1] + roi[3]) / 2, delay)


# ══════════════════════════════════════════════════════════════════════════════
#  SCREEN COLOUR SIGNATURES
# ══════════════════════════════════════════════════════════════════════════════

# Win detection by POSITION of "Winner!" banner (no ref images needed):
#   Opponent wins -> "Winner!" appears in the TOP 30% of the screen
#   We win       -> "Winner!" appears in the LOWER 70% of the screen
TOP_ROI    = (0.0, 0.0, 1.0, 0.30)    # top 30% — opponent's "Winner!"
LOWER_ROI  = (0.0, 0.30, 1.0, 1.0)    # rest of screen — our "Winner!"

# Color ranges for the "Winner!" banner (same style in both positions)
WIN_BLUE_LO     = np.array([ 30,  80, 160], dtype=np.uint8)
WIN_BLUE_HI     = np.array([130, 180, 255], dtype=np.uint8)
WIN_PINK_LO     = np.array([180,  30, 120], dtype=np.uint8)
WIN_PINK_HI     = np.array([255, 130, 220], dtype=np.uint8)
WIN_COLOR_THRESH = 0.04   # min fraction of ROI matching banner color

# End-screen golden background — present on ALL result screens
END_ROI      = (0.10, 0.05, 0.90, 0.35)
END_GOLD_LO  = np.array([160, 110,  10], dtype=np.uint8)
END_GOLD_HI  = np.array([255, 220, 110], dtype=np.uint8)
END_THRESH   = 0.12   # stricter so gameplay isn't mistaken for end screen

# OK / Continue button — blue-purple, centre-bottom of result screen
OK_ROI    = (0.28, 0.68, 0.72, 0.86)
OK_LO     = np.array([ 30,  60, 160], dtype=np.uint8)
OK_HI     = np.array([120, 160, 255], dtype=np.uint8)
OK_THRESH = 0.06

# Battle / Play Again button — green, main lobby (ROI widened so we don't miss it)
BATTLE_ROI    = (0.15, 0.65, 0.85, 0.95)
BATTLE_LO     = np.array([ 25, 120,  20], dtype=np.uint8)   # looser green range
BATTLE_HI     = np.array([150, 255, 140], dtype=np.uint8)
BATTLE_THRESH = 0.05
BATTLE_TEMPLATE_THRESH = 0.58   # template match threshold (approximate match)

# Elixir bar — pink, ONLY visible during a live match (ROI slightly widened for robustness)
ELIXIR_ROI    = (0.50, 0.955, 0.99, 0.995)
ELIXIR_LO     = np.array([180,  40, 140], dtype=np.uint8)
ELIXIR_HI     = np.array([255, 180, 255], dtype=np.uint8)
ELIXIR_THRESH = 0.03   # lower so a single bad frame doesn't flip "match ended"

# ══════════════════════════════════════════════════════════════════════════════
#  STATE DETECTORS
# ══════════════════════════════════════════════════════════════════════════════

def _winner_strength_in_roi(arr: np.ndarray, roi: tuple) -> float:
    """Return max of blue/pink fraction in ROI (indicates 'Winner!' banner presence)."""
    region = crop(arr, roi)
    blue = color_frac(region, WIN_BLUE_LO, WIN_BLUE_HI)
    pink = color_frac(region, WIN_PINK_LO, WIN_PINK_HI)
    return max(blue, pink)


def is_match_live(hwnd) -> bool:
    """True while inside a live match (elixir bar is visible)."""
    arr = grab_full(hwnd)
    return color_frac(crop(arr, ELIXIR_ROI), ELIXIR_LO, ELIXIR_HI) >= ELIXIR_THRESH


def is_match_live_confirmed(hwnd, num_checks: int = 5, interval: float = 0.4) -> bool:
    """
    Require multiple consecutive "elixir missing" checks before returning False.
    Returns False only if the bar is missing for every check (avoids ending on a single glitch).
    """
    for _ in range(num_checks):
        if is_match_live(hwnd):
            return True
        time.sleep(interval)
    return False


def is_end_screen(hwnd) -> bool:
    """
    True when the post-match "Winner!" screen is visible.
    Uses: gold background OR 'Winner!' banner in top 30% OR lower 70% (position-based).
    """
    arr = grab_full(hwnd)
    gold = color_frac(crop(arr, END_ROI), END_GOLD_LO, END_GOLD_HI)
    top_strength  = _winner_strength_in_roi(arr, TOP_ROI)
    lower_strength = _winner_strength_in_roi(arr, LOWER_ROI)
    return (gold >= END_THRESH or
            top_strength >= WIN_COLOR_THRESH or
            lower_strength >= WIN_COLOR_THRESH)


def parse_result(hwnd) -> dict:
    """
    Determine win/loss from the end screen by POSITION of "Winner!" banner:
      Top 30% of screen  -> opponent won (DEFEAT)
      Lower 70%         -> we won (VICTORY)
    No reference images needed. Returns { "won": bool }
    """
    arr = grab_full(hwnd)
    top_strength   = _winner_strength_in_roi(arr, TOP_ROI)
    lower_strength = _winner_strength_in_roi(arr, LOWER_ROI)
    print(f"[Lifecycle] Winner (position) — top30%={top_strength:.3f}  lower70%={lower_strength:.3f}")
    if top_strength >= WIN_COLOR_THRESH and top_strength > lower_strength:
        print("[Lifecycle] -> DEFEAT (Winner! in top 30%)")
        return {"won": False}
    if lower_strength >= WIN_COLOR_THRESH and lower_strength >= top_strength:
        print("[Lifecycle] -> VICTORY (Winner! in lower portion)")
        return {"won": True}
    print("[Lifecycle] -> Could not determine — defaulting to DEFEAT")
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


def _find_battle_template_click(arr: np.ndarray) -> tuple[float, float] | None:
    """
    Search the bottom half of the screen for the Battle button template.
    Returns (rel_x, rel_y) to click (0–1) or None if no good match.
    """
    ref_path = REF_DIR / "battle_button.png"
    if not ref_path.exists():
        return None
    ref = cv2.imread(str(ref_path), cv2.IMREAD_GRAYSCALE)
    if ref is None:
        return None
    h, w = arr.shape[:2]
    # Search bottom 50% of screen (Battle is always in lower part)
    search = crop(arr, (0.0, 0.50, 1.0, 1.0))
    gray = cv2.cvtColor(search, cv2.COLOR_RGB2GRAY)
    sh, sw = gray.shape[:2]
    ref_h, ref_w = ref.shape[:2]
    best_val, best_x, best_y = 0.0, 0, 0
    for scale in (0.6, 0.8, 1.0, 1.2):
        tw = max(10, min(int(ref_w * scale), sw - 5))
        th = max(10, min(int(ref_h * scale), sh - 5))
        if tw > sw or th > sh:
            continue
        ref_scaled = cv2.resize(ref, (tw, th))
        try:
            result = cv2.matchTemplate(gray, ref_scaled, cv2.TM_CCOEFF_NORMED)
            val = float(result.max())
            if val > best_val:
                best_val = val
                idx = np.unravel_index(np.argmax(result), result.shape)
                best_y = idx[0] + th // 2
                best_x = idx[1] + tw // 2
        except cv2.error:
            pass
    if best_val < BATTLE_TEMPLATE_THRESH:
        return None
    # Convert to full-screen relative coords (search is bottom half: y from 0.5 to 1.0)
    rel_x = best_x / w
    rel_y = 0.50 + (best_y / h)
    return (rel_x, rel_y)


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
        # 1) Color in fixed ROI
        if color_frac(crop(arr, BATTLE_ROI), BATTLE_LO, BATTLE_HI) >= BATTLE_THRESH:
            click_roi_centre(hwnd, BATTLE_ROI)
            print("[Lifecycle] Battle clicked (color).")
            return True
        # 2) Template in fixed ROI
        if _template_score(arr, REF_DIR / "battle_button.png", BATTLE_ROI) > BATTLE_TEMPLATE_THRESH:
            click_roi_centre(hwnd, BATTLE_ROI)
            print("[Lifecycle] Battle clicked (template ROI).")
            return True
        # 3) Search bottom half of screen for template (button may be off-center)
        pos = _find_battle_template_click(arr)
        if pos is not None:
            click_rel(hwnd, pos[0], pos[1])
            print("[Lifecycle] Battle clicked (template search).")
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
    """Save refs for OK and Battle buttons (optional — colour detection also works)."""
    REF_DIR.mkdir(parents=True, exist_ok=True)
    for name, roi in [("ok_button", OK_ROI), ("battle_button", BATTLE_ROI)]:
        input(f"\n[Capture] Show '{name}' on screen then press ENTER...")
        arr    = grab_full(hwnd)
        region = crop(arr, roi)
        gray   = cv2.cvtColor(region, cv2.COLOR_RGB2GRAY)
        out    = REF_DIR / f"{name}.png"
        cv2.imwrite(str(out), gray)
        print(f"   Saved -> {out}")
    print("\n[Capture] Done. Win/loss uses position (top 30% vs lower 70%), not refs.\n")


# ══════════════════════════════════════════════════════════════════════════════
#  STANDALONE USAGE
# ══════════════════════════════════════════════════════════════════════════════

def _find_mumu():
    """Find emulator window by title (Android Device)."""
    target = "android device"
    found = []
    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and target in win32gui.GetWindowText(hwnd).lower():
            found.append(hwnd)
    win32gui.EnumWindows(cb, None)
    if not found:
        raise RuntimeError("Window titled 'Android Device' not found. Is the emulator running?")
    return found[0]


if __name__ == "__main__":
    hwnd = _find_mumu()

    if "--capture" in sys.argv:
        capture_refs(hwnd)
    elif "--test" in sys.argv:
        print("\n[Test] Current screen state:")
        arr = grab_full(hwnd)
        top_str = _winner_strength_in_roi(arr, TOP_ROI)
        lower_str = _winner_strength_in_roi(arr, LOWER_ROI)
        print(f"  winner strength: top30%={top_str:.3f}  lower70%={lower_str:.3f}")
        print(f"  is_match_live : {is_match_live(hwnd)}")
        print(f"  is_end_screen : {is_end_screen(hwnd)}")
        if is_end_screen(hwnd):
            print(f"  parse_result  : {parse_result(hwnd)}")
    else:
        print("Usage:")
        print("  python match_lifecycle.py --test")
        print("  python match_lifecycle.py --capture   # OK + Battle refs (optional)")