#!/usr/bin/env python3
import json
import os
import shutil
import sys

import gi

gi.require_version('Gtk', '3.0')
gi.require_version('AyatanaAppIndicator3', '0.1')
from gi.repository import Gtk, GLib, Gio, AyatanaAppIndicator3 as AppIndicator3

VERSION = "0.1.1"

APP_ID = "display-profile-switcher"
STATE_DIR = os.path.join(GLib.get_user_state_dir(), "display-switcher")
STATE_FILE = os.path.join(STATE_DIR, "current-preset")
CLI_INSTALL_URL = "https://github.com/alexdemb/mutter-display-presets"


def log_error(msg):
    print(f"[display-switcher] {msg}", file=sys.stderr)


def cli_path():
    found = shutil.which("mutter-display-presets")
    if found:
        return found
    return os.path.expanduser("~/.local/bin/mutter-display-presets")


def cli_available():
    path = cli_path()
    return os.path.isfile(path) and os.access(path, os.X_OK)


def config_path():
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return os.path.join(xdg_config_home, "display-presets.json")
    return os.path.expanduser("~/.config/display-presets.json")


def load_presets():
    try:
        with open(config_path(), "r") as f:
            data = json.load(f)
        return data.get("presets", [])
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError) as e:
        log_error(f"Failed to read preset config: {e}")
        return []


def preset_output_lines(preset):
    display_config = preset.get("display_config", {})

    current_mode_by_connector = {}
    for monitor in display_config.get("monitors", []):
        connector = monitor.get("monitor_info", {}).get("connector", "?")
        for mode in monitor.get("modes", []):
            if mode.get("properties", {}).get("is-current") == "1":
                refresh = mode.get("refresh_rate")
                refresh_text = f"{refresh:.0f}" if isinstance(refresh, (int, float)) else "?"
                current_mode_by_connector[connector] = (
                    f"{mode.get('width')}x{mode.get('height')}@{refresh_text}"
                )

    lines = []
    for lm in display_config.get("logical_monitors", []):
        for monitor_info in lm.get("monitors", []):
            connector = monitor_info.get("connector", "?")
            mode_text = current_mode_by_connector.get(connector, "unknown")
            primary = " (primary)" if lm.get("primary") else ""
            lines.append(f"{connector}: {mode_text}{primary}")
    return lines


def load_current_preset_name():
    try:
        with open(STATE_FILE, "r") as f:
            return f.read().strip() or None
    except (FileNotFoundError, OSError):
        return None


def save_current_preset_name(name):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w") as f:
            f.write(name)
    except OSError as e:
        log_error(f"Failed to persist current preset state: {e}")


def run_cli_async(args, on_done):
    if not cli_available():
        message = (
            f"mutter-display-presets not found at {cli_path()}.\n"
            f"Install it from {CLI_INSTALL_URL}"
        )
        log_error(message)
        on_done(False, "", message)
        return

    try:
        proc = Gio.Subprocess.new(
            [cli_path()] + args,
            Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
        )
    except GLib.Error as e:
        log_error(f"Failed to launch CLI: {e}")
        on_done(False, "", str(e))
        return

    def _on_communicate(source, result):
        try:
            _, stdout, stderr = source.communicate_utf8_finish(result)
            success = source.get_exit_status() == 0
            on_done(success, stdout or "", stderr or "")
        except GLib.Error as e:
            log_error(f"CLI call {args} failed: {e}")
            on_done(False, "", str(e))

    proc.communicate_utf8_async(None, None, _on_communicate)


def show_error_dialog(primary, secondary=""):
    try:
        dialog = Gtk.MessageDialog(
            transient_for=None,
            flags=0,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=primary,
        )
        if secondary:
            dialog.format_secondary_text(secondary.strip())
        dialog.run()
        dialog.destroy()
    except GLib.Error as e:
        log_error(f"Failed to show error dialog: {e}")


def confirm_dialog(primary, secondary=""):
    try:
        dialog = Gtk.MessageDialog(
            transient_for=None,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=primary,
        )
        if secondary:
            dialog.format_secondary_text(secondary.strip())
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.YES
    except GLib.Error as e:
        log_error(f"Failed to show confirm dialog: {e}")
        return False


