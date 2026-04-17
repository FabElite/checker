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
    style = ttk.Style()
    style.configure("TButton",          font=("Helvetica", 10), padding=(8, 4))
    style.configure("TLabel",           font=("Helvetica", 10))
    style.configure("TEntry",           font=("Helvetica", 10), padding=3)
    style.configure("Status.TLabel",    font=("Helvetica", 10, "bold"))
    style.configure("Treeview",         font=("Helvetica", 9), rowheight=19)
    style.configure("Treeview.Heading", font=("Helvetica", 9, "bold"))

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                          WIDGET CUSTOM MINIMAL                           ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class CanvasLED(tk.Canvas):
    """Un LED grafico minimale con effetto alone (glow)."""
    def __init__(self, parent, size=14, bg_color="#1e1e2e"):
        super().__init__(parent, width=size, height=size, bg=bg_color, highlightthickness=0)
        self.size = size
        self.center = size / 2
        self.radius = (size / 2) - 3

        # Disegna l'alone (nascosto di default) e il core
        self.halo = self.create_oval(1, 1, size-1, size-1, fill="", outline="")
        self.core = self.create_oval(
            self.center - self.radius, self.center - self.radius,
            self.center + self.radius, self.center + self.radius,
            fill="#333344", outline=""
        )

    def set_color(self, core_color, halo_color=None):
        """Imposta il colore del LED. Passa halo_color per un leggero bagliore."""
        self.itemconfig(self.core, fill=core_color)
        if halo_color:
            self.itemconfig(self.halo, fill=halo_color)
        else:
            self.itemconfig(self.halo, fill="")


