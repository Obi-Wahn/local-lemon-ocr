"""
Tests für ocr.py ohne Lemonade Server: ocr.ocr wird durch eine Funktion ersetzt,
die eine feste grounding-Antwort liefert (siehe testseite.py).

Aufruf im Projektordner:   python -m unittest discover -s tests -v
"""
import io
import os
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import openai
import pymupdf as fitz

import testseite as t
from testseite import ocr

STILL = lambda _text: None  # noqa: E731


def _fehler(klasse):
    """openai-Fehler ohne echte HTTP-Anfrage erzeugen (unabhängig von der Version)."""
    f = klasse.__new__(klasse)
    Exception.__init__(f, "Testfehler")
    return f


def zeitueberschreitung():
    return _fehler(openai.APITimeoutError)


def keine_verbindung():
    return _fehler(openai.APIConnectionError)


class Grundlage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = t.original_erzeugen()
        cls.antwort = t.fake_antwort(cls.original)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.object(ocr, "ocr", lambda modell, png, prompt: self.antwort)
        patcher.start()
        self.addCleanup(patcher.stop)

    def scan(self, name="Beispiel.pdf", drehung=0, seiten=1, ordner=None):
        ordner = ordner or self.tmp
        ordner.mkdir(parents=True, exist_ok=True)
        pfad = t.scan_erzeugen(self.original, ordner / name, drehung)
        if seiten > 1:
            doc = fitz.open(pfad)
            for _ in range(seiten - 1):
                doc.insert_pdf(fitz.open(pfad), from_page=0, to_page=0)
            doc.save(pfad, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        return pfad


class TextebeneTest(Grundlage):
    def test_woerter_liegen_an_der_richtigen_stelle(self):
        erzeugt = ocr.verarbeiten("x", self.scan(), pdf=True, melden=STILL)
        iou, gefunden, gesamt = t.woerter_vergleichen(self.original, fitz.open(erzeugt[1])[0])
        self.assertEqual(gefunden, gesamt)
        self.assertGreater(iou, 0.75)

    def test_gedrehte_seiten(self):
        for drehung in (90, 180, 270):
            with self.subTest(drehung=drehung):
                erzeugt = ocr.verarbeiten("x", self.scan(f"Gedreht{drehung}.pdf", drehung),
                                          pdf=True, melden=STILL)
                iou, gefunden, gesamt = t.woerter_vergleichen(self.original, fitz.open(erzeugt[1])[0])
                self.assertEqual(gefunden, gesamt)
                self.assertGreater(iou, 0.75)

    def test_sonderzeichen_bleiben_erhalten(self):
        erzeugt = ocr.verarbeiten("x", self.scan(), pdf=True, melden=STILL)
        text = fitz.open(erzeugt[1])[0].get_text()
        for teil in ("bericht_2024_final.txt", "12,50 €", "5 * 3 * 2", "Ärger"):
            self.assertIn(teil, text)

    def test_vorhandene_textebene_wird_nicht_verdoppelt(self):
        digital = self.tmp / "Digital.pdf"
        self.original.save(digital)
        meldungen = []
        erzeugt = ocr.verarbeiten("x", digital, pdf=True, melden=meldungen.append)
        text = fitz.open(erzeugt[1])[0].get_text()
        self.assertEqual(text.count("Testseite"), 1)
        self.assertTrue(any("schon Text" in m for m in meldungen))

    def test_bilder_werden_ausgeschnitten(self):
        erzeugt = ocr.verarbeiten("x", self.scan(), pdf=True, melden=STILL)
        bilder = list((self.tmp / "Beispiel.ocr-bilder").glob("*.png"))
        self.assertEqual(len(bilder), 1)
        md = erzeugt[0].read_text(encoding="utf-8")
        self.assertIn("](<Beispiel.ocr-bilder/seite01-bild01.png>)", md)


class KlartextTest(unittest.TestCase):
    def test_hervorhebung(self):
        self.assertEqual(ocr.als_klartext("**fett**, *kursiv*, _unter_, `code`"),
                         "fett, kursiv, unter, code")

    def test_unterstriche_und_sternchen_im_text(self):
        for text in ("bericht_2024_final.txt", "5 * 3 * 2 = 30", "a__b__c", "Fußnote*"):
            self.assertEqual(ocr.als_klartext(text), text)

    def test_tabelle(self):
        self.assertEqual(ocr.als_klartext("<table><tr><td>A</td><td>B &amp; C</td></tr></table>"),
                         "A B & C")


class SeitenfehlerTest(Grundlage):
    def test_einzelne_seite_scheitert(self):
        aufrufe = []

        def ocr_mit_fehler(modell, png, prompt):
            aufrufe.append(1)
            if len(aufrufe) == 2:
                raise zeitueberschreitung()
            return self.antwort

        with mock.patch.object(ocr, "ocr", ocr_mit_fehler):
            with self.assertRaises(ocr.SeitenFehler) as f:
                ocr.verarbeiten("x", self.scan(seiten=3), pdf=True, melden=STILL)
        self.assertEqual(f.exception.seiten, [2])
        md = (self.tmp / "Beispiel.ocr.md").read_text(encoding="utf-8")
        self.assertIn("## Seite 3", md)
        self.assertIn(ocr.SEITE_FEHLT, md)
        self.assertEqual(len(fitz.open(self.tmp / "Beispiel.ocr.pdf")), 3)
        self.assertFalse(ocr.ist_erledigt(self.tmp / "Beispiel.pdf", self.tmp, "Beispiel"))

    def test_verbindung_weg_speichert_teilergebnis(self):
        aufrufe = []

        def ocr_ohne_server(modell, png, prompt):
            aufrufe.append(1)
            if len(aufrufe) == 3:
                raise keine_verbindung()
            return self.antwort

        with mock.patch.object(ocr, "ocr", ocr_ohne_server):
            with self.assertRaises(openai.APIConnectionError):
                ocr.verarbeiten("x", self.scan(seiten=4), pdf=True, melden=STILL)
        md = (self.tmp / "Beispiel.ocr.md").read_text(encoding="utf-8")
        self.assertIn("## Seite 2", md)
        self.assertNotIn("## Seite 3", md)
        self.assertIn(ocr.UNVOLLSTAENDIG, md)

    def test_abbruch_speichert_teilergebnis(self):
        seiten = []
        with self.assertRaises(ocr.Abgebrochen):
            ocr.verarbeiten("x", self.scan(seiten=3), melden=STILL,
                            fortschritt=lambda s, g: seiten.append(s),
                            abbrechen=lambda: len(seiten) >= 2)
        md = (self.tmp / "Beispiel.ocr.md").read_text(encoding="utf-8")
        self.assertIn("abgebrochen nach Seite 2 von 3", md)


class NamenUndOrdnerTest(Grundlage):
    def test_gleiche_namen_ueberschreiben_sich_nicht(self):
        belegt = set()
        a = self.scan("Beispiel.pdf", ordner=self.tmp / "a")
        b = self.scan("Beispiel.pdf", ordner=self.tmp / "b")
        png = self.tmp / "a" / "Beispiel.png"
        png.write_bytes(self.original[0].get_pixmap(dpi=100).tobytes("png"))
        ziel = self.tmp / "aus"
        for d in (a, b, png):
            ocr.verarbeiten("x", d, ziel_ordner=ziel, melden=STILL, belegt=belegt)
        namen = sorted(p.name for p in ziel.glob("*.ocr.md"))
        self.assertEqual(namen, ["Beispiel-pdf.ocr.md", "Beispiel-png.ocr.md", "Beispiel.ocr.md"])

    def test_offene_dateien_und_erledigt(self):
        ein, aus = self.tmp / "Eingang", self.tmp / "Ergebnisse"
        self.scan("Erstes.pdf", ordner=ein)
        self.scan("Zweites.pdf", ordner=ein)
        (ein / "Notiz.txt").write_text("kein Bild", encoding="utf-8")
        offen = ocr.offene_dateien(ein, aus)
        self.assertEqual([d.name for d, _ in offen], ["Erstes.pdf", "Zweites.pdf"])
        ocr.verarbeiten("x", ein / "Erstes.pdf", ziel_ordner=aus, melden=STILL)
        self.assertEqual([d.name for d, _ in ocr.offene_dateien(ein, aus)], ["Zweites.pdf"])
        self.assertEqual(len(ocr.offene_dateien(ein, aus, alle=True)), 2)
        # geänderte Datei wird erneut verarbeitet
        zukunft = time.time() + 5
        os.utime(ein / "Erstes.pdf", (zukunft, zukunft))
        self.assertEqual(len(ocr.offene_dateien(ein, aus)), 2)

    def test_eigene_ergebnisse_im_gleichen_ordner_ignorieren(self):
        ocr.verarbeiten("x", self.scan(), pdf=True, melden=STILL)
        self.assertEqual([d.name for d in ocr.ordner_dateien(self.tmp)], ["Beispiel.pdf"])
        self.assertEqual(ocr.offene_dateien(self.tmp), [])

    def test_wartezimmer(self):
        datei = self.scan()
        w = ocr.Wartezimmer()
        self.assertFalse(w.bereit(datei))   # zum ersten Mal gesehen
        self.assertTrue(w.bereit(datei))    # unverändert
        with datei.open("ab") as f:
            f.write(b"\n")
        self.assertFalse(w.bereit(datei))   # wird noch geschrieben


class KommandozeileTest(Grundlage):
    def starten(self, *argv):
        aus, err = io.StringIO(), io.StringIO()
        with redirect_stdout(aus), redirect_stderr(err), mock.patch.object(ocr, "verbinden"):
            with self.assertRaises(SystemExit) as ende:
                ocr.main(list(argv))
        return ende.exception.code, aus.getvalue(), err.getvalue()

    def test_alte_schreibweise_mit_prompt(self):
        a = ocr._argumente(["m", str(self.scan()), "OCR:"])
        self.assertEqual(a.prompt, "OCR:")
        self.assertEqual(len(a.dateien), 1)
        self.assertEqual(ocr._argumente(["m", str(self.scan()), ""]).prompt, "")
        self.assertEqual(ocr._argumente(["m", str(self.scan())]).prompt, "grounding")

    def test_tippfehler_wird_nicht_zum_prompt(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ocr._argumente(["m", str(self.scan()), "--PDF"])
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ocr._argumente(["m", str(self.scan()), "Bespiel.pdf"])

    def test_eine_datei(self):
        code, aus, _ = self.starten("m", str(self.scan()), "--pdf")
        self.assertEqual(code, 0)
        self.assertIn("Beispiel für eine Testseite", aus)
        self.assertTrue((self.tmp / "Beispiel.ocr.pdf").exists())

    def test_eingabe_und_ausgabe_ordner(self):
        ein, aus = self.tmp / "Eingang", self.tmp / "Ergebnisse"
        self.scan("Erstes.pdf", ordner=ein)
        self.scan("Zweites.pdf", ordner=ein)
        code, _, err = self.starten("m", "-e", str(ein), "-o", str(aus), "--pdf")
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(p.name for p in aus.glob("*.ocr.pdf")),
                         ["Erstes.ocr.pdf", "Zweites.ocr.pdf"])
        # zweiter Lauf: nichts mehr zu tun
        with mock.patch.object(ocr, "verarbeiten") as v:
            code, _, _ = self.starten("m", "-e", str(ein), "-o", str(aus))
        self.assertEqual(code, 0)
        v.assert_not_called()

    def test_ueberwachen_verarbeitet_neue_dateien(self):
        ein, aus = self.tmp / "Eingang", self.tmp / "Ergebnisse"
        self.scan("Erstes.pdf", ordner=ein)
        runden = []

        def schlafen(_sek):
            runden.append(1)
            if len(runden) == 1:
                self.scan("Neu.pdf", ordner=ein)   # kommt während der Überwachung dazu
            if len(runden) > 3:
                raise KeyboardInterrupt

        with mock.patch.object(ocr.time, "sleep", schlafen):
            code, _, err = self.starten("m", "-e", str(ein), "-o", str(aus), "--ueberwachen")
        self.assertEqual(code, 130)
        self.assertEqual(sorted(p.name for p in aus.glob("*.ocr.md")), ["Erstes.ocr.md", "Neu.ocr.md"])

    def test_ausgabe_als_utf8(self):
        code, aus, _ = self.starten("m", str(self.scan()))
        self.assertEqual(code, 0)
        self.assertIn("Ärger", aus)


@unittest.skipUnless(shutil.which("pandoc"), "Pandoc nicht installiert")
class PandocTest(Grundlage):
    def test_word_und_libreoffice(self):
        erzeugt = ocr.verarbeiten("x", self.scan(), formate=("docx", "odt"), melden=STILL)
        self.assertEqual([p.name for p in erzeugt],
                         ["Beispiel.ocr.md", "Beispiel.ocr.docx", "Beispiel.ocr.odt"])

    def test_fehlermeldung_von_pandoc(self):
        with self.assertRaises(RuntimeError) as f:
            ocr._pandoc([shutil.which("pandoc"), "--gibt-es-nicht"])
        self.assertIn("Pandoc-Fehler", str(f.exception))
        self.assertIn("gibt-es-nicht", str(f.exception))


if __name__ == "__main__":
    unittest.main()
