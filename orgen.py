import ctypes
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, QRunnable, QSize, Qt, QThread, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QProgressDialog, QPushButton, QSizePolicy, QStyle, QSystemTrayIcon,
    QTabWidget, QVBoxLayout, QWidget,
)
from steam_browser import SteamBrowser, combo_qss

# ---------------------------------------------------------------- paths & config

APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
BIN_DIR = APP_DIR / "bin"
MPV_EXE = BIN_DIR / "mpv.exe"
REPKG_EXE = BIN_DIR / "RePKG.exe"
STEAMCMD_DIR = BIN_DIR / "steamcmd"
STEAMCMD_EXE = STEAMCMD_DIR / "steamcmd.exe"
STEAMCMD_WORKSHOP = STEAMCMD_DIR / "steamapps" / "workshop" / "content" / "431960"
STEAMCMD_STAGING = STEAMCMD_DIR / "steamapps" / "workshop" / "downloads" / "431960"
CONFIG_PATH = APP_DIR / "orgen_config.json"
DEBUG_LOG = APP_DIR / "orgen_debug.log"
MPV_LOG = APP_DIR / "mpv.log"
# Сюда RePKG распаковывает scene.pkg; повторно сцена не распаковывается.
EXTRACTED_DIR = APP_DIR / "projects" / "myprojects"

WORKSHOP_APP_ID = "431960"
STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
# Прямая ссылка на сборку mpv x86_64 (.7z или .zip). Если пусто — положите mpv.exe в bin/ вручную.
MPV_URL = ""
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OrgenWallpaper/1.0"

VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".avi", ".gif")
PREVIEW_NAMES = ("preview.jpg", "preview.png", "preview.gif", "preview.jpeg")
NO_VIDEO_MARK = ".no_video"
THUMB = QSize(220, 124)
PROGRESS_RE = re.compile(r"progress:\s*([0-9]+(?:\.[0-9]+)?)")

# Режимы подгонки видео под экран.
#   fill    — заполнить экран, лишнее по краям обрезается (без полос)
#   fit     — вписать целиком, по краям могут быть полосы
#   stretch — растянуть на весь экран (искажает пропорции)
FIT_MODES = {
    "fill": "Заполнить (обрезать)",
    "fit": "Вписать (полосы)",
    "stretch": "Растянуть",
}
DEFAULT_FIT = "fill"

DARK_QSS = """
QMainWindow, QWidget { background: #13161c; color: #cfd6e4; font-size: 13px; }
QLineEdit, QComboBox {
    background: #1b1f27; border: 1px solid #2a2f3a; border-radius: 8px; padding: 6px 10px;
    color: #e6ebf5; selection-background-color: #3d8bff;
}
QLineEdit:focus, QComboBox:focus { border: 1px solid #3d8bff; }
QComboBox QAbstractItemView { background: #1b1f27; color: #e6ebf5; selection-background-color: #3d8bff; }
QPushButton {
    background: #1f2430; border: 1px solid #2a2f3a; border-radius: 8px; padding: 7px 14px; color: #e6ebf5;
}
QPushButton:hover { background: #262c3a; border-color: #3d8bff; }
QPushButton:pressed { background: #1a1f2a; }
QPushButton:disabled { color: #5b6275; }
QTabWidget::pane { border: none; }
QTabBar::tab {
    background: #1b1f27; color: #8b93a7; padding: 8px 18px; border-top-left-radius: 8px;
    border-top-right-radius: 8px; margin-right: 4px;
}
QTabBar::tab:selected { background: #262c3a; color: #e6ebf5; }
QListWidget { background: #0f1218; border: 1px solid #2a2f3a; border-radius: 10px; }
QListWidget::item { border-radius: 8px; padding: 4px; }
QListWidget::item:selected { background: #22304a; }
QMessageBox, QInputDialog, QDialog { background: #13161c; }
QMenu {
    background: #1b1f27; border: 1px solid #2a2f3a; border-radius: 10px;
    padding: 6px; color: #e6ebf5;
}
QMenu::item { padding: 6px 22px 6px 10px; border-radius: 6px; }
QMenu::item:selected { background: #22304a; }

/* Панель загрузки: всё внутри прозрачное, чтобы не было чужих прямоугольников */
#dlPanel { background: #1b1f27; border: 1px solid #2a2f3a; border-radius: 12px; }
#dlPanel QWidget { background: transparent; }
#dlTitle { font-size: 14px; font-weight: 600; color: #e6ebf5; }
#dlDetail { color: #8b93a7; }
#dlPct { font-size: 18px; font-weight: 700; color: #e6ebf5; }
#dlPanel QProgressBar { background: #0f1218; border: none; border-radius: 5px; }
#dlPanel QProgressBar::chunk {
    border-radius: 5px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #3d8bff, stop:1 #7c5cff);
}
"""


def debug(msg: str) -> None:
    """Пишет диагностику в orgen_debug.log рядом с программой."""
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def load_cfg() -> dict:
    cfg = {
        "steam_login": "",
        "steam_password": "",
        "steam_root": "",
        "last_wallpaper": "",
        "fit_mode": DEFAULT_FIT,
        "steam_api_key": "",
    }
    if CONFIG_PATH.is_file():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    if cfg.get("fit_mode") not in FIT_MODES:
        cfg["fit_mode"] = DEFAULT_FIT
    return cfg


