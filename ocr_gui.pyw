"""
Grafische Oberfläche für ocr.py – Texterkennung über einen lokalen Lemonade Server.

Start:   Doppelklick auf ocr_gui.pyw   (ocr.py muss im selben Ordner liegen)
         oder:  pythonw ocr_gui.pyw
Tipp:    Dateien lassen sich auch direkt auf das Programmsymbol ziehen.
         Mit Eingabe- und Ausgabe-Ordner (optional mit Überwachung) wird alles
         verarbeitet, was im Eingabe-Ordner liegt bzw. neu hinzukommt.

Benötigt: pip install openai pymupdf    (Pandoc nur für Word/LibreOffice)
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ocr  # noqa: E402  (ocr.py aus demselben Ordner)

EINSTELLUNGEN = Path.home() / ".ocr-gui.json"
_BILDER = " ".join("*" + e for e in ocr.ENDUNGEN if e != ".pdf")
DATEITYPEN = [("PDF und Bilder", "*.pdf " + _BILDER), ("PDF", "*.pdf"), ("Bilder", _BILDER),
              ("Alle Dateien", "*.*")]
INTERVALL = 10   # Sekunden zwischen zwei Prüfungen des Eingabe-Ordners
MODI = {
    "LightOnOCR-3 mit Layout und Bildern (empfohlen)": "grounding",
    "Nur Text (LightOnOCR, leerer Prompt)": "",
    "PaddleOCR-VL: Text  (OCR:)": "OCR:",
    "PaddleOCR-VL: Tabellen  (Table Recognition:)": "Table Recognition:",
}


def _aenderungszeit(pfad: Path):
    try:
        return pfad.stat().st_mtime
    except OSError:
        return None


class OcrGui(tk.Tk):
    def __init__(self, startdateien):
        super().__init__()
        self.title("OCR mit Lemonade")
        self.minsize(760, 720)
        self.nachrichten = queue.Queue()
        self.abbruch = threading.Event()
        self.arbeiter = None
        self.letzter_ordner = None

        e = self._einstellungen_laden()
        self.var_url = tk.StringVar(value=e.get("url", ocr.BASE_URL))
        self.var_key = tk.StringVar(value=os.environ.get("LEMONADE_API_KEY", ""))
        self.var_modell = tk.StringVar(value=e.get("modell", ""))
        self.var_modus = tk.StringVar(value=e.get("modus", next(iter(MODI))))
        self.var_pdf = tk.BooleanVar(value=e.get("pdf", True))
        self.var_docx = tk.BooleanVar(value=e.get("docx", False))
        self.var_odt = tk.BooleanVar(value=e.get("odt", False))
        self.var_ziel_eigen = tk.BooleanVar(value=e.get("ziel_eigen", False))
        self.var_ziel = tk.StringVar(value=e.get("ziel", ""))
        self.var_eingabe_eigen = tk.BooleanVar(value=e.get("eingabe_eigen", False))
        self.var_eingabe = tk.StringVar(value=e.get("eingabe", ""))
        self.var_ueberwachen = tk.BooleanVar(value=e.get("ueberwachen", False))
        self.var_alle = tk.BooleanVar(value=False)   # bewusst nicht gespeichert
        self.var_status = tk.StringVar(value="Bereit.")

        self._oberflaeche_bauen()
        for d in startdateien:
            self._datei_hinzufuegen(d)
        self.protocol("WM_DELETE_WINDOW", self._schliessen)
        self.after(100, self._nachrichten_abholen)
        self.after(300, self.modelle_laden)

    # ------------------------------------------------------------ Aufbau ---
    def _oberflaeche_bauen(self):
        pad = dict(padx=8, pady=4)
        haupt = ttk.Frame(self, padding=10)
        haupt.pack(fill="both", expand=True)
        haupt.columnconfigure(0, weight=1)

        # Verbindung
        f = ttk.LabelFrame(haupt, text="Verbindung zu Lemonade", padding=8)
        f.grid(row=0, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Server-Adresse:").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.var_url).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(f, text="API-Key (optional):").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(f, textvariable=self.var_key, show="•").grid(row=1, column=1, sticky="ew", padx=6, pady=(4, 0))
        ttk.Label(f, text="Modell:").grid(row=2, column=0, sticky="w", pady=(4, 0))
        # eintippbar, damit ein Modellname auch ohne laufenden Server geht
        self.cb_modell = ttk.Combobox(f, textvariable=self.var_modell)
        self.cb_modell.grid(row=2, column=1, sticky="ew", padx=6, pady=(4, 0))
        ttk.Button(f, text="Modelle laden", command=self.modelle_laden).grid(row=2, column=2, pady=(4, 0))
        ttk.Label(f, text="Modus:").grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Combobox(f, textvariable=self.var_modus, values=list(MODI), state="readonly") \
            .grid(row=3, column=1, sticky="ew", padx=6, pady=(4, 0))

        # Dateien
        f = ttk.LabelFrame(haupt, text="Dateien", padding=8)
        f.grid(row=1, column=0, sticky="nsew", **pad)
        haupt.rowconfigure(1, weight=1)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)
        self.liste = tk.Listbox(f, selectmode="extended", height=6, activestyle="none")
        self.liste.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(f, orient="vertical", command=self.liste.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.liste.configure(yscrollcommand=sb.set)
        knoepfe = ttk.Frame(f)
        knoepfe.grid(row=0, column=2, sticky="n", padx=(8, 0))
        ttk.Button(knoepfe, text="Hinzufügen …", command=self.dateien_waehlen).pack(fill="x")
        ttk.Button(knoepfe, text="Entfernen", command=self.auswahl_entfernen).pack(fill="x", pady=4)
        ttk.Button(knoepfe, text="Liste leeren", command=lambda: self.liste.delete(0, "end")).pack(fill="x")
        self.liste.bind("<Delete>", lambda _e: self.auswahl_entfernen())

        # Eingabe-Ordner
        f = ttk.LabelFrame(haupt, text="Eingabe-Ordner (optional)", padding=8)
        f.grid(row=2, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Checkbutton(f, text="Eingabe-Ordner:", variable=self.var_eingabe_eigen,
                        command=self._eingabe_umschalten).grid(row=0, column=0, sticky="w")
        self.ent_eingabe = ttk.Entry(f, textvariable=self.var_eingabe)
        self.ent_eingabe.grid(row=0, column=1, sticky="ew", padx=6)
        self.btn_eingabe = ttk.Button(f, text="Wählen …", command=self.eingabe_waehlen)
        self.btn_eingabe.grid(row=0, column=2)
        zeile = ttk.Frame(f)
        zeile.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.chk_ueberwachen = ttk.Checkbutton(
            zeile, text="Ordner überwachen (neue Dateien automatisch verarbeiten)",
            variable=self.var_ueberwachen)
        self.chk_ueberwachen.pack(side="left", padx=(0, 12))
        self.chk_alle = ttk.Checkbutton(zeile, text="Bereits erledigte erneut verarbeiten",
                                        variable=self.var_alle)
        self.chk_alle.pack(side="left")
        ttk.Label(f, text="Verarbeitet alle PDFs und Bilder im Ordner. Schon erledigte werden übersprungen.",
                  foreground="#666").grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self._eingabe_umschalten()

        # Ausgabe
        f = ttk.LabelFrame(haupt, text="Ausgabe", padding=8)
        f.grid(row=3, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        zeile = ttk.Frame(f)
        zeile.grid(row=0, column=0, columnspan=3, sticky="w")
        self.var_md = tk.BooleanVar(value=True)
        ttk.Checkbutton(zeile, text="Markdown (immer)", state="disabled",
                        variable=self.var_md).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(zeile, text="Durchsuchbares PDF", variable=self.var_pdf).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(zeile, text="Word (.docx)", variable=self.var_docx).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(zeile, text="LibreOffice (.odt)", variable=self.var_odt).pack(side="left")
        ttk.Checkbutton(f, text="Ausgabe-Ordner:", variable=self.var_ziel_eigen,
                        command=self._ziel_umschalten).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.ent_ziel = ttk.Entry(f, textvariable=self.var_ziel)
        self.ent_ziel.grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        self.btn_ziel = ttk.Button(f, text="Wählen …", command=self.ziel_waehlen)
        self.btn_ziel.grid(row=1, column=2, pady=(6, 0))
        ttk.Label(f, text="Ohne Ausgabe-Ordner landen die Ergebnisse neben der Originaldatei.",
                  foreground="#666").grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self._ziel_umschalten()

        # Start / Fortschritt
        f = ttk.Frame(haupt)
        f.grid(row=4, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        self.balken = ttk.Progressbar(f, maximum=1000)
        self.balken.grid(row=0, column=0, sticky="ew")
        self.btn_start = ttk.Button(f, text="▶  Start", command=self.starten)
        self.btn_start.grid(row=0, column=1, padx=(8, 0))
        self.btn_abbruch = ttk.Button(f, text="Abbrechen", command=self.abbrechen, state="disabled")
        self.btn_abbruch.grid(row=0, column=2, padx=(4, 0))
        self.btn_ordner = ttk.Button(f, text="Ordner öffnen", command=self.ordner_oeffnen, state="disabled")
        self.btn_ordner.grid(row=0, column=3, padx=(4, 0))
        ttk.Label(f, textvariable=self.var_status).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # Protokoll
        f = ttk.LabelFrame(haupt, text="Protokoll", padding=4)
        f.grid(row=5, column=0, sticky="nsew", **pad)
        haupt.rowconfigure(5, weight=1)
        self.log = ScrolledText(f, height=9, state="disabled", font=("Consolas", 9), wrap="word")
        self.log.pack(fill="both", expand=True)

    # ------------------------------------------------------ Hilfsfunktionen ---
    def _protokoll(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _datei_hinzufuegen(self, pfad):
        pfad = str(Path(pfad))
        if pfad not in self.liste.get(0, "end"):
            self.liste.insert("end", pfad)

    def _ziel_umschalten(self):
        zustand = "normal" if self.var_ziel_eigen.get() else "disabled"
        self.ent_ziel.configure(state=zustand)
        self.btn_ziel.configure(state=zustand)

    def _eingabe_umschalten(self):
        zustand = "normal" if self.var_eingabe_eigen.get() else "disabled"
        for w in (self.ent_eingabe, self.btn_eingabe, self.chk_ueberwachen, self.chk_alle):
            w.configure(state=zustand)

    def _einstellungen_laden(self):
        try:
            return json.loads(EINSTELLUNGEN.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _einstellungen_speichern(self):
        daten = dict(url=self.var_url.get(), modell=self.var_modell.get(), modus=self.var_modus.get(),
                     pdf=self.var_pdf.get(), docx=self.var_docx.get(), odt=self.var_odt.get(),
                     ziel_eigen=self.var_ziel_eigen.get(), ziel=self.var_ziel.get(),
                     eingabe_eigen=self.var_eingabe_eigen.get(), eingabe=self.var_eingabe.get(),
                     ueberwachen=self.var_ueberwachen.get())
        try:  # API-Key wird bewusst nicht gespeichert
            EINSTELLUNGEN.write_text(json.dumps(daten, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _bedienung_sperren(self, laeuft):
        self.btn_start.configure(state="disabled" if laeuft else "normal")
        self.btn_abbruch.configure(state="normal" if laeuft else "disabled")

    # --------------------------------------------------------------- Aktionen ---
    def dateien_waehlen(self):
        for d in filedialog.askopenfilenames(title="Dateien auswählen", filetypes=DATEITYPEN):
            self._datei_hinzufuegen(d)

    def auswahl_entfernen(self):
        for i in reversed(self.liste.curselection()):
            self.liste.delete(i)

    def ziel_waehlen(self):
        ordner = filedialog.askdirectory(title="Zielordner wählen")
        if ordner:
            self.var_ziel.set(ordner)

    def eingabe_waehlen(self):
        ordner = filedialog.askdirectory(title="Eingabe-Ordner wählen")
        if ordner:
            self.var_eingabe.set(ordner)

    def ordner_oeffnen(self):
        if not self.letzter_ordner:
            return
        if sys.platform == "win32":
            os.startfile(self.letzter_ordner)
        else:
            subprocess.Popen(["xdg-open", str(self.letzter_ordner)])

    def modelle_laden(self):
        self.var_status.set("Verbinde mit Lemonade …")
        url, key = self.var_url.get().strip(), self.var_key.get().strip() or None

        def arbeit():
            try:
                ocr.verbinden(url, key)
                self.nachrichten.put(("modelle", ocr.modelle_auflisten()))
            except Exception as fehler:
                self.nachrichten.put(("verbindungsfehler", str(fehler)))
        threading.Thread(target=arbeit, daemon=True).start()

    def starten(self):
        dateien = list(self.liste.get(0, "end"))
        eingabe = Path(self.var_eingabe.get()) \
            if self.var_eingabe_eigen.get() and self.var_eingabe.get().strip() else None
        if eingabe and not eingabe.is_dir():
            messagebox.showerror("Eingabe-Ordner fehlt", f"Den Eingabe-Ordner gibt es nicht:\n\n{eingabe}")
            return
        if not dateien and not eingabe:
            messagebox.showinfo("Keine Dateien",
                                "Bitte zuerst Dateien hinzufügen oder einen Eingabe-Ordner wählen.")
            return
        if not self.var_modell.get():
            messagebox.showinfo("Kein Modell", "Bitte ein Modell auswählen (ggf. „Modelle laden“).")
            return
        formate = [f for f, v in (("docx", self.var_docx), ("odt", self.var_odt)) if v.get()]
        if formate and not shutil.which("pandoc"):
            if not messagebox.askyesno(
                    "Pandoc fehlt",
                    "Für Word/LibreOffice wird Pandoc benötigt:\n\nwinget install JohnMacFarlane.Pandoc\n\n"
                    "Trotzdem ohne Word/LibreOffice fortfahren?"):
                return
            formate = []
        ziel = Path(self.var_ziel.get()) if self.var_ziel_eigen.get() and self.var_ziel.get() else None
        einstellungen = dict(modell=self.var_modell.get(), prompt=MODI.get(self.var_modus.get(), "grounding"),
                             pdf=self.var_pdf.get(), formate=formate, ziel_ordner=ziel,
                             eingabe=eingabe, ueberwachen=bool(eingabe) and self.var_ueberwachen.get(),
                             alle=bool(eingabe) and self.var_alle.get())
        self._einstellungen_speichern()
        ocr.verbinden(self.var_url.get().strip(), self.var_key.get().strip() or None)

        self.abbruch.clear()
        self._bedienung_sperren(True)
        self.balken["value"] = 0
        self.arbeiter = threading.Thread(target=self._arbeiten, args=(dateien, einstellungen), daemon=True)
        self.arbeiter.start()

    def abbrechen(self):
        self.abbruch.set()
        self.var_status.set("Wird nach der aktuellen Seite abgebrochen …")
        self.btn_abbruch.configure(state="disabled")

    # --------------------------------------------------- Hintergrund-Thread ---
    def _datei(self, d: Path, e, i, n, **kwargs) -> bool:
        """Eine Datei verarbeiten. True bei Erfolg; Abgebrochen wird durchgereicht."""
        def fortschritt(seite, gesamt):
            self.nachrichten.put(("fortschritt", (i + seite / max(gesamt, 1)) / n,
                                  f"Datei {i + 1}/{n}: {d.name} – Seite {min(seite + 1, gesamt)}/{gesamt}"))
        try:
            erzeugt = ocr.verarbeiten(
                e["modell"], d, e["prompt"], pdf=e["pdf"], formate=e["formate"],
                ziel_ordner=e["ziel_ordner"], fortschritt=fortschritt,
                melden=lambda t: self.nachrichten.put(("log", t)),
                abbrechen=self.abbruch.is_set, **kwargs)
            if erzeugt:
                self.nachrichten.put(("ordner", erzeugt[0].parent))
            return True
        except ocr.Abgebrochen:
            raise
        except ocr.SeitenFehler as f:
            self.nachrichten.put(("log", f"✖ {d.name}: {f} – der Rest wurde gespeichert."))
            if f.erzeugt:
                self.nachrichten.put(("ordner", f.erzeugt[0].parent))
        except Exception as f:
            self.nachrichten.put(("log", f"✖ Fehler bei {d.name}: {f}"))
            self.nachrichten.put(("log", traceback.format_exc(limit=2)))
        return False

    def _arbeiten(self, dateien, e):
        stand = dict(ok=0, fehler=[])
        try:
            belegt = set()   # verhindert, dass sich gleichnamige Dateien überschreiben
            for i, d in enumerate(dateien):
                self._zaehlen(stand, Path(d), self._datei(Path(d), e, i, len(dateien), belegt=belegt))
            if e["eingabe"]:
                self._ordner_abarbeiten(e, stand)
        except ocr.Abgebrochen:
            self.nachrichten.put(("log", "⏹ Abgebrochen."))
        except Exception as f:   # z. B. Eingabe-Ordner während der Überwachung gelöscht
            self.nachrichten.put(("log", f"✖ Fehler: {f}"))
        self.nachrichten.put(("fertig", stand["ok"], stand["fehler"], self.abbruch.is_set()))

    @staticmethod
    def _zaehlen(stand, d, erfolg):
        if erfolg:
            stand["ok"] += 1
        else:
            stand["fehler"].append(d.name)

    def _ordner_abarbeiten(self, e, stand):
        """Alles Offene im Eingabe-Ordner verarbeiten; beim Überwachen danach
        regelmäßig nach neuen Dateien schauen, bis „Abbrechen“ gedrückt wird."""
        ordner = e["eingabe"]
        wartezimmer, alle = None, e["alle"]
        fehlgeschlagen = {}   # Datei -> Änderungszeit beim Fehlschlag (nicht endlos wiederholen)
        if e["ueberwachen"]:
            self.nachrichten.put(("log", f"Überwache {ordner} …"))
        while True:
            offen = [(d, n) for d, n in ocr.offene_dateien(ordner, e["ziel_ordner"], alle, wartezimmer)
                     if fehlgeschlagen.get(d) != _aenderungszeit(d)]
            for i, (d, name) in enumerate(offen):
                if self.abbruch.is_set():
                    raise ocr.Abgebrochen()
                zeit = _aenderungszeit(d)
                erfolg = self._datei(d, e, i, len(offen), name=name)
                self._zaehlen(stand, d, erfolg)
                if not erfolg:
                    fehlgeschlagen[d] = zeit
            if not e["ueberwachen"]:
                return
            alle, wartezimmer = False, wartezimmer or ocr.Wartezimmer()
            self.nachrichten.put(("status", f"Überwache {ordner} – bisher {stand['ok']} Datei(en) "
                                            "verarbeitet. Beenden mit „Abbrechen“."))
            if self.abbruch.wait(INTERVALL):
                raise ocr.Abgebrochen()

    def _nachrichten_abholen(self):
        try:
            while True:
                art, *daten = self.nachrichten.get_nowait()
                if art == "log":
                    self._protokoll(daten[0])
                elif art == "fortschritt":
                    self.balken["value"] = daten[0] * 1000
                    self.var_status.set(daten[1])
                elif art == "status":
                    self.var_status.set(daten[0])
                elif art == "ordner":
                    self.letzter_ordner = daten[0]
                    self.btn_ordner.configure(state="normal")
                elif art == "modelle":
                    modelle = daten[0]
                    self.cb_modell["values"] = modelle
                    if self.var_modell.get() not in modelle:
                        ocr_modelle = [m for m in modelle if "ocr" in m.lower()]
                        self.var_modell.set((ocr_modelle or modelle or [""])[0])
                    self.var_status.set(f"Verbunden – {len(modelle)} Modelle gefunden.")
                elif art == "verbindungsfehler":
                    self.var_status.set("Keine Verbindung zu Lemonade.")
                    self._protokoll(f"✖ Verbindung fehlgeschlagen: {daten[0]}\n"
                                    "  Läuft die Lemonade-App? Stimmt der Port (lemonade config)?")
                elif art == "fertig":
                    ok, fehler, abgebrochen = daten
                    self._bedienung_sperren(False)
                    if not abgebrochen and not fehler:
                        self.balken["value"] = 1000
                    text = f"Fertig: {ok} Datei(en) verarbeitet"
                    if fehler:
                        text += f", {len(fehler)} mit Fehler"
                    self.var_status.set(text + ("  (abgebrochen)" if abgebrochen else "") + ".")
                    self._protokoll("— " + self.var_status.get())
                    if fehler:
                        messagebox.showwarning("Fehler", "Folgende Dateien konnten nicht verarbeitet werden:\n\n"
                                               + "\n".join(fehler) + "\n\nDetails im Protokoll.")
        except queue.Empty:
            pass
        self.after(100, self._nachrichten_abholen)

    def _schliessen(self):
        if self.arbeiter and self.arbeiter.is_alive():
            if not messagebox.askyesno("Beenden?", "Die Verarbeitung läuft noch. Trotzdem beenden?"):
                return
        self._einstellungen_speichern()
        self.destroy()


if __name__ == "__main__":
    if sys.platform == "win32":
        try:  # scharfe Darstellung auf hochauflösenden Bildschirmen
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    app = OcrGui([a for a in sys.argv[1:] if Path(a).is_file()])
    app.mainloop()