class PillProgress(tk.Canvas):
    """Barra di progresso slim a forma di pillola (angoli arrotondati)."""

    def __init__(self, parent, width=110, height=6, bg_color="#1e1e2e", track_color="#2a2a3d", fill_color="#44ff88"):
        # Chiamata esplicita con keyword arguments per evitare errori Tcl
        super().__init__(parent, width=width, height=height + 4, bg=bg_color, highlightthickness=0)

        # Usiamo nomi variabili che non collidono con quelli interni di tk.Canvas
        self.canvas_w = width
        self.canvas_h = height
        self.y_off = 2
        self.track_c = track_color
        self.fill_c = fill_color

        self.mode = "indeterminate"
        self.val = 0.0
        self.indet_pos = 0.0
        self.indet_dir = 1
        self.is_running = False
        self.anim_job = None

        self._draw_track()

    def _create_round_rect(self, x1, y1, x2, y2, color, tag=""):
        """Disegna un rettangolo con angoli arrotondati (Pill)."""
        r = (y2 - y1) / 2
        if x2 - x1 < 2 * r: x2 = x1 + 2 * r

        # Creazione della forma pillola: due cerchi alle estremità e un rettangolo centrale
        self.create_oval(x1, y1, x1 + 2 * r, y2, fill=color, outline="", tags=tag)
        self.create_oval(x2 - 2 * r, y1, x2, y2, fill=color, outline="", tags=tag)
        self.create_rectangle(x1 + r, y1, x2 - r, y2, fill=color, outline="", tags=tag)

    def _draw_track(self):
        self._create_round_rect(0, self.y_off, self.canvas_w, self.y_off + self.canvas_h, self.track_c)

    def _update_bar(self):
        self.delete("bar")
        if self.mode == "determinate":
            if self.val <= 0: return
            w = (self.val / 100.0) * self.canvas_w
            w = max(w, self.canvas_h)
            self._create_round_rect(0, self.y_off, w, self.y_off + self.canvas_h, self.fill_c, tag="bar")
        else:
            if not self.is_running: return
            pill_w = 30
            x1 = self.indet_pos
            x2 = x1 + pill_w
            self._create_round_rect(x1, self.y_off, x2, self.y_off + self.canvas_h, self.fill_c, tag="bar")

    def _animate(self):
        if not self.is_running: return
        speed = 2.5
        self.indet_pos += speed * self.indet_dir

        if self.indet_pos + 30 >= self.canvas_w:
            self.indet_pos = self.canvas_w - 30
            self.indet_dir = -1
        elif self.indet_pos <= 0:
            self.indet_pos = 0
            self.indet_dir = 1

        self._update_bar()
        self.anim_job = self.after(16, self._animate)

    def config_mode(self, mode: str):
        self.mode = mode
        self.delete("bar")

    def start(self):
        if self.mode == "indeterminate" and not self.is_running:
            self.is_running = True
            self._animate()

    def stop(self):
        self.is_running = False
        if self.anim_job:
            self.after_cancel(self.anim_job)
            self.anim_job = None
        self.val = 0
        self.delete("bar")

    def set_value(self, value: float):
        if self.mode == "determinate":
            self.val = max(0.0, min(100.0, value))
            self._update_bar()

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                       STATUS BAR (3 RIGHE INTEGRATE)                     ║
# ╚══════════════════════════════════════════════════════════════════════════╝
class TriStatusBar(tk.Frame):
    _BG = "#1e1e2e"

    # Colori per i LED (core, halo)
    _LED_COLORS = {
        "ok":   ("#00cc44", "#004411"),
        "err":  ("#cc2222", "#440000"),
        "warn": ("#cc8800", "#442200"),
        "off":  ("#333344", ""),
    }
    _SEV_COLORS = {
        "info":    "#cfd2ff",
        "warn":    "#ffcc66",
        "error":   "#ff7777",
        "success": "#88ffaa",
    }

    def __init__(self, parent):
        super().__init__(parent, bg=self._BG, pady=4)
        self._phase = False

        # ─ riga 0 ─ LED UI
        led_ui_frame = tk.Frame(self, bg=self._BG)
        led_ui_frame.grid(row=0, column=0, padx=(10, 6))
        self.led_ui = CanvasLED(led_ui_frame, size=12, bg_color=self._BG)
        self.led_ui.grid(row=0, column=0, pady=(0, 2))
        tk.Label(led_ui_frame, text="UI", bg=self._BG, fg="#666688",
                 font=("Helvetica", 7, "bold")).grid(row=1, column=0)

        # separatore verticale (ora più delicato)
        sep_canvas = tk.Canvas(self, width=1, height=16, bg=self._BG, highlightthickness=0)
        sep_canvas.create_line(0, 0, 0, 16, fill="#3a3a50", width=1)
        sep_canvas.grid(row=0, column=1, sticky="ns", padx=(2, 6))

        # ─ riga 0 ─ LED BLE
        led_ble_frame = tk.Frame(self, bg=self._BG)
        led_ble_frame.grid(row=0, column=2, padx=(4, 8))
        self.led_ble = CanvasLED(led_ble_frame, size=12, bg_color=self._BG)
        self.led_ble.grid(row=0, column=0, pady=(0, 2))
        tk.Label(led_ble_frame, text="BLE", bg=self._BG, fg="#666688",
                 font=("Helvetica", 7, "bold")).grid(row=1, column=0)

        # ─ riga 0 ─ testo BLE + info dispositivo
        self.lbl_ble = tk.Label(self, text="BLE: Disconnesso",
                                bg=self._BG, fg="#aaaaaa", font=("Helvetica", 9))
        self.lbl_dev = tk.Label(self, text="—",
                                bg=self._BG, fg="#777799", font=("Helvetica", 8))
        self.lbl_ble.grid(row=0, column=3, sticky="w", padx=(2, 8))
        self.lbl_dev.grid(row=0, column=4, padx=(4, 12), sticky="w")

        # ─ riga 0 ─ attività (a sinistra della barra)
        self._activity_var = tk.StringVar(value="")
        self.lbl_activity = tk.Label(self, textvariable=self._activity_var,
                                     bg=self._BG, fg=self._SEV_COLORS["info"],
                                     font=("Helvetica", 9))
        self.lbl_activity.grid(row=0, column=6, sticky="e", padx=(4, 8))

        # ─ riga 0 ─ barra di progresso minimal fissa a destra
        self.progress = PillProgress(self, width=110, height=6, bg_color=self._BG)
        self.progress.grid(row=0, column=7, padx=(0, 10))

        # col 5 (tra lbl_dev e lbl_activity) è lo spazio elastico
        self.grid_columnconfigure(5, weight=1)
        self.led_ui.set_color(*self._LED_COLORS["off"])
        self.led_ble.set_color(*self._LED_COLORS["off"])

    # ── API pubblica (compatibile con l'esistente) ───────────────────────────

    def pulse(self):
        """Battito LED UI: indica che l'UI è viva."""
        self._phase = not self._phase
        core = "#44ff88" if self._phase else "#008822"
        halo = "#004411" if self._phase else ""
        self.led_ui.set_color(core, halo)

    def set_ble(self, state: str):
        """Imposta stato BLE: 'ok' | 'err' | 'warn' | 'off'."""
        colors = self._LED_COLORS.get(state, self._LED_COLORS["off"])
        self.led_ble.set_color(*colors)

        if state == "ok":
            self.lbl_ble.config(text="BLE: Connesso",    fg="#44ff88")
        elif state == "err":
            self.lbl_ble.config(text="BLE: Errore",      fg="#ff6666")
        elif state == "warn":
            self.lbl_ble.config(text="BLE: Attenzione",  fg="#ffcc66")
        else:
            self.lbl_ble.config(text="BLE: Disconnesso", fg="#aaaaaa")

    def set_device_info(self, name: str | None, address: str | None):
        if name and address:
            self.lbl_dev.config(text=f"{name} - {address}", fg="#cfd2ff")
        elif address:
            self.lbl_dev.config(text=address, fg="#cfd2ff")
        else:
            self.lbl_dev.config(text="—", fg="#777799")

    def set_activity(self, text: str, severity: str = "info"):
        self._activity_var.set(text)
        self.lbl_activity.config(
            fg=self._SEV_COLORS.get(severity, self._SEV_COLORS["info"]))

    # ── Progress helpers (Mappati sulla nuova PillProgress) ──────────────────

    def progress_mode(self, mode: str):
        """'indeterminate' | 'determinate'"""
        self.progress.config_mode(mode)

    def progress_start(self):
        self.progress.start()

    def progress_stop(self):
        self.progress.stop()

    def progress_set(self, value: float):
        """Imposta il valore (0–100) in modalità determinate."""
        self.progress.set_value(value)

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
# ║                     TOOLTIP COLONNA "TIPO" (TREEVIEW)                    ║
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