"""Small shared widgets for the GTK4 UI."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402


class Form(Gtk.Grid):
    """Two-column label/widget grid standing in for Qt's QFormLayout."""

    def __init__(self) -> None:
        super().__init__(row_spacing=10, column_spacing=12)
        self.set_margin_top(16)
        self.set_margin_bottom(16)
        self.set_margin_start(16)
        self.set_margin_end(16)
        self._row = 0

    def add(self, label_text: str, widget: Gtk.Widget) -> Gtk.Label:
        """Add a labelled row; returns the label so callers can hide the pair."""
        label = Gtk.Label(label=label_text, xalign=1.0)
        label.add_css_class("dim-label")
        label.set_valign(Gtk.Align.CENTER)
        self.attach(label, 0, self._row, 1, 1)
        widget.set_hexpand(True)
        self.attach(widget, 1, self._row, 1, 1)
        self._row += 1
        return label

    def add_wide(self, widget: Gtk.Widget) -> None:
        widget.set_hexpand(True)
        self.attach(widget, 0, self._row, 2, 1)
        self._row += 1

    def add_separator(self) -> None:
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_margin_top(4)
        sep.set_margin_bottom(4)
        self.add_wide(sep)


def hint(text: str) -> Gtk.Label:
    """Dimmed, wrapped explanatory text."""
    label = Gtk.Label(label=text, xalign=0.0)
    label.set_wrap(True)
    label.add_css_class("dim-label")
    return label


def entry(placeholder: str = "") -> Gtk.Entry:
    e = Gtk.Entry()
    if placeholder:
        e.set_placeholder_text(placeholder)
    return e


def spin(lo: int, hi: int, value: int, step: int = 1) -> Gtk.SpinButton:
    s = Gtk.SpinButton.new_with_range(lo, hi, step)
    s.set_value(value)
    s.set_hexpand(False)
    s.set_halign(Gtk.Align.START)
    return s


def toolbar_button(icon_name: str, tooltip: str) -> Gtk.Button:
    btn = Gtk.Button.new_from_icon_name(icon_name)
    btn.set_tooltip_text(tooltip)
    btn.set_has_frame(False)
    return btn


def error_dialog(parent: Gtk.Window, message: str, detail: str = "") -> None:
    """Show a non-blocking error alert."""
    dialog = Gtk.AlertDialog()
    dialog.set_modal(True)
    dialog.set_message(message)
    if detail:
        dialog.set_detail(detail)
    dialog.set_buttons(["OK"])
    dialog.show(parent)
