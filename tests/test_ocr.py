"""
Tests für ocr.py ohne Lemonade Server: ocr.ocr wird durch eine Funktion ersetzt,
die eine feste grounding-Antwort liefert (siehe testseite.py).

Aufruf im Projektordner:   python -m unittest discover -s tests -v
"""
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import openai
import pymupdf as fitz

import testseite as t
from testseite import ocr

STILL = lambda _text: None  # noqa: E731


def _fehler(klasse, text="Testfehler"):
    """openai-Fehler ohne echte HTTP-Anfrage erzeugen (unabhängig von der Version)."""
    f = klasse.__new__(klasse)
    Exception.__init__(f, text)
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
        patcher = mock.patch.object(ocr, "ocr", lambda modell, png, prompt, empfangen=None: self.antwort)
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


class GanzeSeiteAlsBildTest(Grundlage):
    """LightOnOCR stuft manchmal eine ganze Seite (z. B. eine Zeitleiste) als
    Bild ein und antwortet nur mit "![image](image_1.png)"."""
    BILD = "![image](image_1.png)"

    def mit_antworten(self, antworten):
        aufrufe = []

        def fake(modell, png, prompt, empfangen=None):
            aufrufe.append(prompt)
            return antworten[prompt]
        patcher = mock.patch.object(ocr, "ocr", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return aufrufe

    def test_zweiter_versuch_ohne_grounding(self):
        aufrufe = self.mit_antworten({"grounding": self.BILD, "": "1917 Oktoberrevolution\n\n1919 Vertrag"})
        erzeugt = ocr.verarbeiten("x", self.scan(), pdf=True, melden=STILL)
        self.assertEqual(aufrufe, ["grounding", ""])
        md = erzeugt[0].read_text(encoding="utf-8")
        self.assertIn("1917 Oktoberrevolution", md)
        self.assertNotIn("image_1.png", md)
        self.assertIn("Oktoberrevolution", fitz.open(erzeugt[1])[0].get_text())

    def test_ganze_seite_als_bild_mit_koordinaten(self):
        aufrufe = self.mit_antworten({"grounding": "![image](0,0,1000,1000)\nZeitleiste", "": "Text"})
        md = ocr.verarbeiten("x", self.scan(), melden=STILL)[0].read_text(encoding="utf-8")
        self.assertEqual(aufrufe, ["grounding", ""])
        self.assertIn("Text", md)

    def test_kein_text_auch_im_zweiten_versuch(self):
        aufrufe = self.mit_antworten({"grounding": self.BILD, "": self.BILD})
        meldungen = []
        erzeugt = ocr.verarbeiten("x", self.scan(), pdf=True, melden=meldungen.append)
        self.assertEqual(aufrufe, ["grounding", ""])
        md = erzeugt[0].read_text(encoding="utf-8")
        self.assertIn(ocr.KEIN_TEXT, md)
        self.assertNotIn("image_1.png", md)
        self.assertNotIn("image", fitz.open(erzeugt[1])[0].get_text())
        self.assertTrue(any("Kein Text erkannt" in m for m in meldungen))
        self.assertTrue(ocr.ist_erledigt(Path(self.tmp / "Beispiel.pdf"), self.tmp, "Beispiel"))

    def test_leere_seite_und_normale_seite_ohne_zweiten_versuch(self):
        aufrufe = self.mit_antworten({"grounding": ""})
        ocr.verarbeiten("x", self.scan(), melden=STILL)
        aufrufe2 = self.mit_antworten({"grounding": self.antwort})
        ocr.verarbeiten("x", self.scan(), melden=STILL)
        self.assertEqual(aufrufe + aufrufe2, ["grounding", "grounding"])

    def test_wiederholungsschleife_wird_erkannt(self):
        schleife = '"\n' * 73
        aufrufe = self.mit_antworten({"grounding": "![text](100,100,900,900)\n" + schleife,
                                      "": "1917 Oktoberrevolution"})
        erzeugt = ocr.verarbeiten("x", self.scan(), pdf=True, melden=STILL)
        self.assertEqual(aufrufe, ["grounding", ""])
        self.assertIn("1917 Oktoberrevolution", erzeugt[0].read_text(encoding="utf-8"))

    def test_unbrauchbar_auch_im_zweiten_versuch(self):
        schleife = '"\n' * 73
        aufrufe = self.mit_antworten({"grounding": schleife, "": "- - - - - - - - - - - - -"})
        erzeugt = ocr.verarbeiten("x", self.scan(), pdf=True, melden=STILL)
        self.assertEqual(aufrufe, ["grounding", ""])
        md = erzeugt[0].read_text(encoding="utf-8")
        self.assertIn(ocr.UNBRAUCHBAR, md)
        self.assertNotIn('"\n"', md)
        self.assertEqual(fitz.open(erzeugt[1])[0].get_text().strip(), "")

    def test_leere_seite_ohne_hinweis(self):
        self.mit_antworten({"grounding": ""})
        md = ocr.verarbeiten("x", self.scan(), melden=STILL)[0].read_text(encoding="utf-8")
        self.assertNotIn("Kein Text erkannt", md)

    def test_unbrauchbar(self):
        frage = "".join(f"{i}. Welche Verbindung ist ein Kohlenwasserstoff?\n   A. Ethanol\n   B. Ethen\n"
                        "   C. Essigsäure\n   D. Glucose\n\n" for i in range(1, 21))
        for text in ('"\n' * 73, "....................", "ab\n" * 12, '" " " "\n" "', frage):
            self.assertTrue(ocr.unbrauchbar(text), text)
        for text in ("", "1", "Datum: 03.03.", "| 1 | 2 |\n|---|---|\n| 3 | 4 |",
                     self.antwort, "Ja\nNein\n" * 3,
                     "\n".join(f"| {i} | Position {i} | {i * 3},50 € |" for i in range(40))):
            self.assertFalse(ocr.unbrauchbar(text), text)

    def test_anderer_prompt_ohne_zweiten_versuch(self):
        aufrufe = self.mit_antworten({"OCR:": self.BILD})
        ocr.verarbeiten("x", self.scan(), prompt="OCR:", melden=STILL)
        self.assertEqual(aufrufe, ["OCR:"])


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

        def ocr_mit_fehler(modell, png, prompt, empfangen=None):
            aufrufe.append(1)
            if len(aufrufe) == 2:
                raise zeitueberschreitung()
            return self.antwort

        with mock.patch.object(ocr, "ocr", ocr_mit_fehler):
            with self.assertRaises(ocr.SeitenFehler) as f:
                ocr.verarbeiten("x", self.scan(seiten=3), pdf=True, melden=STILL)
        self.assertEqual(f.exception.seiten, [2])
        md = (self.tmp / "Beispiel.ocr.md").read_text(encoding="utf-8")
        self.assertIn("<!-- Seite 3 -->", md)
        self.assertIn(ocr.SEITE_FEHLT, md)
        self.assertEqual(len(fitz.open(self.tmp / "Beispiel.ocr.pdf")), 3)
        self.assertFalse(ocr.ist_erledigt(self.tmp / "Beispiel.pdf", self.tmp, "Beispiel"))

    def test_fehler_im_stream_betrifft_nur_die_seite(self):
        absturz = _fehler(openai.APIError, "decode() failed: vk::Device::waitForFences: ErrorDeviceLost")
        self.assertTrue(ocr.seitenfehler(absturz))
        self.assertIn("CPU-Backend", ocr.fehler_hinweis(absturz))
        self.assertEqual(ocr.fehler_hinweis(zeitueberschreitung()), "")

        def ocr_mit_absturz(modell, png, prompt, empfangen=None):
            raise absturz
        meldungen = []
        with mock.patch.object(ocr, "ocr", ocr_mit_absturz):
            with self.assertRaises(ocr.SeitenFehler):
                ocr.verarbeiten("x", self.scan(), melden=meldungen.append)
        self.assertTrue(any("systemctl --user restart lemond" in m for m in meldungen))

    def test_verbindung_weg_speichert_teilergebnis(self):
        aufrufe = []

        def ocr_ohne_server(modell, png, prompt, empfangen=None):
            aufrufe.append(1)
            if len(aufrufe) == 3:
                raise keine_verbindung()
            return self.antwort

        with mock.patch.object(ocr, "ocr", ocr_ohne_server):
            with self.assertRaises(openai.APIConnectionError):
                ocr.verarbeiten("x", self.scan(seiten=4), pdf=True, melden=STILL)
        md = (self.tmp / "Beispiel.ocr.md").read_text(encoding="utf-8")
        self.assertIn("<!-- Seite 2 -->", md)
        self.assertNotIn("<!-- Seite 3 -->", md)
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


class AusgabeTest(Grundlage):
    """Keine Überschrift „Seite 1“, Markdown optional, mehrere Dateien zusammenfügen."""

    def test_keine_seitenueberschrift(self):
        md = ocr.verarbeiten("x", self.scan(), melden=STILL)[0].read_text(encoding="utf-8")
        self.assertNotIn("Seite", md)
        self.assertTrue(md.startswith("# Beispiel für eine Testseite"))
        md = ocr.verarbeiten("x", self.scan(seiten=2), melden=STILL)[0].read_text(encoding="utf-8")
        self.assertNotIn("## Seite", md)
        self.assertIn("<!-- Seite 2 -->", md)   # unsichtbare Seitenmarke

    def test_ohne_markdown(self):
        erzeugt = ocr.verarbeiten("x", self.scan(), pdf=True, md=False, melden=STILL)
        self.assertEqual([p.name for p in erzeugt], ["Beispiel.ocr.pdf"])
        self.assertFalse((self.tmp / "Beispiel.ocr.md").exists())
        self.assertTrue(ocr.ist_erledigt(self.tmp / "Beispiel.pdf", self.tmp, "Beispiel"))

    def test_ohne_markdown_teilergebnis_bleibt_markiert(self):
        aufrufe = []

        def ocr_mit_fehler(modell, png, prompt, empfangen=None):
            aufrufe.append(1)
            if len(aufrufe) == 2:
                raise zeitueberschreitung()
            return self.antwort

        with mock.patch.object(ocr, "ocr", ocr_mit_fehler):
            with self.assertRaises(ocr.SeitenFehler):
                ocr.verarbeiten("x", self.scan(seiten=2), pdf=True, md=False, melden=STILL)
        # Markdown trotzdem da, damit die Datei als unvollständig gilt
        self.assertTrue((self.tmp / "Beispiel.ocr.md").exists())
        self.assertFalse(ocr.ist_erledigt(self.tmp / "Beispiel.pdf", self.tmp, "Beispiel"))
        # nächster, vollständiger Lauf ohne Markdown räumt das Teilergebnis weg
        ocr.verarbeiten("x", self.tmp / "Beispiel.pdf", pdf=True, md=False, melden=STILL)
        self.assertFalse((self.tmp / "Beispiel.ocr.md").exists())
        self.assertTrue(ocr.ist_erledigt(self.tmp / "Beispiel.pdf", self.tmp, "Beispiel"))

    def test_zusammenfuegen(self):
        a = self.scan("Erstes.pdf", seiten=2)
        b = self.scan("Zweites.pdf", drehung=90)
        png = self.tmp / "Drittes.png"
        png.write_bytes(self.original[0].get_pixmap(dpi=100).tobytes("png"))
        aus = self.tmp / "aus"
        erzeugt = ocr.verarbeiten("x", [b, png, a], pdf=True, ziel_ordner=aus,
                                  name="Gesamt", melden=STILL)
        self.assertEqual([p.name for p in erzeugt], ["Gesamt.ocr.md", "Gesamt.ocr.pdf"])
        doc = fitz.open(erzeugt[1])
        self.assertEqual(len(doc), 4)
        self.assertEqual(doc[0].rotation, 90)     # Drehung bleibt erhalten
        for seite in doc:
            self.assertIn("Testseite", seite.get_text())
        md = erzeugt[0].read_text(encoding="utf-8")
        marken = [z for z in md.splitlines() if z.startswith("<!--")]
        self.assertEqual(marken, ["<!-- Zweites.pdf, Seite 1 -->", "<!-- Drittes.png, Seite 1 -->",
                                  "<!-- Erstes.pdf, Seite 1 -->", "<!-- Erstes.pdf, Seite 2 -->"])
        # Bilder aller Seiten mit eindeutigen Namen
        self.assertEqual(len(list((aus / "Gesamt.ocr-bilder").glob("*.png"))), 4)

    def test_zusammenfuegen_standardname(self):
        erzeugt = ocr.verarbeiten("x", [self.scan("Erstes.pdf"), self.scan("Zweites.pdf")],
                                  melden=STILL)
        self.assertEqual(erzeugt[0].name, "Erstes-zusammen.ocr.md")

    def test_zielname(self):
        for eingabe, name in (("Beispiel", "Beispiel"), ("Beispiel.pdf", "Beispiel"),
                              ("Beispiel.ocr.docx", "Beispiel"), ("a/b:c", "a_b_c"), (" . ", "")):
            self.assertEqual(ocr.zielname(eingabe), name)


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

    def test_zusammen_ohne_markdown(self):
        a, b = self.scan("Erstes.pdf"), self.scan("Zweites.pdf")
        code, aus, err = self.starten("m", str(b), str(a), "-z", "Gesamt.pdf", "--pdf", "--ohne-md")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.tmp / "Gesamt.ocr.pdf").exists())
        self.assertEqual(len(fitz.open(self.tmp / "Gesamt.ocr.pdf")), 2)
        self.assertEqual(list(self.tmp.glob("*.ocr.md")), [])
        self.assertFalse((self.tmp / "Erstes.ocr.pdf").exists())

    def test_ohne_markdown_braucht_anderes_format(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ocr._argumente(["m", str(self.scan()), "--ohne-md"])

    def test_ausgabe_als_utf8(self):
        code, aus, _ = self.starten("m", str(self.scan()))
        self.assertEqual(code, 0)
        self.assertIn("Ärger", aus)


def _stueck(content=None, reasoning=None, grund=None):
    """Ein Stück einer gestreamten Antwort, wie es der openai-Client liefert."""
    delta = SimpleNamespace(content=content, model_extra={"reasoning_content": reasoning} if reasoning else {})
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=grund)])


