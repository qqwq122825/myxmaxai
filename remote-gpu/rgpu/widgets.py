"""Small native Tk controls matching the desktop application's light palette."""
from __future__ import annotations

import tkinter as tk


class ScrollRail(tk.Canvas):
    """An arrowless scroll rail with a rounded, draggable thumb."""

    def __init__(self, parent, command, **kwargs):
        super().__init__(parent, width=10, bg="#ffffff", highlightthickness=0,
                         borderwidth=0, cursor="arrow", **kwargs)
        self.command = command
        self.first, self.last = 0.0, 1.0
        self.hover = self.dragging = False
        self.offset = 0.0
        self.bind("<Configure>", self._draw)
        self.bind("<Enter>", lambda _: self._hover(True))
        self.bind("<Leave>", lambda _: self._hover(False))
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)

    def set(self, first, last):
        self.first, self.last = float(first), float(last)
        self._draw()

    def _geometry(self):
        height = max(1, self.winfo_height() - 8)
        size = min(height, max(28, height * (self.last - self.first)))
        travel = height - size
        span = max(0, 1 - (self.last - self.first))
        top = 4 + (self.first / span * travel if span else 0)
        return top, size, travel, span

    def _draw(self, _event=None):
        self.delete("thumb")
        if self.last - self.first >= .999:
            return  # Keep the layout width stable; hide the rail when all content fits.
        top, size, _, _ = self._geometry()
        self.create_line(5, top + 3, 5, top + size - 3, width=6,
                         capstyle="round", tags="thumb",
                         fill="#9eacc1" if self.hover or self.dragging else "#d0d8e5")

    def _hover(self, value):
        self.hover = value
        self._draw()

    def _press(self, event):
        if self.last - self.first >= .999:
            return
        top, size, _, _ = self._geometry()
        self.offset = event.y - top if top <= event.y <= top + size else size / 2
        self.dragging = True
        self._drag(event)

    def _drag(self, event):
        if not self.dragging:
            return
        _, _, travel, span = self._geometry()
        fraction = min(span, max(0, (event.y - self.offset - 4) / max(1, travel) * span))
        self.command("moveto", fraction)

    def _release(self, _event):
        self.dragging = False
        self._draw()


def bind_form_wheel(root, canvas, rail):
    """Route wheel input over form children, without changing combobox selections.

    A per-window bind tag avoids global hooks and excludes the combobox popup.
    Pixel-sized canvas units also preserve small touchpad deltas.
    """
    tag = f"FormWheel:{root}"
    remainder = 0.0

    def scroll(event):
        nonlocal remainder
        if rail.last - rail.first < .999:
            remainder += -event.delta * 48 / 120
            pixels = int(remainder)
            remainder -= pixels
            if pixels:
                canvas.yview_scroll(pixels, "units")
        return "break"

    root.bind_class(tag, "<MouseWheel>", scroll)

    def attach(widget):
        widget.bindtags((tag,) + tuple(t for t in widget.bindtags() if t != tag))
        for child in widget.winfo_children():
            attach(child)

    attach(canvas)
    attach(rail)
