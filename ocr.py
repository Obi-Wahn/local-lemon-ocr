"""
OCR über einen lokalen Lemonade Server (OpenAI-kompatible API).

Installation:   pip install openai pymupdf
Aufruf:         python ocr.py <Modell> [Datei ...] [Optionen]

Beispiele:
  python ocr.py user.LightOnOCR-3-4B-GGUF-Q8_0 scan.png
  python ocr.py user.LightOnOCR-3-4B-GGUF-Q8_0 arbeitsblatt.pdf --pdf --docx
  python ocr.py user.LightOnOCR-3-4B-GGUF-Q8_0 a.pdf b.pdf -o Ergebnisse --pdf
  python ocr.py user.LightOnOCR-3-4B-GGUF-Q8_0 -e Eingang -o Ergebnisse --pdf
  python ocr.py user.LightOnOCR-3-4B-GGUF-Q8_0 -e Eingang -o Ergebnisse --pdf --ueberwachen
  python ocr.py user.LightOnOCR-3-4B-GGUF-Q8_0 s1.pdf s2.pdf s3.png -z Gesamt --pdf --docx --ohne-md
  python ocr.py user.PaddleOCR-VL-1.6-GGUF scan.png -p "OCR:"

Ohne Prompt nutzt das Skript den "grounding"-Modus von LightOnOCR-3: Das Modell
liefert zu jedem Block die Position. Bilder und Diagramme werden ausgeschnitten,
im Ordner <Name>.ocr-bilder gespeichert und an ihrer Stelle in Markdown,
Word und LibreOffice eingefügt. Für andere Modelle eigenen Prompt angeben.

--pdf   Durchsuchbares PDF mit dem Original-Layout: Die Originalseite bleibt
        unverändert sichtbar, darüber liegt unsichtbarer Text an den Stellen,
        an denen er im Bild steht (Suchen, Markieren, Kopieren funktioniert).
        Bilder werden zusätzlich als eigene Bildobjekte deckungsgleich an
        ihre Originalstelle gelegt (lassen sich einzeln kopieren/exportieren).

--docx / --odt   Zusätzlich Word- bzw. LibreOffice-Datei
                 (braucht Pandoc: winget install JohnMacFarlane.Pandoc)

-e ORDNER        Eingabe-Ordner: alle PDFs und Bilder darin verarbeiten
                 (bereits erledigte werden übersprungen, außer mit --alle)
-o ORDNER        Ausgabe-Ordner (Standard: neben der Originaldatei)
--ueberwachen    Eingabe-Ordner laufend auf neue Dateien prüfen (Strg+C beendet)
--ohne-md        keine Markdown-Datei (nur PDF/Word/LibreOffice)
-z NAME          alle Dateien in der angegebenen Reihenfolge zu einem Ergebnis
                 NAME.ocr.pdf/.docx/… zusammenfügen (mit -e: offene Dateien des
                 Ordners alphabetisch dahinter)

Den genauen Modellnamen zeigt:  lemonade list
"""
import argparse
import base64
import html
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import openai
import pymupdf as fitz  # pip-Paket "pymupdf"
from openai import OpenAI

# Port prüfen mit: lemonade config. Lässt sich über LEMONADE_BASE_URL ändern.
BASE_URL = os.environ.get("LEMONADE_BASE_URL", "http://localhost:13305/api/v1")
MAX_KANTE = 2048                             # längste Bildkante in Pixeln
WIEDERHOLUNGEN = 2                           # automatische Wiederholungen je Anfrage

# Dateitypen, die verarbeitet werden (Eingabe-Ordner, GUI-Dateiauswahl)
ENDUNGEN = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")

client = None


def verbinden(base_url: str = BASE_URL, api_key: str | None = None):
    """Verbindung zu Lemonade einrichten. Ohne Key wird LEMONADE_API_KEY
    genommen, falls gesetzt – sonst ein Platzhalter."""
    global client
    client = OpenAI(base_url=base_url,
                    api_key=api_key or os.environ.get("LEMONADE_API_KEY", "lemonade"),
                    timeout=600, max_retries=WIEDERHOLUNGEN)
    return client


def modelle_auflisten() -> list[str]:
    """Namen aller Modelle, die Lemonade kennt."""
    if client is None:
        verbinden()
    return sorted(m.id for m in client.models.list().data)


def _melden(text: str):
    print(text, file=sys.stderr)

# Schrift für die unsichtbare Textebene: eine Unicode-Schrift (für €, „“ usw.),
# sonst die eingebaute Helvetica
SCHRIFT_DATEI = next((f for f in (
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
) if os.path.exists(f)), None)
SCHRIFT = dict(fontname="ocrfont", fontfile=SCHRIFT_DATEI) if SCHRIFT_DATEI else dict(fontname="helv")

# Blocktypen, deren Inhalt eine Beschreibung ist und nicht auf der Seite steht
KEIN_SEITENTEXT = {"image", "chart", "header_image", "footer_image"}
BILD_TYPEN = {"image", "header_image", "footer_image"}
BILD_DPI = 300                               # Auflösung der ausgeschnittenen Bilder


# ---------------------------------------------------------------- Eingabe ---

def seiten_laden(pfad: Path) -> fitz.Document:
    """Öffnet PDF oder Bild als PDF-Dokument (ein Bild = eine Seite)."""
    if pfad.suffix.lower() == ".pdf":
        doc = fitz.open(pfad)
        if doc.needs_pass:
            raise ValueError("PDF ist passwortgeschützt – bitte zuerst ohne Passwort speichern.")
        return doc
    bild = fitz.open(pfad)
    return fitz.open("pdf", bild.convert_to_pdf())


def seite_als_png(seite: fitz.Page) -> bytes:
    zoom = MAX_KANTE / max(seite.rect.width, seite.rect.height)
    return seite.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")


def hat_textebene(seite: fitz.Page) -> bool:
    """True, wenn die Seite schon echten Text enthält (z. B. digitales PDF
    oder bereits per OCR bearbeitet)."""
    return bool(seite.get_text("text").strip())


# -------------------------------------------------------------------- OCR ---

