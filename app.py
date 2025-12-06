import json
import logging
import time
import atexit
import platform
import subprocess
import tempfile
from pathlib import Path
from functools import wraps
from flask import Flask, request, jsonify

from escpos.printer import Usb, Network   # python-escpos
import usb.core
import usb.util

# Platform-specific imports for system printing
PLATFORM = platform.system()
if PLATFORM == "Windows":
    try:
        import win32print
        import win32api
        import win32file
        import win32con
        HAS_WIN32PRINT = True
    except ImportError:
        HAS_WIN32PRINT = False
else:
    HAS_WIN32PRINT = False

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("glamiris")

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"


# ---------------- USB CONNECTION MANAGER ----------------

class USBPrinterManager:
    """
    Manages USB printer connections with auto-recovery.
    Handles connection pooling, error recovery, and device reset.
    """

    def __init__(self):
        self._device = None
        self._vid = None
        self._pid = None
        self._last_error_time = 0
        self._error_count = 0
        self._max_retries = 2  # Reduced for faster feedback
        self._retry_delay = 0.3  # Reduced delay
        self._error_cooldown = 5  # seconds before resetting error count

    def configure(self, vid, pid):
        """Set the target printer VID/PID."""
        if self._vid != vid or self._pid != pid:
            self.release()
            self._vid = vid
            self._pid = pid
            self._error_count = 0

    def get_device(self):
        """Get the USB device, reconnecting if necessary."""
        if self._vid is None or self._pid is None:
            raise RuntimeError("Printer not configured")

        # Reset error count if enough time has passed
        if time.time() - self._last_error_time > self._error_cooldown:
            self._error_count = 0

        # Try to find and return device
        dev = usb.core.find(idVendor=self._vid, idProduct=self._pid)
        if dev is None:
            raise RuntimeError(f"Printer not found (VID={hex(self._vid)}, PID={hex(self._pid)}). Check USB connection.")

        return dev

    def release(self):
        """Release USB resources."""
        if self._device:
            try:
                usb.util.dispose_resources(self._device)
            except:
                pass
            self._device = None

    def reset_device(self):
        """Attempt to reset the USB device."""
        try:
            dev = usb.core.find(idVendor=self._vid, idProduct=self._pid)
            if dev:
                try:
                    usb.util.dispose_resources(dev)
                except:
                    pass
                try:
                    dev.reset()
                    logger.info("USB device reset successfully")
                except Exception as e:
                    logger.warning(f"USB reset failed: {e}")
                time.sleep(0.5)  # Wait for device to reinitialize
        except Exception as e:
            logger.error(f"Failed to reset USB device: {e}")

    def execute_with_retry(self, operation, operation_name="operation"):
        """
        Execute a printer operation with automatic retry and recovery.

        Args:
            operation: Callable that takes a USB device and performs the operation
            operation_name: Name for logging purposes

        Returns:
            Result of the operation
        """
        last_error = None

        for attempt in range(self._max_retries):
            try:
                dev = self.get_device()

                # Detach kernel driver if needed (macOS/Linux)
                try:
                    if dev.is_kernel_driver_active(0):
                        dev.detach_kernel_driver(0)
                except (usb.core.USBError, NotImplementedError):
                    pass

                # Set configuration
                try:
                    dev.set_configuration()
                except usb.core.USBError:
                    pass  # May already be configured

                # Execute the operation
                result = operation(dev)

                # Success - release resources
                usb.util.dispose_resources(dev)
                self._error_count = 0
                return result

            except Exception as e:
                last_error = e
                self._error_count += 1
                self._last_error_time = time.time()

                logger.warning(f"{operation_name} failed (attempt {attempt + 1}/{self._max_retries}): {e}")

                # Try to recover
                self.reset_device()
                time.sleep(self._retry_delay * (attempt + 1))  # Exponential backoff

        # All retries failed
        logger.error(f"{operation_name} failed after {self._max_retries} attempts: {last_error}")
        raise last_error

    def get_out_endpoint(self, dev):
        """Find the OUT endpoint for the printer."""
        try:
            cfg = dev.get_active_configuration()
            intf = cfg[(0, 0)]
            ep_out = usb.util.find_descriptor(
                intf,
                custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
            )
            if ep_out:
                return ep_out.bEndpointAddress
        except:
            pass
        return 0x01  # Default OUT endpoint

    def check_health(self):
        """Check if the printer is connected and responsive."""
        try:
            dev = self.get_device()
            usb.util.dispose_resources(dev)
            return {"status": "ok", "connected": True, "vid": hex(self._vid), "pid": hex(self._pid)}
        except Exception as e:
            return {"status": "error", "connected": False, "error": str(e)}


# Global USB printer manager instance
printer_manager = USBPrinterManager()


# ---------------- WINDOWS RAW USB PRINTER ----------------

