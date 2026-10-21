"""Галерея обоев из Мастерской Steam.

Основной источник: JSON, который Steam кладёт в страницу (valve-ssr-data, запрос workshop_browse).
Запасной источник: разбор HTML по ссылкам на предметы.
Если Steam перестанет отдавать карточки, а ключ API задан, галерея пробует Steam Web API.

Клик по карточке открывает страницу обоев: большое превью, автор, разрешение, размер,
описание, теги и кнопка «Скачать и поставить».

Фильтры передаются как requiredtags. Steam считает их И (все выбранные теги должны совпасть).
"""
import html
import json
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PySide6.QtCore import (
    QBuffer, QByteArray, QEvent, QIODevice, QObject, QRunnable, QSize, Qt,
    QThread, QThreadPool, QTimer, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QIcon, QImage, QMovie, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListView, QListWidget, QListWidgetItem, QMenu, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAS_MULTIMEDIA = True

except ImportError:
    # Пакет PySide6-Addons/QtMultimedia не установлен: страница деталей покажет
    # статичную картинку вместо проигрывания видео, остальное работает как раньше.
    HAS_MULTIMEDIA = False
try:
    import upscale
except Exception:
    upscale = None

APP_ID = 431960
BROWSE_URL = "https://steamcommunity.com/workshop/browse/"
API_URL = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
KEY_HELP_URL = "https://steamcommunity.com/dev/apikey"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OrgenWallpaper/1.0"
# Куки для страниц 18+: если Steam всё равно просит подтверждение, открой страницу
# в обычном браузере один раз и подтверди возраст.
COOKIES = "mature_content=1; wants_mature_content=1"
API_PAGE_SIZE = 24
CARD_SIZE = QSize(236, 190)
THUMB_SIZE = QSize(220, 124)

WHEEL_FACTOR = 1.6        # во сколько раз ускорить прокрутку колесом
PREFETCH_PX = 2400        # догружать следующую страницу, когда до конца осталось столько пикселей
THUMB_THREADS = 12        # сколько превью грузится параллельно
DETAIL_MAX_SIDE = 8192    # большой лимит: CDN отдаёт исходник, если он меньше
UPSCALE_ENABLED = False   # waifu2x на CPU медленный, включай только если нужна четкость

# (название, browsesort для страницы, query_type для API)
SORTS = [
    ("Популярное", "trend", 3),
    ("Новое", "mostrecent", 1),
    ("Высший рейтинг", "score", 0),
]

# Фильтры: (подпись, тег Steam или None)
TYPES = [
    ("Все типы", None),
    ("Сцены", "Scene"),
    ("Видео", "Video"),
    ("Веб", "Web"),
    ("Приложения", "Application"),
]
RATINGS = [
    ("Любой возраст", None),
    ("Everyone", "Everyone"),
    ("Questionable", "Questionable"),
    ("Mature", "Mature"),
]
RESOLUTIONS = [
    ("Любое разрешение", None),
    ("1920 x 1080", "1920 x 1080"),
    ("2560 x 1440", "2560 x 1440"),
    ("3840 x 2160", "3840 x 2160"),
]
GENRES = [
    "Abstract", "Animal", "Anime", "Cartoon", "CGI", "Cyberpunk", "Fantasy", "Game",
    "Girls", "Guys", "Landscape", "Medieval", "Memes", "MMD", "Music", "Nature",
    "Pixel art", "Relaxing", "Retro", "Sci-Fi", "Sports", "Technology", "Television",
    "Vehicle", "Unspecified",
]

TYPE_TAGS = {"Scene": "Сцена", "Video": "Видео", "Web": "Веб", "Application": "Приложение"}
RES_RE = re.compile(r"\d{3,5}\s*x\s*\d{3,5}", re.I)

# Параметры query string, которыми Steam CDN просит уменьшенную/пережатую картинку
# (imw/imh = ширина/высота, ima = режим подгонки, impolicy = letterbox и т.п.).
# Их удаление возвращает ссылку на оригинал с полным разрешением.
_RESIZE_QUERY_KEYS = {"imw", "imh", "ima", "impolicy", "imcolor", "letterbox"}
# Суффикс вида ".616x353" перед расширением файла — тоже признак уменьшенной версии.
_RESIZE_SUFFIX_RE = re.compile(r"\.\d{2,5}x\d{2,5}(?=\.[a-zA-Z]{3,4}$)")

# ---------------------------------------------------------------- стрелки (общие для всего приложения)

CHEVRON_COLOR = "#8b93a7"
CHEVRON_PATH = Path(tempfile.gettempdir()) / "orgen_chevron_down.png"


def chevron_url() -> str:
    """Путь к PNG-стрелке для QSS. Рисуется один раз. Используется и в orgen.py."""
    if not CHEVRON_PATH.is_file():
        img = QImage(24, 24, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(CHEVRON_COLOR), 2.4, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawLine(6, 9, 12, 15)
        p.drawLine(12, 15, 18, 9)
        p.end()
        img.save(str(CHEVRON_PATH), "PNG")
    return CHEVRON_PATH.as_posix()


def combo_qss() -> str:
    """Единый стиль всех выпадающих списков: одна стрелка, один размер, одно меню."""
    u = chevron_url()
    return f"""
QComboBox {{
    background: #1f2430; border: 1px solid #2a2f3a; border-radius: 8px;
    padding: 5px 30px 5px 12px; color: #e6ebf5; min-width: 110px;
}}
QComboBox:hover {{ border-color: #3d8bff; }}
QComboBox::drop-down {{
    border: none; width: 28px;
    subcontrol-origin: padding; subcontrol-position: top right;
}}
QComboBox::down-arrow {{ image: url("{u}"); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: #1b1f27; border: 1px solid #2a2f3a; color: #e6ebf5;
    selection-background-color: #22304a; outline: 0;
}}
QToolButton {{
    background: #1f2430; border: 1px solid #2a2f3a; border-radius: 8px;
    padding: 5px 30px 5px 12px; color: #e6ebf5; min-width: 110px;
}}
QToolButton:hover {{ border-color: #3d8bff; }}
QToolButton::menu-button {{ border: none; width: 28px; }}
QToolButton::menu-indicator {{
    image: url("{u}"); width: 12px; height: 12px;
    subcontrol-origin: padding; subcontrol-position: right center; right: 9px;
}}
"""


BROWSER_QSS = """
#browserGrid { background: #0f1218; border: 1px solid #2a2f3a; border-radius: 10px; }
#browserGrid::item { border-radius: 10px; padding: 6px; color: #cfd6e4; }
#browserGrid::item:selected { background: #22304a; border: 1px solid #3d8bff; }

/* Тонкий скроллбар без кнопок-стрелок */
#browserGrid QScrollBar:vertical { background: transparent; width: 8px; margin: 4px 2px 4px 0; }
#browserGrid QScrollBar::handle:vertical { background: #2f3644; border-radius: 4px; min-height: 40px; }
#browserGrid QScrollBar::handle:vertical:hover { background: #3d8bff; }
#browserGrid QScrollBar::add-line:vertical, #browserGrid QScrollBar::sub-line:vertical { height: 0; }
#browserGrid QScrollBar::add-page:vertical, #browserGrid QScrollBar::sub-page:vertical { background: none; }

#filterBar { background: #171b23; border: 1px solid #2a2f3a; border-radius: 12px; }
#filterBar QLabel { color: #8b93a7; }
#filterBar QPushButton {
    background: transparent; border: 1px solid #2a2f3a; border-radius: 8px;
    padding: 5px 12px; color: #8b93a7;
}
#filterBar QPushButton:hover { color: #e6ebf5; border-color: #3d8bff; }

QMenu {
    background: #1b1f27; border: 1px solid #2a2f3a; border-radius: 10px;
    padding: 6px; color: #e6ebf5;
}
QMenu::item { padding: 6px 22px 6px 10px; border-radius: 6px; }
QMenu::item:selected { background: #22304a; }
QMenu::indicator { width: 14px; height: 14px; }

#browserInfo { color: #cfd6e4; font-size: 13px; }
#browserStatus { color: #8b93a7; font-size: 12px; }
#primaryBtn {
    background: #3d8bff; border: none; color: white; font-weight: 600;
    padding: 9px 18px; border-radius: 8px;
}
#primaryBtn:hover { background: #5a9dff; }
#primaryBtn:disabled { background: #2a3550; color: #6b7590; }

/* Страница обоев */
#previewLabel { background: #000000; border: 1px solid #2a2f3a; border-radius: 12px; }
#detailSide { background: #171b23; border: 1px solid #2a2f3a; border-radius: 12px; }
#detailTitle { font-size: 18px; font-weight: 700; color: #f1f4fa; background: transparent; }
#detailInfo { color: #cfd6e4; font-size: 13px; background: transparent; }
#detailHead { color: #8b93a7; font-size: 12px; background: transparent; }
#detailDesc { color: #aab3c5; font-size: 12px; background: transparent; }
#detailTags { color: #7fa8e8; font-size: 12px; background: transparent; }
#detailLib { color: #8b93a7; font-size: 12px; background: transparent; }
"""


def build_qss() -> str:
    return combo_qss() + BROWSER_QSS


# ---------------------------------------------------------------- PARSER (правь здесь, если Steam сменит вёрстку)

class ScrapeError(Exception):
    pass


SSR_RE = re.compile(
    r'<script type="application/json" id="valve-ssr-data"[^>]*>(.*?)</script>', re.S
)
ITEM_LINK_RE = re.compile(r"sharedfiles/filedetails/\?id=(\d{6,})")
IMG_RE = re.compile(r'<img[^>]+src="(https://[^"]+)"', re.I)
ALT_RE = re.compile(r'<img[^>]+alt="([^"]*)"', re.I)
AGE_GATE_HINTS = ("agegate", "app_agegate", "mature_content", "Проверка возраста", "age check")


def full_res_url(url: str) -> str:
    """Ссылка на исходное превью: убираем параметры ресайза и суффикс вида .616x353.

    Steam отдаёт preview_url с параметрами авто-уменьшения (imw/imh/ima/impolicy)
    или с суффиксом перед расширением. Без них CDN отдаёт файл, который лежит на сервере.
    """
    if not url:
        return url
    parsed = urllib.parse.urlsplit(url)
    q = [
        (k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in _RESIZE_QUERY_KEYS
    ]
    path = _RESIZE_SUFFIX_RE.sub("", parsed.path)
    return urllib.parse.urlunsplit(
        parsed._replace(path=path, query=urllib.parse.urlencode(q))
    )


def _format_size(raw) -> str:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f} ГБ"
    return f"{n / (1 << 20):.1f} МБ"


def _names_from_queries(query_data: dict) -> dict[str, str]:
    """Имена авторов по steamid из блоков PlayerLinkDetails."""
    names: dict[str, str] = {}
    for q in query_data.get("queries", []):
        key = q.get("queryKey") or []
        if len(key) >= 2 and key[0] == "PlayerLinkDetails":
            data = (q.get("state") or {}).get("data") or {}
            pub = data.get("public_data") or {}
            if pub.get("steamid"):
                names[str(pub["steamid"])] = pub.get("persona_name", "") or ""
    return names


_VIDEO_EXT_RE = re.compile(r"\.(webm|mp4)(\?|$)", re.I)


def _video_url_from_ssr(r: dict) -> str:
    """Ссылка на видео-ролик превью (не картинку), если Steam его отдал.

    Схема для этого поля официально не документирована и у Steam встречались
    разные варианты в зависимости от версии фронтенда, поэтому проверяем
    по очереди несколько известных мест, а не одно жёстко заданное имя.
    """
    for key in ("preview_video_url", "movie_url", "video_url"):
        val = r.get(key)
        if isinstance(val, str) and val:
            return val

    # Иногда ролик спрятан в списке дополнительных превью предмета.
    for p in r.get("previews") or []:
        if not isinstance(p, dict):
            continue
        url = p.get("url") or p.get("preview_url") or ""
        ptype = str(p.get("preview_type", "")).lower()
        if url and (ptype in ("1", "webm", "video", "youtube") or _VIDEO_EXT_RE.search(url)):
            if ptype != "youtube":
                return url

    # Запасной случай: сам preview_url иногда уже указывает на webm/mp4.
    main = r.get("preview_url") or ""
    if _VIDEO_EXT_RE.search(main):
        return main
    return ""


def _meta_from_ssr(r: dict, names: dict[str, str]) -> dict:
    tag_objs = r.get("tags") or []
    tag_ids = [t.get("tag", "") for t in tag_objs]
    tag_names = [t.get("display_name") or t.get("tag") or "" for t in tag_objs]
    tag_names = [t for t in tag_names if t]

    type_name = next((TYPE_TAGS[t] for t in tag_ids if t in TYPE_TAGS), "")
    res = next((t for t in tag_names if RES_RE.search(t)), "")
    if not res and "Dynamic resolution" in tag_ids:
        res = "Динамическое"

    return {
        "author": names.get(str(r.get("creator") or ""), ""),
        "type": type_name,
        "resolution": res,
        "size": _format_size(r.get("file_size")),
        "description": html.unescape(r.get("short_description") or ""),
        "tags": tag_names,
    }


def parse_ssr_cards(page_html: str) -> list[dict]:
    """Карточки из JSON-блока valve-ssr-data (запрос workshop_browse)."""
    m = SSR_RE.search(page_html)
    if not m:
        return []
    try:
        ssr = json.loads(m.group(1))
        query_data = json.loads(ssr["renderContext"]["queryData"])
    except (ValueError, KeyError, TypeError):
        return []

    names = _names_from_queries(query_data)
    for q in query_data.get("queries", []):
        key = q.get("queryKey") or []
        if not key or key[0] != "workshop_browse":
            continue
        data = (q.get("state") or {}).get("data") or {}
        cards = []
        for r in data.get("results") or []:
            pid = str(r.get("publishedfileid") or "")
            if not pid:
                continue
            cards.append({
                "publishedfileid": pid,
                "title": html.unescape(r.get("title") or pid),
                "preview_url": full_res_url(r.get("preview_url") or ""),
                "video_url": _video_url_from_ssr(r),
                "meta": _meta_from_ssr(r, names),
            })
        return cards
    return []


def parse_html_cards(page_html: str) -> list[dict]:
    """Запасной разбор: ссылки на предметы и картинка рядом с каждой. Без метаданных."""
    cards: list[dict] = []
    seen: set[str] = set()
    for m in ITEM_LINK_RE.finditer(page_html):
        pid = m.group(1)
        if pid in seen:
            continue
        seen.add(pid)
        around = page_html[max(0, m.start() - 600): m.start() + 600]

        img_m = IMG_RE.search(around)
        preview = html.unescape(img_m.group(1)) if img_m else ""

        title = pid
        alt_m = ALT_RE.search(around)
        if alt_m and alt_m.group(1).strip():
            title = html.unescape(alt_m.group(1)).strip()

        cards.append({
            "publishedfileid": pid,
            "title": title,
            "preview_url": full_res_url(preview),
            "video_url": "",
            "meta": {},
        })
    return cards


def parse_cards(page_html: str) -> list[dict]:
    return parse_ssr_cards(page_html) or parse_html_cards(page_html)


def dump_debug_html(page_html: str, page: int) -> str:
    path = Path(__file__).resolve().parent / f"workshop_page_{page}.html"
    try:
        path.write_text(page_html, encoding="utf-8")
    except OSError:
        pass
    return str(path)


# ---------------------------------------------------------------- sources

def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cookie": COOKIES})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_scrape(page: int, search: str, sort_idx: int, tags: list[str]) -> list[dict]:
    _, browse_sort, _ = SORTS[sort_idx]
    params: list[tuple[str, str]] = [
        ("appid", str(APP_ID)),
        ("section", "readytouseitems"),
        ("browsesort", "textsearch" if search else browse_sort),
        ("p", str(page)),
    ]
    if search:
        params.append(("searchtext", search))
    for t in tags:
        params.append(("requiredtags[]", t))
    url = BROWSE_URL + "?" + urllib.parse.urlencode(params)
    try:
        page_html = _http_get(url)
    except urllib.error.HTTPError as e:
        raise ScrapeError(f"Мастерская вернула ошибку {e.code}.")
    except (urllib.error.URLError, TimeoutError) as e:
        raise ScrapeError(f"Нет связи с Мастерской: {e}")

    cards = parse_cards(page_html)
    if cards:
        return cards

    saved = dump_debug_html(page_html, page)
    low = page_html.lower()
    if any(h.lower() in low for h in AGE_GATE_HINTS):
        raise ScrapeError(
            "Steam показывает проверку возраста. Открой Мастерскую в обычном браузере, "
            "подтверди возраст и попробуй снова."
        )
    if page == 1:
        raise ScrapeError(f"На странице нет карточек. HTML сохранён в {saved}. Пришли этот файл.")
    return []


def fetch_api(api_key: str, page: int, search: str, sort_idx: int, tags: list[str]) -> tuple[int, list[dict]]:
    _, _, query_type = SORTS[sort_idx]
    params: list[tuple[str, str]] = [
        ("key", api_key),
        ("appid", str(APP_ID)),
        ("query_type", str(query_type)),
        ("page", str(page)),
        ("numperpage", str(API_PAGE_SIZE)),
        ("filetype", "0"),
        ("return_previews", "true"),
    ]
    if search:
        params.append(("search_text", search))
    for t in tags:
        params.append(("requiredtags", t))
    url = API_URL + "?" + urllib.parse.urlencode(params)
    try:
        data = json.loads(_http_get(url))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ScrapeError("Steam не принял ключ API. Проверь ключ.")
        raise ScrapeError(f"Steam API вернул ошибку {e.code}.")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise ScrapeError(f"Steam API недоступен: {e}")
    resp = data.get("response", {})
    items = [
        {
            "publishedfileid": str(it.get("publishedfileid", "")),
            "title": it.get("title") or str(it.get("publishedfileid", "")),
            "preview_url": full_res_url(it.get("preview_url") or ""),
            "video_url": _video_url_from_ssr(it),
            "meta": {
                "author": "",
                "type": "",
                "resolution": "",
                "size": _format_size(it.get("file_size")),
                "description": it.get("short_description") or "",
                "tags": [t.get("display_name") or t.get("tag", "") for t in it.get("tags") or []],
            },
        }
        for it in resp.get("publishedfiledetails", []) or []
        if it.get("publishedfileid")
    ]
    return int(resp.get("total", 0)), items


# ---------------------------------------------------------------- threads

class QueryThread(QThread):
    done = Signal(list, bool, str)   # карточки, есть ли ещё страницы, источник
    fail = Signal(str)

    def __init__(self, api_key: str, page: int, search: str, sort_idx: int, tags: list[str]):
        super().__init__()
        self.api_key = api_key
        self.page = page
        self.search = search
        self.sort_idx = sort_idx
        self.tags = tags

    def run(self):
        try:
            cards = fetch_scrape(self.page, self.search, self.sort_idx, self.tags)
            self.done.emit(cards, len(cards) > 0, "Мастерская")
            return
        except ScrapeError as e:
            scrape_error = str(e)

        if not self.api_key:
            self.fail.emit(scrape_error)
            return
        try:
            total, cards = fetch_api(self.api_key, self.page, self.search, self.sort_idx, self.tags)
            has_more = self.page * API_PAGE_SIZE < total
            self.done.emit(cards, has_more, "API Steam")
        except ScrapeError as e:
            self.fail.emit(f"{scrape_error}\nЗапасной API тоже не сработал: {e}")


class ThumbSignals(QObject):
    loaded = Signal(str, QImage, bytes)   # id, миниатюра, сырые байты (для GIF) или b""


class ThumbJob(QRunnable):
    def __init__(self, key: str, url: str, signals: ThumbSignals):
        super().__init__()
        self.key = key
        self.url = url
        self.signals = signals

    def run(self):
        raw = b""
        img = QImage()
        try:
            req = urllib.request.Request(self.url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
            img = QImage.fromData(raw)
        except Exception:
            img = QImage()
            raw = b""
        if not img.isNull():
            img = img.scaled(
                THUMB_SIZE,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
        is_gif = self.url.lower().split("?")[0].endswith(".gif")
        self.signals.loaded.emit(self.key, img, raw if is_gif else b"")


class FullSignals(QObject):
    loaded = Signal(str, bytes)   # id, сырые байты полного превью


class FullImageJob(QRunnable):
    """Загружает полное превью для страницы обоев.

    url уже очищен функцией full_res_url на этапе разбора карточек,
    но на всякий случай прогоняем ещё раз здесь.
    """

    def __init__(self, pid: str, url: str, signals: FullSignals):
        super().__init__()
        self.pid = pid
        self.url = full_res_url(url)
        self.signals = signals

    def run(self):
        raw = b""
        try:
            req = urllib.request.Request(self.url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
        except Exception:
            raw = b""

        # Апскейл выключен по умолчанию (UPSCALE_ENABLED), потому что waifu2x на CPU медленный.
        # GIF пропускаем: waifu2x обработает только первый кадр.
        if raw and UPSCALE_ENABLED and upscale is not None and raw[:4] != b"GIF8":
            better = upscale.upscale_bytes(raw, self.url)
            if better:
                raw = better

        self.signals.loaded.emit(self.pid, raw)


def _solid_icon(color: str) -> QIcon:
    pm = QPixmap(THUMB_SIZE)
    pm.fill(QColor(color))
    return QIcon(pm)


def _popup_view() -> QListView:
    view = QListView()
    view.setObjectName("comboPopup")
    return view


# ---------------------------------------------------------------- grid with faster wheel

class FastGrid(QListWidget):
    """Сетка, в которой колесо прокручивает быстрее и плавнее (по пикселям)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

    def wheelEvent(self, event):
        dy = event.angleDelta().y()
        if dy:
            sb = self.verticalScrollBar()
            sb.setValue(sb.value() - int(dy * WHEEL_FACTOR))
        event.accept()


# ---------------------------------------------------------------- filter bar

class FilterBar(QFrame):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("filterBar")

        self.type_box = self._combo(TYPES)
        self.rating_box = self._combo(RATINGS)
        self.res_box = self._combo(RESOLUTIONS)

        self.genre_btn = QToolButton()
        self.genre_btn.setText("Жанры")
        self.genre_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.genre_btn.setToolTip("Жанры. Steam показывает предметы, у которых есть все выбранные жанры.")
        self.genre_menu = QMenu(self.genre_btn)
        self.genre_actions: dict[str, QAction] = {}
        for g in GENRES:
            act = QAction(g, self.genre_menu)
            act.setCheckable(True)
            act.toggled.connect(self._on_genre)
            self.genre_menu.addAction(act)
            self.genre_actions[g] = act
        self.genre_btn.setMenu(self.genre_menu)

        self.clear_btn = QPushButton("Сбросить")
        self.clear_btn.clicked.connect(self.reset)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)
        for label, widget in (
            ("Тип", self.type_box),
            ("Возраст", self.rating_box),
            ("Разрешение", self.res_box),
        ):
            row.addWidget(QLabel(label))
            row.addWidget(widget)
        row.addWidget(self.genre_btn)
        row.addStretch(1)
        row.addWidget(self.clear_btn)

    def _combo(self, items) -> QComboBox:
        box = QComboBox()
        for label, value in items:
            box.addItem(label, value)
        box.setView(_popup_view())
        box.currentIndexChanged.connect(lambda _i: self.changed.emit())
        return box

    def _on_genre(self, _checked: bool):
        n = len(self.selected_genres())
        self.genre_btn.setText(f"Жанры · {n}" if n else "Жанры")
        self.changed.emit()

    def selected_genres(self) -> list[str]:
        return [g for g, a in self.genre_actions.items() if a.isChecked()]

    def tags(self) -> list[str]:
        result = []
        for box in (self.type_box, self.rating_box, self.res_box):
            value = box.currentData()
            if value:
                result.append(value)
        result.extend(self.selected_genres())
        return result

    def reset(self):
        for box in (self.type_box, self.rating_box, self.res_box):
            box.blockSignals(True)
            box.setCurrentIndex(0)
            box.blockSignals(False)
        for act in self.genre_actions.values():
            act.blockSignals(True)
            act.setChecked(False)
            act.blockSignals(False)
        self.genre_btn.setText("Жанры")
        self.changed.emit()


# ---------------------------------------------------------------- страница обоев

class PreviewLabel(QLabel):
    """Большое превью. Статичная картинка или GIF, растягивается на всю область."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("previewLabel")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(520, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._pix: QPixmap | None = None
        self._frame: QImage | None = None
        self._movie: QMovie | None = None
        self._buf: QBuffer | None = None

    def clear_media(self):
        if self._movie is not None:
            self._movie.stop()
            self._movie.deleteLater()
            self._movie = None
        if self._buf is not None:
            self._buf.close()
            self._buf.deleteLater()
            self._buf = None
        self._pix = None
        self._frame = None
        self.clear()

    def set_still(self, img: QImage):
        self.clear_media()
        self._pix = QPixmap.fromImage(img)
        self._refresh()

    def set_gif(self, raw: bytes) -> bool:
        self.clear_media()
        buf = QBuffer(self)
        buf.setData(QByteArray(raw))
        buf.open(QIODevice.OpenModeFlag.ReadOnly)
        movie = QMovie(buf, QByteArray(b"gif"), self)
        if not movie.isValid():
            buf.close()
            buf.deleteLater()
            return False
        movie.frameChanged.connect(self._on_frame)
        self._movie = movie
        self._buf = buf
        movie.start()
        return True

    def _on_frame(self, _n: int):
        if self._movie is None:
            return
        self._frame = self._movie.currentImage()
        self._refresh()

    def _refresh(self):
        src = self._frame if self._movie is not None else self._pix
        if src is None or src.isNull():
            return
        size = self.size()
        if size.width() < 2 or size.height() < 2:
            return
        pix = QPixmap.fromImage(src) if isinstance(src, QImage) else src

        # Размер в физических пикселях экрана (важно при масштабе 125%/150%),
        # картинка растягивается ровно под область без сохранения пропорций.
        dpr = self.devicePixelRatioF() or 1.0
        target_w = max(1, round(size.width() * dpr))
        target_h = max(1, round(size.height() * dpr))

        scaled = pix.scaled(
            target_w, target_h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(dpr)
        self.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()


class VideoPreviewWidget(QWidget):
    """Проигрывает видео-ролик превью (webm/mp4) в цикле, без звука, растягивая на всю область.

    Показывается на странице деталей вместо PreviewLabel, когда у обоев есть
    собственный видео-ролик превью (не просто статичная картинка).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("previewLabel")
        self.setMinimumSize(520, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background: #000000;")

        self.video_widget = QVideoWidget(self)
        # IgnoreAspectRatio растягивает кадр на всю область без полос.
        self.video_widget.setAspectRatioMode(Qt.AspectRatioMode.IgnoreAspectRatio)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.video_widget)

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setMuted(True)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.player.setLoops(QMediaPlayer.Loops.Infinite)
        self.player.errorOccurred.connect(self._on_error)
        self._failed = False

    def _on_error(self, _error, _text):
        self._failed = True

    @property
    def failed(self) -> bool:
        return self._failed

    def play_url(self, url: str) -> None:
        self._failed = False
        self.player.setSource(url)
        self.player.play()

    def stop(self) -> None:
        self.player.stop()
        self.player.setSource("")


class DetailView(QWidget):
    """Страница одних обоев: превью слева, информация и кнопка справа."""

    def __init__(self, on_back, on_download, in_library, thumb_provider=None, parent=None):
        super().__init__(parent)
        self._on_download = on_download
        self._in_library = in_library
        self._thumb_provider = thumb_provider or (lambda _pid: None)
        self.pid = ""
        self.title_text = ""
        self.raw_cache: dict[str, bytes] = {}
        self.pool = QThreadPool.globalInstance()
        self.signals = FullSignals()
        self.signals.loaded.connect(self._on_full)

        self.back_btn = QPushButton("←  Назад к списку")
        self.back_btn.clicked.connect(on_back)

        self.preview = PreviewLabel()
        self.video_preview = VideoPreviewWidget() if HAS_MULTIMEDIA else None

        self.preview_stack = QStackedWidget()
        self.preview_stack.addWidget(self.preview)
        if self.video_preview is not None:
            self.preview_stack.addWidget(self.video_preview)

        self.title_lbl = QLabel()
        self.title_lbl.setObjectName("detailTitle")
        self.title_lbl.setWordWrap(True)

        self.info_lbl = QLabel()
        self.info_lbl.setObjectName("detailInfo")
        self.info_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.info_lbl.setWordWrap(True)

        self.desc_lbl = QLabel()
        self.desc_lbl.setObjectName("detailDesc")
        self.desc_lbl.setWordWrap(True)
        self.desc_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        desc_scroll = QScrollArea()
        desc_scroll.setWidgetResizable(True)
        desc_scroll.setFrameShape(QFrame.Shape.NoFrame)
        desc_scroll.setWidget(self.desc_lbl)
        desc_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QWidget#qt_scrollarea_viewport { background: transparent; }"
        )

        self.tags_lbl = QLabel()
        self.tags_lbl.setObjectName("detailTags")
        self.tags_lbl.setWordWrap(True)

        self.library_lbl = QLabel()
        self.library_lbl.setObjectName("detailLib")

        self.dl_btn = QPushButton("Скачать и поставить")
        self.dl_btn.setObjectName("primaryBtn")
        self.dl_btn.setMinimumHeight(44)
        self.dl_btn.clicked.connect(self._download)

        desc_head = QLabel("Описание")
        desc_head.setObjectName("detailHead")
        tags_head = QLabel("Теги")
        tags_head.setObjectName("detailHead")

        side = QFrame()
        side.setObjectName("detailSide")
        side.setFixedWidth(340)
        side_l = QVBoxLayout(side)
        side_l.setContentsMargins(18, 18, 18, 18)
        side_l.setSpacing(10)
        side_l.addWidget(self.title_lbl)
        side_l.addWidget(self.info_lbl)
        side_l.addWidget(desc_head)
        side_l.addWidget(desc_scroll, 1)
        side_l.addWidget(tags_head)
        side_l.addWidget(self.tags_lbl)
        side_l.addWidget(self.library_lbl)
        side_l.addWidget(self.dl_btn)

        top = QHBoxLayout()
        top.addWidget(self.back_btn)
        top.addStretch(1)

        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self.preview_stack, 1)
        body.addWidget(side)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        root.addLayout(top)
        root.addLayout(body, 1)

    # ---- данные

    def show_card(self, pid: str, title: str, meta: dict, preview_url: str, video_url: str = ""):
        # Если открываем другие обои — старый кэшированный полноразмерный raw не подходит.
        if pid != self.pid:
            self.preview.clear_media()
            if self.video_preview is not None:
                self.video_preview.stop()

        self.pid = pid
        self.title_text = title
        self.title_lbl.setText(title)
        self.info_lbl.setText(self._info_html(meta))
        self.desc_lbl.setText(meta.get("description") or "Описание недоступно.")
        tags = meta.get("tags") or []
        self.tags_lbl.setText(" · ".join(tags) if tags else "—")
        self.refresh_state()

        if self.video_preview is not None and video_url:
            # У обоев есть собственный видео-ролик превью: проигрываем его в цикле.
            self.preview_stack.setCurrentWidget(self.video_preview)
            self.video_preview.play_url(video_url)
            return

        self.preview_stack.setCurrentWidget(self.preview)
        if self.video_preview is not None:
            self.video_preview.stop()

        raw = self.raw_cache.get(pid)
        if raw:
            self._show_raw(raw)
        elif preview_url:
            # Сначала показываем миниатюру из сетки (она уже загружена), полное превью догружаем фоном.
            thumb = self._thumb_provider(pid)
            if thumb is not None and not thumb.isNull():
                self.preview.set_still(thumb.toImage())
            else:
                self.preview.clear_media()
                self.preview.setText("Загрузка превью…")
            self.pool.start(FullImageJob(pid, preview_url, self.signals))
        else:
            self.preview.clear_media()
            self.preview.setText("Превью нет")

    def refresh_state(self):
        in_lib = bool(self.pid) and self._in_library(self.pid)
        self.dl_btn.setText("Установить" if in_lib else "Скачать и поставить")
        self.library_lbl.setText("✓ Уже в библиотеке" if in_lib else "Ещё не скачано")

    def stop(self):
        self.preview.clear_media()
        if self.video_preview is not None:
            self.video_preview.stop()

    # ---- внутреннее

    def _info_html(self, meta: dict) -> str:
        rows = [
            ("Автор", meta.get("author")),
            ("Тип", meta.get("type")),
            ("Разрешение", meta.get("resolution")),
            ("Размер", meta.get("size")),
        ]
        parts = []
        for label, value in rows:
            shown = html.escape(value) if value else "—"
            parts.append(
                f'<div style="margin:5px 0"><span style="color:#8b93a7">{label}</span>'
                f'<br><b style="color:#e6ebf5">{shown}</b></div>'
            )
        return "".join(parts)

    def _on_full(self, pid: str, raw: bytes):
        if pid != self.pid:
            return
        if not raw:
            # Полное превью не загрузилось: если миниатюра уже показана, оставляем её.
            if not self.raw_cache.get(pid) and self.preview.pixmap() is None:
                self.preview.setText("Не удалось загрузить превью")
            return
        self.raw_cache[pid] = raw
        self._show_raw(raw)

    def _show_raw(self, raw: bytes):
        if raw[:4] == b"GIF8" and self.preview.set_gif(raw):
            return
        img = QImage.fromData(raw)
        if img.isNull():
            self.preview.setText("Нет превью")
        else:
            self.preview.set_still(img)

    def _download(self):
        if self.pid:
            self._on_download(self.pid, self.title_text)


# ---------------------------------------------------------------- widget

class SteamBrowser(QWidget):
    """Галерея обоев.

    get_key()        -> str: ключ API (запасной вариант)
    set_key(key)     : сохранить ключ
    on_download(id, title): пользователь выбрал обои
    in_library(id)   -> bool: скачаны ли уже
    """

    def __init__(self, get_key, set_key, on_download, in_library, parent=None):
        super().__init__(parent)
        self._get_key = get_key
        self._set_key = set_key
        self._on_download_fn = on_download
        self._in_library = in_library

        self.setStyleSheet(build_qss())

        self.page = 1
        self.has_more = False
        self.loaded = 0
        self.loading = False
        self._gen = 0
        self._threads: list[QThread] = []
        self.cards: dict[str, QListWidgetItem] = {}
        self.titles: dict[str, str] = {}
        self.meta: dict[str, dict] = {}
        self.preview_urls: dict[str, str] = {}
        self.video_urls: dict[str, str] = {}
        self.icons: dict[str, QIcon] = {}
        self.anim: dict[str, bytes] = {}
        self.pending: set[str] = set()
        self._placeholder = _solid_icon("#262c3a")

        self._movie: QMovie | None = None
        self._movie_buf: QBuffer | None = None
        self._movie_pid: str | None = None

        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(THUMB_THREADS)
        self.thumb_signals = ThumbSignals()
        self.thumb_signals.loaded.connect(self._on_thumb)

        self.reload_timer = QTimer(self)
        self.reload_timer.setSingleShot(True)
        self.reload_timer.setInterval(300)
        self.reload_timer.timeout.connect(self._reset)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск обоев...")
        self.sort_box = QComboBox()
        self.sort_box.addItems([name for name, _, _ in SORTS])
        self.sort_box.setView(_popup_view())
        

        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.sort_box)
        

        self.filters = FilterBar()

        self.grid = FastGrid()
        self.grid.setObjectName("browserGrid")
        self.grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid.setIconSize(THUMB_SIZE)
        self.grid.setGridSize(CARD_SIZE)
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid.setMovement(QListWidget.Movement.Static)
        self.grid.setWordWrap(True)
        self.grid.setSpacing(8)
        self.grid.setUniformItemSizes(True)
        self.grid.setMouseTracking(True)
        self.grid.viewport().installEventFilter(self)

        self.info = QLabel("Выбери обои, чтобы посмотреть")
        self.info.setObjectName("browserInfo")
        self.more_btn = QPushButton("Показать ещё")
        self.download_btn = QPushButton("Открыть")
        self.download_btn.setObjectName("primaryBtn")
        self.download_btn.setEnabled(False)
        self.status = QLabel("")
        self.status.setObjectName("browserStatus")

        bottom = QHBoxLayout()
        bottom.addWidget(self.info, 1)
        bottom.addWidget(self.more_btn)
        bottom.addWidget(self.download_btn)

        self.list_page = QWidget()
        list_layout = QVBoxLayout(self.list_page)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.addLayout(top)
        list_layout.addWidget(self.filters)
        list_layout.addWidget(self.grid, 1)
        list_layout.addWidget(self.status)
        list_layout.addLayout(bottom)

        self.detail = DetailView(
            on_back=self._close_detail,
            on_download=self._on_download,
            in_library=self._in_library,
            thumb_provider=self._thumb_for,
        )

        self.stack = QStackedWidget()
        self.stack.addWidget(self.list_page)
        self.stack.addWidget(self.detail)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.stack)

        self.search.textChanged.connect(lambda _t: self.reload_timer.start())
        self.sort_box.currentIndexChanged.connect(lambda _i: self._reset())
        self.filters.changed.connect(lambda: self.reload_timer.start())
        
        self.more_btn.clicked.connect(self._load_next)
        self.grid.currentItemChanged.connect(self._on_select)
        self.grid.itemClicked.connect(self._on_card_clicked)
        self.grid.itemActivated.connect(self._on_card_clicked)
        self.grid.itemEntered.connect(self._on_hover)
        self.download_btn.clicked.connect(self._open_selected)
        self.grid.verticalScrollBar().valueChanged.connect(self._maybe_more)

        QTimer.singleShot(0, self._reset)

    # ---- публичное

    def refresh_badges(self) -> None:
        for pid in list(self.cards):
            self._refresh_label(pid)
        if self.grid.currentItem() is not None:
            self._on_select(self.grid.currentItem(), None)
        self.detail.refresh_state()

    # ---- миниатюра для страницы обоев

    def _thumb_for(self, pid: str):
        """Миниатюра из сетки как QPixmap (для мгновенного показа на странице обоев)."""
        icon = self.icons.get(pid)
        if icon is None:
            return None
        return icon.pixmap(THUMB_SIZE)

    # ---- клавиши

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.stack.currentWidget() is self.detail:
            self._close_detail()
            return
        super().keyPressEvent(event)

    # ---- ключ API

    def _change_key(self):
        text, ok = QInputDialog.getText(
            self, "Ключ Steam API",
            "Ключ нужен только как запасной вариант.\n"
            f"Получить можно на {KEY_HELP_URL}\n"
            "(в поле домена укажи любое слово, например localhost).",
        )
        if ok and text.strip():
            self._set_key(text.strip())
            self.status.setText("Ключ сохранён. Он будет использован, если страница Мастерской не разберётся.")

    # ---- страница обоев

    def _on_card_clicked(self, item: QListWidgetItem):
        pid = item.data(Qt.ItemDataRole.UserRole)
        if pid:
            self._open_detail(pid)

    def _open_selected(self):
        item = self.grid.currentItem()
        if item is not None:
            self._open_detail(item.data(Qt.ItemDataRole.UserRole))

    def _open_detail(self, pid: str):
        self._stop_anim()
        self.stack.setCurrentWidget(self.detail)
        self.detail.show_card(
            pid,
            self.titles.get(pid, pid),
            self.meta.get(pid, {}),
            self.preview_urls.get(pid, ""),
            self.video_urls.get(pid, ""),
        )

    def _close_detail(self):
        self.detail.stop()
        self.stack.setCurrentWidget(self.list_page)

    def _on_download(self, pid: str, title: str):
        self._on_download_fn(pid, title)

    # ---- загрузка списка

    def _reset(self):
        self._stop_anim()
        self._gen += 1
        self.loading = False
        self.page = 1
        self.has_more = False
        self.loaded = 0
        self.cards.clear()
        self.titles.clear()
        self.video_urls.clear()
        self.grid.clear()
        self.more_btn.hide()
        self.download_btn.setEnabled(False)
        self.info.setText("Выбери обои, чтобы посмотреть")
        self._load_next()

    def _load_next(self):
        if self.loading:
            return
        self.loading = True
        self.more_btn.setEnabled(False)
        self.status.setStyleSheet("")
        self.status.setText("Загружаю...")

        thread = QueryThread(
            (self._get_key() or "").strip(),
            self.page,
            self.search.text().strip(),
            self.sort_box.currentIndex(),
            self.filters.tags(),
        )
        gen = self._gen
        thread.done.connect(lambda cards, more, src, g=gen: self._on_page(g, cards, more, src))
        thread.fail.connect(lambda msg, g=gen: self._on_fail(g, msg))
        thread.finished.connect(lambda t=thread: self._drop_thread(t))
        self._threads.append(thread)
        thread.start()

    def _drop_thread(self, thread: QThread):
        if thread in self._threads:
            self._threads.remove(thread)

    def _on_page(self, gen: int, cards: list, has_more: bool, source: str):
        if gen != self._gen:
            return
        self.loading = False
        self.page += 1
        self.has_more = has_more
        for c in cards:
            pid = c["publishedfileid"]
            if pid in self.cards:
                continue
            self._add_card(pid, c["title"], c["preview_url"], c.get("meta", {}), c.get("video_url", ""))
            self.loaded += 1
        self.more_btn.setEnabled(True)
        self.more_btn.setVisible(self.has_more)
        self.status.setStyleSheet("")
        if self.loaded == 0:
            self.status.setText(f"Ничего не найдено ({source}). Попробуй убрать часть фильтров.")
        else:
            self.status.setText(f"Показано {self.loaded} · источник: {source}")
        QTimer.singleShot(0, self._maybe_more)

    def _on_fail(self, gen: int, msg: str):
        if gen != self._gen:
            return
        self.loading = False
        self.more_btn.setEnabled(True)
        self.status.setStyleSheet("color: #ff7b7b;")
        self.status.setText(msg)

    def _maybe_more(self, _value: int = 0):
        sb = self.grid.verticalScrollBar()
        remaining = sb.maximum() - sb.value()
        if self.has_more and not self.loading and remaining < PREFETCH_PX:
            self._load_next()

    # ---- карточки

    def _add_card(self, pid: str, title: str, preview_url: str, meta: dict, video_url: str = ""):
        self.titles[pid] = title
        self.meta[pid] = meta or {}
        self.preview_urls[pid] = preview_url
        self.video_urls[pid] = video_url
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, pid)
        item.setSizeHint(CARD_SIZE)
        item.setIcon(self.icons.get(pid, self._placeholder))
        self.grid.addItem(item)
        self.cards[pid] = item
        self._refresh_label(pid)

        if pid not in self.icons and preview_url and pid not in self.pending:
            self.pending.add(pid)
            self.pool.start(ThumbJob(pid, preview_url, self.thumb_signals))

    def _refresh_label(self, pid: str):
        item = self.cards.get(pid)
        if item is None:
            return
        badge = "\n✓ в библиотеке" if self._in_library(pid) else ""
        item.setText(self.titles.get(pid, pid) + badge)

    def _on_thumb(self, pid: str, img: QImage, raw: bytes):
        self.pending.discard(pid)
        if img.isNull():
            return
        icon = QIcon(QPixmap.fromImage(img))
        self.icons[pid] = icon
        if raw:
            self.anim[pid] = raw
        item = self.cards.get(pid)
        if item is not None and self._movie_pid != pid:
            item.setIcon(icon)

    # ---- анимация превью в списке при наведении

    def _on_hover(self, item: QListWidgetItem):
        pid = item.data(Qt.ItemDataRole.UserRole)
        if pid == self._movie_pid:
            return
        self._stop_anim()
        raw = self.anim.get(pid)
        if not raw:
            return
        buf = QBuffer(self)
        buf.setData(QByteArray(raw))
        buf.open(QIODevice.OpenModeFlag.ReadOnly)
        movie = QMovie(buf, QByteArray(b"gif"), self)
        if not movie.isValid():
            buf.close()
            buf.deleteLater()
            return
        movie.frameChanged.connect(lambda _n, p=pid, m=movie: self._on_frame(p, m))
        self._movie = movie
        self._movie_buf = buf
        self._movie_pid = pid
        movie.start()

    def _on_frame(self, pid: str, movie: QMovie):
        if movie is not self._movie:
            return
        frame = movie.currentImage()
        if frame.isNull():
            return
        frame = frame.scaled(
            THUMB_SIZE,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        item = self.cards.get(pid)
        if item is not None:
            item.setIcon(QIcon(QPixmap.fromImage(frame)))

    def _stop_anim(self):
        if self._movie is not None:
            self._movie.stop()
            self._movie.deleteLater()
            self._movie = None
        if self._movie_buf is not None:
            self._movie_buf.close()
            self._movie_buf.deleteLater()
            self._movie_buf = None
        pid = self._movie_pid
        self._movie_pid = None
        if pid and pid in self.cards and pid in self.icons:
            self.cards[pid].setIcon(self.icons[pid])

    def eventFilter(self, obj, event):
        if obj is self.grid.viewport() and event.type() == QEvent.Type.Leave:
            self._stop_anim()
        return super().eventFilter(obj, event)

    # ---- выбор

    def _on_select(self, current, _previous):
        if current is None:
            self.download_btn.setEnabled(False)
            self.info.setText("Выбери обои, чтобы посмотреть")
            return
        pid = current.data(Qt.ItemDataRole.UserRole)
        state = "уже в библиотеке" if self._in_library(pid) else "ещё не скачаны"
        self.info.setText(f"{self.titles.get(pid, pid)} · {state}")
        self.download_btn.setEnabled(True)