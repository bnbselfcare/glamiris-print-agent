"""
Glamiris Print Agent - System Tray Application
Runs Flask server in background with system tray icon and settings GUI.
"""

import json
import sys
import threading
import webbrowser
from pathlib import Path
import platform

# Fix for macOS tkinter + pystray threading issues
if platform.system() == 'Darwin':
    import multiprocessing
    multiprocessing.set_start_method('spawn', force=True)

# GUI imports
import tkinter as tk
from tkinter import ttk, messagebox

# Tray imports
import pystray
from PIL import Image

# Flask app import
from app import app, detect_thermal_printers, load_config, save_config, auto_configure

# ---------------- CONSTANTS ----------------

APP_NAME = "Glamiris Print Agent"
VERSION = "1.0.13"
GITHUB_REPO = "bnbselfcare/glamiris-print-agent"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
SUPPORT_URL = "https://glamiris.com/support"

BASE_DIR = Path(__file__).parent
ASSETS_DIR = BASE_DIR / "assets"
ICON_PATH = ASSETS_DIR / "icon.png"

# Brand colors
COLOR_BG = "#0a0e14"
COLOR_FG = "#ffffff"
COLOR_ACCENT = "#ff5722"
COLOR_SECONDARY = "#1a1f2b"
COLOR_SUCCESS = "#4caf50"
COLOR_ERROR = "#f44336"

# ---------------- UPDATE CHECKER ----------------

update_available = None  # Will be set to version string if update available


def check_for_updates():
    """Check GitHub for newer version."""
    global update_available
    import requests

    try:
        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        response = requests.get(api_url, timeout=5)

        if response.status_code == 200:
            data = response.json()
            latest_version = data.get("tag_name", "").lstrip("v")

            # Compare versions
            current_parts = [int(x) for x in VERSION.split(".")]
            latest_parts = [int(x) for x in latest_version.split(".")]

            if latest_parts > current_parts:
                update_available = latest_version
                print(f"[Update] New version available: v{latest_version}")
                return latest_version

        print(f"[Update] Current version {VERSION} is up to date")
        return None

    except Exception as e:
        print(f"[Update] Check failed: {e}")
        return None


def download_update():
    """Open browser to download latest release."""
    webbrowser.open(RELEASES_URL)


# ---------------- FLASK SERVER ----------------

flask_thread = None
server_running = False


def start_flask_server():
    """Start Flask server in background thread."""
    global flask_thread, server_running
    if server_running:
        return

    def run():
        global server_running
        server_running = True
        app.run(host="127.0.0.1", port=5678, threaded=True, use_reloader=False)

    flask_thread = threading.Thread(target=run, daemon=True)
    flask_thread.start()


# ---------------- SETTINGS WINDOW ----------------

