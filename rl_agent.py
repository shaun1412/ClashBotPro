"""
rl_agent.py
-----------
Script 2: DQN-based RL agent for Clash Royale.
Uses mumu_control.py for emulator (MuMu Player) control and action execution.

Deck: PEKKA · Battle Ram · Bandit · Royal Ghost · E-Spirit · Zap · Arrows · Wizard

Optional folders (created only when needed):
    clash_bot/card_refs/     — card reference images for hand detection
    clash_bot/logs/          — CSV training logs (created when training)
    clash_bot/checkpoints/   — model checkpoints (created when saving)
    clash_bot/screenshots/   — debug screenshots (only with --debug)

Requirements:
    pip install torch torchvision opencv-python pywin32 pyautogui pillow numpy
    Optional (for accurate elixir from on-screen number): pip install pytesseract
    and install Tesseract: https://github.com/tesseract-ocr/tesseract

Run:
    python rl_agent.py
"""

import csv
import time
import random
import collections
import numpy as np
from datetime import datetime
from pathlib import Path

import cv2
import torch
import torch.nn as nn
import torch.optim as optim
from PIL import ImageGrab, Image

# ── local import ──────────────────────────────────────────────────────────────
from mumu_control import (
    find_mumu_window,
    focus_window,
    get_window_rect,
    screenshot_window,
    place_card,
    wait_action,
    ARENA_ZONES,
)
from match_lifecycle import (
    is_match_live,
    is_match_live_confirmed,
    is_end_screen,
    wait_for_match_start,
    handle_match_end,
)

# ══════════════════════════════════════════════════════════════════════════════
#  PATHS
# ══════════════════════════════════════════════════════════════════════════════

BASE_DIR        = Path("clash_bot")
CARD_REF_DIR    = BASE_DIR / "card_refs"
SCREENSHOT_DIR  = BASE_DIR / "screenshots"
CHECKPOINT_DIR  = BASE_DIR / "checkpoints"
LOG_DIR         = BASE_DIR / "logs"

# ══════════════════════════════════════════════════════════════════════════════
#  DECK DEFINITION
#  Each card: internal key → { display name, elixir cost, slot index (0-3) }
#  The agent always sees 4 cards in hand; slot index maps to card_slot 1-4
#  in mumu_control.place_card().
# ══════════════════════════════════════════════════════════════════════════════

DECK = {
    "pekka":        {"name": "P.E.K.K.A",     "elixir": 7},
    "battle_ram":   {"name": "Battle Ram",     "elixir": 4},
    "bandit":       {"name": "Bandit",         "elixir": 3},
    "royal_ghost":  {"name": "Royal Ghost",    "elixir": 3},
    "e_spirit":     {"name": "Electro Spirit", "elixir": 1},
    "zap":          {"name": "Zap",            "elixir": 2},
    "arrows":       {"name": "Arrows",         "elixir": 3},
    "wizard":       {"name": "Wizard",         "elixir": 5},
}

CARD_KEYS = list(DECK.keys())   # fixed ordering used throughout
N_CARDS   = len(CARD_KEYS)      # 8

# ══════════════════════════════════════════════════════════════════════════════
#  ACTION SPACE
#  action = card_index (0-7) × zone_index (0-8) + 1 WAIT action
#  Total: 8 cards × 9 zones + 1 = 73 actions
# ══════════════════════════════════════════════════════════════════════════════

ZONE_KEYS   = list(ARENA_ZONES.keys())   # 9 zones from mumu_control
N_ZONES     = len(ZONE_KEYS)             # 9
WAIT_ACTION = N_CARDS * N_ZONES          # index 72 → do nothing
N_ACTIONS   = N_CARDS * N_ZONES + 1     # 73

def decode_action(action_idx: int) -> tuple[str | None, str | None]:
    """
    Decode a flat action index into (card_key, zone_key).
    Returns (None, None) for the WAIT action.
    """
    if action_idx == WAIT_ACTION:
        return None, None
    card_idx = action_idx // N_ZONES
    zone_idx = action_idx  % N_ZONES
    return CARD_KEYS[card_idx], ZONE_KEYS[zone_idx]


