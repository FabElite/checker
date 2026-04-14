import tkinter as tk
from tkinter import messagebox, ttk, filedialog
import asyncio
import threading
import logging
import logging.handlers
from shared_lib.bluetooth_manager import BLEManager
from bleak import BleakError
from concurrent.futures import ThreadPoolExecutor
import csv
import struct
import re

def get_version() -> str:
    """
    Restituisce la stringa di versione con questa priorità:
    1. _version.py  — generato dal build script, presente nell'exe PyInstaller
    2. git describe  — disponibile in sviluppo se il repo ha almeno un tag
    3. "dev"         — fallback
    """
    # 1. File generato dal build — presente nel bundle _MEIPASS di PyInstaller
    import sys as _sys_v, os as _os_v
    if getattr(_sys_v, "frozen", False):
        # Aggiunge _MEIPASS al path così import _version trova il file
        meipass = getattr(_sys_v, "_MEIPASS", None)
        if meipass and meipass not in _sys_v.path:
            _sys_v.path.insert(0, meipass)
    try:
        import _version
        return _version.__version__
    except ImportError:
        pass

    # 2. git describe (solo in sviluppo)
    import subprocess, os, sys
    try:
        cwd = (os.path.dirname(sys.executable)
               if getattr(sys, "frozen", False)
               else os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            ["git", "describe", "--tags", "--dirty", "--always"],
            capture_output=True, text=True, timeout=3,
            cwd=cwd
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    return "dev"


APP_VERSION = get_version()

import os as _os, sys as _sys, configparser as _cp

def _app_dir() -> str:
    """
    Restituisce la cartella "radice" dell'applicazione:
    - Se frozen (exe PyInstaller): cartella che contiene checker.exe
    - Se script Python:            cartella che contiene main.py
    Usata per logs/ e settings.ini, che devono stare accanto all'exe,
    NON nella cartella temporanea _MEIPASS di PyInstaller.
    """
    if getattr(_sys, "frozen", False):
        # exe PyInstaller: sys.executable = .../dist/checker.exe
        return _os.path.dirname(_sys.executable)
    return _os.path.dirname(_os.path.abspath(__file__))

_BASE_DIR     = _app_dir()
LOG_DIR       = _os.path.join(_BASE_DIR, "logs")
LOG_FILENAME  = _os.path.join(LOG_DIR, "app.log")
SETTINGS_FILE = _os.path.join(_BASE_DIR, "settings.ini")
_os.makedirs(LOG_DIR, exist_ok=True)
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Parametri per la lettura a blocchi
READ_MAX_GAP = 8     # byte: gap massimo tollerato tra due parametri per unirli nello stesso chunk
READ_MAX_CHUNK = 128 # byte: dimensione massima di un singolo chunk di lettura
READ_RETRIES = 3     # Numero di tentativi in caso di fallimento

# ── Spaziatura uniforme ───────────────────────────────────────────────────────
PAD = 8
PAD_SM = 4
PAD_LG = 12


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                       STATUS BAR (3 RIGHE INTEGRATE)                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝
class TriStatusBar(tk.Frame):
    """
    Barra di stato compatta a tre righe:
      riga 0: LED UI (heartbeat), LED BLE, label stato BLE, device info
      riga 1: riga attività (testo con severità: info|warn|error|success)
      riga 2: progress bar (indeterminate|determinate)
    """
    _BG = "#1e1e2e"
    _LED_COLORS = {
        "ok":   "#00cc44",
        "err":  "#cc2222",
        "warn": "#cc8800",
        "off":  "#555555",
    }
    _SEV_COLORS = {
        "info":    "#cfd2ff",
        "warn":    "#ffcc66",
        "error":   "#ff7777",
        "success": "#88ffaa",
    }

    def __init__(self, parent):
        super().__init__(parent, bg=self._BG, pady=6)
        # ─ riga 0
        self._phase = False

        # Blocco LED UI (colonna 0-1)
        led_ui_frame = tk.Frame(self, bg=self._BG)
        led_ui_frame.grid(row=0, column=0, padx=(10, 4))
        self.led_ui = tk.Label(led_ui_frame, text="●", bg=self._BG, fg="#555555", font=("Helvetica", 16))
        self.led_ui.grid(row=0, column=0)
        tk.Label(led_ui_frame, text="UI", bg=self._BG, fg="#666688", font=("Helvetica", 7)).grid(row=1, column=0)

        # Separatore verticale
        tk.Frame(self, bg="#444466", width=1).grid(row=0, column=1, sticky="ns", padx=(2, 4), pady=4)

        # Blocco LED BLE (colonna 2-3)
        led_ble_frame = tk.Frame(self, bg=self._BG)
        led_ble_frame.grid(row=0, column=2, padx=(4, 6))
        self.led_ble = tk.Label(led_ble_frame, text="●", bg=self._BG, fg="#555555", font=("Helvetica", 16))
        self.led_ble.grid(row=0, column=0)
        tk.Label(led_ble_frame, text="BLE", bg=self._BG, fg="#666688", font=("Helvetica", 7)).grid(row=1, column=0)

        # Testo stato BLE + info dispositivo
        self.lbl_ble = tk.Label(self, text="BLE: Disconnesso", bg=self._BG, fg="#aaaaaa", font=("Helvetica", 9, "bold"))
        self.lbl_dev = tk.Label(self, text="—", bg=self._BG, fg="#777799", font=("Helvetica", 8))
        self.lbl_ble.grid(row=0, column=3, sticky="w", padx=(2, 8))
        self.lbl_dev.grid(row=0, column=4, padx=(4, 10), sticky="w")

        # ─ riga 1 (attività + checkbox autoscroll a destra)
        self._activity_var = tk.StringVar(value="")
        self.lbl_activity = tk.Label(self, textvariable=self._activity_var, bg=self._BG, fg=self._SEV_COLORS["info"], font=("Helvetica", 9))
        self.lbl_activity.grid(row=1, column=0, columnspan=5, sticky="w", padx=10, pady=(2, 0))
        # Checkbox auto-scroll – inserita dall'app dopo init
        self._autoscroll_placeholder = tk.Frame(self, bg=self._BG)
        self._autoscroll_placeholder.grid(row=1, column=99, sticky="e", padx=(4, 10), pady=(2, 0))

        # ─ riga 2 (progress)
        self.progress = ttk.Progressbar(self, orient="horizontal", mode="indeterminate", length=260)
        self.progress.grid(row=2, column=0, columnspan=99, sticky="ew", padx=10, pady=(4, 4))
        self.grid_columnconfigure(99, weight=1)

    # ── API pubblica ─────────────────────────────────────────────────────────
    def pulse(self):
        """Battito LED UI: indica che l'UI è viva."""
        self._phase = not self._phase
        self.led_ui.config(fg=("#44dd66" if self._phase else "#008822"))

    def set_ble(self, state: str):
        """Imposta stato BLE: 'ok' | 'err' | 'warn' | 'off'."""
        col = self._LED_COLORS.get(state, "#555555")
        self.led_ble.config(fg=col)
        if state == "ok":
            self.lbl_ble.config(text="BLE: Connesso", fg="#44ff88")
        elif state == "err":
            self.lbl_ble.config(text="BLE: Errore", fg="#ff6666")
        elif state == "warn":
            self.lbl_ble.config(text="BLE: Attenzione", fg="#ffcc66")
        else:
            self.lbl_ble.config(text="BLE: Disconnesso", fg="#aaaaaa")

    def set_device_info(self, name: str | None, address: str | None):
        """Mostra/azzera info dispositivo collegato."""
        if name and address:
            self.lbl_dev.config(text=f"{name} - {address}", fg="#88ffaa")
        elif address:
            self.lbl_dev.config(text=f"{address}", fg="#88ffaa")
        else:
            self.lbl_dev.config(text="—", fg="#777799")

    def set_activity(self, text: str, severity: str = "info"):
        """Imposta testo attività con severità: info|warn|error|success."""
        self._activity_var.set(text)
        self.lbl_activity.config(fg=self._SEV_COLORS.get(severity, self._SEV_COLORS["info"]))

    # Progress helpers
    def progress_mode(self, mode: str):
        """'indeterminate' | 'determinate'"""
        self.progress.config(mode=mode)

    def progress_start(self):
        self.progress.start(10)

    def progress_stop(self):
        self.progress.stop()

    def progress_set(self, value: float):
        """Set range 0..100 (determinate)."""
        self.progress["value"] = max(0, min(100, value))


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                              LOG HANDLER TK                              ║
# ╚══════════════════════════════════════════════════════════════════════════╝
class TkTextHandler(logging.Handler):
    """
    Handler di logging che scrive i messaggi nel widget Text di Tkinter.
    Thread-safe: usa root.after() per aggiornare la GUI dal thread corretto.
    autoscroll_var: BooleanVar opzionale; se False non chiama see("end").
    """
    LEVEL_TAGS = {
        logging.DEBUG: "debug",
        logging.INFO: "info",
        logging.WARNING: "warning",
        logging.ERROR: "error",
        logging.CRITICAL: "critical",
    }

    def __init__(self, text_widget, autoscroll_var=None):
        super().__init__()
        self.text_widget = text_widget
        self.autoscroll_var = autoscroll_var
        # Configura i tag colore nel widget
        text_widget.tag_configure("debug", foreground="gray")
        text_widget.tag_configure("info", foreground="black")
        text_widget.tag_configure("warning", foreground="darkorange")
        text_widget.tag_configure("error", foreground="red")
        text_widget.tag_configure("critical", foreground="red", font=("Helvetica", 10, "bold"))

    def emit(self, record):
        msg = self.format(record)
        tag = self.LEVEL_TAGS.get(record.levelno, "info")

        def _append():
            try:
                self.text_widget.configure(state="normal")
                self.text_widget.insert("end", msg + "\n", tag)
                if self.autoscroll_var is None or self.autoscroll_var.get():
                    self.text_widget.see("end")
            finally:
                self.text_widget.configure(state="disabled")

        # Schedula l'aggiornamento nel thread principale di Tkinter
        self.text_widget.after(0, _append)


def setup_style():
    """Configura font e padding mantenendo il tema di sistema."""
    style = ttk.Style()
    # Font e padding uniformi
    style.configure("TButton", font=("Helvetica", 10), padding=(8, 4))
    style.configure("TLabel", font=("Helvetica", 10))
    style.configure("TEntry", font=("Helvetica", 10), padding=3)
    style.configure("Status.TLabel", font=("Helvetica", 10, "bold"))
    # Treeview: font leggibile (da 7pt a 9pt) e righe più alte
    style.configure("Treeview", font=("Helvetica", 9), rowheight=22)
    style.configure("Treeview.Heading", font=("Helvetica", 9, "bold"))


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                             APPLICAZIONE GUI                             ║
# ╚══════════════════════════════════════════════════════════════════════════╝
class BluetoothApp:
    """
    Classe principale per la gestione dell'applicazione Bluetooth.
    """
    ble_manager = BLEManager()

    def __init__(self, main_window):
        """Inizializza l'applicazione e configura l'interfaccia utente."""
        # Path iniziali dei file
        self.default_kmap_file_path = None
        self.default_resmap_file_path = None
        self.serial_finder_file_path = None

        self.executor = ThreadPoolExecutor(max_workers=5)

        self._ble_loop = None
        self._ble_loop_thread = None
        self._ble_loop_ready = threading.Event()
        self._init_ble_loop()

        # Configurazione finestra principale
        self.root = main_window
        self.root.title(f"Checker  {APP_VERSION}")

        # Variabili di stato applicazione
        self.is_scanning = False

        # Carica preferenze persistenti (settings.ini)
        _saved = self._load_settings()
        self.log_autoscroll = tk.BooleanVar(value=_saved.get("log_autoscroll", True))
        self.log_debug      = tk.BooleanVar(value=_saved.get("log_debug", False))
        self.log_autoscroll.trace_add("write", lambda *_: self._save_settings())
        self.log_debug.trace_add("write", self._on_log_debug_changed)
        self._save_settings()   # crea settings.ini subito al primo avvio

        # Configura il logger (file handler subito, TkTextHandler dopo create_widgets)
        self._setup_file_logging()

        # Configura gli stili e crea i widget
        setup_style()
        self.create_widgets()

        # Heartbeat UI
        self._pulse_heartbeat()

    # ── Logging su file + UI ─────────────────────────────────────────────────
    def _load_settings(self) -> dict:
        """Legge settings.ini e restituisce le preferenze salvate."""
        cfg = _cp.ConfigParser()
        cfg.read(SETTINGS_FILE, encoding="utf-8")
        result = {}
        if "ui" in cfg:
            result["log_autoscroll"] = cfg["ui"].getboolean("log_autoscroll", fallback=True)
            result["log_debug"]      = cfg["ui"].getboolean("log_debug",      fallback=True)
        return result

    def _save_settings(self, *_):
        """Salva le preferenze correnti in settings.ini."""
        cfg = _cp.ConfigParser()
        cfg["ui"] = {
            "log_autoscroll": str(self.log_autoscroll.get()),
            "log_debug":      str(self.log_debug.get()),
        }
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            cfg.write(f)

    def _on_log_debug_changed(self, *_):
        """Aggiorna il livello del root logger quando cambia la checkbox Debug."""
        level = logging.DEBUG if self.log_debug.get() else logging.INFO
        logging.getLogger().setLevel(level)
        self._save_settings()

    def _setup_file_logging(self):
        """
        Configura il root logger con handler su file rotativo (logs/app.log).
        Rotazione: 5 MB x 3 backup.
        - Il livello dipende dalla preferenza log_debug.
        - bleak/winrt/asyncio restano sempre a WARNING (troppo rumorosi).
        - shared_lib resta a INFO (niente raw byte di notifiche BLE).
        """
        formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILENAME, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)   # filtra il root logger, non l'handler

        initial_level = logging.DEBUG if self.log_debug.get() else logging.INFO
        root_logger = logging.getLogger()
        root_logger.setLevel(initial_level)
        root_logger.addHandler(file_handler)

        # Silenzia librerie terze rumorose (scanner BLE: decine di righe per scansione)
        for noisy in ("bleak", "winrt", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        logging.getLogger("shared_lib").setLevel(logging.INFO)

        self.log = logging.getLogger(__name__)
        self.log.info(f"Applicazione avviata — versione {APP_VERSION} — debug={'on' if self.log_debug.get() else 'off'}")
        self.log.info(f"Log: {LOG_FILENAME}")

    def _attach_ui_logging(self, text_widget):
        """Aggiunge il TkTextHandler al root logger dopo che il widget è pronto."""
        formatter = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
        tk_handler = TkTextHandler(text_widget, autoscroll_var=self.log_autoscroll)
        tk_handler.setFormatter(formatter)
        tk_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(tk_handler)

    # ── UI ───────────────────────────────────────────────────────────────────
    def create_widgets(self):
        # Frame principale verticale
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill="both", expand=True)
        main_frame.rowconfigure(0, weight=0)  # status bar
        main_frame.rowconfigure(1, weight=1)  # paned top area
        main_frame.rowconfigure(2, weight=0)  # log header
        main_frame.rowconfigure(3, weight=0)  # log area
        main_frame.columnconfigure(0, weight=1)

        # ─ Status Bar a 3 righe
        self.status_bar = TriStatusBar(main_frame)
        self.status_bar.grid(row=0, column=0, sticky="ew")

        # ─ PanedWindow nella parte superiore
        paned_window = ttk.PanedWindow(main_frame, orient="horizontal")
        paned_window.grid(row=1, column=0, sticky="nsew")

        # Frame sinistro (controlli)
        left_frame = ttk.Frame(paned_window)
        paned_window.add(left_frame, weight=1)

        # Frame destro (matrice)
        right_frame = ttk.Frame(paned_window)
        paned_window.add(right_frame, weight=1)

        # Contenuti del frame sinistro
        self.create_left_widgets(left_frame)

        # Contenuti del frame destro
        self.create_right_widgets(right_frame)

        # ─ Log attività in basso a tutta larghezza
        main_frame.rowconfigure(2, weight=0)
        main_frame.rowconfigure(3, weight=0)
        main_frame.rowconfigure(4, weight=0)

        ttk.Label(main_frame, text="Log Attività",
                  font=("Helvetica", 9, "bold")).grid(
            row=2, column=0, sticky="w", padx=10, pady=(4, 0))

        log_frame = ttk.Frame(main_frame)
        log_frame.grid(row=3, column=0, sticky="ew", padx=10, pady=(2, 0))
        log_frame.columnconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=7, state="disabled", wrap="none",
                                relief="sunken", borderwidth=1)
        self.log_text.grid(row=0, column=0, sticky="ew")

        scrollbar_v = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar_v.grid(row=0, column=1, sticky="ns")
        scrollbar_h = ttk.Scrollbar(log_frame, orient="horizontal", command=self.log_text.xview)
        scrollbar_h.grid(row=1, column=0, sticky="ew")
        self.log_text.configure(yscrollcommand=scrollbar_v.set, xscrollcommand=scrollbar_h.set)

        # Footer con checkbox in basso a sinistra, sotto il log
        log_footer = ttk.Frame(main_frame)
        log_footer.grid(row=4, column=0, sticky="w", padx=10, pady=(2, 8))
        ttk.Checkbutton(log_footer, text="Auto-scroll",
                        variable=self.log_autoscroll).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(log_footer, text="Debug",
                        variable=self.log_debug).pack(side="left")

        # Collega il TkTextHandler ora che il widget è pronto
        self._attach_ui_logging(self.log_text)

    def create_left_widgets(self, frame):
        # ─ Dispositivi Bluetooth
        device_frame = ttk.LabelFrame(frame, text="Dispositivi Bluetooth")
        device_frame.pack(fill="x", padx=PAD_LG, pady=PAD_LG)

        self.device_list = tk.Listbox(device_frame, height=6, width=40)
        self.device_list.grid(row=0, column=0, padx=PAD_SM, pady=PAD_SM)

        device_controls = ttk.Frame(device_frame)
        device_controls.grid(row=0, column=1, padx=PAD, sticky="n")

        self.refresh_button = ttk.Button(device_controls, text="Scansiona", command=self.search_devices)
        self.refresh_button.grid(row=0, column=0, pady=PAD_SM, sticky="ew")

        self.connect_button = ttk.Button(device_controls, text="Connetti", command=self.connect_device)
        self.connect_button.grid(row=1, column=0, pady=PAD_SM, sticky="ew")

        self.disconnect_button = ttk.Button(
            device_controls, text="Disconnetti", command=self.disconnect_device, state="disabled"
        )
        self.disconnect_button.grid(row=2, column=0, pady=PAD_SM, sticky="ew")

        # ─ Lettura EEPROM
        read_frame = ttk.LabelFrame(frame, text="Lettura EEPROM")
        read_frame.pack(fill="x", padx=PAD_LG, pady=(0, PAD))
        read_frame.columnconfigure(1, weight=1)

        ttk.Label(read_frame, text="Indirizzo:").grid(row=0, column=0, padx=PAD, pady=PAD_SM, sticky="w")
        self.read_address_entry = ttk.Entry(read_frame, width=12)
        self.read_address_entry.insert(0, "0x0016")
        self.read_address_entry.bind("<FocusIn>", lambda e: self.read_address_entry.delete(0,"end") if self.read_address_entry.get()=="0x0016" else None)
        self.read_address_entry.grid(row=0, column=1, padx=PAD_SM, pady=PAD_SM, sticky="w")

        ttk.Label(read_frame, text="N. byte:").grid(row=1, column=0, padx=PAD, pady=PAD_SM, sticky="w")
        self.data_size_entry = ttk.Entry(read_frame, width=6)
        self.data_size_entry.grid(row=1, column=1, padx=PAD_SM, pady=PAD_SM, sticky="w")

        self.read_button = ttk.Button(read_frame, text="Leggi", command=self.on_read_button_pressed)
        self.read_button.grid(row=0, column=2, rowspan=2, padx=PAD_SM, pady=PAD_SM, sticky="ns")

        # Output lettura: tabella per-byte (Offset | HEX | DEC | ASCII)
        result_lbl_frame = ttk.Frame(read_frame)
        result_lbl_frame.grid(row=2, column=0, columnspan=3, padx=PAD, pady=(PAD_SM, PAD), sticky="ew")
        result_lbl_frame.columnconfigure(0, weight=1)

        ttk.Label(result_lbl_frame, text="Risultato:").grid(row=0, column=0, sticky="w")

        self.copy_to_write_btn = ttk.Button(result_lbl_frame, text="→ Copia in Scrittura", command=self._copy_read_to_write)
        self.copy_to_write_btn.grid(row=0, column=1, sticky="e")
        self.copy_to_write_btn.config(state="disabled")

        read_tree_frame = ttk.Frame(read_frame)
        read_tree_frame.grid(row=3, column=0, columnspan=3, padx=PAD_SM, pady=(0, PAD), sticky="ew")
        read_tree_frame.columnconfigure(0, weight=1)

        self.read_tree = ttk.Treeview(
            read_tree_frame,
            columns=("Offset", "HEX", "DEC", "ASCII"),
            show="headings",
            height=4
        )
        self.read_tree.heading("Offset", text="Offset")
        self.read_tree.heading("HEX", text="HEX")
        self.read_tree.heading("DEC", text="DEC")
        self.read_tree.heading("ASCII", text="ASCII")
        self.read_tree.column("Offset", width=55, minwidth=45, anchor="center", stretch=False)
        self.read_tree.column("HEX", width=55, minwidth=45, anchor="center", stretch=False)
        self.read_tree.column("DEC", width=55, minwidth=45, anchor="center", stretch=False)
        self.read_tree.column("ASCII", width=55, minwidth=45, anchor="center", stretch=True)
        self.read_tree.grid(row=0, column=0, sticky="ew")
        self.read_tree.tag_configure("oddrow", background="lightgrey")
        self.read_tree.tag_configure("evenrow", background="white")

        read_tree_scroll = ttk.Scrollbar(read_tree_frame, orient="vertical", command=self.read_tree.yview)
        self.read_tree.configure(yscrollcommand=read_tree_scroll.set)
        read_tree_scroll.grid(row=0, column=1, sticky="ns")

        # ─ Scrittura EEPROM
        write_frame = ttk.LabelFrame(frame, text="Scrittura EEPROM")
        write_frame.pack(fill="x", padx=PAD_LG, pady=(0, PAD_LG))
        write_frame.columnconfigure(1, weight=1)

        ttk.Label(write_frame, text="Indirizzo (es. 0x0016):").grid(row=0, column=0, padx=PAD, pady=PAD_SM, sticky="w")
        self.write_address_entry = ttk.Entry(write_frame, width=12)
        self.write_address_entry.grid(row=0, column=1, padx=PAD_SM, pady=PAD_SM, sticky="w")

        self.write_button = ttk.Button(write_frame, text="Scrivi", command=self.write_data_manually)
        self.write_button.grid(row=0, column=2, padx=PAD_SM, pady=PAD_SM, sticky="ew")

        ttk.Label(write_frame, text="Dati hex (es. 03 66 36):").grid(row=1, column=0, padx=PAD, pady=PAD_SM, sticky="w")
        data_entry_frame = ttk.Frame(write_frame)
        data_entry_frame.grid(row=1, column=1, columnspan=2, padx=PAD_SM, pady=PAD_SM, sticky="ew")
        data_entry_frame.columnconfigure(0, weight=1)

        # tk.Entry per poter impostare highlightcolor (validazione visuale)
        self.data_entry = tk.Entry(
            data_entry_frame,
            font=("Courier New", 10),
            relief="sunken", borderwidth=1,
            highlightthickness=2,
            highlightbackground="gray", highlightcolor="gray",
            insertbackground="black"
        )
        self.data_entry.grid(row=0, column=0, sticky="ew")
        # Auto-spacing: inserisce uno spazio ogni 2 caratteri hex
        self.data_entry.bind("<KeyRelease>", self._on_data_entry_key)

        ttk.Button(
            data_entry_frame, text="✕ Pulisci",
            command=lambda: (self.data_entry.delete(0, "end"), self._validate_hex_input(self.data_entry))
        ).grid(row=0, column=1, padx=(PAD_SM, 0))

    def create_right_widgets(self, frame):
        # ─ Tabella parametri
        param_frame = ttk.LabelFrame(frame, text="Parametri")
        param_frame.pack(fill="both", expand=True, padx=PAD_LG, pady=PAD_LG)
        param_frame.grid_rowconfigure(0, weight=1)
        param_frame.grid_columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            param_frame,
            columns=("Nome", "Indirizzo", "Tipo", "Da Scrivere", "Letti"),
            show="headings", height=20
        )
        self.tree.heading("Nome", text="Nome Parametro")
        self.tree.heading("Indirizzo", text="Indirizzo")
        self.tree.heading("Tipo", text="Tipo")
        self.tree.heading("Da Scrivere", text="Da Scrivere")
        self.tree.heading("Letti", text="Letti")
        self.tree.column("Nome", width=160, minwidth=100, stretch=True)
        self.tree.column("Indirizzo", width=80, minwidth=60, stretch=False, anchor="center")
        self.tree.column("Tipo", width=75, minwidth=55, stretch=False, anchor="center")
        self.tree.column("Da Scrivere", width=100, minwidth=80, stretch=True, anchor="center")
        self.tree.column("Letti", width=100, minwidth=80, stretch=True, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.tag_configure("oddrow", background="lightgrey")
        self.tree.tag_configure("evenrow", background="white")
        self.tree.tag_configure("mismatch", background="#FEF3C7")  # giallo chiaro

        scrollbar = ttk.Scrollbar(param_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.load_config_parameters()

        # ─ Barra pulsanti
        buttons_frame = ttk.Frame(frame)
        buttons_frame.pack(fill="x", padx=PAD_LG, pady=(0, PAD))

        # Azioni safe – sinistra
        self.carica_btn = ttk.Button(buttons_frame, text="Carica Parametri", command=self.load_new_config)
        self.carica_btn.pack(side="left", padx=(0, PAD_SM))

        self.leggi_btn = ttk.Button(buttons_frame, text="Leggi Parametri", command=self.scarica_parametri)
        self.leggi_btn.pack(side="left", padx=(0, PAD_SM))

        self.verifica_btn = ttk.Button(buttons_frame, text="Leggi e Verifica", command=self.lettura_e_verifica)
        self.verifica_btn.pack(side="left", padx=(0, PAD_SM))

        self.salva_btn = ttk.Button(buttons_frame, text="Salva come CSV", command=self.save_as_csv)
        self.salva_btn.pack(side="left", padx=(0, PAD_SM))

        # Azione distruttiva – destra
        self.scrivi_btn = ttk.Button(buttons_frame, text="⚠ Scrivi Parametri", command=self.scrivi_parametri)
        self.scrivi_btn.pack(side="right")

    # ── Funzioni di utilità UI ───────────────────────────────────────────────
    def _validate_hex_input(self, entry_widget, event=None):
        """Valida in tempo reale il campo hex: bordo verde/rosso."""
        val = entry_widget.get().replace(" ", "")
        if val == "":
            entry_widget.config(highlightthickness=0)
            return
        try:
            bytearray.fromhex(val)
            entry_widget.config(highlightbackground="green", highlightcolor="green", highlightthickness=2)
        except ValueError:
            entry_widget.config(highlightbackground="red", highlightcolor="red", highlightthickness=2)

    def _on_data_entry_key(self, event=None):
        """Auto-spacing hex nel campo dati scrittura: inserisce spazio ogni 2 char."""
        # Ignora tasti di navigazione/modifica che non aggiungono testo
        if event and event.keysym in (
            "BackSpace", "Delete", "Left", "Right", "Home", "End",
            "Tab", "Return", "Escape", "Control_L", "Control_R",
            "Shift_L", "Shift_R", "Alt_L", "Alt_R"
        ):
            self._validate_hex_input(self.data_entry)
            return
        widget = self.data_entry
        raw = widget.get().replace(" ", "").upper()
        # Ricostruisce la stringa con spazi ogni 2 caratteri
        spaced = " ".join(raw[i:i+2] for i in range(0, len(raw), 2))
        # Evita di riscrivere se già uguale (previene loop)
        if widget.get() != spaced:
            widget.delete(0, "end")
            widget.insert(0, spaced)
            widget.icursor("end")
        self._validate_hex_input(self.data_entry)

    def _update_read_tree(self, raw_bytes: bytes):
        """Popola il Treeview di risultato lettura con una riga per byte."""
        # Svuota
        for row in self.read_tree.get_children():
            self.read_tree.delete(row)
        # Popola
        for i, b in enumerate(raw_bytes):
            ascii_ch = chr(b) if 32 <= b < 127 else "·"
            tag = "oddrow" if i % 2 == 0 else "evenrow"
            self.read_tree.insert(
                "", "end",
                values=(f"{i}", f"{b:02X}", f"{b}", ascii_ch),
                tags=(tag,)
            )
        # Abilita il pulsante copia
        self.copy_to_write_btn.config(state="normal")
        # Salva i byte grezzi per la funzione copia
        self._last_read_bytes = raw_bytes

    def _copy_read_to_write(self):
        """Copia i byte dell'ultima lettura nel campo dati scrittura."""
        if not hasattr(self, "_last_read_bytes") or not self._last_read_bytes:
            return
        hex_str = " ".join(f"{b:02X}" for b in self._last_read_bytes)
        self.data_entry.delete(0, "end")
        self.data_entry.insert(0, hex_str)
        self._validate_hex_input(self.data_entry)

    # ── Caricamento parametri ────────────────────────────────────────────────
    def load_config_parameters(self, config_file="config.csv"):
        # Pulisci la Treeview
        for item in self.tree.get_children():
            self.tree.delete(item)
        try:
            with open(config_file, 'r', newline='') as f:
                reader = csv.reader(f, delimiter=';')
                next(reader)  # Salta header
                for idx, row in enumerate(reader):
                    if len(row) < 3:
                        continue
                    name = row[0].strip()
                    address = row[1].strip()
                    data_type = row[2].strip()
                    to_write = row[3].strip() if len(row) > 3 else ""
                    # Gestione separatore decimale con virgola per "Da Scrivere" solo se non finisce con 'H'
                    if to_write and data_type.upper() == 'FLOAT32':
                        try:
                            parsed_val = float(to_write.replace(',', '.'))
                            to_write = f"{parsed_val:.10f}".replace('.', ',').rstrip('0').rstrip(',')
                        except ValueError:
                            self.log.error(f"Valore non valido in 'Da Scrivere' per {name}: {to_write}")
                    tag = 'oddrow' if idx % 2 == 0 else 'evenrow'
                    self.tree.insert("", "end", values=(
                        name,
                        address,
                        data_type,
                        to_write,
                        "0"
                    ), tags=(tag,))
        except FileNotFoundError:
            self.log.warning(f"File di configurazione '{config_file}' non trovato. Carico valori di default.")
            for i in range(100):
                tag = 'oddrow' if i % 2 == 0 else 'evenrow'
                self.tree.insert("", "end", values=(f"Parametro {i+1}", "0x0000", "uint8", "", "0"), tags=(tag,))
        except csv.Error:
            self.log.error(f"Errore nel parsing del file '{config_file}'.")

    def load_new_config(self):
        file_path = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv")])
        if file_path:
            self.load_config_parameters(file_path)
            self.log.info(f"Caricato nuovo file di configurazione: {file_path}")

    # ── Pipeline Lettura Parametri (ottimizzata per chunk) ───────────────────
    def scarica_parametri(self):
        if not self.ble_manager.get_connection_status():
            messagebox.showerror("Errore", "Connetti un dispositivo prima di scaricare i parametri.")
            return
        self.log.info("Inizio scaricamento parametri...")

        # Status bar: attività + progress determinato
        self.status_bar.set_activity("📥 Lettura parametri in corso…", "info")
        self.status_bar.progress_mode('determinate')
        self.status_bar.progress_set(0)

        asyncio.run_coroutine_threadsafe(self._scarica_parametri_async(), self._ble_loop)

    def _build_read_chunks(self, params):
        """
        Raggruppa i parametri in chunk di lettura contigui o quasi-contigui.
        Ogni parametro è una tupla: (item_id, name, address, size, data_type).
        Restituisce una lista di chunk, ciascuno nella forma:
        {
            'start': indirizzo di inizio del chunk,
            'size': numero totale di byte da leggere,
            'params': [(item_id, name, address, size, data_type), ...]
        }

        Regole di raggruppamento:
        - Due parametri consecutivi vengono uniti se il gap tra loro è <= READ_MAX_GAP byte.
        - Un chunk viene chiuso se aggiungere il parametro successivo supererebbe READ_MAX_CHUNK.
        - Parametri non validi vengono scartati (log errore).
        """
        # Scarta i parametri non validi e ordina per indirizzo
        valid = []
        for item_id, name, address_str, data_type in params:
            try:
                address = int(address_str, 16)
                size = self.get_size_from_type(data_type)
                valid.append((item_id, name, address, size, data_type))
            except (ValueError, KeyError):
                self.log.error(f"Parametro '{name}' scartato: indirizzo o tipo non valido.")
                # Evidenzia il parametro nel Treeview
                self.root.after(0, lambda it=item_id: (
                    self.tree.set(it, column="Letti", value="⛔ TIPO ERRATO"),
                    self.tree.item(it, tags=["mismatch"])
                ))
        valid.sort(key=lambda p: p[2])  # ordina per address

        chunks = []
        current = None
        for param in valid:
            item_id, name, address, size, data_type = param
            if current is None:
                current = {'start': address, 'size': size, 'params': [param]}
            else:
                chunk_end = current['start'] + current['size']
                gap = address - chunk_end
                new_size = (address + size) - current['start']
                if gap <= READ_MAX_GAP and new_size <= READ_MAX_CHUNK:
                    current['size'] = new_size
                    current['params'].append(param)
                else:
                    chunks.append(current)
                    current = {'start': address, 'size': size, 'params': [param]}
        if current is not None:
            chunks.append(current)
        return chunks

    async def _scarica_parametri_async(self):
        """
        Legge tutti i parametri dal dispositivo e aggiorna il Treeview.
        Restituisce un dict {item_id: valore_letto} per uso immediato
        da parte di _lettura_e_verifica_async (evita la race condition
        con root.after che aggiorna il Treeview in modo asincrono).
        """
        children = list(self.tree.get_children())
        if not children:
            return {}
        letti_map = {}  # item_id → valore interpretato

        # Rimuove highlight mismatch dalla sessione precedente
        for item in children:
            tags = [t for t in self.tree.item(item, "tags") if t != "mismatch"]
            self.tree.item(item, tags=tags)

        # Raccoglie (item_id, name, address_str, data_type)
        raw_params = [
            (item, self.tree.item(item)['values'][0],
             self.tree.item(item)['values'][1],
             self.tree.item(item)['values'][2])
            for item in children
        ]
        chunks = self._build_read_chunks(raw_params)
        total_params = sum(len(c['params']) for c in chunks)
        done = 0

        self.log.info(f"Lettura ottimizzata: {total_params} parametri raggruppati in {len(chunks)} chunk.")
        for chunk in chunks:
            chunk_start = chunk['start']
            chunk_size = chunk['size']
            self.log.debug(f"Chunk 0x{chunk_start:04X} | {chunk_size} byte | {len(chunk['params'])} parametri")

            # Aggiorna activity (seconda riga status bar)
            self.root.after(0, lambda s=chunk_start, z=chunk_size:
                            self.status_bar.set_activity(f"📥 Lettura chunk 0x{s:04X} ({z} byte)…", "info"))

            # Lettura dell'intero chunk
            chunk_data = await self.read_data(chunk_start, chunk_size)
            for item_id, name, address, size, data_type in chunk['params']:
                if chunk_data is not None:
                    offset = address - chunk_start
                    param_bytes = chunk_data[offset: offset + size]
                    if len(param_bytes) == size:
                        value = self.interpret_data(param_bytes, data_type)
                    else:
                        self.log.error(f"'{name}': byte estratti ({len(param_bytes)}) != attesi ({size}).")
                        value = "N/A"
                else:
                    self.log.error(f"'{name}': lettura chunk fallita.")
                    value = "N/A"
                letti_map[item_id] = value
                self.root.after(0, lambda it=item_id, v=value: self.tree.set(it, column="Letti", value=v))

                done += 1
                progress = (done / total_params) * 100
                self.root.after(0, lambda p=progress: self.update_progress_bar(p))

        # ── Log riepilogo lettura ────────────────────────────────────────────
        self.log.info(f"── Lettura parametri: {total_params} letti ──")
        for item in self.tree.get_children():
            v = self.tree.item(item)['values']
            name, addr_str, dtype, _, letto = v
            self.log.info(f"  {name:<30s} {addr_str}  {dtype:<8s}  {letto}")

        # Fine
        self.root.after(0, self._end_activity_success, "✓ Lettura parametri completata.")
        invalid = [
            self.tree.item(it)['values'][0]
            for it in self.tree.get_children()
            if self.tree.item(it)['values'][4] == "⛔ TIPO ERRATO"
        ]
        if invalid:
            self.root.after(0, lambda names=invalid: (
                self.status_bar.set_activity(
                    f"⚠ Lettura completata con {len(names)} tipo/i non valido/i.", "warn"),
                messagebox.showwarning(
                    "Tipi non validi",
                    f"I seguenti parametri hanno un tipo non riconosciuto nel CSV "
                    f"e non sono stati letti:\n\n" + "\n".join(f"  • {n}" for n in names)
                )
            ))
        return letti_map

    def _end_activity_success(self, msg: str):
        self.status_bar.set_activity(msg, "success")
        self.status_bar.progress_mode('indeterminate')
        self.status_bar.progress_stop()

    # ── Pipeline Scrittura Parametri ─────────────────────────────────────────
    def scrivi_parametri(self):
        if not self.ble_manager.get_connection_status():
            messagebox.showerror("Errore", "Connetti un dispositivo prima di scrivere i parametri.")
            return

        # Conta i parametri con un valore da scrivere
        # Nota: str() necessario perché Tkinter restituisce 0 (int) per valori numerici,
        # e `if 0` sarebbe falsy anche quando 0 è un valore legittimo da scrivere.
        n_params = sum(1 for item in self.tree.get_children() if str(self.tree.item(item)['values'][3]) != "")
        if n_params == 0:
            messagebox.showinfo("Scrivi Parametri", "Nessun parametro da scrivere.")
            return

        risposta = messagebox.askokcancel(
            "Conferma Scrittura",
            f"Stai per scrivere {n_params} parametro/i sul dispositivo.\n\n"
            "Questa operazione sovrascrive i valori attuali.\n"
            "Vuoi procedere?"
        )
        if not risposta:
            return

        self.log.info(f"── Scrittura parametri: {n_params} da scrivere ──")
        # Status bar
        self.status_bar.set_activity("📤 Scrittura parametri in corso…", "info")
        self.status_bar.progress_mode('determinate')
        self.status_bar.progress_set(0)

        asyncio.run_coroutine_threadsafe(self._scrivi_parametri_async(), self._ble_loop)

    async def _scrivi_parametri_async(self):
        children = list(self.tree.get_children())
        total = len(children)
        if total == 0:
            return

        n_params = sum(1 for item in children if str(self.tree.item(item)['values'][3]) != "")
        errors = []
        for idx, item in enumerate(children):
            values = self.tree.item(item)['values']
            name, address_str, data_type, to_write, _ = values
            if str(to_write) == "":
                self.log.debug(f"Salto '{name}': nessun valore da scrivere.")
                continue
            try:
                address = int(address_str, 16)
                data = self.prepare_data_for_write(to_write, data_type)
                if data is None:
                    self.log.error(f"Impossibile preparare i dati per '{name}'")
                    errors.append(name)
                    continue

                # Aggiorna activity
                self.root.after(0, lambda n=name, a=address:
                                self.status_bar.set_activity(f"📤 Scrittura '{n}' @ 0x{a:04X}…", "info"))

                success = await self.write_data(address, data)
                if success:
                    hex_suffix = f"  [{data.hex(' ')}]" if self.log_debug.get() else ""
                    self.log.info(f"  SCRITTO  {name:<30s} {address_str}  {data_type:<8s}  {to_write}{hex_suffix}")
                else:
                    self.log.error(f"  ERRORE   {name:<30s} {address_str}  valore={to_write}")
                    errors.append(name)
            except ValueError:
                self.log.error(f"Indirizzo o tipo non valido per '{name}'")
                errors.append(name)
                continue

            progress = ((idx + 1) / total) * 100
            self.root.after(0, lambda p=progress: self.update_progress_bar(p))

        # Ripristino progress
        self.root.after(0, lambda: self.status_bar.progress_mode('indeterminate'))

        # Esito
        ok_count = n_params - len(errors)
        if errors:
            self.log.error(f"── Scrittura completata: OK={ok_count}  KO={len(errors)} ──")
            self.log.error(f"   Falliti: {', '.join(errors)}")
            error_msg = f"Errori nella scrittura di: {', '.join(errors)}"
            self.root.after(0, lambda: (
                self.status_bar.set_activity("⛔ Scrittura parametri con errori.", "error"),
                messagebox.showerror("Scrittura Parametri", error_msg)
            ))
        else:
            self.log.info(f"── Scrittura completata: OK={ok_count}  KO=0 ──")
            self.root.after(0, lambda: (
                self.status_bar.set_activity("✓ Scrittura parametri completata.", "success"),
                messagebox.showinfo("Scrittura Parametri", "Tutti i parametri scritti con successo.")
            ))

    # ── Lettura + Verifica ──────────────────────────────────────────────────
    def lettura_e_verifica(self):
        if not self.ble_manager.get_connection_status():
            messagebox.showerror("Errore", "Connetti un dispositivo prima di verificare i parametri.")
            return

        self.log.info("── Lettura e verifica parametri ──")
        self.status_bar.set_activity("🔎 Lettura+Verifica in corso…", "info")
        self.status_bar.progress_mode('determinate')
        self.status_bar.progress_set(0)

        asyncio.run_coroutine_threadsafe(self._lettura_e_verifica_async(), self._ble_loop)

    async def _lettura_e_verifica_async(self):
        # Prima scarica (legge) i parametri; il dict ritornato ha i valori
        # già disponibili senza aspettare che root.after aggiorni il Treeview.
        letti_map = await self._scarica_parametri_async()

        # Ora verifica usando direttamente letti_map (nessuna race condition)
        children = list(self.tree.get_children())
        errors = []
        for item in children:
            values = self.tree.item(item)['values']
            name, _, data_type, to_write, _ = values
            if str(to_write) == "":
                continue
            letto = letti_map.get(item, "")
            # Normalizza per confronto (stringhe con virgola)
            to_write_norm = str(to_write).replace(',', '.')
            letti_norm    = str(letto).replace(',', '.')
            if to_write_norm != letti_norm:
                errors.append(name)
                tags = [t for t in self.tree.item(item, "tags") if t not in ("oddrow", "evenrow", "mismatch")]
                self.tree.item(item, tags=tags + ["mismatch"])

        # ── Log riepilogo verifica ───────────────────────────────────────────
        verified = sum(1 for item in self.tree.get_children()
                       if str(self.tree.item(item)['values'][3]) != "")  # ha valore atteso
        ok_count = verified - len(errors)
        self.log.info(f"── Verifica parametri: OK={ok_count}  KO={len(errors)} ──")
        if errors:
            self.log.error("  Parametri non conformi (atteso → letto):")
            for item in self.tree.get_children():
                v = self.tree.item(item)['values']
                name, addr_str, dtype, to_write, _ = v
                if str(to_write) == "":
                    continue
                letto = letti_map.get(item, "")
                if str(to_write).replace(',', '.') != str(letto).replace(',', '.'):
                    self.log.error(f"  KO  {name:<30s} {addr_str}  atteso={to_write}  letto={letto}")

        # Esito
        if errors:
            error_msg = f"{len(errors)} parametro/i non conforme/i"
            self.root.after(0, lambda: (
                self.status_bar.set_activity("⚠ Verifica: non conformità riscontrate.", "warn"),
                messagebox.showerror("Verifica", error_msg)
            ))
        else:
            self.root.after(0, lambda: (
                self.status_bar.set_activity("✓ Verifica completata con successo.", "success"),
                messagebox.showinfo("Verifica", "Tutti i parametri verificati con successo.")
            ))

        # Ripristina progress
        self.root.after(0, lambda: self.status_bar.progress_mode('indeterminate'))

    def save_as_csv(self):
        file_path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV files", "*.csv")])
        if file_path:
            with open(file_path, 'w', newline='') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(["Nome Parametro", "Indirizzo (hex)", "Tipo Dato", "Valori Letti"])
                for item in self.tree.get_children():
                    values = self.tree.item(item)['values']
                    writer.writerow([values[0], values[1], values[2], values[4]])  # Solo Nome, Indirizzo, Tipo, Letti
            self.log.info(f"Parametri salvati come CSV: {file_path}")

    def _init_ble_loop(self):
        """Crea un thread dedicato con un event loop asyncio persistente per BLE."""
        def _worker():
            self._ble_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._ble_loop)
            self._ble_loop_ready.set()
            try:
                self._ble_loop.run_forever()
            finally:
                try:
                    if hasattr(self._ble_loop, "shutdown_asyncgens"):
                        self._ble_loop.run_until_complete(self._ble_loop.shutdown_asyncgens())
                except Exception:
                    pass
                self._ble_loop.close()

        self._ble_loop_thread = threading.Thread(target=_worker, name="BLE-Asyncio-Loop", daemon=True)
        self._ble_loop_thread.start()
        self._ble_loop_ready.wait()

    def _shutdown_ble_loop(self, join_timeout=3.0):
        """Ferma il loop BLE e attende il thread."""
        if self._ble_loop is not None:
            try:
                self._ble_loop.call_soon_threadsafe(self._ble_loop.stop)
            except Exception:
                pass
        if self._ble_loop_thread is not None:
            self._ble_loop_thread.join(timeout=join_timeout)

    # ── Scansione / Connessione / Disconnessione ────────────────────────────
    def search_devices(self):
        self.log.info("Avvio scansione dispositivi BLE...")
        self.status_bar.set_activity("📡 Scansione dispositivi…", "info")
        self.status_bar.progress_mode('indeterminate')
        self.status_bar.progress_start()
        self.executor.submit(self._search_devices)

    def _search_devices(self):
        fut = asyncio.run_coroutine_threadsafe(self.ble_manager.scan_devices(timeout=5), self._ble_loop)
        try:
            devices = fut.result()
        except Exception as e:
            self.log.error(f"Errore nella scansione BLE: {e}")
            devices = {}
        self.root.after(0, self._populate_device_list, devices)

    def _populate_device_list(self, devices):
        self.device_list.delete(0, tk.END)
        count = 0
        for address, (name, rssi) in devices.items():
            count += 1
            self.device_list.insert(tk.END, f"{name} - {address} - RSSI: {rssi}")
            if rssi > -50:
                try:
                    self.device_list.itemconfig(tk.END, {'bg': 'lightcoral'})
                except Exception:
                    pass
            self.log.info(f"Dispositivo trovato: {name} | {address} | RSSI: {rssi}")
        self.status_bar.progress_stop()
        self.status_bar.set_activity(f"✓ Scansione completata: {count} dispositivo/i trovati.", "success")

    def connect_device(self):
        selected_device = self.device_list.get(tk.ACTIVE)
        if not selected_device:
            return
        try:
            parts = selected_device.split(" - ")
            dev_name = parts[0].strip()
            address = parts[1].strip()
        except Exception:
            self.log.error("Formato voce lista dispositivi inatteso: impossibile estrarre l'indirizzo MAC.")
            return

        self.status_bar.set_activity(f"🔄 Connessione a {address}…", "info")
        self.status_bar.progress_mode('indeterminate')
        self.status_bar.progress_start()
        self.executor.submit(self._connect_device, address, dev_name)

    def _connect_device(self, address, dev_name=""):
        self.log.info(f"Tentativo di connessione a {address}...")
        fut = asyncio.run_coroutine_threadsafe(
            self.ble_manager.connect_to_device(address, connection_timeout=15.0),
            self._ble_loop
        )
        try:
            ok = fut.result()
            if ok:
                self.root.after(0, self.on_device_connected, address, dev_name)
            else:
                self.root.after(0, lambda: (
                    self.status_bar.set_ble("err"),
                    self.status_bar.set_activity("⛔ Connessione fallita.", "error")
                ))
        except Exception as e:
            self.log.error(f"Errore durante la connessione BLE: {e}")
            self.root.after(0, lambda: (
                self.status_bar.set_ble("err"),
                self.status_bar.set_activity("⛔ Connessione fallita.", "error")
            ))
        finally:
            self.root.after(0, self.status_bar.progress_stop)

    def on_device_connected(self, addr, dev_name=""):
        """Aggiorna l'interfaccia dopo una connessione riuscita."""
        display = f"{dev_name} [{addr}]" if dev_name else addr
        self.log.info(f"═══ CONNESSO a {display} ═══")
        self.status_bar.set_ble("ok")
        self.status_bar.set_device_info(dev_name, addr)
        self.status_bar.set_activity(f"✓ Connesso a {addr}", "success")
        self.connect_button.config(state="disabled")
        self.disconnect_button.config(state="normal")
        self.monitor_connection()

    def disconnect_device(self):
        """Disconnette il dispositivo BLE in modo asincrono e aggiorna la GUI."""
        if not self.ble_manager.get_connection_status():
            self.log.warning("Nessun dispositivo connesso da disconnettere.")
            return

        self.log.info("Disconnessione in corso...")
        self.status_bar.set_activity("🔌 Disconnessione in corso…", "info")
        self.status_bar.progress_mode('indeterminate')
        self.status_bar.progress_start()

        async def perform_disconnect():
            try:
                await self.ble_manager.disconnect_device()
                self.root.after(0, self.on_disconnect_success)
            except Exception as e:
                self.root.after(0, lambda: self.on_disconnect_error(e))

        asyncio.run_coroutine_threadsafe(perform_disconnect(), self._ble_loop)

    def on_disconnect_success(self):
        """Callback per gestire una disconnessione completata con successo."""
        self.log.info("═══ DISCONNESSO ═══")
        self.status_bar.set_ble("off")
        self.status_bar.set_device_info(None, None)
        self.status_bar.set_activity("✓ Disconnesso.", "success")
        self.status_bar.progress_stop()
        self.connect_button.config(state="normal")
        self.disconnect_button.config(state="disabled")
        messagebox.showinfo("Disconnessione", "Dispositivo disconnesso correttamente.")

    def on_disconnect_error(self, error):
        """Callback per gestire errori durante la disconnessione."""
        self.log.error(f"Errore durante la disconnessione: {error}")
        self.status_bar.set_ble("warn")
        self.status_bar.set_activity("⛔ Errore disconnessione.", "error")
        self.status_bar.progress_stop()
        messagebox.showerror("Errore", f"Errore durante la disconnessione: {error}")

    def monitor_connection(self):
        """Monitora periodicamente lo stato della connessione BLE."""
        def check_connection():
            try:
                if not self.ble_manager.get_connection_status():
                    self.handle_disconnection()
                else:
                    self.root.after(5000, check_connection)
            except Exception as e:
                self.log.error(f"Errore durante il monitoraggio della connessione: {e}")
                self.root.after(5000, check_connection)
        self.root.after(5000, check_connection)

    def handle_disconnection(self):
        """Gestisce la disconnessione del dispositivo."""
        self.ble_manager.reset_connection_state()
        self.status_bar.set_ble("err")
        self.status_bar.set_device_info(None, None)
        self.status_bar.set_activity("⚠ Connessione BLE persa inaspettatamente.", "warn")
        self.connect_button.config(state="normal")
        self.disconnect_button.config(state="disabled")

    # ── Lettura/Scrittura di basso livello ──────────────────────────────────
    async def read_data(self, address, size, timeout=5):
        """
        Legge i dati BLE gestendo la segmentazione (se size > READ_MAX_CHUNK)
        e i tentativi di retry in caso di errore.
        Distingue tra errori transitori (timeout, payload corto) per cui ha
        senso riprovare, ed errori fatali (BleakError) per cui si abbandona.
        """
        full_data = bytearray()
        bytes_read = 0
        while bytes_read < size:
            chunk_to_read = min(size - bytes_read, READ_MAX_CHUNK)
            current_addr = address + bytes_read
            chunk_success = False

            # Activity live
            self.root.after(0, lambda a=current_addr, s=chunk_to_read:
                            self.status_bar.set_activity(f"📥 Leggo 0x{a:04X} ({s} B)…", "info"))

            for attempt in range(READ_RETRIES):
                try:
                    self.log.debug(f"Lettura 0x{current_addr:04X} ({chunk_to_read}b) - Tentativo {attempt + 1}/{READ_RETRIES}")
                    data = await asyncio.wait_for(
                        self.ble_manager.read_eeprom(current_addr, chunk_to_read),
                        timeout=timeout
                    )
                    if data and len(data) >= 5:
                        payload = data[5:]
                        if len(payload) >= chunk_to_read:
                            full_data.extend(payload[:chunk_to_read])
                            chunk_success = True
                            break
                        else:
                            # Payload corto: errore transitorio, si riprova
                            self.log.warning(f"Payload corto a 0x{current_addr:04X}: ricevuti {len(payload)}b, attesi {chunk_to_read}b")
                            self.root.after(0, lambda a=current_addr, r=len(payload), e=chunk_to_read:
                                            self.status_bar.set_activity(f"⚠ Payload corto @0x{a:04X}: {r}/{e} B", "warn"))
                    else:
                        # Risposta nulla o troppo corta: errore transitorio
                        self.log.warning(f"Risposta non valida o nulla a 0x{current_addr:04X}")
                        self.root.after(0, lambda a=current_addr:
                                        self.status_bar.set_activity(f"⚠ Risposta nulla @0x{a:04X}", "warn"))
                except asyncio.TimeoutError:
                    # Errore transitorio: il dispositivo non ha risposto in tempo
                    self.log.warning(f"Timeout al tentativo {attempt + 1}/{READ_RETRIES} per 0x{current_addr:04X}")
                    self.root.after(0, lambda a=current_addr, t=attempt+1:
                                    self.status_bar.set_activity(f"⚠ Timeout @{t}/{READ_RETRIES} @0x{a:04X}", "warn"))
                except BleakError as e:
                    # Errore fatale BLE
                    self.log.error(f"Errore BLE fatale a 0x{current_addr:04X}: {e} — lettura abortita")
                    self.root.after(0, lambda: self.status_bar.set_activity("⛔ Errore BLE: lettura abortita.", "error"))
                    return None
                except Exception as e:
                    # Errore inatteso: loggato come fatale, si abbandona
                    self.log.error(f"Errore imprevisto a 0x{current_addr:04X}: {e} — lettura abortita")
                    self.root.after(0, lambda: self.status_bar.set_activity("⛔ Errore imprevisto: lettura abortita.", "error"))
                    return None

                # Backoff solo per errori transitori
                await asyncio.sleep(0.2)

            if not chunk_success:
                self.log.error(f"Fallimento definitivo dopo {READ_RETRIES} tentativi a 0x{current_addr:04X}")
                self.root.after(0, lambda a=current_addr:
                                self.status_bar.set_activity(f"⛔ Fallimento definitivo @0x{a:04X}", "error"))
                return None

            bytes_read += chunk_to_read

        return full_data

    async def read_data_manually(self):
        try:
            address = int(self.read_address_entry.get(), 16)
            size = int(self.data_size_entry.get())
        except ValueError:
            messagebox.showerror("Errore", "Indirizzo o dimensione non valida.")
            return

        # Status bar: activity + progress indeterminate
        self.status_bar.set_activity(f"📥 Lettura manuale @0x{address:04X} ({size} B)…", "info")
        self.status_bar.progress_mode('indeterminate')
        self.status_bar.progress_start()

        output = await self.read_data(address, size)
        if output:
            self.root.after(0, self._update_read_output, bytes(output))
            self.root.after(0, lambda: self.status_bar.set_activity("✓ Lettura completata.", "success"))
        else:
            self.root.after(0, lambda: self.status_bar.set_activity("⛔ Errore lettura.", "error"))

        self.root.after(0, self.status_bar.progress_stop)

    def on_read_button_pressed(self):
        """Callback associata al pulsante per avviare la lettura."""
        asyncio.run_coroutine_threadsafe(self.read_data_manually(), self._ble_loop)

    def _update_read_output(self, raw_bytes: bytes):
        """Aggiorna il Treeview di output lettura (thread-safe, da after())."""
        self._update_read_tree(raw_bytes)

    def write_data_manually(self):
        """
        Funzione chiamata manualmente dal pulsante di scrittura.
        Ottiene gli indirizzi e i dati dalla GUI e avvia il processo asincrono di scrittura.
        """
        try:
            address = int(self.write_address_entry.get(), 16)  # Indirizzo (in hex)
            data = bytearray.fromhex(self.data_entry.get())    # Dati (in formato hex)
        except ValueError:
            self.log.error("Input manuale non valido: indirizzo o dati in formato errato.")
            messagebox.showerror("Errore", "Indirizzo o dati in formato non valido.")
            return

        self.status_bar.set_activity(f"📤 Scrittura manuale @0x{address:04X}…", "info")
        self.status_bar.progress_mode('indeterminate')
        self.status_bar.progress_start()

        async def _do_write():
            ok = await self.write_data(address, data)
            if ok:
                self.root.after(0, lambda: self.status_bar.set_activity("✓ Scrittura completata.", "success"))
            else:
                self.root.after(0, lambda: self.status_bar.set_activity("⛔ Scrittura fallita.", "error"))
            self.root.after(0, self.status_bar.progress_stop)

        asyncio.run_coroutine_threadsafe(_do_write(), self._ble_loop)
        self.log.info(f"Scrittura manuale avviata: indirizzo={hex(address)}, dati={self.data_entry.get()}")

    async def write_data(self, starting_address, data):
        """Scrive dati su BLE in modo asincrono utilizzando AsyncioWorker."""
        if not self.ble_manager.get_connection_status():
            self.log.error("Scrittura fallita: nessun dispositivo connesso.")
            self.root.after(0, lambda: messagebox.showerror(
                "Errore", "Nessun dispositivo connesso!"))
            return False
        try:
            result = await self.ble_manager.write_eeprom(starting_address, data)
            if not result:
                self.log.error(f"Scrittura a {hex(starting_address)}: dispositivo non ha confermato.")
                self.root.after(0, lambda: messagebox.showerror(
                    "Errore", "Scrittura fallita: dispositivo non ha confermato l'operazione."))
                return False
            else:
                self.log.debug(f"write_eeprom {hex(starting_address)}: confermato dal dispositivo.")
                return True
        except Exception as e:
            self.log.error(f"Errore durante la scrittura all'indirizzo {hex(starting_address)}: {e}")
            self.root.after(0, lambda err=str(e): messagebox.showerror(
                "Errore", f"Errore durante la scrittura: {err}"))
            return False

    # ── Progress wrapper (compatibilità con logica esistente) ───────────────
    def update_progress_bar(self, value):
        """
        Aggiorna il valore della barra di progresso.
        :param value: Valore percentuale (0-100).
        """
        if 0 <= value <= 100:
            self.status_bar.progress_set(value)
            self.root.update_idletasks()
        else:
            self.log.error(f"Valore non valido per la barra di progresso: {value} (atteso 0-100)")

    # ── Helpers formati/interpretazione ─────────────────────────────────────
    def prepare_data_for_write(self, value_str, data_type):
        try:
            data_type = data_type.upper()
            value_str = str(value_str).strip()
            # HEX grezzo (es. "4H", "2H")
            if data_type.endswith('H'):
                clean_hex = value_str.replace(" ", "")
                if len(clean_hex) % 2 != 0:
                    clean_hex = "0" + clean_hex
                return bytearray.fromhex(clean_hex)
            # STRING<N>
            if data_type.startswith('STRING'):
                size = self.get_size_from_type(data_type)
                encoded = value_str.encode('utf-8')
                return encoded[:size].ljust(size, b'\x00')
            # FLOAT32
            if data_type == 'FLOAT32':
                val = float(value_str.replace(',', '.'))
                return struct.pack('<f', val)
            # UINT8[N] — array di byte (es. IP: "192.168.1.1" o "192,168,1,1")
            if re.match(r'^UINT8\[\d+\]$', data_type):
                n = int(re.search(r'\d+', data_type[5:]).group())
                # Accetta "." o "," come separatori (formato usato da interpret_data)
                # Se nessun separatore è presente, il valore è uno scalare: replicalo N volte
                # es. "0" → [0, 0, 0, 0]  oppure  "192.168.1.1" → [192, 168, 1, 1]
                if '.' in value_str or ',' in value_str:
                    sep = ',' if ',' in value_str else '.'
                    parts = [int(x.strip()) for x in value_str.split(sep)]
                    if len(parts) != n:
                        raise ValueError(
                            f"UINT8[{n}]: attesi {n} valori, ricevuti {len(parts)} ('{value_str}')"
                        )
                else:
                    scalar = int(value_str)
                    parts = [scalar] * n
                if any(v < 0 or v > 255 for v in parts):
                    raise ValueError(f"UINT8[{n}]: tutti i valori devono essere 0-255")
                return bytearray(parts)
            # UINT8 / UINT16 / UINT24 / UINT32
            if data_type in ('UINT8', 'UINT16', 'UINT24', 'UINT32'):
                val = int(value_str)
                if data_type == 'UINT8':
                    return struct.pack('<B', val)
                elif data_type == 'UINT16':
                    return struct.pack('<H', val)
                elif data_type == 'UINT24':
                    return struct.pack('<I', val)[:3]
                elif data_type == 'UINT32':
                    return struct.pack('<I', val)
            # Tipo non riconosciuto — errore esplicito, nessun fallback silenzioso
            raise ValueError(f"Tipo dati non riconosciuto: '{data_type}'. "
                             f"Tipi validi: UINT8, UINT16, UINT24, UINT32, FLOAT32, "
                             f"STRING<N>, UINT8[N], <N>H")
        except Exception as e:
            self.log.error(f"Errore preparazione dati per scrittura: {e}")
            return None

    def get_size_from_type(self, data_type):
        data_type = data_type.upper()
        if data_type.endswith('H'):
            data_type = data_type[:-1]  # rimuovi 'H'
        sizes = {
            'UINT8': 1,
            'UINT16': 2,
            'UINT24': 3,
            'UINT32': 4,
            'FLOAT32': 4,
        }
        if data_type.startswith('STRING'):
            match = re.search(r'\d+', data_type)
            return int(match.group()) if match else 20
        # UINT8[N] — array di N byte (es. UINT8[4] per un indirizzo IP)
        if re.match(r'^UINT8\[\d+\]$', data_type):
            return int(re.search(r'\d+', data_type[5:]).group())
        if data_type in sizes:
            return sizes[data_type]
        # Tipo non riconosciuto — errore esplicito, nessun fallback a 1
        raise ValueError(f"Tipo dati non riconosciuto: '{data_type}'. "
                         f"Tipi validi: UINT8, UINT16, UINT24, UINT32, FLOAT32, "
                         f"STRING<N>, UINT8[N], <N>H")

    def interpret_data(self, data, data_type):
        if not data:
            return "N/A"
        data_type = data_type.upper()
        # HEX grezzo
        if data_type.endswith('H'):
            return ' '.join(f'{b:02x}' for b in data)
        # STRING<N>
        if data_type.startswith('STRING'):
            try:
                text = data.decode('utf-8', errors='ignore').split('\x00')[0]
                return text.strip()
            except Exception:
                return "Errore Decodifica"
        # FLOAT32
        if data_type == 'FLOAT32':
            val = struct.unpack_from('<f', data)[0]
            formatted = f"{val:.10f}".replace('.', ',').rstrip('0').rstrip(',')
            return formatted if formatted else "0"
        # UINT8[N] — array di N byte (es. IP: "192.168.1.1")
        if re.match(r'^UINT8\[\d+\]$', data_type):
            return '.'.join(str(b) for b in data)
        # UINT little-endian
        if data_type in ('UINT8', 'UINT16', 'UINT24', 'UINT32'):
            val = 0
            for i, byte in enumerate(data):
                val |= (byte << (8 * i))
            return val
        # Tipo non riconosciuto — errore esplicito, nessun fallback silenzioso
        raise ValueError(
            f"Tipo dati non riconosciuto in interpret_data: '{data_type}'. "
            f"Tipi validi: UINT8, UINT16, UINT24, UINT32, FLOAT32, STRING<N>, UINT8[N], <N>H"
        )

    # ── Heartbeat UI e chiusura ─────────────────────────────────────────────
    def _pulse_heartbeat(self):
        try:
            self.status_bar.pulse()
        except Exception:
            pass
        self.root.after(500, self._pulse_heartbeat)

    def on_close(self):
        self._shutdown_ble_loop(join_timeout=3.0)
        self.executor.shutdown(wait=True)
        self.root.destroy()


# ── Avvio applicazione ───────────────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()
    app = BluetoothApp(root)
    # root.iconbitmap('logo2.ico')
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()