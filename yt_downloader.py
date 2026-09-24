import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import os
import queue
import yt_dlp
import imageio_ffmpeg


def get_ffmpeg_path():
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


BG      = "#1a1a2e"
PANEL   = "#16213e"
ACCENT  = "#e94560"
ACCENT2 = "#0f3460"
TEXT    = "#eaeaea"
MUTED   = "#8892a4"
SUCCESS = "#4ecca3"

FORMAT_LABELS = {
    "video_best": "최고 화질",
    "1080":       "1080p",
    "720":        "720p",
    "480":        "480p",
    "mp3":        "MP3 (음악)",
}

PLACEHOLDER = "https://www.youtube.com/watch?v=..."


class YoutubeDownloader(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("유튜브 다운로더")
        self.resizable(True, True)
        self.configure(bg=BG)
        self.minsize(480, 400)

        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w  = min(720, int(sw * 0.55))
        h  = min(800, int(sh * 0.85))
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

        self._scale = max(0.75, min(1.3, sw / 1920))

        self.save_dir    = tk.StringVar(value=os.path.expanduser("~/Downloads"))
        self.format_var  = tk.StringVar(value="video_best")
        self.status_var  = tk.StringVar(value="준비됨")
        self.progress_var = tk.DoubleVar(value=0)
        self.pct_var     = tk.StringVar(value="0%")

        self.dl_queue    = queue.Queue()
        self.queue_items = []          # list of (url, fmt) for display
        self._is_downloading = False

        self._build_ui()
        self._start_worker()

    # ── helpers ──────────────────────────────────────────────────────────────
    def _fs(self, base):
        return max(8, int(base * self._scale))

    def _btn(self, parent, text, cmd, bg=None, fg=TEXT, **kw):
        b = tk.Button(
            parent, text=text, command=cmd,
            bg=bg or ACCENT2, fg=fg,
            activebackground=ACCENT, activeforeground="white",
            relief="flat", cursor="hand2",
            font=("맑은 고딕", self._fs(11), "bold"),
            **kw
        )
        return b

    # ── UI build ─────────────────────────────────────────────────────────────
    def _build_ui(self):
        # top accent bar
        tk.Frame(self, bg=ACCENT, height=6).pack(side="top", fill="x")

        # FIXED bottom progress panel (pack before scroll area)
        self._build_progress_panel()

        # scrollable main area
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill="both", expand=True)

        self._vscroll = tk.Scrollbar(outer, orient="vertical")
        self._vscroll.pack(side="right", fill="y")

        self._canvas = tk.Canvas(
            outer, bg=BG, bd=0, highlightthickness=0,
            yscrollcommand=self._vscroll.set
        )
        self._canvas.pack(side="left", fill="both", expand=True)
        self._vscroll.config(command=self._canvas.yview)

        self._inner = tk.Frame(self._canvas, bg=BG)
        self._inner_id = self._canvas.create_window(
            (0, 0), window=self._inner, anchor="nw"
        )

        self._inner.bind(
            "<Configure>",
            lambda e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")
            )
        )
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfig(self._inner_id, width=e.width)
        )
        self._canvas.bind_all(
            "<MouseWheel>",
            lambda e: self._canvas.yview_scroll(
                int(-1 * (e.delta / 120)), "units"
            )
        )

        self._build_content(self._inner)

    def _build_progress_panel(self):
        """항상 보이는 하단 고정 패널"""
        tk.Frame(self, bg=ACCENT, height=4).pack(side="bottom", fill="x")

        pf = tk.Frame(self, bg=PANEL, padx=16, pady=10)
        pf.pack(side="bottom", fill="x")

        # status + percentage row
        row = tk.Frame(pf, bg=PANEL)
        row.pack(fill="x", pady=4)

        self.status_label = tk.Label(
            row, textvariable=self.status_var,
            font=("맑은 고딕", self._fs(11)),
            bg=PANEL, fg=MUTED, anchor="w"
        )
        self.status_label.pack(side="left", fill="x", expand=True)

        self.pct_label = tk.Label(
            row, textvariable=self.pct_var,
            font=("맑은 고딕", self._fs(16), "bold"),
            bg=PANEL, fg=ACCENT
        )
        self.pct_label.pack(side="right")

        # progress bar
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "dl.Horizontal.TProgressbar",
            troughcolor=ACCENT2, background=ACCENT,
            thickness=8, borderwidth=0
        )
        self._pb = ttk.Progressbar(
            pf, variable=self.progress_var,
            style="dl.Horizontal.TProgressbar",
            orient="horizontal", length=100, mode="determinate"
        )
        self._pb.pack(fill="x", pady=4)

    def _build_content(self, parent):
        pad = int(24 * self._scale)

        # ── title ──
        tk.Label(
            parent, text="유튜브 다운로더",
            font=("맑은 고딕", self._fs(22), "bold"),
            bg=BG, fg=TEXT
        ).pack(pady=(pad, 4))

        tk.Label(
            parent, text="URL을 붙여넣고 화질을 선택하세요",
            font=("맑은 고딕", self._fs(11)),
            bg=BG, fg=MUTED
        ).pack(pady=4)

        sep = tk.Frame(parent, bg=ACCENT, height=2)
        sep.pack(fill="x", padx=pad, pady=8)

        # ── URL input ──
        url_frame = tk.Frame(parent, bg=PANEL, padx=16, pady=12)
        url_frame.pack(fill="x", padx=pad, pady=4)

        tk.Label(
            url_frame, text="YouTube URL",
            font=("맑은 고딕", self._fs(10), "bold"),
            bg=PANEL, fg=MUTED
        ).pack(anchor="w")

        entry_row = tk.Frame(url_frame, bg=ACCENT2, pady=1)
        entry_row.pack(fill="x", pady=4)

        self.url_entry = tk.Entry(
            entry_row,
            font=("맑은 고딕", self._fs(12)),
            bg=ACCENT2, fg=MUTED,
            insertbackground=TEXT,
            relief="flat", bd=8
        )
        self.url_entry.pack(fill="x")
        self.url_entry.insert(0, PLACEHOLDER)

        self.url_entry.bind("<FocusIn>",  self._url_focus_in)
        self.url_entry.bind("<FocusOut>", self._url_focus_out)
        self.url_entry.bind("<KeyPress>", self._url_key)

        # ── format buttons ──
        fmt_frame = tk.Frame(parent, bg=BG, padx=pad)
        fmt_frame.pack(fill="x", pady=8)

        tk.Label(
            fmt_frame, text="화질 / 형식",
            font=("맑은 고딕", self._fs(10), "bold"),
            bg=BG, fg=MUTED
        ).pack(anchor="w", pady=4)

        self._fmt_btn_frame = tk.Frame(fmt_frame, bg=BG)
        self._fmt_btn_frame.pack(fill="x")
        self._build_format_buttons()

        # ── save folder ──
        folder_frame = tk.Frame(parent, bg=PANEL, padx=16, pady=12)
        folder_frame.pack(fill="x", padx=pad, pady=4)

        tk.Label(
            folder_frame, text="저장 위치",
            font=("맑은 고딕", self._fs(10), "bold"),
            bg=PANEL, fg=MUTED
        ).pack(anchor="w")

        folder_row = tk.Frame(folder_frame, bg=PANEL)
        folder_row.pack(fill="x", pady=4)

        self.folder_label = tk.Label(
            folder_row, textvariable=self.save_dir,
            font=("맑은 고딕", self._fs(10)),
            bg=PANEL, fg=TEXT, anchor="w"
        )
        self.folder_label.pack(side="left", fill="x", expand=True)

        self._btn(
            folder_row, "  폴더 변경  ", self._choose_folder,
            padx=8, pady=4
        ).pack(side="right")

        # ── add to queue button ──
        self.add_btn = self._btn(
            parent,
            "  ＋  다운로드  ",
            self._add_to_queue,
            bg=ACCENT, fg="white",
            padx=20, pady=12,
            width=22
        )
        self.add_btn.pack(pady=12)

        # ── queue list ──
        q_frame = tk.Frame(parent, bg=PANEL, padx=16, pady=12)
        q_frame.pack(fill="x", padx=pad, pady=4)

        hdr = tk.Frame(q_frame, bg=PANEL)
        hdr.pack(fill="x", pady=4)

        tk.Label(
            hdr, text="다운로드 대기열",
            font=("맑은 고딕", self._fs(11), "bold"),
            bg=PANEL, fg=TEXT
        ).pack(side="left")

        self.queue_count_label = tk.Label(
            hdr, text="(0개)",
            font=("맑은 고딕", self._fs(10)),
            bg=PANEL, fg=MUTED
        )
        self.queue_count_label.pack(side="left", padx=6)

        self.queue_list_frame = tk.Frame(q_frame, bg=PANEL)
        self.queue_list_frame.pack(fill="x")

        self._refresh_queue_ui()

    # ── format buttons ────────────────────────────────────────────────────────
    def _build_format_buttons(self):
        for w in self._fmt_btn_frame.winfo_children():
            w.destroy()

        current = self.format_var.get()
        per_row = 3
        keys = list(FORMAT_LABELS.keys())

        for i, key in enumerate(keys):
            label = FORMAT_LABELS[key]
            selected = (key == current)

            row_idx = i // per_row
            col_idx = i % per_row

            if col_idx == 0:
                row_frame = tk.Frame(self._fmt_btn_frame, bg=BG)
                row_frame.pack(fill="x", pady=2)

            btn_text = ("✔  " + label) if selected else label
            btn = tk.Button(
                row_frame,
                text=btn_text,
                bg=ACCENT if selected else ACCENT2,
                fg="white" if selected else MUTED,
                activebackground=ACCENT,
                activeforeground="white",
                relief="flat", cursor="hand2",
                font=("맑은 고딕", self._fs(11), "bold" if selected else "normal"),
                padx=10, pady=8,
                command=lambda k=key: self._select_format(k)
            )
            btn.pack(side="left", padx=3, expand=True, fill="x")

    def _select_format(self, val):
        self.format_var.set(val)
        self._build_format_buttons()
        # URL은 건드리지 않음

    # ── URL placeholder ───────────────────────────────────────────────────────
    def _url_focus_in(self, e):
        if self.url_entry.get() == PLACEHOLDER:
            self.url_entry.delete(0, "end")
            self.url_entry.config(fg=TEXT)

    def _url_focus_out(self, e):
        if not self.url_entry.get().strip():
            self.url_entry.insert(0, PLACEHOLDER)
            self.url_entry.config(fg=MUTED)

    def _url_key(self, e):
        self.url_entry.config(fg=TEXT)

    # ── folder ────────────────────────────────────────────────────────────────
    def _choose_folder(self):
        d = filedialog.askdirectory(initialdir=self.save_dir.get())
        if d:
            self.save_dir.set(d)

    # ── queue ─────────────────────────────────────────────────────────────────
    def _add_to_queue(self):
        url = self.url_entry.get().strip()
        if not url or url == PLACEHOLDER:
            messagebox.showwarning("URL 없음", "YouTube URL을 입력해주세요.")
            return

        fmt      = self.format_var.get()
        save_dir = self.save_dir.get()

        self.queue_items.append((url, fmt))
        self.dl_queue.put((url, fmt, save_dir))
        self._refresh_queue_ui()

        # URL 초기화
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, PLACEHOLDER)
        self.url_entry.config(fg=MUTED)

    def _refresh_queue_ui(self):
        for w in self.queue_list_frame.winfo_children():
            w.destroy()

        count = len(self.queue_items)
        self.queue_count_label.config(text=f"({count}개)")

        if count == 0:
            tk.Label(
                self.queue_list_frame,
                text="대기 중인 항목 없음",
                font=("맑은 고딕", self._fs(10)),
                bg=PANEL, fg=MUTED
            ).pack(pady=8)
            return

        for i, (url, fmt) in enumerate(self.queue_items):
            item_frame = tk.Frame(self.queue_list_frame, bg=ACCENT2, pady=6, padx=10)
            item_frame.pack(fill="x", pady=2)

            num_label = tk.Label(
                item_frame,
                text=f"{i+1}.",
                font=("맑은 고딕", self._fs(10), "bold"),
                bg=ACCENT2, fg=ACCENT, width=3, anchor="w"
            )
            num_label.pack(side="left")

            short_url = url if len(url) <= 48 else url[:45] + "..."
            tk.Label(
                item_frame,
                text=short_url,
                font=("맑은 고딕", self._fs(9)),
                bg=ACCENT2, fg=TEXT, anchor="w"
            ).pack(side="left", fill="x", expand=True)

            tk.Label(
                item_frame,
                text=FORMAT_LABELS.get(fmt, fmt),
                font=("맑은 고딕", self._fs(9)),
                bg=ACCENT2, fg=MUTED
            ).pack(side="right")

    # ── worker thread ─────────────────────────────────────────────────────────
    def _start_worker(self):
        def worker():
            while True:
                url, fmt, save_dir = self.dl_queue.get()
                self.after(0, self._on_start)
                try:
                    self._download(url, fmt, save_dir)
                except Exception as exc:
                    self.after(0, lambda e=exc: messagebox.showerror(
                        "다운로드 오류", str(e)
                    ))
                finally:
                    self.dl_queue.task_done()
                    self.after(0, self._on_finish_one, save_dir)

        threading.Thread(target=worker, daemon=True).start()

    def _on_start(self):
        self._is_downloading = True
        self.progress_var.set(0)
        self.pct_var.set("0%")
        self.status_var.set("다운로드 준비 중...")

    def _on_finish_one(self, save_dir):
        if self.queue_items:
            self.queue_items.pop(0)
        self._refresh_queue_ui()

        remaining = self.dl_queue.qsize()
        if remaining == 0:
            self._is_downloading = False
            self.progress_var.set(100)
            self.pct_var.set("완료!")
            self.status_var.set(f"저장 완료 → {save_dir}")
        else:
            self.status_var.set(f"다음 항목 준비 중... (남은 항목: {remaining}개)")

    # ── download ──────────────────────────────────────────────────────────────
    def _download(self, url, fmt, save_dir):
        ffmpeg = get_ffmpeg_path()

        if fmt == "mp3":
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": os.path.join(save_dir, "%(title)s.%(ext)s"),
                "ffmpeg_location": ffmpeg,
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
                "progress_hooks": [self._progress_hook],
            }
        else:
            if fmt == "video_best":
                fmt_str = "bestvideo+bestaudio/best"
            else:
                fmt_str = (
                    f"bestvideo[height<={fmt}]+bestaudio/best[height<={fmt}]/best"
                )
            ydl_opts = {
                "format": fmt_str,
                "outtmpl": os.path.join(save_dir, "%(title)s.%(ext)s"),
                "ffmpeg_location": ffmpeg,
                "merge_output_format": "mp4",
                "progress_hooks": [self._progress_hook],
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    def _progress_hook(self, d):
        if d["status"] == "downloading":
            total   = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)

            if total > 0:
                pct = downloaded / total * 100
                self.after(0, self.progress_var.set, pct)
                self.after(0, self.pct_var.set, f"{pct:.1f}%")
            else:
                self.after(0, self.pct_var.set, "...")

            speed   = d.get("speed") or 0
            eta     = d.get("eta") or 0
            dl_mb   = downloaded / 1024 / 1024
            sp_str  = f"{speed/1024/1024:.1f} MB/s" if speed else ""
            eta_str = f"  남은 시간: {eta}초" if eta else ""
            q_remaining = self.dl_queue.qsize()
            q_str = f"  |  대기: {q_remaining}개" if q_remaining else ""

            status = f"{dl_mb:.1f} MB  {sp_str}{eta_str}{q_str}"
            self.after(0, self.status_var.set, status)

        elif d["status"] == "finished":
            self.after(0, self.progress_var.set, 99)
            self.after(0, self.pct_var.set, "99%")
            self.after(0, self.status_var.set, "변환 중...")


if __name__ == "__main__":
    app = YoutubeDownloader()
    app.mainloop()
