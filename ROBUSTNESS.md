# Making the Clash Royale RL Agent More Robust

This doc covers: **what reference data to add**, **how to capture it**, **calibration**, and **architecture ideas** so detection stops guessing and the agent learns from real game state.

---

## 1. Reference Data You Should Add

### Card refs (stop hallucinating hands)

- **Where:** `clash_bot/card_refs/`
- **Files:** One image per deck card, named by internal key:
  - `pekka.png`, `battle_ram.png`, `bandit.png`, `royal_ghost.png`
  - `e_spirit.png`, `zap.png`, `arrows.png`, `wizard.png`
- **What to capture:** A **crop of the card as it appears in the in-game hand bar** (bottom of screen). Same resolution and aspect as your game.
- **How:**
  1. Start a match so the hand is visible.
  2. For each card, when it appears in a slot, take a screenshot and crop **only that slot** (same rectangle the code uses: see `CARD_SLOT_ROIS` in `rl_agent.py`). Save as the right filename.
  3. Or run with `--debug` to get screenshots with slot boxes; crop each slot when each card appears and save as `cardname.png`.
- **Tips:** Good lighting, no overlap with other UI. One ref per card is enough; you can add 2–3 per card (different games) and the code could take max match (not implemented yet).

### Winner refs (reliable end screen + win/loss)

- **Where:** `clash_bot/lifecycle_refs/`
- **Files:** `winner_blue.png`, `winner_pink.png` (or `.jpeg` if you use that)
- **What:** Crop of the **"Winner!" banner** when **you** win (blue) and when the **opponent** wins (pink).
- **How:** Finish one game as winner, screenshot, crop the banner area. Finish one as loser, screenshot, crop the banner. Save as above.

### Elixir number

- Elixir is read from the **number under the leftmost card** (OCR). If it’s wrong, adjust `ELIXIR_NUMBER_ROI` in `rl_agent.py` so the crop contains only that number, or run `python elixir_calibrate.py` and use the ROI it produces for the **bar** (the code also has an OCR path for the digit).

### Timer

- Timer is read from the **top-right** in "minute:seconds" form. If it’s wrong, adjust `TIMER_ROI` in `rl_agent.py` so the crop contains only the timer (no crowns or other text).

---

## 2. Calibration

- **Elixir bar ROI:** `python elixir_calibrate.py` — click the two corners of the elixir bar; it patches `ELIXIR_BAR_ROI` in `rl_agent.py`. Use this if the bar-fill fallback is used.
- **Elixir number / Timer:** No GUI yet. Edit in code:
  - `ELIXIR_NUMBER_ROI` = (x1, y1, x2, y2) as fraction of client area for the digit under the leftmost card.
  - `TIMER_ROI` = (x1, y1, x2, y2) for the top-right timer.
- **Card slots:** If cards are detected in the wrong order or not at all, adjust `CARD_SLOT_ROIS` and the matching `CARD_SLOTS` in `mumu_control.py` so they align with your layout.

---

## 3. Detection Tuning (in code)

- **Card confidence:** `HAND_DETECT_THRESH` in `rl_agent.py` (default 0.48). Only slots with template match ≥ this are reported. **Increase** (e.g. 0.55) if you get wrong cards; **decrease** (e.g. 0.42) if too many slots are left empty.
- **Ref size:** `HAND_REF_SIZE` (default 64). Larger = more detail but slower; keep ref images at least this size (they get resized to this).
- **Winner ref threshold:** `WINNER_REF_THRESH` in `match_lifecycle.py`. Raise if end screen is detected during gameplay; lower if real end screen is missed.

---

## 4. Architecture and Algorithm Improvements

### Detection (less guessing, better state)

- **Card classifier:** Replace template matching with a small CNN that takes the 4 slot crops and outputs 8-class (+ “unknown”) per slot. Train on a few hundred labeled screens (you label which card is in each slot). That removes hallucination at the cost of labeling and training.
- **Multi-frame agreement:** Only confirm a card in a slot if it matches the same card for 2–3 consecutive frames.
- **Timer in state:** Feed “seconds remaining” (and maybe “confidence” per slot) into the policy so the agent can use time and uncertainty explicitly.

### State representation

- **Current:** `[4, 84, 84]` = RGB screenshot + elixir channel.
- **Add:** Extra channels or a small vector: normalized timer, one-hot or embedding of detected hand, per-slot confidence. Concatenate to the conv output before the Q-head.

### Policy network

- **Deeper/wider DQN:** More conv layers or channels; add batch norm. Helps if the game state is visually complex.
- **Dueling DQN:** Split the last layer into value + advantage; often improves stability.
- **Recurrence:** If you pass a short history of states (or LSTM over state features), the agent can better handle partial observability and timing.

### Reward and training

- **Reward shaping:** You already have step rewards (wait penalty, play bonus). Optionally add small reward for “elixir efficiency” or for not overcommitting when timer is low.
- **Prioritized replay:** Sample transitions with higher TD error more often to learn from rare events (e.g. wins).
- **Double DQN:** You already use a target network; make sure the argmax action for the next state is from the policy net and the value from the target (you have this).

### Match end and lifecycle

- **End detection:** Prefer **winner refs** (blue/pink) over gold bar. Use **timer 0:00** as a strong signal; require either ref match or timer 0:00 before trusting “elixir bar gone”.
- **No guessing:** Card hand is only what passes the confidence threshold; if no refs, hand is empty and only WAIT is valid. No random cards.

---

## 5. Quick Checklist

- [ ] Add all 8 card refs to `clash_bot/card_refs/` (crops of in-game hand slots).
- [ ] Add `winner_blue.png` and `winner_pink.png` to `clash_bot/lifecycle_refs/`.
- [ ] Run elixir calibration if you rely on bar fill; else set `ELIXIR_NUMBER_ROI` so the digit is in frame.
- [ ] Set `TIMER_ROI` so the top-right timer is fully inside the crop.
- [ ] Tune `HAND_DETECT_THRESH` so you get 4 cards when refs are good and no wrong cards.
- [ ] Optionally add timer (and hand/confidence) to the state tensor and deepen the DQN once detection is stable.
