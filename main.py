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

LOG_FILENAME = "app.log"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Parametri per la lettura a blocchi
READ_MAX_GAP       = 8    # byte: gap massimo tollerato tra due parametri per unirli nello stesso chunk
READ_MAX_CHUNK     = 128  # byte: dimensione massima di un singolo chunk di lettura
READ_RETRIES       = 3    # Numero di tentativi in caso di fallimento

# ── Spaziatura uniforme ───────────────────────────────────────────────────────
PAD    = 8
PAD_SM = 4
PAD_LG = 12


class TkTextHandler(logging.Handler):
    """
    Handler di logging che scrive i messaggi nel widget Text di Tkinter.
    Thread-safe: usa root.after() per aggiornare la GUI dal thread corretto.
    autoscroll_var: BooleanVar opzionale; se False non chiama see("end").
    """
    LEVEL_TAGS = {
        logging.DEBUG:    "debug",
        logging.INFO:     "info",
        logging.WARNING:  "warning",
        logging.ERROR:    "error",
        logging.CRITICAL: "critical",
    }

    def __init__(self, text_widget, autoscroll_var=None):
        super().__init__()
        self.text_widget = text_widget
        self.autoscroll_var = autoscroll_var
        # Configura i tag colore nel widget
        text_widget.tag_configure("debug",    foreground="gray")
        text_widget.tag_configure("info",     foreground="black")
        text_widget.tag_configure("warning",  foreground="darkorange")
        text_widget.tag_configure("error",    foreground="red")
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
    """
    Configura font e padding mantenendo il tema di sistema.
    """
    style = ttk.Style()
    # Font e padding uniformi
    style.configure("TButton",   font=("Helvetica", 10), padding=(8, 4))
    style.configure("TLabel",    font=("Helvetica", 10))
    style.configure("TEntry",    font=("Helvetica", 10), padding=3)
    style.configure("Status.TLabel", font=("Helvetica", 10, "bold"))
    # Treeview: font leggibile (da 7pt a 9pt) e righe più alte
    style.configure("Treeview",         font=("Helvetica", 9), rowheight=22)
    style.configure("Treeview.Heading", font=("Helvetica", 9, "bold"))