def prompt_for_name(title, initial_text=""):
    dialog = Gtk.Dialog(
        title=title,
        transient_for=None,
        flags=Gtk.DialogFlags.MODAL,
    )
    dialog.add_buttons(
        Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
        Gtk.STOCK_OK, Gtk.ResponseType.OK,
    )
    dialog.set_default_response(Gtk.ResponseType.OK)

    entry = Gtk.Entry()
    entry.set_text(initial_text)
    entry.set_activates_default(True)
    entry.select_region(0, -1)

    box = dialog.get_content_area()
    box.set_border_width(10)
    box.set_spacing(6)
    box.add(Gtk.Label(label=title))
    box.add(entry)
    dialog.show_all()

    response = dialog.run()
    text = entry.get_text().strip()
    dialog.destroy()

    if response == Gtk.ResponseType.OK and text:
        return text
    return None


class DisplaySwitcher:
    def __init__(self):
        self.indicator = AppIndicator3.Indicator.new(
            APP_ID,
            "video-display-symbolic",
            AppIndicator3.IndicatorCategory.SYSTEM_SERVICES,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.current_preset = load_current_preset_name()
        self._apply_seq = 0
        self.rebuild_menu()

    def rebuild_menu(self):
        menu = Gtk.Menu()

        if not cli_available():
            self._build_missing_cli_menu(menu)
            menu.show_all()
            self.indicator.set_menu(menu)
            self.menu = menu
            return

        presets = load_presets()

        if not presets:
            empty_item = Gtk.MenuItem(label="No presets saved")
            empty_item.set_sensitive(False)
            menu.append(empty_item)
        else:
            # Independent check items drawn as radios, deliberately NOT a
            # RadioMenuItem group: a radio group always forces exactly one
            # item active (so a fresh install would falsely mark the first
            # preset), and clicking an already-active radio emits nothing
            # (so the current preset could never be re-applied).
            # Set initial state before connecting "activate", so building
            # the menu doesn't itself trigger an apply.
            items = []
            for preset in presets:
                name = preset.get("name", "?")
                item = Gtk.CheckMenuItem(label=name)
                item.set_draw_as_radio(True)
                item.set_active(name == self.current_preset)
                items.append((item, name))
                menu.append(item)

            for item, name in items:
                item.connect("activate", self.on_apply, name)

        menu.append(Gtk.SeparatorMenuItem())

        manage_item = Gtk.MenuItem(label="Manage Presets")
        manage_item.set_submenu(self._build_manage_submenu(presets))
        menu.append(manage_item)

        save_item = Gtk.MenuItem(label="Save current layout as…")
        save_item.connect("activate", self.on_save_as)
        menu.append(save_item)

        settings_item = Gtk.MenuItem(label="Display Settings…")
        settings_item.connect("activate", self.on_open_display_settings)
        menu.append(settings_item)

        menu.show_all()
        self.indicator.set_menu(menu)
        self.menu = menu

    def _build_missing_cli_menu(self, menu):
        warning_item = Gtk.MenuItem(label="mutter-display-presets not found")
        warning_item.set_sensitive(False)
        menu.append(warning_item)

        install_item = Gtk.MenuItem(label="Install mutter-display-presets…")
        install_item.connect("activate", self.on_open_install_instructions)
        menu.append(install_item)

        menu.append(Gtk.SeparatorMenuItem())

        settings_item = Gtk.MenuItem(label="Display Settings…")
        settings_item.connect("activate", self.on_open_display_settings)
        menu.append(settings_item)

    def on_open_install_instructions(self, _widget):
        try:
            Gio.AppInfo.launch_default_for_uri(CLI_INSTALL_URL, None)
        except GLib.Error as e:
            log_error(f"Failed to open install instructions: {e}")
            show_error_dialog(
                "Failed to open browser",
                f"Install mutter-display-presets from:\n{CLI_INSTALL_URL}",
            )

    def _build_manage_submenu(self, presets):
        submenu = Gtk.Menu()

        if not presets:
            empty_item = Gtk.MenuItem(label="No presets saved")
            empty_item.set_sensitive(False)
            submenu.append(empty_item)
            return submenu

        for preset in presets:
            name = preset.get("name", "?")
            preset_item = Gtk.MenuItem(label=name)
            preset_item.set_submenu(self._build_preset_detail_submenu(preset, name))
            submenu.append(preset_item)

        return submenu

    def _build_preset_detail_submenu(self, preset, name):
        submenu = Gtk.Menu()

        lines = preset_output_lines(preset)
        if lines:
            for line in lines:
                detail_item = Gtk.MenuItem(label=line)
                detail_item.set_sensitive(False)
                submenu.append(detail_item)
        else:
            no_detail_item = Gtk.MenuItem(label="No output details")
            no_detail_item.set_sensitive(False)
            submenu.append(no_detail_item)

        submenu.append(Gtk.SeparatorMenuItem())

        update_item = Gtk.MenuItem(label="Update with current layout")
        update_item.connect("activate", self.on_update, name)
        submenu.append(update_item)

        rename_item = Gtk.MenuItem(label="Rename…")
        rename_item.connect("activate", self.on_rename, name)
        submenu.append(rename_item)

        delete_item = Gtk.MenuItem(label="Delete…")
        delete_item.connect("activate", self.on_delete, name)
        submenu.append(delete_item)

        return submenu

    def on_apply(self, _widget, name):
        # Applies can overlap (a Mutter D-Bus call may take seconds); only
        # the most recently requested one may update the active marker.
        self._apply_seq += 1
        seq = self._apply_seq

        def on_done(success, stdout, stderr):
            if seq != self._apply_seq:
                return
            if success:
                self.current_preset = name
                save_current_preset_name(name)
            else:
                show_error_dialog(f"Failed to apply preset '{name}'", stdout + stderr)
            # Clicking a CheckMenuItem toggles it visually; redraw from the
            # real state either way.
            self.rebuild_menu()

        run_cli_async(["apply", "--", name], on_done)

    def on_save_as(self, _widget):
        name = prompt_for_name("Save current layout as…")
        if not name:
            return
        self._save(name, force=False)

    def _save(self, name, force):
        args = ["save"] + (["--force"] if force else []) + ["--", name]

        def on_done(success, stdout, stderr):
            if success:
                self.rebuild_menu()
                return
            message = stdout + stderr
            if not force and "already exists" in message.lower():
                if confirm_dialog(
                    f"Preset '{name}' already exists.",
                    "Overwrite it with the current layout?",
                ):
                    self._save(name, force=True)
                return
            show_error_dialog(f"Failed to save preset '{name}'", message)

        run_cli_async(args, on_done)

    def on_update(self, _widget, name):
        if not confirm_dialog(
            f"Update '{name}' with the current layout?",
            "The previously saved layout for this preset will be overwritten.",
        ):
            return
        self._save(name, force=True)

    def on_delete(self, _widget, name):
        if not confirm_dialog(
            f"Delete preset '{name}'?",
            "This cannot be undone.",
        ):
            return

        def on_done(success, stdout, stderr):
            if success:
                if self.current_preset == name:
                    self.current_preset = None
                    save_current_preset_name("")
                self.rebuild_menu()
            else:
                show_error_dialog(f"Failed to delete preset '{name}'", stdout + stderr)

        run_cli_async(["delete", "--", name], on_done)

    def on_rename(self, _widget, old_name):
        new_name = prompt_for_name(f"Rename '{old_name}' to…", old_name)
        if not new_name or new_name == old_name:
            return
        self._rename(old_name, new_name, force=False)

    def _rename(self, old_name, new_name, force):
        args = ["rename"] + (["--force"] if force else []) + ["--", old_name, new_name]

        def on_done(success, stdout, stderr):
            if success:
                if self.current_preset == old_name:
                    self.current_preset = new_name
                    save_current_preset_name(new_name)
                elif force and self.current_preset == new_name:
                    # The active preset was just overwritten by a different
                    # layout; the marker no longer reflects what's on screen.
                    self.current_preset = None
                    save_current_preset_name("")
                self.rebuild_menu()
                return
            message = stdout + stderr
            if not force and "already exist" in message.lower():
                if confirm_dialog(
                    f"Preset '{new_name}' already exists.",
                    "Overwrite it?",
                ):
                    self._rename(old_name, new_name, force=True)
                return
            show_error_dialog(
                f"Failed to rename preset '{old_name}' to '{new_name}'", message
            )

        run_cli_async(args, on_done)

    def on_open_display_settings(self, _widget):
        try:
            app_info = Gio.DesktopAppInfo.new("gnome-display-panel.desktop")
            if app_info:
                app_info.launch(None, None)
                return
        except GLib.Error as e:
            log_error(f"Failed to launch display panel via desktop file: {e}")

        try:
            Gio.Subprocess.new(
                ["gnome-control-center", "display"],
                Gio.SubprocessFlags.NONE,
            )
        except GLib.Error as e:
            log_error(f"Failed to launch gnome-control-center: {e}")
            show_error_dialog("Failed to open Display Settings", str(e))


if __name__ == "__main__":
    DisplaySwitcher()
    Gtk.main()