def get_valid_actions(hand: list[str], elixir: float) -> list[int]:
    """
    Return all action indices valid given the current hand and elixir count.
    A card action is valid if:
      - that card is currently in hand, AND
      - the player has enough elixir to play it.
    WAIT is always valid.
    """
    valid = [WAIT_ACTION]
    for card_key in hand:
        card_idx = CARD_KEYS.index(card_key)
        cost     = DECK[card_key]["elixir"]
        if elixir >= cost:
            for zone_idx in range(N_ZONES):
                valid.append(card_idx * N_ZONES + zone_idx)
    return valid

# ══════════════════════════════════════════════════════════════════════════════
#  STATE PREPROCESSING
#  State tensor shape: [C=3+1, H=84, W=84]
#    channels 0-2 : battlefield screenshot (RGB, downsampled)
#    channel  3   : elixir heatmap (scalar broadcast to full image)
# ══════════════════════════════════════════════════════════════════════════════

IMG_H, IMG_W = 84, 84

def preprocess_screenshot(img: Image.Image) -> np.ndarray:
    """Resize and normalise a PIL screenshot → float32 numpy [3, 84, 84]."""
    img = img.resize((IMG_W, IMG_H), Image.BILINEAR)
    arr = np.array(img, dtype=np.float32) / 255.0   # H×W×3
    return arr.transpose(2, 0, 1)                    # 3×H×W


def build_state_tensor(img: Image.Image, elixir: float) -> torch.Tensor:
    """
    Combine visual observation with elixir into a [4, 84, 84] state tensor.
    The elixir channel is normalised to [0, 1] (max elixir = 10).
    """
    visual   = preprocess_screenshot(img)            # 3×84×84
    elixir_n = float(elixir) / 10.0
    elixir_ch = np.full((1, IMG_H, IMG_W), elixir_n, dtype=np.float32)  # 1×84×84
    state = np.concatenate([visual, elixir_ch], axis=0)                  # 4×84×84
    return torch.tensor(state, dtype=torch.float32)

# ══════════════════════════════════════════════════════════════════════════════
#  ELIXIR DETECTION  (OCR-free, template-match approach)
#  Reads the elixir bar pixel width to estimate current elixir (0-10).
#  You must calibrate ELIXIR_BAR_ROI to your window layout.
# ══════════════════════════════════════════════════════════════════════════════

# Relative bounding box of the elixir bar in the client area: (x1, y1, x2, y2)
# Elixir: the number is shown as digits under the leftmost card at the very bottom.
# ROI (x1, y1, x2, y2) as fraction of client area — adjust if your layout differs.
ELIXIR_NUMBER_ROI = (0.18, 0.942, 0.34, 0.995)
# Legacy: bar-fill fallback if OCR not available (same as before)
ELIXIR_BAR_ROI   = (0.5498, 0.9625, 0.9788, 0.9883)
ELIXIR_COLOR_LO  = np.array([200,  50, 150], dtype=np.uint8)
ELIXIR_COLOR_HI  = np.array([255, 160, 255], dtype=np.uint8)


def _elixir_from_ocr(crop_bgr: np.ndarray) -> float | None:
    """Read elixir (0–10) from a small image of the number. Returns None if OCR fails or unavailable."""
    try:
        import pytesseract
    except ImportError:
        return None
    try:
        gray = cv2.cvtColor(crop_bgr, cv2.COLOR_RGB2GRAY) if crop_bgr.ndim == 3 else np.asarray(crop_bgr)
        if gray.dtype != np.uint8:
            gray = (gray * 255).astype(np.uint8) if gray.max() <= 1.0 else gray.astype(np.uint8)
        # Improve contrast for digits
        gray = cv2.resize(gray, (gray.shape[1] * 2, gray.shape[0] * 2), interpolation=cv2.INTER_CUBIC)
        _, gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        text = pytesseract.image_to_string(
            gray, config="-c tessedit_char_whitelist=0123456789 --psm 7"
        ).strip()
        digits = "".join(c for c in text if c.isdigit())
        if not digits:
            return None
        val = int(digits) if len(digits) <= 2 else int(digits[:2])
        return float(min(max(val, 0), 10))
    except Exception:
        return None


