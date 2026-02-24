"""
match_lifecycle.py
------------------
Handles the full match lifecycle for the Clash Royale RL agent:
  1. Detect match end via the "Winner!" screen (ref images or gold fallback)
  2. Determine win/loss by blue vs pink Winner ref match (or colour fallback)
  3. Click OK / Continue
  4. Click Battle again
  5. Wait for the next match to start (elixir bar reappears)

Winner detection (preferred): add your own ref images for reliable detection:
  - clash_bot/lifecycle_refs/winner_blue.png   — screenshot/crop when YOU win (blue Winner)
  - clash_bot/lifecycle_refs/winner_pink.png   — screenshot/crop when OPPONENT wins (pink Winner)
Crop just the "Winner!" banner/text area from each screen; multi-scale matching is used.

Standalone usage:
    python match_lifecycle.py --test      # print current screen state
    python match_lifecycle.py --capture   # save OK / Battle / Winner refs
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

# "Winner!" text can appear ANYWHERE on screen — we scan the full client area
# via a grid of overlapping tiles and take the max blue / max pink fraction.
WINNER_FULL_SCREEN = True   # if True, use grid scan; else use legacy WINNER_ROI strip
WINNER_ROI = (0.10, 0.03, 0.90, 0.30)   # legacy top strip (used when WINNER_FULL_SCREEN is False)

# Grid scan: tile size and step as fraction of screen (overlapping windows)
WINNER_TILE_W, WINNER_TILE_H = 0.35, 0.25   # each tile 35% x 25% of screen
WINNER_STEP_W, WINNER_STEP_H = 0.12, 0.10   # step so we don't miss the banner

# WE win  -> "Winner!" is BLUE (blueish shade)
WIN_BLUE_LO     = np.array([ 30,  80, 160], dtype=np.uint8)
WIN_BLUE_HI     = np.array([130, 180, 255], dtype=np.uint8)
WIN_BLUE_THRESH = 0.04

# THEY win -> "Winner!" is PINK (pinkish shade)
WIN_PINK_LO     = np.array([180,  30, 120], dtype=np.uint8)
WIN_PINK_HI     = np.array([255, 130, 220], dtype=np.uint8)
WIN_PINK_THRESH = 0.04

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

# Elixir bar — pink, ONLY visible during a live match
ELIXIR_ROI    = (0.5498, 0.9625, 0.9788, 0.9883)   # calibrated
ELIXIR_LO     = np.array([200,  50, 150], dtype=np.uint8)
ELIXIR_HI     = np.array([255, 160, 255], dtype=np.uint8)
ELIXIR_THRESH = 0.05

# Winner ref images (optional but recommended — use your screenshots for reliable detection)
WINNER_REF_BLUE = REF_DIR / "winner_blue.jpeg"   # crop of "Winner!" when agent wins (blue)
WINNER_REF_PINK = REF_DIR / "winner_pink.jpeg"   # crop of "Winner!" when opponent wins (pink)
WINNER_REF_THRESH = 0.65   # min template match (stricter to avoid false end during gameplay)
WINNER_REF_SCALES = (0.5, 0.7, 0.9, 1.1, 1.3)  # multi-scale matching for different resolutions
# ROI to crop when capturing winner refs (banner usually in upper-center): x1, y1, x2, y2 frac
WINNER_CAPTURE_ROI = (0.15, 0.12, 0.85, 0.45)

# Cached ref templates (loaded once)
_winner_ref_blue_gray = None
_winner_ref_pink_gray = None


# ══════════════════════════════════════════════════════════════════════════════
#  STATE DETECTORS
# ══════════════════════════════════════════════════════════════════════════════

def _winner_scan_full_screen(arr: np.ndarray) -> tuple[float, float]:
    """
    Scan the full screen with overlapping tiles; return (max_blue_frac, max_pink_frac).
    So wherever the 'Winner!' text appears (blue or pink), we catch it.
    """
    h, w = arr.shape[:2]
    tw = max(1, int(w * WINNER_TILE_W))
    th = max(1, int(h * WINNER_TILE_H))
    sw = max(1, int(w * WINNER_STEP_W))
    sh = max(1, int(h * WINNER_STEP_H))
    max_blue, max_pink = 0.0, 0.0
    for y0 in range(0, max(1, h - th + 1), sh):
        for x0 in range(0, max(1, w - tw + 1), sw):
            tile = arr[y0 : y0 + th, x0 : x0 + tw]
            if tile.size == 0:
                continue
            b = color_frac(tile, WIN_BLUE_LO, WIN_BLUE_HI)
            p = color_frac(tile, WIN_PINK_LO, WIN_PINK_HI)
            max_blue = max(max_blue, b)
            max_pink = max(max_pink, p)
    return max_blue, max_pink


def _winner_scan_roi(arr: np.ndarray) -> tuple[float, float]:
    """Legacy: single ROI at top of screen."""
    region = crop(arr, WINNER_ROI)
    return (
        color_frac(region, WIN_BLUE_LO, WIN_BLUE_HI),
        color_frac(region, WIN_PINK_LO, WIN_PINK_HI),
    )


def _load_winner_refs():
    """Load winner ref images once (grayscale for template matching)."""
    global _winner_ref_blue_gray, _winner_ref_pink_gray
    if _winner_ref_blue_gray is None and WINNER_REF_BLUE.exists():
        img = cv2.imread(str(WINNER_REF_BLUE), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            _winner_ref_blue_gray = img
    if _winner_ref_pink_gray is None and WINNER_REF_PINK.exists():
        img = cv2.imread(str(WINNER_REF_PINK), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            _winner_ref_pink_gray = img


def _winner_template_scores(arr: np.ndarray) -> tuple[float, float]:
    """
    Match winner ref images (blue and pink) against the screen at multiple scales.
    Returns (blue_score, pink_score) in [0, 1]. Uses refs if both exist; else (0, 0).
    """
    _load_winner_refs()
    if _winner_ref_blue_gray is None or _winner_ref_pink_gray is None:
        return 0.0, 0.0
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape[:2]
    blue_best, pink_best = 0.0, 0.0
    for ref_gray, best in [( _winner_ref_blue_gray, "blue"), (_winner_ref_pink_gray, "pink")]:
        ref_h, ref_w = ref_gray.shape[:2]
        for scale in WINNER_REF_SCALES:
            tw = max(10, min(int(ref_w * scale), w - 5))
            th = max(10, min(int(ref_h * scale), h - 5))
            if tw > w or th > h:
                continue
            ref_scaled = cv2.resize(ref_gray, (tw, th))
            try:
                result = cv2.matchTemplate(gray, ref_scaled, cv2.TM_CCOEFF_NORMED)
                val = float(result.max())
            except cv2.error:
                val = 0.0
            if best == "blue":
                blue_best = max(blue_best, val)
            else:
                pink_best = max(pink_best, val)
    return blue_best, pink_best


def _winner_refs_available() -> bool:
    """True if both winner ref images exist and loaded."""
    _load_winner_refs()
    return _winner_ref_blue_gray is not None and _winner_ref_pink_gray is not None


def is_match_live(hwnd) -> bool:
    """True while inside a live match (elixir bar is visible)."""
    arr = grab_full(hwnd)
    return color_frac(crop(arr, ELIXIR_ROI), ELIXIR_LO, ELIXIR_HI) >= ELIXIR_THRESH


def is_end_screen(hwnd) -> bool:
    """
    True when the post-match "Winner!" screen is visible.
    Preferred: template match your winner_blue.png / winner_pink.png refs (reliable, no gold).
    Fallback: golden result-screen background (gold >= END_THRESH).
    """
    arr = grab_full(hwnd)
    if _winner_refs_available():
        blue_score, pink_score = _winner_template_scores(arr)
        if blue_score >= WINNER_REF_THRESH or pink_score >= WINNER_REF_THRESH:
            return True
        return False
    gold = color_frac(crop(arr, END_ROI), END_GOLD_LO, END_GOLD_HI)
    return gold >= END_THRESH


def parse_result(hwnd) -> dict:
    """
    Determine win/loss from the end screen.
    Preferred: template match winner_blue vs winner_pink refs (your screenshots).
    Fallback: colour scan (blue vs pink fraction).
    Returns { "won": bool }
    """
    arr = grab_full(hwnd)
    if _winner_refs_available():
        blue_score, pink_score = _winner_template_scores(arr)
        print(f"[Lifecycle] Winner (ref match) — blue={blue_score:.3f}  pink={pink_score:.3f}")
        if blue_score >= WINNER_REF_THRESH and blue_score > pink_score:
            print("[Lifecycle] -> VICTORY (blue Winner ref)")
            return {"won": True}
        if pink_score >= WINNER_REF_THRESH and pink_score > blue_score:
            print("[Lifecycle] -> DEFEAT (pink Winner ref)")
            return {"won": False}
        print("[Lifecycle] -> No clear ref match — defaulting to DEFEAT")
        return {"won": False}
    # Colour fallback
    if WINNER_FULL_SCREEN:
        blue_frac, pink_frac = _winner_scan_full_screen(arr)
    else:
        blue_frac, pink_frac = _winner_scan_roi(arr)
    print(f"[Lifecycle] Winner (colour fallback) — blue={blue_frac:.3f}  pink={pink_frac:.3f}")
    if blue_frac >= WIN_BLUE_THRESH and blue_frac > pink_frac:
        print("[Lifecycle] -> VICTORY (blue Winner)")
        return {"won": True}
    if pink_frac >= WIN_PINK_THRESH and pink_frac > blue_frac:
        print("[Lifecycle] -> DEFEAT (pink Winner)")
        return {"won": False}
    print("[Lifecycle] -> Could not read Winner — defaulting to DEFEAT")
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

def capture_refs(hwnd, include_winner: bool = False):
    """Save refs for OK, Battle, and optionally Winner (blue/pink) screens."""
    REF_DIR.mkdir(parents=True, exist_ok=True)
    for name, roi in [("ok_button", OK_ROI), ("battle_button", BATTLE_ROI)]:
        input(f"\n[Capture] Show '{name}' on screen then press ENTER...")
        arr    = grab_full(hwnd)
        region = crop(arr, roi)
        gray   = cv2.cvtColor(region, cv2.COLOR_RGB2GRAY)
        out    = REF_DIR / f"{name}.png"
        cv2.imwrite(str(out), gray)
        print(f"   Saved -> {out}")
    if include_winner:
        for name, path in [("blue Winner (YOU won)", WINNER_REF_BLUE), ("pink Winner (opponent won)", WINNER_REF_PINK)]:
            input(f"\n[Capture] Show '{name}' screen then press ENTER...")
            arr    = grab_full(hwnd)
            region = crop(arr, WINNER_CAPTURE_ROI)
            gray   = cv2.cvtColor(region, cv2.COLOR_RGB2GRAY)
            cv2.imwrite(str(path), gray)
            print(f"   Saved -> {path}")
    print("\n[Capture] Done. Winner refs (winner_blue.png / winner_pink.png) are used for end-screen and win/loss detection.\n")


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
        capture_refs(hwnd, include_winner="--winner" in sys.argv)
    elif "--test" in sys.argv:
        print("\n[Test] Current screen state:")
        print(f"  winner refs   : {_winner_refs_available()} (winner_blue.png, winner_pink.png)")
        if _winner_refs_available():
            arr = grab_full(hwnd)
            b, p = _winner_template_scores(arr)
            print(f"  winner scores : blue={b:.3f}  pink={p:.3f}  (threshold={WINNER_REF_THRESH})")
        print(f"  is_match_live : {is_match_live(hwnd)}")
        print(f"  is_end_screen : {is_end_screen(hwnd)}")
        if is_end_screen(hwnd):
            print(f"  parse_result  : {parse_result(hwnd)}")
    else:
        print("Usage:")
        print("  python match_lifecycle.py --test")
        print("  python match_lifecycle.py --capture           # OK + Battle refs")
        print("  python match_lifecycle.py --capture --winner  # + blue/pink Winner refs")