def save_cfg(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def workshop_dirs(cfg: dict) -> list[Path]:
    dirs = [STEAMCMD_WORKSHOP]
    if cfg.get("steam_root"):
        dirs.append(Path(cfg["steam_root"]) / "steamapps" / "workshop" / "content" / WORKSHOP_APP_ID)
    return [d for d in dirs if d.is_dir()]


def item_id_from_url(text: str) -> str | None:
    text = text.strip()
    if text.isdigit():
        return text
    ids = urllib.parse.parse_qs(urllib.parse.urlparse(text).query).get("id")
    return ids[0] if ids and ids[0].isdigit() else None


def dir_size(p: Path) -> int:
    total = 0
    if p.is_dir():
        for f in p.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                pass
    return total


def kill_stray_processes() -> None:
    """Убивает дочерние steamcmd.exe и RePKG.exe, если они остались после выхода."""
    if sys.platform != "win32":
        return
    for name in ("steamcmd.exe", "RePKG.exe"):
        subprocess.run(
            ["taskkill", "/F", "/IM", name, "/T"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


# ---------------------------------------------------------------- download helpers

def download(url: str, dest: Path, progress_cb) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                progress_cb(done * 100 // total)


def extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix.lower() == ".7z":
        try:
            import py7zr
        except ImportError as e:
            raise RuntimeError("Для .7z нужен пакет py7zr: pip install py7zr") from e
        with py7zr.SevenZipFile(archive, "r") as z:
            z.extractall(dest)
    else:
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)


class BootstrapThread(QThread):
    progress = Signal(int, str)
    ok = Signal(str)     # список недостающих компонентов (может быть пустым)
    fail = Signal(str)

    def run(self):
        try:
            BIN_DIR.mkdir(exist_ok=True)
            if not STEAMCMD_EXE.is_file():
                self._fetch_steamcmd()
            if not MPV_EXE.is_file() and MPV_URL:
                self._fetch_mpv()
            missing = []
            if not MPV_EXE.is_file():
                missing.append("mpv.exe (положите в bin/ или укажите MPV_URL)")
            if not REPKG_EXE.is_file():
                missing.append("RePKG.exe (положите в bin/)")
            self.ok.emit(", ".join(missing))
        except Exception as e:
            self.fail.emit(f"Не удалось подготовить компоненты: {e}")

    def _fetch_steamcmd(self):
        tmp = Path(tempfile.gettempdir()) / "orgen_steamcmd.zip"
        self.progress.emit(0, "Подготовка компонентов...")
        download(STEAMCMD_URL, tmp, lambda p: self.progress.emit(p // 2, "Подготовка компонентов..."))
        self.progress.emit(50, "Распаковка...")
        extract(tmp, STEAMCMD_DIR)
        tmp.unlink(missing_ok=True)

    def _fetch_mpv(self):
        suffix = ".7z" if MPV_URL.lower().endswith(".7z") else ".zip"
        tmp = Path(tempfile.gettempdir()) / f"orgen_mpv{suffix}"
        unpack = Path(tempfile.gettempdir()) / "orgen_mpv_unpack"
        shutil.rmtree(unpack, ignore_errors=True)
        self.progress.emit(50, "Подготовка компонентов...")
        download(MPV_URL, tmp, lambda p: self.progress.emit(50 + p // 2, "Подготовка компонентов..."))
        self.progress.emit(100, "Распаковка...")
        extract(tmp, unpack)
        exe = next(unpack.rglob("mpv.exe"), None)
        if exe is None:
            raise RuntimeError("mpv.exe не найден в архиве")
        for f in exe.parent.iterdir():
            if f.is_file():
                shutil.copy2(f, BIN_DIR / f.name)
        shutil.rmtree(unpack, ignore_errors=True)
        tmp.unlink(missing_ok=True)


DOWNLOAD_TIMEOUT_SEC = 180
DOWNLOAD_MAX_ATTEMPTS = 3
NO_RETRY_MARKERS = (
    "invalid password", "password is incorrect", "no subscription",
    "doesn't own", "does not own",
)


def classify_steam_error(output: str) -> bool:
    """True, если повтор имеет смысл (сеть, таймаут, временный сбой)."""
    low = output.lower()
    return not any(m in low for m in NO_RETRY_MARKERS)


class SteamCmdThread(QThread):
    """Скачивает предмет Мастерской без видимого окна, с таймаутом и повторами (1/3, 2/3, 3/3)."""

    progress = Signal(int)          # процент загрузки
    detail = Signal(str)            # текстовый статус (номер попытки)
    need_input = Signal(str, bool)  # текст запроса, секретное ли поле (пароль)
    ok = Signal(str)
    fail = Signal(str)

    CANCEL_TEXT = "Загрузка отменена."
    PROMPT_KEYS = ("password", "steam guard", "two-factor", "code", "login", "name")

    def __init__(self, login: str, password: str, item_id: str):
        super().__init__()
        self.login = login
        self.password = password
        self.item_id = item_id
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._cancelled = False
        self._waiting_input = False
        self._sent_password = False
        self._last_activity = 0.0

    def send(self, text: str) -> None:
        """Отправляет ответ (логин, пароль или код) в загрузчик."""
        with self._lock:
            self._waiting_input = False
            if self._proc and self._proc.stdin:
                try:
                    self._proc.stdin.write(text + "\n")
                    self._proc.stdin.flush()
                except OSError:
                    pass

    def cancel(self) -> None:
        self._cancelled = True
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()

    def run(self):
        if not self.login or not self.password:
            self.fail.emit("В orgen_config.json не заданы steam_login и steam_password.")
            return

        last_text = ""
        attempt = 0
        for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
            if self._cancelled:
                self.fail.emit(self.CANCEL_TEXT)
                return
            self.detail.emit(f"Попытка {attempt}/{DOWNLOAD_MAX_ATTEMPTS}...")
            ok, text, code = self._attempt_once()
            last_text = text
            debug(f"attempt {attempt}/{DOWNLOAD_MAX_ATTEMPTS} code={code} tail={text[-300:]!r}")
            if self._cancelled:
                self.fail.emit(self.CANCEL_TEXT)
                return
            if ok:
                self.progress.emit(100)
                self.ok.emit(self.item_id)
                return
            if not classify_steam_error(text) or attempt == DOWNLOAD_MAX_ATTEMPTS:
                break
            time.sleep(3)

        self.fail.emit(
            f"Не удалось скачать после {attempt} попыток.\n"
            "Проверь логин, пароль, Steam Guard и владение Wallpaper Engine.\n\n"
            f"Последняя ошибка:\n{last_text.strip()[-400:]}"
        )

    @classmethod
    def _is_prompt(cls, line: str) -> bool:
        s = line.strip().lower()
        return s.endswith((":", "?")) and any(k in s for k in cls.PROMPT_KEYS)

    def _answer_prompt(self, line: str) -> None:
        low = line.lower()

        if "login" in low or ("name" in low and "steam" not in low and "password" not in low):
            self.send(self.login)
            return

        if "password" in low and self.password and not self._sent_password:
            self._sent_password = True
            self.send(self.password)
            return

        # Код Steam Guard или повторный запрос пароля: спрашиваем пользователя.
        self._waiting_input = True
        self.need_input.emit(line.strip(), "password" in low)

    def _watchdog(self, timed_out: list[bool]) -> None:
        while self._proc and self._proc.poll() is None:
            time.sleep(1)
            if self._waiting_input:
                self._last_activity = time.time()   # ждём человека, таймаут не тикает
            elif time.time() - self._last_activity > DOWNLOAD_TIMEOUT_SEC:
                timed_out[0] = True
                self._proc.kill()
                return

    def _attempt_once(self) -> tuple[bool, str, int]:
        cmd = [
            str(STEAMCMD_EXE),
            "+login", self.login,
            "+workshop_download_item", WORKSHOP_APP_ID, self.item_id,
            "+quit",
        ]
        started = time.time()
        self._sent_password = False
        self._waiting_input = False
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=str(STEAMCMD_DIR),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError as e:
            return False, f"Не удалось запустить загрузчик: {e}", -1

        self._last_activity = time.time()
        timed_out = [False]
        threading.Thread(target=self._watchdog, args=(timed_out,), daemon=True).start()

        lines: list[str] = []
        line = ""
        last_pct = -1
        while True:
            ch = self._proc.stdout.read(1)
            if not ch:
                break
            self._last_activity = time.time()
            c = ch.decode("utf-8", errors="replace")
            if c in "\r\n":
                if line.strip():
                    lines.append(line.strip())
                    debug(f"steamcmd: {line.strip()[:200]}")
                line = ""
                continue

            line += c
            if len(line) > 300:
                line = line[-150:]

            m = PROGRESS_RE.search(line)
            if m:
                pct = min(100, int(float(m.group(1))))
                if pct != last_pct:
                    last_pct = pct
                    self.progress.emit(pct)

            if self._is_prompt(line):
                debug(f"steamcmd prompt: {line.strip()[:120]!r}")
                self._answer_prompt(line)
                line = ""

        code = self._proc.wait()
        text = "\n".join(lines) + ("\ntimeout" if timed_out[0] else "")
        item_dir = STEAMCMD_WORKSHOP / self.item_id
        fresh = item_dir.is_dir() and any(
            p.is_file() and p.stat().st_mtime >= started for p in item_dir.iterdir()
        )
        return code == 0 and fresh, text, code


# ---------------------------------------------------------------- RePKG (pkg -> assets)

def find_pkg(folder: Path) -> Path | None:
    for name in ("scene.pkg", "gif.pkg", "video.pkg"):
        p = folder / name
        if p.is_file():
            return p
    return next(iter(sorted(folder.glob("*.pkg"))), None)


def extracted_dir_for(wallpaper_id: str) -> Path:
    return EXTRACTED_DIR / wallpaper_id


def _largest(extracted: Path, exts: tuple[str, ...]) -> Path | None:
    def usable(p: Path) -> bool:
        return p.is_file() and not p.name.lower().startswith("preview")

    candidates: list[Path] = []
    materials = extracted / "materials"
    if materials.is_dir():
        for ext in exts:
            candidates += [p for p in sorted(materials.glob(f"*{ext}")) if usable(p)]
    if not candidates:
        for ext in exts:
            candidates += [p for p in sorted(extracted.rglob(f"*{ext}")) if usable(p)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def find_scene_video(extracted: Path) -> Path | None:
    """Видео (предпочтительно) или гифка, которые RePKG вытащил из сцены."""
    return _largest(extracted, (".mp4", ".webm")) or _largest(extracted, (".gif",))


def find_preview_gif(folder: Path) -> Path | None:
    """Анимированное превью сцены. Используется, если в сцене нет видео или гифки."""
    p = folder / "preview.gif"
    return p if p.is_file() else None


STILL_EXTS = (".jpg", ".jpeg", ".png")


def find_scene_still(extracted: Path) -> Path | None:
    """Самая крупная картинка сцены (обычно это основной арт)."""
    if not extracted.is_dir():
        return None
    candidates = [
        p for p in extracted.rglob("*")
        if p.is_file() and p.suffix.lower() in STILL_EXTS and not p.name.lower().startswith("preview")
    ]
    return max(candidates, key=lambda p: p.stat().st_size) if candidates else None


def find_scene_fallback(extracted: Path, folder: Path) -> Path | None:
    """Чем заменить сцену без видео: анимированное превью, картинка сцены или статичное превью."""
    gif = find_preview_gif(folder)
    if gif is not None:
        return gif
    still = find_scene_still(extracted)
    if still is not None:
        return still
    for name in PREVIEW_NAMES:
        p = folder / name
        if p.is_file() and p.suffix.lower() in STILL_EXTS:
            return p
    return None


def explain_no_video(out_dir: Path) -> str:
    files = [p for p in out_dir.rglob("*") if p.is_file()]
    kinds: dict[str, int] = {}
    for p in files:
        ext = p.suffix.lower() or "(без расширения)"
        kinds[ext] = kinds.get(ext, 0) + 1
    top = ", ".join(f"{e}: {n}" for e, n in sorted(kinds.items(), key=lambda x: -x[1])[:6])
    msg = (
        "Это интерактивная сцена Wallpaper Engine (scene.json, шейдеры, частицы), "
        "а не готовое видео. mpv такие сцены проиграть не может."
    )
    if kinds.get(".tex"):
        msg += "\n\nВ пакете есть текстуры (.tex), но готового видео среди них нет."
    msg += f"\n\nРаспаковано файлов: {len(files)} ({top or 'пусто'})."
    return msg


class PkgExtractThread(QThread):
    ok = Signal(Path)   # папка с распакованной сценой
    fail = Signal(str)

    def __init__(self, pkg_path: Path, wallpaper_id: str):
        super().__init__()
        self.pkg_path = pkg_path
        self.wallpaper_id = wallpaper_id

    def run(self):
        if not REPKG_EXE.is_file():
            self.fail.emit("RePKG.exe не найден в папке bin.")
            return

        out_dir = extracted_dir_for(self.wallpaper_id)
        out_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            str(REPKG_EXE), "extract",
            "-o", str(out_dir),
            "--overwrite",
            str(self.pkg_path),
        ]
        debug(f"RePKG: {cmd}")
        try:
            r = subprocess.run(
                cmd, cwd=str(BIN_DIR), capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=1800,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            self.fail.emit(f"Не удалось выполнить RePKG: {e}")
            return

        if r.returncode != 0:
            text = (r.stdout + "\n" + r.stderr).strip()
            self.fail.emit(f"RePKG завершился с ошибкой (код {r.returncode}):\n{text[-800:]}")
            return

        if find_scene_video(out_dir) is None:
            (out_dir / NO_VIDEO_MARK).write_text("no video", encoding="utf-8")
            if find_scene_fallback(out_dir, self.pkg_path.parent) is None:
                self.fail.emit(explain_no_video(out_dir))
                return

        self.ok.emit(out_dir)


# ---------------------------------------------------------------- workshop parsing

@dataclass
class Wallpaper:
    wallpaper_id: str
    title: str
    type: str
    folder: Path
    video: Path | None
    preview: Path | None
    pkg: Path | None = None
    no_video: bool = False

    @property
    def playable(self) -> bool:
        return self.video is not None or (self.pkg is not None and not self.no_video)


def parse_wallpaper(folder: Path) -> Wallpaper:
    data: dict = {}
    pj = folder / "project.json"
    if pj.is_file():
        try:
            loaded = json.loads(pj.read_text(encoding="utf-8-sig"))
            data = loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, OSError):
            data = {}

    wtype = str(data.get("type", "")).lower()
    title = str(data.get("title") or folder.name)

    video = None
    file_rel = data.get("file")
    if isinstance(file_rel, str) and (folder / file_rel).is_file() \
            and (folder / file_rel).suffix.lower() in VIDEO_EXTS:
        video = folder / file_rel
    if video is None:
        for p in sorted(folder.iterdir()):
            if (p.is_file() and p.suffix.lower() in VIDEO_EXTS
                    and not p.name.lower().startswith("preview")):
                video = p
                break

    # Уже распакованная сцена (есть scene.json и видео внутри папки).
    if video is None and (folder / "scene.json").is_file():
        video = find_scene_video(folder) or find_scene_fallback(folder, folder)

    pkg = None
    no_video = False
    if video is None:
        cached = extracted_dir_for(folder.name)
        if cached.is_dir():
            cached_video = find_scene_video(cached)
            if cached_video is not None:
                video = cached_video
            elif (cached / NO_VIDEO_MARK).exists():
                video = find_scene_fallback(cached, folder)
                no_video = video is None
        if video is None and not no_video:
            pkg = find_pkg(folder)

    if not wtype:
        if video is not None:
            wtype = "video"
        elif pkg is not None or no_video:
            wtype = "scene"
        elif (folder / "index.html").is_file():
            wtype = "web"

    preview = None
    preview_rel = data.get("preview")
    if isinstance(preview_rel, str) and (folder / preview_rel).is_file():
        preview = folder / preview_rel
    if preview is None:
        for name in PREVIEW_NAMES:
            if (folder / name).is_file():
                preview = folder / name
                break

    return Wallpaper(folder.name, title, wtype or "unknown", folder, video, preview, pkg, no_video)


def scan_all(dirs: list[Path]) -> list[Wallpaper]:
    by_id: dict[str, Wallpaper] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        for folder in sorted(d.iterdir()):
            if folder.is_dir():
                by_id.setdefault(folder.name, parse_wallpaper(folder))
    return list(by_id.values())


# ---------------------------------------------------------------- screen & WorkerW + mpv

if sys.platform == "win32":
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OpenWallpaper.App")
    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowExW.restype = wintypes.HWND
    user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

WM_SPAWN_WORKER = 0x052C
SPI_SETDESKWALLPAPER = 0x0014
SM_CXSCREEN, SM_CYSCREEN = 0, 1
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


def screen_info() -> dict:
    if sys.platform != "win32":
        return {"primary": (1920, 1080), "virtual": (1920, 1080), "virtual_origin": (0, 0)}
    gm = user32.GetSystemMetrics
    return {
        "primary": (gm(SM_CXSCREEN), gm(SM_CYSCREEN)),
        "virtual": (gm(SM_CXVIRTUALSCREEN), gm(SM_CYVIRTUALSCREEN)),
        "virtual_origin": (gm(SM_XVIRTUALSCREEN), gm(SM_YVIRTUALSCREEN)),
    }


def fit_args(mode: str) -> list[str]:
    if mode == "stretch":
        return ["--keepaspect=no"]
    if mode == "fit":
        return ["--keepaspect=yes"]
    return ["--keepaspect=yes", "--panscan=1.0"]


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def find_workerw() -> tuple[int, str]:
    progman = user32.FindWindowW("Progman", None)
    if not progman:
        raise RuntimeError("Окно Progman не найдено")

    result = ctypes.c_size_t()
    user32.SendMessageTimeoutW(
        progman, WM_SPAWN_WORKER, 0xD, 0x1, 0, 1000, ctypes.byref(result)
    )

    found = [0]

    def enum_proc(hwnd, _lparam):
        if user32.FindWindowExW(hwnd, 0, "SHELLDLL_DefView", None):
            found[0] = user32.FindWindowExW(0, hwnd, "WorkerW", None) or 0
        return True

    callback = WNDENUMPROC(enum_proc)
    user32.EnumWindows(callback, 0)

    hwnd = found[0]
    if not hwnd:
        hwnd = user32.FindWindowExW(progman, 0, "WorkerW", None) or 0

    if not hwnd:
        raise RuntimeError(
            "WorkerW не создан. Перезапусти explorer.exe или проверь, не включён ли "
            "другой менеджер обоев (Lively, Wallpaper Engine)."
        )
    return hwnd, window_class(hwnd)


def restore_system_wallpaper() -> None:
    if sys.platform == "win32":
        ctypes.windll.user32.SystemParametersInfoW(SPI_SETDESKWALLPAPER, 0, None, 3)


class WallpaperEngine:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.current: Path | None = None

    def _kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        self.proc = None

    def play(self, video: Path, fit_mode: str = DEFAULT_FIT) -> None:
        self._kill()
        hwnd, cls = find_workerw()
        scr = screen_info()
        debug(f"play: video={video}")
        debug(f"play: screen primary={scr['primary']} virtual={scr['virtual']} "
              f"origin={scr['virtual_origin']} fit={fit_mode}")
        debug(f"play: target hwnd={hwnd} class={cls!r}")
        if cls != "WorkerW":
            debug("play: WARNING — окно не WorkerW, видео может оказаться под иконками")

        if video.suffix.lower() in STILL_EXTS:
            loop_args = ["--image-display-duration=inf", "--keep-open=yes"]
        else:
            loop_args = ["--loop=inf"]

        args = [
            str(MPV_EXE), f"--wid={hwnd}", *loop_args, "--no-border", "--no-osc",
            "--no-input-default-bindings", "--hwdec=auto", "--mute=yes",
            "--background=color", "--background-color=#000000",
            *fit_args(fit_mode),
            f"--log-file={MPV_LOG}", "--msg-level=all=info", str(video),
        ]
        debug(f"play: cmd={args}")
        self.proc = subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(1.5)
        code = self.proc.poll()
        if code is not None:
            self.proc = None
            debug(f"play: mpv exited early with code {code}")
            raise RuntimeError(f"mpv завершился сразу (код {code}). Смотри mpv.log в папке приложения.")
        self.current = video
        debug(f"play: mpv running, pid={self.proc.pid}")

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, restore: bool = True) -> None:
        self._kill()
        self.current = None
        if restore:
            restore_system_wallpaper()


# ---------------------------------------------------------------- thumbnails

class ThumbSignals(QObject):
    loaded = Signal(str, QImage)


class ThumbTask(QRunnable):
    def __init__(self, key: str, path: str, signals: ThumbSignals):
        super().__init__()
        self.key = key
        self.path = path
        self.signals = signals

    def run(self):
        img = QImage(self.path)
        if not img.isNull():
            img = img.scaled(
                THUMB,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
        self.signals.loaded.emit(self.key, img)


# ---------------------------------------------------------------- download panel (UI)

class SpinnerWidget(QWidget):
    def __init__(self, size: int = 34, parent=None):
        super().__init__(parent)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(size, size)

    def start(self) -> None:
        self._timer.start(16)

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        self._angle = (self._angle + 6) % 360
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(3, 3, self.width() - 6, self.height() - 6)

        p.setPen(QPen(QColor("#2a2f3a"), 3))
        p.drawEllipse(rect)

        p.setPen(QPen(QColor("#3d8bff"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(rect, int(-self._angle * 16), int(-100 * 16))
        p.end()


class DownloadPanel(QFrame):
    cancel_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("dlPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(96)
        self._has_pct = False

        self.spinner = SpinnerWidget(34)

        self.title = QLabel("Загрузка")
        self.title.setObjectName("dlTitle")
        self.detail = QLabel("")
        self.detail.setObjectName("dlDetail")

        self.pct = QLabel("…")
        self.pct.setObjectName("dlPct")
        self.pct.setMinimumWidth(72)
        self.pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(10)

        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.setFixedWidth(104)
        self.cancel_btn.clicked.connect(self.cancel_requested)

        text_col = QVBoxLayout()
        text_col.setSpacing(3)
        text_col.addWidget(self.title)
        text_col.addWidget(self.detail)

        top = QHBoxLayout()
        top.setSpacing(14)
        top.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addLayout(text_col, 1)
        top.addWidget(self.pct, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.cancel_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(12)
        layout.addLayout(top)
        layout.addWidget(self.bar)
        self.hide()

    @property
    def has_percent(self) -> bool:
        return self._has_pct

    def begin(self, item_id: str) -> None:
        self._has_pct = False
        self.title.setText(f"Загружаю предмет {item_id}")
        self.detail.setText("Подключение к Steam...")
        self.pct.setText("…")
        self.bar.setRange(0, 0)
        self.spinner.start()
        self.show()

    def set_detail(self, text: str) -> None:
        self.detail.setText(text)

    def set_progress(self, value: int) -> None:
        value = max(0, min(100, value))
        if self.bar.maximum() == 0:
            self.bar.setRange(0, 100)
        self._has_pct = True
        self.bar.setValue(value)
        self.pct.setText(f"{value}%")
        self.detail.setText("Скачивание...")

    def end(self) -> None:
        self.spinner.stop()
        self.hide()


# ---------------------------------------------------------------- setup progress UI

class WallpaperSetupDialog(QDialog):
    def __init__(self, parent=None, title: str = "OpenWallpaper"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setFixedSize(420, 130)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        self.stage_label = QLabel("Подготовка...")
        self.stage_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.spinner = QProgressBar()
        self.spinner.setRange(0, 0)
        self.spinner.setTextVisible(False)
        self.spinner.setFixedHeight(6)

        self.detail_label = QLabel("")
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_label.setStyleSheet("color: #8b93a7;")

        layout = QVBoxLayout(self)
        layout.addWidget(self.stage_label)
        layout.addWidget(self.spinner)
        layout.addWidget(self.detail_label)

    def set_stage(self, text: str):
        self.stage_label.setText(text)

    def set_detail(self, text: str):
        self.detail_label.setText(text)


# ---------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OpenWallpaper")
        self.setWindowIcon(QIcon(str(APP_DIR / "logo.ico")))
        self.resize(1200, 800)

        self.cfg = load_cfg()
        self.engine = WallpaperEngine()
        self.items: list[Wallpaper] = []
        self.by_id: dict[str, Wallpaper] = {}
        self.list_items: dict[str, QListWidgetItem] = {}
        self.icons: dict[str, QIcon] = {}
        self.pending: set[str] = set()
        self.pool = QThreadPool.globalInstance()
        self.thumb_signals = ThumbSignals()
        self.thumb_signals.loaded.connect(self._on_thumb)
        self.dl_thread: SteamCmdThread | None = None
        self.current_item: str | None = None
        self.boot_thread: BootstrapThread | None = None
        self.boot_dialog: QProgressDialog | None = None
        self.extract_thread: PkgExtractThread | None = None
        self.setup_dialog: WallpaperSetupDialog | None = None
        self.pending_wp: Wallpaper | None = None
        self.quitting = False

        self.dl_timer = QTimer(self)
        self.dl_timer.setInterval(700)
        self.dl_timer.timeout.connect(self._poll_download)

        placeholder = QPixmap(THUMB)
        placeholder.fill(QColor("#262c3a"))
        self.placeholder = QIcon(placeholder)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("Ссылка на обои из Мастерской или ID (заполнится само при выборе)")
        self.download_btn = QPushButton("Загрузить")

        self.workshop_tab = SteamBrowser(
            get_key=lambda: self.cfg.get("steam_api_key", ""),
            set_key=self._set_api_key,
            on_download=self.on_browser_download,
            in_library=lambda pid: pid in self.by_id,
        )

        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по названию или ID")
        self.type_box = QComboBox()
        self.type_box.addItems(["Все", "video", "scene", "web", "unknown"])

        self.fit_keys = list(FIT_MODES)
        self.fit_box = QComboBox()
        self.fit_box.addItems(list(FIT_MODES.values()))
        self.fit_box.setCurrentIndex(self.fit_keys.index(self.cfg["fit_mode"]))
        self.fit_box.setToolTip("Как подгонять видео под экран: без полос, с полосами или растянуть")

        self.refresh_btn = QPushButton("Обновить")
        self.stop_btn = QPushButton("Остановить обои")
        self.delete_btn = QPushButton("Удалить")
        self.delete_btn.setToolTip("Удалить выбранные обои из библиотеки (клавиша Delete)")

        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid.setIconSize(THUMB)
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid.setMovement(QListWidget.Movement.Static)
        self.grid.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.grid.setSpacing(12)
        self.grid.setWordWrap(True)

        row_ctl = QHBoxLayout()
        row_ctl.addWidget(self.search, 1)
        row_ctl.addWidget(self.type_box)
        row_ctl.addWidget(self.fit_box)
        row_ctl.addWidget(self.refresh_btn)
        row_ctl.addWidget(self.stop_btn)
        row_ctl.addWidget(self.delete_btn)

        library_page = QWidget()
        library_layout = QVBoxLayout(library_page)
        library_layout.addLayout(row_ctl)
        library_layout.addWidget(self.grid, 1)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.workshop_tab, "Мастерская")
        self.tabs.addTab(library_page, "Моя библиотека")

        self.download_panel = DownloadPanel()
        self.download_panel.cancel_requested.connect(self.cancel_download)

        row_url = QHBoxLayout()
        row_url.addWidget(self.url_edit, 1)
        row_url.addWidget(self.download_btn)

        self.status = QLabel()
        self.status.setStyleSheet("color: #8b93a7;")

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(row_url)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.download_panel)
        layout.addWidget(self.status)
        self.setCentralWidget(central)

        self.download_btn.clicked.connect(self.start_download)
        self.url_edit.returnPressed.connect(self.start_download)
        self.refresh_btn.clicked.connect(self.reload)
        self.stop_btn.clicked.connect(self.stop_wallpaper)
        self.delete_btn.clicked.connect(self.delete_selected)
        self.search.textChanged.connect(self.apply_filter)
        self.type_box.currentIndexChanged.connect(self.apply_filter)
        self.fit_box.currentIndexChanged.connect(self.change_fit)
        self.grid.itemActivated.connect(self.play_item)

        QShortcut(QKeySequence(Qt.Key.Key_Delete), self.grid, activated=self.delete_selected)

        debug("=== app start ===")
        self.reload()
        self.ensure_components()
        self._setup_tray()

    # ---- трей

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(QIcon(str(APP_DIR / "logo.ico")))
        self.tray.setToolTip("OpenWallpaper")
        menu = QMenu()
        menu.addAction("Открыть", self.show_from_tray)
        menu.addAction("Остановить обои", self.stop_wallpaper)
        menu.addSeparator()
        menu.addAction("Выход", self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_from_tray()

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _stop_workers(self, timeout_ms: int = 5000) -> None:
        """Останавливает все фоновые потоки: загрузку, распаковку, bootstrap, галерею и пул."""
        self.dl_timer.stop()

        # Галерея: сначала запрещаем новые запросы (если метод есть в steam_browser.py).
        shutdown = getattr(self.workshop_tab, "shutdown", None)
        if callable(shutdown):
            shutdown()
        for t in list(getattr(self.workshop_tab, "_threads", [])):
            if t.isRunning() and not t.wait(timeout_ms):
                debug(f"gallery {type(t).__name__} не остановился, terminate()")
                t.terminate()
                t.wait(2000)

        if self.dl_thread and self.dl_thread.isRunning():
            self.dl_thread.cancel()
        for t in (self.dl_thread, self.extract_thread, self.boot_thread):
            if t is not None and t.isRunning():
                if not t.wait(timeout_ms):
                    debug(f"thread {type(t).__name__} не остановился, terminate()")
                    t.terminate()
                    t.wait(2000)

        if not QThreadPool.globalInstance().waitForDone(timeout_ms):
            debug("QThreadPool не завершился за отведённое время")
        kill_stray_processes()

    def quit_app(self):
        self.quitting = True
        self._stop_workers()
        self.engine.stop(restore=True)
        self.tray.hide()
        debug("=== app exit (tray) ===")
        QApplication.quit()

    # ---- связь с галереей Мастерской

    def _set_api_key(self, key: str):
        self.cfg["steam_api_key"] = key
        save_cfg(self.cfg)

    def on_browser_download(self, item_id: str, title: str):
        self.url_edit.setText(f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}")
        self.status.setText(f"Выбрано: {title}")
        self.start_download()

    # ---- components bootstrap

    def ensure_components(self):
        if STEAMCMD_EXE.is_file() and MPV_EXE.is_file() and REPKG_EXE.is_file():
            return
        self.boot_dialog = QProgressDialog("Подготовка компонентов...", None, 0, 100, self)
        self.boot_dialog.setWindowTitle("OpenWallpaper")
        self.boot_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.boot_dialog.setMinimumDuration(0)
        self.boot_dialog.setValue(0)
        self.boot_thread = BootstrapThread()
        self.boot_thread.progress.connect(self._boot_progress)
        self.boot_thread.ok.connect(self._boot_ok)
        self.boot_thread.fail.connect(self._boot_fail)
        self.boot_thread.start()

    def _boot_progress(self, value: int, text: str):
        if self.boot_dialog:
            self.boot_dialog.setLabelText(text)
            self.boot_dialog.setValue(value)

    def _boot_ok(self, missing: str):
        if self.boot_dialog:
            self.boot_dialog.close()
        if missing:
            QMessageBox.warning(self, "OpenWallpaper", f"Не хватает компонентов:\n{missing}")
            self.status.setText(f"Не хватает: {missing}")
        else:
            self.status.setText("Компоненты готовы.")
        self.reload()

    def _boot_fail(self, msg: str):
        if self.boot_dialog:
            self.boot_dialog.close()
        QMessageBox.warning(self, "OpenWallpaper", msg)

    # ---- fit mode

    def change_fit(self, index: int):
        key = self.fit_keys[index]
        if key == self.cfg["fit_mode"]:
            return
        self.cfg["fit_mode"] = key
        save_cfg(self.cfg)
        debug(f"fit_mode changed to {key}")
        if self.engine.is_running() and self.engine.current:
            try:
                self.engine.play(self.engine.current, key)
                self.status.setText(f"Режим: {FIT_MODES[key]}")
            except Exception as e:
                debug(f"change_fit failed: {e}")
                QMessageBox.warning(self, "OpenWallpaper", f"Не удалось применить режим: {e}")

    # ---- library

    def reload(self):
        self.items = scan_all(workshop_dirs(self.cfg))
        self.by_id = {wp.wallpaper_id: wp for wp in self.items}
        self.apply_filter()
        if hasattr(self, "workshop_tab"):
            self.workshop_tab.refresh_badges()

    def apply_filter(self):
        q = self.search.text().strip().lower()
        wtype = self.type_box.currentText()
        shown = [
            wp for wp in self.items
            if (wtype == "Все" or wp.type == wtype)
            and (not q or q in wp.title.lower() or q in wp.wallpaper_id)
        ]

        self.grid.clear()
        self.list_items = {}
        for wp in shown:
            label = f"{wp.title}\n[{wp.type}]" + ("  (нет видео)" if wp.no_video else "")
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, wp.wallpaper_id)
            item.setSizeHint(QSize(THUMB.width() + 20, THUMB.height() + 50))
            item.setIcon(self.icons.get(wp.wallpaper_id, self.placeholder))
            self.grid.addItem(item)
            self.list_items[wp.wallpaper_id] = item
            if wp.wallpaper_id not in self.icons:
                self._request_thumb(wp)

        self.status.setText(f"Показано: {len(shown)} из {len(self.items)}")

    def _request_thumb(self, wp: Wallpaper):
        if not wp.preview or wp.wallpaper_id in self.pending:
            return
        self.pending.add(wp.wallpaper_id)
        self.pool.start(ThumbTask(wp.wallpaper_id, str(wp.preview), self.thumb_signals))

    def _on_thumb(self, key: str, img: QImage):
        self.pending.discard(key)
        if img.isNull():
            return
        icon = QIcon(QPixmap.fromImage(img))
        self.icons[key] = icon
        item = self.list_items.get(key)
        if item:
            item.setIcon(icon)

    def choose_steam(self):
        path = QFileDialog.getExistingDirectory(self, "Папка Steam (где лежит steamapps)")
        if path:
            self.cfg["steam_root"] = path
            save_cfg(self.cfg)
            self.reload()

    # ---- delete

    def delete_selected(self):
        item = self.grid.currentItem()
        if item is None:
            self.status.setText("Выбери обои в библиотеке.")
            return
        wp = self.by_id.get(item.data(Qt.ItemDataRole.UserRole))
        if wp:
            self.delete_wallpaper(wp)

    def delete_wallpaper(self, wp: Wallpaper):
        answer = QMessageBox.question(
            self, "OpenWallpaper",
            f"Удалить «{wp.title}» из библиотеки?\n\n"
            "Файлы обоев будут стёрты с диска. Отменить это нельзя.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        cache = extracted_dir_for(wp.wallpaper_id)

        cur = self.engine.current
        if cur is not None and (wp.folder in cur.parents or cache in cur.parents):
            self.engine.stop(restore=True)
            time.sleep(0.5)

        targets = [d / wp.wallpaper_id for d in workshop_dirs(self.cfg)] + [cache]
        errors: list[str] = []
        for t in targets:
            if not t.exists():
                continue
            try:
                shutil.rmtree(t)
            except OSError as e:
                errors.append(f"{t}: {e}")

        if self.cfg.get("last_wallpaper") == wp.wallpaper_id:
            self.cfg["last_wallpaper"] = ""
            save_cfg(self.cfg)
        self.icons.pop(wp.wallpaper_id, None)
        debug(f"delete: id={wp.wallpaper_id} errors={errors}")

        self.reload()
        if errors:
            QMessageBox.warning(
                self, "OpenWallpaper",
                "Часть файлов не удалось стереть (возможно, они заняты другой программой):\n\n"
                + "\n".join(errors[:5]),
            )
            self.status.setText("Удаление выполнено не полностью.")
        else:
            self.status.setText(f"Удалено: {wp.title}")

    # ---- download

    def start_download(self):
        if self.dl_thread and self.dl_thread.isRunning():
            return
        item_id = item_id_from_url(self.url_edit.text())
        if not item_id:
            QMessageBox.information(
                self, "OpenWallpaper",
                "Не нашёл ID предмета. Выбери обои в галерее или вставь ссылку вида ...?id=123456.",
            )
            return
        if not STEAMCMD_EXE.is_file():
            QMessageBox.warning(self, "OpenWallpaper", "Компоненты ещё не готовы. Подожди окончания подготовки.")
            return

        login = self.cfg.get("steam_login", "")
        password = self.cfg.get("steam_password", "")

        if not login or not password:
            if not login:
                login, ok = QInputDialog.getText(
                    self, "Вход в Steam",
                    "Логин аккаунта Steam, который владеет Wallpaper Engine:",
                )
                if not ok or not login.strip():
                    return
                self.cfg["steam_login"] = login.strip()

            if not password:
                password, ok = QInputDialog.getText(
                    self, "Вход в Steam",
                    "Пароль от аккаунта Steam:",
                    QLineEdit.EchoMode.Password,
                )
                if not ok or not password.strip():
                    return
                self.cfg["steam_password"] = password.strip()

            save_cfg(self.cfg)

        self.current_item = item_id
        self.download_btn.setEnabled(False)
        self.download_panel.begin(item_id)
        self.dl_timer.start()
        self.status.setText(f"Загружаю предмет {item_id}...")

        self.dl_thread = SteamCmdThread(self.cfg["steam_login"], self.cfg["steam_password"], item_id)
        self.dl_thread.progress.connect(self.download_panel.set_progress)
        self.dl_thread.detail.connect(self.download_panel.set_detail)
        self.dl_thread.need_input.connect(self._on_need_input)
        self.dl_thread.ok.connect(self._download_ok)
        self.dl_thread.fail.connect(self._download_fail)
        self.dl_thread.start()

    def _poll_download(self):
        if not self.current_item or self.download_panel.has_percent:
            return
        size = dir_size(STEAMCMD_STAGING / self.current_item) or dir_size(STEAMCMD_WORKSHOP / self.current_item)
        if size:
            self.download_panel.set_detail(f"Скачано {size / 1048576:.1f} МБ")

    def _on_need_input(self, prompt: str, secret: bool):
        if self.dl_thread is None:
            return
        self.download_panel.set_detail("Нужен вход в Steam...")
        echo = QLineEdit.EchoMode.Password if secret else QLineEdit.EchoMode.Normal
        text, ok = QInputDialog.getText(self, "Вход в Steam", prompt, echo)
        if ok and text:
            if secret:
                self.cfg["steam_password"] = text.strip()
                save_cfg(self.cfg)
                self.dl_thread.password = text.strip()
            self.download_panel.set_detail("Проверяю вход...")
            self.dl_thread.send(text)
        else:
            self.dl_thread.cancel()

    def cancel_download(self):
        if self.dl_thread and self.dl_thread.isRunning():
            self.dl_thread.cancel()

    def _finish_download_ui(self):
        self.dl_timer.stop()
        self.download_panel.end()
        self.download_btn.setEnabled(True)
        self.current_item = None

    def _download_ok(self, item_id: str):
        self._finish_download_ui()
        self.reload()
        wp = self.by_id.get(item_id)
        if wp and wp.playable:
            self.play_wallpaper(wp)
        elif wp and wp.no_video:
            self.status.setText(f"Скачано {item_id}, но в сцене нет видео.")
        else:
            self.status.setText(f"Скачано {item_id}, но этот тип пока не воспроизводится.")

    def _download_fail(self, msg: str):
        self._finish_download_ui()
        if msg == SteamCmdThread.CANCEL_TEXT:
            self.status.setText("Загрузка отменена.")
            return
        self.status.setText("Ошибка загрузки.")
        QMessageBox.warning(self, "OpenWallpaper", msg)

    # ---- playback

    def play_item(self, item: QListWidgetItem):
        wp = self.by_id.get(item.data(Qt.ItemDataRole.UserRole))
        if wp:
            self.play_wallpaper(wp)

    def play_wallpaper(self, wp: Wallpaper):
        debug(f"play_wallpaper: id={wp.wallpaper_id} type={wp.type} video={wp.video} "
              f"pkg={wp.pkg} no_video={wp.no_video}")
        if wp.type == "web" and wp.video is None:
            QMessageBox.information(
                self, "OpenWallpaper",
                "Веб-обои пока не поддерживаются: для них нужен отдельный показ через WebEngine.",
            )
            return
        if wp.no_video:
            QMessageBox.information(self, "OpenWallpaper", explain_no_video(extracted_dir_for(wp.wallpaper_id)))
            return
        if not wp.playable:
            QMessageBox.information(
                self, "OpenWallpaper",
                f"Тип «{wp.type}» не поддерживается: нет ни видео, ни .pkg для распаковки.",
            )
            return
        if not MPV_EXE.is_file():
            QMessageBox.warning(self, "OpenWallpaper", "mpv.exe не найден в папке bin.")
            return

        if wp.video is not None:
            self._install_wallpaper(wp, wp.video)
            return

        self._extract_then_install(wp)

    def _extract_then_install(self, wp: Wallpaper):
        self.pending_wp = wp
        self.setup_dialog = WallpaperSetupDialog(self)
        self.setup_dialog.set_stage(f"Распаковываю «{wp.title}»...")
        self.setup_dialog.set_detail("Извлекаю текстуры, шейдеры и видео. Это может занять минуту.")
        self.setup_dialog.show()

        self.extract_thread = PkgExtractThread(wp.pkg, wp.wallpaper_id)
        self.extract_thread.ok.connect(self._on_extract_ok)
        self.extract_thread.fail.connect(self._on_extract_fail)
        self.extract_thread.start()

    def _on_extract_ok(self, out_dir: Path):
        wp = self.pending_wp
        video = find_scene_video(out_dir)
        if video is None and wp is not None:
            video = find_scene_fallback(out_dir, wp.folder)
        debug(f"extract_ok: out_dir={out_dir} video={video}")
        if wp is None or video is None:
            self._on_extract_fail("Видео не найдено в распакованной сцене.")
            return
        wp.video = video
        wp.pkg = None
        wp.no_video = False
        self._install_wallpaper(wp, video)

    def _on_extract_fail(self, msg: str):
        self._close_dialog()
        self.pending_wp = None
        debug(f"extract_fail: {msg[:300]!r}")
        if msg.startswith("Это интерактивная сцена"):
            self.reload()
            self.status.setText("Сцена без видео: открой её в Wallpaper Engine.")
            QMessageBox.information(self, "OpenWallpaper", msg)
            return
        self.status.setText("Ошибка распаковки.")
        QMessageBox.warning(self, "OpenWallpaper", msg)

    def _install_wallpaper(self, wp: Wallpaper, video: Path):
        if self.setup_dialog is None:
            self.setup_dialog = WallpaperSetupDialog(self)
            self.setup_dialog.show()
        self.setup_dialog.set_stage("Устанавливаю обои...")
        self.setup_dialog.set_detail("Ищу рабочий стол и запускаю проигрыватель")
        QApplication.processEvents()
        try:
            self.engine.play(video, self.cfg["fit_mode"])
        except Exception as e:
            debug(f"install failed: {e}")
            self._close_dialog()
            self.status.setText("Ошибка установки обоев.")
            QMessageBox.warning(
                self, "OpenWallpaper",
                f"Не удалось запустить обои: {e}\n\nПодробности: orgen_debug.log и mpv.log",
            )
            return

        self._close_dialog()
        self.pending_wp = None
        self.cfg["last_wallpaper"] = wp.wallpaper_id
        save_cfg(self.cfg)
        self.status.setText(f"Обои: {wp.title} ({FIT_MODES[self.cfg['fit_mode']]})")

    def _close_dialog(self):
        if self.setup_dialog:
            self.setup_dialog.close()
            self.setup_dialog = None

    def stop_wallpaper(self):
        self.engine.stop(restore=True)
        self.status.setText("Обои остановлены, возвращены стандартные обои Windows.")

    def closeEvent(self, event):
        if not self.quitting and self.tray.isVisible():
            event.ignore()
            self.hide()
            self.tray.showMessage(
                "OpenWallpaper",
                "Программа работает в трее. Обои продолжают работать.",
            )
            return

        self._stop_workers()
        self.engine.stop(restore=True)
        self.tray.hide()
        debug("=== app exit ===")
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(APP_DIR / "logo.ico")))
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(DARK_QSS + combo_qss())
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()