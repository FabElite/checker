"""
ui_widgets.py
─────────────
Widget helper riutilizzabili: status bar, log handler, tooltip Treeview.
Importato da main.py.
"""
import tkinter as tk
from tkinter import ttk
import logging


# ── Spaziatura uniforme (condivisa con main.py) ──────────────────────────────
PAD    = 8
PAD_SM = 4
PAD_LG = 12


# ── Tabella di riferimento tipi dati supportati ──────────────────────────────
# (tipo, byte, esempio valore, descrizione)
TIPI_SUPPORTATI = [
    ("UINT8",      "1",   "0 … 255",                  "Intero senza segno 8 bit"),
    ("UINT16",     "2",   "0 … 65535",                 "Intero senza segno 16 bit, little-endian"),
    ("UINT24",     "3",   "0 … 16777215",              "Intero senza segno 24 bit, little-endian"),
    ("UINT32",     "4",   "0 … 4294967295",            "Intero senza segno 32 bit, little-endian"),
    ("FLOAT32",    "4",   "-0,6704  /  3,14",          "Virgola mobile 32 bit IEEE 754, sep. virgola"),
    ("STRING<N>",  "N",   "STRING20  →  testo",        "Stringa UTF-8 di N byte, padding con \\0"),
    ("UINT8[N]",   "N",   "UINT8[4]  →  192.168.1.1", "Array di N byte separati da punto"),
    ("<N>H",       "N",   "4H  →  AB CD EF 01",        "N byte grezzi in esadecimale"),
]


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                              STILE GLOBALE                               ║
# ╚══════════════════════════════════════════════════════════════════════════╝
def setup_style():
    """Configura font e padding mantenendo il tema di sistema."""
    style = ttk.Style()
    style.configure("TButton",          font=("Helvetica", 10), padding=(8, 4))
    style.configure("TLabel",           font=("Helvetica", 10))
    style.configure("TEntry",           font=("Helvetica", 10), padding=3)
    style.configure("Status.TLabel",    font=("Helvetica", 10, "bold"))
    style.configure("Treeview",         font=("Helvetica", 9), rowheight=22)
    style.configure("Treeview.Heading", font=("Helvetica", 9, "bold"))


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                       STATUS BAR (3 RIGHE INTEGRATE)                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝
class TriStatusBar(tk.Frame):
    """
    Barra di stato compatta a tre righe:
      riga 0 : LED UI (heartbeat) · LED BLE · label stato BLE · device info
      riga 1 : testo attività (con severità cromatica)
      riga 2 : barra di progresso (indeterminate | determinate)
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
        self._phase = False

        # ─ riga 0 ─ LED UI
        led_ui_frame = tk.Frame(self, bg=self._BG)
        led_ui_frame.grid(row=0, column=0, padx=(10, 4))
        self.led_ui = tk.Label(led_ui_frame, text="●", bg=self._BG, fg="#555555",
                               font=("Helvetica", 16))
        self.led_ui.grid(row=0, column=0)
        tk.Label(led_ui_frame, text="UI", bg=self._BG, fg="#666688",
                 font=("Helvetica", 7)).grid(row=1, column=0)

        # separatore verticale
        tk.Frame(self, bg="#444466", width=1).grid(
            row=0, column=1, sticky="ns", padx=(2, 4), pady=4)

        # ─ riga 0 ─ LED BLE
        led_ble_frame = tk.Frame(self, bg=self._BG)
        led_ble_frame.grid(row=0, column=2, padx=(4, 6))
        self.led_ble = tk.Label(led_ble_frame, text="●", bg=self._BG, fg="#555555",
                                font=("Helvetica", 16))
        self.led_ble.grid(row=0, column=0)
        tk.Label(led_ble_frame, text="BLE", bg=self._BG, fg="#666688",
                 font=("Helvetica", 7)).grid(row=1, column=0)

        # ─ riga 0 ─ testo BLE + info dispositivo
        self.lbl_ble = tk.Label(self, text="BLE: Disconnesso",
                                bg=self._BG, fg="#aaaaaa", font=("Helvetica", 9, "bold"))
        self.lbl_dev = tk.Label(self, text="—",
                                bg=self._BG, fg="#777799", font=("Helvetica", 8))
        self.lbl_ble.grid(row=0, column=3, sticky="w", padx=(2, 8))
        self.lbl_dev.grid(row=0, column=4, padx=(4, 10), sticky="w")

        # ─ riga 1 ─ attività
        self._activity_var = tk.StringVar(value="")
        self.lbl_activity = tk.Label(self, textvariable=self._activity_var,
                                     bg=self._BG, fg=self._SEV_COLORS["info"],
                                     font=("Helvetica", 9))
        self.lbl_activity.grid(row=1, column=0, columnspan=5, sticky="w", padx=10, pady=(2, 0))

        # placeholder per checkbox autoscroll (inserito dall'app dopo init)
        self._autoscroll_placeholder = tk.Frame(self, bg=self._BG)
        self._autoscroll_placeholder.grid(row=1, column=99, sticky="e",
                                          padx=(4, 10), pady=(2, 0))

        # ─ riga 2 ─ progress bar
        self.progress = ttk.Progressbar(self, orient="horizontal",
                                        mode="indeterminate", length=260)
        self.progress.grid(row=2, column=0, columnspan=99,
                           sticky="ew", padx=10, pady=(4, 4))
        self.grid_columnconfigure(99, weight=1)

    # ── API pubblica ─────────────────────────────────────────────────────────

    def pulse(self):
        """Battito LED UI: indica che l'UI è viva."""
        self._phase = not self._phase
        self.led_ui.config(fg="#44dd66" if self._phase else "#008822")

    def set_ble(self, state: str):
        """Imposta stato BLE: 'ok' | 'err' | 'warn' | 'off'."""
        col = self._LED_COLORS.get(state, "#555555")
        self.led_ble.config(fg=col)
        if state == "ok":
            self.lbl_ble.config(text="BLE: Connesso",    fg="#44ff88")
        elif state == "err":
            self.lbl_ble.config(text="BLE: Errore",      fg="#ff6666")
        elif state == "warn":
            self.lbl_ble.config(text="BLE: Attenzione",  fg="#ffcc66")
        else:
            self.lbl_ble.config(text="BLE: Disconnesso", fg="#aaaaaa")

    def set_device_info(self, name: str | None, address: str | None):
        """Mostra/azzera info dispositivo collegato."""
        if name and address:
            self.lbl_dev.config(text=f"{name} - {address}", fg="#88ffaa")
        elif address:
            self.lbl_dev.config(text=address, fg="#88ffaa")
        else:
            self.lbl_dev.config(text="—", fg="#777799")

    def set_activity(self, text: str, severity: str = "info"):
        """Imposta testo attività con severità: info | warn | error | success."""
        self._activity_var.set(text)
        self.lbl_activity.config(
            fg=self._SEV_COLORS.get(severity, self._SEV_COLORS["info"]))

    # ── Progress helpers ─────────────────────────────────────────────────────

    def progress_mode(self, mode: str):
        """'indeterminate' | 'determinate'"""
        self.progress.config(mode=mode)

    def progress_start(self):
        self.progress.start(10)

    def progress_stop(self):
        self.progress.stop()
        self.progress["value"] = 0

    def progress_set(self, value: float):
        """Imposta il valore (0–100) in modalità determinate."""
        self.progress["value"] = max(0.0, min(100.0, value))


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                           LOG HANDLER TK                                 ║
# ╚══════════════════════════════════════════════════════════════════════════╝
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
        self.text_widget    = text_widget
        self.autoscroll_var = autoscroll_var
        text_widget.tag_configure("debug",    foreground="gray")
        text_widget.tag_configure("info",     foreground="black")
        text_widget.tag_configure("warning",  foreground="darkorange")
        text_widget.tag_configure("error",    foreground="red")
        text_widget.tag_configure("critical", foreground="red",
                                  font=("Helvetica", 10, "bold"))

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

        self.text_widget.after(0, _append)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                     TOOLTIP COLONNA "TIPO" (TREEVIEW)                   ║