class WindowsRawUSBPrinter:
    """
    Send ESC/POS commands to USB printers on Windows using win32print RAW mode.
    This bypasses libusb and uses Windows' native USB printer support.
    Works when the printer shows up in Device Manager but libusb can't access it.
    """

    def __init__(self):
        self._printer_name = None
        self._vid = None
        self._pid = None

    def find_printer_by_vid_pid(self, vid, pid):
        """
        Find a Windows printer that matches the given VID/PID.
        Returns the printer name or None.
        """
        if not HAS_WIN32PRINT:
            return None

        # Format VID/PID for matching
        vid_str = f"{vid:04X}".upper()
        pid_str = f"{pid:04X}".upper()
        vid_hex = hex(vid).upper()
        pid_hex = hex(pid).upper()

        logger.info(f"Searching for Windows printer with VID={vid_hex} PID={pid_hex}")

        try:
            # Get all printers
            printers = win32print.EnumPrinters(
                win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            )

            logger.info(f"Found {len(printers)} Windows printers")

            for flags, desc, name, comment in printers:
                # Try to get printer port info
                try:
                    hprinter = win32print.OpenPrinter(name)
                    try:
                        # Get printer info level 2 which includes port name
                        info = win32print.GetPrinter(hprinter, 2)
                        port_name = info.get('pPortName', '').upper()

                        logger.debug(f"Checking printer '{name}' on port '{port_name}'")

                        # USB printers often have port names like "USB001" or contain VID/PID
                        # Some drivers include VID_xxxx&PID_xxxx in the port name
                        if f"VID_{vid_str}" in port_name or f"VID_{vid_hex[2:]}" in port_name:
                            if f"PID_{pid_str}" in port_name or f"PID_{pid_hex[2:]}" in port_name:
                                logger.info(f"Found Windows printer '{name}' matching VID={vid_hex} PID={pid_hex}")
                                return name

                        # Check if it's a USB port and the name suggests it's our printer type
                        if port_name.startswith("USB"):
                            # Could be our printer - check name for thermal/POS keywords
                            name_lower = name.lower()
                            if any(kw in name_lower for kw in ["pos", "thermal", "receipt", "rongta", "58", "80"]):
                                logger.info(f"Found likely USB thermal printer: {name} on {port_name}")
                                return name

                    finally:
                        win32print.ClosePrinter(hprinter)
                except Exception as e:
                    logger.debug(f"Could not get info for printer {name}: {e}")
                    continue

            # If no printer found by VID/PID, try to find ANY printer on a USB port
            # as a fallback (user may have only one USB printer)
            logger.info("No VID/PID match, looking for any USB-connected printer...")
            for flags, desc, name, comment in printers:
                try:
                    hprinter = win32print.OpenPrinter(name)
                    try:
                        info = win32print.GetPrinter(hprinter, 2)
                        port_name = info.get('pPortName', '').upper()
                        if port_name.startswith("USB"):
                            logger.info(f"Found USB printer as fallback: {name} on {port_name}")
                            return name
                    finally:
                        win32print.ClosePrinter(hprinter)
                except:
                    continue

        except Exception as e:
            logger.error(f"Error enumerating Windows printers: {e}")

        logger.warning("No matching Windows printer found")
        return None

    def configure(self, vid, pid):
        """Configure the printer by VID/PID, finding the Windows printer name."""
        self._vid = vid
        self._pid = pid
        self._printer_name = self.find_printer_by_vid_pid(vid, pid)

        if self._printer_name:
            logger.info(f"Windows RAW USB configured: {self._printer_name}")
        else:
            logger.warning(f"No Windows printer found for VID={hex(vid)} PID={hex(pid)}")

    def configure_by_name(self, printer_name):
        """Configure directly by Windows printer name."""
        self._printer_name = printer_name
        logger.info(f"Windows RAW USB configured by name: {printer_name}")

    def send_raw(self, data):
        """Send raw bytes to the printer using Windows Print Spooler RAW mode."""
        if not self._printer_name:
            raise RuntimeError("Windows printer not configured")

        if not HAS_WIN32PRINT:
            raise RuntimeError("win32print not available")

        try:
            hprinter = win32print.OpenPrinter(self._printer_name)
            try:
                # Start a RAW document - this sends bytes directly without processing
                win32print.StartDocPrinter(hprinter, 1, ("ESC/POS", None, "RAW"))
                try:
                    win32print.StartPagePrinter(hprinter)
                    win32print.WritePrinter(hprinter, data)
                    win32print.EndPagePrinter(hprinter)
                finally:
                    win32print.EndDocPrinter(hprinter)
            finally:
                win32print.ClosePrinter(hprinter)

            return True

        except Exception as e:
            raise RuntimeError(f"Windows RAW print failed: {e}")

    def print_text(self, text):
        """Print text with ESC/POS formatting."""
        commands = b"\x1B\x40"  # ESC @ - Initialize
        commands += text.encode('cp437', errors='replace')
        commands += b"\n"
        commands += b"\x1D\x56\x00"  # GS V 0 - Full cut
        return self.send_raw(commands)

    def test_print(self):
        """Send a test print."""
        commands = b"\x1B\x40"  # Initialize
        commands += b"*** Glamiris Test Print ***\n\n"
        commands += b"\x1D\x56\x00"  # Cut
        return self.send_raw(commands)

    def open_cash_drawer(self):
        """Open the cash drawer."""
        # ESC p 0 25 250
        return self.send_raw(b"\x1B\x70\x00\x19\xFA")

    def is_available(self):
        """Check if this printer backend is available."""
        return self._printer_name is not None and HAS_WIN32PRINT


# Global Windows RAW USB printer instance
windows_raw_printer = WindowsRawUSBPrinter()


# ---------------- SYSTEM PRINTER MANAGER ----------------