def estimate_elixir(hwnd) -> float:
    """
    Read elixir (0.0–10.0) from the number under the leftmost card (OCR).
    Falls back to bar-fill estimate if OCR unavailable; then to 5.0 on failure.
    """
    try:
        left, top, right, bottom = get_window_rect(hwnd)
        w, h = right - left, bottom - top
        x1 = left + int(ELIXIR_NUMBER_ROI[0] * w)
        y1 = top  + int(ELIXIR_NUMBER_ROI[1] * h)
        x2 = left + int(ELIXIR_NUMBER_ROI[2] * w)
        y2 = top  + int(ELIXIR_NUMBER_ROI[3] * h)
        img = ImageGrab.grab(bbox=(x1, y1, x2, y2))
        arr = np.array(img)
        # Try OCR first (number at bottom under leftmost card)
        val = _elixir_from_ocr(arr)
        if val is not None:
            return round(val, 1)
        # Fallback: bar fill
        x1b = left + int(ELIXIR_BAR_ROI[0] * w)
        y1b = top  + int(ELIXIR_BAR_ROI[1] * h)
        x2b = left + int(ELIXIR_BAR_ROI[2] * w)
        y2b = top  + int(ELIXIR_BAR_ROI[3] * h)
        img_b = ImageGrab.grab(bbox=(x1b, y1b, x2b, y2b))
        arr_b = np.array(img_b)
        mask = np.all((arr_b >= ELIXIR_COLOR_LO) & (arr_b <= ELIXIR_COLOR_HI), axis=2)
        frac = mask.sum() / max(mask.size, 1)
        return round(min(max(frac * 10.0, 0.0), 10.0), 1)
    except Exception:
        return 5.0


# ══════════════════════════════════════════════════════════════════════════════
#  MATCH TIMER (top-right, format "minute:seconds")
# ══════════════════════════════════════════════════════════════════════════════

# ROI for the timer in the top-right: (x1, y1, x2, y2) as fraction of client area
TIMER_ROI = (0.72, 0.02, 0.98, 0.09)


def _parse_timer_ocr(crop_rgb: np.ndarray) -> tuple[int, int] | None:
    """Parse "minute:seconds" from a small crop. Returns (minutes, seconds) or None."""
    try:
        import pytesseract
    except ImportError:
        return None
    try:
        gray = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2GRAY) if crop_rgb.ndim == 3 else np.asarray(crop_rgb)
        if gray.dtype != np.uint8:
            gray = (gray * 255).astype(np.uint8) if gray.max() <= 1.0 else gray.astype(np.uint8)
        gray = cv2.resize(gray, (gray.shape[1] * 2, gray.shape[0] * 2), interpolation=cv2.INTER_CUBIC)
        _, gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        text = pytesseract.image_to_string(
            gray, config="-c tessedit_char_whitelist=0123456789: --psm 7"
        ).strip()
        # Expect "M:SS" or "MM:SS"
        parts = text.replace(" ", "").split(":")
        if len(parts) != 2:
            return None
        m_str = "".join(c for c in parts[0] if c.isdigit())
        s_str = "".join(c for c in parts[1] if c.isdigit())
        if not m_str or not s_str:
            return None
        minutes = int(m_str) if m_str else 0
        seconds = int(s_str) if s_str else 0
        if seconds > 59:
            seconds = seconds % 100  # e.g. 60 -> 0
        return (minutes, seconds)
    except Exception:
        return None


def get_match_timer(hwnd) -> tuple[int, int] | None:
    """
    Read the match timer from the top-right (format "minute:seconds").
    Returns (minutes, seconds) remaining, or None if unreadable.
    """
    try:
        left, top, right, bottom = get_window_rect(hwnd)
        w, h = right - left, bottom - top
        x1 = left + int(TIMER_ROI[0] * w)
        y1 = top  + int(TIMER_ROI[1] * h)
        x2 = left + int(TIMER_ROI[2] * w)
        y2 = top  + int(TIMER_ROI[3] * h)
        img = ImageGrab.grab(bbox=(x1, y1, x2, y2))
        arr = np.array(img)
        return _parse_timer_ocr(arr)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  HAND DETECTION (template matching against card_refs/)
#  Returns the 4 cards currently visible in the card bar.
# ══════════════════════════════════════════════════════════════════════════════

# Relative positions of the 4 card slots in the card bar (centre of each slot)
CARD_SLOT_ROIS = {
    1: (0.220, 0.870, 0.310, 0.940),
    2: (0.355, 0.870, 0.445, 0.940),
    3: (0.490, 0.870, 0.580, 0.940),
    4: (0.625, 0.870, 0.715, 0.940),
}

_ref_cache: dict[str, np.ndarray] = {}
_ref_load_attempted: bool = False  # so we only print missing warnings once