class SettingsWindow:
    def __init__(self):
        self.window = None
        self.printer_var = None
        self.status_label = None
        self.printers = []

    def show(self):
        """Show the settings window in a separate thread."""
        # Run GUI in separate thread to avoid blocking tray
        def run_gui():
            if self.window is not None:
                try:
                    self.window.lift()
                    self.window.focus_force()
                    return
                except tk.TclError:
                    self.window = None

            self.window = tk.Tk()
            self.window.title(APP_NAME)
            self.window.geometry("420x520")
            self.window.configure(bg=COLOR_BG)
            self.window.resizable(False, False)

            # Set window icon
            if ICON_PATH.exists():
                try:
                    icon_img = tk.PhotoImage(file=str(ICON_PATH))
                    self.window.iconphoto(True, icon_img)
                except:
                    pass

            self._build_ui()
            self._refresh_printers()

            # Center window on screen
            self.window.update_idletasks()
            x = (self.window.winfo_screenwidth() // 2) - (420 // 2)
            y = (self.window.winfo_screenheight() // 2) - (520 // 2)
            self.window.geometry(f"420x520+{x}+{y}")

            self.window.protocol("WM_DELETE_WINDOW", self._on_close)
            self.window.mainloop()

        thread = threading.Thread(target=run_gui, daemon=True)
        thread.start()

    def _on_close(self):
        """Handle window close."""
        if self.window:
            self.window.destroy()
            self.window = None

    def _build_ui(self):
        """Build the settings UI."""
        # Main container with padding
        main_frame = tk.Frame(self.window, bg=COLOR_BG, padx=30, pady=20)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Logo
        if ICON_PATH.exists():
            try:
                logo_img = Image.open(ICON_PATH)
                logo_img = logo_img.resize((80, 80), Image.Resampling.LANCZOS)
                self.logo_photo = tk.PhotoImage(file=str(ICON_PATH))
                # Scale down for display
                self.logo_photo = self.logo_photo.subsample(
                    self.logo_photo.width() // 80,
                    self.logo_photo.height() // 80
                )
                logo_label = tk.Label(main_frame, image=self.logo_photo, bg=COLOR_BG)
                logo_label.pack(pady=(0, 10))
            except Exception as e:
                pass

        # App name
        title_label = tk.Label(
            main_frame,
            text=APP_NAME,
            font=("Helvetica", 20, "bold"),
            fg=COLOR_FG,
            bg=COLOR_BG
        )
        title_label.pack(pady=(0, 5))

        # Version
        version_label = tk.Label(
            main_frame,
            text=f"Version {VERSION}",
            font=("Helvetica", 10),
            fg="#888888",
            bg=COLOR_BG
        )
        version_label.pack(pady=(0, 20))

        # Status indicator
        status_frame = tk.Frame(main_frame, bg=COLOR_SECONDARY, padx=15, pady=10)
        status_frame.pack(fill=tk.X, pady=(0, 20))

        self.status_label = tk.Label(
            status_frame,
            text="● Server running on port 5678",
            font=("Helvetica", 11),
            fg=COLOR_SUCCESS,
            bg=COLOR_SECONDARY
        )
        self.status_label.pack()

        # Printer selection section
        printer_section = tk.LabelFrame(
            main_frame,
            text=" Printer Selection ",
            font=("Helvetica", 11, "bold"),
            fg=COLOR_FG,
            bg=COLOR_BG,
            padx=15,
            pady=15
        )
        printer_section.pack(fill=tk.X, pady=(0, 20))

        # Printer dropdown
        self.printer_var = tk.StringVar()
        self.printer_dropdown = ttk.Combobox(
            printer_section,
            textvariable=self.printer_var,
            state="readonly",
            width=35,
            font=("Helvetica", 10)
        )
        self.printer_dropdown.pack(pady=(0, 10))

        # Refresh button
        refresh_btn = tk.Button(
            printer_section,
            text="↻ Refresh Printers",
            font=("Helvetica", 10),
            fg=COLOR_FG,
            bg=COLOR_SECONDARY,
            activebackground=COLOR_ACCENT,
            activeforeground=COLOR_FG,
            bd=0,
            padx=15,
            pady=5,
            cursor="hand2",
            command=self._refresh_printers
        )
        refresh_btn.pack()

        # Action buttons section
        actions_frame = tk.Frame(main_frame, bg=COLOR_BG)
        actions_frame.pack(fill=tk.X, pady=(0, 20))

        # Test Print button
        test_print_btn = tk.Button(
            actions_frame,
            text="🖨 Test Print",
            font=("Helvetica", 11, "bold"),
            fg=COLOR_FG,
            bg=COLOR_ACCENT,
            activebackground="#ff7043",
            activeforeground=COLOR_FG,
            bd=0,
            padx=20,
            pady=10,
            cursor="hand2",
            command=self._test_print
        )
        test_print_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 5))

        # Open Drawer button
        drawer_btn = tk.Button(
            actions_frame,
            text="💰 Open Drawer",
            font=("Helvetica", 11, "bold"),
            fg=COLOR_FG,
            bg=COLOR_SECONDARY,
            activebackground="#2a3040",
            activeforeground=COLOR_FG,
            bd=0,
            padx=20,
            pady=10,
            cursor="hand2",
            command=self._open_drawer
        )
        drawer_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(5, 0))

        # Save button
        save_btn = tk.Button(
            main_frame,
            text="Save Configuration",
            font=("Helvetica", 11, "bold"),
            fg=COLOR_BG,
            bg=COLOR_FG,
            activebackground="#cccccc",
            activeforeground=COLOR_BG,
            bd=0,
            padx=20,
            pady=12,
            cursor="hand2",
            command=self._save_config
        )
        save_btn.pack(fill=tk.X, pady=(0, 20))

        # Footer with support link
        footer_frame = tk.Frame(main_frame, bg=COLOR_BG)
        footer_frame.pack(side=tk.BOTTOM, fill=tk.X)

        support_link = tk.Label(
            footer_frame,
            text="Need help? Visit Support",
            font=("Helvetica", 10, "underline"),
            fg=COLOR_ACCENT,
            bg=COLOR_BG,
            cursor="hand2"
        )
        support_link.pack()
        support_link.bind("<Button-1>", lambda e: webbrowser.open(SUPPORT_URL))

    def _refresh_printers(self):
        """Refresh the list of available printers."""
        try:
            self.printers = detect_thermal_printers()
            cfg = load_config()
            current_usb = cfg.get("usb")

            printer_names = []
            selected_index = 0

            for i, p in enumerate(self.printers):
                name = f"{p['manufacturer'] or 'Unknown'} {p['product'] or 'Printer'} ({p['vendorId']}:{p['productId']})"
                printer_names.append(name)

                # Check if this is the currently configured printer
                if current_usb:
                    if p['idVendor'] == current_usb.get('idVendor') or \
                       hex(p['idVendor']) == str(current_usb.get('idVendor')):
                        selected_index = i

            if not printer_names:
                printer_names = ["No printers detected"]

            self.printer_dropdown['values'] = printer_names
            self.printer_dropdown.current(selected_index if self.printers else 0)

            if self.printers:
                self.status_label.config(
                    text=f"● Server running | {len(self.printers)} printer(s) found",
                    fg=COLOR_SUCCESS
                )
            else:
                self.status_label.config(
                    text="● Server running | No printers detected",
                    fg=COLOR_ERROR
                )

        except Exception as e:
            messagebox.showerror("Error", f"Failed to detect printers: {e}")

    def _save_config(self):
        """Save the selected printer configuration."""
        if not self.printers:
            messagebox.showwarning("Warning", "No printers available to configure")
            return

        selected_index = self.printer_dropdown.current()
        if selected_index < 0 or selected_index >= len(self.printers):
            return

        printer = self.printers[selected_index]
        cfg = load_config()
        cfg["backend"] = "usb"
        cfg["usb"] = {
            "idVendor": hex(printer["idVendor"]),
            "idProduct": hex(printer["idProduct"]),
            "interface": 0
        }
        cfg["network"] = {"host": "", "port": 9100}
        save_config(cfg)

        messagebox.showinfo("Success", f"Printer configured:\n{printer['manufacturer']} {printer['product']}")

    def _test_print(self):
        """Send a test print."""
        import requests
        try:
            resp = requests.post("http://127.0.0.1:5678/test-print", timeout=15)
            if resp.status_code == 200:
                messagebox.showinfo("Success", "Test print sent!")
            else:
                messagebox.showerror("Error", f"Print failed: {resp.json().get('error', 'Unknown error')}")
        except requests.exceptions.Timeout:
            messagebox.showerror("Error", "Request timed out. Check if printer is connected.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to send test print: {e}")

    def _open_drawer(self):
        """Open the cash drawer."""
        import requests
        try:
            resp = requests.post("http://127.0.0.1:5678/open-cashdrawer", timeout=15)
            if resp.status_code == 200:
                messagebox.showinfo("Success", "Cash drawer opened!")
            else:
                messagebox.showerror("Error", f"Failed: {resp.json().get('error', 'Unknown error')}")
        except requests.exceptions.Timeout:
            messagebox.showerror("Error", "Request timed out. Check if printer is connected.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open drawer: {e}")

    def close(self):
        """Close the settings window."""
        if self.window:
            try:
                self.window.destroy()
            except:
                pass
            self.window = None


