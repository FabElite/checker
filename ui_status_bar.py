import tkinter as tk
from tkinter import ttk


class MiniStatusBar(tk.Frame):
    """
    Barra di stato compatta a tre righe:
      riga 0 : LED heartbeat UI · LED BLE · testo stato BLE · nome/MAC dispositivo
      riga 1 : testo attività corrente (con severità cromatica)
      riga 2 : barra di progresso (indeterminate | determinate)

    Utilizzo tipico
    ---------------
        bar = MiniStatusBar(root)
        bar.pack(fill="x", side="bottom")

        # loop heartbeat
        def _pulse():
            bar.pulse()
            root.after(500, _pulse)
        _pulse()

        # aggiornamento BLE
        bar.set_ble("ok")
        bar.set_device_info("MySensor", "E4:B3:23:A1:5D:12")
        bar.set_activity("Connessione riuscita", "success")
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

        # ── riga 0: LED + info dispositivo ────────────────────────────────────
        self._phase = False

        self.led_ui = tk.Label(
            self, text="●", bg=self._BG, fg="#555555", font=("Helvetica", 18))
        self.led_ui.grid(row=0, column=0, padx=(10, 6))

        self.led_ble = tk.Label(
            self, text="●", bg=self._BG, fg="#555555", font=("Helvetica", 18))
        self.led_ble.grid(row=0, column=1, padx=(8, 6))

        self.lbl_ble = tk.Label(
            self, text="BLE: Disconnesso",
            bg=self._BG, fg="#aaaaaa", font=("Helvetica", 9, "bold"))
        self.lbl_ble.grid(row=0, column=2, sticky="w")

        self.lbl_dev = tk.Label(
            self, text="—",
            bg=self._BG, fg="#777799", font=("Helvetica", 8))
        self.lbl_dev.grid(row=0, column=3, padx=(12, 10), sticky="w")

        # ── riga 1: testo attività ─────────────────────────────────────────────
        self._activity_var = tk.StringVar(value="")
        self.lbl_activity = tk.Label(
            self, textvariable=self._activity_var,
            bg=self._BG, fg=self._SEV_COLORS["info"],
            font=("Helvetica", 9))
        self.lbl_activity.grid(
            row=1, column=0, columnspan=99, sticky="w", padx=10, pady=(2, 0))

        # ── riga 2: progress bar ───────────────────────────────────────────────
        self.progress = ttk.Progressbar(
            self, orient="horizontal", mode="indeterminate", length=260)
        self.progress.grid(
            row=2, column=0, columnspan=99, sticky="ew", padx=10, pady=(4, 4))

        self.grid_columnconfigure(99, weight=1)

    # ── API pubblica ───────────────────────────────────────────────────────────

    def pulse(self):
        """Alterna il colore del LED UI: indica che l'applicazione è viva."""
        self._phase = not self._phase
        self.led_ui.config(fg="#44dd66" if self._phase else "#008822")

    def set_ble(self, state: str):
        """
        Imposta lo stato del LED e del testo BLE.
        state ∈ {'ok', 'err', 'warn', 'off'}
        """
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
        """Mostra nome + MAC quando connesso; ripristina il trattino altrimenti."""
        if name and address:
            self.lbl_dev.config(text=f"{name}  —  {address}", fg="#88ffaa")
        elif address:
            self.lbl_dev.config(text=address, fg="#88ffaa")
        else:
            self.lbl_dev.config(text="—", fg="#777799")

    def set_activity(self, text: str, severity: str = "info"):
        """
        Aggiorna il testo di attività con il colore corrispondente alla severità.
        severity ∈ {'info', 'warn', 'error', 'success'}
        """
        self._activity_var.set(text)
        self.lbl_activity.config(
            fg=self._SEV_COLORS.get(severity, self._SEV_COLORS["info"]))

    # ── Progress bar helpers ───────────────────────────────────────────────────

    def progress_mode(self, mode: str):
        """'indeterminate' | 'determinate'"""
        self.progress.config(mode=mode)

    def progress_start(self):
        """Avvia l'animazione indeterminate."""
        self.progress.start(10)

    def progress_stop(self):
        """Ferma l'animazione e azzera."""
        self.progress.stop()
        self.progress["value"] = 0

    def progress_set(self, value: float):
        """Imposta il valore (0–100) in modalità determinate."""
        self.progress["value"] = max(0.0, min(100.0, value))