class SystemPrinterManager:
    """
    Manages system-installed printers via OS print spooler.
    Works with CUPS (macOS/Linux) and Windows Print Spooler.
    """

    def __init__(self):
        self._selected_printer = None

    def detect_printers(self):
        """Detect system-installed printers."""
        printers = []

        if PLATFORM == "Darwin" or PLATFORM == "Linux":
            # Use CUPS via lpstat
            printers = self._detect_cups_printers()
        elif PLATFORM == "Windows" and HAS_WIN32PRINT:
            printers = self._detect_windows_printers()

        return printers

    def _detect_cups_printers(self):
        """Detect printers via CUPS (macOS/Linux)."""
        printers = []
        try:
            # Get list of printers
            result = subprocess.run(
                ["lpstat", "-p", "-d"],
                capture_output=True,
                text=True,
                timeout=5
            )

            # Parse printer names
            for line in result.stdout.split("\n"):
                if line.startswith("printer "):
                    parts = line.split()
                    if len(parts) >= 2:
                        name = parts[1]
                        # Check if it's a receipt/POS printer by name
                        is_receipt = self._is_likely_receipt_printer(name)
                        printers.append({
                            "name": name,
                            "type": "system",
                            "backend": "cups",
                            "is_receipt_printer": is_receipt
                        })

            # Get default printer
            default = None
            for line in result.stdout.split("\n"):
                if "system default destination:" in line:
                    default = line.split(":")[-1].strip()
                    break

            # Mark default
            for p in printers:
                p["is_default"] = (p["name"] == default)

        except Exception as e:
            logger.error(f"CUPS detection failed: {e}")

        return printers

    def _detect_windows_printers(self):
        """Detect printers via Windows Print Spooler."""
        printers = []
        try:
            if HAS_WIN32PRINT:
                # Get all printers
                printer_list = win32print.EnumPrinters(
                    win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
                )
                default = win32print.GetDefaultPrinter()

                for flags, desc, name, comment in printer_list:
                    is_receipt = self._is_likely_receipt_printer(name)
                    printers.append({
                        "name": name,
                        "type": "system",
                        "backend": "win32",
                        "description": desc,
                        "is_receipt_printer": is_receipt,
                        "is_default": (name == default)
                    })
        except Exception as e:
            logger.error(f"Windows printer detection failed: {e}")

        return printers

    def _is_likely_receipt_printer(self, name):
        """Check if printer name suggests it's a receipt/POS printer."""
        name_lower = name.lower()
        receipt_keywords = [
            "receipt", "pos", "thermal", "star", "epson", "tm-", "tsp",
            "bixolon", "citizen", "rongta", "xprinter", "zjiang", "sewoo",
            "brother", "ql-", "label", "zebra", "zanprint", "futureprnt"
        ]
        return any(kw in name_lower for kw in receipt_keywords)

    def configure(self, printer_name):
        """Set the target system printer."""
        self._selected_printer = printer_name

    def print_text(self, text):
        """Print text via system printer."""
        if not self._selected_printer:
            raise RuntimeError("No system printer configured")

        if PLATFORM == "Darwin" or PLATFORM == "Linux":
            return self._print_cups(text)
        elif PLATFORM == "Windows" and HAS_WIN32PRINT:
            return self._print_windows(text)
        else:
            raise RuntimeError("System printing not supported on this platform")

    def _print_cups(self, text):
        """Print via CUPS (macOS/Linux)."""
        # Create temp file with text
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(text)
            temp_path = f.name

        try:
            # Print using lp command
            result = subprocess.run(
                ["lp", "-d", self._selected_printer, temp_path],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                raise RuntimeError(f"CUPS print failed: {result.stderr}")

            return True
        finally:
            # Clean up temp file
            try:
                Path(temp_path).unlink()
            except:
                pass

    def _print_windows(self, text):
        """Print via Windows Print Spooler with ESC/POS commands."""
        if not HAS_WIN32PRINT:
            raise RuntimeError("win32print not available")

        try:
            # Open printer
            hprinter = win32print.OpenPrinter(self._selected_printer)
            try:
                # Start document in RAW mode
                win32print.StartDocPrinter(hprinter, 1, ("Receipt", None, "RAW"))
                try:
                    win32print.StartPagePrinter(hprinter)
                    # Build ESC/POS commands
                    data = b"\x1B\x40"  # ESC @ - Initialize printer
                    data += text.encode('cp437', errors='replace')
                    data += b"\n"
                    data += b"\x1D\x56\x00"  # GS V 0 - Full cut
                    # Send raw ESC/POS data
                    win32print.WritePrinter(hprinter, data)
                    win32print.EndPagePrinter(hprinter)
                finally:
                    win32print.EndDocPrinter(hprinter)
            finally:
                win32print.ClosePrinter(hprinter)
            return True
        except Exception as e:
            raise RuntimeError(f"Windows print failed: {e}")

    def open_cash_drawer(self):
        """
        Open cash drawer via system printer.
        Sends ESC/POS command through the print spooler.
        """
        if not self._selected_printer:
            raise RuntimeError("No system printer configured")

        # ESC p 0 25 250 - Cash drawer kick
        drawer_cmd = b"\x1B\x70\x00\x19\xFA"

        if PLATFORM == "Darwin" or PLATFORM == "Linux":
            return self._send_raw_cups(drawer_cmd)
        elif PLATFORM == "Windows" and HAS_WIN32PRINT:
            return self._send_raw_windows(drawer_cmd)
        else:
            raise RuntimeError("Cash drawer not supported on this platform")

    def _send_raw_cups(self, data):
        """Send raw bytes via CUPS."""
        with tempfile.NamedTemporaryFile(mode='wb', suffix='.bin', delete=False) as f:
            f.write(data)
            temp_path = f.name

        try:
            result = subprocess.run(
                ["lp", "-d", self._selected_printer, "-o", "raw", temp_path],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                raise RuntimeError(f"CUPS raw send failed: {result.stderr}")

            return True
        finally:
            try:
                Path(temp_path).unlink()
            except:
                pass

    def _send_raw_windows(self, data):
        """Send raw bytes via Windows Print Spooler."""
        if not HAS_WIN32PRINT:
            raise RuntimeError("win32print not available")

        try:
            hprinter = win32print.OpenPrinter(self._selected_printer)
            try:
                job = win32print.StartDocPrinter(hprinter, 1, ("CashDrawer", None, "RAW"))
                try:
                    win32print.StartPagePrinter(hprinter)
                    win32print.WritePrinter(hprinter, data)
                    win32print.EndPagePrinter(hprinter)
                finally:
                    win32print.EndDocPrinter(hprinter)
            finally:
                win32print.ClosePrinter(hprinter)
            return True
        except Exception as e:
            raise RuntimeError(f"Windows raw send failed: {e}")

    def check_health(self):
        """Check if the system printer is available."""
        if not self._selected_printer:
            return {"status": "error", "connected": False, "error": "Not configured"}

        printers = self.detect_printers()
        for p in printers:
            if p["name"] == self._selected_printer:
                return {"status": "ok", "connected": True, "printer": self._selected_printer}

        return {"status": "error", "connected": False, "error": "Printer not found"}


# Global system printer manager instance
system_printer_manager = SystemPrinterManager()


def cleanup_on_exit():
    """Release USB resources on application exit."""
    logger.info("Cleaning up USB resources...")
    printer_manager.release()


atexit.register(cleanup_on_exit)


# ---------------- RETRY DECORATOR ----------------

def with_printer_retry(operation_name):
    """Decorator to add retry logic to printer endpoints."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.error(f"{operation_name} error: {e}")
                # Try to recover for next request
                printer_manager.reset_device()
                return jsonify({"error": str(e)}), 500
        return wrapper
    return decorator


# ---------------- PRINTER DETECTION ----------------
# USB Class 7 = Printer
USB_CLASS_PRINTER = 7


def is_usb_printer(device):
    """
    Check if a USB device is a printer by examining:
    1. Device class = 7 (Printer)
    2. Interface class = 7 (Printer)
    3. Product string contains printer-related keywords
    """
    # Check device-level class
    if device.bDeviceClass == USB_CLASS_PRINTER:
        return True

    # Check interface-level class (most printers use this)
    try:
        for cfg in device:
            for intf in cfg:
                if intf.bInterfaceClass == USB_CLASS_PRINTER:
                    return True
    except:
        pass

    # Check product string for printer keywords
    try:
        product = usb.util.get_string(device, device.iProduct) or ""
        product_lower = product.lower()
        printer_keywords = ["printer", "receipt", "pos", "thermal", "escpos", "esc/pos"]
        if any(kw in product_lower for kw in printer_keywords):
            return True
    except:
        pass

    return False


def detect_thermal_printers():
    """Scan USB devices and return printers (detected by USB class, not VID/PID)."""
    devices = usb.core.find(find_all=True)
    printers = []

    for d in devices:
        if not is_usb_printer(d):
            continue

        # Get device strings
        try:
            manufacturer = usb.util.get_string(d, d.iManufacturer) or ""
        except:
            manufacturer = ""

        try:
            product = usb.util.get_string(d, d.iProduct) or ""
        except:
            product = ""

        printers.append({
            "idVendor": d.idVendor,
            "idProduct": d.idProduct,
            "vendorId": hex(d.idVendor),
            "productId": hex(d.idProduct),
            "manufacturer": manufacturer,
            "product": product,
        })

    return printers


# ---------------- CONFIG HELPERS ----------------

def load_config():
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "backend": "usb",
        "usb": None,
        "network": {"host": "", "port": 9100}
    }


def save_config(cfg):
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def is_printer_connected(usb_cfg):
    """Check if the configured USB printer is actually connected."""
    if not usb_cfg:
        return False

    try:
        vid = parse_int_maybe_hex(usb_cfg["idVendor"])
        pid = parse_int_maybe_hex(usb_cfg["idProduct"])
        device = usb.core.find(idVendor=vid, idProduct=pid)
        return device is not None
    except:
        return False


def auto_configure():
    """
    Auto-detect and configure the best available printer.
    Priority:
    1. Check if existing config is valid
    2. Try USB/ESC/POS printers (direct, no driver needed)
    3. Fall back to system printers (driver-based)

    Returns True if a printer was configured, False otherwise.
    """
    cfg = load_config()

    # Check if USB printer is configured AND actually connected
    if cfg.get("backend") == "usb" and cfg.get("usb"):
        if is_printer_connected(cfg["usb"]):
            usb_cfg = cfg["usb"]
            print(f"[Auto-Config] USB printer already configured and connected: "
                  f"VID={hex(parse_int_maybe_hex(usb_cfg['idVendor']))}, "
                  f"PID={hex(parse_int_maybe_hex(usb_cfg['idProduct']))}")
            # Configure the manager
            printer_manager.configure(
                parse_int_maybe_hex(usb_cfg['idVendor']),
                parse_int_maybe_hex(usb_cfg['idProduct'])
            )
            return True
        else:
            print("[Auto-Config] Configured USB printer not found, re-scanning...")

    # Check if system printer is configured
    if cfg.get("backend") == "system" and cfg.get("system", {}).get("name"):
        system_name = cfg["system"]["name"]
        system_printer_manager.configure(system_name)
        health = system_printer_manager.check_health()
        if health["connected"]:
            print(f"[Auto-Config] System printer already configured: {system_name}")
            return True
        else:
            print(f"[Auto-Config] Configured system printer '{system_name}' not found, re-scanning...")

    # Check if network printer is configured
    if cfg.get("backend") == "network" and cfg.get("network", {}).get("host"):
        print(f"[Auto-Config] Network printer configured: {cfg['network']['host']}")
        return True

    # On Windows, prefer system printers (driver-based) because libusb often conflicts with Windows drivers
    # On macOS/Linux, prefer USB direct access (no driver needed)

    if PLATFORM == "Windows":
        # WINDOWS: Try system printers first (more reliable)
        print("[Auto-Config] Windows detected - checking system printers first...")
        system_printers = system_printer_manager.detect_printers()

        # Get detailed port info for each printer to find USB-connected ones
        usb_port_printers = []
        receipt_printers = []

        if HAS_WIN32PRINT:
            for p in system_printers:
                try:
                    hprinter = win32print.OpenPrinter(p["name"])
                    try:
                        info = win32print.GetPrinter(hprinter, 2)
                        port = info.get('pPortName', '').upper()
                        p["port"] = port
                        # Prefer printers on USB ports (USB001, USB002, etc.)
                        if port.startswith("USB"):
                            usb_port_printers.append(p)
                            print(f"[Auto-Config] Found USB-connected printer: {p['name']} on {port}")
                    finally:
                        win32print.ClosePrinter(hprinter)
                except:
                    pass

                if p.get("is_receipt_printer"):
                    receipt_printers.append(p)

        # Priority: USB-connected receipt printers > USB printers > receipt printers > default
        if usb_port_printers:
            # Prefer USB receipt printers
            usb_receipt = [p for p in usb_port_printers if p.get("is_receipt_printer")]
            available_printers = usb_receipt if usb_receipt else usb_port_printers
        elif receipt_printers:
            available_printers = receipt_printers
        else:
            available_printers = system_printers

        if available_printers:
            # Take first available (already prioritized)
            printer = available_printers[0]

            print(f"[Auto-Config] Found system printer: {printer['name']} (port: {printer.get('port', 'unknown')})")

            cfg["backend"] = "system"
            cfg["system"] = {
                "name": printer["name"],
                "is_receipt_printer": printer.get("is_receipt_printer", False)
            }
            cfg["usb"] = None
            cfg["network"] = {"host": "", "port": 9100}
            save_config(cfg)

            # Configure manager
            system_printer_manager.configure(printer["name"])

            print(f"[Auto-Config] System printer configured successfully!")
            return True

        # Fall back to USB on Windows (may not work without Zadig driver)
        print("[Auto-Config] No system printers found, trying USB...")

    # STEP 1: Scan for USB/ESC/POS printers (preferred on macOS/Linux)
    print("[Auto-Config] Scanning for USB printers...")
    usb_printers = detect_thermal_printers()

    if usb_printers:
        printer = usb_printers[0]
        print(f"[Auto-Config] Found USB printer: {printer['manufacturer']} {printer['product']} "
              f"(VID={printer['vendorId']}, PID={printer['productId']})")

        cfg["backend"] = "usb"
        cfg["usb"] = {
            "idVendor": hex(printer["idVendor"]),
            "idProduct": hex(printer["idProduct"]),
            "interface": 0
        }
        cfg["network"] = {"host": "", "port": 9100}
        cfg["system"] = None
        save_config(cfg)

        # Configure manager
        printer_manager.configure(printer["idVendor"], printer["idProduct"])

        print(f"[Auto-Config] USB printer configured successfully!")
        return True

    # STEP 2: Fall back to system printers (for macOS/Linux if USB not found)
    if PLATFORM != "Windows":
        print("[Auto-Config] No USB printers found, checking system printers...")
    system_printers = system_printer_manager.detect_printers()

    # Prefer receipt printers
    receipt_printers = [p for p in system_printers if p.get("is_receipt_printer")]
    available_printers = receipt_printers if receipt_printers else system_printers

    if available_printers:
        # Prefer default printer if it's a receipt printer
        default_printer = next((p for p in available_printers if p.get("is_default")), None)
        printer = default_printer or available_printers[0]

        print(f"[Auto-Config] Found system printer: {printer['name']}")

        cfg["backend"] = "system"
        cfg["system"] = {
            "name": printer["name"],
            "is_receipt_printer": printer.get("is_receipt_printer", False)
        }
        cfg["usb"] = None
        cfg["network"] = {"host": "", "port": 9100}
        save_config(cfg)

        # Configure manager
        system_printer_manager.configure(printer["name"])

        print(f"[Auto-Config] System printer configured successfully!")
        return True

    print("[Auto-Config] No printers detected")
    return False


def detect_all_printers():
    """Detect all available printers (USB + System)."""
    result = {
        "usb": [],
        "system": [],
        "total": 0
    }

    # USB printers
    try:
        result["usb"] = detect_thermal_printers()
    except Exception as e:
        logger.error(f"USB detection error: {e}")

    # System printers
    try:
        result["system"] = system_printer_manager.detect_printers()
    except Exception as e:
        logger.error(f"System printer detection error: {e}")

    result["total"] = len(result["usb"]) + len(result["system"])
    return result


def parse_int_maybe_hex(value):
    if isinstance(value, int):
        return value
    s = str(value).strip()
    return int(s, 0)  # handles “1234” or “0x4b8”


# ---------------- PRINTER CREATION ----------------

def get_printer():
    cfg = load_config()
    backend = cfg.get("backend", "usb")

    if backend == "usb":
        usb_cfg = cfg.get("usb")
        if not usb_cfg:
            raise RuntimeError("USB printer not configured")

        vid = parse_int_maybe_hex(usb_cfg["idVendor"])
        pid = parse_int_maybe_hex(usb_cfg["idProduct"])
        interface = int(usb_cfg.get("interface", 0))

        return Usb(vid, pid, interface)

    elif backend == "network":
        net = cfg.get("network") or {}
        host = net.get("host")
        port = int(net.get("port", 9100))

        if not host:
            raise RuntimeError("Network printer host not set")

        return Network(host, port=port)

    else:
        raise RuntimeError("Unknown backend type")


# ---------------- ENDPOINTS ----------------

@app.get("/status")
def status():
    cfg = load_config()
    return jsonify({
        "status": "ok",
        "agent": "Glamiris Python Print Agent",
        "version": "1.0.11",
        "configured": cfg.get("usb") or cfg.get("network")
    })


@app.get("/detect-usb")
def detect_usb():
    """List all connected USB devices (for debugging)."""
    devices = usb.core.find(find_all=True)
    results = []

    for d in devices:
        try:
            manufacturer = usb.util.get_string(d, d.iManufacturer) or ""
        except:
            manufacturer = ""

        try:
            product = usb.util.get_string(d, d.iProduct) or ""
        except:
            product = ""

        results.append({
            "vendorId": hex(d.idVendor),
            "productId": hex(d.idProduct),
            "manufacturer": manufacturer,
            "product": product
        })

    return jsonify({"devices": results})


@app.get("/debug-windows-printers")
def debug_windows_printers():
    """Debug endpoint to see all Windows printers and their ports."""
    if PLATFORM != "Windows" or not HAS_WIN32PRINT:
        return jsonify({"error": "Only available on Windows", "platform": PLATFORM})

    printers = []
    try:
        printer_list = win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        )

        for flags, desc, name, comment in printer_list:
            printer_info = {
                "name": name,
                "description": desc,
                "comment": comment,
                "flags": flags
            }

            # Try to get detailed info
            try:
                hprinter = win32print.OpenPrinter(name)
                try:
                    info = win32print.GetPrinter(hprinter, 2)
                    printer_info["port"] = info.get('pPortName', '')
                    printer_info["driver"] = info.get('pDriverName', '')
                    printer_info["status"] = info.get('Status', 0)
                finally:
                    win32print.ClosePrinter(hprinter)
            except Exception as e:
                printer_info["error"] = str(e)

            printers.append(printer_info)

    except Exception as e:
        return jsonify({"error": str(e)})

    return jsonify({
        "windows_printers": printers,
        "count": len(printers),
        "default": win32print.GetDefaultPrinter() if HAS_WIN32PRINT else None
    })


@app.get("/detect-printers")
def detect_printers_endpoint():
    """Detect all available printers (USB + System)."""
    all_printers = detect_all_printers()
    cfg = load_config()

    return jsonify({
        "usb_printers": all_printers["usb"],
        "system_printers": all_printers["system"],
        "total": all_printers["total"],
        "current_config": {
            "backend": cfg.get("backend"),
            "usb": cfg.get("usb"),
            "system": cfg.get("system"),
            "network": cfg.get("network")
        }
    })


@app.post("/reconfigure")
def reconfigure():
    """
    Force re-detection and auto-configuration.
    Clears existing config and re-scans for USB and system printers.
    """
    # Clear existing config
    cfg = load_config()
    cfg["usb"] = None
    cfg["system"] = None
    cfg["backend"] = None
    save_config(cfg)

    # Run auto-configuration
    success = auto_configure()
    cfg = load_config()  # Reload after auto_configure

    if not success:
        return jsonify({
            "status": "no_printer_found",
            "message": "No printers detected (USB or system)",
            "printers": detect_all_printers()
        }), 404

    return jsonify({
        "status": "ok",
        "message": f"Configured {cfg.get('backend')} printer",
        "config": {
            "backend": cfg.get("backend"),
            "usb": cfg.get("usb"),
            "system": cfg.get("system")
        },
        "all_printers": detect_all_printers()
    })


@app.post("/set-usb-printer")
def set_usb():
    data = request.get_json(force=True, silent=True) or {}

    if "idVendor" not in data or "idProduct" not in data:
        return jsonify({"error": "Missing VID/PID"}), 400

    cfg = load_config()
    cfg["backend"] = "usb"
    cfg["usb"] = {
        "idVendor": data["idVendor"],
        "idProduct": data["idProduct"],
        "interface": data.get("interface", 0)
    }
    save_config(cfg)

    return jsonify({"status": "ok"})


@app.post("/set-network-printer")
def set_network():
    data = request.get_json(force=True, silent=True) or {}

    host = data.get("host")
    if not host:
        return jsonify({"error": "host required"}), 400

    cfg = load_config()
    cfg["backend"] = "network"
    cfg["network"] = {
        "host": host,
        "port": int(data.get("port", 9100))
    }
    cfg["usb"] = None
    cfg["system"] = None
    save_config(cfg)

    return jsonify({"status": "ok"})


@app.post("/set-system-printer")
def set_system_printer():
    """Configure a system-installed printer (driver-based)."""
    data = request.get_json(force=True, silent=True) or {}

    name = data.get("name")
    if not name:
        return jsonify({"error": "Printer name required"}), 400

    # Verify printer exists
    system_printers = system_printer_manager.detect_printers()
    found = any(p["name"] == name for p in system_printers)

    if not found:
        return jsonify({"error": f"Printer '{name}' not found"}), 404

    cfg = load_config()
    cfg["backend"] = "system"
    cfg["system"] = {"name": name}
    cfg["usb"] = None
    cfg["network"] = {"host": "", "port": 9100}
    save_config(cfg)

    # Configure manager
    system_printer_manager.configure(name)

    return jsonify({"status": "ok"})


@app.post("/print")
@with_printer_retry("Print")
def do_print():
    body = request.get_json(force=True, silent=True) or {}
    text = body.get("text")

    if not text:
        return jsonify({"error": "Missing text"}), 400

    cfg = load_config()
    backend = cfg.get("backend", "usb")

    # SYSTEM PRINTER (driver-based)
    if backend == "system":
        system_cfg = cfg.get("system")
        if not system_cfg or not system_cfg.get("name"):
            return jsonify({"error": "System printer not configured"}), 400

        try:
            system_printer_manager.configure(system_cfg["name"])
            system_printer_manager.print_text(text + "\n")
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # NETWORK PRINTER
    if backend == "network":
        try:
            printer = get_printer()
            printer.text(text + "\n")
            printer.cut()
            printer.close()
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # USB PRINTER (ESC/POS direct)
    usb_cfg = cfg.get("usb")
    if not usb_cfg:
        return jsonify({"error": "USB printer not configured"}), 400

    vid = parse_int_maybe_hex(usb_cfg["idVendor"])
    pid = parse_int_maybe_hex(usb_cfg["idProduct"])

    # On Windows, try Windows RAW printing first (bypasses libusb issues)
    if PLATFORM == "Windows" and HAS_WIN32PRINT:
        try:
            windows_raw_printer.configure(vid, pid)
            if windows_raw_printer.is_available():
                windows_raw_printer.print_text(text)
                return jsonify({"status": "ok"})
        except Exception as e:
            logger.warning(f"Windows RAW print failed, trying libusb: {e}")

    # Try libusb (works on macOS/Linux, may fail on Windows)
    printer_manager.configure(vid, pid)

    def print_operation(dev):
        ep_out = printer_manager.get_out_endpoint(dev)
        # Initialize printer, send text, cut
        commands = b"\x1B\x40"  # ESC @ - Initialize
        commands += (text + "\n").encode('cp437', errors='replace')
        commands += b"\x1D\x56\x00"  # GS V 0 - Full cut
        dev.write(ep_out, commands)
        return True

    printer_manager.execute_with_retry(print_operation, "Print")
    return jsonify({"status": "ok"})


@app.post("/test-print")
@with_printer_retry("Test Print")
def test_print():
    cfg = load_config()
    backend = cfg.get("backend", "usb")

    # SYSTEM PRINTER
    if backend == "system":
        system_cfg = cfg.get("system")
        if not system_cfg or not system_cfg.get("name"):
            return jsonify({"error": "System printer not configured"}), 400

        try:
            system_printer_manager.configure(system_cfg["name"])
            system_printer_manager.print_text("*** Glamiris Test Print ***\n\n")
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # NETWORK PRINTER
    if backend == "network":
        try:
            printer = get_printer()
            printer.text("*** Glamiris Test Print ***\n\n")
            printer.cut()
            printer.close()
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # USB PRINTER
    usb_cfg = cfg.get("usb")
    if not usb_cfg:
        return jsonify({"error": "USB printer not configured"}), 400

    vid = parse_int_maybe_hex(usb_cfg["idVendor"])
    pid = parse_int_maybe_hex(usb_cfg["idProduct"])

    # On Windows, try Windows RAW printing first (bypasses libusb issues)
    if PLATFORM == "Windows" and HAS_WIN32PRINT:
        try:
            windows_raw_printer.configure(vid, pid)
            if windows_raw_printer.is_available():
                windows_raw_printer.test_print()
                return jsonify({"status": "ok"})
        except Exception as e:
            logger.warning(f"Windows RAW print failed, trying libusb: {e}")

    # Try libusb (works on macOS/Linux, may fail on Windows)
    printer_manager.configure(vid, pid)

    def test_print_operation(dev):
        ep_out = printer_manager.get_out_endpoint(dev)
        commands = b"\x1B\x40"  # ESC @ - Initialize
        commands += b"*** Glamiris Test Print ***\n\n"
        commands += b"\x1D\x56\x00"  # GS V 0 - Full cut
        dev.write(ep_out, commands)
        return True

    printer_manager.execute_with_retry(test_print_operation, "Test Print")
    return jsonify({"status": "ok"})


@app.post("/open-cashdrawer")
@with_printer_retry("Cash Drawer")
def open_cashdrawer():
    """Open cash drawer - works with USB and system printers."""
    cfg = load_config()
    backend = cfg.get("backend", "usb")

    # SYSTEM PRINTER
    if backend == "system":
        system_cfg = cfg.get("system")
        if not system_cfg or not system_cfg.get("name"):
            return jsonify({"error": "System printer not configured"}), 400

        try:
            system_printer_manager.configure(system_cfg["name"])
            system_printer_manager.open_cash_drawer()
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # NETWORK PRINTER - send ESC/POS command
    if backend == "network":
        try:
            printer = get_printer()
            printer._raw(b"\x1B\x70\x00\x19\xFA")
            printer.close()
            return jsonify({"status": "ok"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # USB PRINTER
    usb_cfg = cfg.get("usb")
    if not usb_cfg:
        return jsonify({"error": "USB printer not configured"}), 400

    vid = parse_int_maybe_hex(usb_cfg["idVendor"])
    pid = parse_int_maybe_hex(usb_cfg["idProduct"])

    # On Windows, try Windows RAW printing first (bypasses libusb issues)
    if PLATFORM == "Windows" and HAS_WIN32PRINT:
        try:
            windows_raw_printer.configure(vid, pid)
            if windows_raw_printer.is_available():
                windows_raw_printer.open_cash_drawer()
                return jsonify({"status": "ok"})
        except Exception as e:
            logger.warning(f"Windows RAW cash drawer failed, trying libusb: {e}")

    # Try libusb (works on macOS/Linux, may fail on Windows)
    printer_manager.configure(vid, pid)

    def cashdrawer_operation(dev):
        ep_out = printer_manager.get_out_endpoint(dev)
        # ESC p 0 25 250 - Kick drawer 1
        dev.write(ep_out, b"\x1B\x70\x00\x19\xFA")
        return True

    printer_manager.execute_with_retry(cashdrawer_operation, "Cash Drawer")
    return jsonify({"status": "ok"})


@app.get("/health")
def health_check():
    """
    Health check endpoint for monitoring printer connectivity.
    Returns detailed status of the printer connection.
    """
    cfg = load_config()
    backend = cfg.get("backend")

    result = {
        "agent": "Glamiris Print Agent",
        "version": "1.0.11",
        "server": "running",
        "backend": backend,
        "configured": bool(cfg.get("usb") or cfg.get("system") or cfg.get("network", {}).get("host")),
    }

    # Check health based on backend type
    if backend == "usb":
        usb_cfg = cfg.get("usb")
        if usb_cfg:
            vid = parse_int_maybe_hex(usb_cfg["idVendor"])
            pid = parse_int_maybe_hex(usb_cfg["idProduct"])
            printer_manager.configure(vid, pid)

            health = printer_manager.check_health()
            result["printer"] = health

            if health["connected"]:
                result["status"] = "healthy"
            else:
                result["status"] = "degraded"
                result["message"] = "Printer not connected"
        else:
            result["status"] = "unconfigured"
            result["message"] = "No USB printer configured"

    elif backend == "system":
        system_cfg = cfg.get("system")
        if system_cfg and system_cfg.get("name"):
            system_printer_manager.configure(system_cfg["name"])
            health = system_printer_manager.check_health()
            result["printer"] = health

            if health["connected"]:
                result["status"] = "healthy"
            else:
                result["status"] = "degraded"
                result["message"] = "System printer not available"
        else:
            result["status"] = "unconfigured"
            result["message"] = "No system printer configured"

    elif backend == "network":
        net_cfg = cfg.get("network", {})
        if net_cfg.get("host"):
            result["printer"] = {"host": net_cfg["host"], "port": net_cfg.get("port", 9100)}
            result["status"] = "configured"  # Can't easily verify network printer
        else:
            result["status"] = "unconfigured"
            result["message"] = "No network printer configured"

    else:
        result["status"] = "unconfigured"
        result["message"] = "No printer configured"

    return jsonify(result)


@app.post("/test-direct-usb")
def test_direct_usb():
    """
    Test direct USB communication bypassing Windows Print Spooler.
    Uses libusb to send ESC/POS commands directly to the USB device.
    """
    # Find the USB printer
    usb_printers = detect_thermal_printers()

    if not usb_printers:
        return jsonify({"error": "No USB printer detected", "hint": "Check USB connection"}), 404

    printer = usb_printers[0]
    vid = printer["idVendor"]
    pid = printer["idProduct"]

    result = {
        "printer": {
            "vid": hex(vid),
            "pid": hex(pid),
            "manufacturer": printer.get("manufacturer", ""),
            "product": printer.get("product", "")
        },
        "steps": []
    }

    try:
        # Step 1: Find device
        result["steps"].append("Finding USB device...")
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None:
            return jsonify({"error": "Device not found", "result": result}), 500
        result["steps"].append(f"Found device: {vid:04x}:{pid:04x}")

        # Step 2: Detach kernel driver if needed
        try:
            if dev.is_kernel_driver_active(0):
                result["steps"].append("Detaching kernel driver...")
                dev.detach_kernel_driver(0)
                result["steps"].append("Kernel driver detached")
            else:
                result["steps"].append("No kernel driver attached")
        except (usb.core.USBError, NotImplementedError) as e:
            result["steps"].append(f"Kernel driver check: {e}")

        # Step 3: Set configuration
        try:
            result["steps"].append("Setting USB configuration...")
            dev.set_configuration()
            result["steps"].append("Configuration set")
        except usb.core.USBError as e:
            result["steps"].append(f"Set config (may already be set): {e}")

        # Step 4: Find OUT endpoint
        result["steps"].append("Finding OUT endpoint...")
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        ep_out = None
        for ep in intf:
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_OUT:
                ep_out = ep
                break

        if ep_out is None:
            # Try default endpoint
            ep_out_addr = 0x01
            result["steps"].append(f"No OUT endpoint found, using default 0x01")
        else:
            ep_out_addr = ep_out.bEndpointAddress
            result["steps"].append(f"Found OUT endpoint: {hex(ep_out_addr)}")

        # Step 5: Send test print
        result["steps"].append("Sending ESC/POS test print...")
        commands = b"\x1B\x40"  # Initialize
        commands += b"=== DIRECT USB TEST ===\n"
        commands += b"If you see this, USB works!\n"
        commands += b"VID: " + f"{vid:04x}".encode() + b"\n"
        commands += b"PID: " + f"{pid:04x}".encode() + b"\n"
        commands += b"\n\n"
        commands += b"\x1D\x56\x00"  # Cut

        bytes_written = dev.write(ep_out_addr, commands)
        result["steps"].append(f"Wrote {bytes_written} bytes")

        # Cleanup
        usb.util.dispose_resources(dev)
        result["steps"].append("USB resources released")

        result["status"] = "ok"
        result["message"] = "Direct USB print sent successfully"
        return jsonify(result)

    except usb.core.USBError as e:
        result["steps"].append(f"USB Error: {e}")
        result["status"] = "error"
        result["error"] = str(e)
        result["error_code"] = e.errno if hasattr(e, 'errno') else None
        return jsonify(result), 500
    except Exception as e:
        result["steps"].append(f"Error: {e}")
        result["status"] = "error"
        result["error"] = str(e)
        return jsonify(result), 500


@app.post("/reset-usb")
def reset_usb():
    """
    Manually reset the USB connection.
    Use this if the printer becomes unresponsive.
    """
    cfg = load_config()
    usb_cfg = cfg.get("usb")

    if not usb_cfg:
        return jsonify({"error": "USB printer not configured"}), 400

    vid = parse_int_maybe_hex(usb_cfg["idVendor"])
    pid = parse_int_maybe_hex(usb_cfg["idProduct"])
    printer_manager.configure(vid, pid)
    printer_manager.reset_device()

    # Check if reset worked
    health = printer_manager.check_health()

    return jsonify({
        "status": "ok" if health["connected"] else "error",
        "message": "USB reset completed",
        "printer": health
    })


if __name__ == "__main__":
    print("Glamiris Python Print Agent")
    print("=" * 40)

    # Auto-detect and configure printer on startup
    auto_configure()

    print("=" * 40)
    print("Server running on http://127.0.0.1:5678")
    print("Endpoints:")
    print("  GET  /status          - Check agent status")
    print("  GET  /detect-printers - List thermal printers")
    print("  POST /reconfigure     - Re-detect and configure")
    print("  POST /print           - Print text")
    print("  POST /test-print      - Test print")
    print("  POST /open-cashdrawer - Open cash drawer")
    print("=" * 40)

    app.run(host="127.0.0.1", port=5678)