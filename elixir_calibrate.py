"""
elixir_calibrate.py
-------------------
Interactive calibration tool. Takes a screenshot of BlueStacks, then
lets you CLICK the top-left and bottom-right corners of the elixir bar.
Automatically calculates the correct ROI fractions and patches rl_agent.py.

Run:
    python elixir_calibrate.py
"""

import time
import tkinter as tk
from tkinter import messagebox
from PIL import Image, ImageTk, ImageDraw
import win32gui
import win32con
import numpy as np
from pathlib import Path

# ── Find BlueStacks & grab screenshot ────────────────────────────────────────

def find_bluestacks():
    found = []
    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            if "bluestacks" in win32gui.GetWindowText(hwnd).lower():
                found.append(hwnd)
    win32gui.EnumWindows(cb, None)
    if not found:
        raise RuntimeError("BlueStacks not found! Is it running?")
    return found[0]

def get_client_rect(hwnd):
    r   = win32gui.GetClientRect(hwnd)
    org = win32gui.ClientToScreen(hwnd, (0, 0))
    return org[0], org[1], org[0]+r[2], org[1]+r[3]

def grab_screenshot(hwnd):
    from PIL import ImageGrab
    left, top, right, bottom = get_client_rect(hwnd)
    img = ImageGrab.grab(bbox=(left, top, right, bottom))
    return img, (left, top, right, bottom)

# ── Patch rl_agent.py with new ROI ───────────────────────────────────────────

def patch_rl_agent(roi: tuple[float, float, float, float]):
    """Update ELIXIR_BAR_ROI in rl_agent.py with the calibrated values."""
    # Search common locations
    candidates = [
        Path("rl_agent.py"),
        Path("../rl_agent.py"),
        Path("clash_bot/../rl_agent.py"),
    ]
    # Also search recursively from cwd
    import os
    for root, dirs, files in os.walk(Path.cwd().parent):
        for f in files:
            if f == "rl_agent.py":
                candidates.append(Path(root) / f)
        break  # only one level up

    path = next((p for p in candidates if p.exists()), None)
    if not path:
        print(f"[WARN] Could not find rl_agent.py to patch automatically.")
        print(f"       Manually set ELIXIR_BAR_ROI = {roi}")
        return

    text = path.read_text()
    import re
    pattern = r"ELIXIR_BAR_ROI\s*=\s*\([^)]+\)"
    new_val  = f"ELIXIR_BAR_ROI  = ({roi[0]:.4f}, {roi[1]:.4f}, {roi[2]:.4f}, {roi[3]:.4f})   # calibrated via elixir_calibrate.py"
    updated  = re.sub(pattern, new_val, text)
    if updated == text:
        print("[WARN] Could not find ELIXIR_BAR_ROI in rl_agent.py — patch manually.")
        print(f"       New value: ELIXIR_BAR_ROI = {roi}")
    else:
        path.write_text(updated)
        print(f"[✓] rl_agent.py patched at {path}: ELIXIR_BAR_ROI = {roi}")

# ── Interactive click UI ──────────────────────────────────────────────────────

