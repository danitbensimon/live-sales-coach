"""Always-on-top coaching overlay window using tkinter."""

import tkinter as tk
from tkinter import font as tkfont
import threading
import queue
from collections import deque

# Total tips kept in memory (older ones auto-evicted when full).
# Tips area is scrollable so all are reachable.
MAX_TIPS = 30

ICON_COLORS = {
    "ASK": "#4FC3F7",   # blue
    "WARN": "#FF8A65",  # orange-red
    "TIP": "#AED581",   # green
    "WIN": "#FFD54F",   # gold
}

DEFAULT_COLOR = "#B0BEC5"


class CoachOverlay:
    """Floating always-on-top window that displays coaching tips."""

    def __init__(self, tip_queue: queue.Queue, on_start=None, on_stop=None):
        self.tip_queue = tip_queue
        self.on_start = on_start
        self.on_stop = on_stop
        self.tips = deque(maxlen=MAX_TIPS)
        self.root = None
        self._running = False
        self._active = False  # session state — controls if audio is captured
        self.contact_email_var = None  # set in start()
        self._tip_wraplength = 480     # current wrap width for tip labels (responsive)
        self._transcript_wraplength = 540

    def _toggle(self, _event=None):
        if self._active:
            self._active = False
            self._update_button()
            if self.on_stop:
                self.on_stop()
        else:
            self._active = True
            self._update_button()
            if self.on_start:
                email = self.contact_email_var.get().strip() if self.contact_email_var else ""
                self.on_start(email)

    def _update_button(self):
        if not hasattr(self, "button"):
            return
        if self._active:
            self.button.config(text="■  Stop", bg="#C62828", activebackground="#8B0000")
        else:
            self.button.config(text="▶  Start", bg="#2E7D32", activebackground="#1B5E20")

    def start(self):
        """Start the overlay (blocks — run in main thread or dedicated thread)."""
        self._running = True
        self.root = tk.Tk()
        self.root.title("Sales Coach")
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#1E1E1E")
        self.root.attributes("-alpha", 0.94)

        # Position: bottom-right corner
        width, height = 560, 520
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = screen_w - width - 20
        y = screen_h - height - 80
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(320, 280)
        self.root.resizable(True, True)

        # ---- Header ----
        header = tk.Frame(self.root, bg="#2D2D2D", height=54)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        title_font = tkfont.Font(family="Helvetica", size=16, weight="bold")
        tk.Label(
            header, text="Sales Coach", fg="#E0E0E0", bg="#2D2D2D",
            font=title_font, padx=12
        ).pack(side=tk.LEFT)

        btn_font = tkfont.Font(family="Helvetica", size=13, weight="bold")
        self.button = tk.Label(
            header, text="▶  Start", fg="white", bg="#2E7D32",
            font=btn_font, padx=18, pady=8, cursor="hand2",
        )
        self.button.bind("<Button-1>", self._toggle)
        self.button.pack(side=tk.RIGHT, padx=(0, 10), pady=8)

        self.status_label = tk.Label(
            header, text="STOPPED", fg="#9E9E9E", bg="#2D2D2D",
            font=tkfont.Font(family="Helvetica", size=12, weight="bold"), padx=12
        )
        self.status_label.pack(side=tk.RIGHT)

        # ---- Contact row ----
        contact_row = tk.Frame(self.root, bg="#252525", height=44)
        contact_row.pack(fill=tk.X)
        contact_row.pack_propagate(False)
        tk.Label(
            contact_row, text="Contact:", fg="#90A4AE", bg="#252525",
            font=tkfont.Font(family="Helvetica", size=12), padx=12,
        ).pack(side=tk.LEFT)
        self.contact_email_var = tk.StringVar()
        entry = tk.Entry(
            contact_row, textvariable=self.contact_email_var,
            font=tkfont.Font(family="Helvetica", size=12),
            bg="#1A1A1A", fg="#ECEFF1", insertbackground="#ECEFF1",
            relief=tk.FLAT, highlightthickness=1, highlightbackground="#404040",
            highlightcolor="#69F0AE",
        )
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 12), pady=8)

        # ---- Scrollable tips area ----
        # Canvas + inner frame + vertical scrollbar. Mouse-wheel works on hover.
        tips_container = tk.Frame(self.root, bg="#1E1E1E")
        tips_container.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(
            tips_container, bg="#1E1E1E", highlightthickness=0, bd=0,
        )
        self.scrollbar = tk.Scrollbar(
            tips_container, orient=tk.VERTICAL, command=self.canvas.yview,
            bg="#1E1E1E", troughcolor="#2A2A2A", activebackground="#69F0AE",
            relief=tk.FLAT, borderwidth=0,
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.tips_frame = tk.Frame(self.canvas, bg="#1E1E1E")
        self._tips_window = self.canvas.create_window(
            (0, 0), window=self.tips_frame, anchor="nw",
        )

        # Keep the inner frame's width matching the canvas width (so wrap works)
        def _on_canvas_configure(event):
            self.canvas.itemconfig(self._tips_window, width=event.width)
            # Update tip wraplength = canvas width minus the ✓ button + padding
            new_wrap = max(140, event.width - 70)
            if new_wrap != self._tip_wraplength:
                self._tip_wraplength = new_wrap
                self._redraw_tips()

        self.canvas.bind("<Configure>", _on_canvas_configure)
        self.tips_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )

        # Mouse-wheel scrolling (macOS sends event.delta in small units)
        def _on_mousewheel(event):
            # Negate delta because tkinter convention vs. macOS scroll direction
            self.canvas.yview_scroll(int(-1 * (event.delta)), "units")

        def _bind_wheel(_event):
            self.canvas.bind_all("<MouseWheel>", _on_mousewheel)

        def _unbind_wheel(_event):
            self.canvas.unbind_all("<MouseWheel>")

        self.canvas.bind("<Enter>", _bind_wheel)
        self.canvas.bind("<Leave>", _unbind_wheel)

        # ---- Transcript snippet (bottom) ----
        self.transcript_var = tk.StringVar(value="Waiting for audio...")
        transcript_font = tkfont.Font(family="Helvetica", size=14)
        tf = tk.Frame(self.root, bg="#262626", height=90)
        tf.pack(fill=tk.X, side=tk.BOTTOM)
        tf.pack_propagate(False)
        self.transcript_label = tk.Label(
            tf, textvariable=self.transcript_var, fg="#ECEFF1", bg="#262626",
            font=transcript_font, anchor="w",
            wraplength=self._transcript_wraplength, justify="left",
            padx=10, pady=8,
        )
        self.transcript_label.pack(fill=tk.BOTH, expand=True)

        # Root <Configure> updates transcript wraplength so text doesn't get clipped
        def _on_root_configure(event):
            if event.widget is not self.root:
                return
            new_wrap = max(180, event.width - 30)
            if new_wrap != self._transcript_wraplength:
                self._transcript_wraplength = new_wrap
                self.transcript_label.config(wraplength=new_wrap)

        self.root.bind("<Configure>", _on_root_configure)

        self.root.protocol("WM_DELETE_WINDOW", self.stop)
        self._poll_queue()
        self.root.mainloop()

    def _poll_queue(self):
        if not self._running:
            return
        scroll_to_bottom_after_add = False
        try:
            while True:
                msg = self.tip_queue.get_nowait()
                if msg.get("type") == "tip":
                    self._add_tip(msg["text"])
                    scroll_to_bottom_after_add = True
                elif msg.get("type") == "transcript":
                    snippet = msg["text"]
                    if len(snippet) > 200:
                        snippet = "..." + snippet[-197:]
                    self.transcript_var.set(snippet)
                elif msg.get("type") == "status":
                    self.status_label.config(text=msg["text"])
        except queue.Empty:
            pass
        if scroll_to_bottom_after_add:
            # Let geometry settle, then scroll to newest tip
            self.canvas.after(50, lambda: self.canvas.yview_moveto(1.0))
        if self._running:
            self.root.after(80, self._poll_queue)

    def _add_tip(self, text: str):
        self.tips.append(text)
        self._redraw_tips()

    def _dismiss_tip(self, text_to_remove: str):
        try:
            self.tips.remove(text_to_remove)
        except ValueError:
            pass
        self._redraw_tips()

    def _redraw_tips(self):
        for widget in self.tips_frame.winfo_children():
            widget.destroy()

        tip_font = tkfont.Font(family="Helvetica", size=15, weight="bold")
        check_font = tkfont.Font(family="Helvetica", size=18, weight="bold")

        for tip_text in list(self.tips):
            color = DEFAULT_COLOR
            for prefix, c in ICON_COLORS.items():
                if tip_text.startswith(prefix + ":"):
                    color = c
                    break

            row = tk.Frame(self.tips_frame, bg="#2A2A2A", padx=4, pady=2)
            row.pack(fill=tk.X, pady=4, padx=6)

            check_btn = tk.Label(
                row, text="✓", fg="#9E9E9E", bg="#2A2A2A",
                font=check_font, padx=10, pady=6, cursor="hand2",
            )
            check_btn.bind("<Button-1>", lambda _e, t=tip_text: self._dismiss_tip(t))
            check_btn.bind("<Enter>", lambda _e, b=check_btn: b.config(fg="#69F0AE"))
            check_btn.bind("<Leave>", lambda _e, b=check_btn: b.config(fg="#9E9E9E"))
            check_btn.pack(side=tk.LEFT, anchor="n")

            tk.Label(
                row, text=tip_text, fg=color, bg="#2A2A2A",
                font=tip_font, anchor="w",
                wraplength=self._tip_wraplength, justify="left",
                padx=6, pady=6,
            ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Recompute scrollregion after redraw
        self.tips_frame.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def stop(self):
        self._running = False
        if self.root:
            self.root.destroy()


def run_overlay(tip_queue: queue.Queue):
    overlay = CoachOverlay(tip_queue)
    overlay.start()


if __name__ == "__main__":
    import time

    q = queue.Queue()
    t = threading.Thread(target=run_overlay, args=(q,), daemon=True)
    t.start()

    time.sleep(2)
    demo_tips = [
        "→ Discovery stage — anchor question: 'Walk me through the last campaign you ran.'",
        "⚠️ Vague — gives no anchor and gets rambling answers.",
        "→ Cut in: 'Actually, let me be specific — walk me through the last event you ran. What happened?'",
        "✅ Great — you anchored to a past event. Keep digging.",
        "💰 They named a deadline — use it: 'If we kick off Monday, we can have v1 live by your deadline.'",
    ]
    for tip in demo_tips:
        q.put({"type": "tip", "text": tip})
        time.sleep(1.5)
    q.put({"type": "transcript", "text": "...so we've been looking at ways to reduce our onboarding time from three weeks down to maybe one..."})
    time.sleep(60)
