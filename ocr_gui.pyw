"""
Grafische Oberfläche für ocr.py – Texterkennung über einen lokalen Lemonade Server.

Start:   Doppelklick auf ocr_gui.pyw   (ocr.py muss im selben Ordner liegen)
         oder:  pythonw ocr_gui.pyw   bzw. unter Linux:  python3 ocr_gui.pyw
Tipp:    Dateien lassen sich direkt ins Fenster oder auf das Programmsymbol ziehen.
         Mit Eingabe- und Ausgabe-Ordner (optional mit Überwachung) wird alles
         verarbeitet, was im Eingabe-Ordner liegt bzw. neu hinzukommt.

Benötigt: pip install openai pymupdf PySide6    (Pandoc nur für Word/LibreOffice)
Die Oberfläche nutzt Qt (PySide6) und läuft unter Linux nativ auf Wayland,
unter X11 und unter Windows.
"""
import json
import os
import queue
import shutil
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ocr  # noqa: E402  (ocr.py aus demselben Ordner)

EINSTELLUNGEN = Path.home() / ".ocr-gui.json"
_BILDER = " ".join("*" + e for e in ocr.ENDUNGEN if e != ".pdf")
DATEIFILTER = f"PDF und Bilder (*.pdf {_BILDER});;PDF (*.pdf);;Bilder ({_BILDER});;Alle Dateien (*)"
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