def _load_refs() -> dict[str, np.ndarray]:
    """Load and cache card reference images from card_refs/ (grayscale, 32×32).
    Accepts .jpg, .jpeg, or .png — whichever exists first. Missing refs warned once."""
    global _ref_cache, _ref_load_attempted
    if _ref_load_attempted:
        return _ref_cache
    _ref_load_attempted = True
    missing = []
    for key in CARD_KEYS:
        found = None
        for ext in (".jpg", ".jpeg", ".png"):
            candidate = CARD_REF_DIR / f"{key}{ext}"
            if candidate.exists():
                found = candidate
                break
        if found:
            img = cv2.imread(str(found), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                _ref_cache[key] = cv2.resize(img, (32, 32))
                print(f"[Refs] Loaded: {found.name}")
            else:
                missing.append(key)
        else:
            missing.append(key)
    if missing:
        print(f"[Refs] Missing card refs for: {missing}")
        print(f"       Add images to {CARD_REF_DIR.absolute()} as e.g. pekka.png, zap.jpg")
        print(f"       Hand detection will use random cards until refs are added.\n")
    return _ref_cache


def detect_hand(hwnd) -> list[str]:
    """
    Identify which cards are in each of the 4 hand slots via template matching.
    Returns a list of up to 4 card keys.
    If card_refs are missing, returns a random 4-card hand (for early testing).
    """
    refs = _load_refs()
    if not refs:
        # No reference images yet — return a random subset for smoke testing
        return random.sample(CARD_KEYS, 4)

    left, top, right, bottom = get_window_rect(hwnd)
    w = right - left
    h = bottom - top

    hand = []
    for slot_idx, roi in CARD_SLOT_ROIS.items():
        x1 = left + int(roi[0] * w)
        y1 = top  + int(roi[1] * h)
        x2 = left + int(roi[2] * w)
        y2 = top  + int(roi[3] * h)
        slot_img = np.array(ImageGrab.grab(bbox=(x1, y1, x2, y2)))
        slot_gray = cv2.cvtColor(slot_img, cv2.COLOR_RGB2GRAY)
        slot_gray = cv2.resize(slot_gray, (32, 32))

        best_key, best_score = None, -1.0
        for key, ref in refs.items():
            res   = cv2.matchTemplate(slot_gray, ref, cv2.TM_CCOEFF_NORMED)
            score = float(res.max())
            if score > best_score:
                best_score, best_key = score, key

        if best_key:
            hand.append(best_key)

    return hand


# ══════════════════════════════════════════════════════════════════════════════
#  REWARD FUNCTION
#  Called at the end of each match; in-match shaping uses elixir efficiency.
# ══════════════════════════════════════════════════════════════════════════════

def compute_match_reward(won: bool) -> float:
    """
    Sparse terminal reward for a completed match.
      +3.0  win
      -1.0  loss
    """
    return 3.0 if won else -1.0


def compute_step_reward(action_was_wait: bool, elixir_before: float,
                        card_played: str | None) -> float:
    """
    Dense per-step shaping reward to reduce sparsity.
      -0.01  for every WAIT when a playable card exists  (punish passivity)
      +0.05  for playing a card (any action is better than nothing)
      +0.10  bonus for playing a high-value card when elixir allows
    """
    if action_was_wait:
        return -0.01
    if card_played is None:
        return 0.0
    bonus = 0.05
    if DECK[card_played]["elixir"] >= 4 and elixir_before >= DECK[card_played]["elixir"]:
        bonus += 0.10   # reward committing high-elixir cards when affordable
    return bonus


def _print_training_summary(results: list[dict]) -> None:
    """Print learning summary and what to look for. Called after training completes."""
    if not results:
        return
    n = len(results)
    wins = sum(1 for r in results if r.get("won"))
    win_rate = 100.0 * wins / n
    last = min(20, n)
    recent_wins = sum(1 for r in results[-last:] if r.get("won"))
    recent_win_rate = 100.0 * recent_wins / last if last else 0
    losses = [r.get("avg_loss", 0) for r in results if r.get("avg_loss") is not None]
    avg_loss = float(np.mean(losses)) if losses else 0.0
    recent_losses = [r.get("avg_loss", 0) for r in results[-last:] if r.get("avg_loss") is not None]
    recent_avg_loss = float(np.mean(recent_losses)) if recent_losses else 0.0

    print("\n" + "=" * 50)
    print("  LEARNING SUMMARY")
    print("=" * 50)
    print(f"  Matches:     {n}")
    print(f"  Win rate:    {win_rate:.1f}%  (recent {last}: {recent_win_rate:.1f}%)")
    print(f"  Avg loss:    {avg_loss:.5f}  (recent {last}: {recent_avg_loss:.5f})")
    print("=" * 50)
    print("  What to look for:")
    print("  - Loss:  Should trend down over time (recent < early).")
    print("  - Wins:  Win rate may rise as policy improves (recent > early).")
    print("  - Logs:  CSV in clash_bot/logs/ — plot with: python rl_agent.py --plot")
    print("=" * 50 + "\n")


# ══════════════════════════════════════════════════════════════════════════════
#  REPLAY BUFFER
# ══════════════════════════════════════════════════════════════════════════════

Transition = collections.namedtuple(
    "Transition", ["state", "action", "reward", "next_state", "done"]
)

class ReplayBuffer:
    def __init__(self, capacity: int = 10_000):
        self.buffer = collections.deque(maxlen=capacity)

    def push(self, *args):
        self.buffer.append(Transition(*args))

    def sample(self, batch_size: int) -> list[Transition]:
        return random.sample(self.buffer, batch_size)

    def __len__(self):
        return len(self.buffer)


# ══════════════════════════════════════════════════════════════════════════════
#  DQN MODEL
#  Input : [4, 84, 84] (3 RGB + 1 elixir channel)
#  Output: Q-values for each of the 73 actions
# ══════════════════════════════════════════════════════════════════════════════

class DQN(nn.Module):
    def __init__(self, n_actions: int = N_ACTIONS):
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(4,  32, kernel_size=8, stride=4),  # → 32×20×20
            nn.ReLU(),
            nn.Conv2d(32, 64, kernel_size=4, stride=2),  # → 64×9×9
            nn.ReLU(),
            nn.Conv2d(64, 64, kernel_size=3, stride=1),  # → 64×7×7
            nn.ReLU(),
        )
        self.fc = nn.Sequential(
            nn.Flatten(),
            nn.Linear(64 * 7 * 7, 512),
            nn.ReLU(),
            nn.Linear(512, n_actions),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.fc(self.conv(x))


# ══════════════════════════════════════════════════════════════════════════════
#  AGENT
# ══════════════════════════════════════════════════════════════════════════════

class ClashRoyaleAgent:
    # ── Hyperparameters ──────────────────────────────────────────────────────
    GAMMA          = 0.99     # discount factor
    LR             = 1e-4     # learning rate
    BATCH_SIZE     = 32
    BUFFER_SIZE    = 10_000
    EPS_START      = 1.00     # ε-greedy exploration start
    EPS_END        = 0.10
    EPS_DECAY      = 0.995    # multiply ε by this after each match
    TARGET_UPDATE  = 10       # sync target net every N matches
    MIN_BUFFER     = 500      # don't train until buffer has this many samples
    STEP_SLEEP     = 0.5      # seconds between decision steps (don't spam clicks)

    def __init__(self, device: str = "cpu"):
        self.device  = torch.device(device)
        self.policy_net = DQN().to(self.device)
        self.target_net = DQN().to(self.device)
        self.target_net.load_state_dict(self.policy_net.state_dict())
        self.target_net.eval()

        self.optimizer = optim.Adam(self.policy_net.parameters(), lr=self.LR)
        self.buffer    = ReplayBuffer(self.BUFFER_SIZE)
        self.epsilon   = self.EPS_START
        self.match_num = 0

        # CSV log (create dir only when first needed)
        self.log_path = LOG_DIR / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "w", newline="") as f:
            csv.writer(f).writerow(
                ["match", "epsilon", "total_reward", "avg_loss", "won"]
            )

    # ── Action selection ─────────────────────────────────────────────────────

    def select_action(self, state: torch.Tensor,
                      hand: list[str], elixir: float) -> int:
        """ε-greedy action selection restricted to valid actions only."""
        valid = get_valid_actions(hand, elixir)

        if random.random() < self.epsilon:
            return random.choice(valid)                   # explore

        with torch.no_grad():
            q_vals = self.policy_net(state.unsqueeze(0).to(self.device))[0]
        # Mask invalid actions with -inf before argmax
        mask = torch.full((N_ACTIONS,), float("-inf"))
        mask[valid] = q_vals[valid]
        return int(mask.argmax().item())                  # exploit

    # ── Training step ────────────────────────────────────────────────────────

    def train_step(self) -> float | None:
        if len(self.buffer) < self.MIN_BUFFER:
            return None

        batch      = self.buffer.sample(self.BATCH_SIZE)
        states     = torch.stack([t.state      for t in batch]).to(self.device)
        actions    = torch.tensor([t.action    for t in batch], dtype=torch.long).to(self.device)
        rewards    = torch.tensor([t.reward    for t in batch], dtype=torch.float32).to(self.device)
        next_states= torch.stack([t.next_state for t in batch]).to(self.device)
        dones      = torch.tensor([t.done      for t in batch], dtype=torch.float32).to(self.device)

        # Current Q
        q_current = self.policy_net(states).gather(1, actions.unsqueeze(1)).squeeze(1)

        # Target Q (Double DQN style)
        with torch.no_grad():
            next_actions = self.policy_net(next_states).argmax(1)
            q_next       = self.target_net(next_states).gather(1, next_actions.unsqueeze(1)).squeeze(1)
            q_target     = rewards + self.GAMMA * q_next * (1 - dones)

        loss = nn.functional.smooth_l1_loss(q_current, q_target)
        self.optimizer.zero_grad()
        loss.backward()
        nn.utils.clip_grad_norm_(self.policy_net.parameters(), 10.0)
        self.optimizer.step()
        return loss.item()

    # ── Execute action in emulator ───────────────────────────────────────────

    def execute_action(self, hwnd, action_idx: int, hand: list[str]):
        """Translate action index → mumu_control calls."""
        card_key, zone_key = decode_action(action_idx)

        if card_key is None:
            wait_action(hwnd)
            return

        # Map card_key to its slot in the current hand (1-indexed)
        if card_key not in hand:
            wait_action(hwnd)
            return

        slot = hand.index(card_key) + 1   # 1-4
        place_card(hwnd, card_slot=slot, arena_zone=zone_key)

    # ── Match loop ───────────────────────────────────────────────────────────

    def run_match(self, hwnd) -> dict:
        """
        Run one full match, collecting transitions, executing actions,
        and returning match summary stats.

        The loop runs until you manually signal match end (see TODOs below).
        """
        print(f"\n{'='*60}")
        print(f"  MATCH {self.match_num + 1}   ε={self.epsilon:.3f}")
        print(f"{'='*60}\n")

        total_reward = 0.0
        step         = 0
        match_losses = []
        state        = None

        # ── TODO: add your match-start detection here ──────────────────────
        # e.g. wait for the "Battle" button on screen before entering the loop
        # ───────────────────────────────────────────────────────────────────

        while True:
            # 1. Observe
            img    = screenshot_window(hwnd)
            elixir = estimate_elixir(hwnd)
            hand   = detect_hand(hwnd)
            timer  = get_match_timer(hwnd)  # (minutes, seconds) top-right, or None

            timer_str = f"{timer[0]}:{timer[1]:02d}" if timer else "?"
            print(f"[Step {step:03d}]  elixir={elixir:.1f}  time={timer_str}  hand={[DECK[k]['name'] for k in hand]}")

            next_state = build_state_tensor(img, elixir)

            # 2. Store previous transition
            if state is not None:
                step_r = compute_step_reward(
                    action_was_wait=(last_action == WAIT_ACTION),
                    elixir_before=last_elixir,
                    card_played=last_card,
                )
                self.buffer.push(state, last_action, step_r, next_state, False)
                total_reward += step_r

                # Train
                loss = self.train_step()
                if loss is not None:
                    match_losses.append(loss)

            # 3. Select & execute action
            action     = self.select_action(next_state, hand, elixir)
            card_key, zone_key = decode_action(action)
            self.execute_action(hwnd, action, hand)

            last_action = action
            last_elixir = elixir
            last_card   = card_key
            state        = next_state
            step        += 1

            time.sleep(self.STEP_SLEEP)

            # ── Match end detection (only after minimum steps to avoid false positives) ──
            MIN_STEPS_BEFORE_END = 25
            if step >= 600:
                print("[Match] Step cap reached — forcing end.")
                break
            if step < MIN_STEPS_BEFORE_END:
                continue
            if timer is not None and timer[0] == 0 and timer[1] == 0:
                print("[Match] Timer 0:00 — match time expired.")
                break
            if is_end_screen(hwnd):
                print("[Match] End screen detected mid-loop — breaking.")
                break
            # Only end on "elixir gone" after several consecutive misses (avoids one bad frame)
            if not is_match_live(hwnd) and not is_match_live_confirmed(hwnd):
                print("[Match] Elixir bar gone (confirmed) — match ended.")
                break

        # ── Terminal reward (real detection via match_lifecycle) ───────────
        result     = handle_match_end(hwnd)   # clicks OK + Battle, waits for next match
        won        = result["won"]

        terminal_r = compute_match_reward(won)
        print(f"[Match] Terminal reward: {terminal_r:.2f}  ({'VICTORY' if won else 'DEFEAT'})")
        if state is not None:
            dummy_next = torch.zeros_like(state)
            self.buffer.push(state, last_action, terminal_r, dummy_next, True)
        total_reward += terminal_r

        # ── Post-match updates ─────────────────────────────────────────────
        self.match_num += 1
        self.epsilon    = max(self.EPS_END, self.epsilon * self.EPS_DECAY)

        if self.match_num % self.TARGET_UPDATE == 0:
            self.target_net.load_state_dict(self.policy_net.state_dict())
            print(f"[Agent] Target network synced at match {self.match_num}")

        avg_loss = float(np.mean(match_losses)) if match_losses else 0.0

        # Log
        with open(self.log_path, "a", newline="") as f:
            csv.writer(f).writerow(
                [self.match_num, f"{self.epsilon:.4f}",
                 f"{total_reward:.3f}", f"{avg_loss:.5f}", int(won)]
            )

        # Checkpoint every 10 matches
        if self.match_num % 10 == 0:
            CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
            ckpt = CHECKPOINT_DIR / f"dqn_match_{self.match_num}.pt"
            torch.save(self.policy_net.state_dict(), ckpt)
            print(f"[Agent] Checkpoint saved → {ckpt}")

        summary = {
            "match":        self.match_num,
            "steps":        step,
            "total_reward": total_reward,
            "avg_loss":     avg_loss,
            "won":          won,
            "epsilon":      self.epsilon,
        }
        print(f"\n[Summary] {summary}\n")
        return summary

    # ── Training loop ────────────────────────────────────────────────────────

    def train(self, n_matches: int = 200, wait_for_first_match: bool = True):
        """
        Full training loop:
          1. You start the first game (click Battle in Clash Royale).
          2. This script waits for the match to start (elixir bar visible).
          3. Agent plays the match, gets rewards, learns.
          4. On end screen: detect win/loss (blue vs pink 'Winner!'), click OK, click Battle.
          5. Waits for the next match to start, then repeats from step 3.
        Repeats until n_matches is reached.
        """
        hwnd = find_mumu_window()
        focus_window(hwnd)

        print("\n╔══════════════════════════════════════════╗")
        print("║   Clash Bot Pro — DQN Training Started  ║")
        print(f"║   Deck: {', '.join(CARD_KEYS[:4])}...")
        print(f"║   Actions: {N_ACTIONS}  |  Matches: {n_matches}")
        print("╚══════════════════════════════════════════╝\n")
        if wait_for_first_match:
            print("⚠  Click Battle in Clash Royale now. Waiting for first match to start...\n")
            if not wait_for_match_start(hwnd, timeout=90.0):
                print("[Train] First match did not start in time. Exiting.")
                return []
            print("[Train] First match started. Beginning training loop.\n")
        else:
            print("⚠  Make sure a match is already in progress!\n")
            time.sleep(5)

        results = []
        for i in range(n_matches):
            summary = self.run_match(hwnd)
            results.append(summary)
            # handle_match_end() already: OK → Battle → wait_for_match_start for next match

        print("\n✓ Training complete.")
        _print_training_summary(results)
        return results


