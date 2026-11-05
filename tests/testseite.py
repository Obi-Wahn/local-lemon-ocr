"""
Hilfen für die Tests: erzeugt eine Testseite mit bekanntem Text, macht daraus
einen „Scan“ (Bild-PDF ohne Textebene) und baut eine gefälschte grounding-Antwort
mit absichtlich ungenauen Boxen – so lässt sich ocr.py ohne Lemonade testen.
"""
import sys
from pathlib import Path

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ocr  # noqa: E402

BREITE, HOEHE = 595, 842   # A4 in Punkt

ABSAETZE = [
    ("title", ["Beispiel für eine Testseite"]),
    ("text", ["Dies ist ein neutraler Beispieltext, der über",
              "mehrere Zeilen läuft und von der Texterkennung",
              "wieder an der richtigen Stelle landen soll."]),
    ("text", ["Die Datei bericht_2024_final.txt kostet 12,50 €",
              "und die Rechnung 5 * 3 * 2 ergibt dreißig."]),
    ("list", ["- Erster Punkt der Liste",
              "- Zweiter Punkt mit Umlauten: Ärger, Öl, Übung"]),
]
BILD_BOX = fitz.Rect(320, 560, 520, 720)


def original_erzeugen() -> fitz.Document:
    """Digitale Testseite mit echtem Text und einem grauen „Bild“."""
    doc = fitz.open()
    seite = doc.new_page(width=BREITE, height=HOEHE)
    y = 90
    for typ, zeilen in ABSAETZE:
        groesse = 20 if typ == "title" else 12
        for z in zeilen:
            seite.insert_text((60, y), z.lstrip("- "), fontsize=groesse,
                              fontname="ocrfont", fontfile=ocr.SCHRIFT_DATEI)
            y += groesse * 1.45
        y += 18
    seite.draw_rect(BILD_BOX, color=(0.2, 0.2, 0.2), fill=(0.5, 0.5, 0.5))
    return doc


def scan_erzeugen(original: fitz.Document, ziel: Path, drehung: int = 0) -> Path:
    """Rastert die Seite (150 dpi) und speichert sie als Bild-PDF ohne Text.
    Mit drehung wird eine gedrehte PDF-Seite (page.rotation) erzeugt, die in der
    Anzeige genauso aussieht wie das Original."""
    png = original[0].get_pixmap(dpi=150, alpha=False).tobytes("png")
    scan = fitz.open()
    if drehung in (90, 270):
        seite = scan.new_page(width=HOEHE, height=BREITE)
    else:
        seite = scan.new_page(width=BREITE, height=HOEHE)
    seite.set_rotation(drehung)
    seite.insert_image(seite.rect * seite.derotation_matrix, stream=png, rotate=drehung)
    scan.save(ziel)
    return ziel


def zeilen_des_originals(original: fitz.Document):
    """Liefert je Absatz (typ, Rect, [Zeilentexte]) aus dem echten Text."""
    woerter = original[0].get_text("words")
    zeilen = {}
    for w in woerter:
        zeilen.setdefault((w[5], w[6]), []).append(w)
    zeilen = [fitz.Rect(min(w[0] for w in ws), min(w[1] for w in ws),
                        max(w[2] for w in ws), max(w[3] for w in ws)) for ws in zeilen.values()]
    zeilen.sort(key=lambda r: r.y0)
    ergebnis, i = [], 0
    for typ, texte in ABSAETZE:
        rects = zeilen[i:i + len(texte)]
        i += len(texte)
        box = fitz.Rect(rects[0])
        for r in rects[1:]:
            box |= r
        ergebnis.append((typ, box, texte))
    return ergebnis


def fake_antwort(original: fitz.Document) -> str:
    """grounding-Antwort wie von LightOnOCR-3, aber mit ungenauen Boxen:
    um einige Promille verschoben und vergrößert."""
    def norm(r: fitz.Rect, dx, dy, plus):
        return (round((r.x0 - plus) / BREITE * 1000 + dx), round((r.y0 - plus) / HOEHE * 1000 + dy),
                round((r.x1 + plus) / BREITE * 1000 + dx), round((r.y1 + plus) / HOEHE * 1000 + dy))
    teile = []
    for n, (typ, box, texte) in enumerate(zeilen_des_originals(original)):
        x0, y0, x1, y1 = norm(box, (-1) ** n * 6, (-1) ** (n + 1) * 4, 5)
        inhalt = "\n".join(texte)
        if typ == "title":
            inhalt = "# " + inhalt
        elif n == 2:
            inhalt = inhalt.replace("12,50 €", "**12,50 €**")
        # Inhalt mal in derselben Zeile, mal darunter (beides kommt vor)
        trenner = " " if n % 2 else "\n"
        teile.append(f"![{typ}]({x0},{y0},{x1},{y1}){trenner}{inhalt}")
    x0, y0, x1, y1 = norm(BILD_BOX, 3, -3, 2)
    teile.append(f"![image]({x0},{y0},{x1},{y1})\nGraues Rechteck als Beispielbild")
    return "\n\n".join(teile)


def woerter_vergleichen(original: fitz.Document, ergebnis_seite: fitz.Page):
    """Mittlere IoU der Wortboxen (Original gegen OCR-Textebene) und die Anzahl
    der Wörter, die gefunden wurden. Vergleich in Anzeige-Koordinaten."""
    rot = ergebnis_seite.rotation_matrix
    erg = [(fitz.Rect(w[:4]) * rot, w[4]) for w in ergebnis_seite.get_text("words")]
    orig = [(fitz.Rect(w[:4]), w[4]) for w in original[0].get_text("words")]
    werte, frei = [], list(erg)
    for r, t in orig:
        kandidaten = [(i, e) for i, e in enumerate(frei) if e[1] == t]
        if not kandidaten:
            continue
        i, (er, _) = max(kandidaten, key=lambda k: _iou(r, k[1][0]))
        werte.append(_iou(r, er))
        frei.pop(i)
    return (sum(werte) / len(werte) if werte else 0.0), len(werte), len(orig)


def _iou(a: fitz.Rect, b: fitz.Rect) -> float:
    schnitt = fitz.Rect(a) & b
    if schnitt.is_empty:
        return 0.0
    f = schnitt.width * schnitt.height
    return f / (a.width * a.height + b.width * b.height - f)
