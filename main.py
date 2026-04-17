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

from ui_widgets import (
    TriStatusBar, TkTextHandler, TreeviewTypeTooltip,
    setup_style, TIPI_SUPPORTATI,
    PAD, PAD_SM, PAD_LG,
)


def get_version() -> str:
    """
    Restituisce la stringa di versione con questa priorità:
    1. _version.py  — generato dal build script, presente nell'exe PyInstaller
    2. git describe  — disponibile in sviluppo se il repo ha almeno un tag
    3. "dev"         — fallback
    """
    import sys as _sys_v
    if getattr(_sys_v, "frozen", False):
        meipass = getattr(_sys_v, "_MEIPASS", None)
        if meipass and meipass not in _sys_v.path:
            _sys_v.path.insert(0, meipass)
    try:
        import _version
        return _version.__version__
    except ImportError:
        pass

    import subprocess, os, sys
    try:
        cwd = (os.path.dirname(sys.executable)
               if getattr(sys, "frozen", False)
               else os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            ["git", "describe", "--tags", "--dirty", "--always"],
            capture_output=True, text=True, timeout=3, cwd=cwd
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    return "dev"


APP_VERSION = get_version()

import os as _os, sys as _sys, configparser as _cp

def _app_dir() -> str:
    if getattr(_sys, "frozen", False):
        return _os.path.dirname(_sys.executable)
    return _os.path.dirname(_os.path.abspath(__file__))

_BASE_DIR     = _app_dir()
LOG_DIR       = _os.path.join(_BASE_DIR, "logs")
LOG_FILENAME  = _os.path.join(LOG_DIR, "app.log")
SETTINGS_FILE = _os.path.join(_BASE_DIR, "settings.ini")
_os.makedirs(LOG_DIR, exist_ok=True)
LOG_FORMAT      = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Parametri per la lettura a blocchi
READ_MAX_GAP   = 8    # byte: gap massimo tollerato tra due parametri per unirli nello stesso chunk
READ_MAX_CHUNK = 128  # byte: dimensione massima di un singolo chunk di lettura
READ_RETRIES   = 3    # Numero di tentativi in caso di fallimento


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                             APPLICAZIONE GUI                             ║
# ╚══════════════════════════════════════════════════════════════════════════╝
class BluetoothApp:
    """Classe principale per la gestione dell'applicazione Bluetooth."""


    def __init__(self, main_window):
        self.ble_manager = BLEManager()
        self.default_kmap_file_path    = None
        self.default_resmap_file_path  = None
        self.serial_finder_file_path   = None

        self.executor = ThreadPoolExecutor(max_workers=5)

        self._ble_loop        = None
        self._ble_loop_thread = None
        self._ble_loop_ready  = threading.Event()
        self._init_ble_loop()

        self.root = main_window
        self.root.title(f"Checker  {APP_VERSION}")

        self.is_scanning = False

        _saved = self._load_settings()
        self.log_autoscroll = tk.BooleanVar(value=_saved.get("log_autoscroll", True))
        self.log_debug      = tk.BooleanVar(value=_saved.get("log_debug", False))
        self.log_autoscroll.trace_add("write", lambda *_: self._save_settings())
        self.log_debug.trace_add("write", self._on_log_debug_changed)
        self._save_settings()

        self._setup_file_logging()
        setup_style()
        self.create_widgets()
        self._pulse_heartbeat()

    # ── Logging su file + UI ─────────────────────────────────────────────────

    def _load_settings(self) -> dict:
        cfg = _cp.ConfigParser()
        cfg.read(SETTINGS_FILE, encoding="utf-8")
        result = {}
        if "ui" in cfg:
            result["log_autoscroll"] = cfg["ui"].getboolean("log_autoscroll", fallback=True)
            result["log_debug"]      = cfg["ui"].getboolean("log_debug",      fallback=True)
        return result

    def _save_settings(self, *_):
        cfg = _cp.ConfigParser()
        cfg["ui"] = {
            "log_autoscroll": str(self.log_autoscroll.get()),
            "log_debug":      str(self.log_debug.get()),
        }
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            cfg.write(f)

    def _on_log_debug_changed(self, *_):
        level = logging.DEBUG if self.log_debug.get() else logging.INFO
        logging.getLogger().setLevel(level)
        self._save_settings()

    def _setup_file_logging(self):
        formatter    = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILENAME, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)

        initial_level = logging.DEBUG if self.log_debug.get() else logging.INFO
        root_logger   = logging.getLogger()
        root_logger.setLevel(initial_level)
        root_logger.addHandler(file_handler)

        for noisy in ("bleak", "winrt", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        logging.getLogger("shared_lib").setLevel(logging.INFO)

        self.log = logging.getLogger(__name__)
        self.log.info(
            f"Applicazione avviata — versione {APP_VERSION} — "
            f"debug={'on' if self.log_debug.get() else 'off'}"
        )
        self.log.info(f"Log: {LOG_FILENAME}")

    def _attach_ui_logging(self, text_widget):
        formatter  = logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
        tk_handler = TkTextHandler(text_widget, autoscroll_var=self.log_autoscroll)
        tk_handler.setFormatter(formatter)
        tk_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(tk_handler)

    # ── UI — struttura principale ────────────────────────────────────────────

    def _setup_menubar(self):
        menubar   = tk.Menu(self.root)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Tipi supportati…", command=self._show_tipi_supportati)
        help_menu.add_separator()
        help_menu.add_command(label=f"Versione  {APP_VERSION}", state="disabled")
        menubar.add_cascade(label="?", menu=help_menu)
        self.root.config(menu=menubar)

    def _show_tipi_supportati(self):
        """Apre una finestra di dialogo con la tabella dei tipi dati supportati."""
        win = tk.Toplevel(self.root)
        win.title("Tipi dati supportati")
        win.resizable(False, False)
        win.grab_set()

        tk.Label(win,
                 text="Tipi dati utilizzabili nella colonna 'Tipo' del CSV",
                 font=("Helvetica", 10, "bold"),
                 pady=10).grid(row=0, column=0, columnspan=5, padx=16, sticky="w")

        headers    = ("Tipo", "Byte", "Esempio valore", "Descrizione")
        col_widths = (12, 6, 28, 46)
        for c, (h, w) in enumerate(zip(headers, col_widths)):
            tk.Label(win, text=h, font=("Helvetica", 9, "bold"),
                     width=w, anchor="w", bg="#dde", relief="flat",
                     padx=6, pady=4
                     ).grid(row=1, column=c,
                            padx=(16 if c == 0 else 1, 1 if c < 3 else 16),
                            pady=(0, 2), sticky="ew")

        for r, (tipo, byte_n, esempio, descr) in enumerate(TIPI_SUPPORTATI):
            bg = "#f7f7ff" if r % 2 == 0 else "#ffffff"
            for c, (val, w) in enumerate(zip((tipo, byte_n, esempio, descr), col_widths)):
                tk.Label(win, text=val,
                         font=("Courier New" if c == 0 else "Helvetica", 9),
                         width=w, anchor="w", bg=bg, padx=6, pady=3
                         ).grid(row=r + 2, column=c,
                                padx=(16 if c == 0 else 1, 1 if c < 3 else 16),
                                sticky="ew")

        note = (
            "• I tipi sono case-insensitive nel CSV (es. uint8 = UINT8).\n"
            "• STRING senza numero usa dimensione 20 come default.\n"
            "• FLOAT32 accetta sia punto che virgola come separatore decimale.\n"
            "• UINT8[N] in lettura usa '.' come separatore (es. 192.168.1.1);\n"
            "  in scrittura accetta anche un singolo valore scalare (es. 0 → tutti zero)."
        )
        tk.Label(win, text=note, font=("Helvetica", 8), fg="#555555",
                 justify="left", anchor="w", padx=16, pady=10
                 ).grid(row=len(TIPI_SUPPORTATI) + 2, column=0, columnspan=4, sticky="w")

        ttk.Button(win, text="Chiudi", command=win.destroy).grid(
            row=len(TIPI_SUPPORTATI) + 3, column=0, columnspan=4, pady=(0, 12))

    def create_widgets(self):
        self._setup_menubar()

        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill="both", expand=True)
        main_frame.rowconfigure(1, weight=1)
        main_frame.columnconfigure(0, weight=1)

        self.status_bar = TriStatusBar(main_frame)
        self.status_bar.grid(row=0, column=0, sticky="ew")

        paned_window = ttk.PanedWindow(main_frame, orient="horizontal")
        paned_window.grid(row=1, column=0, sticky="nsew")

        left_frame  = ttk.Frame(paned_window)
        right_frame = ttk.Frame(paned_window)
        paned_window.add(left_frame,  weight=1)
        paned_window.add(right_frame, weight=1)

        self.create_left_widgets(left_frame)
        self.create_right_widgets(right_frame)

        ttk.Label(main_frame, text="Log Attività",
                  font=("Helvetica", 9, "bold")).grid(
            row=2, column=0, sticky="w", padx=10, pady=(4, 0))

        log_frame = ttk.Frame(main_frame)
        log_frame.grid(row=3, column=0, sticky="ew", padx=10, pady=(2, 0))
        log_frame.columnconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=7, state="disabled",
                                wrap="none", relief="sunken", borderwidth=1)
        self.log_text.grid(row=0, column=0, sticky="ew")

        scrollbar_v = ttk.Scrollbar(log_frame, orient="vertical",   command=self.log_text.yview)
        scrollbar_h = ttk.Scrollbar(log_frame, orient="horizontal",  command=self.log_text.xview)
        scrollbar_v.grid(row=0, column=1, sticky="ns")
        scrollbar_h.grid(row=1, column=0, sticky="ew")
        self.log_text.configure(yscrollcommand=scrollbar_v.set,
                                xscrollcommand=scrollbar_h.set)

        log_footer = ttk.Frame(main_frame)
        log_footer.grid(row=4, column=0, sticky="w", padx=10, pady=(2, 4))
        ttk.Checkbutton(log_footer, text="Auto-scroll",
                        variable=self.log_autoscroll).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(log_footer, text="Debug",
                        variable=self.log_debug).pack(side="left")

        self._attach_ui_logging(self.log_text)

    # ── Pannello sinistro ────────────────────────────────────────────────────

    def create_left_widgets(self, frame):
        # ─ Dispositivi Bluetooth
        device_frame = ttk.LabelFrame(frame, text="Dispositivi Bluetooth")
        device_frame.pack(fill="x", padx=PAD_LG, pady=PAD_LG)

        self.device_list = tk.Listbox(device_frame, height=6, width=40)
        self.device_list.grid(row=0, column=0, padx=PAD_SM, pady=PAD_SM)

        device_controls = ttk.Frame(device_frame)
        device_controls.grid(row=0, column=1, padx=PAD, sticky="n")

        self.refresh_button = ttk.Button(device_controls, text="Scansiona",
                                         command=self.search_devices)
        self.refresh_button.grid(row=0, column=0, pady=PAD_SM, sticky="ew")

        self.connect_button = ttk.Button(device_controls, text="Connetti",
                                         command=self.connect_device)
        self.connect_button.grid(row=1, column=0, pady=PAD_SM, sticky="ew")

        self.disconnect_button = ttk.Button(device_controls, text="Disconnetti",
                                            command=self.disconnect_device, state="disabled")
        self.disconnect_button.grid(row=2, column=0, pady=PAD_SM, sticky="ew")

        # ─ Lettura EEPROM
        read_frame = ttk.LabelFrame(frame, text="Lettura EEPROM")
        read_frame.pack(fill="x", padx=PAD_LG, pady=(0, PAD))
        read_frame.columnconfigure(1, weight=1)

        ttk.Label(read_frame, text="Indirizzo:").grid(
            row=0, column=0, padx=PAD, pady=PAD_SM, sticky="w")
        self.read_address_entry = ttk.Entry(read_frame, width=12)
        self.read_address_entry.insert(0, "0x0016")
        self.read_address_entry.bind(
            "<FocusIn>",
            lambda e: self.read_address_entry.delete(0, "end")
            if self.read_address_entry.get() == "0x0016" else None
        )
        self.read_address_entry.grid(row=0, column=1, padx=PAD_SM, pady=PAD_SM, sticky="w")

        ttk.Label(read_frame, text="N. byte:").grid(
            row=1, column=0, padx=PAD, pady=PAD_SM, sticky="w")
        self.data_size_entry = ttk.Entry(read_frame, width=6)
        self.data_size_entry.grid(row=1, column=1, padx=PAD_SM, pady=PAD_SM, sticky="w")

        self.read_button = ttk.Button(read_frame, text="Leggi",
                                      command=self.on_read_button_pressed)
        self.read_button.grid(row=0, column=2, rowspan=2, padx=PAD_SM, pady=PAD_SM, sticky="ns")

        result_lbl_frame = ttk.Frame(read_frame)
        result_lbl_frame.grid(row=2, column=0, columnspan=3,
                               padx=PAD, pady=(PAD_SM, PAD), sticky="ew")
        result_lbl_frame.columnconfigure(0, weight=1)

        ttk.Label(result_lbl_frame, text="Risultato:").grid(row=0, column=0, sticky="w")
        self.copy_to_write_btn = ttk.Button(result_lbl_frame, text="→ Copia in Scrittura",
                                            command=self._copy_read_to_write, state="disabled")
        self.copy_to_write_btn.grid(row=0, column=1, sticky="e")

        read_tree_frame = ttk.Frame(read_frame)
        read_tree_frame.grid(row=3, column=0, columnspan=3,
                              padx=PAD_SM, pady=(0, PAD), sticky="ew")
        read_tree_frame.columnconfigure(0, weight=1)

        self.read_tree = ttk.Treeview(
            read_tree_frame,
            columns=("Offset", "HEX", "DEC", "ASCII"),
            show="headings", height=4
        )
        for col, w in (("Offset", 55), ("HEX", 55), ("DEC", 55), ("ASCII", 55)):
            self.read_tree.heading(col, text=col)
            self.read_tree.column(col, width=w, minwidth=45, anchor="center",
                                  stretch=(col == "ASCII"))
        self.read_tree.grid(row=0, column=0, sticky="ew")
        self.read_tree.tag_configure("oddrow",  background="lightgrey")
        self.read_tree.tag_configure("evenrow", background="white")

        read_tree_scroll = ttk.Scrollbar(read_tree_frame, orient="vertical",
                                         command=self.read_tree.yview)
        self.read_tree.configure(yscrollcommand=read_tree_scroll.set)
        read_tree_scroll.grid(row=0, column=1, sticky="ns")

        # ─ Scrittura EEPROM
        write_frame = ttk.LabelFrame(frame, text="Scrittura EEPROM")
        write_frame.pack(fill="x", padx=PAD_LG, pady=(0, PAD))
        write_frame.columnconfigure(1, weight=1)

        ttk.Label(write_frame, text="Indirizzo (es. 0x0016):").grid(
            row=0, column=0, padx=PAD, pady=PAD_SM, sticky="w")
        self.write_address_entry = ttk.Entry(write_frame, width=12)
        self.write_address_entry.grid(row=0, column=1, padx=PAD_SM, pady=PAD_SM, sticky="w")

        self.write_button = ttk.Button(write_frame, text="Scrivi",
                                       command=self.write_data_manually)
        self.write_button.grid(row=0, column=2, padx=PAD_SM, pady=PAD_SM, sticky="ew")

        ttk.Label(write_frame, text="Dati hex (es. 03 66 36):").grid(
            row=1, column=0, padx=PAD, pady=PAD_SM, sticky="w")
        data_entry_frame = ttk.Frame(write_frame)
        data_entry_frame.grid(row=1, column=1, columnspan=2,
                               padx=PAD_SM, pady=PAD_SM, sticky="ew")
        data_entry_frame.columnconfigure(0, weight=1)

        self.data_entry = tk.Entry(
            data_entry_frame, font=("Courier New", 10),
            relief="sunken", borderwidth=1,
            highlightthickness=2,
            highlightbackground="gray", highlightcolor="gray",
            insertbackground="black"
        )
        self.data_entry.grid(row=0, column=0, sticky="ew")
        self.data_entry.bind("<KeyRelease>", self._on_data_entry_key)

        ttk.Button(
            data_entry_frame, text="✕ Pulisci",
            command=lambda: (
                self.data_entry.delete(0, "end"),
                self._validate_hex_input(self.data_entry)
            )
        ).grid(row=0, column=1, padx=(PAD_SM, 0))

    # ── Pannello destro (Sostituisci la tua funzione esistente con questa) ──────────────────────

    def create_right_widgets(self, frame):
        # ─ Tabella parametri (rimane invariata)
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
        self.tree.heading("Da Scrivere", text="Da Scrivere  ✎")
        self.tree.heading("Letti", text="Letti")

        self.tree.column("Nome", width=200, minwidth=100, stretch=True)
        self.tree.column("Indirizzo", width=80, minwidth=60, stretch=False, anchor="center")
        self.tree.column("Tipo", width=75, minwidth=55, stretch=False, anchor="center")
        self.tree.column("Da Scrivere", width=110, minwidth=80, stretch=True, anchor="center")
        self.tree.column("Letti", width=100, minwidth=80, stretch=True, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")

        self.tree.tag_configure("oddrow", background="lightgrey")
        self.tree.tag_configure("evenrow", background="white")
        self.tree.tag_configure("mismatch", background="#FEF3C7")

        self._tipo_tooltip = TreeviewTypeTooltip(self.tree, tipo_col="Tipo")
        scrollbar = ttk.Scrollbar(param_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.load_config_parameters()
        self._setup_inline_edit()
        self._setup_context_menu()

        # ── NUOVA DISPOSIZIONE PULSANTI ──────────────────────────────────────

        # Container principale per i pulsanti
        controls_container = ttk.Frame(frame)
        controls_container.pack(fill="x", padx=PAD_LG, pady=(0, PAD_SM))

        # GRUPPO A: Gestione Configurazione (File CSV)
        # ---------------------------------------------------------
        file_group = ttk.LabelFrame(controls_container, text=" Configurazione (PC) ")
        file_group.pack(side="left", fill="y", padx=(0, PAD_SM))

        self.carica_btn = ttk.Button(file_group, text="📂 Carica...",
                                     command=self.load_new_config, width=15)
        self.carica_btn.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=5, pady=5)

        self.salva_config_btn = ttk.Button(file_group, text="💾 Salva Config",
                                           command=self.save_config_csv, width=15)
        self.salva_config_btn.grid(row=0, column=1, padx=5, pady=5)

        self.esporta_btn = ttk.Button(file_group, text="📋 Esporta Letti",
                                      command=self.save_as_csv, width=15)
        self.esporta_btn.grid(row=1, column=1, padx=5, pady=5)

        # GRUPPO B: Operazioni Dispositivo (BLE)
        # ---------------------------------------------------------
        dev_group = ttk.LabelFrame(controls_container, text=" Operazioni Dispositivo (BLE) ")
        dev_group.pack(side="left", fill="both", expand=True)
        dev_group.columnconfigure(2, weight=1)  # Spazio flessibile per separare lo "Scrivi"

        # Sottogruppo Lettura
        self.leggi_btn = ttk.Button(dev_group, text="📥 Leggi Parametri",
                                    command=self.scarica_parametri)
        self.leggi_btn.grid(row=0, column=0, padx=5, pady=6, sticky="ew")

        self.verifica_btn = ttk.Button(dev_group, text="🔎 Leggi e Verifica",
                                       command=self.lettura_e_verifica)
        self.verifica_btn.grid(row=1, column=0, padx=5, pady=(0, 6), sticky="ew")

        # Azione Critica: Scrivi (messa a destra e più visibile)
        self.scrivi_btn = ttk.Button(dev_group, text="⚠ SCRIVI PARAMETRI",
                                     command=self.scrivi_parametri, style="Accent.TButton")
        self.scrivi_btn.grid(row=0, column=1, rowspan=2, padx=15, pady=6, sticky="nsew")

    # ── Editing inline "Da Scrivere" ─────────────────────────────────────────

    def _setup_inline_edit(self):
        """Doppio-click su 'Da Scrivere' apre un Entry inline sovrapposto alla cella."""
        self._edit_entry = None
        self._edit_row   = None
        self.tree.bind("<Double-1>", self._on_tree_double_click)

    def _on_tree_double_click(self, event):
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if not row:
            return
        try:
            col_name = self.tree.column(col, "id")
        except Exception:
            return
        if col_name != "Da Scrivere":
            return
        self._open_cell_editor(row, col)

    def _open_cell_editor(self, row, col):
        # Chiudi eventuale editor già aperto
        if self._edit_entry:
            self._edit_entry.destroy()
            self._edit_entry = None

        bbox = self.tree.bbox(row, col)
        if not bbox:          # riga non visibile (fuori scroll)
            return
        x, y, w, h = bbox

        current_val = self.tree.set(row, "Da Scrivere")

        entry = tk.Entry(self.tree, font=("Helvetica", 9))
        entry.insert(0, current_val)
        entry.select_range(0, "end")
        entry.place(x=x, y=y, width=w, height=h)
        entry.focus_set()

        self._edit_entry = entry
        self._edit_row   = row

        entry.bind("<Return>",   self._commit_edit)
        entry.bind("<Escape>",   lambda _e: self._cancel_edit())
        entry.bind("<FocusOut>", self._commit_edit)

    def _commit_edit(self, event=None):
        if not self._edit_entry:
            return
        new_val = self._edit_entry.get()
        self.tree.set(self._edit_row, "Da Scrivere", new_val)
        self._edit_entry.destroy()
        self._edit_entry = None
        self._mark_dirty()

    def _cancel_edit(self):
        if self._edit_entry:
            self._edit_entry.destroy()
            self._edit_entry = None

    # ── Menu contestuale tasto-destro ────────────────────────────────────────

    def _setup_context_menu(self):
        """Tasto destro su una riga: copia il valore letto in 'Da Scrivere'."""
        self._ctx_menu = tk.Menu(self.root, tearoff=0)
        self._ctx_menu.add_command(
            label="↓ Copia valore letto in 'Da Scrivere'",
            command=self._copy_letto_to_da_scrivere
        )
        self.tree.bind("<Button-3>", self._show_context_menu)

    def _show_context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self._ctx_menu.post(event.x_root, event.y_root)

    def _copy_letto_to_da_scrivere(self):
        for item in self.tree.selection():
            letto = self.tree.set(item, "Letti")
            if letto and letto != "0":
                self.tree.set(item, "Da Scrivere", letto)
                self._mark_dirty()

    # ── Indicatore modifiche non salvate ────────────────────────────────────

    def _mark_dirty(self):
        """Aggiunge '●' al titolo finestra per segnalare modifiche non salvate."""
        if "●" not in self.root.title():
            self.root.title(self.root.title() + "  ●")

    def _clear_dirty(self):
        """Rimuove l'indicatore di modifiche non salvate dal titolo."""
        self.root.title(self.root.title().replace("  ●", ""))

    # ── Funzioni di utilità UI ───────────────────────────────────────────────

    def _validate_hex_input(self, entry_widget, event=None):
        """Valida in tempo reale il campo hex: bordo verde/rosso."""
        val = entry_widget.get().replace(" ", "")
        if val == "":
            entry_widget.config(highlightthickness=0)
            return
        try:
            bytearray.fromhex(val)
            entry_widget.config(highlightbackground="green",
                                highlightcolor="green", highlightthickness=2)
        except ValueError:
            entry_widget.config(highlightbackground="red",
                                highlightcolor="red", highlightthickness=2)

    def _on_data_entry_key(self, event=None):
        """Auto-spacing hex nel campo dati scrittura: inserisce spazio ogni 2 char."""
        if event and event.keysym in (
            "BackSpace", "Delete", "Left", "Right", "Home", "End",
            "Tab", "Return", "Escape", "Control_L", "Control_R",
            "Shift_L", "Shift_R", "Alt_L", "Alt_R"
        ):
            self._validate_hex_input(self.data_entry)
            return
        widget = self.data_entry
        raw    = widget.get().replace(" ", "").upper()
        spaced = " ".join(raw[i:i+2] for i in range(0, len(raw), 2))
        if widget.get() != spaced:
            widget.delete(0, "end")
            widget.insert(0, spaced)
            widget.icursor("end")
        self._validate_hex_input(self.data_entry)

    def _update_read_tree(self, raw_bytes: bytes):
        """Popola il Treeview di risultato lettura con una riga per byte."""
        for row in self.read_tree.get_children():
            self.read_tree.delete(row)
        for i, b in enumerate(raw_bytes):
            ascii_ch = chr(b) if 32 <= b < 127 else "·"
            tag = "oddrow" if i % 2 == 0 else "evenrow"
            self.read_tree.insert("", "end",
                                  values=(f"{i}", f"{b:02X}", f"{b}", ascii_ch),
                                  tags=(tag,))
        self.copy_to_write_btn.config(state="normal")
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
        for item in self.tree.get_children():
            self.tree.delete(item)
        try:
            with open(config_file, 'r', newline='') as f:
                reader = csv.reader(f, delimiter=';')
                next(reader)
                for idx, row in enumerate(reader):
                    if len(row) < 3:
                        continue
                    name      = row[0].strip()
                    address   = row[1].strip()
                    data_type = row[2].strip()
                    to_write  = row[3].strip() if len(row) > 3 else ""
                    if to_write and data_type.upper() == 'FLOAT32':
                        try:
                            parsed_val = float(to_write.replace(',', '.'))
                            to_write = (f"{parsed_val:.10f}"
                                        .replace('.', ',').rstrip('0').rstrip(','))
                        except ValueError:
                            self.log.error(
                                f"Valore non valido in 'Da Scrivere' per {name}: {to_write}")
                    tag = 'oddrow' if idx % 2 == 0 else 'evenrow'
                    self.tree.insert("", "end",
                                     values=(name, address, data_type, to_write, "0"),
                                     tags=(tag,))
        except FileNotFoundError:
            self.log.warning(
                f"File '{config_file}' non trovato. Carico valori di default.")
            for i in range(100):
                tag = 'oddrow' if i % 2 == 0 else 'evenrow'
                self.tree.insert("", "end",
                                 values=(f"Parametro {i+1}", "0x0000", "uint8", "", "0"),
                                 tags=(tag,))
        except csv.Error:
            self.log.error(f"Errore nel parsing del file '{config_file}'.")

    def load_new_config(self):
        file_path = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv")])
        if file_path:
            self.load_config_parameters(file_path)
            self._clear_dirty()
            self.log.info(f"Caricato nuovo file di configurazione: {file_path}")

    # ── Pipeline Lettura Parametri (ottimizzata per chunk) ───────────────────

    def scarica_parametri(self):
        if not self.ble_manager.get_connection_status():
            messagebox.showerror("Errore", "Connetti un dispositivo prima di scaricare i parametri.")
            return
        self.log.info("Inizio scaricamento parametri...")
        self.status_bar.set_activity("📥 Lettura parametri in corso…", "info")
        self.status_bar.progress_mode('determinate')
        self.status_bar.progress_set(0)
        asyncio.run_coroutine_threadsafe(self._scarica_parametri_async(), self._ble_loop)

    def _build_read_chunks(self, params):
        """
        Raggruppa i parametri in chunk di lettura contigui o quasi-contigui.
        Ogni parametro: (item_id, name, address_str, data_type).
        """
        valid = []
        for item_id, name, address_str, data_type in params:
            try:
                address = int(address_str, 16)
                size    = self.get_size_from_type(data_type)
                valid.append((item_id, name, address, size, data_type))
            except (ValueError, KeyError):
                self.log.error(f"Parametro '{name}' scartato: indirizzo o tipo non valido.")
                self.root.after(0, lambda it=item_id: (
                    self.tree.set(it, column="Letti", value="⛔ TIPO ERRATO"),
                    self.tree.item(it, tags=["mismatch"])
                ))
        valid.sort(key=lambda p: p[2])

        chunks  = []
        current = None
        for param in valid:
            item_id, name, address, size, data_type = param
            if current is None:
                current = {'start': address, 'size': size, 'params': [param]}
            else:
                chunk_end = current['start'] + current['size']
                gap       = address - chunk_end
                new_size  = (address + size) - current['start']
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
        """Legge tutti i parametri e aggiorna il Treeview. Ritorna {item_id: valore}."""
        children = list(self.tree.get_children())
        if not children:
            return {}
        letti_map = {}

        for item in children:
            tags = [t for t in self.tree.item(item, "tags") if t != "mismatch"]
            self.tree.item(item, tags=tags)

        raw_params = [
            (item,
             self.tree.item(item)['values'][0],
             self.tree.item(item)['values'][1],
             self.tree.item(item)['values'][2])
            for item in children
        ]
        chunks       = self._build_read_chunks(raw_params)
        total_params = sum(len(c['params']) for c in chunks)
        done         = 0

        self.log.info(
            f"Lettura ottimizzata: {total_params} parametri in {len(chunks)} chunk.")
        for chunk in chunks:
            chunk_start = chunk['start']
            chunk_size  = chunk['size']
            self.log.debug(
                f"Chunk 0x{chunk_start:04X} | {chunk_size} byte | "
                f"{len(chunk['params'])} parametri")
            self.root.after(0, lambda s=chunk_start, z=chunk_size:
                            self.status_bar.set_activity(
                                f"📥 Lettura chunk 0x{s:04X} ({z} byte)…", "info"))

            chunk_data = await self.read_data(chunk_start, chunk_size)
            for item_id, name, address, size, data_type in chunk['params']:
                if chunk_data is not None:
                    offset      = address - chunk_start
                    param_bytes = chunk_data[offset: offset + size]
                    if len(param_bytes) == size:
                        value = self.interpret_data(param_bytes, data_type)
                    else:
                        self.log.error(
                            f"'{name}': byte estratti ({len(param_bytes)}) != attesi ({size}).")
                        value = "N/A"
                else:
                    self.log.error(f"'{name}': lettura chunk fallita.")
                    value = "N/A"
                letti_map[item_id] = value
                self.root.after(0, lambda it=item_id, v=value:
                                self.tree.set(it, column="Letti", value=v))
                done     += 1
                progress  = (done / total_params) * 100
                self.root.after(0, lambda p=progress: self.update_progress_bar(p))

        self.log.info(f"── Lettura parametri: {total_params} letti ──")
        for item in self.tree.get_children():
            v = self.tree.item(item)['values']
            self.log.info(
                f"  {v[0]:<30s} {v[1]}  {v[2]:<8s}  {v[4]}")

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
                    "I seguenti parametri hanno un tipo non riconosciuto nel CSV "
                    "e non sono stati letti:\n\n"
                    + "\n".join(f"  • {n}" for n in names)
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
            messagebox.showerror("Errore",
                                 "Connetti un dispositivo prima di scrivere i parametri.")
            return

        n_params = sum(
            1 for item in self.tree.get_children()
            if str(self.tree.item(item)['values'][3]) != ""
        )
        if n_params == 0:
            messagebox.showinfo("Scrivi Parametri", "Nessun parametro da scrivere.")
            return

        if not messagebox.askokcancel(
            "Conferma Scrittura",
            f"Stai per scrivere {n_params} parametro/i sul dispositivo.\n\n"
            "Questa operazione sovrascrive i valori attuali.\nVuoi procedere?"
        ):
            return

        self.log.info(f"── Scrittura parametri: {n_params} da scrivere ──")
        self.status_bar.set_activity("📤 Scrittura parametri in corso…", "info")
        self.status_bar.progress_mode('determinate')
        self.status_bar.progress_set(0)
        asyncio.run_coroutine_threadsafe(self._scrivi_parametri_async(), self._ble_loop)

    async def _scrivi_parametri_async(self):
        children = list(self.tree.get_children())
        total    = len(children)
        if total == 0:
            return

        n_params = sum(
            1 for item in children
            if str(self.tree.item(item)['values'][3]) != ""
        )
        errors = []
        for idx, item in enumerate(children):
            values = self.tree.item(item)['values']
            name, address_str, data_type, to_write, _ = values
            if str(to_write) == "":
                self.log.debug(f"Salto '{name}': nessun valore da scrivere.")
                continue
            try:
                address = int(address_str, 16)
                data    = self.prepare_data_for_write(to_write, data_type)
                if data is None:
                    self.log.error(f"Impossibile preparare i dati per '{name}'")
                    errors.append(name)
                    continue
                self.root.after(0, lambda n=name, a=address:
                                self.status_bar.set_activity(
                                    f"📤 Scrittura '{n}' @ 0x{a:04X}…", "info"))
                success = await self.write_data(address, data)
                if success:
                    hex_suffix = f"  [{data.hex(' ')}]" if self.log_debug.get() else ""
                    self.log.info(
                        f"  SCRITTO  {name:<30s} {address_str}  "
                        f"{data_type:<8s}  {to_write}{hex_suffix}")
                else:
                    self.log.error(
                        f"  ERRORE   {name:<30s} {address_str}  valore={to_write}")
                    errors.append(name)
            except ValueError:
                self.log.error(f"Indirizzo o tipo non valido per '{name}'")
                errors.append(name)
                continue
            progress = ((idx + 1) / total) * 100
            self.root.after(0, lambda p=progress: self.update_progress_bar(p))

        self.root.after(0, lambda: self.status_bar.progress_mode('indeterminate'))

        ok_count = n_params - len(errors)
        if errors:
            self.log.error(
                f"── Scrittura completata: OK={ok_count}  KO={len(errors)} ──")
            self.log.error(f"   Falliti: {', '.join(errors)}")
            self.root.after(0, lambda: (
                self.status_bar.set_activity(
                    "⛔ Scrittura parametri con errori.", "error"),
                messagebox.showerror(
                    "Scrittura Parametri",
                    f"Errori nella scrittura di: {', '.join(errors)}")
            ))
        else:
            self.log.info(f"── Scrittura completata: OK={ok_count}  KO=0 ──")
            self.root.after(0, lambda: (
                self.status_bar.set_activity(
                    "✓ Scrittura parametri completata.", "success"),
                messagebox.showinfo(
                    "Scrittura Parametri", "Tutti i parametri scritti con successo.")
            ))

    # ── Lettura + Verifica ───────────────────────────────────────────────────

    def lettura_e_verifica(self):
        if not self.ble_manager.get_connection_status():
            messagebox.showerror("Errore",
                                 "Connetti un dispositivo prima di verificare i parametri.")
            return
        self.log.info("── Lettura e verifica parametri ──")
        self.status_bar.set_activity("🔎 Lettura+Verifica in corso…", "info")
        self.status_bar.progress_mode('determinate')
        self.status_bar.progress_set(0)
        asyncio.run_coroutine_threadsafe(self._lettura_e_verifica_async(), self._ble_loop)

    async def _lettura_e_verifica_async(self):
        letti_map = await self._scarica_parametri_async()
        children  = list(self.tree.get_children())
        errors    = []
        for item in children:
            values = self.tree.item(item)['values']
            name, _, data_type, to_write, _ = values
            if str(to_write) == "":
                continue
            letto          = letti_map.get(item, "")
            to_write_norm  = str(to_write).replace(',', '.')
            letti_norm     = str(letto).replace(',', '.')
            if to_write_norm != letti_norm:
                errors.append(name)
                tags = [t for t in self.tree.item(item, "tags")
                        if t not in ("oddrow", "evenrow", "mismatch")]
                self.tree.item(item, tags=tags + ["mismatch"])

        verified = sum(
            1 for item in self.tree.get_children()
            if str(self.tree.item(item)['values'][3]) != ""
        )
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
                    self.log.error(
                        f"  KO  {name:<30s} {addr_str}  "
                        f"atteso={to_write}  letto={letto}")

        if errors:
            self.root.after(0, lambda: (
                self.status_bar.set_activity(
                    "⚠ Verifica: non conformità riscontrate.", "warn"),
                messagebox.showerror("Verifica",
                                     f"{len(errors)} parametro/i non conforme/i")
            ))
        else:
            self.root.after(0, lambda: (
                self.status_bar.set_activity(
                    "✓ Verifica completata con successo.", "success"),
                messagebox.showinfo("Verifica",
                                    "Tutti i parametri verificati con successo.")
            ))

        self.root.after(0, lambda: self.status_bar.progress_mode('indeterminate'))

    # ── Salvataggio CSV ──────────────────────────────────────────────────────

    def save_config_csv(self):
        """
        Salva Nome / Indirizzo / Tipo / Da Scrivere.
        Stesso formato del CSV di input → ricaricabile con 'Carica Parametri'.
        """
        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            title="Salva configurazione (ricaricabile)"
        )
        if not file_path:
            return
        with open(file_path, 'w', newline='') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(["Nome Parametro", "Indirizzo (hex)", "Tipo Dato", "Da Scrivere"])
            for item in self.tree.get_children():
                v = self.tree.item(item)['values']
                writer.writerow([v[0], v[1], v[2], v[3]])
        self._clear_dirty()
        self.log.info(f"Config salvata: {file_path}")

    def save_as_csv(self):
        """
        Esporta Nome / Indirizzo / Tipo / Valori Letti.
        Snapshot di lettura — non ricaricabile come config.
        """
        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            title="Esporta valori letti"
        )
        if file_path:
            with open(file_path, 'w', newline='') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(["Nome Parametro", "Indirizzo (hex)", "Tipo Dato", "Valori Letti"])
                for item in self.tree.get_children():
                    values = self.tree.item(item)['values']
                    writer.writerow([values[0], values[1], values[2], values[4]])
            self.log.info(f"Valori letti esportati: {file_path}")

    # ── BLE Loop asyncio ─────────────────────────────────────────────────────

    def _init_ble_loop(self):
        def _worker():
            self._ble_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._ble_loop)
            self._ble_loop_ready.set()
            try:
                self._ble_loop.run_forever()
            finally:
                try:
                    if hasattr(self._ble_loop, "shutdown_asyncgens"):
                        self._ble_loop.run_until_complete(
                            self._ble_loop.shutdown_asyncgens())
                except Exception:
                    pass
                self._ble_loop.close()

        self._ble_loop_thread = threading.Thread(
            target=_worker, name="BLE-Asyncio-Loop", daemon=True)
        self._ble_loop_thread.start()
        self._ble_loop_ready.wait()

    def _shutdown_ble_loop(self, join_timeout=3.0):
        if self._ble_loop is not None:
            try:
                self._ble_loop.call_soon_threadsafe(self._ble_loop.stop)
            except Exception:
                pass
        if self._ble_loop_thread is not None:
            self._ble_loop_thread.join(timeout=join_timeout)

    # ── Scansione / Connessione / Disconnessione ─────────────────────────────

    def search_devices(self):
        self.log.info("Avvio scansione dispositivi BLE...")
        self.status_bar.set_activity("📡 Scansione dispositivi…", "info")
        self.status_bar.progress_mode('indeterminate')
        self.status_bar.progress_start()
        self.executor.submit(self._search_devices)

    def _search_devices(self):
        fut = asyncio.run_coroutine_threadsafe(
            self.ble_manager.scan_devices(timeout=5), self._ble_loop)
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
        self.status_bar.set_activity(
            f"✓ Scansione completata: {count} dispositivo/i trovati.", "success")

    def connect_device(self):
        selected_device = self.device_list.get(tk.ACTIVE)
        if not selected_device:
            return
        try:
            parts    = selected_device.split(" - ")
            dev_name = parts[0].strip()
            address  = parts[1].strip()
        except Exception:
            self.log.error(
                "Formato voce lista dispositivi inatteso: "
                "impossibile estrarre l'indirizzo MAC.")
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
        display = f"{dev_name} [{addr}]" if dev_name else addr
        self.log.info(f"═══ CONNESSO a {display} ═══")
        self.status_bar.set_ble("ok")
        self.status_bar.set_device_info(dev_name, addr)
        self.status_bar.set_activity(f"✓ Connesso a {addr}", "success")
        self.connect_button.config(state="disabled")
        self.disconnect_button.config(state="normal")
        self.monitor_connection()

    def disconnect_device(self):
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
        self.log.info("═══ DISCONNESSO ═══")
        self.status_bar.set_ble("off")
        self.status_bar.set_device_info(None, None)
        self.status_bar.set_activity("✓ Disconnesso.", "success")
        self.status_bar.progress_stop()
        self.connect_button.config(state="normal")
        self.disconnect_button.config(state="disabled")
        messagebox.showinfo("Disconnessione", "Dispositivo disconnesso correttamente.")

    def on_disconnect_error(self, error):
        self.log.error(f"Errore durante la disconnessione: {error}")
        self.status_bar.set_ble("warn")
        self.status_bar.set_activity("⛔ Errore disconnessione.", "error")
        self.status_bar.progress_stop()
        messagebox.showerror("Errore", f"Errore durante la disconnessione: {error}")

    def monitor_connection(self):
        def check_connection():
            try:
                if not self.ble_manager.get_connection_status():
                    self.handle_disconnection()
                else:
                    self.root.after(5000, check_connection)
            except Exception as e:
                self.log.error(
                    f"Errore durante il monitoraggio della connessione: {e}")
                self.root.after(5000, check_connection)
        self.root.after(5000, check_connection)

    def handle_disconnection(self):
        self.ble_manager.reset_connection_state()
        self.status_bar.set_ble("err")
        self.status_bar.set_device_info(None, None)
        self.status_bar.set_activity(
            "⚠ Connessione BLE persa inaspettatamente.", "warn")
        self.connect_button.config(state="normal")
        self.disconnect_button.config(state="disabled")

    # ── Lettura / Scrittura di basso livello ─────────────────────────────────

    async def read_data(self, address, size, timeout=5):
        """Legge i dati BLE con segmentazione e retry."""
        full_data  = bytearray()
        bytes_read = 0
        while bytes_read < size:
            chunk_to_read = min(size - bytes_read, READ_MAX_CHUNK)
            current_addr  = address + bytes_read
            chunk_success = False

            self.root.after(0, lambda a=current_addr, s=chunk_to_read:
                            self.status_bar.set_activity(
                                f"📥 Leggo 0x{a:04X} ({s} B)…", "info"))

            for attempt in range(READ_RETRIES):
                try:
                    self.log.debug(
                        f"Lettura 0x{current_addr:04X} ({chunk_to_read}b) "
                        f"- Tentativo {attempt + 1}/{READ_RETRIES}")
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
                            self.log.warning(
                                f"Payload corto a 0x{current_addr:04X}: "
                                f"ricevuti {len(payload)}b, attesi {chunk_to_read}b")
                            self.root.after(0, lambda a=current_addr, r=len(payload),
                                            e=chunk_to_read:
                                            self.status_bar.set_activity(
                                                f"⚠ Payload corto @0x{a:04X}: {r}/{e} B",
                                                "warn"))
                    else:
                        self.log.warning(
                            f"Risposta non valida o nulla a 0x{current_addr:04X}")
                        self.root.after(0, lambda a=current_addr:
                                        self.status_bar.set_activity(
                                            f"⚠ Risposta nulla @0x{a:04X}", "warn"))
                except asyncio.TimeoutError:
                    self.log.warning(
                        f"Timeout al tentativo {attempt + 1}/{READ_RETRIES} "
                        f"per 0x{current_addr:04X}")
                    self.root.after(0, lambda a=current_addr, t=attempt + 1:
                                    self.status_bar.set_activity(
                                        f"⚠ Timeout @{t}/{READ_RETRIES} @0x{a:04X}", "warn"))
                except BleakError as e:
                    self.log.error(
                        f"Errore BLE fatale a 0x{current_addr:04X}: {e} — abortita")
                    self.root.after(0, lambda:
                                    self.status_bar.set_activity(
                                        "⛔ Errore BLE: lettura abortita.", "error"))
                    return None
                except Exception as e:
                    self.log.error(
                        f"Errore imprevisto a 0x{current_addr:04X}: {e} — abortita")
                    self.root.after(0, lambda:
                                    self.status_bar.set_activity(
                                        "⛔ Errore imprevisto: lettura abortita.", "error"))
                    return None

                await asyncio.sleep(0.2)

            if not chunk_success:
                self.log.error(
                    f"Fallimento definitivo dopo {READ_RETRIES} tentativi "
                    f"a 0x{current_addr:04X}")
                self.root.after(0, lambda a=current_addr:
                                self.status_bar.set_activity(
                                    f"⛔ Fallimento definitivo @0x{a:04X}", "error"))
                return None

            bytes_read += chunk_to_read

        return full_data

    async def read_data_manually(self):
        try:
            address = int(self.read_address_entry.get(), 16)
            size    = int(self.data_size_entry.get())
        except ValueError:
            messagebox.showerror("Errore", "Indirizzo o dimensione non valida.")
            return
        self.status_bar.set_activity(
            f"📥 Lettura manuale @0x{address:04X} ({size} B)…", "info")
        self.status_bar.progress_mode('indeterminate')
        self.status_bar.progress_start()
        output = await self.read_data(address, size)
        if output:
            self.root.after(0, self._update_read_output, bytes(output))
            self.root.after(0, lambda:
                            self.status_bar.set_activity("✓ Lettura completata.", "success"))
        else:
            self.root.after(0, lambda:
                            self.status_bar.set_activity("⛔ Errore lettura.", "error"))
        self.root.after(0, self.status_bar.progress_stop)

    def on_read_button_pressed(self):
        asyncio.run_coroutine_threadsafe(self.read_data_manually(), self._ble_loop)

    def _update_read_output(self, raw_bytes: bytes):
        self._update_read_tree(raw_bytes)

    def write_data_manually(self):
        try:
            address = int(self.write_address_entry.get(), 16)
            data    = bytearray.fromhex(self.data_entry.get())
        except ValueError:
            self.log.error(
                "Input manuale non valido: indirizzo o dati in formato errato.")
            messagebox.showerror("Errore", "Indirizzo o dati in formato non valido.")
            return
        self.status_bar.set_activity(
            f"📤 Scrittura manuale @0x{address:04X}…", "info")
        self.status_bar.progress_mode('indeterminate')
        self.status_bar.progress_start()

        async def _do_write():
            ok = await self.write_data(address, data)
            if ok:
                self.root.after(0, lambda:
                                self.status_bar.set_activity(
                                    "✓ Scrittura completata.", "success"))
            else:
                self.root.after(0, lambda:
                                self.status_bar.set_activity(
                                    "⛔ Scrittura fallita.", "error"))
            self.root.after(0, self.status_bar.progress_stop)

        asyncio.run_coroutine_threadsafe(_do_write(), self._ble_loop)
        self.log.info(
            f"Scrittura manuale avviata: indirizzo={hex(address)}, "
            f"dati={self.data_entry.get()}")

    async def write_data(self, starting_address, data):
        if not self.ble_manager.get_connection_status():
            self.log.error("Scrittura fallita: nessun dispositivo connesso.")
            self.root.after(0, lambda: messagebox.showerror(
                "Errore", "Nessun dispositivo connesso!"))
            return False
        try:
            result = await self.ble_manager.write_eeprom(starting_address, data)
            if not result:
                self.log.error(
                    f"Scrittura a {hex(starting_address)}: "
                    "dispositivo non ha confermato.")
                self.root.after(0, lambda: messagebox.showerror(
                    "Errore",
                    "Scrittura fallita: dispositivo non ha confermato l'operazione."))
                return False
            self.log.debug(
                f"write_eeprom {hex(starting_address)}: confermato dal dispositivo.")
            return True
        except Exception as e:
            self.log.error(
                f"Errore durante la scrittura all'indirizzo "
                f"{hex(starting_address)}: {e}")
            self.root.after(0, lambda err=str(e): messagebox.showerror(
                "Errore", f"Errore durante la scrittura: {err}"))
            return False

    # ── Progress wrapper ─────────────────────────────────────────────────────

    def update_progress_bar(self, value):
        if 0 <= value <= 100:
            self.status_bar.progress_set(value)
            self.root.update_idletasks()
        else:
            self.log.error(
                f"Valore non valido per la barra di progresso: "
                f"{value} (atteso 0-100)")

    # ── Helpers formati / interpretazione ────────────────────────────────────

    def prepare_data_for_write(self, value_str, data_type):
        try:
            data_type = data_type.upper()
            value_str = str(value_str).strip()
            if data_type.endswith('H'):
                clean_hex = value_str.replace(" ", "")
                if len(clean_hex) % 2 != 0:
                    clean_hex = "0" + clean_hex
                return bytearray.fromhex(clean_hex)
            if data_type.startswith('STRING'):
                size    = self.get_size_from_type(data_type)
                encoded = value_str.encode('utf-8')
                return encoded[:size].ljust(size, b'\x00')
            if data_type == 'FLOAT32':
                val = float(value_str.replace(',', '.'))
                return struct.pack('<f', val)
            if re.match(r'^UINT8\[\d+\]$', data_type):
                n = int(re.search(r'\d+', data_type[5:]).group())
                if '.' in value_str or ',' in value_str:
                    sep   = ',' if ',' in value_str else '.'
                    parts = [int(x.strip()) for x in value_str.split(sep)]
                    if len(parts) != n:
                        raise ValueError(
                            f"UINT8[{n}]: attesi {n} valori, "
                            f"ricevuti {len(parts)} ('{value_str}')")
                else:
                    scalar = int(value_str)
                    parts  = [scalar] * n
                if any(v < 0 or v > 255 for v in parts):
                    raise ValueError(f"UINT8[{n}]: tutti i valori devono essere 0-255")
                return bytearray(parts)
            if data_type in ('UINT8', 'UINT16', 'UINT24', 'UINT32'):
                val = int(value_str)
                if data_type == 'UINT8':   return struct.pack('<B', val)
                if data_type == 'UINT16':  return struct.pack('<H', val)
                if data_type == 'UINT24':  return struct.pack('<I', val)[:3]
                if data_type == 'UINT32':  return struct.pack('<I', val)
            raise ValueError(
                f"Tipo dati non riconosciuto: '{data_type}'. "
                f"Tipi validi: UINT8, UINT16, UINT24, UINT32, FLOAT32, "
                f"STRING<N>, UINT8[N], <N>H")
        except Exception as e:
            self.log.error(f"Errore preparazione dati per scrittura: {e}")
            return None

    def get_size_from_type(self, data_type):
        data_type = data_type.upper()
        if data_type.endswith('H'):
            data_type = data_type[:-1]
        sizes = {'UINT8': 1, 'UINT16': 2, 'UINT24': 3, 'UINT32': 4, 'FLOAT32': 4}
        if data_type.startswith('STRING'):
            match = re.search(r'\d+', data_type)
            return int(match.group()) if match else 20
        if re.match(r'^UINT8\[\d+\]$', data_type):
            return int(re.search(r'\d+', data_type[5:]).group())
        if data_type in sizes:
            return sizes[data_type]
        raise ValueError(
            f"Tipo dati non riconosciuto: '{data_type}'. "
            f"Tipi validi: UINT8, UINT16, UINT24, UINT32, FLOAT32, "
            f"STRING<N>, UINT8[N], <N>H")

    def interpret_data(self, data, data_type):
        if not data:
            return "N/A"
        data_type = data_type.upper()
        if data_type.endswith('H'):
            return ' '.join(f'{b:02x}' for b in data)
        if data_type.startswith('STRING'):
            try:
                return data.decode('utf-8', errors='ignore').split('\x00')[0].strip()
            except Exception:
                return "Errore Decodifica"
        if data_type == 'FLOAT32':
            val       = struct.unpack_from('<f', data)[0]
            formatted = f"{val:.10f}".replace('.', ',').rstrip('0').rstrip(',')
            return formatted if formatted else "0"
        if re.match(r'^UINT8\[\d+\]$', data_type):
            return '.'.join(str(b) for b in data)
        if data_type in ('UINT8', 'UINT16', 'UINT24', 'UINT32'):
            val = 0
            for i, byte in enumerate(data):
                val |= (byte << (8 * i))
            return val
        raise ValueError(
            f"Tipo dati non riconosciuto in interpret_data: '{data_type}'. "
            f"Tipi validi: UINT8, UINT16, UINT24, UINT32, FLOAT32, "
            f"STRING<N>, UINT8[N], <N>H")

    # ── Heartbeat UI e chiusura ──────────────────────────────────────────────

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
    app  = BluetoothApp(root)
    # root.iconbitmap('logo2.ico')
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()