class StreamingTest(unittest.TestCase):
    def anfragen(self, stuecke):
        client = mock.Mock()
        client.chat.completions.create.return_value = iter(stuecke)
        zaehler = []
        with mock.patch.object(ocr, "client", client):
            text = ocr.ocr("m", b"png", "grounding", empfangen=zaehler.append)
        return text, zaehler, client.chat.completions.create.call_args.kwargs

    def test_text_wird_zusammengesetzt_und_gezaehlt(self):
        text, zaehler, argumente = self.anfragen(
            [_stueck("Hallo "), _stueck("Welt"), _stueck(grund="stop")])
        self.assertEqual(text, "Hallo Welt")
        self.assertEqual(zaehler[:2], [6, 10])
        self.assertTrue(argumente["stream"])
        # Denkmodus bleibt abgeschaltet (siehe CLAUDE.md)
        self.assertEqual(argumente["extra_body"], {"chat_template_kwargs": {"enable_thinking": False}})

    def test_denk_block_wird_entfernt(self):
        self.assertEqual(ocr._antwort_text("<think>\n\n</think>\n\n# Vorwort\nText", "", "stop"),
                         "# Vorwort\nText")
        self.assertEqual(ocr._antwort_text("\n</think>\nText", "", "stop"), "Text")
        self.assertEqual(ocr._antwort_text("<think>Nur hier</think>", "", "stop"), "Nur hier")
        self.assertEqual(ocr._antwort_text("a < b > c", "", "stop"), "a < b > c")
        self.assertEqual(ocr._antwort_text("<think></think>", "Reasoning", "stop"), "Reasoning")

    def test_rueckfall_auf_reasoning_und_warnung(self):
        text, zaehler, _ = self.anfragen([_stueck(reasoning="Nur gedacht"), _stueck(grund="length")])
        self.assertTrue(text.startswith("Nur gedacht"))
        self.assertIn("WARNUNG: Ausgabe abgeschnitten", text)
        self.assertEqual(zaehler[0], 11)