def debug_calibrate():
    """
    Quick calibration check — run this instead of train() to verify:
      1. Elixir is being read correctly
      2. Card slots are being detected correctly
    Saves annotated screenshots to clash_bot/screenshots/ so you can inspect them.
    """
    import cv2 as _cv2
    hwnd = find_mumu_window()
    focus_window(hwnd)
    print("\n=== CALIBRATION DEBUG ===")
    print("Reading elixir + hand for 5 steps. Check the saved screenshots.\n")

    for i in range(5):
        img    = screenshot_window(hwnd)
        elixir = estimate_elixir(hwnd)
        hand   = detect_hand(hwnd)

        print(f"Step {i+1}: elixir={elixir}  hand={[DECK[k]['name'] for k in hand]}")

        # Save annotated screenshot
        arr = np.array(img)
        bgr = _cv2.cvtColor(arr, _cv2.COLOR_RGB2BGR)

        # Draw card slot boxes
        left, top, right, bottom = get_window_rect(hwnd)
        w = right - left
        h = bottom - top
        for slot, roi in CARD_SLOT_ROIS.items():
            x1 = int(roi[0] * w); y1 = int(roi[1] * h)
            x2 = int(roi[2] * w); y2 = int(roi[3] * h)
            _cv2.rectangle(bgr, (x1, y1), (x2, y2), (0, 255, 0), 2)
            _cv2.putText(bgr, str(slot), (x1+2, y1+15),
                         _cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,0), 1)

        # Draw elixir bar box
        ex1 = int(ELIXIR_BAR_ROI[0] * w); ey1 = int(ELIXIR_BAR_ROI[1] * h)
        ex2 = int(ELIXIR_BAR_ROI[2] * w); ey2 = int(ELIXIR_BAR_ROI[3] * h)
        _cv2.rectangle(bgr, (ex1, ey1), (ex2, ey2), (0, 0, 255), 2)
        _cv2.putText(bgr, f"elixir={elixir}", (ex1, ey1-5),
                     _cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,0,255), 1)

        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = str(SCREENSHOT_DIR / f"debug_step_{i+1}.png")
        _cv2.imwrite(out_path, bgr)
        print(f"  → Saved: {out_path}")
        time.sleep(1)

    print("\n=== Check clash_bot/screenshots/ to verify the green boxes")
    print("    land on the card slots and the red box covers the elixir bar ===\n")