class CalibrationTool:
    def __init__(self, img: Image.Image, window_rect: tuple):
        self.img         = img
        self.window_rect = window_rect   # (left, top, right, bottom) in screen coords
        self.img_w       = img.width
        self.img_h       = img.height
        self.clicks      = []            # stores (x, y) in image coords
        self.rect_coords = None

        # Scale image to fit screen nicely (max 500px wide)
        self.scale = min(1.0, 500 / self.img_w)
        disp_w = int(self.img_w * self.scale)
        disp_h = int(self.img_h * self.scale)
        self.disp_img = img.resize((disp_w, disp_h), Image.LANCZOS)

        self.root = tk.Tk()
        self.root.title("Elixir Bar Calibration — Click top-left then bottom-right")
        self.root.resizable(False, False)

        # Instructions label
        self.label = tk.Label(
            self.root,
            text="STEP 1: Click the TOP-LEFT corner of the elixir bar",
            font=("Arial", 12, "bold"), fg="white", bg="#1a1a2e",
            pady=8
        )
        self.label.pack(fill=tk.X)

        # Canvas
        self.tk_img = ImageTk.PhotoImage(self.disp_img)
        self.canvas = tk.Canvas(self.root, width=disp_w, height=disp_h,
                                 cursor="crosshair", bg="black")
        self.canvas.pack()
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.tk_img)
        self.canvas.bind("<Button-1>", self.on_click)

        # Reset button
        btn_frame = tk.Frame(self.root, bg="#1a1a2e")
        btn_frame.pack(fill=tk.X)
        tk.Button(btn_frame, text="Reset", command=self.reset,
                  bg="#e74c3c", fg="white", font=("Arial", 10)).pack(side=tk.LEFT, padx=5, pady=5)
        tk.Button(btn_frame, text="Confirm & Save", command=self.confirm,
                  bg="#27ae60", fg="white", font=("Arial", 10, "bold")).pack(side=tk.RIGHT, padx=5, pady=5)

        self.root.configure(bg="#1a1a2e")
        self.root.mainloop()

    def on_click(self, event):
        if len(self.clicks) >= 2:
            return

        # Convert display coords → original image coords
        ix = int(event.x / self.scale)
        iy = int(event.y / self.scale)
        self.clicks.append((ix, iy))

        # Draw marker
        r = 5
        color = "#00ff88" if len(self.clicks) == 1 else "#ff4444"
        sx, sy = event.x, event.y
        self.canvas.create_oval(sx-r, sy-r, sx+r, sy+r, fill=color, outline="white", width=2)
        self.canvas.create_text(sx+10, sy, text=f"{'TL' if len(self.clicks)==1 else 'BR'}",
                                 fill=color, font=("Arial", 10, "bold"))

        if len(self.clicks) == 1:
            self.label.config(
                text="STEP 2: Click the BOTTOM-RIGHT corner of the elixir bar",
                fg="#00ff88"
            )
        elif len(self.clicks) == 2:
            self.label.config(text="✓ Both corners selected — click Confirm & Save", fg="#f39c12")
            # Draw rectangle
            x1 = int(self.clicks[0][0] * self.scale)
            y1 = int(self.clicks[0][1] * self.scale)
            x2 = int(self.clicks[1][0] * self.scale)
            y2 = int(self.clicks[1][1] * self.scale)
            self.rect_id = self.canvas.create_rectangle(
                x1, y1, x2, y2,
                outline="#f39c12", width=2, dash=(4,2)
            )

    def reset(self):
        self.clicks = []
        self.rect_coords = None
        # Redraw original image
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.tk_img)
        self.label.config(
            text="STEP 1: Click the TOP-LEFT corner of the elixir bar",
            fg="white"
        )

    def confirm(self):
        if len(self.clicks) < 2:
            messagebox.showwarning("Not ready", "Please click both corners first.")
            return

        x1_px, y1_px = self.clicks[0]
        x2_px, y2_px = self.clicks[1]

        # Ensure correct ordering (top-left / bottom-right)
        lx = min(x1_px, x2_px)
        rx = max(x1_px, x2_px)
        ty = min(y1_px, y2_px)
        by = max(y1_px, y2_px)

        # Convert to fractions of the original image size
        roi = (
            round(lx / self.img_w, 4),
            round(ty / self.img_h, 4),
            round(rx / self.img_w, 4),
            round(by / self.img_h, 4),
        )

        print(f"\n{'='*50}")
        print(f"  Calibrated ELIXIR_BAR_ROI = {roi}")
        print(f"  Pixel region: ({lx},{ty}) → ({rx},{by})")
        print(f"  Image size:   {self.img_w} × {self.img_h}")
        print(f"{'='*50}\n")

        # Save a preview image showing the selected ROI
        preview = self.img.copy()
        draw    = ImageDraw.Draw(preview)
        draw.rectangle([lx, ty, rx, by], outline=(255, 165, 0), width=3)
        out = Path("clash_bot/screenshots/elixir_roi_calibrated.png")
        out.parent.mkdir(parents=True, exist_ok=True)
        preview.save(out)
        print(f"[✓] Preview saved → {out}")

        # Patch rl_agent.py
        patch_rl_agent(roi)

        messagebox.showinfo(
            "Calibration saved!",
            f"ELIXIR_BAR_ROI = {roi}\n\nrl_agent.py has been updated.\n\nPreview saved to:\n{out}"
        )
        self.root.destroy()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Finding BlueStacks and taking screenshot...")
    print("Make sure a match is running so the elixir bar is visible.\n")
    time.sleep(1)

    hwnd = find_bluestacks()
    img, rect = grab_screenshot(hwnd)

    # Minimise BlueStacks so calibration window is visible
    # (don't close it, we still need the screenshot)

    print("Screenshot captured! Opening calibration window...\n")
    print("Instructions:")
    print("  1. Click the TOP-LEFT  corner of the pink elixir bar")
    print("  2. Click the BOTTOM-RIGHT corner of the pink elixir bar")
    print("  3. Click 'Confirm & Save'\n")

    CalibrationTool(img, rect)