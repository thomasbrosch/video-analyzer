#!/usr/bin/env python3
"""
Modern desktop app for Gemini-powered video analysis (customtkinter, dark theme).

Launch with:  video-analyzer-gui   (or: python -m video_analyzer.gui)

Pick a video file or a folder, choose options, click Analyze, read the report.
Uses the GEMINI_API_KEY environment variable, or a key saved in the app.
"""

import os
import math
import queue
import logging
import colorsys
import threading
from pathlib import Path

import tkinter as tk
import customtkinter as ctk
from tkinter import filedialog, messagebox

# Drag-and-drop support (optional — the app still works without it)
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except Exception:
    HAS_DND = False

# Only lightweight constants at import time — the heavy Gemini SDK (imported by
# .analyzer) is loaded lazily when the user clicks Analyze, so the window opens
# instantly.
from .constants import MODELS, DEFAULT_MODEL, VIDEO_EXTENSIONS
from .prompts import LENSES, COMPILE

# Segmented model control labels -> internal model key
MODEL_SEG = {
    "Flash · fast": "flash",
    "Pro · deep": "pro",
}

# Output choices -> format key (or None for don't-save)
SAVE_OPTIONS = {
    "Don't save (screen only)": None,
    "Markdown (.md)": "md",
    "Word document (.docx)": "docx",
    "PDF (.pdf)": "pdf",
}

# Palette (dark)
COL_BG = "#0f1115"
COL_CARD = "#181b22"
COL_MUTED = "#8b93a7"
COL_OK = "#3fb950"
COL_ERR = "#f85149"
COL_TEXT = "#e6e9ef"
COL_DROP = "#11141a"
COL_DROP_HOVER = "#16233a"
COL_ACCENT = "#3b8ed0"


def _persist_key(key: str) -> None:
    """Save the API key to the user's environment so future launches pick it up."""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
        ) as k:
            winreg.SetValueEx(k, "GEMINI_API_KEY", 0, winreg.REG_SZ, key)
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x1A, 0, "Environment", 0x0002, 5000,
            ctypes.byref(ctypes.c_ulong()),
        )
    except ImportError:
        import subprocess
        subprocess.run(["setx", "GEMINI_API_KEY", key], check=True)
    os.environ["GEMINI_API_KEY"] = key


class QueueLogHandler(logging.Handler):
    """Push analyzer log lines into a queue for the UI to display."""

    def __init__(self, q: "queue.Queue"):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put(("log", self.format(record)))
        except Exception:
            pass