def ocr(modell: str, png: bytes, prompt: str, empfangen=None) -> str:
    """Schickt ein Seitenbild an Lemonade und liefert den erkannten Text.
    empfangen(zeichen) wird, falls angegeben, laufend mit der Zahl der bisher
    empfangenen Zeichen aufgerufen. Dafür wird die Antwort gestreamt."""
    if client is None:
        verbinden()
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    inhalt = [{"type": "image_url", "image_url": {"url": data_url}}]
    if prompt:
        inhalt.append({"type": "text", "text": prompt})
    anfrage = dict(
        model=modell,
        messages=[{"role": "user", "content": inhalt}],
        temperature=0.2,          # Empfehlung von LightOn
        top_p=0.9,
        max_tokens=8192,
        # "Denkmodus" der Qwen3.5-Vorlage abschalten, sonst landet der Text
        # in reasoning_content und content bleibt leer
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    if empfangen is None:
        antwort = client.chat.completions.create(**anfrage)
        msg = antwort.choices[0].message
        return _antwort_text(msg.content, _reasoning(msg), antwort.choices[0].finish_reason)

    text, denken, grund, zeichen = [], [], None, 0
    for stueck in client.chat.completions.create(**anfrage, stream=True):
        if not stueck.choices:
            continue
        wahl = stueck.choices[0]
        if wahl.delta is not None:
            for ziel, teil in ((text, wahl.delta.content), (denken, _reasoning(wahl.delta))):
                if teil:
                    ziel.append(teil)
                    zeichen += len(teil)
            empfangen(zeichen)
        grund = wahl.finish_reason or grund
    return _antwort_text("".join(text), "".join(denken), grund)


def _reasoning(msg) -> str:
    return getattr(msg, "reasoning_content", None) \
        or (getattr(msg, "model_extra", None) or {}).get("reasoning_content", "") or ""


# Denk-Block der Qwen3.5-Vorlage. Trotz enable_thinking=False schreibt der
# Server manchmal einen leeren "<think>\n\n</think>" vor die Antwort, bei
# manchen Vorlagen fehlt das öffnende Tag.
DENKEN = re.compile(r"<think>(.*?)</think>|^\s*(.*?)</think>", re.S)


def denken_entfernen(text: str) -> str:
    """Entfernt <think>…</think> aus der Antwort. Steht außerhalb nichts, wird
    der Inhalt des Denk-Blocks genommen (wie beim Rückfall auf reasoning_content)."""
    if "</think>" not in text:
        return text
    innen = []
    rest = DENKEN.sub(lambda m: innen.append(m.group(1) or m.group(2) or "") or "", text)
    rest = rest.replace("<think>", "").strip()
    return rest or "\n".join(innen).strip()


def _antwort_text(text, reasoning, grund) -> str:
    text = denken_entfernen(text or "")
    if not text.strip():
        # Fallback: Text steckt trotzdem im Reasoning-Feld
        text = reasoning or ""
    if grund == "length":
        text += "\n\n[WARNUNG: Ausgabe abgeschnitten – max_tokens/Kontext erhöhen]"
    return text


# ------------------------------------------------------ Fortschritt/Zeit ---

def dauer_text(sekunden: float) -> str:
    """125 -> "2:05", 3725 -> "1:02:05"."""
    sekunden = max(0, int(round(sekunden)))
    std, rest = divmod(sekunden, 3600)
    return f"{std}:{rest // 60:02d}:{rest % 60:02d}" if std else f"{rest // 60}:{rest % 60:02d}"


class Fortschritt:
    """Stand der gerade laufenden Seite – für die Live-Anzeige in GUI und
    Kommandozeile. verarbeiten() meldet Seitenbeginn, empfangene Zeichen und
    Seitenende; die Anzeige fragt stand() bzw. text() so oft ab, wie sie will
    (auch während das Modell noch das Bild auswertet und nichts schickt).

    Die Restzeit wird aus der mittleren Dauer der letzten fertigen Seiten
    geschätzt, auch über Dateien hinweg. Vor der ersten Seite gibt es keine
    Schätzung."""

    MERKEN = 10                      # so viele Seiten fließen in den Mittelwert ein

    def __init__(self):
        self.dauern, self.laengen = [], []
        self.aktiv, self.nachbereitung = False, False
        self.datei, self.seite, self.seiten, self.zeichen, self.start = "", 0, 0, 0, 0.0

    def seite_beginnt(self, datei: str, seite: int, seiten: int):
        self.datei, self.seite, self.seiten = datei, seite, seiten
        self.zeichen, self.start, self.aktiv = 0, time.monotonic(), True
        self.nachbereitung = False

    def empfangen(self, zeichen: int):
        self.zeichen = zeichen

    def seite_fertig(self, erfolgreich: bool = True) -> float:
        dauer = time.monotonic() - self.start
        self.nachbereitung = True     # Antwort da, jetzt Textebene/Bilder/Speichern
        if erfolgreich:
            self.dauern = (self.dauern + [dauer])[-self.MERKEN:]
            self.laengen = (self.laengen + [self.zeichen])[-self.MERKEN:]
        return dauer

    def ende(self):
        self.aktiv = False

    def mittlere_dauer(self):
        return sum(self.dauern) / len(self.dauern) if self.dauern else None

    def stand(self) -> dict:
        """sekunden: Laufzeit der Seite, anteil: Fortschritt in der Datei (0–1),
        rest: geschätzte Restzeit der Datei in Sekunden oder None."""
        sekunden = time.monotonic() - self.start if self.aktiv else 0.0
        mittel = self.mittlere_dauer()
        if self.nachbereitung:
            seite_anteil = 1.0
            rest = mittel * (self.seiten - self.seite) if mittel else None
        elif mittel:
            seite_anteil = min(0.95, sekunden / mittel)
            rest = max(0.0, mittel - sekunden) + mittel * (self.seiten - self.seite)
        else:
            erwartet = sum(self.laengen) / len(self.laengen) if self.laengen else 2500
            seite_anteil = min(0.9, self.zeichen / erwartet)
            rest = None
        anteil = (self.seite - 1 + seite_anteil) / max(self.seiten, 1)
        return dict(datei=self.datei, seite=self.seite, seiten=self.seiten, zeichen=self.zeichen,
                    sekunden=sekunden, anteil=anteil, rest=rest)

    def text(self) -> str:
        """z. B. "Seite 2/5 · 1:23 · Text kommt an (340 Zeichen) · noch ca. 4:10"."""
        st = self.stand()
        if self.nachbereitung:
            phase = "wird weiterverarbeitet"
        elif st["zeichen"]:
            phase = f"Text kommt an ({st['zeichen']} Zeichen)"
        else:
            phase = "Bild wird ausgewertet"
        teile = [f"Seite {st['seite']}/{st['seiten']}", dauer_text(st["sekunden"]), phase]
        if st["rest"] is not None and not (self.nachbereitung and self.seite == self.seiten):
            teile.append(f"noch ca. {dauer_text(st['rest'])}" if st["rest"] >= 5 else "gleich fertig")
        return " · ".join(teile)


def seitenfehler(fehler: Exception) -> bool:
    """True, wenn der Fehler nur diese eine Seite betrifft (Zeitüberschreitung,
    Serverfehler, zu große Anfrage). Dann lohnt es sich, mit der nächsten Seite
    weiterzumachen. Bei fehlender Verbindung, falschem Key oder unbekanntem
    Modell würden alle Seiten scheitern – dann wird die Datei abgebrochen."""
    if isinstance(fehler, openai.APITimeoutError):
        return True
    if isinstance(fehler, (openai.APIConnectionError, openai.AuthenticationError,
                           openai.PermissionDeniedError, openai.NotFoundError)):
        return False
    # APIError ohne Statuscode: Der Server hat mitten in der gestreamten
    # Antwort einen Fehler geschickt (z. B. Vulkan "ErrorDeviceLost")
    return isinstance(fehler, openai.APIError)


def fehler_hinweis(fehler: Exception) -> str:
    """Zusätzlicher Hinweis zu bekannten Serverfehlern, sonst leer."""
    text = str(fehler)
    if "DeviceLost" in text or "vk::" in text:
        return ("Die Grafikkarte (Vulkan) ist im Lemonade Server abgestürzt. Server neu starten "
                "(Linux: systemctl --user restart lemond) und bei schwacher Grafik in Lemonade "
                "das CPU-Backend wählen.")
    return ""


# ------------------------------------------------------- Grounding-Format ---

PLATZHALTER = re.compile(
    r"!\[([^\]\n]*)\]\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*,"
    r"\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)")


def bloecke_parsen(text: str):
    """Zerlegt die grounding-Ausgabe in Blöcke (typ, (x0,y0,x1,y1), inhalt).
    Ein Block beginnt mit ![typ](x0,y0,x1,y1) – egal ob der Inhalt in derselben
    oder in der nächsten Zeile folgt. Koordinaten sind auf 0–1000 normiert.
    Ohne Platzhalter: ein Block für die ganze Seite (z. B. bei PaddleOCR)."""
    treffer = list(PLATZHALTER.finditer(text))
    if not treffer:
        return [("text", (0, 0, 1000, 1000), text.strip())]
    bloecke = []
    vorspann = text[:treffer[0].start()].strip()
    if vorspann:
        bloecke.append(("text", (0, 0, 1000, 1000), vorspann))
    for i, m in enumerate(treffer):
        ende = treffer[i + 1].start() if i + 1 < len(treffer) else len(text)
        typ = m.group(1).strip().lower().rstrip("+").replace("-", "_").replace(" ", "_")
        a, b, c, d = (float(m.group(k)) for k in (2, 3, 4, 5))
        box = (min(a, c), min(b, d), max(a, c), max(b, d))
        bloecke.append((typ or "text", box, text[m.end():ende].strip()))
    return bloecke


# Bildverweis ohne Koordinaten, z. B. "![image](image_1.png)". So antwortet
# LightOnOCR, wenn es einen Bereich (oder die ganze Seite) für ein Bild hält,
# aber nicht im grounding-Format. Die Datei gibt es nicht, der Link wäre kaputt.
BILDVERWEIS = re.compile(r"!\[[^\]\n]*\]\((?!\s*\d+(?:\.\d+)?\s*,)[^)\n]*\)")
KEIN_TEXT = "[Kein Text erkannt: Das Modell hat die Seite als Bild eingestuft]"
UNBRAUCHBAR = "[Kein Text erkannt: Die Antwort des Modells war unbrauchbar (nur Sonderzeichen oder Wiederholungen)]"


def bildverweise_entfernen(bloecke):
    """Entfernt Bildverweise ohne Koordinaten aus den Blockinhalten."""
    return [(typ, box, BILDVERWEIS.sub("", inhalt).strip()) for typ, box, inhalt in bloecke]


def nur_bild(antwort: str, bloecke) -> bool:
    """True, wenn das Modell auf der Seite keinen Text erkannt, sondern sie als
    Bild eingestuft hat: kein Textblock mit Inhalt und dafür ein Bildverweis
    ohne Koordinaten oder Bildboxen über mindestens die halbe Seite. Leere
    Seiten und ein kleines Logo auf einer sonst leeren Seite zählen nicht.
    bloecke: schon ohne Bildverweise (bildverweise_entfernen)."""
    if any(inhalt for typ, _, inhalt in bloecke if typ not in BILD_TYPEN):
        return False
    if BILDVERWEIS.search(antwort):
        return True
    flaeche = sum((x1 - x0) * (y1 - y0) for typ, (x0, y0, x1, y1), _ in bloecke
                  if typ in BILD_TYPEN)
    return flaeche >= 0.5 * 1000 * 1000


def textinhalt(bloecke) -> str:
    """Der Text aller Blöcke, die keine Bilder sind."""
    return "\n".join(inhalt for typ, _, inhalt in bloecke if typ not in BILD_TYPEN and inhalt)


def unbrauchbar(text: str) -> bool:
    """True, wenn die Antwort Text enthält, der keiner ist: nur Sonderzeichen
    (z. B. 70 Zeilen mit einem einzelnen Anführungszeichen) oder dieselbe
    kurze Zeile immer wieder. So sieht es aus, wenn das Modell in eine
    Wiederholungsschleife gerät. Eine leere Antwort ist nicht unbrauchbar."""
    zeichen = "".join(text.split())
    if not zeichen:
        return False
    buchstaben = sum(c.isalnum() for c in zeichen)
    if buchstaben == 0 and len(zeichen) >= 3:
        return True
    if len(zeichen) >= 20 and buchstaben < 0.1 * len(zeichen):
        return True
    zeilen = [z.strip() for z in text.splitlines() if z.strip()]
    if len(zeilen) >= 10 and len(set(zeilen)) <= 2 and all(len(z) <= 5 for z in zeilen):
        return True
    # Derselbe Abschnitt immer wieder, nur anders nummeriert (z. B. 20-mal die
    # gleiche Frage mit "1.", "2.", …): kaum verschiedene Zeilen bei vielen Zeilen
    ohne_nummer = {re.sub(r"^[\d.)\s]+", "", z) for z in zeilen}
    return len(zeilen) >= 30 and len(ohne_nummer) < 0.15 * len(zeilen)


def _pruefen(antwort: str, bloecke):
    """None, wenn die Seite brauchbaren Text (oder gar nichts) enthält, sonst
    der Hinweis, warum kein Text erkannt wurde."""
    if nur_bild(antwort, bloecke):
        return KEIN_TEXT
    if unbrauchbar(textinhalt(bloecke)):
        return UNBRAUCHBAR
    return None


def seite_erkennen(modell: str, png: bytes, prompt: str, empfangen=None, melden=_melden):
    """OCR einer Seite, liefert (Antwort, Blöcke, Hinweis). Hält LightOnOCR die
    ganze Seite für ein Bild (passiert z. B. bei Zeitleisten oder Schaubildern
    mit wenig Text) oder ist die Antwort unbrauchbar (Wiederholungsschleife),
    wird einmal im anderen LightOnOCR-Modus nachgefragt ("grounding" ↔ "").
    Hilft das nicht, wird unbrauchbarer Text verworfen, damit er weder im
    Markdown noch in der Textebene landet. Hinweis ist dann KEIN_TEXT oder
    UNBRAUCHBAR, sonst None (auch bei einer leeren Seite)."""
    antwort = ocr(modell, png, prompt, empfangen=empfangen)
    bloecke = bildverweise_entfernen(bloecke_parsen(antwort))
    hinweis = _pruefen(antwort, bloecke)
    if hinweis and prompt in ("grounding", ""):
        anderer = "" if prompt == "grounding" else "grounding"
        grund = ("Das Modell hat die Seite als Bild eingestuft" if hinweis == KEIN_TEXT
                 else "Die Antwort des Modells war unbrauchbar")
        melden(f"   {grund} – zweiter Versuch im Modus "
               + ("„nur Text“" if anderer == "" else "„grounding“") + " …")
        zweite = ocr(modell, png, anderer, empfangen=empfangen)
        zweite_bloecke = bildverweise_entfernen(bloecke_parsen(zweite))
        if textinhalt(zweite_bloecke) and not _pruefen(zweite, zweite_bloecke):
            return zweite, zweite_bloecke, None
        melden("   Auch im zweiten Versuch kein Text erkannt. Passiert das bei jedem Bild, sieht das "
               "Modell das Bild wahrscheinlich nicht: in Lemonade prüfen, ob das MMProj geladen ist.")
    if hinweis == UNBRAUCHBAR:
        # Bilder behalten, den unbrauchbaren Text verwerfen
        bloecke = [(typ, box, inhalt if typ in BILD_TYPEN else "") for typ, box, inhalt in bloecke]
    return antwort, bloecke, hinweis


def als_markdown(bloecke, bilder=None) -> str:
    """Markdown ohne Platzhalter. Ausgeschnittene Bilder werden an ihrer Stelle
    eingefügt (Beschreibung des Modells als Bildunterschrift)."""
    bilder = bilder or {}
    teile = []
    for i, (typ, _, inhalt) in enumerate(bloecke):
        if i in bilder:
            beschreibung = " ".join(inhalt.split()) if typ in BILD_TYPEN else ""
            teile.append(f"![{beschreibung}](<{bilder[i]}>)")
            if typ == "chart" and inhalt:
                teile.append(inhalt)          # Diagrammdaten als Tabelle darunter
            continue
        if not inhalt:
            continue
        if typ in BILD_TYPEN:
            teile.append(f"*[Bild: {inhalt}]*")
        elif typ in ("page_number", "header", "footer"):
            continue  # Kopf-/Fußzeilen und Seitenzahlen nicht in den Fließtext
        else:
            teile.append(inhalt)
    return "\n\n".join(teile)


# Hervorhebung nur erkennen, wenn die Zeichen ein Wort umschließen: so bleiben
# "datei_name_neu.txt" und "5 * 3 * 2" unverändert, "**fett**" wird zu "fett"
_HERVORHEBUNG = re.compile(r"(?<![\w*`])(\*\*|__|\*|_|`)(?=\S)(.+?)(?<=\S)\1(?![\w*`])")


def als_klartext(inhalt: str) -> str:
    """Markdown/HTML zu schlichtem Text für die unsichtbare Textebene."""
    t = re.sub(r"</t[dh]>", " ", inhalt)
    t = re.sub(r"</tr>|<br\s*/?>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)      # Überschriften
    t = _HERVORHEBUNG.sub(r"\2", t)                           # Fett, kursiv, Code
    t = re.sub(r"^\s*\|?[-:| ]+\|?\s*$", "", t, flags=re.M)   # Markdown-Tabellenlinien
    t = t.replace("|", " ")
    return html.unescape(re.sub(r"[ \t]+", " ", t)).strip()


# ----------------------------------------------------- Durchsuchbares PDF ---
# Gedrehte Seiten (page.rotation): Das Modell sieht die Seite so, wie sie
# angezeigt wird. seite.rect, get_pixmap() und clip arbeiten ebenfalls in
# diesen Anzeige-Koordinaten. insert_text/insert_textbox/insert_image erwarten
# dagegen ungedrehte Koordinaten: Deshalb werden Punkte und Rechtecke mit
# seite.derotation_matrix umgerechnet und der Text mit rotate=seite.rotation
# gesetzt.

def _passt(rect: fitz.Rect, text: str, groesse: float) -> bool:
    probe = fitz.open()
    seite = probe.new_page(width=rect.width + 10, height=rect.height + 10)
    rest = seite.insert_textbox(fitz.Rect(0, 0, rect.width, rect.height), text,
                                fontsize=groesse, **SCHRIFT)
    probe.close()
    return rest >= 0


def _block_einpassen(seite: fitz.Page, rect: fitz.Rect, text: str):
    """Notlösung: ganzen Block mit passender Schriftgröße in die Box setzen."""
    lo, hi = 1.0, min(72.0, rect.height)
    for _ in range(12):
        mitte = (lo + hi) / 2
        lo, hi = (mitte, hi) if _passt(rect, text, mitte) else (lo, mitte)
    seite.insert_textbox(rect * seite.derotation_matrix, text, fontsize=lo, render_mode=3,
                         rotate=seite.rotation, **SCHRIFT)


# Tabelle für bytes.translate: dunkles Pixel -> 1, helles -> 0
_DUNKEL = bytes(1 if v < 150 else 0 for v in range(256))


def zeilen_finden(grau: fitz.Pixmap, px0, py0, px1, py1):
    """Sucht im Graustufenbild innerhalb der Box die Textzeilen.
    Liefert Liste von (x0, y0, x1, y1) in Pixeln – eine pro Zeile."""
    w, h, stride = grau.width, grau.height, grau.stride
    data = grau.samples
    # Box etwas vergrößern – die Boxen des Modells sind nicht pixelgenau
    rand_x, rand_y = int((px1 - px0) * 0.03) + 4, int((py1 - py0) * 0.08) + 4
    px0, px1 = max(0, int(px0) - rand_x), min(w, int(px1) + rand_x)
    py0, py1 = max(0, int(py0) - rand_y), min(h, int(py1) + rand_y)
    if px1 - px0 < 3 or py1 - py0 < 3:
        return []
    min_pixel = max(1, (px1 - px0) // 400)        # Rauschen ignorieren
    zeilen, band = [], None
    for y in range(py0, py1):
        reihe = data[y * stride + px0: y * stride + px1].translate(_DUNKEL)
        if reihe.count(1) >= min_pixel:
            l, r = reihe.find(1), reihe.rfind(1)
            if band is None:
                band = [px0 + l, y, px0 + r + 1, y + 1]
            else:
                band = [min(band[0], px0 + l), band[1], max(band[2], px0 + r + 1), y + 1]
        elif band is not None:
            zeilen.append(band)
            band = None
    if band is not None:
        zeilen.append(band)
    if not zeilen:
        return []
    # sehr flache Bänder (i-Punkte, Umlaute, Unterstriche) mit Nachbarn verschmelzen
    hoehen = sorted(z[3] - z[1] for z in zeilen)
    median = hoehen[len(hoehen) // 2]
    ergebnis = []
    for z in zeilen:
        if ergebnis and (z[3] - z[1] < 0.4 * median or z[1] - ergebnis[-1][3] < 0.15 * median) \
                and (z[1] - ergebnis[-1][3] < 0.6 * median):
            e = ergebnis[-1]
            ergebnis[-1] = [min(e[0], z[0]), e[1], max(e[2], z[2]), z[3]]
        else:
            ergebnis.append(list(z))
    # Bänder, die am Rand der vergrößerten Box zu Nachbarblöcken gehören, verwerfen
    return [z for z in ergebnis if z[3] - z[1] >= 0.4 * median]


def text_verteilen(text: str, breiten):
    """Verteilt den Text auf die Zeilen – nach den Zeilenumbrüchen des Modells,
    wenn die Anzahl passt, sonst nach Zeilenbreite."""
    eigene = [z.strip() for z in text.splitlines() if z.strip()]
    if len(eigene) == len(breiten):
        return eigene
    woerter = " ".join(eigene).split()
    if len(breiten) == 1 or len(woerter) <= 1:
        return [" ".join(woerter)] + [""] * (len(breiten) - 1)
    gesamt_zeichen = sum(len(wo) + 1 for wo in woerter)
    gesamt_breite = sum(breiten)
    zeilen, aktuell, zeichen, grenze, i = [], [], 0, 0.0, 0
    for wo in woerter:
        if aktuell and i < len(breiten) - 1 and \
                zeichen + (len(wo) + 1) / 2 > grenze + breiten[i] / gesamt_breite * gesamt_zeichen:
            zeilen.append(" ".join(aktuell))
            grenze += breiten[i] / gesamt_breite * gesamt_zeichen
            aktuell, i = [], i + 1
        aktuell.append(wo)
        zeichen += len(wo) + 1
    zeilen.append(" ".join(aktuell))
    return zeilen + [""] * (len(breiten) - len(zeilen))


def textebene_einfuegen(seite: fitz.Page, bloecke):
    """Legt unsichtbaren Text (render_mode=3) über die Seite – zeilengenau:
    Innerhalb jeder Box des Modells werden die echten Textzeilen im Bild
    gesucht, und jede Zeile bekommt genau so viel Breite und Höhe wie im Scan."""
    zoom = MAX_KANTE / max(seite.rect.width, seite.rect.height)
    grau = seite.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
    sx, sy = seite.rect.width / grau.width, seite.rect.height / grau.height
    schrift = fitz.Font(fontfile=SCHRIFT_DATEI) if SCHRIFT_DATEI else fitz.Font("helv")
    drehung = seite.rotation
    ungedreht = seite.derotation_matrix

    for typ, (x0, y0, x1, y1), inhalt in bloecke:
        if typ in KEIN_SEITENTEXT:
            continue
        text = als_klartext(inhalt)
        if not text:
            continue
        box = fitz.Rect(x0 * seite.rect.width / 1000, y0 * seite.rect.height / 1000,
                        x1 * seite.rect.width / 1000, y1 * seite.rect.height / 1000)
        if box.is_empty or box.width < 2 or box.height < 2:
            continue
        zeilen = zeilen_finden(grau, box.x0 / sx, box.y0 / sy, box.x1 / sx, box.y1 / sy)
        if not zeilen:
            _block_einpassen(seite, box, text)
            continue
        texte = text_verteilen(text, [z[2] - z[0] for z in zeilen])
        # einheitliche Schriftgröße je Block: Tinte einer Zeile (Oberlänge bis
        # Unterlänge) ist etwa 0,95 × Schriftgröße hoch
        hoehen = sorted((z[3] - z[1]) * sy for z in zeilen)
        groesse = max(hoehen[len(hoehen) // 2] / 0.95, 1.0)
        for (zx0, zy0, zx1, zy1), zeile in zip(zeilen, texte):
            if not zeile:
                continue
            r = fitz.Rect(zx0 * sx, zy0 * sy, zx1 * sx, zy1 * sy)
            laenge = schrift.text_length(zeile, fontsize=groesse)
            if laenge <= 0:
                continue
            # Grundlinie so legen, dass der Text vertikal mittig auf der Zeile sitzt
            grundlinie = fitz.Point(r.x0, (r.y0 + r.y1) / 2 + groesse * 0.255) * ungedreht
            # Text horizontal genau auf die Zeilenbreite strecken/stauchen
            # (bei 90°/270° liegt die Leserichtung auf der ungedrehten y-Achse)
            faktor = r.width / laenge
            strecken = fitz.Matrix(1, faktor) if drehung in (90, 270) else fitz.Matrix(faktor, 1)
            seite.insert_text(grundlinie, zeile, fontsize=groesse, render_mode=3,
                              rotate=drehung, morph=(grundlinie, strecken), **SCHRIFT)


# ------------------------------------------------------- Bilder ausschneiden ---

def bilder_ausschneiden(seite: fitz.Page, bloecke, ordner: Path, seitennr: int):
    """Schneidet Bilder und Diagramme an ihren Boxen aus der Seite aus.
    Liefert {Blockindex: (Dateipfad, Rect, PNG-Bytes)}; Rect in Anzeige-Koordinaten."""
    ergebnis = {}
    W, H = seite.rect.width, seite.rect.height
    for i, (typ, (x0, y0, x1, y1), _) in enumerate(bloecke):
        if typ not in KEIN_SEITENTEXT:
            continue
        rand = 4  # Punkte Zugabe, die Boxen des Modells sind nicht pixelgenau
        rect = fitz.Rect(x0 * W / 1000 - rand, y0 * H / 1000 - rand,
                         x1 * W / 1000 + rand, y1 * H / 1000 + rand) & seite.rect
        if rect.is_empty or rect.width < 10 or rect.height < 10:
            continue
        png = seite.get_pixmap(dpi=BILD_DPI, clip=rect, alpha=False).tobytes("png")
        ordner.mkdir(parents=True, exist_ok=True)
        datei = ordner / f"seite{seitennr:02d}-bild{len(ergebnis) + 1:02d}.png"
        datei.write_bytes(png)
        ergebnis[i] = (datei, rect, png)
    return ergebnis


def bilder_einfuegen(seite: fitz.Page, bilder):
    """Ausgeschnittene Bilder als eigene Objekte deckungsgleich auf die Seite legen."""
    for _, rect, png in bilder.values():
        seite.insert_image(rect * seite.derotation_matrix, stream=png, rotate=seite.rotation)


# ----------------------------------------------------------- Office-Export ---

def _pandoc(befehl, **kwargs) -> str:
    """Pandoc ausführen; bei Fehlern Pandocs eigene Meldung weitergeben."""
    try:
        return subprocess.run(befehl, capture_output=True, text=True, encoding="utf-8",
                              check=True, **kwargs).stdout
    except subprocess.CalledProcessError as f:
        meldung = (f.stderr or "").strip() or f"Rückgabewert {f.returncode}"
        raise RuntimeError(f"Pandoc-Fehler: {meldung}") from None


def office_export(markdown: str, basis: Path, formate, melden=_melden):
    """Markdown -> HTML -> DOCX/ODT über Pandoc, Ziel: basis + ".docx" usw.
    (basis z. B. Ergebnisse/Beispiel.ocr). Der Markdown-Text geht über stdin an
    Pandoc, eine .md-Datei ist also nicht nötig; Bildpfade gelten relativ zum
    Ordner von basis. Der Umweg über HTML erhält Tabellen, die das Modell als
    HTML ausgibt; --mathml macht aus LaTeX-Formeln echte Office-Formeln."""
    pandoc = shutil.which("pandoc")
    if not pandoc:
        melden("Pandoc nicht gefunden – installieren mit: winget install JohnMacFarlane.Pandoc")
        return []
    basis = Path(basis)
    ordner = basis.parent.resolve()
    html_text = _pandoc([pandoc, "-f", "markdown", "-t", "html", "--mathml"],
                        input=markdown, cwd=ordner)
    erzeugt = []
    for fmt in formate:
        ziel = Path(f"{basis}.{fmt}")   # nicht with_suffix(): das ersetzte ".ocr"
        _pandoc([pandoc, "-f", "html", "-o", str(ziel.resolve()),
                 "--resource-path", str(ordner)],
                input=html_text, cwd=ordner)
        melden(f"Gespeichert: {ziel}")
        erzeugt.append(ziel)
    return erzeugt


# ------------------------------------------------- Ordner und Dateinamen ---

def ausgabe_name(datei: Path, ordner: Path, belegt: set | None = None) -> str:
    """Namensstamm der Ergebnisse (ohne ".ocr"). Normalerweise der Name der
    Originaldatei. Ist er in diesem Durchlauf schon vergeben (z. B. Beispiel.pdf
    und Beispiel.png oder zwei Beispiel.pdf aus verschiedenen Ordnern), wird die
    Endung bzw. eine Nummer angehängt: Beispiel-png, Beispiel-2 …"""
    datei = Path(datei)
    kandidaten = [datei.stem, f"{datei.stem}-{datei.suffix.lstrip('.').lower()}"]
    kandidaten += [f"{datei.stem}-{n}" for n in range(2, 1000)]
    for name in kandidaten:
        schluessel = str(Path(ordner).resolve() / name).lower()  # Windows: Groß/klein egal
        if belegt is None or schluessel not in belegt:
            if belegt is not None:
                belegt.add(schluessel)
            return name
    raise RuntimeError(f"Kein freier Ausgabename für {datei.name}")


def ist_ergebnis(pfad: Path) -> bool:
    """True für Dateien, die dieses Programm selbst erzeugt (Beispiel.ocr.pdf).
    Wichtig, wenn Eingabe- und Ausgabe-Ordner derselbe sind."""
    return Path(pfad).stem.lower().endswith(".ocr")


def ordner_dateien(eingabe: Path) -> list[Path]:
    """Alle verarbeitbaren Dateien im Eingabe-Ordner (ohne Unterordner),
    alphabetisch sortiert und ohne eigene Ergebnisse."""
    return sorted((p for p in Path(eingabe).iterdir()
                   if p.is_file() and p.suffix.lower() in ENDUNGEN and not ist_ergebnis(p)
                   and not p.name.startswith((".", "~$"))),
                  key=lambda p: p.name.lower())


AUSGABE_ENDUNGEN = (".md", ".pdf", ".docx", ".odt")


def ist_erledigt(datei: Path, ordner: Path, name: str) -> bool:
    """True, wenn es schon ein Ergebnis (name.ocr.md/.pdf/.docx/.odt) gibt, das
    neuer ist als die Datei. Liegt ein neueres Markdown dabei, entscheidet es:
    Teilergebnisse (Abbruch, fehlende Seiten) zählen nicht. Ohne Markdown-Ausgabe
    wird ein Teilergebnis trotzdem als .md gespeichert (siehe _speichern())."""
    try:
        stand = Path(datei).stat().st_mtime
    except OSError:
        return False
    neuer = []
    for endung in AUSGABE_ENDUNGEN:
        try:
            if (Path(ordner) / f"{name}.ocr{endung}").stat().st_mtime >= stand:
                neuer.append(endung)
        except OSError:
            pass
    if ".md" not in neuer:
        return bool(neuer)
    try:
        text = (Path(ordner) / f"{name}.ocr.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return not ist_unvollstaendig(text)


def ist_unvollstaendig(text: str) -> bool:
    return UNVOLLSTAENDIG in text or SEITE_FEHLT in text


class Wartezimmer:
    """Merkt sich beim Überwachen Größe und Änderungszeit jeder Datei. Eine Datei
    gilt erst als bereit, wenn sie bei zwei Prüfungen gleich geblieben ist – so
    wird keine Datei verarbeitet, die gerade noch kopiert oder gescannt wird."""

    def __init__(self):
        self.stand = {}

    def bereit(self, datei: Path) -> bool:
        try:
            st = datei.stat()
        except OSError:
            return False
        jetzt = (st.st_size, st.st_mtime)
        vorher = self.stand.get(datei)
        self.stand[datei] = jetzt
        return vorher == jetzt and st.st_size > 0


def offene_dateien(eingabe: Path, ausgabe: Path | None = None, alle: bool = False,
                   wartezimmer: "Wartezimmer | None" = None) -> list[tuple[Path, str]]:
    """Dateien im Eingabe-Ordner, die (noch) zu verarbeiten sind, als Liste
    (Datei, Ausgabename). Bereits erledigte werden übersprungen, außer mit alle.
    Mit wartezimmer nur Dateien, die sich seit der letzten Prüfung nicht mehr
    verändert haben."""
    belegt, ergebnis = set(), []
    for d in ordner_dateien(eingabe):
        ziel = Path(ausgabe) if ausgabe else d.parent
        name = ausgabe_name(d, ziel, belegt)      # Namen immer gleich vergeben
        if not alle and ist_erledigt(d, ziel, name):
            continue
        if wartezimmer is not None and not wartezimmer.bereit(d):
            continue
        ergebnis.append((d, name))
    return ergebnis


# ------------------------------------------------------------------- Main ---

UNVOLLSTAENDIG = "[UNVOLLSTÄNDIG"   # Markierungen im Markdown, siehe ist_erledigt()
SEITE_FEHLT = "[FEHLER: Seite nicht erkannt"


class Abgebrochen(Exception):
    pass


class SeitenFehler(Exception):
    """Die Datei wurde gespeichert, aber einzelne Seiten konnten nicht erkannt
    werden. erzeugt: geschriebene Dateien, seiten: Nummern der fehlenden Seiten."""

    def __init__(self, erzeugt, seiten):
        self.erzeugt, self.seiten = erzeugt, seiten
        super().__init__("Seite(n) " + ", ".join(map(str, seiten)) + " nicht erkannt")


def seiten_zusammenfuegen(dateien) -> tuple[fitz.Document, list[tuple[Path, int, int]]]:
    """Alle Dateien in der angegebenen Reihenfolge zu einem Dokument zusammenfügen.
    Liefert das Dokument und pro Seite (Quelldatei, Seite in der Quelle, Seiten der Quelle)."""
    doc, herkunft = fitz.open(), []
    for d in dateien:
        quelle = seiten_laden(Path(d))
        doc.insert_pdf(quelle)        # übernimmt auch die Drehung der Seiten
        herkunft += [(Path(d), nr, quelle.page_count) for nr in range(1, quelle.page_count + 1)]
    return doc, herkunft


def seitenmarke(herkunft, nr: int) -> str:
    """Unsichtbare Seitenmarke im Markdown (HTML-Kommentar), z. B. "<!-- Seite 2 -->".
    In Word/LibreOffice erscheint sie nicht; so bleibt der Text ohne eine sichtbare
    Überschrift „Seite 1“, und im Markdown sieht man trotzdem, wo eine Seite beginnt."""
    quelle, seite, _ = herkunft[nr - 1]
    if len({q for q, _, _ in herkunft}) > 1:
        return f"<!-- {quelle.name}, Seite {seite} -->"
    return f"<!-- Seite {seite} -->"


def verarbeiten(modell: str, datei, prompt: str = "grounding", pdf: bool = False,
                formate=(), ziel_ordner: Path | None = None, melden=_melden,
                fortschritt=None, abbrechen=None, belegt: set | None = None,
                name: str | None = None, anzeige: Fortschritt | None = None,
                md: bool = True) -> list[Path]:
    """Eine Datei komplett verarbeiten. Liefert die erzeugten Dateien.
    datei kann auch eine Liste von Dateien sein: Dann werden sie in dieser
    Reihenfolge zu einem Ergebnis zusammengefügt (ein PDF, ein Markdown, eine
    Word-Datei …); Name ohne Angabe: <erste Datei>-zusammen.
    fortschritt(seite, gesamt) und abbrechen() -> bool sind optional (für die GUI).
    belegt: Menge der in diesem Durchlauf schon vergebenen Ausgabenamen,
    name: fester Ausgabename (z. B. aus offene_dateien()),
    anzeige: Fortschritt-Objekt für eine Live-Anzeige (Laufzeit, Zeichen, Restzeit),
    md: Markdown-Datei speichern (bei Teilergebnissen wird sie immer gespeichert).

    Scheitert eine einzelne Seite (z. B. Zeitüberschreitung), geht es mit der
    nächsten weiter; am Ende kommt SeitenFehler. Bei Abbruch oder einem Fehler,
    der alle Seiten beträfe, wird das bisher Erkannte trotzdem gespeichert."""
    dateien = [Path(d) for d in datei] if isinstance(datei, (list, tuple)) else [Path(datei)]
    if not dateien:
        raise ValueError("Keine Datei angegeben.")
    zusammen = len(dateien) > 1
    ordner = Path(ziel_ordner) if ziel_ordner else dateien[0].parent
    ordner.mkdir(parents=True, exist_ok=True)
    if not name:
        vorlage = dateien[0].with_name(dateien[0].stem + "-zusammen.pdf") if zusammen else dateien[0]
        name = ausgabe_name(vorlage, ordner, belegt)
    basis = str(ordner / (name + ".ocr"))   # + ".md", ".pdf" …
    bild_ordner = ordner / (name + ".ocr-bilder")

    if zusammen:
        melden(f"Füge {len(dateien)} Dateien zu {name} zusammen: "
               + ", ".join(d.name for d in dateien))
        doc, herkunft = seiten_zusammenfuegen(dateien)
    else:
        doc = seiten_laden(dateien[0])
        herkunft = [(dateien[0], nr, doc.page_count) for nr in range(1, doc.page_count + 1)]
    gesamt = doc.page_count
    teile, fehlend, mit_text = [], [], []
    fertig = 0
    anzeige = anzeige or Fortschritt()

    def seite_anfuegen(nr, inhalt):
        if gesamt > 1:
            teile.append(seitenmarke(herkunft, nr))
        teile.append(inhalt)

    try:
        for nr, seite in enumerate(doc, start=1):
            if abbrechen and abbrechen():
                raise Abgebrochen()
            quelle, nr_quelle, seiten_quelle = herkunft[nr - 1]
            if zusammen:
                melden(f"→ {quelle.name}: Seite {nr_quelle}/{seiten_quelle} "
                       f"(gesamt {nr}/{gesamt}) …")
            else:
                melden(f"→ {quelle.name}: Seite {nr}/{gesamt} …")
            if fortschritt:
                fortschritt(nr - 1, gesamt)
            anzeige.seite_beginnt(f"{name} ({quelle.name})" if zusammen else quelle.name, nr, gesamt)
            try:
                antwort, bloecke, hinweis = seite_erkennen(modell, seite_als_png(seite), prompt,
                                                  empfangen=anzeige.empfangen, melden=melden)
            except Exception as f:
                anzeige.seite_fertig(erfolgreich=False)
                if not seitenfehler(f):
                    raise
                melden(f"   ✖ Seite {nr} nicht erkannt: {f}")
                if fehler_hinweis(f):
                    melden(f"     {fehler_hinweis(f)}")
                fehlend.append(nr)
                wo = f"{quelle.name}, Seite {nr_quelle}" if zusammen else f"Seite {nr}"
                seite_anfuegen(nr, f"*{SEITE_FEHLT} – {wo}, {type(f).__name__}]*")
                fertig = nr
                continue
            dauer = anzeige.seite_fertig()
            rest = anzeige.mittlere_dauer() * (gesamt - nr)
            melden(f"   fertig nach {dauer_text(dauer)} ({len(antwort)} Zeichen)"
                   + (f", noch ca. {dauer_text(rest)} für diese Datei" if nr < gesamt else ""))
            bilder = bilder_ausschneiden(seite, bloecke, bild_ordner, nr)
            links = {i: f"{bild_ordner.name}/{d.name}" for i, (d, _, _) in bilder.items()}
            inhalt = als_markdown(bloecke, links)
            if hinweis and not inhalt.strip():
                inhalt = f"*{hinweis}*"
                melden(f"   ⚠ Seite {nr}: {hinweis[1:-1]}")
            seite_anfuegen(nr, inhalt)
            if pdf:
                # Bilder als eigene Objekte deckungsgleich an die Originalstelle legen
                bilder_einfuegen(seite, bilder)
                if hat_textebene(seite):
                    mit_text.append(nr)   # sonst doppelte Suchtreffer
                else:
                    textebene_einfuegen(seite, bloecke)
            if bilder:
                melden(f"   {len(bilder)} Bild(er) gespeichert in {bild_ordner}")
            fertig = nr
    except (Exception, KeyboardInterrupt) as f:
        anzeige.ende()
        if fertig:
            grund = "abgebrochen" if isinstance(f, (Abgebrochen, KeyboardInterrupt)) else "Fehler"
            teile.append(f"*{UNVOLLSTAENDIG}: {grund} nach Seite {fertig} von {gesamt}]*")
            melden(f"   Teilergebnis wird gespeichert ({fertig} von {gesamt} Seiten) …")
            _speichern(doc, teile, basis, pdf, (), melden, md=True)
        raise
    anzeige.ende()
    if fortschritt:
        fortschritt(gesamt, gesamt)
    if mit_text:
        melden(f"   Hinweis: Seite(n) {', '.join(map(str, mit_text))} enthielten schon Text – "
               "dort wurde keine zweite Textebene eingefügt.")
    if fehlend and not md:
        melden("   Markdown wird trotzdem gespeichert, weil Seiten fehlen – "
               "so wird die Datei beim nächsten Lauf erneut verarbeitet.")
    erzeugt = _speichern(doc, teile, basis, pdf, formate, melden, md=md or bool(fehlend))
    if fehlend:
        raise SeitenFehler(erzeugt, fehlend)
    return erzeugt


def _speichern(doc, teile, basis, pdf, formate, melden, md: bool = True) -> list[Path]:
    erzeugt = []
    markdown = "\n\n".join(teile)
    md_ziel = Path(basis + ".md")
    if md:
        md_ziel.write_text(markdown, encoding="utf-8")
        melden(f"Gespeichert: {md_ziel}")
        erzeugt.append(md_ziel)
    else:
        _altes_teilergebnis_entfernen(md_ziel, melden)

    if pdf:
        pdf_ziel = Path(basis + ".pdf")
        if SCHRIFT_DATEI:
            doc.subset_fonts()   # nur benutzte Zeichen einbetten -> kleine Datei
        doc.save(pdf_ziel, garbage=3, deflate=True)
        melden(f"Gespeichert: {pdf_ziel}")
        erzeugt.append(pdf_ziel)

    if formate:
        erzeugt += office_export(markdown, Path(basis), formate, melden)
    return erzeugt


def _altes_teilergebnis_entfernen(md_ziel: Path, melden):
    """Ohne Markdown-Ausgabe: Ein älteres, unvollständiges name.ocr.md aus einem
    früheren Lauf löschen. Sonst hielte ist_erledigt() die Datei für unfertig und
    würde sie im Eingabe-Ordner immer wieder verarbeiten. Vollständige
    Markdown-Dateien bleiben liegen."""
    try:
        if ist_unvollstaendig(md_ziel.read_text(encoding="utf-8")):
            md_ziel.unlink()
            melden(f"Altes Teilergebnis gelöscht: {md_ziel}")
    except (OSError, UnicodeDecodeError):
        pass


# ---------------------------------------------------------- Kommandozeile ---

class Konsolenanzeige:
    """Live-Fortschrittszeile auf stderr, z. B.
    [########------------]  40 % Beispiel.pdf: Seite 2/5 · 1:23 · Bild wird ausgewertet · noch ca. 4:10
    Sie wird jede Sekunde neu gezeichnet, auch wenn das Modell gerade nichts
    schickt. Nur im Terminal aktiv; beim Umleiten in eine Datei bleiben nur die
    normalen Meldungen übrig."""

    BREITE = 20

    def __init__(self, fortschritt: Fortschritt, strom=None, takt: float = 1.0):
        self.fortschritt, self.strom, self.takt = fortschritt, strom or sys.stderr, takt
        self.sperre, self.stopp, self.sichtbar = threading.Lock(), threading.Event(), False
        try:
            self.aktiv = self.strom.isatty()
        except (AttributeError, ValueError):
            self.aktiv = False
        if self.aktiv:
            threading.Thread(target=self._laufen, daemon=True).start()

    def zeile(self) -> str:
        st = self.fortschritt.stand()
        voll = int(st["anteil"] * self.BREITE)
        return (f"[{'#' * voll}{'-' * (self.BREITE - voll)}] {st['anteil'] * 100:3.0f} % "
                f"{st['datei']}: {self.fortschritt.text()}")

    def _loeschen(self):
        if self.sichtbar:
            # Leerzeichen statt Escape-Sequenzen: funktioniert auch in alten Windows-Konsolen
            self.strom.write("\r" + " " * (shutil.get_terminal_size((80, 20)).columns - 1) + "\r")
            self.sichtbar = False

    def _zeichnen(self):
        if not self.fortschritt.aktiv:
            self._loeschen()
            return
        breite = shutil.get_terminal_size((80, 20)).columns - 1
        self.strom.write("\r" + self.zeile()[:breite].ljust(breite))
        self.strom.flush()
        self.sichtbar = True

    def _laufen(self):
        while not self.stopp.wait(self.takt):
            with self.sperre:
                self._zeichnen()

    def melden(self, text: str):
        """Normale Meldung ausgeben, ohne die Fortschrittszeile zu zerreißen."""
        with self.sperre:
            if self.aktiv:
                self._loeschen()
            print(text, file=self.strom, flush=True)
            if self.aktiv:
                self._zeichnen()

    def beenden(self):
        self.stopp.set()
        with self.sperre:
            if self.aktiv:
                self._loeschen()
                self.strom.flush()


def zielname(text: str) -> str:
    """Name für ein zusammengefügtes Ergebnis aus einer Eingabe wie "Beispiel",
    "Beispiel.pdf" oder "Beispiel.ocr.docx": ohne Endung und ohne Zeichen, die
    unter Windows in Dateinamen verboten sind."""
    name = re.sub(r'[\\/:*?"<>|]', "_", (text or "").strip()).strip(" .")
    for endung in (*AUSGABE_ENDUNGEN, *ENDUNGEN):
        if name.lower().endswith(endung):
            name = name[: -len(endung)]
            break
    if name.lower().endswith(".ocr"):
        name = name[:-4]
    return name.strip(" .")


def _argumente(argv):
    p = argparse.ArgumentParser(
        prog="ocr.py", description="OCR über einen lokalen Lemonade Server.",
        epilog="Den genauen Modellnamen zeigt: lemonade list")
    p.add_argument("modell", help="Modellname, z. B. user.LightOnOCR-3-4B-GGUF-Q8_0")
    p.add_argument("dateien", nargs="*", metavar="DATEI", help="PDFs oder Bilder")
    p.add_argument("-p", "--prompt", default=None,
                   help='Prompt (Standard: "grounding"; "" = nur Text; PaddleOCR-VL: "OCR:")')
    p.add_argument("-e", "--eingabe", metavar="ORDNER", type=Path,
                   help="Eingabe-Ordner: alle PDFs und Bilder darin verarbeiten")
    p.add_argument("-o", "--ausgabe", metavar="ORDNER", type=Path,
                   help="Ausgabe-Ordner (Standard: neben der Originaldatei)")
    p.add_argument("--ueberwachen", action="store_true",
                   help="Eingabe-Ordner laufend auf neue Dateien prüfen (Strg+C beendet)")
    p.add_argument("--intervall", type=float, default=10, metavar="SEK",
                   help="Prüfabstand beim Überwachen in Sekunden (Standard 10)")
    p.add_argument("--alle", action="store_true",
                   help="Im Eingabe-Ordner auch bereits erledigte Dateien neu verarbeiten")
    p.add_argument("--pdf", action="store_true", help="durchsuchbares PDF erzeugen")
    p.add_argument("--docx", action="store_true", help="Word-Datei erzeugen (braucht Pandoc)")
    p.add_argument("--odt", action="store_true", help="LibreOffice-Datei erzeugen (braucht Pandoc)")
    p.add_argument("--ohne-md", action="store_true",
                   help="keine Markdown-Datei speichern (dann --pdf, --docx oder --odt angeben)")
    p.add_argument("-z", "--zusammen", metavar="NAME",
                   help="alle Dateien in der angegebenen Reihenfolge zu einem Ergebnis "
                        "NAME.ocr.pdf/.docx … zusammenfügen")
    p.add_argument("--url", default=BASE_URL, help=f"Server-Adresse (Standard {BASE_URL})")
    a = p.parse_args(argv)

    # Alte Schreibweise "ocr.py Modell Datei Prompt": letztes Argument ist der
    # Prompt, wenn es keine Datei ist und auch nicht wie ein Dateiname aussieht
    if a.prompt is None and len(a.dateien) >= 2:
        letztes = a.dateien[-1]
        if not letztes or (not Path(letztes).exists()
                           and Path(letztes).suffix.lower() not in ENDUNGEN):
            a.prompt = a.dateien.pop()
    if a.prompt is None:
        a.prompt = "grounding"
    if not a.dateien and not a.eingabe:
        p.error("Bitte eine Datei oder mit -e einen Eingabe-Ordner angeben.")
    if a.ueberwachen and not a.eingabe:
        p.error("--ueberwachen braucht einen Eingabe-Ordner (-e).")
    if a.ohne_md and not (a.pdf or a.docx or a.odt):
        p.error("Mit --ohne-md bitte --pdf, --docx oder --odt angeben, sonst entsteht nichts.")
    if a.zusammen is not None:
        if a.ueberwachen:
            p.error("--zusammen geht nicht zusammen mit --ueberwachen.")
        a.zusammen = zielname(a.zusammen)
        if not a.zusammen:
            p.error("--zusammen braucht einen Namen, z. B. --zusammen Beispiel")
    if a.eingabe and not a.eingabe.is_dir():
        p.error(f"Eingabe-Ordner nicht gefunden: {a.eingabe}")
    fehlt = [d for d in a.dateien if not Path(d).is_file()]
    if fehlt:
        p.error("Datei nicht gefunden: " + ", ".join(fehlt))
    return a


def main(argv=None):
    for strom in (sys.stdout, sys.stderr):   # Windows: Umleiten in Datei ohne UnicodeEncodeError
        try:
            strom.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    a = _argumente(sys.argv[1:] if argv is None else argv)
    verbinden(a.url)
    formate = [f for f in ("docx", "odt") if getattr(a, f)]
    fehler, letztes = 0, None
    fortschritt = Fortschritt()
    konsole = Konsolenanzeige(fortschritt)
    melden = konsole.melden

    def eine_datei(d: Path, **kwargs) -> bool:
        nonlocal fehler, letztes
        try:
            letztes = verarbeiten(a.modell, d, a.prompt, pdf=a.pdf, formate=formate,
                                  ziel_ordner=a.ausgabe, melden=melden, anzeige=fortschritt,
                                  md=not a.ohne_md, **kwargs)
            return True
        except SeitenFehler as f:
            melden(f"✖ {beschriftung(d)}: {f}")
        except Abgebrochen:
            raise
        except Exception as f:
            melden(f"✖ Fehler bei {beschriftung(d)}: {f}")
        fehler += 1
        return False

    def beschriftung(d) -> str:
        return a.zusammen if isinstance(d, list) else d.name

    try:
        belegt = set()
        if a.zusammen:
            # Reihenfolge wie angegeben, danach die offenen Dateien des Eingabe-Ordners
            alle = [Path(d) for d in a.dateien]
            if a.eingabe:
                alle += [d for d, _ in offene_dateien(a.eingabe, a.ausgabe, a.alle)]
            if not alle:
                melden("Keine offenen Dateien zum Zusammenfügen.")
            else:
                eine_datei(alle, name=a.zusammen)
            a.dateien, a.eingabe = [], None
        for d in a.dateien:
            eine_datei(Path(d), belegt=belegt)
        if a.eingabe:
            if a.ueberwachen:
                melden(f"Überwache {a.eingabe} – beenden mit Strg+C")
            wartezimmer, fehlgeschlagen = None, {}   # Datei -> Änderungszeit beim Fehlschlag
            while True:
                for d, name in offene_dateien(a.eingabe, a.ausgabe, a.alle, wartezimmer):
                    mtime = d.stat().st_mtime
                    if fehlgeschlagen.get(d) == mtime:
                        continue                     # nicht endlos wiederholen
                    if not eine_datei(d, name=name):
                        fehlgeschlagen[d] = mtime
                if not a.ueberwachen:
                    break
                # Beim ersten Durchlauf alles Vorhandene, danach nur Neues, das
                # sich nicht mehr verändert (fertig kopiert/gescannt)
                a.alle, wartezimmer = False, wartezimmer or Wartezimmer()
                time.sleep(a.intervall)
    except KeyboardInterrupt:
        konsole.beenden()
        _melden("⏹ Abgebrochen.")
        sys.exit(130)
    konsole.beenden()

    if len(a.dateien) == 1 and not a.eingabe and letztes and letztes[0].suffix == ".md":
        print(letztes[0].read_text(encoding="utf-8"))
    sys.exit(1 if fehler else 0)


if __name__ == "__main__":
    main()
