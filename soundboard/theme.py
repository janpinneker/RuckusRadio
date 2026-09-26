"""Design tokens — single source of truth for colors, type and sizes."""

from __future__ import annotations

from functools import lru_cache

BG = "#101014"
PANEL = "#17171C"
RAISED = "#1F1F27"
LINE = "#2A2A34"
TEXT = "#EDEDF2"
MUTED = "#8B8B95"
ACCENT = "#22D3EE"
ACCENT_INK = "#0B0B0D"
DANGER = "#F97316"
DANGER_HOVER = "#C2410C"
GRAD_FROM = "#7C3AED"
GRAD_TO = "#22D3EE"

DISPLAY_FAMILIES = ("Bahnschrift SemiBold", "Bahnschrift SemiBold Condensed", "Bahnschrift")
BODY_FAMILIES = ("Segoe UI Variable Text", "Segoe UI")
MONO_FAMILIES = ("Cascadia Mono", "Consolas")

# Pillow needs font files, not family names.
DISPLAY_FILES = ("bahnschrift.ttf", "segoeuib.ttf")
MONO_FILES = ("CascadiaMono.ttf", "consola.ttf")

SIZE_WORDMARK = 28
SIZE_SECTION = 15
SIZE_BODY = 13
SIZE_TILE_NAME = 12
SIZE_KEYCAP = 10

TILE_SIZE = 88
COLUMNS = 6
RAIL_WIDTH = 72
BANNER_HEIGHT = 110
DOCK_HEIGHT = 56


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


@lru_cache(maxsize=None)
def _installed_families() -> frozenset[str]:
    import tkinter.font as tkfont

    return frozenset(tkfont.families())  # requires an existing Tk root


def pick_family(candidates: tuple[str, ...]) -> str:
    installed = _installed_families()
    for name in candidates:
        if name in installed:
            return name
    return candidates[-1]


def display_font(size: int, weight: str = "normal"):
    import customtkinter as ctk

    return ctk.CTkFont(family=pick_family(DISPLAY_FAMILIES), size=size, weight=weight)


def body_font(size: int = SIZE_BODY, weight: str = "normal"):
    import customtkinter as ctk

    return ctk.CTkFont(family=pick_family(BODY_FAMILIES), size=size, weight=weight)


def mono_font(size: int = SIZE_KEYCAP, weight: str = "normal"):
    import customtkinter as ctk

    return ctk.CTkFont(family=pick_family(MONO_FAMILIES), size=size, weight=weight)