# ---------------- SYSTEM TRAY ----------------

settings_window = SettingsWindow()


def create_tray_icon():
    """Create the system tray icon."""
    # Load icon
    if ICON_PATH.exists():
        icon_image = Image.open(ICON_PATH)
    else:
        # Fallback: create a simple colored icon
        icon_image = Image.new('RGB', (64, 64), COLOR_ACCENT)

    # Build menu items
    menu_items = [
        pystray.MenuItem(
            f"{APP_NAME} v{VERSION}",
            lambda icon, item: None,
            enabled=False
        ),
    ]

    # Add update notification if available
    if update_available:
        menu_items.append(
            pystray.MenuItem(
                f"Update to v{update_available}",
                lambda icon, item: download_update(),
                default=True
            )
        )

    menu_items.extend([
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Settings",
            on_settings
        ),
        pystray.MenuItem(
            "Test Print",
            on_test_print
        ),
        pystray.MenuItem(
            "Open Cash Drawer",
            on_open_drawer
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Quit",
            on_quit
        )
    ])

    menu = pystray.Menu(*menu_items)

    icon = pystray.Icon(
        APP_NAME,
        icon_image,
        APP_NAME,
        menu
    )

    return icon


def on_settings(icon, item):
    """Handle settings from tray menu."""
    settings_window.show()


def on_test_print(icon, item):
    """Handle test print from tray menu."""
    def do_print():
        import requests
        try:
            requests.post("http://127.0.0.1:5678/test-print", timeout=10)
        except Exception as e:
            print(f"Test print error: {e}")
    threading.Thread(target=do_print, daemon=True).start()


def on_open_drawer(icon, item):
    """Handle open drawer from tray menu."""
    def do_drawer():
        import requests
        try:
            requests.post("http://127.0.0.1:5678/open-cashdrawer", timeout=10)
        except Exception as e:
            print(f"Open drawer error: {e}")
    threading.Thread(target=do_drawer, daemon=True).start()


def on_quit(icon, item):
    """Handle quit from tray menu."""
    settings_window.close()
    icon.stop()
    import os
    os._exit(0)  # Force exit to avoid threading cleanup issues


# ---------------- MAIN ----------------

def main():
    print(f"{APP_NAME} v{VERSION}")
    print("=" * 40)

    # Check for updates in background
    print("Checking for updates...")
    threading.Thread(target=check_for_updates, daemon=True).start()

    # Auto-configure printer
    auto_configure()

    # Start Flask server
    print("Starting server...")
    start_flask_server()
    print("Server running on http://127.0.0.1:5678")

    # Create and run tray icon
    print("Starting system tray...")
    print("=" * 40)

    icon = create_tray_icon()
    icon.run()


if __name__ == "__main__":
    main()