class FortschrittTest(Grundlage):
    def test_dauer_text(self):
        self.assertEqual(ocr.dauer_text(5), "0:05")
        self.assertEqual(ocr.dauer_text(125), "2:05")
        self.assertEqual(ocr.dauer_text(3725), "1:02:05")

    def test_restzeit_aus_bisherigen_seiten(self):
        f = ocr.Fortschritt()
        uhr = [100.0]
        with mock.patch.object(ocr.time, "monotonic", lambda: uhr[0]):
            f.seite_beginnt("Beispiel.pdf", 1, 3)
            self.assertIsNone(f.stand()["rest"])          # noch keine Erfahrung
            self.assertIn("Bild wird ausgewertet", f.text())
            f.empfangen(500)
            self.assertIn("500 Zeichen", f.text())
            uhr[0] = 160.0
            self.assertEqual(f.seite_fertig(), 60.0)
            f.seite_beginnt("Beispiel.pdf", 2, 3)
            uhr[0] = 190.0
            st = f.stand()
        self.assertAlmostEqual(st["anteil"], 1.5 / 3)    # halbe zweite Seite
        self.assertAlmostEqual(st["rest"], 30 + 60)      # Rest Seite 2 + Seite 3
        self.assertEqual(ocr.dauer_text(st["rest"]), "1:30")

    def test_verarbeiten_meldet_seitendauer_und_zeichen(self):
        zaehler = []

        def ocr_mit_strom(modell, png, prompt, empfangen=None):
            for n in (100, 200):
                empfangen(n)
                zaehler.append(anzeige.stand()["zeichen"])
            return self.antwort

        anzeige = ocr.Fortschritt()
        meldungen = []
        with mock.patch.object(ocr, "ocr", ocr_mit_strom):
            ocr.verarbeiten("x", self.scan(seiten=2), melden=meldungen.append, anzeige=anzeige)
        self.assertEqual(zaehler, [100, 200, 100, 200])
        self.assertFalse(anzeige.aktiv)
        self.assertEqual(len(anzeige.dauern), 2)
        fertig = [m for m in meldungen if "fertig nach" in m]
        self.assertEqual(len(fertig), 2)
        self.assertIn("noch ca.", fertig[0])

    def test_konsolenanzeige(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        strom = Terminal()
        f = ocr.Fortschritt()
        breit = mock.patch.object(ocr.shutil, "get_terminal_size", lambda *_: os.terminal_size((200, 20)))
        with breit:
            konsole = ocr.Konsolenanzeige(f, strom=strom, takt=0.01)
            f.seite_beginnt("Beispiel.pdf", 1, 2)
            time.sleep(0.1)
            konsole.melden("→ Beispiel.pdf: Seite 1/2 …")
            f.ende()
            konsole.beenden()
        ausgabe = strom.getvalue()
        self.assertIn("Beispiel.pdf: Seite 1/2 · 0:00 · Bild wird ausgewertet", ausgabe)
        self.assertIn("[----------", ausgabe)
        self.assertIn("→ Beispiel.pdf: Seite 1/2 …\n", ausgabe)

    def test_keine_konsolenanzeige_ohne_terminal(self):
        strom = io.StringIO()
        konsole = ocr.Konsolenanzeige(ocr.Fortschritt(), strom=strom, takt=0.01)
        konsole.melden("Hallo")
        konsole.beenden()
        self.assertEqual(strom.getvalue(), "Hallo\n")


@unittest.skipUnless(shutil.which("pandoc"), "Pandoc nicht installiert")
class PandocTest(Grundlage):
    def test_word_und_libreoffice(self):
        erzeugt = ocr.verarbeiten("x", self.scan(), formate=("docx", "odt"), melden=STILL)
        self.assertEqual([p.name for p in erzeugt],
                         ["Beispiel.ocr.md", "Beispiel.ocr.docx", "Beispiel.ocr.odt"])

    def test_word_ohne_markdown_und_ohne_seitenmarken(self):
        erzeugt = ocr.verarbeiten("x", [self.scan("Erstes.pdf", seiten=2), self.scan("Zweites.pdf")],
                                  formate=("docx",), md=False, name="Gesamt", melden=STILL)
        self.assertEqual([p.name for p in erzeugt], ["Gesamt.ocr.docx"])
        text = subprocess.run([shutil.which("pandoc"), "-t", "plain", str(erzeugt[0])],
                              capture_output=True, text=True, encoding="utf-8", check=True).stdout
        self.assertEqual(text.count("Beispiel für eine Testseite"), 3)
        self.assertNotIn("Seite 1", text)
        self.assertNotIn("Erstes.pdf", text)
        # Bild aus dem Bildordner ist eingebettet
        with zipfile.ZipFile(erzeugt[0]) as z:
            self.assertEqual(len([n for n in z.namelist() if n.startswith("word/media/")]), 3)

    def test_fehlermeldung_von_pandoc(self):
        with self.assertRaises(RuntimeError) as f:
            ocr._pandoc([shutil.which("pandoc"), "--gibt-es-nicht"])
        self.assertIn("Pandoc-Fehler", str(f.exception))
        self.assertIn("gibt-es-nicht", str(f.exception))


if __name__ == "__main__":
    unittest.main()