def plot_training_log(log_path: Path | None = None) -> None:
    """
    Plot training CSV: match vs total_reward, avg_loss, and rolling win rate.
    If log_path is None, uses the most recent run_*.csv in clash_bot/logs/.
    """
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("Install matplotlib to plot: pip install matplotlib")
        return
    if log_path is None:
        if not LOG_DIR.exists():
            print(f"No log dir: {LOG_DIR}. Run training first.")
            return
        csvs = list(LOG_DIR.glob("run_*.csv"))
        if not csvs:
            print(f"No run_*.csv in {LOG_DIR}. Run training first.")
            return
        log_path = max(csvs, key=lambda p: p.stat().st_mtime)
    import csv as csv_module
    rows = []
    with open(log_path, newline="") as f:
        for row in csv_module.reader(f):
            rows.append(row)
    if len(rows) < 2:
        print("Log has no data rows.")
        return
    header, data = rows[0], rows[1:]
    try:
        match_idx = header.index("match")
        reward_idx = header.index("total_reward")
        loss_idx = header.index("avg_loss")
        won_idx = header.index("won")
    except ValueError:
        print("Log missing expected columns.")
        return
    matches = [int(r[match_idx]) for r in data]
    rewards = [float(r[reward_idx]) for r in data]
    losses = [float(r[loss_idx]) for r in data]
    wins = [int(r[won_idx]) for r in data]
    window = min(10, len(matches))
    rolling_wr = [100.0 * sum(wins[max(0, i - window + 1) : i + 1]) / min(i + 1, window) for i in range(len(wins))]

    fig, axes = plt.subplots(3, 1, figsize=(8, 7), sharex=True)
    axes[0].plot(matches, rewards, "b.", markersize=4, alpha=0.7)
    axes[0].set_ylabel("Total reward")
    axes[0].set_title("Training progress")
    axes[0].grid(True, alpha=0.3)
    axes[1].plot(matches, losses, "r.", markersize=4, alpha=0.7)
    axes[1].set_ylabel("Avg loss")
    axes[1].grid(True, alpha=0.3)
    axes[2].plot(matches, rolling_wr, "g-", linewidth=1.5)
    axes[2].set_ylabel("Rolling win %")
    axes[2].set_xlabel("Match")
    axes[2].set_ylim(0, 100)
    axes[2].grid(True, alpha=0.3)
    plt.tight_layout()
    out = log_path.with_suffix(".png")
    plt.savefig(out, dpi=120)
    print(f"Saved plot: {out}")
    plt.show()


if __name__ == "__main__":
    import sys
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[Device] Using: {device}")

    if "--debug" in sys.argv:
        debug_calibrate()
    elif "--plot" in sys.argv:
        plot_training_log()
    else:
        agent = ClashRoyaleAgent(device=device)
        agent.train(n_matches=200)