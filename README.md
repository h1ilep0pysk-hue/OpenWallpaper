# OpenWallpaper

Бесплатный открытый аналог Wallpaper Engine для Windows. Ставит видео-обои и сцены из Мастерской Steam на рабочий стол через mpv.

> Проект не связан с Valve и Wallpaper Engine. Wallpaper Engine — коммерческий продукт, его название упоминается только для описания аналога. Приложение не содержит кода Wallpaper Engine.

## Возможности

- Галерея Мастерской Steam внутри приложения: поиск, сортировка, фильтры по типу, возрасту, разрешению и жанрам.
- Превью обоев с проигрыванием видео-роликов и анимированных GIF.
- Загрузка обоев через SteamCMD с логином своего аккаунта Steam.
- Воспроизведение видео на рабочем столе через mpv (зацикленно, без звука).
- Распаковка сцен `.pkg` через RePKG (видео и изображения из сцены).
- Режимы подгонки: заполнить, вписать, растянуть.
- Библиотека скачанных обоев с поиском и удалением.
- Работа в системном трее: остановка обоев и выход.

## Ограничения

- Интерактивные сцены Wallpaper Engine (шейдеры, частицы, скрипты) без готового видео не воспроизводятся. Их нужно открывать в Wallpaper Engine.
- Веб-обои пока не поддерживаются.
- Для загрузки нужен аккаунт Steam, который владеет Wallpaper Engine. Скачиваются только те обои, которые доступны этому аккаунту.

## Требования

- Windows 10/11
- Python 3.11+ (если запускаешь из исходников)
- Аккаунт Steam с Wallpaper Engine

## Установка из исходников

git clone https://github.com/h1ilep0pysk-hue/OpenWallpaper.git
cd OpenWallpaper
pip install PySide6

Положи в папку `bin/`:

bin/
├── mpv.exe
├── RePKG.exe
└── ...dll из сборки mpv

`steamcmd` скачивается автоматически при первом запуске в `bin/steamcmd/`.

Запуск:

python orgen.py

## Сборка exe

pip install pyinstaller PySide6
pyinstaller --noconsole --onefile --icon=logo.ico --name OpenWallpaper --hidden-import steam_browser orgen.py

Результат: `dist/OpenWallpaper.exe`. Рядом с exe положи `logo.ico` и папку `bin/` (с `mpv.exe`, `RePKG.exe` и dll). Конфиг `orgen_config.json` и лог `orgen_debug.log` создаются рядом с exe.

## Настройка

При первом скачивании приложение спросит логин и пароль Steam и сохранит их в `orgen_config.json`. Пароль хранится в открытом виде, поэтому этот файл нельзя выкладывать в репозиторий.

Добавь в `.gitignore`:

orgen_config.json
orgen_debug.log
mpv.log
projects/
dist/
build/
bin/steamcmd/

## Безопасность

- Не коммить `orgen_config.json`: в нём логин и пароль Steam.
- По возможности используй отдельный аккаунт Steam.

## Компоненты

mpv, RePKG и SteamCMD распространяются по своим лицензиям.
