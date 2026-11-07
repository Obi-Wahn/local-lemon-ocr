"""
Headless-Test der Qt-Oberfläche (PySide6). Läuft ohne Bildschirm über die
Qt-Plattform „offscreen“; ohne PySide6 wird der Test übersprungen.

Aufruf:   python -m unittest discover -s tests -v
"""
import importlib.util
import os
import shutil
import tempfile
import threading
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

import testseite as t
from testseite import ocr

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None


def gui_laden():
    pfad = Path(__file__).resolve().parent.parent / "ocr_gui.pyw"
    loader = SourceFileLoader("ocr_gui", str(pfad))
    spec = importlib.util.spec_from_loader("ocr_gui", loader)
    modul = importlib.util.module_from_spec(spec)
    loader.exec_module(modul)
    return modul


@unittest.skipUnless(QApplication, "PySide6 nicht installiert")
class GuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.gui = gui_laden()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr-gui-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.original = t.original_erzeugen()
        antwort = t.fake_antwort(self.original)
        for ziel, ersatz in ((ocr, dict(ocr=lambda m, p, pr, empfangen=None: antwort,
                                        modelle_auflisten=lambda: ["user.LightOnOCR-3-4B-GGUF-Q8_0"])),
                             (self.gui, dict(EINSTELLUNGEN=self.tmp / "einstellungen.json",
                                             INTERVALL=0.2))):
            for name, wert in ersatz.items():
                p = mock.patch.object(ziel, name, wert)
                p.start()
                self.addCleanup(p.stop)
        mb = mock.patch.object(self.gui, "QMessageBox")   # keine modalen Dialoge im Test
        self.messagebox = mb.start()
        self.addCleanup(mb.stop)
        self.app = self.gui.OcrGui([])
        self.app.show()
        self.addCleanup(self.schliessen)
        self.warten(lambda: self.app.cb_modell.currentText())

    def schliessen(self):
        self.app.abbruch.set()
        if self.app.arbeiter:
            self.app.arbeiter.join(10)
        self.app.takt_nachrichten.stop()
        self.app.takt_anzeige.stop()
        self.app.deleteLater()
        self.qt.processEvents()

    def warten(self, bedingung, sekunden=30):
        ende = time.time() + sekunden
        while time.time() < ende:
            self.qt.processEvents()
            if bedingung():
                return
            time.sleep(0.05)
        self.fail("Zeitüberschreitung beim Warten auf die Oberfläche")

    def fertig(self):
        return not self.app.laeuft() and self.app.btn_start.isEnabled()

    def test_modell_wird_gewaehlt_und_ist_eintippbar(self):
        self.assertEqual(self.app.cb_modell.currentText(), "user.LightOnOCR-3-4B-GGUF-Q8_0")
        self.assertTrue(self.app.cb_modell.isEditable())

    def test_eingabe_und_ausgabe_ordner(self):
        ein, aus = self.tmp / "Eingang", self.tmp / "Ergebnisse"
        ein.mkdir()
        t.scan_erzeugen(self.original, ein / "Erstes.pdf")
        t.scan_erzeugen(self.original, ein / "Zweites.pdf")
        self.app.chk_eingabe.setChecked(True)
        self.app.ed_eingabe.setText(str(ein))
        self.app.chk_ziel.setChecked(True)
        self.app.ed_ziel.setText(str(aus))
        self.app.chk_pdf.setChecked(True)
        self.app.starten()
        self.warten(self.fertig)
        self.assertEqual(sorted(p.name for p in aus.glob("*.ocr.pdf")),
                         ["Erstes.ocr.pdf", "Zweites.ocr.pdf"])
        self.assertIn("Fertig: 2 Datei(en)", self.app.lbl_status.text())
        self.assertIn("fertig nach", self.app.log.toPlainText())

    def test_ordner_ueberwachen(self):
        ein, aus = self.tmp / "Eingang", self.tmp / "Ergebnisse"
        ein.mkdir()
        self.app.chk_eingabe.setChecked(True)
        self.app.ed_eingabe.setText(str(ein))
        self.app.chk_ueberwachen.setChecked(True)
        self.app.chk_ziel.setChecked(True)
        self.app.ed_ziel.setText(str(aus))
        self.app.starten()
        self.warten(lambda: "Überwache" in self.app.lbl_status.text())
        t.scan_erzeugen(self.original, ein / "Neu.pdf")
        self.warten(lambda: (aus / "Neu.ocr.md").exists())
        self.app.abbrechen()
        self.warten(self.fertig)
        self.assertIn("abgebrochen", self.app.lbl_status.text())

    def test_live_fortschritt_waehrend_einer_seite(self):
        weiter = threading.Event()
        antwort = t.fake_antwort(self.original)

        def langsame_ocr(modell, png, prompt, empfangen=None):
            empfangen(1234)
            weiter.wait(20)           # Seite „dauert“, bis der Test weitermacht
            return antwort

        self.app._datei_hinzufuegen(t.scan_erzeugen(self.original, self.tmp / "Beispiel.pdf"))
        with mock.patch.object(ocr, "ocr", langsame_ocr):
            self.app.starten()
            self.warten(lambda: "1234 Zeichen" in self.app.lbl_status.text())
            status = self.app.lbl_status.text()
            weiter.set()
            self.warten(self.fertig)
        self.assertIn("Datei 1/1: Beispiel.pdf – Seite 1/1", status)
        self.assertIn("Fertig: 1 Datei(en)", self.app.lbl_status.text())
        self.assertEqual(self.app.balken.value(), 1000)

    def test_ohne_markdown(self):
        self.app._datei_hinzufuegen(t.scan_erzeugen(self.original, self.tmp / "Beispiel.pdf"))
        self.app.chk_md.setChecked(False)
        self.app.chk_pdf.setChecked(True)
        self.app.starten()
        self.warten(self.fertig)
        self.assertTrue((self.tmp / "Beispiel.ocr.pdf").exists())
        self.assertFalse((self.tmp / "Beispiel.ocr.md").exists())

    def test_ohne_jede_ausgabe_wird_nicht_gestartet(self):
        self.app._datei_hinzufuegen(t.scan_erzeugen(self.original, self.tmp / "Beispiel.pdf"))
        for w in (self.app.chk_md, self.app.chk_pdf, self.app.chk_docx, self.app.chk_odt):
            w.setChecked(False)
        self.app.starten()
        self.assertFalse(self.app.laeuft())
        self.messagebox.information.assert_called()

    def test_reihenfolge_in_der_liste(self):
        for name in ("A.pdf", "B.pdf", "C.pdf"):
            self.app._datei_hinzufuegen(self.tmp / name)
        self.app.liste.setCurrentRow(2)
        self.gui._eintrag_verschieben(self.app.liste, -1)
        self.gui._eintrag_verschieben(self.app.liste, -1)
        self.gui._eintrag_verschieben(self.app.liste, -1)   # oben angekommen, bleibt
        self.assertEqual([Path(d).name for d in self.app.dateien()], ["C.pdf", "A.pdf", "B.pdf"])

    def test_reihenfolge_dialog(self):
        dialog = self.gui.ReihenfolgeDialog([self.tmp / "B.pdf", self.tmp / "A.pdf", self.tmp / "C.png"],
                                            "B-zusammen")
        dialog.liste.setCurrentRow(2)
        self.gui._eintrag_verschieben(dialog.liste, -1)
        self.assertEqual([d.name for d in dialog.dateien()], ["B.pdf", "C.png", "A.pdf"])
        dialog.sortieren()
        self.assertEqual([d.name for d in dialog.dateien()], ["A.pdf", "B.pdf", "C.png"])
        dialog.ed_name.setText("Gesamt.pdf")
        self.assertEqual(dialog.name(), "Gesamt")
        dialog.deleteLater()

    def test_zusammenfuegen(self):
        ein, aus = self.tmp / "Eingang", self.tmp / "Ergebnisse"
        ein.mkdir()
        t.scan_erzeugen(self.original, ein / "Erstes.pdf")
        t.scan_erzeugen(self.original, ein / "Zweites.pdf")
        self.app._datei_hinzufuegen(t.scan_erzeugen(self.original, self.tmp / "Deckblatt.pdf"))
        self.app.chk_eingabe.setChecked(True)
        self.app.ed_eingabe.setText(str(ein))
        self.app.chk_ziel.setChecked(True)
        self.app.ed_ziel.setText(str(aus))
        self.app.chk_pdf.setChecked(True)
        self.app.chk_md.setChecked(False)
        self.app.chk_zusammen.setChecked(True)
        gefragt = []

        def abfragen(dateien, ziel):
            gefragt.append([d.name for d in dateien])
            return [dateien[2], dateien[0], dateien[1]], "Gesamt"   # Zweites, Deckblatt, Erstes

        with mock.patch.object(self.app, "_reihenfolge_abfragen", abfragen):
            self.app.starten()
            self.warten(self.fertig)
        self.assertEqual(gefragt, [["Deckblatt.pdf", "Erstes.pdf", "Zweites.pdf"]])
        self.assertEqual(sorted(p.name for p in aus.iterdir() if p.is_file()), ["Gesamt.ocr.pdf"])
        self.assertEqual(len(ocr.fitz.open(aus / "Gesamt.ocr.pdf")), 3)
        self.assertIn("Fertig: 1 Datei(en)", self.app.lbl_status.text())
        self.assertIn("Zweites.pdf, Deckblatt.pdf, Erstes.pdf", self.app.log.toPlainText())

    def test_zusammenfuegen_beim_ueberwachen_gesperrt(self):
        self.app.chk_eingabe.setChecked(True)
        self.app.chk_ueberwachen.setChecked(True)
        self.assertFalse(self.app.chk_zusammen.isEnabled())
        self.app.chk_ueberwachen.setChecked(False)
        self.assertTrue(self.app.chk_zusammen.isEnabled())

    def test_einstellungen_ohne_api_key(self):
        self.app.ed_key.setText("geheim")
        self.app.ed_eingabe.setText(str(self.tmp))
        self.app._einstellungen_speichern()
        text = (self.tmp / "einstellungen.json").read_text(encoding="utf-8")
        self.assertNotIn("geheim", text)
        self.assertIn("eingabe", text)


if __name__ == "__main__":
    unittest.main()
