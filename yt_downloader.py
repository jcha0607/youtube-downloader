import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import time
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


def get_js_runtimes():
    """유튜브 주소 해독용 JS 런타임(QuickJS). 앱에 같이 들어 있으면 그걸 쓰고, 없으면 yt-dlp 기본값(PATH의 deno)"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "qjs.exe" if IS_WIN else "qjs")
    if os.path.exists(path):
        return {"quickjs": {"path": path}}
    return None


# ── 화질 / 음질 선택 ────────────────────────────────────────────────────────────
TIER_STEPS = [144, 240, 360, 480, 720, 1080, 1440, 2160, 4320]


def _tier_of(f):
    """형식의 화질 단계(짧은 변 기준). 3840x1920 → 2160(4K), 1080x1920 세로 영상 → 1080"""
    h, w = f.get("height") or 0, f.get("width") or 0
    side = min(h, w) if (h and w) else (h or w)
    for t in TIER_STEPS:
        if side <= t:
            return t
    return TIER_STEPS[-1]


def _is_hls(f):
    return str(f.get("protocol") or "").startswith("m3u8")


def _size_of(f, duration):
    n = f.get("filesize") or f.get("filesize_approx")
    if n:
        return int(n)
    tbr = f.get("tbr") or f.get("vbr") or f.get("abr")
    if tbr and duration:
        return int(tbr * 1000 / 8 * duration)
    return None


MP3_BITRATES = [32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]


def _mp3_bitrate(abr):
    """원본 오디오의 비트레이트와 가장 가까운 표준 MP3 비트레이트 (MP3는 임의 값을 못 씀)"""
    if not abr:
        return 128
    return min(MP3_BITRATES, key=lambda b: abs(b - abr))


def _mp3_post(bitrate):
    return {"postprocessors": [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": str(bitrate),
    }]}


def _fmt_size(n):
    mb = n / 1024 / 1024
    if mb >= 1024:
        return f"{mb / 1024:.1f}GB"
    return f"{mb:.0f}MB" if mb >= 10 else f"{mb:.1f}MB"


def _fmt_duration(sec):
    if not sec:
        return ""
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def legacy_spec(key, hls_first=False):
    """영상 정보를 못 읽었을 때(재생목록 등) 쓰는 예전 방식의 형식 지정"""
    if key == "audio_orig":
        return "bestaudio/best", {}
    if key == "audio_mp3":
        return "bestaudio/best", _mp3_post(128)
    limit = "" if key == "video_best" else f"[height<={key}]"
    tail = "bestvideo+bestaudio/best" if not limit else \
        f"bestvideo{limit}+bestaudio/best{limit}/best"
    # 일부 직접 주소(https) 형식이 간헐적으로 403이 나서, 마지막 재시도에서는 HLS 형식을 먼저 시도
    sel = f"bestvideo{limit}[protocol^=m3u8]+bestaudio/{tail}" if hls_first else tail
    return sel, {"merge_output_format": "mp4"}


class FormatPlan:
    """영상 정보에서 선택 가능한 화질/음질, 예상 용량, 실제 받을 형식을 계산"""

    def __init__(self, info):
        self.title = info.get("title") or ""
        self.duration = info.get("duration")
        fmts = info.get("formats") or []

        vids = [f for f in fmts
                if f.get("vcodec") not in (None, "none") and f.get("ext") != "mhtml"
                and (f.get("height") or f.get("width"))]
        video_only = [f for f in vids if f.get("acodec") in (None, "none")]
        self.muxed = not video_only            # 영상+음성이 합쳐진 형식뿐인 경우
        self.tiers = {}
        for f in (video_only or vids):
            self.tiers.setdefault(_tier_of(f), []).append(f)
        for lst in self.tiers.values():        # 직접 주소(https) 먼저, 그 안에서 비트레이트 높은 순
            lst.sort(key=lambda f: (not _is_hls(f), f.get("tbr") or 0), reverse=True)

        auds = [f for f in fmts
                if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
                and "DRC" not in str(f.get("format_note") or "")]
        auds.sort(key=lambda f: (f.get("language_preference") or -1,
                                 f.get("abr") or f.get("tbr") or 0), reverse=True)
        self.audios = auds

        def abr(f):
            return f.get("abr") or f.get("tbr") or 0

        # 가장 좋은 음질. 음질이 거의 같으면(10% 이내) 어느 기기에서나 재생되는 m4a를 우선
        best = auds[0] if auds else None
        if best and best.get("ext") != "m4a":
            for g in auds:
                if (g.get("ext") == "m4a" and abr(best) * 0.9 <= abr(g) <= abr(best) * 1.1
                        and g.get("language_preference") == best.get("language_preference")):
                    best = g
                    break
        self.audio = best

    def best_tier(self):
        return max(self.tiers) if self.tiers else None

    def _video_size(self, tier):
        v = _size_of(self.tiers[tier][0], self.duration)
        if v is None:
            return None
        if not self.muxed and self.audios:
            v += _size_of(self.audios[0], self.duration) or 0
        return v

    def option(self, key):
        """(선택 가능 여부, 버튼에 보여줄 부가 설명)"""
        if key in ("audio_orig", "audio_mp3"):
            f = self.audio
            if not f:
                return False, "없음"
            abr = f.get("abr") or f.get("tbr") or 0
            if key == "audio_orig":
                text = f"{f.get('ext')} · {int(round(abr))}kbps"
                size = _size_of(f, self.duration)
            else:
                br = _mp3_bitrate(abr)
                text = f"mp3 · {br}kbps"
                size = br * 1000 / 8 * self.duration if self.duration else None
            if size:
                text += f" · 약 {_fmt_size(size)}"
            return True, text
        tier = self.best_tier() if key == "video_best" else int(key)
        if tier not in self.tiers:
            return False, "없음"
        size = self._video_size(tier)
        parts = []
        if key == "video_best":
            parts.append(f"{tier}p")
        if size:
            parts.append(f"약 {_fmt_size(size)}")
        return True, " · ".join(parts)

    def download_spec(self, key, hls_first=False):
        """(yt-dlp 형식 문자열, 추가 옵션)"""
        if key in ("audio_orig", "audio_mp3"):
            f = self.audio
            if not f:
                return legacy_spec(key)
            if key == "audio_orig":    # 변환 없이 원본 그대로
                return f["format_id"], {}
            # MP3: 원본 비트레이트를 그대로 가져가고 파일 형식만 mp3로
            return f["format_id"], _mp3_post(_mp3_bitrate(f.get("abr") or f.get("tbr")))
        if key == "video_best" or int(key) in self.tiers:
            tier = self.best_tier() if key == "video_best" else int(key)
        else:   # 그 화질이 없으면 가장 가까운 낮은 화질로
            lower = [t for t in self.tiers if t <= int(key)]
            tier = max(lower) if lower else self.best_tier()
        cands = self.tiers[tier]
        https = [f for f in cands if not _is_hls(f)]
        hls = [f for f in cands if _is_hls(f)]
        order = hls + https if hls_first else https + hls
        with_audio = not self.muxed and bool(self.audios)
        sel = "/".join(f"{f['format_id']}+bestaudio" if with_audio else f["format_id"]
                       for f in order)
        return sel, {"merge_output_format": "mp4"}


def _is_degraded(info):
    """유튜브가 가끔 영상+음성이 합쳐진 저화질 형식 몇 개만 돌려준다. 분리된 영상/음성 형식이 없으면 그런 응답으로 본다"""
    if info.get("_type") == "playlist":
        return False
    fm = [f for f in (info.get("formats") or []) if f.get("ext") != "mhtml"]
    return not any((f.get("vcodec") in (None, "none")) != (f.get("acodec") in (None, "none"))
                   for f in fm)


def fetch_info(url, tries=3):
    """영상 정보 읽기 (다운로드 없이). 재생목록 링크가 섞여 있어도 영상 1개만 본다.
    형식이 부실한 응답이 오면 몇 번 다시 읽어 가장 형식이 많은 결과를 쓴다"""
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "ignore_no_formats_error": True, "skip_download": True}
    js = get_js_runtimes()
    if js:
        opts["js_runtimes"] = js
    best = None
    for i in range(tries):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if best is None or len(info.get("formats") or []) > len(best.get("formats") or []):
            best = info
        if not _is_degraded(info):
            break
        time.sleep(1)
    return best


# 다시 시도해도 소용없는 오류 (영상 자체의 문제)
PERMANENT_ERRORS = (
    "private video", "sign in to confirm", "members-only", "members only",
    "copyright", "not available in your country", "geo restrict", "has been removed",
    "account associated", "has been terminated", "no longer available",
    "age-restricted", "confirm your age", "premieres in", "live event will begin",
    "unsupported url", "is not a valid url",
)
MAX_TRIES = 5


def is_permanent_error(exc):
    msg = str(exc).lower()
    return any(k in msg for k in PERMANENT_ERRORS)


def plan_from_info(info):
    """읽은 정보로 FormatPlan을 만들 수 없으면(재생목록 등) None"""
    if not info or info.get("_type") == "playlist" or not info.get("formats"):
        return None
    plan = FormatPlan(info)
    return plan if (plan.tiers or plan.audios) else None


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
                font=None, padx=0, pady=0, disabled=False, **kw):
    """Windows는 일반 버튼. macOS의 Tk 버튼은 배경색이 안 먹어서 라벨로 버튼을 흉내.
    disabled=True면 눌러도 아무 일도 일어나지 않는다."""
    abg = abg or "#e94560"
    if disabled:
        return tk.Label(parent, text=text, bg=bg, fg=fg, font=font,
                        padx=padx, pady=pady, **kw)
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
    "2160":       "4K",
    "1440":       "2K",
    "1080":       "1080p",
    "720":        "720p",
    "480":        "480p",
    "audio_orig": "오디오: 원본",
    "audio_mp3":  "오디오: MP3",
}

PLACEHOLDER = "https://www.youtube.com/watch?v=..."


class YoutubeDownloader(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"유튜브 다운로더  v{APP_VERSION}")
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
        # 대기열 항목: {"url","fmt","save_dir","status": waiting|running|failed,"error"}
        self.queue_items = []
        self._is_downloading = False
        self._plan = None              # 지금 입력한 URL의 화질/음질 정보 (FormatPlan)
        self._info_token = 0           # 오래된 정보 읽기 결과를 무시하기 위한 번호
        self._info_after = None        # 입력 후 잠깐 기다렸다 읽기 위한 타이머
        self._last_info_url = None

        cleanup_update_leftovers()
        self._update_banner = None     # 현재 떠 있는 업데이트 배너
        self._update_action = None     # 배너의 "지금 업데이트" 동작
        self._build_ui()
        self._build_menu()
        self._fit_initial_size(w, sh)
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

    # ── 메뉴바 ───────────────────────────────────────────────────────────────────
    def _build_menu(self):
        menubar = tk.Menu(self)
        if IS_MAC:
            # macOS는 앱 이름 메뉴(맨 왼쪽)에 "업데이트 확인"을 두는 것이 관례
            app_menu = tk.Menu(menubar, name="apple")
            menubar.add_cascade(menu=app_menu)
            app_menu.add_command(label="업데이트 확인...", command=self._menu_check_update)
            self.createcommand("tkAboutDialog", self._show_about)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="업데이트 확인...", command=self._menu_check_update)
        help_menu.add_separator()
        help_menu.add_command(label="버전 정보", command=self._show_about)
        menubar.add_cascade(label="도움말", menu=help_menu)
        self.config(menu=menubar)

    def _show_about(self):
        messagebox.showinfo("버전 정보", f"유튜브 다운로더\n현재 버전: v{APP_VERSION}")

    def _menu_check_update(self):
        """메뉴에서 직접 업데이트 확인 (결과를 항상 알려줌)"""
        if not self._is_downloading:
            self.status_var.set("업데이트 확인 중...")

        def _check():
            info = check_for_update()
            self.after(0, self._on_manual_check, info)

        threading.Thread(target=_check, daemon=True).start()

    def _on_manual_check(self, info):
        if not self._is_downloading:
            self.status_var.set("준비됨")
        if info is None:
            messagebox.showerror(
                "업데이트 확인", "업데이트 정보를 가져오지 못했습니다.\n인터넷 연결을 확인해 주세요."
            )
            return
        latest, asset_url = info
        if not _is_newer(latest, APP_VERSION):
            messagebox.showinfo("업데이트 확인", f"최신 버전입니다.  (v{APP_VERSION})")
            return
        self._show_update_banner(latest, asset_url)
        if messagebox.askyesno(
            "업데이트 확인",
            f"새 버전 {latest} 이 있습니다.  (현재: {APP_VERSION})\n\n지금 업데이트할까요?"
        ):
            if self._update_action:
                self._update_action()

    def _show_update_banner(self, latest_version, asset_url=None):
        """상단에 업데이트 알림 배너 표시"""
        if self._update_banner is not None and self._update_banner.winfo_exists():
            return
        banner = tk.Frame(self, bg="#2d6a4f", padx=16, pady=8)
        # 상단 빨간 바 바로 아래에 삽입
        banner.pack(side="top", fill="x", after=self._top_bar)
        self._update_banner = banner

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
        self._update_action = on_update

        def close_banner():
            h = banner.winfo_height()
            banner.destroy()
            self._grow_window(-h)

        close_btn = make_button(
            banner, "✕", close_banner,
            bg="#2d6a4f", fg="white", abg="#245a41",
            font=(FONT, self._fs(10)), padx=6
        )
        close_btn.pack(side="right")
        self._grow_window(widget=banner)   # 배너가 다 만들어진 뒤 높이 반영

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
        self._top_bar = tk.Frame(self, bg=ACCENT, height=6)
        self._top_bar.pack(side="top", fill="x")

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

    def _fit_initial_size(self, w, sh):
        """처음 켰을 때 스크롤 없이 모든 UI가 보이도록 창 높이를 내용에 맞춘다
        (화면보다 커지면 화면에 맞추고, 그때부터는 스크롤)"""
        self.update_idletasks()
        need = (self._top_bar.winfo_reqheight() + self._inner.winfo_reqheight()
                + sum(b.winfo_reqheight() for b in self._bottom_widgets))
        self._max_h = max(400, sh - 140)       # 메뉴바/독 자리
        h = max(400, min(need + 4, self._max_h))
        y = max(20, (sh - h) // 2 - 20)
        self.geometry(f"{w}x{h}+{(self.winfo_screenwidth() - w) // 2}+{y}")
        self._auto_h = h                        # 사용자가 크기를 바꾸기 전의 자동 높이

    def _grow_window(self, dh=0, widget=None):
        """업데이트 배너가 생기거나 사라질 때, 사용자가 크기를 안 바꿨다면 창 높이를 같이 조절
        (widget을 주면 그 위젯이 차지할 높이를 레이아웃이 끝난 뒤 읽는다)"""
        self.update_idletasks()
        if widget is not None:
            dh = widget.winfo_reqheight()
        cur = self.winfo_height()
        if abs(cur - self._auto_h) > 2:
            return
        new = max(400, min(cur + dh, self._max_h))
        self.geometry(f"{self.winfo_width()}x{new}")
        self._auto_h = new

    def _on_mousewheel(self, e):
        # Windows는 한 칸에 delta 120, macOS는 이미 작은 정수 단위
        step = int(-1 * (e.delta / 120)) if IS_WIN else -e.delta
        if step:
            self._canvas.yview_scroll(step, "units")

    def _build_progress_panel(self):
        """항상 보이는 하단 고정 패널"""
        bottom_line = tk.Frame(self, bg=ACCENT, height=4)
        bottom_line.pack(side="bottom", fill="x")

        pf = tk.Frame(self, bg=PANEL, padx=16, pady=10)
        pf.pack(side="bottom", fill="x")
        self._bottom_widgets = [bottom_line, pf]

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
        self.url_entry.bind("<KeyRelease>", self._url_changed)
        self.url_entry.bind("<<Paste>>", self._url_paste)
        self.url_entry.bind("<<Cut>>", lambda e: self.after(80, self._url_changed))

        # ── format buttons ──
        fmt_frame = tk.Frame(parent, bg=BG, padx=pad)
        fmt_frame.pack(fill="x", pady=8)

        tk.Label(
            fmt_frame, text="화질 / 형식",
            font=(FONT, self._fs(10), "bold"),
            bg=BG, fg=MUTED
        ).pack(anchor="w", pady=4)

        self._info_label = tk.Label(
            fmt_frame, text="", anchor="nw", justify="left", height=2,
            font=(FONT, self._fs(9)), bg=BG, fg=MUTED,
            wraplength=int(620 * self._scale)
        )
        self._info_label.pack(fill="x", pady=(0, 4))

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
        keys = list(FORMAT_LABELS.keys())
        # 화질 3개씩 두 줄, 오디오 두 개 한 줄 (같은 줄의 버튼은 너비를 똑같이)
        rows = [keys[0:3], keys[3:6], keys[6:8]]

        for r, row_keys in enumerate(rows):
            row_frame = tk.Frame(self._fmt_btn_frame, bg=BG)
            row_frame.pack(fill="x", pady=2)
            group = "fmt3" if len(row_keys) == 3 else "fmt2"
            for c, key in enumerate(row_keys):
                row_frame.grid_columnconfigure(c, weight=1, uniform=group)
                enabled, sub = (True, "")
                if self._plan:
                    enabled, sub = self._plan.option(key)
                selected = enabled and (key == current)

                text = ("✔  " if selected else "") + FORMAT_LABELS[key]
                # 영상 정보가 오기 전에도 두 줄 높이를 잡아 둬서, 정보가 와도 레이아웃이 안 변하게
                text += "\n" + (sub or " ")
                if not enabled:
                    bg, fg = "#141b2d", "#4a5468"
                else:
                    bg = ACCENT if selected else ACCENT2
                    fg = "white" if selected else MUTED
                btn = make_button(
                    row_frame, text,
                    lambda k=key: self._select_format(k),
                    bg=bg, fg=fg, abg=ACCENT, disabled=not enabled,
                    font=(FONT, self._fs(11), "bold" if selected else "normal"),
                    padx=10, pady=8
                )
                btn.grid(row=0, column=c, sticky="nsew", padx=3)

    def _select_format(self, val):
        if self._plan and not self._plan.option(val)[0]:
            return
        self.format_var.set(val)
        self._build_format_buttons()
        # URL은 건드리지 않음

    # ── 영상 정보 읽기 (화질/음질/용량) ───────────────────────────────────────────
    def _url_changed(self, e=None):
        url = self.url_entry.get().strip()
        if self._info_after:
            self.after_cancel(self._info_after)
            self._info_after = None
        if not url or url == PLACEHOLDER or not url.lower().startswith("http"):
            self._reset_plan()
            return
        if url == self._last_info_url:
            return
        self._info_after = self.after(700, self._fetch_plan_async, url)

    def _reset_plan(self):
        self._info_token += 1
        self._last_info_url = None
        had = self._plan is not None
        self._plan = None
        self._info_label.config(text="")
        if had:
            self._build_format_buttons()

    def _fetch_plan_async(self, url):
        self._info_after = None
        self._last_info_url = url
        self._info_token += 1
        token = self._info_token
        self._info_label.config(text="영상 정보 확인 중...", fg=MUTED)

        def work():
            plan, title, err = None, "", None
            try:
                info = fetch_info(url)
                title = (info or {}).get("title") or ""
                plan = plan_from_info(info)
            except Exception as ex:
                err = str(ex)
            self.after(0, self._apply_plan, token, plan, title, err)

        threading.Thread(target=work, daemon=True).start()

    def _apply_plan(self, token, plan, title, err):
        if token != self._info_token:      # 그 사이 URL이 바뀜
            return
        self._plan = plan
        if plan:
            dur = _fmt_duration(plan.duration)
            short = title if len(title) <= 60 else title[:57] + "..."
            self._info_label.config(
                text=short + (f"   ·   {dur}" if dur else ""), fg=TEXT
            )
            if not plan.option(self.format_var.get())[0]:
                self.format_var.set("video_best")   # 선택했던 화질이 이 영상에는 없음
        else:
            self._info_label.config(
                text="영상 정보를 읽지 못했습니다. 그래도 다운로드는 시도할 수 있습니다."
                if err else "재생목록이거나 화질 정보를 알 수 없습니다.",
                fg=MUTED
            )
        self._build_format_buttons()

    # ── URL placeholder ───────────────────────────────────────────────────────
    def _clear_placeholder(self):
        """안내 문구가 진짜 글자처럼 남아 있으면 지운다 (입력/붙여넣기 전에 호출)"""
        if self.url_entry.get() == PLACEHOLDER:
            self.url_entry.delete(0, "end")
        self.url_entry.config(fg=TEXT)

    def _url_focus_in(self, e):
        self._clear_placeholder()

    def _url_focus_out(self, e):
        if not self.url_entry.get().strip():
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, PLACEHOLDER)
            self.url_entry.config(fg=MUTED)

    def _url_key(self, e):
        self._clear_placeholder()

    def _url_paste(self, e=None):
        self._clear_placeholder()           # 안내 문구 뒤에 붙지 않고 문구를 대체
        self.after(80, self._url_changed)

    def _reset_url_entry(self):
        """입력칸 비우기. 커서가 입력칸에 있으면 비워 두고(바로 붙여넣기 가능), 아니면 안내 문구 표시"""
        self.url_entry.delete(0, "end")
        if self.focus_get() is self.url_entry:
            self.url_entry.config(fg=TEXT)
        else:
            self.url_entry.insert(0, PLACEHOLDER)
            self.url_entry.config(fg=MUTED)

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

        item = {
            "url": url, "fmt": self.format_var.get(), "save_dir": self.save_dir.get(),
            "status": "waiting", "error": "",
        }
        self.queue_items.append(item)
        self.dl_queue.put(item)
        self._refresh_queue_ui()

        # URL 초기화
        self._reset_url_entry()
        self._reset_plan()

    def _waiting_count(self):
        return sum(1 for i in list(self.queue_items) if i["status"] == "waiting")

    def _retry_item(self, item):
        """실패한 항목을 대기열 맨 뒤에 다시 넣는다"""
        if item not in self.queue_items or item["status"] != "failed":
            return
        item["status"], item["error"], item["cancelled"] = "waiting", "", False
        self.dl_queue.put(item)
        self._refresh_queue_ui()
        if not self._is_downloading:
            self.status_var.set("다시 시도합니다...")

    def _remove_item(self, item):
        """대기 중이거나 실패한 항목을 대기열에서 지운다 (다운로드 중인 항목은 지울 수 없음)"""
        if item not in self.queue_items or item["status"] == "running":
            return
        item["cancelled"] = True             # 이미 줄 서 있다면 워커가 건너뛴다
        self.queue_items.remove(item)
        self._refresh_queue_ui()

    @staticmethod
    def _short_error(msg, limit=90):
        line = (msg or "").strip().splitlines()[0] if msg else ""
        line = line.replace("ERROR: ", "", 1)
        return line if len(line) <= limit else line[:limit - 3] + "..."

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

        for i, item in enumerate(self.queue_items):
            failed = item["status"] == "failed"
            item_frame = tk.Frame(self.queue_list_frame, bg=ACCENT2, pady=6, padx=10)
            item_frame.pack(fill="x", pady=2)

            row = tk.Frame(item_frame, bg=ACCENT2)
            row.pack(fill="x")

            tk.Label(
                row, text=f"{i+1}.",
                font=(FONT, self._fs(10), "bold"),
                bg=ACCENT2, fg=ACCENT, width=3, anchor="w"
            ).pack(side="left")

            url = item["url"]
            short_url = url if len(url) <= 40 else url[:37] + "..."
            tk.Label(
                row, text=short_url,
                font=(FONT, self._fs(9)),
                bg=ACCENT2, fg=TEXT, anchor="w"
            ).pack(side="left", fill="x", expand=True)

            # 오른쪽 끝부터: 버튼들, 상태, 형식
            btn_font = (FONT, self._fs(9), "bold")
            if item["status"] != "running":
                make_button(
                    row, "삭제", lambda it=item: self._remove_item(it),
                    bg="#5b2a3b", fg="white", abg=ACCENT, font=btn_font, padx=8, pady=2
                ).pack(side="right", padx=(4, 0))
            if failed:
                make_button(
                    row, "재시도", lambda it=item: self._retry_item(it),
                    bg=SUCCESS, fg="#1a1a2e", abg="#3aa88a", afg="#1a1a2e",
                    font=btn_font, padx=8, pady=2
                ).pack(side="right", padx=(4, 0))

            status_text, status_color = {
                "running": ("다운로드 중", SUCCESS),
                "failed": ("실패", "#ff6b6b"),
            }.get(item["status"], ("", MUTED))
            if status_text:
                tk.Label(
                    row, text=status_text, font=(FONT, self._fs(9), "bold"),
                    bg=ACCENT2, fg=status_color
                ).pack(side="right", padx=6)

            tk.Label(
                row, text=FORMAT_LABELS.get(item["fmt"], item["fmt"]),
                font=(FONT, self._fs(9)), bg=ACCENT2, fg=MUTED
            ).pack(side="right", padx=6)

            if failed and item["error"]:
                tk.Label(
                    item_frame, text="오류: " + self._short_error(item["error"]),
                    font=(FONT, self._fs(8)), bg=ACCENT2, fg="#ff9b9b",
                    anchor="w", justify="left", wraplength=int(560 * self._scale)
                ).pack(fill="x", padx=(28, 0), pady=(3, 0))

    # ── worker thread ─────────────────────────────────────────────────────────
    def _start_worker(self):
        def worker():
            while True:
                item = self.dl_queue.get()
                if item.get("cancelled") or item["status"] != "waiting":
                    self.dl_queue.task_done()       # 그 사이 삭제된 항목
                    continue
                self.after(0, self._on_start, item)
                error = None
                try:
                    self._download(item["url"], item["fmt"], item["save_dir"])
                except Exception as exc:
                    error = str(exc) or exc.__class__.__name__
                    if not is_permanent_error(exc):
                        error += f"\n(자동으로 {MAX_TRIES}번 시도했지만 실패했습니다)"
                finally:
                    self.dl_queue.task_done()
                    self.after(0, self._on_finish_one, item, error)

        threading.Thread(target=worker, daemon=True).start()

    def _on_start(self, item):
        item["status"] = "running"
        self._is_downloading = True
        self.progress_var.set(0)
        self.pct_var.set("0%")
        self.status_var.set("다운로드 준비 중...")
        self._refresh_queue_ui()

    def _on_finish_one(self, item, error):
        if error is None:
            if item in self.queue_items:
                self.queue_items.remove(item)          # 성공한 항목은 목록에서 뺀다
        elif item in self.queue_items:
            item["status"], item["error"] = "failed", error  # 실패한 항목은 남겨 둔다
        self._refresh_queue_ui()

        waiting = self._waiting_count()
        if waiting == 0:
            self._is_downloading = False
            failed = sum(1 for i in self.queue_items if i["status"] == "failed")
            if failed:
                self.progress_var.set(0)
                self.pct_var.set("일부 실패")
                self.status_var.set(f"실패 {failed}개 — 대기열에서 재시도하거나 삭제할 수 있습니다")
            else:
                self.progress_var.set(100)
                self.pct_var.set("완료!")
                self.status_var.set(f"저장 완료 → {item['save_dir']}")
        else:
            self.status_var.set(f"다음 항목 준비 중... (남은 항목: {waiting}개)")

    # ── download ──────────────────────────────────────────────────────────────
    def _download(self, url, fmt, save_dir):
        ffmpeg = get_ffmpeg_path()
        common = {
            "outtmpl": os.path.join(save_dir, "%(title)s.%(ext)s"),
            "ffmpeg_location": ffmpeg,
            "progress_hooks": [self._progress_hook],
            "retries": 10,
            "fragment_retries": 10,
            "noplaylist": True,
        }
        js_runtimes = get_js_runtimes()
        if js_runtimes:
            common["js_runtimes"] = js_runtimes

        # 다운로드 중 오류(네트워크 끊김, 403 등)가 나면 자동으로 다시 시도한다.
        # 새 주소를 받아 이어받기(받던 부분부터)로 재개하고, 3·5번째 시도에서는 HLS 형식을 먼저 시도
        for attempt in range(MAX_TRIES):
            hls_first = attempt >= 2 and attempt % 2 == 0
            try:
                self.after(0, self.status_var.set, "영상 정보 확인 중...")
                info = fetch_info(url)
                plan = plan_from_info(info)
                if plan:
                    sel, extra = plan.download_spec(fmt, hls_first)
                else:   # 재생목록 등 정보를 못 읽은 경우는 예전 방식으로
                    sel, extra = legacy_spec(fmt, hls_first)
                with yt_dlp.YoutubeDL({**common, "format": sel, **extra}) as ydl:
                    if plan:
                        ydl.process_ie_result(info, download=True)   # 위에서 읽은 정보를 재사용
                    else:
                        ydl.download([url])
                return
            except Exception as e:
                if attempt == MAX_TRIES - 1 or is_permanent_error(e):
                    raise
                self.after(0, self.status_var.set,
                           f"오류가 발생해 다시 시도하는 중... ({attempt + 2}/{MAX_TRIES})")
                time.sleep(min(3 * (attempt + 1), 10))

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
            q_remaining = self._waiting_count()
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