def _hinweis(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setEnabled(False)          # grau, wie ein Hinweistext
    return label


class OcrGui(QWidget):
    def __init__(self, startdateien=()):
        super().__init__()
        self.setWindowTitle("OCR mit Lemonade")
        self.setAcceptDrops(True)
        self.resize(820, 860)
        self.nachrichten = queue.Queue()
        self.abbruch = threading.Event()
        self.arbeiter = None
        self.letzter_ordner = None
        # Live-Fortschritt der laufenden Seite; bleibt über mehrere Starts erhalten,
        # damit die Restzeit-Schätzung die bisherigen Seitendauern kennt
        self.anzeige = ocr.Fortschritt()
        self.position = None   # (Index der Datei, Anzahl Dateien) für den Gesamtbalken

        self._oberflaeche_bauen()
        self._einstellungen_anwenden(self._einstellungen_laden())
        for d in startdateien:
            self._datei_hinzufuegen(d)

        self.takt_nachrichten = QTimer(self, interval=100, timeout=self._nachrichten_abholen)
        self.takt_nachrichten.start()
        self.takt_anzeige = QTimer(self, interval=500, timeout=self._live_anzeige)
        self.takt_anzeige.start()
        QTimer.singleShot(300, self.modelle_laden)

    # ------------------------------------------------------------ Aufbau ---
    def _oberflaeche_bauen(self):
        haupt = QVBoxLayout(self)

        # Verbindung
        gruppe = QGroupBox("Verbindung zu Lemonade")
        form = QFormLayout(gruppe)
        self.ed_url = QLineEdit()
        form.addRow("Server-Adresse:", self.ed_url)
        self.ed_key = QLineEdit(os.environ.get("LEMONADE_API_KEY", ""))
        self.ed_key.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API-Key (optional):", self.ed_key)
        zeile = QHBoxLayout()
        # eintippbar, damit ein Modellname auch ohne laufenden Server geht
        self.cb_modell = QComboBox(editable=True)
        self.cb_modell.setSizePolicy(self.ed_url.sizePolicy())
        zeile.addWidget(self.cb_modell, 1)
        knopf = QPushButton("Modelle laden")
        knopf.clicked.connect(self.modelle_laden)
        zeile.addWidget(knopf)
        form.addRow("Modell:", zeile)
        self.cb_modus = QComboBox()
        self.cb_modus.addItems(list(MODI))
        form.addRow("Modus:", self.cb_modus)
        haupt.addWidget(gruppe)

        # Dateien
        gruppe = QGroupBox("Dateien (auch per Drag && Drop)")
        zeile = QHBoxLayout(gruppe)
        self.liste = QListWidget()
        self.liste.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        zeile.addWidget(self.liste, 1)
        knoepfe = QVBoxLayout()
        for text, aktion in (("Hinzufügen …", self.dateien_waehlen), ("Entfernen", self.auswahl_entfernen),
                             ("Liste leeren", self.liste.clear)):
            k = QPushButton(text)
            k.clicked.connect(aktion)
            knoepfe.addWidget(k)
        knoepfe.addStretch()
        zeile.addLayout(knoepfe)
        QShortcut(QKeySequence.StandardKey.Delete, self.liste, activated=self.auswahl_entfernen)
        haupt.addWidget(gruppe, 1)

        # Eingabe-Ordner
        gruppe = QGroupBox("Eingabe-Ordner (optional)")
        raster = QGridLayout(gruppe)
        self.chk_eingabe = QCheckBox("Eingabe-Ordner:")
        self.ed_eingabe = QLineEdit()
        self.btn_eingabe = QPushButton("Wählen …")
        self.btn_eingabe.clicked.connect(lambda: self._ordner_waehlen(self.ed_eingabe, "Eingabe-Ordner wählen"))
        raster.addWidget(self.chk_eingabe, 0, 0)
        raster.addWidget(self.ed_eingabe, 0, 1)
        raster.addWidget(self.btn_eingabe, 0, 2)
        zeile = QHBoxLayout()
        self.chk_ueberwachen = QCheckBox("Ordner überwachen (neue Dateien automatisch verarbeiten)")
        self.chk_alle = QCheckBox("Bereits erledigte erneut verarbeiten")   # bewusst nicht gespeichert
        zeile.addWidget(self.chk_ueberwachen)
        zeile.addWidget(self.chk_alle)
        zeile.addStretch()
        raster.addLayout(zeile, 1, 0, 1, 3)
        raster.addWidget(_hinweis("Verarbeitet alle PDFs und Bilder im Ordner. "
                                  "Schon erledigte werden übersprungen."), 2, 0, 1, 3)
        self.chk_eingabe.toggled.connect(self._umschalten)
        haupt.addWidget(gruppe)

        # Ausgabe
        gruppe = QGroupBox("Ausgabe")
        raster = QGridLayout(gruppe)
        zeile = QHBoxLayout()
        md = QCheckBox("Markdown (immer)", checked=True, enabled=False)
        self.chk_pdf = QCheckBox("Durchsuchbares PDF")
        self.chk_docx = QCheckBox("Word (.docx)")
        self.chk_odt = QCheckBox("LibreOffice (.odt)")
        for w in (md, self.chk_pdf, self.chk_docx, self.chk_odt):
            zeile.addWidget(w)
        zeile.addStretch()
        raster.addLayout(zeile, 0, 0, 1, 3)
        self.chk_ziel = QCheckBox("Ausgabe-Ordner:")
        self.ed_ziel = QLineEdit()
        self.btn_ziel = QPushButton("Wählen …")
        self.btn_ziel.clicked.connect(lambda: self._ordner_waehlen(self.ed_ziel, "Ausgabe-Ordner wählen"))
        raster.addWidget(self.chk_ziel, 1, 0)
        raster.addWidget(self.ed_ziel, 1, 1)
        raster.addWidget(self.btn_ziel, 1, 2)
        raster.addWidget(_hinweis("Ohne Ausgabe-Ordner landen die Ergebnisse neben der Originaldatei."),
                         2, 0, 1, 3)
        self.chk_ziel.toggled.connect(self._umschalten)
        haupt.addWidget(gruppe)

        # Start / Fortschritt
        zeile = QHBoxLayout()
        self.balken = QProgressBar(minimum=0, maximum=1000, value=0)
        self.balken.setFormat("%p %")
        zeile.addWidget(self.balken, 1)
        self.btn_start = QPushButton("▶  Start")
        self.btn_start.clicked.connect(self.starten)
        self.btn_abbruch = QPushButton("Abbrechen", enabled=False)
        self.btn_abbruch.clicked.connect(self.abbrechen)
        self.btn_ordner = QPushButton("Ordner öffnen", enabled=False)
        self.btn_ordner.clicked.connect(self.ordner_oeffnen)
        for k in (self.btn_start, self.btn_abbruch, self.btn_ordner):
            zeile.addWidget(k)
        haupt.addLayout(zeile)
        self.lbl_status = QLabel("Bereit.")
        self.lbl_status.setWordWrap(True)
        haupt.addWidget(self.lbl_status)

        # Protokoll
        gruppe = QGroupBox("Protokoll")
        box = QVBoxLayout(gruppe)
        self.log = QPlainTextEdit(readOnly=True)
        schrift = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        schrift.setPointSize(max(8, QFont().pointSize() - 1))
        self.log.setFont(schrift)
        box.addWidget(self.log)
        haupt.addWidget(gruppe, 1)

    # ------------------------------------------------------ Hilfsfunktionen ---
    def _protokoll(self, text):
        self.log.appendPlainText(text)

    def _datei_hinzufuegen(self, pfad):
        pfad = str(Path(pfad))
        vorhanden = {self.liste.item(i).text() for i in range(self.liste.count())}
        if pfad not in vorhanden:
            self.liste.addItem(pfad)

    def dateien(self) -> list[str]:
        return [self.liste.item(i).text() for i in range(self.liste.count())]

    def _umschalten(self):
        an = self.chk_eingabe.isChecked()
        for w in (self.ed_eingabe, self.btn_eingabe, self.chk_ueberwachen, self.chk_alle):
            w.setEnabled(an)
        an = self.chk_ziel.isChecked()
        for w in (self.ed_ziel, self.btn_ziel):
            w.setEnabled(an)

    def _einstellungen_laden(self):
        try:
            return json.loads(EINSTELLUNGEN.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _einstellungen_anwenden(self, e):
        self.ed_url.setText(e.get("url", ocr.BASE_URL))
        self.cb_modell.setCurrentText(e.get("modell", ""))
        if e.get("modus") in MODI:
            self.cb_modus.setCurrentText(e["modus"])
        self.chk_pdf.setChecked(e.get("pdf", True))
        self.chk_docx.setChecked(e.get("docx", False))
        self.chk_odt.setChecked(e.get("odt", False))
        self.chk_ziel.setChecked(e.get("ziel_eigen", False))
        self.ed_ziel.setText(e.get("ziel", ""))
        self.chk_eingabe.setChecked(e.get("eingabe_eigen", False))
        self.ed_eingabe.setText(e.get("eingabe", ""))
        self.chk_ueberwachen.setChecked(e.get("ueberwachen", False))
        self._umschalten()

    def _einstellungen_speichern(self):
        daten = dict(url=self.ed_url.text(), modell=self.cb_modell.currentText(),
                     modus=self.cb_modus.currentText(), pdf=self.chk_pdf.isChecked(),
                     docx=self.chk_docx.isChecked(), odt=self.chk_odt.isChecked(),
                     ziel_eigen=self.chk_ziel.isChecked(), ziel=self.ed_ziel.text(),
                     eingabe_eigen=self.chk_eingabe.isChecked(), eingabe=self.ed_eingabe.text(),
                     ueberwachen=self.chk_ueberwachen.isChecked())
        try:  # API-Key wird bewusst nicht gespeichert
            EINSTELLUNGEN.write_text(json.dumps(daten, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _bedienung_sperren(self, laeuft):
        self.btn_start.setEnabled(not laeuft)
        self.btn_abbruch.setEnabled(laeuft)

    def laeuft(self) -> bool:
        return self.arbeiter is not None and self.arbeiter.is_alive()

    # ------------------------------------------------------- Drag & Drop ---
    def dragEnterEvent(self, ereignis):
        if ereignis.mimeData().hasUrls():
            ereignis.acceptProposedAction()

    def dropEvent(self, ereignis):
        for url in ereignis.mimeData().urls():
            pfad = Path(url.toLocalFile())
            if pfad.is_dir():
                self.chk_eingabe.setChecked(True)
                self.ed_eingabe.setText(str(pfad))
            elif pfad.is_file():
                self._datei_hinzufuegen(pfad)

    # --------------------------------------------------------------- Aktionen ---
    def dateien_waehlen(self):
        dateien, _ = QFileDialog.getOpenFileNames(self, "Dateien auswählen", "", DATEIFILTER)
        for d in dateien:
            self._datei_hinzufuegen(d)

    def auswahl_entfernen(self):
        for eintrag in self.liste.selectedItems():
            self.liste.takeItem(self.liste.row(eintrag))

    def _ordner_waehlen(self, feld: QLineEdit, titel: str):
        ordner = QFileDialog.getExistingDirectory(self, titel, feld.text())
        if ordner:
            feld.setText(ordner)

    def ordner_oeffnen(self):
        if self.letzter_ordner:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.letzter_ordner)))

    def modelle_laden(self):
        self.lbl_status.setText("Verbinde mit Lemonade …")
        url, key = self.ed_url.text().strip(), self.ed_key.text().strip() or None

        def arbeit():
            try:
                ocr.verbinden(url, key)
                self.nachrichten.put(("modelle", ocr.modelle_auflisten()))
            except Exception as fehler:
                self.nachrichten.put(("verbindungsfehler", str(fehler)))
        threading.Thread(target=arbeit, daemon=True).start()

    def starten(self):
        dateien = self.dateien()
        eingabe = Path(self.ed_eingabe.text()) \
            if self.chk_eingabe.isChecked() and self.ed_eingabe.text().strip() else None
        if eingabe and not eingabe.is_dir():
            QMessageBox.critical(self, "Eingabe-Ordner fehlt", f"Den Eingabe-Ordner gibt es nicht:\n\n{eingabe}")
            return
        if not dateien and not eingabe:
            QMessageBox.information(self, "Keine Dateien",
                                    "Bitte zuerst Dateien hinzufügen oder einen Eingabe-Ordner wählen.")
            return
        modell = self.cb_modell.currentText().strip()
        if not modell:
            QMessageBox.information(self, "Kein Modell", "Bitte ein Modell auswählen (ggf. „Modelle laden“).")
            return
        formate = [f for f, w in (("docx", self.chk_docx), ("odt", self.chk_odt)) if w.isChecked()]
        if formate and not shutil.which("pandoc"):
            antwort = QMessageBox.question(
                self, "Pandoc fehlt",
                "Für Word/LibreOffice wird Pandoc benötigt:\n\n"
                "Windows: winget install JohnMacFarlane.Pandoc\nFedora: sudo dnf install pandoc\n\n"
                "Trotzdem ohne Word/LibreOffice fortfahren?")
            if antwort != QMessageBox.StandardButton.Yes:
                return
            formate = []
        ziel = Path(self.ed_ziel.text()) if self.chk_ziel.isChecked() and self.ed_ziel.text().strip() else None
        einstellungen = dict(modell=modell, prompt=MODI.get(self.cb_modus.currentText(), "grounding"),
                             pdf=self.chk_pdf.isChecked(), formate=formate, ziel_ordner=ziel,
                             eingabe=eingabe, ueberwachen=bool(eingabe) and self.chk_ueberwachen.isChecked(),
                             alle=bool(eingabe) and self.chk_alle.isChecked())
        self._einstellungen_speichern()
        ocr.verbinden(self.ed_url.text().strip(), self.ed_key.text().strip() or None)

        self.abbruch.clear()
        self._bedienung_sperren(True)
        self.balken.setValue(0)
        self.balken.setFormat("%p %")
        self.arbeiter = threading.Thread(target=self._arbeiten, args=(dateien, einstellungen), daemon=True)
        self.arbeiter.start()

    def abbrechen(self):
        self.abbruch.set()
        self.lbl_status.setText("Wird nach der aktuellen Seite abgebrochen …")
        self.btn_abbruch.setEnabled(False)

    # --------------------------------------------------- Hintergrund-Thread ---
    def _datei(self, d: Path, e, i, n, **kwargs) -> bool:
        """Eine Datei verarbeiten. True bei Erfolg; Abgebrochen wird durchgereicht."""
        self.position = (i, n)
        try:
            erzeugt = ocr.verarbeiten(
                e["modell"], d, e["prompt"], pdf=e["pdf"], formate=e["formate"],
                ziel_ordner=e["ziel_ordner"], anzeige=self.anzeige,
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

    # ------------------------------------------------------- Anzeige (Takt) ---
    def _live_anzeige(self):
        """Zweimal pro Sekunde Balken und Statuszeile aus dem Stand der laufenden
        Seite aktualisieren – so sieht man auch während einer langen Seite, dass
        das Programm arbeitet und wie weit es ist."""
        if not (self.anzeige.aktiv and self.position):
            return
        i, n = self.position
        st = self.anzeige.stand()
        self.balken.setValue(int((i + st["anteil"]) / max(n, 1) * 1000))
        self.balken.setFormat("%p %" + (f" · noch ca. {ocr.dauer_text(st['rest'])}"
                                        if st["rest"] is not None and n == 1 else ""))
        text = f"Datei {i + 1}/{n}: {st['datei']} – {self.anzeige.text()}"
        if self.abbruch.is_set():
            text = "Wird nach der aktuellen Seite abgebrochen … " + text
        self.lbl_status.setText(text)

    def _nachrichten_abholen(self):
        try:
            while True:
                art, *daten = self.nachrichten.get_nowait()
                if art == "log":
                    self._protokoll(daten[0])
                elif art == "status":
                    self.lbl_status.setText(daten[0])
                elif art == "ordner":
                    self.letzter_ordner = daten[0]
                    self.btn_ordner.setEnabled(True)
                elif art == "modelle":
                    modelle = daten[0]
                    gewaehlt = self.cb_modell.currentText()
                    self.cb_modell.clear()
                    self.cb_modell.addItems(modelle)
                    if gewaehlt not in modelle:
                        ocr_modelle = [m for m in modelle if "ocr" in m.lower()]
                        gewaehlt = (ocr_modelle or modelle or [gewaehlt])[0]
                    self.cb_modell.setCurrentText(gewaehlt)
                    self.lbl_status.setText(f"Verbunden – {len(modelle)} Modelle gefunden.")
                elif art == "verbindungsfehler":
                    self.lbl_status.setText("Keine Verbindung zu Lemonade.")
                    self._protokoll(f"✖ Verbindung fehlgeschlagen: {daten[0]}\n"
                                    "  Läuft Lemonade (unter Linux der Dienst lemond)? "
                                    "Stimmt der Port (lemonade config)?")
                elif art == "fertig":
                    self._fertig(*daten)
        except queue.Empty:
            pass

    def _fertig(self, ok, fehler, abgebrochen):
        self._bedienung_sperren(False)
        self.balken.setFormat("%p %")
        if not abgebrochen and not fehler:
            self.balken.setValue(1000)
        text = f"Fertig: {ok} Datei(en) verarbeitet"
        if fehler:
            text += f", {len(fehler)} mit Fehler"
        text += ("  (abgebrochen)" if abgebrochen else "") + "."
        self.lbl_status.setText(text)
        self._protokoll("— " + text)
        if fehler:
            QMessageBox.warning(self, "Fehler", "Folgende Dateien konnten nicht verarbeitet werden:\n\n"
                                + "\n".join(fehler) + "\n\nDetails im Protokoll.")

    def closeEvent(self, ereignis):
        if self.laeuft():
            antwort = QMessageBox.question(self, "Beenden?", "Die Verarbeitung läuft noch. Trotzdem beenden?")
            if antwort != QMessageBox.StandardButton.Yes:
                ereignis.ignore()
                return
            self.abbruch.set()
        self._einstellungen_speichern()
        ereignis.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("OCR mit Lemonade")
    # Bewusst kein setDesktopFileName(): Ohne passende, installierte .desktop-Datei
    # meldet das XDG-Portal unter KDE/GNOME sonst „App info not found“.
    fenster = OcrGui([a for a in sys.argv[1:] if Path(a).is_file()])
    fenster.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
