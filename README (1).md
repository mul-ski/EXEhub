# EXE//HUB

Console-style game launcher for Windows. Borderless fullscreen, carousel or grid, controller support, and a live Spotify card.

## Controls
| Input | Action |
|---|---|
| Arrows / D-pad / left stick | Browse |
| Enter / A | Play |
| Tab / Back button | Switch carousel / grid |
| F11 / Start | Fullscreen / windowed |
| Del, F2, Menu key / Y | Remove, edit, options |
| Ctrl+O / X | Add games (or drop `.exe` files on the window) |
| Type | Search |
| Esc / B | Back |

The Spotify card reads Windows' media session: no login, no API key.

## Run from source
```
py -m venv venv
venv\Scripts\python -m pip install -r requirements.txt
venv\Scripts\python exehub.py
```
Or use `run.bat` after the first `build.bat` has created the venv.

## Build the exe
Run `build.bat`. The result is `dist\EXEHUB.exe`. Build output is not tracked by git; ship the exe through GitHub Releases.

## Data
Library, icons, backups and logs live in `%APPDATA%\exehub`.
