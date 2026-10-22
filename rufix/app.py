"""Rufix GTK3 user interface (Rufus-style layout)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from typing import List, Optional
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import APP_ID, APP_NAME, __version__  # noqa: E402
from .devices import Device, human_size, list_devices, mountable_volumes  # noqa: E402
from .imageinfo import ImageInfo, inspect  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
INSTALLED_HELPER = "/usr/lib/rufix/rufix-helper"
SOURCE_HELPER = os.path.join(HERE, "helper.py")
SETTINGS_FILE = os.path.join(GLib.get_user_config_dir(), "rufix", "settings.json")

FS_LABELS = {
    "fat32": "FAT32",
    "fat16": "FAT",
    "exfat": "exFAT",
    "ntfs": "NTFS",
    "ext4": "ext4",
}

K = 1024
CLUSTERS = {
    "fat32": [512, 1 * K, 2 * K, 4 * K, 8 * K, 16 * K, 32 * K, 64 * K],
    "fat16": [512, 1 * K, 2 * K, 4 * K, 8 * K, 16 * K, 32 * K, 64 * K],
    "exfat": [4 * K, 8 * K, 16 * K, 32 * K, 64 * K, 128 * K, 256 * K, 512 * K, 1024 * K],
    "ntfs": [512, 1 * K, 2 * K, 4 * K, 8 * K, 16 * K, 32 * K, 64 * K],
    "ext4": [1 * K, 2 * K, 4 * K],
}

CSS = b"""
.section-title { font-weight: bold; font-size: 115%; }
.section-sep { margin-top: 4px; }
.status-bar { padding: 4px 8px; }
progressbar.ready progress { background-color: #4caf50; border-color: #3e8e41; }
progressbar.failed progress { background-color: #d9534f; border-color: #b52b27; }
progressbar trough, progressbar progress { min-height: 22px; }
.start-button { font-weight: bold; }
"""


def cluster_text(n: int) -> str:
    return f"{n} bytes" if n < K else f"{n // K} kilobytes"


def section(title: str) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    box.get_style_context().add_class("section-sep")
    label = Gtk.Label(label=title, xalign=0)
    label.get_style_context().add_class("section-title")
    sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
    sep.set_valign(Gtk.Align.CENTER)
    box.pack_start(label, False, False, 0)
    box.pack_start(sep, True, True, 0)
    return box


def field_label(text: str) -> Gtk.Label:
    lbl = Gtk.Label(label=text, xalign=0)
    lbl.set_margin_top(4)
    return lbl


def helper_command() -> List[str]:
    if os.path.exists(INSTALLED_HELPER):
        helper = [INSTALLED_HELPER]
    else:
        helper = [sys.executable, SOURCE_HELPER]
    if os.geteuid() == 0:
        return helper
    return ["pkexec"] + helper


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except OSError:
        pass


class LogWindow(Gtk.Window):
    def __init__(self, parent: Gtk.Window):
        super().__init__(title=f"{APP_NAME} Log")
        self.set_transient_for(parent)
        self.set_default_size(640, 400)
        self.connect("delete-event", lambda *_: self.hide() or True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(8)
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        self.view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.buffer = self.view.get_buffer()
        scroll.add(self.view)
        box.pack_start(scroll, True, True, 0)

        buttons = Gtk.Box(spacing=6)
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", lambda *_: self.buffer.set_text(""))
        save = Gtk.Button(label="Save")
        save.connect("clicked", self._save)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.hide())
        buttons.pack_end(close, False, False, 0)
        buttons.pack_end(save, False, False, 0)
        buttons.pack_end(clear, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        self.add(box)

    def append(self, text: str):
        stamp = time.strftime("%H:%M:%S")
        end = self.buffer.get_end_iter()
        self.buffer.insert(end, f"[{stamp}] {text}\n")
        mark = self.buffer.create_mark(None, self.buffer.get_end_iter(), False)
        self.view.scroll_mark_onscreen(mark)
        self.buffer.delete_mark(mark)

    def _save(self, *_):
        dlg = Gtk.FileChooserDialog(title="Save log", parent=self,
                                    action=Gtk.FileChooserAction.SAVE)
        dlg.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Save", Gtk.ResponseType.ACCEPT)
        dlg.set_do_overwrite_confirmation(True)
        dlg.set_current_name("rufix.log")
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            start, end = self.buffer.get_bounds()
            with open(dlg.get_filename(), "w") as f:
                f.write(self.buffer.get_text(start, end, False))
        dlg.destroy()


class ChecksumDialog(Gtk.Dialog):
    """Rufus' '#' button: MD5 / SHA-1 / SHA-256 of the selected image."""

    def __init__(self, parent: Gtk.Window, path: str):
        super().__init__(title="Checksums", transient_for=parent, modal=True)
        self.add_button("_Close", Gtk.ResponseType.CLOSE)
        self.set_default_size(560, -1)
        self.path = path
        self.cancelled = False
        area = self.get_content_area()
        area.set_spacing(8)
        area.set_border_width(12)
        area.add(Gtk.Label(label=os.path.basename(path), xalign=0))
        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        self.values = {}
        for row, name in enumerate(("MD5", "SHA-1", "SHA-256")):
            grid.attach(Gtk.Label(label=name + ":", xalign=0), 0, row, 1, 1)
            val = Gtk.Label(label="…", xalign=0, selectable=True)
            val.set_ellipsize(Pango.EllipsizeMode.NONE)
            val.get_style_context().add_class("monospace")
            val.set_attributes(self._mono())
            grid.attach(val, 1, row, 1, 1)
            self.values[name] = val
        area.add(grid)
        self.bar = Gtk.ProgressBar(show_text=True)
        area.add(self.bar)
        self.connect("response", self._on_response)
        self.show_all()
        threading.Thread(target=self._compute, daemon=True).start()

    @staticmethod
    def _mono():
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_family_new("monospace"))
        return attrs

    def _on_response(self, *_):
        self.cancelled = True
        self.destroy()

    def _compute(self):
        hashes = {"MD5": hashlib.md5(), "SHA-1": hashlib.sha1(), "SHA-256": hashlib.sha256()}
        total = max(os.path.getsize(self.path), 1)
        done = 0
        last = 0.0
        with open(self.path, "rb") as f:
            while not self.cancelled:
                data = f.read(4 << 20)
                if not data:
                    break
                for h in hashes.values():
                    h.update(data)
                done += len(data)
                if time.time() - last > 0.1:
                    last = time.time()
                    GLib.idle_add(self.bar.set_fraction, done / total)
        if self.cancelled:
            return

        def finish():
            if self.cancelled:
                return
            for name, h in hashes.items():
                self.values[name].set_text(h.hexdigest())
            self.bar.set_fraction(1.0)
            self.bar.set_text("Done")
        GLib.idle_add(finish)


class WindowsOptionsDialog(Gtk.Dialog):
    """Rufus' 'Windows User Experience' dialog."""

    def __init__(self, parent: Gtk.Window, defaults: Optional[dict] = None):
        super().__init__(title="Windows User Experience", transient_for=parent, modal=True)
        defaults = defaults or {}
        self.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("_OK", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        area = self.get_content_area()
        area.set_spacing(8)
        area.set_border_width(12)
        area.add(Gtk.Label(label="Customize Windows installation?", xalign=0))
        self.hw = Gtk.CheckButton(
            label="Remove requirement for 4GB+ RAM, Secure Boot and TPM 2.0")
        self.msa = Gtk.CheckButton(
            label="Remove requirement for an online Microsoft account")
        self.tele = Gtk.CheckButton(label="Disable data collection (Skip privacy questions)")
        self.hw.set_active(defaults.get("bypass_hw", True))
        self.msa.set_active(defaults.get("bypass_msa", True))
        self.tele.set_active(defaults.get("no_telemetry", False))
        for w in (self.hw, self.msa, self.tele):
            area.add(w)
        self.show_all()

    def options(self) -> dict:
        return {"bypass_hw": self.hw.get_active(), "bypass_msa": self.msa.get_active(),
                "no_telemetry": self.tele.get_active()}


class RufixWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title=f"{APP_NAME} {__version__}")
        self.set_default_size(500, -1)
        self.set_resizable(False)
        if Gtk.IconTheme.get_default().has_icon("rufix"):
            self.set_icon_name("rufix")
        else:
            src_icon = os.path.join(os.path.dirname(HERE), "data", "rufix.svg")
            if os.path.exists(src_icon):
                self.set_icon_from_file(src_icon)

        self.devices: List[Device] = []
        self.device_sig = None
        self.image: Optional[ImageInfo] = None
        self.proc: Optional[subprocess.Popen] = None
        self.busy = False
        self.start_time = 0.0
        self.got_done = False
        self.settings = load_settings()
        self.last_dir = self.settings.get("last_dir") or os.path.expanduser("~/Downloads")
        self.win_opts = self.settings.get("win_opts") or {}
        self.log_window = LogWindow(self)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._build()
        self.connect("delete-event", self._on_delete)
        self._setup_dnd()

        self.log(f"{APP_NAME} {__version__} started")
        self.refresh_devices(force=True)
        self._restore_settings()
        GLib.timeout_add_seconds(2, self._poll_devices)

    # ------------------------------------------------------------ settings
    def _restore_settings(self):
        s = self.settings
        # Each setter fires "changed" handlers that repopulate later widgets,
        # so restore in dependency order: image -> mode -> fs -> cluster.
        self.usb_hdd_check.set_active(s.get("usb_hdd", False))
        self.verify_check.set_active(s.get("verify", True))
        self.quick_check.set_active(s.get("quick", True))
        self.bb_check.set_active(s.get("badblocks", False))
        self.bb_combo.set_active_id(str(s.get("badblocks_passes", 1)))
        image = s.get("image")
        if s.get("boot") == "none":
            self.boot_combo.set_active_id("none")
        elif image and os.path.isfile(image):
            self.load_image(image)
        if s.get("mode"):
            self.mode_combo.set_active_id(s["mode"])
        if s.get("scheme"):
            self.scheme_combo.set_active_id(s["scheme"])
        self._update_state()
        if s.get("filesystem"):
            self.fs_combo.set_active_id(s["filesystem"])
        if s.get("cluster") is not None:
            self.cluster_combo.set_active_id(str(s["cluster"]))
        if s.get("label") is not None:
            self.label_entry.set_text(s["label"])
        self._update_state()

    def _save_settings(self):
        save_settings({
            "boot": self.boot_combo.get_active_id(),
            "image": self.image.path if self.image else None,
            "last_dir": self.last_dir,
            "mode": self.mode_combo.get_active_id(),
            "scheme": self.scheme_combo.get_active_id(),
            "filesystem": self.fs_combo.get_active_id(),
            "cluster": int(self.cluster_combo.get_active_id() or 0),
            "label": self.label_entry.get_text(),
            "usb_hdd": self.usb_hdd_check.get_active(),
            "verify": self.verify_check.get_active(),
            "quick": self.quick_check.get_active(),
            "badblocks": self.bb_check.get_active(),
            "badblocks_passes": int(self.bb_combo.get_active_id() or 1),
            "win_opts": self.win_opts,
        })

    # ------------------------------------------------------------------ UI
    def _build(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        main.set_border_width(12)
        outer.pack_start(main, True, True, 0)

        # ---- Drive Properties
        main.pack_start(section("Drive Properties"), False, False, 0)

        main.pack_start(field_label("Device"), False, False, 0)
        self.device_combo = Gtk.ComboBoxText()
        self.device_combo.connect("changed", self._on_device_changed)
        main.pack_start(self.device_combo, False, False, 0)

        main.pack_start(field_label("Boot selection"), False, False, 0)
        row = Gtk.Box(spacing=6)
        self.boot_combo = Gtk.ComboBoxText()
        self.boot_combo.append("image", "Disk or ISO image (Please select)")
        self.boot_combo.append("none", "Non bootable")
        self.boot_combo.set_active_id("image")
        self.boot_combo.connect("changed", lambda *_: self._update_state())
        self.hash_btn = Gtk.Button(label="#")
        self.hash_btn.set_tooltip_text("Compute image checksums (MD5, SHA-1, SHA-256)")
        self.hash_btn.connect("clicked", self._on_hash)
        self.select_btn = Gtk.Button(label="SELECT")
        self.select_btn.connect("clicked", self._on_select_image)
        row.pack_start(self.boot_combo, True, True, 0)
        row.pack_start(self.hash_btn, False, False, 0)
        row.pack_start(self.select_btn, False, False, 0)
        main.pack_start(row, False, False, 0)

        self.mode_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.mode_box.pack_start(field_label("Image option"), False, False, 0)
        self.mode_combo = Gtk.ComboBoxText()
        self.mode_combo.connect("changed", lambda *_: self._update_state())
        self.mode_box.pack_start(self.mode_combo, False, False, 0)
        for child in self.mode_box.get_children():
            child.show()
        self.mode_box.set_no_show_all(True)
        main.pack_start(self.mode_box, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=4, column_homogeneous=True)
        grid.attach(field_label("Partition scheme"), 0, 0, 1, 1)
        grid.attach(field_label("Target system"), 1, 0, 1, 1)
        self.scheme_combo = Gtk.ComboBoxText()
        self.scheme_combo.append("mbr", "MBR")
        self.scheme_combo.append("gpt", "GPT")
        self.scheme_combo.set_active_id("mbr")
        self.scheme_combo.connect("changed", lambda *_: self._update_state())
        self.target_combo = Gtk.ComboBoxText()
        self.target_combo.set_sensitive(False)
        grid.attach(self.scheme_combo, 0, 1, 1, 1)
        grid.attach(self.target_combo, 1, 1, 1, 1)
        main.pack_start(grid, False, False, 0)

        adv_drive = Gtk.Expander(label="Show advanced drive properties")
        adv_drive.set_margin_top(4)
        adv_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        adv_box.set_margin_start(12)
        self.usb_hdd_check = Gtk.CheckButton(label="List USB Hard Drives")
        self.usb_hdd_check.connect("toggled", lambda *_: self.refresh_devices(force=True))
        self.verify_check = Gtk.CheckButton(label="Verify device after writing (DD mode)")
        self.verify_check.set_active(True)
        adv_box.pack_start(self.usb_hdd_check, False, False, 0)
        adv_box.pack_start(self.verify_check, False, False, 0)
        adv_drive.add(adv_box)
        main.pack_start(adv_drive, False, False, 0)

        # ---- Format Options
        main.pack_start(section("Format Options"), False, False, 6)
        main.pack_start(field_label("Volume label"), False, False, 0)
        self.label_entry = Gtk.Entry()
        main.pack_start(self.label_entry, False, False, 0)

        grid2 = Gtk.Grid(column_spacing=12, row_spacing=4, column_homogeneous=True)
        grid2.attach(field_label("File system"), 0, 0, 1, 1)
        grid2.attach(field_label("Cluster size"), 1, 0, 1, 1)
        self.fs_combo = Gtk.ComboBoxText()
        self.fs_combo.connect("changed", self._on_fs_changed)
        self.cluster_combo = Gtk.ComboBoxText()
        grid2.attach(self.fs_combo, 0, 1, 1, 1)
        grid2.attach(self.cluster_combo, 1, 1, 1, 1)
        main.pack_start(grid2, False, False, 0)

        adv_fmt = Gtk.Expander(label="Show advanced format options")
        adv_fmt.set_margin_top(4)
        fmt_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        fmt_box.set_margin_start(12)
        self.quick_check = Gtk.CheckButton(label="Quick format")
        self.quick_check.set_active(True)
        bb_row = Gtk.Box(spacing=6)
        self.bb_check = Gtk.CheckButton(label="Check device for bad blocks")
        self.bb_combo = Gtk.ComboBoxText()
        for n in (1, 2, 3, 4):
            self.bb_combo.append(str(n), f"{n} pass" + ("es" if n > 1 else ""))
        self.bb_combo.set_active_id("1")
        self.bb_check.connect("toggled", lambda *_: self._update_state())
        bb_row.pack_start(self.bb_check, False, False, 0)
        bb_row.pack_start(self.bb_combo, False, False, 0)
        fmt_box.pack_start(self.quick_check, False, False, 0)
        fmt_box.pack_start(bb_row, False, False, 0)
        adv_fmt.add(fmt_box)
        main.pack_start(adv_fmt, False, False, 0)

        # ---- Status
        main.pack_start(section("Status"), False, False, 6)
        self.progress = Gtk.ProgressBar(show_text=True)
        self.progress.set_text("READY")
        self.progress.set_margin_top(4)
        main.pack_start(self.progress, False, False, 0)

        buttons = Gtk.Box(spacing=6)
        buttons.set_margin_top(10)
        log_btn = Gtk.Button.new_from_icon_name("text-x-generic-symbolic", Gtk.IconSize.BUTTON)
        log_btn.set_tooltip_text("Show log")
        log_btn.connect("clicked", lambda *_: self.log_window.show_all() or self.log_window.present())
        about_btn = Gtk.Button.new_from_icon_name("help-about-symbolic", Gtk.IconSize.BUTTON)
        about_btn.set_tooltip_text("About")
        about_btn.connect("clicked", self._on_about)
        self.start_btn = Gtk.Button(label="START")
        self.start_btn.get_style_context().add_class("start-button")
        self.start_btn.get_style_context().add_class("suggested-action")
        self.start_btn.set_size_request(100, -1)
        self.start_btn.connect("clicked", self._on_start)
        self.close_btn = Gtk.Button(label="CLOSE")
        self.close_btn.set_size_request(100, -1)
        self.close_btn.connect("clicked", self._on_close_clicked)
        buttons.pack_start(log_btn, False, False, 0)
        buttons.pack_start(about_btn, False, False, 0)
        buttons.pack_end(self.close_btn, False, False, 0)
        buttons.pack_end(self.start_btn, False, False, 0)
        main.pack_start(buttons, False, False, 0)

        # ---- Status bar
        outer.pack_start(Gtk.Separator(), False, False, 0)
        bar = Gtk.Box(spacing=6)
        bar.get_style_context().add_class("status-bar")
        self.status_label = Gtk.Label(xalign=0)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.timer_label = Gtk.Label(label="00:00:00")
        bar.pack_start(self.status_label, True, True, 0)
        bar.pack_end(self.timer_label, False, False, 0)
        outer.pack_start(bar, False, False, 0)

        self.add(outer)
        self.controls = [self.device_combo, self.boot_combo, self.select_btn, self.hash_btn,
                         self.mode_combo, self.scheme_combo, self.usb_hdd_check,
                         self.verify_check, self.label_entry, self.fs_combo,
                         self.cluster_combo, self.quick_check, self.bb_check, self.bb_combo]
        self._update_state()

    def _setup_dnd(self):
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self._on_drop)

    # ------------------------------------------------------------ helpers
    def log(self, text: str):
        self.log_window.append(text)

    def set_status(self, text: str):
        self.status_label.set_text(text)

    @property
    def device(self) -> Optional[Device]:
        path = self.device_combo.get_active_id()
        return next((d for d in self.devices if d.path == path), None)

    @property
    def bootable(self) -> bool:
        return self.boot_combo.get_active_id() == "image"

    @property
    def mode(self) -> str:
        if not self.bootable:
            return "format"
        return self.mode_combo.get_active_id() or "dd"

    # ------------------------------------------------------------ devices
    def _poll_devices(self):
        if not self.busy:
            self.refresh_devices()
        return True

    def refresh_devices(self, force: bool = False):
        devices = list_devices(self.usb_hdd_check.get_active())
        sig = [(d.path, d.size, d.display_name) for d in devices]
        if sig == self.device_sig and not force:
            return
        self.device_sig = sig
        current = self.device_combo.get_active_id()
        self.devices = devices
        self.device_combo.remove_all()
        for d in devices:
            self.device_combo.append(d.path, d.display_name)
        if devices:
            if current and any(d.path == current for d in devices):
                self.device_combo.set_active_id(current)
            else:
                self.device_combo.set_active(0)
        n = len(devices)
        self.set_status(f"{n} device{'s' if n != 1 else ''} found" if n else
                        "No USB drives found - plug one in")
        self._update_state()

    def _on_device_changed(self, *_):
        dev = self.device
        if dev and not (self.bootable and self.image):
            self.label_entry.set_text(dev.display_label or "")
        self._populate_fs()
        self._update_state()

    # ------------------------------------------------------------ image
    def _on_select_image(self, *_):
        dlg = Gtk.FileChooserNative(title="Select disk image", transient_for=self,
                                    action=Gtk.FileChooserAction.OPEN)
        flt = Gtk.FileFilter()
        flt.set_name("Disk images (*.iso, *.img, *.img.xz, ...)")
        for pat in ("*.iso", "*.ISO", "*.img", "*.IMG", "*.raw", "*.bin", "*.img.xz",
                    "*.img.gz", "*.img.bz2", "*.xz", "*.gz", "*.bz2"):
            flt.add_pattern(pat)
        dlg.add_filter(flt)
        all_f = Gtk.FileFilter()
        all_f.set_name("All files")
        all_f.add_pattern("*")
        dlg.add_filter(all_f)
        if os.path.isdir(self.last_dir):
            dlg.set_current_folder(self.last_dir)
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            self.load_image(dlg.get_filename())
        dlg.destroy()

    def _on_drop(self, _w, _ctx, _x, _y, data, _info, _time):
        uris = data.get_uris()
        if uris:
            path = unquote(urlparse(uris[0]).path)
            if os.path.isfile(path):
                self.load_image(path)

    def load_image(self, path: str):
        try:
            info = inspect(path)
        except OSError as e:
            self.error(f"Cannot read image: {e}")
            return
        if info.kind == "unknown":
            self.error("This file is not a recognised ISO or disk image.")
            return
        self.image = info
        self.last_dir = os.path.dirname(path)
        self.boot_combo.remove(0)
        self.boot_combo.insert(0, "image", info.filename)
        self.boot_combo.set_active_id("image")
        self.log(f"Selected image: {path} ({human_size(info.size)}), kind={info.kind}, "
                 f"label='{info.label}', hybrid={info.is_hybrid}, windows={info.is_windows}, "
                 f"efi={info.has_efi}, bios={info.has_bios}")
        for n in info.notes:
            self.log(n)

        self.mode_combo.remove_all()
        if info.supports_iso_mode:
            self.mode_combo.append("iso", "Write in ISO Image mode (file copy)" +
                                   (" (Recommended)" if info.recommended_mode == "iso" else ""))
        if info.supports_dd_mode:
            self.mode_combo.append("dd", "Write in DD Image mode" +
                                   (" (Recommended)" if info.recommended_mode == "dd" else ""))
        self.mode_combo.set_active_id(info.recommended_mode)
        if info.label:
            self.label_entry.set_text(info.label)
        if info.is_windows:
            self.scheme_combo.set_active_id("gpt")
        self.set_status(f"Using image: {info.filename}")
        self._populate_fs()
        self._update_state()

    def _on_hash(self, *_):
        if self.image:
            ChecksumDialog(self, self.image.path)

    # ------------------------------------------------------------ fs / state
    def _fs_choices(self) -> List[str]:
        if self.mode == "format":
            dev = self.device
            choices = ["fat32", "exfat", "ntfs", "ext4"]
            if dev and dev.size <= 4 * 1024 ** 3:
                choices.append("fat16")
            return choices
        if self.mode == "iso":
            return ["fat32", "ntfs"] if self.image and self.image.is_windows else ["fat32"]
        return []

    def _populate_fs(self):
        current = self.fs_combo.get_active_id()
        choices = self._fs_choices()
        if [r[1] for r in self.fs_combo.get_model() or []] == choices:
            return
        self.fs_combo.remove_all()
        for fs in choices:
            self.fs_combo.append(fs, FS_LABELS[fs])
        if current in choices:
            self.fs_combo.set_active_id(current)
        elif choices:
            dev = self.device
            default = "fat32"
            if self.mode == "format" and dev and dev.size > 32 * 1000 ** 3:
                default = "exfat"
            self.fs_combo.set_active_id(default)

    def _on_fs_changed(self, *_):
        fs = self.fs_combo.get_active_id()
        self.cluster_combo.remove_all()
        if not fs:
            return
        self.cluster_combo.append("0", f"{cluster_text(self._default_cluster(fs))} (Default)")
        for c in CLUSTERS[fs]:
            self.cluster_combo.append(str(c), cluster_text(c))
        self.cluster_combo.set_active_id("0")

    def _default_cluster(self, fs: str) -> int:
        size = self.device.size if self.device else 0
        if fs == "fat32":
            return 4 * K if size < 8 * 1024 ** 3 else (8 * K if size < 16 * 1024 ** 3 else
                                                      (16 * K if size < 32 * 1024 ** 3 else 32 * K))
        if fs == "exfat":
            return 32 * K if size <= 32 * 1024 ** 3 else 128 * K
        if fs == "fat16":
            return 32 * K
        return 4 * K

    def _update_state(self):
        if not hasattr(self, "start_btn"):
            return
        bootable = self.bootable
        has_image = bool(self.image)
        self.select_btn.set_sensitive(bootable and not self.busy)
        self.hash_btn.set_sensitive(bootable and has_image and not self.busy)
        show_mode = bootable and has_image
        self.mode_box.set_visible(show_mode)

        self._populate_fs()
        mode = self.mode
        dd = mode == "dd"
        self.scheme_combo.set_sensitive(not dd and not self.busy)
        self.label_entry.set_sensitive(not dd and not self.busy)
        self.fs_combo.set_sensitive(not dd and not self.busy)
        self.cluster_combo.set_sensitive(not dd and not self.busy and mode == "format")
        self.quick_check.set_sensitive(mode == "format" and not self.busy)
        self.verify_check.set_sensitive(not self.busy)
        self.bb_combo.set_sensitive(self.bb_check.get_active() and not self.busy)

        self.target_combo.remove_all()
        if dd:
            target = "As defined by the image"
        elif mode == "iso":
            target = "UEFI (non CSM)"
        else:
            target = "BIOS or UEFI" if self.scheme_combo.get_active_id() == "mbr" else "UEFI (non CSM)"
        self.target_combo.append("t", target)
        self.target_combo.set_active(0)

        ready = self.device is not None and (not bootable or has_image)
        self.start_btn.set_sensitive(ready and not self.busy)

    # ------------------------------------------------------------ dialogs
    def error(self, text: str, secondary: str = ""):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.CLOSE, text=text)
        if secondary:
            dlg.format_secondary_text(secondary)
        dlg.run()
        dlg.destroy()

    def confirm(self, text: str, secondary: str, kind=Gtk.MessageType.WARNING) -> bool:
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=kind,
                                buttons=Gtk.ButtonsType.OK_CANCEL, text=text)
        dlg.format_secondary_text(secondary)
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        ok = dlg.run() == Gtk.ResponseType.OK
        dlg.destroy()
        return ok

    def _on_about(self, *_):
        dlg = Gtk.AboutDialog(transient_for=self, modal=True)
        dlg.set_program_name(APP_NAME)
        dlg.set_version(__version__)
        dlg.set_logo_icon_name("rufix")
        dlg.set_comments("Create bootable USB drives and format USB keys on Linux.\n"
                         "Inspired by Rufus (rufus.ie).")
        dlg.set_license_type(Gtk.License.GPL_3_0)
        dlg.run()
        dlg.destroy()

    # ------------------------------------------------------------ start
    def _build_job(self) -> Optional[dict]:
        dev = self.device
        job = {
            "device": dev.path,
            "allow_usb_hdd": self.usb_hdd_check.get_active(),
            "action": self.mode,
            "scheme": self.scheme_combo.get_active_id(),
            "filesystem": self.fs_combo.get_active_id(),
            "cluster": int(self.cluster_combo.get_active_id() or 0),
            "label": self.label_entry.get_text(),
            "quick": self.quick_check.get_active(),
            "badblocks": int(self.bb_combo.get_active_id()) if self.bb_check.get_active() else 0,
            "verify": self.verify_check.get_active(),
        }
        if self.mode in ("dd", "iso"):
            img = self.image
            if not os.path.exists(img.path):
                self.error("The selected image no longer exists.")
                return None
            job["image"] = img.path
            job["compression"] = img.compression
            if not img.compression and img.size > dev.size:
                self.error("Image is too big for the selected device.",
                           f"The image is {human_size(img.size)} but the device is only "
                           f"{human_size(dev.size)}.")
                return None
            if self.mode == "iso" and img.is_windows:
                dlg = WindowsOptionsDialog(self, self.win_opts)
                resp = dlg.run()
                job["win_opts"] = dlg.options()
                dlg.destroy()
                if resp != Gtk.ResponseType.OK:
                    return None
                self.win_opts = job["win_opts"]
        return job

    def _on_start(self, *_):
        dev = self.device
        if not dev:
            return
        job = self._build_job()
        if not job:
            return
        extra = ""
        if dev.all_mounts:
            extra = "\n\nThe following mounted partitions will be unmounted:\n" + \
                    "\n".join(dev.all_mounts)
        if not self.confirm(
                f"WARNING: ALL DATA ON DEVICE '{dev.display_name}' WILL BE DESTROYED.",
                "To continue with this operation, click OK. To quit click CANCEL." + extra):
            return
        if job["badblocks"]:
            if not self.confirm("Bad blocks check",
                                "The bad blocks check writes to every sector of the drive "
                                "and can take several hours on large drives. Continue?",
                                Gtk.MessageType.QUESTION):
                return
        self._save_settings()
        self._run_job(job)

    def _run_job(self, job: dict):
        self._job_device = job["device"]
        cmd = helper_command()
        self.log("Starting: " + " ".join(cmd))
        self.log("Job: " + json.dumps({k: v for k, v in job.items()}))
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as e:
            self.error("Could not start the privileged helper.", str(e))
            return
        self.proc.stdin.write(json.dumps(job) + "\n")
        self.proc.stdin.flush()

        self.busy = True
        self.got_done = False
        self._done_msg = {}
        self.start_time = time.time()
        for w in self.controls:
            w.set_sensitive(False)
        self.close_btn.set_label("CANCEL")
        self.start_btn.set_sensitive(False)
        for cls in ("ready", "failed"):
            self.progress.get_style_context().remove_class(cls)
        self.progress.set_fraction(0)
        self.progress.set_text("Waiting for authorization...")
        self._pulse_id = None
        GLib.timeout_add(500, self._tick)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _tick(self):
        if not self.busy:
            return False
        elapsed = int(time.time() - self.start_time)
        self.timer_label.set_text(f"{elapsed // 3600:02d}:{elapsed // 60 % 60:02d}:{elapsed % 60:02d}")
        return True

    def _pulse(self):
        if self._pulse_id is None:
            return False
        self.progress.pulse()
        return True

    def _reader(self, proc: subprocess.Popen):
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                msg = {"t": "log", "m": line}
            GLib.idle_add(self._handle, msg)
        rc = proc.wait()
        GLib.idle_add(self._finished, rc)

    def _handle(self, msg: dict):
        t = msg.get("t")
        if t == "log":
            self.log(msg.get("m", ""))
        elif t == "status":
            self.progress.set_text(msg.get("m", ""))
            self.set_status(msg.get("m", ""))
        elif t == "progress":
            p = msg.get("p", 0)
            if p < 0:
                if self._pulse_id is None:
                    self._pulse_id = GLib.timeout_add(120, self._pulse)
            else:
                if self._pulse_id is not None:
                    GLib.source_remove(self._pulse_id)
                    self._pulse_id = None
                self.progress.set_fraction(p)
        elif t == "done":
            self.got_done = True
            self._done_msg = msg
        return False

    def _finished(self, rc: int):
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
        self.busy = False
        self.proc = None
        self.close_btn.set_label("CLOSE")
        for w in self.controls:
            w.set_sensitive(True)
        self.refresh_devices(force=True)
        ctx = self.progress.get_style_context()
        elapsed = int(time.time() - self.start_time)

        if self.got_done and self._done_msg.get("ok"):
            self.progress.set_fraction(1.0)
            self.progress.set_text("READY")
            ctx.add_class("ready")
            self.set_status(f"Operation completed in {elapsed // 60}m {elapsed % 60}s")
            self.log("Operation completed successfully.")
            self._notify("Rufix: operation completed", "Your USB drive is ready.")
            threading.Thread(target=self._remount, args=(self._job_device,), daemon=True).start()
        else:
            if self.got_done:
                reason = self._done_msg.get("m", "Unknown error")
            elif rc in (126, 127):
                reason = "Authorization was cancelled or denied"
            else:
                reason = f"Helper exited unexpectedly (code {rc})"
            self.progress.set_fraction(0)
            self.progress.set_text("FAILED" if reason != "Cancelled" else "CANCELLED")
            ctx.add_class("failed")
            self.set_status(reason)
            self.log(f"Operation failed: {reason}")
            if reason != "Cancelled" and rc not in (126, 127):
                self.error("The operation failed.", reason + "\n\nSee the log for details.")
        self._update_state()
        return False

    def _remount(self, dev_path: str):
        """Mount the new volumes as the user, like plugging the drive in again.

        The helper has to unmount the drive to write it, and the desktop only
        automounts on hotplug, so without this the drive vanishes from the file
        manager until it is unplugged and reinserted.
        """
        subprocess.run(["udevadm", "settle", "--timeout=10"], capture_output=True)
        volumes: List[str] = []
        for _ in range(10):
            volumes = mountable_volumes(dev_path)
            if volumes:
                break
            time.sleep(0.5)
        for vol in volumes:
            for attempt in range(5):
                proc = subprocess.run(["udisksctl", "mount", "-b", vol, "--no-user-interaction"],
                                      capture_output=True, text=True)
                if proc.returncode == 0:
                    GLib.idle_add(self.log, proc.stdout.strip())
                    break
                time.sleep(1)
            else:
                GLib.idle_add(self.log, f"Could not mount {vol}: "
                              f"{(proc.stderr or proc.stdout).strip()}")
        GLib.idle_add(self.refresh_devices, True)

    def _notify(self, title: str, body: str):
        app = self.get_application()
        if app and not self.is_active():
            n = Gio.Notification.new(title)
            n.set_body(body)
            app.send_notification("done", n)

    # ------------------------------------------------------------ close / cancel
    def _cancel(self) -> bool:
        if not self.busy or not self.proc:
            return True
        if not self.confirm("Cancel the current operation?",
                            "Cancelling now may leave the drive in an unusable state "
                            "until it is formatted again."):
            return False
        try:
            self.proc.stdin.write("cancel\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        self.progress.set_text("Cancelling...")
        return False

    def _on_close_clicked(self, *_):
        if self.busy:
            self._cancel()
        else:
            self.close()

    def _on_delete(self, *_):
        if self.busy:
            self._cancel()
            return True
        self._save_settings()
        return False


class RufixApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.window: Optional[RufixWindow] = None

    def do_activate(self):
        if not self.window:
            self.window = RufixWindow(self)
        self.window.show_all()
        self.window.present()

    def do_open(self, files, n_files, hint):
        self.do_activate()
        if files and files[0].get_path():
            self.window.load_image(files[0].get_path())


def main():
    GLib.set_application_name(APP_NAME)
    GLib.set_prgname("rufix")
    return RufixApp().run(sys.argv)