class VideoAnalyzerApp:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.q: "queue.Queue" = queue.Queue()
        self.worker = None
        self._cancel = threading.Event()
        self._viz_running = False
        self._viz_phase = 0.0
        self._dropped_paths = []   # files/folders from the last drag-and-drop
        self._drop_display = ""    # what the path box shows for a multi-item drop

        root.title("Video Analyzer")
        # Size to fit the screen so the results box is always visible, then center.
        try:
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            w = min(1040, sw - 80)
            h = min(1000, sh - 120)
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2 - 20)
            root.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            root.geometry("1000x940")
        root.minsize(820, 600)
        root.configure(fg_color=COL_BG)
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(0, weight=1)

        # Fonts
        self.f_title = ctk.CTkFont("Segoe UI", 26, "bold")
        self.f_sub = ctk.CTkFont("Segoe UI", 13)
        self.f_section = ctk.CTkFont("Segoe UI", 13, "bold")
        self.f_body = ctk.CTkFont("Segoe UI", 13)
        self.f_mono = ctk.CTkFont("Cascadia Code", 12)
        self.f_btn = ctk.CTkFont("Segoe UI", 14, "bold")

        # Everything lives in one scrollable page so it fits any screen size.
        page = ctk.CTkScrollableFrame(root, fg_color=COL_BG)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)

        # ---- Header --------------------------------------------------------
        header = ctk.CTkFrame(page, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 6))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="🎬  Video Analyzer", font=self.f_title,
                     text_color=COL_TEXT).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header, text="AI ad-creative analysis, powered by Gemini",
                     font=self.f_sub, text_color=COL_MUTED).grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.viz_var = ctk.StringVar(value="on")
        self.viz_switch = ctk.CTkSwitch(header, text="Visualizer", variable=self.viz_var,
                                        onvalue="on", offvalue="off", font=self.f_body,
                                        command=self._on_viz_toggle)
        self.viz_switch.grid(row=0, column=1, rowspan=2, sticky="e", padx=(0, 4))

        # ---- Controls area (cards) ----------------------------------------
        body = ctk.CTkFrame(page, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew", padx=14, pady=6)
        body.grid_columnconfigure(0, weight=1)

        self._last_results = []  # successful analyses from the last run (for compile)
        self._build_source_card(body)
        self._build_options_card(body)
        self._build_key_card(body)
        self._build_batch_card(body)
        self._build_action_row(body)

        # ---- Visualizer (hidden until running, if enabled) -----------------
        self.viz_frame = ctk.CTkFrame(page, fg_color=COL_CARD, corner_radius=14, height=150)
        self.viz_frame.grid(row=2, column=0, sticky="ew", padx=14, pady=(8, 0))
        self.viz_frame.grid_propagate(False)
        self.viz_canvas = tk.Canvas(self.viz_frame, bg="#08090c", highlightthickness=0)
        self.viz_canvas.pack(fill="both", expand=True, padx=6, pady=6)
        self.viz_frame.grid_remove()  # shown only while analyzing

        # ---- Output --------------------------------------------------------
        out_card = ctk.CTkFrame(page, fg_color=COL_CARD, corner_radius=14)
        out_card.grid(row=3, column=0, sticky="nsew", padx=14, pady=(8, 14))
        out_card.grid_columnconfigure(0, weight=1)
        out_card.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(out_card, text="Results", font=self.f_section,
                     text_color=COL_TEXT).grid(row=0, column=0, sticky="w", padx=16, pady=(12, 4))
        self.output = ctk.CTkTextbox(out_card, wrap="word", font=self.f_mono, height=340,
                                     fg_color="#0c0e12", text_color=COL_TEXT, corner_radius=10)
        self.output.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))
        self.output.insert("end", "Drag & drop videos into the box above, or pick a file or folder, then click Analyze.\n")
        self.output.configure(state="disabled")

        # Wire analyzer logs into the UI
        self._log_handler = QueueLogHandler(self.q)
        self._log_handler.setFormatter(logging.Formatter("%(message)s"))
        alog = logging.getLogger("video_analyzer.analyzer")
        alog.setLevel(logging.INFO)
        alog.addHandler(self._log_handler)

        self.root.after(120, self._poll_queue)

    # --------------------------------------------------------------- cards
    def _card(self, parent, title):
        card = ctk.CTkFrame(parent, fg_color=COL_CARD, corner_radius=14)
        card.grid(sticky="ew", pady=6)
        card.grid_columnconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(card, text=title, font=self.f_section, text_color=COL_TEXT).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(12, 6))
        return card

    def _build_source_card(self, parent):
        card = self._card(parent, "1 · Choose what to analyze")
        card.grid_columnconfigure(0, weight=1)
        self.path_var = ctk.StringVar()
        self.path_entry = ctk.CTkEntry(card, textvariable=self.path_var, font=self.f_body,
                                       placeholder_text="No file or folder selected",
                                       height=38, corner_radius=8)
        self.path_entry.grid(row=1, column=0, sticky="ew", padx=(16, 8), pady=(0, 16))
        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.grid(row=1, column=1, sticky="e", padx=(0, 16), pady=(0, 16))
        ctk.CTkButton(btns, text="Choose File", width=110, height=38, font=self.f_body,
                      command=self.choose_file).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Choose Folder", width=120, height=38, font=self.f_body,
                      command=self.choose_folder).pack(side="left", padx=4)

        if not HAS_DND:
            return
        # Drop zone — drag videos (or a folder) straight in for a quick breakdown
        self.drop_zone = ctk.CTkFrame(card, fg_color=COL_DROP, corner_radius=10, height=96,
                                      border_width=2, border_color="#2a3040")
        self.drop_zone.grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 8))
        self.drop_zone.grid_propagate(False)
        self.drop_zone.grid_columnconfigure(0, weight=1)
        self.drop_zone.grid_rowconfigure(0, weight=1)
        self.drop_lbl = ctk.CTkLabel(
            self.drop_zone, text="⬇  Drag & drop videos here  (or a folder)",
            font=self.f_section, text_color=COL_MUTED)
        self.drop_lbl.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.autorun_var = ctk.StringVar(value="on")
        ctk.CTkCheckBox(card, text="Start analyzing as soon as I drop files",
                        variable=self.autorun_var, onvalue="on", offvalue="off",
                        font=self.f_body).grid(row=3, column=0, columnspan=2, sticky="w",
                                               padx=16, pady=(0, 14))
        self.path_entry.grid_configure(pady=(0, 10))
        btns.grid_configure(pady=(0, 10))
        self._register_drop(self.drop_zone)

    # ------------------------------------------------------------ drag & drop
    def _register_drop(self, widget):
        """Make a widget (and everything inside it) accept dropped files."""
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<DropEnter>>", self._on_drag_enter)
            widget.dnd_bind("<<DropLeave>>", self._on_drag_leave)
            widget.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass
        for child in widget.winfo_children():
            self._register_drop(child)

    def _on_drag_enter(self, event):
        self.drop_zone.configure(fg_color=COL_DROP_HOVER, border_color=COL_ACCENT)
        self.drop_lbl.configure(text="Release to add", text_color=COL_TEXT)
        return event.action

    def _on_drag_leave(self, event):
        self.drop_zone.configure(fg_color=COL_DROP, border_color="#2a3040")
        self.drop_lbl.configure(text="⬇  Drag & drop videos here  (or a folder)",
                                text_color=COL_MUTED)
        return event.action

    def _on_drop(self, event):
        self._on_drag_leave(event)
        items = [Path(p) for p in self.root.tk.splitlist(event.data)]
        usable = [p for p in items
                  if p.is_dir() or (p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS)]
        ignored = [p.name for p in items if p not in usable]
        if not usable:
            messagebox.showwarning(
                "Not a video",
                "Those files aren't videos this app can read.\n\n"
                "Supported: " + ", ".join(sorted(VIDEO_EXTENSIONS)))
            return event.action

        if len(usable) == 1:
            self._dropped_paths = []
            self.path_var.set(str(usable[0]))
        else:
            self._dropped_paths = [str(p) for p in usable]
            self._drop_display = f"{len(usable)} items dropped: " + ", ".join(p.name for p in usable)
            self.path_var.set(self._drop_display)

        note = f"Added {len(usable)} item(s)"
        if ignored:
            note += f" · ignored {len(ignored)} non-video file(s)"
        self.status_var.set(note)

        if self.autorun_var.get() == "on":
            self.root.after(50, self.start_analysis)
        return event.action

    def _selected_path(self):
        """The path(s) to analyze: the dropped list, or whatever is in the box."""
        text = self.path_var.get().strip().strip('"')
        if self._dropped_paths and text == self._drop_display:
            return self._dropped_paths
        return text

    def _build_options_card(self, parent):
        card = self._card(parent, "2 · Options")
        card.grid_columnconfigure(1, weight=1)

        # Analysis type (lens)
        ctk.CTkLabel(card, text="Analysis type", font=self.f_body, text_color=COL_MUTED,
                     width=110, anchor="w").grid(row=1, column=0, sticky="w", padx=(16, 8), pady=6)
        self.lens_var = ctk.StringVar(value=list(LENSES.keys())[0])
        self.lens_menu = ctk.CTkOptionMenu(card, values=list(LENSES.keys()),
                                           variable=self.lens_var, font=self.f_body,
                                           width=260, command=lambda v: self._on_lens_change())
        self.lens_menu.grid(row=1, column=1, sticky="w", pady=6)

        # Model
        ctk.CTkLabel(card, text="Model", font=self.f_body, text_color=COL_MUTED,
                     width=110, anchor="w").grid(row=2, column=0, sticky="w", padx=(16, 8), pady=6)
        self.model_seg = ctk.CTkSegmentedButton(card, values=list(MODEL_SEG.keys()),
                                                font=self.f_body)
        self.model_seg.set(list(MODEL_SEG.keys())[0])
        self.model_seg.grid(row=2, column=1, sticky="w", pady=6)

        # Detail (media resolution) — Low cuts token cost, fine for ad creatives
        ctk.CTkLabel(card, text="Detail", font=self.f_body, text_color=COL_MUTED,
                     width=110, anchor="w").grid(row=3, column=0, sticky="w", padx=(16, 8), pady=6)
        drow = ctk.CTkFrame(card, fg_color="transparent")
        drow.grid(row=3, column=1, sticky="w", pady=6)
        self.detail_var = ctk.StringVar(value="Standard")
        ctk.CTkOptionMenu(drow, values=["Standard", "Low (cheaper)"],
                          variable=self.detail_var, font=self.f_body, width=150).pack(side="left")
        ctk.CTkLabel(drow, text="Low ≈ ⅓ the tokens; fine unless you need fine print",
                     font=self.f_sub, text_color=COL_MUTED).pack(side="left", padx=10)

        # Videos at once (parallel workers) — big speed lever on the paid tier
        ctk.CTkLabel(card, text="Videos at once", font=self.f_body, text_color=COL_MUTED,
                     width=110, anchor="w").grid(row=4, column=0, sticky="w", padx=(16, 8), pady=6)
        wrow = ctk.CTkFrame(card, fg_color="transparent")
        wrow.grid(row=4, column=1, sticky="ew", pady=6)
        self.workers_var = ctk.IntVar(value=1)
        self.workers_slider = ctk.CTkSlider(wrow, from_=1, to=5, number_of_steps=4,
                                            variable=self.workers_var, width=200,
                                            command=lambda v: self._update_workers_label())
        self.workers_slider.pack(side="left")
        self.workers_lbl = ctk.CTkLabel(wrow, text="1 at a time (paid tier: try 3–5)",
                                        font=self.f_body, text_color=COL_TEXT)
        self.workers_lbl.pack(side="left", padx=12)

        # Pause slider
        ctk.CTkLabel(card, text="Pause between", font=self.f_body, text_color=COL_MUTED,
                     width=110, anchor="w").grid(row=5, column=0, sticky="w", padx=(16, 8), pady=6)
        prow = ctk.CTkFrame(card, fg_color="transparent")
        prow.grid(row=5, column=1, sticky="ew", pady=6)
        self.delay_var = ctk.IntVar(value=2)
        self.delay_slider = ctk.CTkSlider(prow, from_=0, to=30, number_of_steps=30,
                                          variable=self.delay_var, width=280,
                                          command=lambda v: self._update_delay_label())
        self.delay_slider.pack(side="left")
        self.delay_lbl = ctk.CTkLabel(prow, text="2 sec between videos", font=self.f_body,
                                      text_color=COL_TEXT)
        self.delay_lbl.pack(side="left", padx=12)

        # Output format
        ctk.CTkLabel(card, text="Save reports as", font=self.f_body, text_color=COL_MUTED,
                     width=110, anchor="w").grid(row=6, column=0, sticky="w", padx=(16, 8), pady=6)
        self.format_var = ctk.StringVar(value=list(SAVE_OPTIONS.keys())[0])
        self.format_menu = ctk.CTkOptionMenu(card, values=list(SAVE_OPTIONS.keys()),
                                             variable=self.format_var, font=self.f_body,
                                             width=220, command=lambda v: self._toggle_save())
        self.format_menu.grid(row=6, column=1, sticky="w", pady=6)

        # Save folder (hidden/disabled until a format is chosen)
        ctk.CTkLabel(card, text="Save to", font=self.f_body, text_color=COL_MUTED,
                     width=110, anchor="w").grid(row=7, column=0, sticky="w", padx=(16, 8), pady=(6, 16))
        srow = ctk.CTkFrame(card, fg_color="transparent")
        srow.grid(row=7, column=1, sticky="ew", padx=(0, 16), pady=(6, 16))
        srow.grid_columnconfigure(0, weight=1)
        self.outdir_var = ctk.StringVar()
        self.outdir_entry = ctk.CTkEntry(srow, textvariable=self.outdir_var, font=self.f_body,
                                         placeholder_text="Choose a destination folder",
                                         height=36, corner_radius=8, state="disabled")
        self.outdir_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.outdir_btn = ctk.CTkButton(srow, text="Choose Folder", width=120, height=36,
                                        font=self.f_body, command=self.choose_outdir,
                                        state="disabled")
        self.outdir_btn.grid(row=0, column=1, sticky="e")

        # Resume (skip already-saved) — lets a stopped batch pick up where it left off
        self.resume_var = ctk.StringVar(value="on")
        self.resume_chk = ctk.CTkCheckBox(
            card, text="Resume — skip videos already saved in that folder",
            variable=self.resume_var, onvalue="on", offvalue="off", font=self.f_body)
        self.resume_chk.grid(row=8, column=0, columnspan=2, sticky="w", padx=16, pady=(0, 14))

    def _build_key_card(self, parent):
        card = self._card(parent, "3 · Gemini API key")
        card.grid_columnconfigure(0, weight=1)
        self.key_var = ctk.StringVar()
        self.key_entry = ctk.CTkEntry(card, textvariable=self.key_var, font=self.f_body,
                                      show="•", placeholder_text="Paste your AIza… key",
                                      height=38, corner_radius=8)
        self.key_entry.grid(row=1, column=0, sticky="ew", padx=(16, 8), pady=(0, 4))
        self.save_key_btn = ctk.CTkButton(card, text="Save key", width=110, height=38,
                                          font=self.f_body, command=self.save_key)
        self.save_key_btn.grid(row=1, column=1, sticky="e", padx=(0, 16), pady=(0, 4))
        self.key_hint = ctk.CTkLabel(card, text="", font=self.f_sub, text_color=COL_MUTED,
                                     anchor="w")
        self.key_hint.grid(row=2, column=0, columnspan=2, sticky="w", padx=16, pady=(0, 14))
        self._refresh_key_hint()

    def _build_batch_card(self, parent):
        card = self._card(parent, "4 · Batch tools — turn many reports into one")
        card.grid_columnconfigure(2, weight=1)
        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.grid(row=1, column=0, columnspan=3, sticky="w", padx=16, pady=(0, 6))
        self.synth_btn = ctk.CTkButton(btns, text="Build synthesis report", width=190, height=38,
                                       font=self.f_body, command=lambda: self.build_compile("synthesis"))
        self.synth_btn.pack(side="left", padx=(0, 8))
        self.hook_btn = ctk.CTkButton(btns, text="Build hook bank", width=150, height=38,
                                      font=self.f_body, command=lambda: self.build_compile("hookbank"))
        self.hook_btn.pack(side="left")
        ctk.CTkLabel(card,
                     text="Uses this session's analyzed videos, or pick a folder of saved .md reports.",
                     font=self.f_sub, text_color=COL_MUTED, anchor="w").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=16, pady=(0, 14))

    def _build_action_row(self, parent):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.grid(sticky="ew", pady=(10, 4))
        row.grid_columnconfigure(2, weight=1)
        self.run_btn = ctk.CTkButton(row, text="Analyze", width=150, height=44,
                                     font=self.f_btn, corner_radius=10,
                                     command=self.start_analysis)
        self.run_btn.grid(row=0, column=0, sticky="w")
        self.stop_btn = ctk.CTkButton(row, text="Stop", width=90, height=44,
                                      font=self.f_btn, corner_radius=10,
                                      fg_color="#3a2326", hover_color="#5a2a2f",
                                      text_color="#f2b8bc", command=self.stop_analysis,
                                      state="disabled")
        self.stop_btn.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.progress = ctk.CTkProgressBar(row, mode="determinate", height=10, corner_radius=6)
        self.progress.grid(row=0, column=2, sticky="ew", padx=16)
        self.progress.set(0)
        self.status_var = ctk.StringVar(value="Ready")
        ctk.CTkLabel(row, textvariable=self.status_var, font=self.f_body,
                     text_color=COL_MUTED).grid(row=0, column=3, sticky="e")
        self.usage_var = ctk.StringVar(value="Tokens this run: 0")
        ctk.CTkLabel(row, textvariable=self.usage_var, font=self.f_sub,
                     text_color=COL_MUTED).grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

    # --------------------------------------------------------------- helpers
    def _update_delay_label(self):
        n = int(self.delay_var.get())
        self.delay_lbl.configure(text=f"{n} sec between videos")

    def _update_workers_label(self):
        n = int(self.workers_var.get())
        if n <= 1:
            self.workers_lbl.configure(text="1 at a time (paid tier: try 3–5)")
        else:
            self.workers_lbl.configure(text=f"{n} at a time — faster (paid key recommended)")

    def _on_lens_change(self):
        lens = LENSES.get(self.lens_var.get(), {})
        ext = lens.get("ext")
        if ext:
            self.status_var.set(f"Transcript mode — reports save as .{ext}")
        else:
            self.status_var.set("Ready")

    def _refresh_key_hint(self):
        saved = (os.environ.get("GEMINI_API_KEY") or "").strip()
        if saved.startswith("AIza"):
            self.key_hint.configure(text="✓ Using your saved key — you can leave this box blank.",
                                    text_color=COL_OK)
        elif saved:
            self.key_hint.configure(text="Saved key looks invalid. Paste your AIza… key and click Save key.",
                                    text_color=COL_ERR)
        else:
            self.key_hint.configure(text="Paste your key (starts with AIza), then click Save key so it's remembered.",
                                    text_color=COL_MUTED)

    def save_key(self):
        key = self.key_var.get().strip()
        if not key:
            messagebox.showwarning("No key", "Paste your API key into the box first.")
            return
        if not key.startswith("AIza"):
            if not messagebox.askyesno(
                "Doesn't look right",
                "A Google AI Studio API key normally starts with 'AIza'.\n\n"
                f"The key you pasted starts with '{key[:6]}…'. Save it anyway?",
            ):
                return
        try:
            _persist_key(key)
        except Exception as e:
            messagebox.showerror("Couldn't save", f"Could not save the key:\n{e}")
            return
        self._refresh_key_hint()
        messagebox.showinfo(
            "Saved",
            "Your API key is saved. From now on the app uses it automatically — "
            "you can leave the key box blank next time.",
        )

    def _toggle_save(self):
        saving = SAVE_OPTIONS.get(self.format_var.get()) is not None
        state = "normal" if saving else "disabled"
        self.outdir_entry.configure(state=state)
        self.outdir_btn.configure(state=state)

    def choose_file(self):
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS))
        path = filedialog.askopenfilename(
            title="Choose a video file",
            filetypes=[("Video files", exts), ("All files", "*.*")],
        )
        if path:
            self.path_var.set(path)

    def choose_folder(self):
        path = filedialog.askdirectory(title="Choose a folder of videos")
        if path:
            self.path_var.set(path)

    def choose_outdir(self):
        path = filedialog.askdirectory(title="Choose where to save reports")
        if path:
            self.outdir_var.set(path)

    def _append(self, text: str):
        self.output.configure(state="normal")
        self.output.insert("end", text)
        self.output.see("end")
        self.output.configure(state="disabled")

    def _set_running(self, running: bool):
        self.run_btn.configure(state="disabled" if running else "normal",
                               text="Analyzing…" if running else "Analyze")
        self.stop_btn.configure(state="normal" if running else "disabled")
        for b in (self.synth_btn, self.hook_btn):
            b.configure(state="disabled" if running else "normal")
        if running:
            self.progress.configure(mode="indeterminate")
            self.progress.start()
            self._viz_start()
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress.set(0)
            self._viz_stop()

    # ------------------------------------------------------------ visualizer
    def _on_viz_toggle(self):
        if self._viz_is_running_job():
            # analysis in progress: start/stop the animation live
            if self.viz_var.get() == "on":
                self._viz_start()
            else:
                self._viz_stop()

    def _viz_is_running_job(self):
        return bool(self.worker and self.worker.is_alive())

    def _viz_start(self):
        if self.viz_var.get() != "on":
            return
        self.viz_frame.grid()  # show
        if not self._viz_running:
            self._viz_running = True
            self._viz_tick()

    def _viz_stop(self):
        self._viz_running = False
        self.viz_frame.grid_remove()  # hide

    def _viz_tick(self):
        if not self._viz_running:
            return
        c = self.viz_canvas
        try:
            w = c.winfo_width()
            h = c.winfo_height()
        except Exception:
            w = h = 0
        if w > 1 and h > 1:
            c.delete("all")
            bars = 56
            bw = w / bars
            t = self._viz_phase
            for i in range(bars):
                # layered sine waves for a lively equalizer feel
                amp = (math.sin(i * 0.30 + t * 0.16)
                       + math.sin(i * 0.13 - t * 0.11)
                       + math.sin(i * 0.07 + t * 0.05)) / 3.0
                amp = (amp + 1) / 2  # -> 0..1
                bh = 6 + amp * (h - 12)
                hue = ((i / bars) + t * 0.012) % 1.0
                r, g, b = colorsys.hsv_to_rgb(hue, 0.95, 1.0)
                col = f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"
                x0 = i * bw + 1
                x1 = (i + 1) * bw - 1
                # mirrored top+bottom from the vertical center = retro scope vibe
                cy = h / 2
                c.create_rectangle(x0, cy - bh / 2, x1, cy + bh / 2, fill=col, outline="")
            self._viz_phase += 1
        self.root.after(40, self._viz_tick)

    # -------------------------------------------------------------- action
    def start_analysis(self):
        if self.worker and self.worker.is_alive():
            return

        path = self._selected_path()
        if not path:
            messagebox.showwarning("No selection", "Please choose (or drop in) a video file or a folder first.")
            return
        for p in (path if isinstance(path, list) else [path]):
            if not Path(p).expanduser().exists():
                messagebox.showerror("Not found", f"That path doesn't exist:\n{p}")
                return

        key = self.key_var.get().strip() or os.environ.get("GEMINI_API_KEY")
        if not key:
            messagebox.showerror(
                "No API key",
                "No Gemini API key found.\n\nPaste your key into the box (and click Save key), "
                "or get one at https://aistudio.google.com/apikey",
            )
            return

        model = MODEL_SEG.get(self.model_seg.get(), DEFAULT_MODEL)
        lens = LENSES.get(self.lens_var.get(), {})
        custom_prompt = lens.get("prompt")
        lens_ext = lens.get("ext")
        fmt = SAVE_OPTIONS.get(self.format_var.get())
        output_dir = self.outdir_var.get().strip() if fmt else None
        if fmt and not output_dir:
            messagebox.showwarning("No save folder", "Please choose a folder to save reports, or pick 'Don't save'.")
            return
        # Transcript lenses always save as their native file type (.txt/.srt)
        if fmt and lens_ext:
            fmt = lens_ext
        delay = float(int(self.delay_var.get()))
        skip_existing = (self.resume_var.get() == "on") and bool(output_dir)
        workers = int(self.workers_var.get())
        media_resolution = "low" if self.detail_var.get().startswith("Low") else "default"

        self._cancel.clear()
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")
        self._append(f"Starting analysis with {MODELS.get(model, model)}…\n\n")
        self.status_var.set("Working…")
        self.usage_var.set("Tokens this run: 0")
        self._set_running(True)

        self.worker = threading.Thread(
            target=self._run_worker,
            args=(path, model, key, output_dir, fmt or "md", delay, skip_existing,
                  custom_prompt, workers, media_resolution),
            daemon=True,
        )
        self.worker.start()

    def stop_analysis(self):
        self._cancel.set()
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Stopping after current video…")

    def _progress(self, done, total, name):
        self.q.put(("progress", (done, total)))

    def _usage(self, info):
        self.q.put(("usage", info))

    # ------------------------------------------------------------ batch compile
    def build_compile(self, kind):
        if self.worker and self.worker.is_alive():
            return
        key = self.key_var.get().strip() or os.environ.get("GEMINI_API_KEY")
        if not key:
            messagebox.showerror("No API key", "Paste and Save your Gemini API key first.")
            return

        analyses = [
            {"filename": r["filename"], "analysis": r["analysis"]}
            for r in self._last_results
            if r.get("status") == "success" and r.get("analysis")
        ]
        folder = None
        if not analyses:
            folder = filedialog.askdirectory(
                title="No analyses in this session — pick a folder of saved .md reports")
            if not folder:
                return

        title = COMPILE[kind]["title"]
        out_path = filedialog.asksaveasfilename(
            title=f"Save {title} as",
            defaultextension=".md",
            initialfile=title.replace(" ", "_").lower() + ".md",
            filetypes=[("Markdown", "*.md"), ("Word document", "*.docx"), ("PDF", "*.pdf")],
        )
        if not out_path:
            return

        model = MODEL_SEG.get(self.model_seg.get(), DEFAULT_MODEL)
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")
        self._append(f"Building {title}…\n\n")
        self.status_var.set(f"Building {title}…")
        self._set_running(True)

        self.worker = threading.Thread(
            target=self._run_compile,
            args=(kind, analyses, folder, key, model, out_path),
            daemon=True,
        )
        self.worker.start()

    def _run_compile(self, kind, analyses, folder, key, model, out_path):
        try:
            from . import synthesize, report_writers
            data = analyses
            if not data and folder:
                self.q.put(("log", f"Reading saved reports from {folder}…"))
                data = synthesize.load_saved_reports(folder)
            if not data:
                self.q.put(("fatal", "No reports found to compile."))
                return
            body = synthesize.compile_report(
                kind, data, api_key=key, model=model,
                log=lambda m: self.q.put(("log", m)),
            )
            title = COMPILE[kind]["title"]
            report_writers.write_compiled(Path(out_path), title, body)
            self.q.put(("compiled", (title, out_path, body)))
        except Exception as e:
            self.q.put(("fatal", str(e)))

    def _run_worker(self, path, model, key, output_dir, fmt, delay, skip_existing,
                    custom_prompt, workers, media_resolution):
        try:
            from .analyzer import analyze_path  # lazy: loads Gemini SDK on first run
            results = analyze_path(
                path=path,
                model=model,
                api_key=key,
                custom_prompt=custom_prompt,
                output_dir=output_dir,
                output_format=fmt,
                delay=delay,
                cleanup=True,
                progress_cb=self._progress,
                cancel_check=self._cancel.is_set,
                skip_existing=skip_existing,
                usage_cb=self._usage,
                workers=workers,
                media_resolution=media_resolution,
            )
            self.q.put(("done", results))
        except Exception as e:
            self.q.put(("fatal", str(e)))

    # --------------------------------------------------------------- queue
    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append(payload + "\n")
                elif kind == "progress":
                    done, total = payload
                    self.status_var.set(f"{done} of {total} done")
                elif kind == "usage":
                    t = payload.get("tokens", 0)
                    d = payload.get("done", 0)
                    r = payload.get("retries", 0)
                    msg = f"Tokens this run: {t:,}  ·  {d} analyzed"
                    if r:
                        msg += f"  ·  {r} rate-limit waits"
                    self.usage_var.set(msg)
                elif kind == "fatal":
                    self._append(f"\nERROR: {payload}\n")
                    self.status_var.set("Failed")
                    self._set_running(False)
                elif kind == "compiled":
                    title, out_path, body = payload
                    self._append(body + f"\n\n[Saved to: {out_path}]\n")
                    self.status_var.set(f"{title} saved")
                    self._set_running(False)
                elif kind == "done":
                    self._render_results(payload)
                    self._set_running(False)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    def _render_results(self, results: list):
        # keep successful analyses so Batch tools can compile them
        self._last_results = [r for r in results if r.get("status") == "success"]
        success = sum(1 for r in results if r["status"] == "success")
        skipped = sum(1 for r in results if r["status"] == "skipped")
        failed = sum(1 for r in results if r["status"] == "error")
        stopped = self._cancel.is_set()
        self._append("\n" + "═" * 70 + "\n")
        for r in results:
            if r["status"] == "success":
                self._append(f"\n### {r['filename']}\n\n{r['analysis']}\n")
                if r.get("output_path"):
                    self._append(f"\n[Saved to: {r['output_path']}]\n")
                self._append("\n" + "─" * 70 + "\n")
            elif r["status"] == "skipped":
                self._append(f"\n• Skipped (already done): {r['filename']}\n")
            else:
                self._append(f"\nERROR — {r['filename']}: {r.get('error', 'Unknown error')}\n")
                self._append("\n" + "─" * 70 + "\n")
        parts = [f"{success} analyzed"]
        if skipped:
            parts.append(f"{skipped} skipped")
        if failed:
            parts.append(f"{failed} failed")
        prefix = "Stopped" if stopped else "Done"
        self.status_var.set(f"{prefix} · " + ", ".join(parts))
        if stopped:
            self._append("\nStopped. Click Analyze again to resume where it left off "
                         "(already-saved videos are skipped).\n")
        elif failed:
            self._append(f"\n{failed} video(s) failed (usually a temporary server hiccup). "
                         "Click Analyze again — Resume re-tries only the ones that didn't finish.\n")


if HAS_DND:
    class _AppWindow(ctk.CTk, TkinterDnD.DnDWrapper):
        """A customtkinter window with drag-and-drop turned on."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.TkdndVersion = TkinterDnD._require(self)
else:
    _AppWindow = ctk.CTk


def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = _AppWindow()
    VideoAnalyzerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