# ╚══════════════════════════════════════════════════════════════════════════╝
class TreeviewTypeTooltip:
    """
    Mostra un tooltip quando il cursore si trova sopra la colonna 'Tipo'
    del Treeview dei parametri. Il popup riporta tipo, dimensione e
    una breve descrizione presi da TIPI_SUPPORTATI.
    """

    _TYPE_INFO = {t: (b, ex, desc) for t, b, ex, desc in TIPI_SUPPORTATI}
    _FAMILY_PREFIX = [
        ("STRING", "STRING<N>"),
        ("UINT8[", "UINT8[N]"),
    ]

    def __init__(self, tree: ttk.Treeview, tipo_col: str = "Tipo"):
        self._tree = tree
        self._col  = tipo_col
        self._tip  = None
        self._last = None

        tree.bind("<Motion>", self._on_motion)
        tree.bind("<Leave>",  self._hide)
        tree.bind("<Button>", self._hide)

    def _resolve(self, raw: str):
        """Trova la riga in _TYPE_INFO corrispondente al tipo raw."""
        key = raw.upper()
        if key in self._TYPE_INFO:
            return key, *self._TYPE_INFO[key]
        for prefix, canonical in self._FAMILY_PREFIX:
            if key.startswith(prefix):
                return canonical, *self._TYPE_INFO[canonical]
        if key.endswith("H") and key[:-1].isdigit():
            return "<N>H", *self._TYPE_INFO["<N>H"]
        return None

    def _on_motion(self, event):
        tree   = self._tree
        col_id = tree.identify_column(event.x)
        row_id = tree.identify_row(event.y)
        try:
            col_name = tree.column(col_id, option="id")
        except Exception:
            self._hide()
            return

        if col_name != self._col or not row_id:
            self._hide()
            return

        cell_key = (row_id, col_id)
        if cell_key == self._last:
            return
        self._last = cell_key

        raw_type = tree.set(row_id, self._col)
        info = self._resolve(raw_type)
        if not info:
            self._hide()
            return

        canonical, byte_size, esempio, descrizione = info
        self._show(event, raw_type, canonical, byte_size, esempio, descrizione)

    def _show(self, event, raw, canonical, byte_size, esempio, descrizione):
        self._hide()
        x = self._tree.winfo_rootx() + event.x + 16
        y = self._tree.winfo_rooty() + event.y + 12

        tip = tk.Toplevel(self._tree)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tip.configure(bg="#fffbe6", relief="solid", borderwidth=1)

        header = raw if raw.upper() == canonical else f"{raw}  →  {canonical}"

        tk.Label(tip, text=header, bg="#fffbe6", fg="#333300",
                 font=("Helvetica", 9, "bold"),
                 padx=8, anchor="w").pack(fill="x", pady=(4, 0))

        tk.Label(tip, text=descrizione, bg="#fffbe6", fg="#555500",
                 font=("Helvetica", 8),
                 padx=8, anchor="w").pack(fill="x")

        tk.Frame(tip, bg="#cccc99", height=1).pack(fill="x", padx=6, pady=3)

        tk.Label(tip, text=f"Dimensione:  {byte_size} byte",
                 bg="#fffbe6", fg="#444400",
                 font=("Helvetica", 8),
                 padx=8, anchor="w").pack(fill="x")

        tk.Label(tip, text=f"Esempio:       {esempio}",
                 bg="#fffbe6", fg="#444400",
                 font=("Helvetica", 8),
                 padx=8, anchor="w").pack(fill="x", pady=(0, 4))

        self._tip = tip

    def _hide(self, _event=None):
        self._last = None
        if self._tip:
            self._tip.destroy()
            self._tip = None
