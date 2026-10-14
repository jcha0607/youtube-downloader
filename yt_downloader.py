import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import os
import sys
import shutil
import subprocess
import tempfile
import queue
import urllib.request
import json
import ssl
import webbrowser
import yt_dlp
import imageio_ffmpeg

# ── 플랫폼 ─────────────────────────────────────────────────────────────────────
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
FONT = "맑은 고딕" if IS_WIN else "Apple SD Gothic Neo"

# ── 버전 관리 ──────────────────────────────────────────────────────────────────
# GitHub Actions 빌드 시 이 값이 자동으로 업데이트됩니다
APP_VERSION = "1.0.0"
GITHUB_USER = "jcha0607"
GITHUB_REPO = "youtube-downloader"
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/releases/latest"
DOWNLOAD_PAGE = f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}/releases/latest"
# 릴리스에 올라가는 플랫폼별 파일 이름
ASSET_NAME = "YoutubeDownloader.exe" if IS_WIN else "YoutubeDownloader.dmg"


def get_ffmpeg_path():
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _ssl_context():
    """패키징된 앱(특히 macOS)은 시스템 인증서를 못 찾아 https 접속이 실패하므로 certifi 인증서를 사용"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _version_tuple(v):
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def _is_newer(latest, current):
    """숫자 단위로 비교 (1.0.10 > 1.0.9). 형식이 달라 비교 불가면 값이 다를 때만 새 버전으로 취급"""
    a, b = _version_tuple(latest), _version_tuple(current)
    if a == (0,) or b == (0,):
        return latest != current
    return a > b


def check_for_update():
    """최신 버전 확인 (백그라운드 스레드에서 호출).

    새 버전 정보 (버전, 이 플랫폼용 파일 URL 또는 None) 를 반환, 실패 시 None.
    """
    try:
        req = urllib.request.Request(
            RELEASES_URL,
            headers={"User-Agent": "YoutubeDownloader"}
        )
        with urllib.request.urlopen(req, timeout=5, context=_ssl_context()) as resp:
            data = json.loads(resp.read().decode())
        latest = data.get("tag_name", "").lstrip("v")
        if not latest:
            return None
        asset_url = None
        for a in data.get("assets", []):
            if a.get("name") == ASSET_NAME:
                asset_url = a.get("browser_download_url")
        return latest, asset_url
    except Exception:
        return None


# ── 자동 업데이트 (실행 중인 앱을 같은 위치에서 새 버전으로 교체) ─────────────────────
def install_target():
    """지금 실행 중인 앱의 설치 경로 (Windows: exe 파일, Mac: .app 폴더).
    소스로 직접 실행 중이면 None."""
    if not getattr(sys, "frozen", False):
        return None
    exe = os.path.abspath(sys.executable)
    if IS_WIN:
        return exe
    if IS_MAC:
        p = exe
        for _ in range(3):  # X.app/Contents/MacOS/X → X.app
            p = os.path.dirname(p)
        return p if p.endswith(".app") else None
    return None


def can_self_update():
    target = install_target()
    if not target:
        return False
    if IS_MAC and target.startswith("/Volumes/"):  # 디스크 이미지에서 바로 실행 중
        return False
    return os.access(os.path.dirname(target), os.W_OK)


def download_file(url, dest, progress_cb=None):
    req = urllib.request.Request(url, headers={"User-Agent": "YoutubeDownloader"})
    with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp, \
            open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress_cb and total:
                progress_cb(done / total * 100)


def _relaunch_env():
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # 새 프로세스가 이전 실행의 임시 폴더를 물려받지 않게
    return env


def apply_update_windows(url, target, progress_cb=None):
    """새 exe를 같은 폴더에 받아 두고, 종료 후 배치 파일이 기존 exe 위치에 덮어쓰고 재실행"""
    new_path = target + ".new"
    download_file(url, new_path, progress_cb)
    bat = os.path.join(tempfile.gettempdir(), "ytdl_update.bat")
    script = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "set n=0\r\n"
        ":retry\r\n"
        "set /a n+=1\r\n"
        "timeout /t 1 /nobreak >nul\r\n"
        f'move /y "{new_path}" "{target}" >nul 2>&1\r\n'
        "if errorlevel 1 (\r\n"
        "  if %n% lss 60 goto retry\r\n"
        "  goto done\r\n"
        ")\r\n"
        f'start "" "{target}"\r\n'
        ":done\r\n"
        '(goto) 2>nul & del "%~f0"\r\n'
    )
    with open(bat, "w", encoding="utf-8", newline="") as f:
        f.write(script)
    subprocess.Popen(
        ["cmd", "/c", bat],
        creationflags=0x00000008 | 0x08000000,  # DETACHED_PROCESS | CREATE_NO_WINDOW
        close_fds=True, env=_relaunch_env(),
    )


def apply_update_mac(url, target, progress_cb=None):
    """dmg를 받아 .app을 기존 위치 옆에 복사해 두고, 종료 후 교체하고 재실행"""
    tmp = tempfile.mkdtemp(prefix="ytdl_update_")
    dmg = os.path.join(tmp, ASSET_NAME)
    mount = os.path.join(tmp, "mnt")
    os.makedirs(mount)
    staging = target + ".new"
    download_file(url, dmg, progress_cb)
    subprocess.run(
        ["hdiutil", "attach", "-nobrowse", "-readonly", "-noverify",
         "-mountpoint", mount, dmg],
        check=True, capture_output=True,
    )
    try:
        apps = [n for n in os.listdir(mount) if n.endswith(".app")]
        if not apps:
            raise RuntimeError("dmg 안에 앱이 없습니다")
        if os.path.exists(staging):
            shutil.rmtree(staging)
        subprocess.run(["ditto", os.path.join(mount, apps[0]), staging],
                       check=True, capture_output=True)
    finally:
        subprocess.run(["hdiutil", "detach", "-force", mount], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)
    script = (
        'while kill -0 "$1" 2>/dev/null; do sleep 0.5; done\n'
        'rm -rf "$2" && mv "$3" "$2" || exit 1\n'
        'xattr -dr com.apple.quarantine "$2" 2>/dev/null\n'
        'open "$2"\n'
    )
    subprocess.Popen(
        ["/bin/sh", "-c", script, "sh", str(os.getpid()), target, staging],
        start_new_session=True, close_fds=True, env=_relaunch_env(),
    )


def cleanup_update_leftovers():
    """이전 업데이트가 실패해 남은 임시 파일 정리 (실패해도 무시)"""
    target = install_target()
    if not target:
        return
    leftover = target + ".new"
    try:
        if os.path.isdir(leftover):
            shutil.rmtree(leftover)
        elif os.path.exists(leftover):
            os.remove(leftover)
    except Exception:
        pass


def make_button(parent, text, command, bg, fg, abg=None, afg="white",
                font=None, padx=0, pady=0, **kw):
    """Windows는 일반 버튼. macOS의 Tk 버튼은 배경색이 안 먹어서 라벨로 버튼을 흉내."""
    abg = abg or "#e94560"
    if not IS_MAC:
        return tk.Button(
            parent, text=text, command=command, bg=bg, fg=fg,
            activebackground=abg, activeforeground=afg,
            relief="flat", cursor="hand2", font=font,
            padx=padx, pady=pady, **kw
        )
    lbl = tk.Label(parent, text=text, bg=bg, fg=fg, font=font,
                   padx=padx, pady=pady, cursor="pointinghand", **kw)
    lbl.bind("<ButtonPress-1>", lambda e: lbl.config(bg=abg, fg=afg))

    def _release(e):
        lbl.config(bg=bg, fg=fg)
        if 0 <= e.x < lbl.winfo_width() and 0 <= e.y < lbl.winfo_height():
            command()

    lbl.bind("<ButtonRelease-1>", _release)
    return lbl


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

        cleanup_update_leftovers()
        self._build_ui()
        self._start_worker()
        # 앱 시작 3초 후 업데이트 체크 (UI 로딩 방해 안 되게)
        self.after(3000, self._check_update_async)

    # ── 업데이트 체크 ────────────────────────────────────────────────────────────
    def _check_update_async(self):
        def _check():
            info = check_for_update()
            if info and _is_newer(info[0], APP_VERSION):
                self.after(0, self._show_update_banner, *info)

        threading.Thread(target=_check, daemon=True).start()

    def _show_update_banner(self, latest_version, asset_url=None):
        """상단에 업데이트 알림 배너 표시"""
        banner = tk.Frame(self, bg="#2d6a4f", padx=16, pady=8)
        # 상단 빨간 바 바로 아래에 삽입
        banner.pack(side="top", fill="x", after=self.winfo_children()[0])

        msg = tk.Label(
            banner,
            text=f"🔔  새 버전 {latest_version} 이 있습니다!  (현재: {APP_VERSION})",
            font=(FONT, self._fs(11), "bold"),
            bg="#2d6a4f", fg="white"
        )
        msg.pack(side="left")

        def on_update():
            if asset_url and can_self_update():
                self._start_self_update(asset_url, banner, msg, update_btn)
            else:
                webbrowser.open(DOWNLOAD_PAGE)

        update_btn = make_button(
            banner, "  지금 업데이트  ", on_update,
            bg=SUCCESS, fg="#1a1a2e", abg="#3aa88a", afg="#1a1a2e",
            font=(FONT, self._fs(10), "bold"), padx=10, pady=4
        )
        update_btn.pack(side="right", padx=8)

        close_btn = make_button(
            banner, "✕", banner.destroy,
            bg="#2d6a4f", fg="white", abg="#245a41",
            font=(FONT, self._fs(10)), padx=6
        )
        close_btn.pack(side="right")

    def _start_self_update(self, url, banner, msg, btn):
        """새 버전을 받아 지금 설치된 위치에 덮어쓰고 재시작"""
        if self._is_downloading and not messagebox.askyesno(
            "업데이트", "다운로드가 진행 중입니다.\n업데이트하면 중단됩니다. 계속할까요?"
        ):
            return
        btn.pack_forget()
        msg.config(text="⬇  업데이트 받는 중... 0%")
        apply = apply_update_windows if IS_WIN else apply_update_mac
        target = install_target()

        def progress(pct):
            self.after(0, lambda: msg.config(text=f"⬇  업데이트 받는 중... {int(pct)}%"))

        def work():
            try:
                apply(url, target, progress)
            except Exception:
                self.after(0, fail)
                return
            self.after(0, lambda: msg.config(text="🔄  재시작 중..."))
            self.after(300, lambda: os._exit(0))

        def fail():
            msg.config(text="업데이트에 실패했습니다. 다운로드 페이지에서 직접 받아주세요.")
            webbrowser.open(DOWNLOAD_PAGE)

        threading.Thread(target=work, daemon=True).start()

    # ── helpers ──────────────────────────────────────────────────────────────
    def _fs(self, base):
        return max(8, int(base * self._scale))

    def _btn(self, parent, text, cmd, bg=None, fg=TEXT, **kw):
        return make_button(
            parent, text, cmd,
            bg=bg or ACCENT2, fg=fg, abg=ACCENT,
            font=(FONT, self._fs(11), "bold"),
            **kw
        )

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
        self._canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        self._build_content(self._inner)

    def _on_mousewheel(self, e):
        # Windows는 한 칸에 delta 120, macOS는 이미 작은 정수 단위
        step = int(-1 * (e.delta / 120)) if IS_WIN else -e.delta
        if step:
            self._canvas.yview_scroll(step, "units")

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
            font=(FONT, self._fs(11)),
            bg=PANEL, fg=MUTED, anchor="w"
        )
        self.status_label.pack(side="left", fill="x", expand=True)

        self.pct_label = tk.Label(
            row, textvariable=self.pct_var,
            font=(FONT, self._fs(16), "bold"),
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
            font=(FONT, self._fs(22), "bold"),
            bg=BG, fg=TEXT
        ).pack(pady=(pad, 4))

        tk.Label(
            parent, text="URL을 붙여넣고 화질을 선택하세요",
            font=(FONT, self._fs(11)),
            bg=BG, fg=MUTED
        ).pack(pady=4)

        sep = tk.Frame(parent, bg=ACCENT, height=2)
        sep.pack(fill="x", padx=pad, pady=8)

        # ── URL input ──
        url_frame = tk.Frame(parent, bg=PANEL, padx=16, pady=12)
        url_frame.pack(fill="x", padx=pad, pady=4)

        tk.Label(
            url_frame, text="YouTube URL",
            font=(FONT, self._fs(10), "bold"),
            bg=PANEL, fg=MUTED
        ).pack(anchor="w")

        entry_row = tk.Frame(url_frame, bg=ACCENT2, pady=1)
        entry_row.pack(fill="x", pady=4)

        self.url_entry = tk.Entry(
            entry_row,
            font=(FONT, self._fs(12)),
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
            font=(FONT, self._fs(10), "bold"),
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
            font=(FONT, self._fs(10), "bold"),
            bg=PANEL, fg=MUTED
        ).pack(anchor="w")

        folder_row = tk.Frame(folder_frame, bg=PANEL)
        folder_row.pack(fill="x", pady=4)

        self.folder_label = tk.Label(
            folder_row, textvariable=self.save_dir,
            font=(FONT, self._fs(10)),
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
            font=(FONT, self._fs(11), "bold"),
            bg=PANEL, fg=TEXT
        ).pack(side="left")

        self.queue_count_label = tk.Label(
            hdr, text="(0개)",
            font=(FONT, self._fs(10)),
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
            btn = make_button(
                row_frame, btn_text,
                lambda k=key: self._select_format(k),
                bg=ACCENT if selected else ACCENT2,
                fg="white" if selected else MUTED,
                abg=ACCENT,
                font=(FONT, self._fs(11), "bold" if selected else "normal"),
                padx=10, pady=8
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
                font=(FONT, self._fs(10)),
                bg=PANEL, fg=MUTED
            ).pack(pady=8)
            return

        for i, (url, fmt) in enumerate(self.queue_items):
            item_frame = tk.Frame(self.queue_list_frame, bg=ACCENT2, pady=6, padx=10)
            item_frame.pack(fill="x", pady=2)

            num_label = tk.Label(
                item_frame,
                text=f"{i+1}.",
                font=(FONT, self._fs(10), "bold"),
                bg=ACCENT2, fg=ACCENT, width=3, anchor="w"
            )
            num_label.pack(side="left")

            short_url = url if len(url) <= 48 else url[:45] + "..."
            tk.Label(
                item_frame,
                text=short_url,
                font=(FONT, self._fs(9)),
                bg=ACCENT2, fg=TEXT, anchor="w"
            ).pack(side="left", fill="x", expand=True)

            tk.Label(
                item_frame,
                text=FORMAT_LABELS.get(fmt, fmt),
                font=(FONT, self._fs(9)),
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

        def make_opts(hls_first):
            common = {
                "outtmpl": os.path.join(save_dir, "%(title)s.%(ext)s"),
                "ffmpeg_location": ffmpeg,
                "progress_hooks": [self._progress_hook],
                "retries": 10,
                "fragment_retries": 10,
            }
            if fmt == "mp3":
                return {
                    **common,
                    "format": "bestaudio/best",
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }],
                }
            if fmt == "video_best":
                limit, tail = "", "bestvideo+bestaudio/best"
            else:
                limit = f"[height<={fmt}]"
                tail = f"bestvideo{limit}+bestaudio/best{limit}/best"
            # 일부 직접 주소(https) 형식이 간헐적으로 403이 나서, 마지막 재시도에서는 HLS 형식을 먼저 시도
            fmt_str = f"bestvideo{limit}[protocol^=m3u8]+bestaudio/{tail}" if hls_first else tail
            return {**common, "format": fmt_str, "merge_output_format": "mp4"}

        # 유튜브가 간헐적으로 403(Forbidden)을 주므로, 새 주소를 받아 다시 시도 → 그래도 안 되면 HLS 형식
        for attempt in range(3):
            try:
                with yt_dlp.YoutubeDL(make_opts(hls_first=(attempt == 2))) as ydl:
                    ydl.download([url])
                return
            except yt_dlp.utils.DownloadError as e:
                if "403" not in str(e) or attempt == 2:
                    raise
                self.after(0, self.status_var.set, "접근이 거부되어 다시 시도하는 중...")

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