class BluetoothApp:
    """
    Classe principale per la gestione dell'applicazione Bluetooth.
    """
    ble_manager = BLEManager()

    def __init__(self, main_window):
        """
        Inizializza l'applicazione e configura l'interfaccia utente.
        """
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
        self.root.title("Checker")

        # Variabili legate allo stato dell'applicazione
        self.log_autoscroll = tk.BooleanVar(value=True)  # flag auto-scroll log
        self.connection_status = tk.StringVar(value="Disconnesso")
        self.device_info = tk.StringVar(value="")   # "Nome  |  MAC"
        self.data_output = tk.StringVar(value="")
        self.is_scanning = False

        # Configura il logger (file handler subito, TkTextHandler dopo create_widgets)
        self._setup_file_logging()

        # Configura gli stili e crea i widget
        setup_style()
        self.create_widgets()


    def _setup_file_logging(self):
        """Configura il root logger con handler su file rotativo."""
        formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILENAME, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        root_logger.addHandler(file_handler)

        self.log = logging.getLogger(__name__)
        self.log.info("Applicazione avviata.")

    def _attach_ui_logging(self, text_widget):
        """Aggiunge il TkTextHandler al root logger dopo che il widget è pronto."""
        formatter = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
        tk_handler = TkTextHandler(text_widget, autoscroll_var=self.log_autoscroll)
        tk_handler.setFormatter(formatter)
        tk_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(tk_handler)

    def create_widgets(self):
        # Frame principale verticale
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill="both", expand=True)
        main_frame.rowconfigure(0, weight=1)
        main_frame.rowconfigure(1, weight=0)
        main_frame.columnconfigure(0, weight=1)

        # PanedWindow nella parte superiore
        paned_window = ttk.PanedWindow(main_frame, orient="horizontal")
        paned_window.grid(row=0, column=0, sticky="nsew")

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

        # Log attività in basso a tutta larghezza
        # Header: titolo + checkbox auto-scroll sulla stessa riga
        log_header = ttk.Frame(main_frame)
        log_header.grid(row=1, column=0, sticky="ew", padx=10, pady=(4, 0))
        log_header.columnconfigure(0, weight=1)

        ttk.Label(log_header, text="Log Attività",
                  font=("Helvetica", 9, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(
            log_header, text="Auto-scroll",
            variable=self.log_autoscroll
        ).grid(row=0, column=1, sticky="e")

        log_frame = ttk.Frame(main_frame)
        log_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        log_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(2, weight=0)

        self.log_text = tk.Text(log_frame, height=7, state="disabled", wrap="none",
                                relief="sunken", borderwidth=1)
        self.log_text.grid(row=0, column=0, sticky="ew", padx=0, pady=0)

        scrollbar_v = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar_v.grid(row=0, column=1, sticky="ns")

        scrollbar_h = ttk.Scrollbar(log_frame, orient="horizontal", command=self.log_text.xview)
        scrollbar_h.grid(row=1, column=0, sticky="ew", pady=(0, 0))

        self.log_text.configure(yscrollcommand=scrollbar_v.set, xscrollcommand=scrollbar_h.set)

        # Collega il TkTextHandler ora che il widget è pronto, passando il flag
        self._attach_ui_logging(self.log_text)

    def _set_connection_status(self, connected: bool, text: str | None = None):
        """Aggiorna pallino colorato + testo stato + info dispositivo."""
        if connected:
            color = "green"
            label = text or "Connesso"
        else:
            color = "red"
            label = text or "Disconnesso"
            self.device_info.set("")
        self._dot.itemconfig(self._dot_id, fill=color, outline=color)
        self.connection_status.set(label)
        self.status_label.config(foreground=color)

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

    def _format_read_output(self, raw_bytes: bytes) -> str:
        """Formatta i byte letti in tre righe: HEX / DEC / ASCII."""
        hex_str   = "  ".join(f"{b:02X}" for b in raw_bytes)
        dec_str   = "  ".join(f"{b:>3d}" for b in raw_bytes)
        ascii_str = "   ".join(chr(b) if 32 <= b < 127 else "·" for b in raw_bytes)
        return f"HEX:   {hex_str}\nDEC:   {dec_str}\nASCII: {ascii_str}"

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
            pos = widget.index("insert")
            widget.delete(0, "end")
            widget.insert(0, spaced)
            # Riposiziona il cursore alla fine
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
            self.read_tree.insert("", "end",
                values=(f"{i}", f"{b:02X}", f"{b}", ascii_ch),
                tags=(tag,))
        self.read_tree.tag_configure("oddrow",  background="lightgrey")
        self.read_tree.tag_configure("evenrow", background="white")
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

    def create_left_widgets(self, frame):
        # ── Dispositivi Bluetooth ──────────────────────────────────────────────
        device_frame = ttk.LabelFrame(frame, text="Dispositivi Bluetooth")
        device_frame.pack(fill="x", padx=PAD_LG, pady=PAD_LG)

        self.device_list = tk.Listbox(device_frame, height=6, width=40)
        self.device_list.grid(row=0, column=0, padx=PAD_SM, pady=PAD_SM)

        device_controls = ttk.Frame(device_frame)
        device_controls.grid(row=0, column=1, padx=PAD, sticky="n")

        self.refresh_button = ttk.Button(
            device_controls, text="Scansiona", command=self.search_devices)
        self.refresh_button.grid(row=0, column=0, pady=PAD_SM, sticky="ew")

        self.connect_button = ttk.Button(
            device_controls, text="Connetti", command=self.connect_device)
        self.connect_button.grid(row=1, column=0, pady=PAD_SM, sticky="ew")

        self.disconnect_button = ttk.Button(
            device_controls, text="Disconnetti", command=self.disconnect_device,
            state="disabled")
        self.disconnect_button.grid(row=2, column=0, pady=PAD_SM, sticky="ew")

        # ── Stato connessione ──────────────────────────────────────────────────
        status_frame = ttk.LabelFrame(frame, text="Stato Connessione")
        status_frame.pack(fill="x", padx=PAD_LG, pady=(0, PAD))
        status_frame.columnconfigure(1, weight=1)

        # Pallino colorato (verde=connesso, rosso=disconnesso)
        self._dot = tk.Canvas(status_frame, width=14, height=14,
                              highlightthickness=0)
        self._dot_id = self._dot.create_oval(2, 2, 12, 12,
                                             fill="red", outline="red")
        self._dot.grid(row=0, column=0, rowspan=2,
                       padx=(PAD, PAD_SM), pady=PAD_SM, sticky="n")

        # Riga 1: testo stato bold
        self.status_label = ttk.Label(
            status_frame, textvariable=self.connection_status,
            style="Status.TLabel", foreground="red")
        self.status_label.grid(row=0, column=1, sticky="w", pady=(PAD_SM, 0))

        # Riga 2: nome dispositivo + MAC (visibile solo quando connesso)
        self.device_info_label = ttk.Label(
            status_frame, textvariable=self.device_info,
            font=("Helvetica", 9), foreground="gray")
        self.device_info_label.grid(row=1, column=1, sticky="w",
                                    pady=(0, PAD_SM))

        # Progress bar
        self.progress = ttk.Progressbar(
            status_frame, orient="horizontal",
            mode="indeterminate", length=140)
        self.progress.grid(row=0, column=2, rowspan=2,
                           padx=PAD, pady=PAD_SM, sticky="e")

        # ── Lettura EEPROM ─────────────────────────────────────────────────────
        read_frame = ttk.LabelFrame(frame, text="Lettura EEPROM")
        read_frame.pack(fill="x", padx=PAD_LG, pady=(0, PAD))
        read_frame.columnconfigure(1, weight=1)

        ttk.Label(read_frame, text="Indirizzo (es. 0x0016):").grid(
            row=0, column=0, padx=PAD, pady=PAD_SM, sticky="w")
        self.read_address_entry = ttk.Entry(read_frame, width=12)
        self.read_address_entry.grid(row=0, column=1, padx=PAD_SM,
                                     pady=PAD_SM, sticky="w")

        ttk.Label(read_frame, text="Dimensione (byte):").grid(
            row=1, column=0, padx=PAD, pady=PAD_SM, sticky="w")
        self.data_size_entry = ttk.Entry(read_frame, width=6)
        self.data_size_entry.grid(row=1, column=1, padx=PAD_SM,
                                  pady=PAD_SM, sticky="w")

        self.read_button = ttk.Button(
            read_frame, text="Leggi", command=self.on_read_button_pressed)
        self.read_button.grid(row=0, column=2, rowspan=2,
                              padx=PAD_SM, pady=PAD_SM, sticky="ns")

        # Output lettura: tabella per-byte (Offset | HEX | DEC | ASCII)
        result_lbl_frame = ttk.Frame(read_frame)
        result_lbl_frame.grid(row=2, column=0, columnspan=3,
                              padx=PAD, pady=(PAD_SM, PAD), sticky="ew")
        result_lbl_frame.columnconfigure(0, weight=1)

        ttk.Label(result_lbl_frame, text="Risultato:").grid(
            row=0, column=0, sticky="w")
        self.copy_to_write_btn = ttk.Button(
            result_lbl_frame, text="→ Copia in Scrittura",
            command=self._copy_read_to_write)
        self.copy_to_write_btn.grid(row=0, column=1, sticky="e")
        self.copy_to_write_btn.config(state="disabled")

        read_tree_frame = ttk.Frame(read_frame)
        read_tree_frame.grid(row=3, column=0, columnspan=3,
                             padx=PAD_SM, pady=(0, PAD), sticky="ew")
        read_tree_frame.columnconfigure(0, weight=1)

        self.read_tree = ttk.Treeview(
            read_tree_frame,
            columns=("Offset", "HEX", "DEC", "ASCII"),
            show="headings", height=4)
        self.read_tree.heading("Offset", text="Offset")
        self.read_tree.heading("HEX",    text="HEX")
        self.read_tree.heading("DEC",    text="DEC")
        self.read_tree.heading("ASCII",  text="ASCII")
        self.read_tree.column("Offset", width=55,  minwidth=45, anchor="center", stretch=False)
        self.read_tree.column("HEX",    width=55,  minwidth=45, anchor="center", stretch=False)
        self.read_tree.column("DEC",    width=55,  minwidth=45, anchor="center", stretch=False)
        self.read_tree.column("ASCII",  width=55,  minwidth=45, anchor="center", stretch=True)
        self.read_tree.grid(row=0, column=0, sticky="ew")

        read_tree_scroll = ttk.Scrollbar(
            read_tree_frame, orient="vertical", command=self.read_tree.yview)
        self.read_tree.configure(yscrollcommand=read_tree_scroll.set)
        read_tree_scroll.grid(row=0, column=1, sticky="ns")

        # ── Scrittura EEPROM ───────────────────────────────────────────────────
        write_frame = ttk.LabelFrame(frame, text="Scrittura EEPROM")
        write_frame.pack(fill="x", padx=PAD_LG, pady=(0, PAD_LG))
        write_frame.columnconfigure(1, weight=1)

        ttk.Label(write_frame, text="Indirizzo (es. 0x0016):").grid(
            row=0, column=0, padx=PAD, pady=PAD_SM, sticky="w")
        self.write_address_entry = ttk.Entry(write_frame, width=12)
        self.write_address_entry.grid(row=0, column=1, padx=PAD_SM,
                                      pady=PAD_SM, sticky="w")

        self.write_button = ttk.Button(
            write_frame, text="Scrivi", command=self.write_data_manually)
        self.write_button.grid(row=0, column=2, padx=PAD_SM,
                               pady=PAD_SM, sticky="ew")

        ttk.Label(write_frame, text="Dati hex (es. 03 66 36):").grid(
            row=1, column=0, padx=PAD, pady=PAD_SM, sticky="w")

        data_entry_frame = ttk.Frame(write_frame)
        data_entry_frame.grid(row=1, column=1, columnspan=2,
                              padx=PAD_SM, pady=PAD_SM, sticky="ew")
        data_entry_frame.columnconfigure(0, weight=1)

        # tk.Entry per poter impostare highlightcolor (validazione visuale)
        self.data_entry = tk.Entry(
            data_entry_frame, width=28,
            font=("Courier New", 10),
            relief="sunken", borderwidth=1,
            highlightthickness=2,
            highlightbackground="gray", highlightcolor="gray",
            insertbackground="black")
        self.data_entry.grid(row=0, column=0, sticky="ew")
        # Auto-spacing: inserisce uno spazio ogni 2 caratteri hex
        self.data_entry.bind("<KeyRelease>", self._on_data_entry_key)

        ttk.Button(
            data_entry_frame, text="✕ Pulisci",
            command=lambda: (
                self.data_entry.delete(0, "end"),
                self._validate_hex_input(self.data_entry)
            )
        ).grid(row=0, column=1, padx=(PAD_SM, 0))

    def create_right_widgets(self, frame):
        # ── Tabella parametri ──────────────────────────────────────────────────
        param_frame = ttk.LabelFrame(frame, text="Parametri")
        param_frame.pack(fill="both", expand=True, padx=PAD_LG, pady=PAD_LG)
        param_frame.grid_rowconfigure(0, weight=1)
        param_frame.grid_columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            param_frame,
            columns=("Nome", "Indirizzo", "Tipo", "Da Scrivere", "Letti"),
            show="headings", height=20)
        self.tree.heading("Nome",        text="Nome Parametro")
        self.tree.heading("Indirizzo",   text="Indirizzo")
        self.tree.heading("Tipo",        text="Tipo")
        self.tree.heading("Da Scrivere", text="Da Scrivere")
        self.tree.heading("Letti",       text="Letti")
        self.tree.column("Nome",        width=160, minwidth=100, stretch=True)
        self.tree.column("Indirizzo",   width=80,  minwidth=60,  stretch=False, anchor="center")
        self.tree.column("Tipo",        width=75,  minwidth=55,  stretch=False, anchor="center")
        self.tree.column("Da Scrivere", width=100, minwidth=80,  stretch=True,  anchor="center")
        self.tree.column("Letti",       width=100, minwidth=80,  stretch=True,  anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")

        self.tree.tag_configure("oddrow",   background="lightgrey")
        self.tree.tag_configure("evenrow",  background="white")
        self.tree.tag_configure("mismatch", background="#FEF3C7")  # giallo chiaro

        scrollbar = ttk.Scrollbar(param_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.load_config_parameters()

        # ── Barra pulsanti ─────────────────────────────────────────────────────
        buttons_frame = ttk.Frame(frame)
        buttons_frame.pack(fill="x", padx=PAD_LG, pady=(0, PAD))

        # Azioni safe – sinistra
        self.carica_btn = ttk.Button(
            buttons_frame, text="Carica Parametri", command=self.load_new_config)
        self.carica_btn.pack(side="left", padx=(0, PAD_SM))

        self.leggi_btn = ttk.Button(
            buttons_frame, text="Leggi Parametri", command=self.scarica_parametri)
        self.leggi_btn.pack(side="left", padx=(0, PAD_SM))

        self.verifica_btn = ttk.Button(
            buttons_frame, text="Leggi e Verifica", command=self.lettura_e_verifica)
        self.verifica_btn.pack(side="left", padx=(0, PAD_SM))

        self.salva_btn = ttk.Button(
            buttons_frame, text="Salva come CSV", command=self.save_as_csv)
        self.salva_btn.pack(side="left", padx=(0, PAD_SM))

        # Azione distruttiva – destra, visivamente separata
        self.scrivi_btn = ttk.Button(
            buttons_frame, text="⚠  Scrivi Parametri", command=self.scrivi_parametri)
        self.scrivi_btn.pack(side="right")

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
                            # Sostituisci ',' con '.' per parsing float
                            parsed_val = float(to_write.replace(',', '.'))
                            # Riformatta con virgola e rimuovi zeri finali
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

    def scarica_parametri(self):
        if not self.ble_manager.get_connection_status():
            messagebox.showerror("Errore", "Connetti un dispositivo prima di scaricare i parametri.")
            return

        self.log.info("Inizio scaricamento parametri...")
        self.progress.config(mode='determinate')
        self.progress['value'] = 0
        asyncio.run_coroutine_threadsafe(self._scarica_parametri_async(), self._ble_loop)

    def _build_read_chunks(self, params):
        """
        Raggruppa i parametri in chunk di lettura contigui o quasi-contigui.

        Ogni parametro è una tupla: (item_id, name, address, size, data_type).
        Restituisce una lista di chunk, ciascuno nella forma:
            {
                'start':  indirizzo di inizio del chunk,
                'size':   numero totale di byte da leggere,
                'params': [(item_id, name, address, size, data_type), ...]
            }

        Regole di raggruppamento:
          - Due parametri consecutivi (per indirizzo) vengono uniti se il gap tra loro
            è <= READ_MAX_GAP byte. I byte di gap vengono letti ma ignorati.
          - Un chunk viene chiuso e ne viene aperto uno nuovo se aggiungere il parametro
            successivo farebbe superare READ_MAX_CHUNK byte.
          - Parametri con indirizzo o size non validi vengono scartati con un log di errore.
        """
        # Scarta i parametri non validi e ordina per indirizzo
        valid = []
        for item_id, name, address_str, data_type in params:
            try:
                address = int(address_str, 16)
                size    = self.get_size_from_type(data_type)
                valid.append((item_id, name, address, size, data_type))
            except (ValueError, KeyError):
                self.log.error(f"Parametro '{name}' scartato: indirizzo o tipo non valido.")

        valid.sort(key=lambda p: p[2])  # ordina per address

        chunks = []
        current = None

        for param in valid:
            item_id, name, address, size, data_type = param

            if current is None:
                # Primo parametro: apri il primo chunk
                current = {'start': address, 'size': size, 'params': [param]}
            else:
                chunk_end  = current['start'] + current['size']  # byte successivo all'ultimo letto
                gap        = address - chunk_end                  # byte tra fine chunk e inizio parametro
                new_size   = (address + size) - current['start'] # dimensione chunk se aggiungessimo questo param

                if gap <= READ_MAX_GAP and new_size <= READ_MAX_CHUNK:
                    # Il parametro rientra nel chunk corrente (con eventuale gap tollerato)
                    current['size'] = new_size
                    current['params'].append(param)
                else:
                    # Chiudi il chunk corrente e aprine uno nuovo
                    chunks.append(current)
                    current = {'start': address, 'size': size, 'params': [param]}

        if current is not None:
            chunks.append(current)

        return chunks

    async def _scarica_parametri_async(self):
        children = list(self.tree.get_children())
        if not children:
            return

        # Rimuove highlight mismatch dalla sessione precedente
        for item in children:
            tags = [t for t in self.tree.item(item, "tags") if t != "mismatch"]
            self.tree.item(item, tags=tags)

        # Raccoglie (item_id, name, address_str, data_type) da tutti i parametri
        raw_params = [
            (item, self.tree.item(item)['values'][0],   # name
                   self.tree.item(item)['values'][1],   # address_str
                   self.tree.item(item)['values'][2])   # data_type
            for item in children
        ]

        chunks = self._build_read_chunks(raw_params)
        total_params = sum(len(c['params']) for c in chunks)
        done = 0

        self.log.info(
            f"Lettura ottimizzata: {total_params} parametri raggruppati in {len(chunks)} chunk."
        )

        for chunk in chunks:
            chunk_start = chunk['start']
            chunk_size  = chunk['size']

            self.log.debug(
                f"Chunk 0x{chunk_start:04X} | {chunk_size} byte | "
                f"{len(chunk['params'])} parametri"
            )

            # Lettura dell'intero chunk
            chunk_data = await self.read_data(chunk_start, chunk_size)

            for item_id, name, address, size, data_type in chunk['params']:
                if chunk_data is not None:
                    offset = address - chunk_start
                    param_bytes = chunk_data[offset: offset + size]
                    if len(param_bytes) == size:
                        value = self.interpret_data(param_bytes, data_type)
                    else:
                        self.log.error(
                            f"'{name}': byte estratti ({len(param_bytes)}) != attesi ({size})."
                        )
                        value = "N/A"
                else:
                    self.log.error(f"'{name}': lettura chunk fallita.")
                    value = "N/A"

                self.root.after(0, lambda it=item_id, v=value: self.tree.set(it, column="Letti", value=v))

                done += 1
                progress = (done / total_params) * 100
                self.root.after(0, lambda p=progress: self.update_progress_bar(p))

        self.root.after(0, lambda: self.progress.config(mode='indeterminate'))
        self.log.info("Scaricamento parametri completato.")

    def scrivi_parametri(self):
        if not self.ble_manager.get_connection_status():
            messagebox.showerror("Errore", "Connetti un dispositivo prima di scrivere i parametri.")
            return

        # Conta i parametri con un valore da scrivere
        n_params = sum(
            1 for item in self.tree.get_children()
            if self.tree.item(item)['values'][3]  # colonna "Da Scrivere"
        )
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

        self.log.info("Inizio scrittura parametri...")
        self.progress.config(mode='determinate')
        self.progress['value'] = 0
        asyncio.run_coroutine_threadsafe(self._scrivi_parametri_async(), self._ble_loop)

    async def _scrivi_parametri_async(self):
        children = list(self.tree.get_children())
        total = len(children)
        if total == 0:
            return

        errors = []
        for idx, item in enumerate(children):
            values = self.tree.item(item)['values']
            name, address_str, data_type, to_write, _ = values
            if not to_write:
                self.log.debug(f"Salto '{name}': nessun valore da scrivere.")
                continue
            try:
                address = int(address_str, 16)
                data = self.prepare_data_for_write(to_write, data_type)
                if data is None:
                    self.log.error(f"Impossibile preparare i dati per '{name}'")
                    errors.append(name)
                    continue
                success = await self.write_data(address, data)
                if success:
                    self.log.info(f"'{name}' scritto con successo.")
                else:
                    self.log.error(f"Scrittura fallita per '{name}'")
                    errors.append(name)
            except ValueError:
                self.log.error(f"Indirizzo o tipo non valido per '{name}'")
                errors.append(name)
                continue

            progress = ((idx + 1) / total) * 100
            self.root.after(0, lambda p=progress: self.update_progress_bar(p))

        self.root.after(0, lambda: self.progress.config(mode='indeterminate'))
        self.log.info("Scrittura parametri completata.")
        if errors:
            error_msg = f"Errori nella scrittura dei parametri: {', '.join(errors)}"
            self.log.error(error_msg)
            self.root.after(0, lambda: messagebox.showerror("Scrittura Parametri", error_msg))
        else:
            self.root.after(0, lambda: messagebox.showinfo("Scrittura Parametri", "Tutti i parametri scritti con successo."))

    def prepare_data_for_write(self, value_str, data_type):
        try:
            data_type = data_type.upper()
            value_str = str(value_str).strip()

            if data_type.endswith('H'):
                # Rimuove gli spazi interni (es. "03 66" -> "0366") perché fromhex
                # può essere schizzinoso con gli spazi a seconda della versione di Python
                clean_hex = value_str.replace(" ", "")

                # Se la stringa ha un numero dispari di caratteri (es. "A"), aggiunge uno zero (es. "0A")
                if len(clean_hex) % 2 != 0:
                    clean_hex = "0" + clean_hex

                return bytearray.fromhex(clean_hex)

            # Gestione STRING
            if data_type.startswith('STRING'):
                size = self.get_size_from_type(data_type)
                # Codifica la stringa in byte
                encoded = value_str.encode('utf-8')
                # Taglia se troppo lunga, o riempie con \x00 se più corta
                return encoded[:size].ljust(size, b'\x00')

            # Gestione FLOAT32
            if data_type == 'FLOAT32':
                val = float(value_str.replace(',', '.'))
                return struct.pack('<f', val)

            # Gestione UINT
            elif data_type.startswith('UINT'):
                bits = int(re.search(r'\d+', data_type).group())
                val = int(value_str)
                if bits == 8:
                    return struct.pack('<B', val)
                elif bits == 16:
                    return struct.pack('<H', val)
                elif bits == 24:
                    return struct.pack('<I', val)[:3]
                elif bits == 32:
                    return struct.pack('<I', val)

            return None
        except Exception as e:
            self.log.error(f"Errore preparazione dati per scrittura: {e}")
            return None

    def lettura_e_verifica(self):
        if not self.ble_manager.get_connection_status():
            messagebox.showerror("Errore", "Connetti un dispositivo prima di verificare i parametri.")
            return

        self.log.info("Inizio lettura e verifica parametri...")
        self.progress.config(mode='determinate')
        self.progress['value'] = 0
        asyncio.run_coroutine_threadsafe(self._lettura_e_verifica_async(), self._ble_loop)

    async def _lettura_e_verifica_async(self):
        # Prima scarica (legge) i parametri
        await self._scarica_parametri_async()

        # Ora verifica
        children = list(self.tree.get_children())
        errors = []
        for item in children:
            values = self.tree.item(item)['values']
            name, _, data_type, to_write, letti = values
            if not to_write:
                continue
            # Normalizza per confronto (stringhe con virgola)
            to_write_norm = str(to_write).replace(',', '.')
            letti_norm = str(letti).replace(',', '.')
            if to_write_norm != letti_norm:
                errors.append(name)
                tags = [t for t in self.tree.item(item, "tags")
                        if t not in ("oddrow", "evenrow", "mismatch")]
                self.tree.item(item, tags=tags + ["mismatch"])

        self.root.after(0, lambda: self.progress.config(mode='indeterminate'))
        if errors:
            error_msg = f"Parametri non corrispondenti: {', '.join(errors)}"
            self.log.error(error_msg)
            self.root.after(0, lambda: messagebox.showerror("Verifica", error_msg))
        else:
            self.log.info("Tutti i parametri verificati con successo.")
            self.root.after(0, lambda: messagebox.showinfo("Verifica", "Tutti i parametri verificati con successo."))

    def get_size_from_type(self, data_type):
        original_data_type = data_type
        data_type = data_type.upper()
        if data_type.endswith('H'):
            data_type = data_type[:-1]  # Rimuovi 'H' per calcolare la size sul tipo base
        sizes = {
            'UINT8': 1,
            'UINT16': 2,
            'UINT24': 3,
            'FLOAT32': 4,
        }
        if data_type.startswith('STRING'):
            # Prova a estrarre la lunghezza dal nome (es. STRING10 -> 10)
            # Se è solo STRING, metti un default (es. 20)
            match = re.search(r'\d+', data_type)
            return int(match.group()) if match else 20

        return sizes.get(data_type, 1)

    def interpret_data(self, data, data_type):
        if not data:
            return "N/A"

        data_type = data_type.upper()

        if data_type.endswith('H'):
            # Interpreta come esadecimale
            return ' '.join(f'{b:02x}' for b in data)

        # Caso STRING
        if data_type.startswith('STRING'):
            try:
                # Decodifica i byte in stringa, ignorando errori di decodifica
                text = data.decode('utf-8', errors='ignore').split('\x00')[0]
                return text.strip()
            except Exception:
                return "Errore Decodifica"

        # Caso FLOAT32
        if data_type == 'FLOAT32':
            val = struct.unpack_from('<f', data)[0]
            formatted = f"{val:.10f}".replace('.', ',').rstrip('0').rstrip(',')
            return formatted if formatted else "0"

        # Caso UINT (Little-endian)
        val = 0
        for i, byte in enumerate(data):
            val |= (byte << (8 * i))
        return val

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


    def search_devices(self):
        self.log.info("Avvio scansione dispositivi BLE...")
        self.progress.start()
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
        for address, (name, rssi) in devices.items():
            self.device_list.insert(tk.END, f"{name} - {address} - RSSI: {rssi}")
            if rssi > -50:
                try:
                    self.device_list.itemconfig(tk.END, {'bg': 'lightcoral'})
                except Exception:
                    pass
                self.log.info(f"Dispositivo trovato: {name} | {address} | RSSI: {rssi}")
        self.progress.stop()

    def connect_device(self):
        selected_device = self.device_list.get(tk.ACTIVE)
        if not selected_device:
            return
        try:
            parts = selected_device.split(" - ")
            dev_name = parts[0].strip()
            address  = parts[1].strip()
        except Exception:
            self.log.error("Formato voce lista dispositivi inatteso: impossibile estrarre l'indirizzo MAC.")
            return
        self.progress.start()
        self.executor.submit(self._connect_device, address, dev_name)

    def _connect_device(self, address, dev_name=""):
        self.log.info(f"Tentativo di connessione a {address}...")
        fut = asyncio.run_coroutine_threadsafe(
            self.ble_manager.connect_to_device(address, connection_timeout=15.0),
            self._ble_loop
        )
        try:
            fut.result()
            self.root.after(0, self.on_device_connected, address, dev_name)
        except Exception as e:
            self.log.error(f"Errore durante la connessione BLE: {e}")
            self.root.after(0, self._set_connection_status, False)
        finally:
            self.root.after(0, self.progress.stop)

    def on_device_connected(self, addr, dev_name=""):
        """Aggiorna l'interfaccia dopo una connessione riuscita."""
        self.log.info(f"Connesso con successo a {addr}")
        self._set_connection_status(True)
        self.device_info.set(f"{dev_name}   |   {addr}" if dev_name else addr)
        self.connect_button.config(state="disabled")
        self.disconnect_button.config(state="normal")
        self.monitor_connection()

    def disconnect_device(self):
        """Disconnette il dispositivo BLE in modo asincrono e aggiorna la GUI."""
        if not self.ble_manager.get_connection_status():
            self.log.warning("Nessun dispositivo connesso da disconnettere.")
            return

        self.log.info("Disconnessione in corso...")

        # Funzione asincrona per la disconnessione
        async def perform_disconnect():
            try:
                await self.ble_manager.disconnect_device()  # Tentativo di disconnessione
                self.root.after(0, self.on_disconnect_success)  # Callback per disconnessione riuscita
            except Exception as e:
                self.root.after(0, lambda: self.on_disconnect_error(e))

        # Esegui la coroutine nel loop BLE
        asyncio.run_coroutine_threadsafe(perform_disconnect(), self._ble_loop)

    def on_disconnect_success(self):
        """Callback per gestire una disconnessione completata con successo."""
        self.log.info("Disconnessione completata.")
        self._set_connection_status(False)
        self.connect_button.config(state="normal")
        self.disconnect_button.config(state="disabled")
        messagebox.showinfo("Disconnessione", "Dispositivo disconnesso correttamente.")

    def on_disconnect_error(self, error):
        """Callback per gestire errori durante la disconnessione."""
        self.log.error(f"Errore durante la disconnessione: {error}")
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

    def handle_disconnection(self):
        """Gestisce la disconnessione del dispositivo."""
        self.ble_manager.reset_connection_state()
        self._set_connection_status(False)
        self.log.warning("Connessione BLE persa inaspettatamente.")
        self.connect_button.config(state="normal")
        self.disconnect_button.config(state="disabled")
        #self.log_message("Tentativo di riconnessione...")
        #self.reconnect_device()

    def reconnect_device(self):
        """Tenta di riconnettere il dispositivo BLE."""
        try:
            self.connect_device()
            if self.ble_manager.get_connection_status():
                self._set_connection_status(True)
                self.log.info("Riconnessione riuscita.")
            else:
                self.log.error("Tentativo di riconnessione fallito.")
                self.root.after(10000, self.reconnect_device)
        except Exception as e:
            self.log.error(f"Errore durante la riconnessione: {e}")
            self.root.after(10000, self.reconnect_device)

    async def read_data(self, address, size, timeout=5):
        """
        Legge i dati BLE gestendo la segmentazione (se size > READ_MAX_CHUNK)
        e i tentativi di retry in caso di errore.

        Distingue tra errori transitori (timeout, payload corto) per cui ha
        senso riprovare, ed errori fatali (BleakError, connessione caduta) per
        cui il retry è inutile e si abbandona subito.
        """
        full_data = bytearray()
        bytes_read = 0

        while bytes_read < size:
            chunk_to_read = min(size - bytes_read, READ_MAX_CHUNK)
            current_addr  = address + bytes_read

            chunk_success = False
            for attempt in range(READ_RETRIES):
                try:
                    self.log.debug(
                        f"Lettura 0x{current_addr:04X} ({chunk_to_read}b)"
                        f" - Tentativo {attempt + 1}/{READ_RETRIES}"
                    )

                    data = await asyncio.wait_for(
                        self.ble_manager.read_eeprom(current_addr, chunk_to_read),
                        timeout=timeout
                    )

                    if data and len(data) >= 5:
                        payload = data[5:]
                        if len(payload) >= chunk_to_read:
                            full_data.extend(payload[:chunk_to_read])
                            chunk_success = True
                            break  # successo, esci dal retry
                        else:
                            # Payload corto: errore transitorio, si riprova
                            self.log.warning(
                                f"Payload corto a 0x{current_addr:04X}: "
                                f"ricevuti {len(payload)}b, attesi {chunk_to_read}b"
                            )
                    else:
                        # Risposta nulla o troppo corta: errore transitorio
                        self.log.warning(f"Risposta non valida o nulla a 0x{current_addr:04X}")

                except asyncio.TimeoutError:
                    # Errore transitorio: il dispositivo non ha risposto in tempo
                    self.log.warning(
                        f"Timeout al tentativo {attempt + 1}/{READ_RETRIES}"
                        f" per 0x{current_addr:04X}"
                    )

                except BleakError as e:
                    # Errore fatale BLE (connessione caduta, caratteristica non trovata, ecc.)
                    # Riprovare non ha senso: si abbandona immediatamente
                    self.log.error(
                        f"Errore BLE fatale a 0x{current_addr:04X}: {e} — lettura abortita"
                    )
                    return None

                except Exception as e:
                    # Errore inatteso: loggato come fatale, si abbandona
                    self.log.error(
                        f"Errore imprevisto a 0x{current_addr:04X}: {e} — lettura abortita"
                    )
                    return None

                # Backoff solo per errori transitori (timeout / payload corto)
                await asyncio.sleep(0.2)

            if not chunk_success:
                self.log.error(
                    f"Fallimento definitivo dopo {READ_RETRIES} tentativi a 0x{current_addr:04X}"
                )
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

        self.connection_status.set("Lettura in corso...")
        self.status_label.config(foreground="darkorange")
        self._dot.itemconfig(self._dot_id, fill="darkorange", outline="darkorange")
        self.root.update()

        # Avvia la lettura
        output = await self.read_data(address, size)
        if output:
            self._set_connection_status(True, "Lettura completata")
            self.root.after(0, self._update_read_output, bytes(output))
        else:
            self._set_connection_status(False, "Errore lettura")

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
            # Ottieni i dati dalla GUI (assume che gli input siano validati correttamente)
            address = int(self.write_address_entry.get(), 16)  # Indirizzo (in hex)
            data = bytearray.fromhex(self.data_entry.get())  # Dati (in formato hex)
        except ValueError:
            self.log.error("Input manuale non valido: indirizzo o dati in formato errato.")
            messagebox.showerror("Errore", "Indirizzo o dati in formato non valido.")
            return

        asyncio.run_coroutine_threadsafe(self.write_data(address, data), self._ble_loop)
        self.log.info(f"Scrittura manuale avviata: indirizzo={hex(address)}, dati={self.data_entry.get()}")

    async def write_data(self, starting_address, data):
        """ Scrive dati su BLE in modo asincrono utilizzando AsyncioWorker. """
        if not self.ble_manager.get_connection_status():
            self.log.error("Scrittura fallita: nessun dispositivo connesso.")
            messagebox.showerror("Errore", "Nessun dispositivo connesso!")
            return False

        try:
            result = await self.ble_manager.write_eeprom(starting_address, data)
            if not result:
                self.log.error(f"Scrittura all'indirizzo {hex(starting_address)} non confermata dal dispositivo.")
                messagebox.showerror("Errore", "Scrittura fallita: dispositivo non ha confermato l'operazione.")
                return False
            else:
                self.log.info(f"Scrittura all'indirizzo {hex(starting_address)} completata con successo.")
                return True
        except Exception as e:
            self.log.error(f"Errore durante la scrittura all'indirizzo {hex(starting_address)}: {e}")
            messagebox.showerror("Errore", f"Errore durante la scrittura: {str(e)}")
            return False


    def update_progress_bar(self, value):
        """
        Aggiorna il valore della barra di progresso.
        :param value: Valore percentuale (0-100).
        """
        # Controlla che il valore sia nel range corretto
        if 0 <= value <= 100:
            self.progress["value"] = value
            self.root.update_idletasks()  # Forza l'aggiornamento della GUI
        else:
            self.log.error(f"Valore non valido per la barra di progresso: {value} (atteso 0-100)")

    def on_close(self):
        self._shutdown_ble_loop(join_timeout=3.0)
        self.executor.shutdown(wait=True)
        self.root.destroy()



if __name__ == "__main__":
    root = tk.Tk()
    app = BluetoothApp(root)
    #root.iconbitmap('logo2.ico')
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()