"""
Headless-Test der Oberfläche: braucht ein Python mit tkinter und ein Display,
unter Linux z. B.:   xvfb-run python -m unittest discover -s tests -v
Ohne Display wird der Test übersprungen.
"""
import importlib.util
import os
import shutil
import tempfile
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

import testseite as t
from testseite import ocr

try:
    import tkinter
    ANZEIGE = bool(os.environ.get("DISPLAY")) or os.name == "nt"
except ImportError:
    tkinter, ANZEIGE = None, False


def gui_laden():
    pfad = Path(__file__).resolve().parent.parent / "ocr_gui.pyw"
    loader = SourceFileLoader("ocr_gui", str(pfad))
    spec = importlib.util.spec_from_loader("ocr_gui", loader)
    modul = importlib.util.module_from_spec(spec)
    loader.exec_module(modul)
    return modul


@unittest.skipUnless(tkinter and ANZEIGE, "kein tkinter oder kein Display")
class GuiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr-gui-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.original = t.original_erzeugen()
        antwort = t.fake_antwort(self.original)
        self.gui = gui_laden()
        for ziel, ersatz in ((ocr, dict(ocr=lambda m, p, pr: antwort,
                                        modelle_auflisten=lambda: ["user.LightOnOCR-3-4B-GGUF-Q8_0"])),
                             (self.gui, dict(EINSTELLUNGEN=self.tmp / "einstellungen.json",
                                             INTERVALL=0.2))):
            for name, wert in ersatz.items():
                p = mock.patch.object(ziel, name, wert)
                p.start()
                self.addCleanup(p.stop)
        mb = mock.patch.object(self.gui, "messagebox")
        self.messagebox = mb.start()
        self.addCleanup(mb.stop)
        self.app = self.gui.OcrGui([])
        self.addCleanup(self.schliessen)
        self.warten(lambda: self.app.var_modell.get())

    def schliessen(self):
        for kennung in self.app.tk.call("after", "info"):
            self.app.after_cancel(kennung)
        self.app.destroy()

    def warten(self, bedingung, sekunden=30):
        ende = time.time() + sekunden
        while time.time() < ende:
            self.app.update()
            if bedingung():
                return
            time.sleep(0.05)
        self.fail("Zeitüberschreitung beim Warten auf die Oberfläche")

    def laeuft(self):
        return self.app.arbeiter is not None and self.app.arbeiter.is_alive()

    def test_modell_wird_gewaehlt_und_ist_eintippbar(self):
        self.assertEqual(self.app.var_modell.get(), "user.LightOnOCR-3-4B-GGUF-Q8_0")
        self.assertNotEqual(str(self.app.cb_modell.cget("state")), "readonly")

    def test_eingabe_und_ausgabe_ordner(self):
        ein, aus = self.tmp / "Eingang", self.tmp / "Ergebnisse"
        ein.mkdir()
        t.scan_erzeugen(self.original, ein / "Erstes.pdf")
        t.scan_erzeugen(self.original, ein / "Zweites.pdf")
        self.app.var_eingabe_eigen.set(True)
        self.app.var_eingabe.set(str(ein))
        self.app.var_ziel_eigen.set(True)
        self.app.var_ziel.set(str(aus))
        self.app.var_pdf.set(True)
        self.app.starten()
        self.warten(lambda: not self.laeuft() and str(self.app.btn_start.cget("state")) == "normal")
        self.assertEqual(sorted(p.name for p in aus.glob("*.ocr.pdf")),
                         ["Erstes.ocr.pdf", "Zweites.ocr.pdf"])
        self.assertIn("Fertig: 2 Datei(en)", self.app.var_status.get())

    def test_ordner_ueberwachen(self):
        ein, aus = self.tmp / "Eingang", self.tmp / "Ergebnisse"
        ein.mkdir()
        self.app.var_eingabe_eigen.set(True)
        self.app.var_eingabe.set(str(ein))
        self.app.var_ueberwachen.set(True)
        self.app.var_ziel_eigen.set(True)
        self.app.var_ziel.set(str(aus))
        self.app.starten()
        self.warten(lambda: "Überwache" in self.app.var_status.get())
        t.scan_erzeugen(self.original, ein / "Neu.pdf")
        self.warten(lambda: (aus / "Neu.ocr.md").exists())
        self.app.abbrechen()
        self.warten(lambda: not self.laeuft() and str(self.app.btn_start.cget("state")) == "normal")
        self.assertIn("abgebrochen", self.app.var_status.get())

    def test_einstellungen_ohne_api_key(self):
        self.app.var_key.set("geheim")
        self.app.var_eingabe.set(str(self.tmp))
        self.app._einstellungen_speichern()
        text = (self.tmp / "einstellungen.json").read_text(encoding="utf-8")
        self.assertNotIn("geheim", text)
        self.assertIn("eingabe", text)


if __name__ == "__main__":
    unittest.main()
