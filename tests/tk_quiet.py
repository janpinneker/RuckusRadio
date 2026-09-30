"""Keeps test Tk windows off the screen: `install()` before building any window.

Tests build the real app, its dialogs and the onboarding window. Shown windows pop up
over whatever runs (a fullscreen game), and the onboarding's focus_force pulls itself to
the front. Here every Tk/Toplevel is fully transparent and taskbar-free from birth,
focus_force/lift/tkraise do nothing - map/withdraw/state() keep working unchanged."""

import tkinter as tk

_installed = False


def _hide(win: tk.Wm) -> None:
    win.wm_attributes("-alpha", 0.0)
    win.wm_attributes("-toolwindow", True)


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    tk_init, toplevel_init = tk.Tk.__init__, tk.Toplevel.__init__

    def quiet_tk_init(self, *args, **kwargs):
        tk_init(self, *args, **kwargs)
        _hide(self)

    def quiet_toplevel_init(self, *args, **kwargs):
        toplevel_init(self, *args, **kwargs)
        _hide(self)

    def no_op(self, *args, **kwargs):
        return None

    tk.Tk.__init__ = quiet_tk_init
    tk.Toplevel.__init__ = quiet_toplevel_init
    tk.Misc.focus_force = no_op
    tk.Misc.tkraise = no_op
    tk.Misc.lift = no_op
