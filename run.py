"""PyInstaller onefile entry point.

soundboard/main.py does `from soundboard import ...`, which needs the project
root importable as a package parent. PyInstaller's Analysis only guarantees
that for the entry script's own directory, so this tiny top-level launcher
(not soundboard/main.py itself) is what build.spec points at.

Also runnable directly in dev: `venv\\Scripts\\python.exe run.py`."""

from soundboard.main import main

if __name__ == "__main__":
    main()
