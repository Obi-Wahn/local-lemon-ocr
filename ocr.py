"""
OCR über einen lokalen Lemonade Server (OpenAI-kompatible API).

Installation:   pip install openai pymupdf
Aufruf:         python ocr.py <Modellname> <Bild-oder-PDF> [Prompt] [--pdf] [--docx] [--odt]

Beispiele:
  python ocr.py user.LightOnOCR-3-4B-GGUF-Q8_0 scan.png
  python ocr.py user.LightOnOCR-3-4B-GGUF-Q8_0 arbeitsblatt.pdf --pdf
  python ocr.py user.LightOnOCR-3-4B-GGUF-Q8_0 arbeitsblatt.pdf --pdf --docx
  python ocr.py user.PaddleOCR-VL-1.6-GGUF scan.png "OCR:"

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

Den genauen Modellnamen zeigt:  lemonade list
"""
import base64
import html
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pymupdf as fitz  # pip-Paket "pymupdf"
from openai import OpenAI

BASE_URL = "http://localhost:13305/api/v1"  # Port prüfen mit: lemonade config
MAX_KANTE = 2048                             # längste Bildkante in Pixeln

client = None


def verbinden(base_url: str = BASE_URL, api_key: str | None = None):
    """Verbindung zu Lemonade einrichten. Ohne Key wird LEMONADE_API_KEY
    genommen, falls gesetzt – sonst ein Platzhalter."""
    global client
    client = OpenAI(base_url=base_url,
                    api_key=api_key or os.environ.get("LEMONADE_API_KEY", "lemonade"),
                    timeout=600)
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
        return fitz.open(pfad)
    bild = fitz.open(pfad)
    return fitz.open("pdf", bild.convert_to_pdf())


def seite_als_png(seite: fitz.Page) -> bytes:
    zoom = MAX_KANTE / max(seite.rect.width, seite.rect.height)
    return seite.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")


# -------------------------------------------------------------------- OCR ---

def ocr(modell: str, png: bytes, prompt: str) -> str:
    if client is None:
        verbinden()
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    inhalt = [{"type": "image_url", "image_url": {"url": data_url}}]
    if prompt:
        inhalt.append({"type": "text", "text": prompt})
    antwort = client.chat.completions.create(
        model=modell,
        messages=[{"role": "user", "content": inhalt}],
        temperature=0.2,          # Empfehlung von LightOn
        top_p=0.9,
        max_tokens=8192,
        # "Denkmodus" der Qwen3.5-Vorlage abschalten, sonst landet der Text
        # in reasoning_content und content bleibt leer
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    msg = antwort.choices[0].message
    text = msg.content or ""
    if not text.strip():
        # Fallback: Text steckt trotzdem im Reasoning-Feld
        text = getattr(msg, "reasoning_content", None) \
            or (msg.model_extra or {}).get("reasoning_content", "") or ""
    if antwort.choices[0].finish_reason == "length":
        text += "\n\n[WARNUNG: Ausgabe abgeschnitten – max_tokens/Kontext erhöhen]"
    return text


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


def als_klartext(inhalt: str) -> str:
    """Markdown/HTML zu schlichtem Text für die unsichtbare Textebene."""
    t = re.sub(r"</t[dh]>", " ", inhalt)
    t = re.sub(r"</tr>|<br\s*/?>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)      # Überschriften
    t = re.sub(r"(\*\*|__|\*|_|`)(.+?)\1", r"\2", t)          # Fett, kursiv, Code
    t = re.sub(r"^\s*\|?[-:| ]+\|?\s*$", "", t, flags=re.M)   # Markdown-Tabellenlinien
    t = t.replace("|", " ")
    return html.unescape(re.sub(r"[ \t]+", " ", t)).strip()


# ----------------------------------------------------- Durchsuchbares PDF ---

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
    seite.insert_textbox(rect, text, fontsize=lo, render_mode=3, **SCHRIFT)


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
            grundlinie = fitz.Point(r.x0, (r.y0 + r.y1) / 2 + groesse * 0.255)
            # Text horizontal genau auf die Zeilenbreite strecken/stauchen
            seite.insert_text(grundlinie, zeile, fontsize=groesse, render_mode=3,
                              morph=(grundlinie, fitz.Matrix(r.width / laenge, 1)), **SCHRIFT)


# ------------------------------------------------------- Bilder ausschneiden ---

def bilder_ausschneiden(seite: fitz.Page, bloecke, ordner: Path, seitennr: int):
    """Schneidet Bilder und Diagramme an ihren Boxen aus der Seite aus.
    Liefert {Blockindex: (Dateipfad, Rect, PNG-Bytes)}."""
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
        ordner.mkdir(exist_ok=True)
        datei = ordner / f"seite{seitennr:02d}-bild{len(ergebnis) + 1:02d}.png"
        datei.write_bytes(png)
        ergebnis[i] = (datei, rect, png)
    return ergebnis


# ----------------------------------------------------------- Office-Export ---

def office_export(md_datei: Path, formate, melden=_melden):
    """Markdown -> HTML -> DOCX/ODT über Pandoc.
    Der Umweg über HTML erhält Tabellen, die das Modell als HTML ausgibt;
    --mathml macht aus LaTeX-Formeln echte Office-Formeln."""
    pandoc = shutil.which("pandoc")
    if not pandoc:
        melden("Pandoc nicht gefunden – installieren mit: winget install JohnMacFarlane.Pandoc")
        return []
    html_text = subprocess.run(
        [pandoc, "-f", "markdown", "-t", "html", "--mathml", str(md_datei)],
        capture_output=True, text=True, encoding="utf-8", check=True,
        cwd=md_datei.parent,
    ).stdout
    erzeugt = []
    for fmt in formate:
        ziel = md_datei.with_suffix("." + fmt)   # scan.ocr.md -> scan.ocr.docx
        subprocess.run([pandoc, "-f", "html", "-o", str(ziel.resolve()),
                        "--resource-path", str(md_datei.parent.resolve())],
                       input=html_text, text=True, encoding="utf-8", check=True,
                       cwd=md_datei.parent)
        melden(f"Gespeichert: {ziel}")
        erzeugt.append(ziel)
    return erzeugt


# ------------------------------------------------------------------- Main ---

class Abgebrochen(Exception):
    pass


def verarbeiten(modell: str, datei: Path, prompt: str = "grounding", pdf: bool = False,
                formate=(), ziel_ordner: Path | None = None, melden=_melden,
                fortschritt=None, abbrechen=None) -> list[Path]:
    """Eine Datei komplett verarbeiten. Liefert die erzeugten Dateien.
    fortschritt(seite, gesamt) und abbrechen() -> bool sind optional (für die GUI)."""
    datei = Path(datei)
    ordner = Path(ziel_ordner) if ziel_ordner else datei.parent
    ordner.mkdir(parents=True, exist_ok=True)
    basis = str(ordner / (datei.stem + ".ocr"))   # + ".md", ".pdf" …
    bild_ordner = ordner / (datei.stem + ".ocr-bilder")

    doc = seiten_laden(datei)
    teile, erzeugt = [], []
    for nr, seite in enumerate(doc, start=1):
        if abbrechen and abbrechen():
            raise Abgebrochen()
        melden(f"→ {datei.name}: Seite {nr}/{doc.page_count} …")
        if fortschritt:
            fortschritt(nr - 1, doc.page_count)
        antwort = ocr(modell, seite_als_png(seite), prompt)
        bloecke = bloecke_parsen(antwort)
        bilder = bilder_ausschneiden(seite, bloecke, bild_ordner, nr)
        links = {i: f"{bild_ordner.name}/{d.name}" for i, (d, _, _) in bilder.items()}
        teile.append(f"## Seite {nr}\n\n{als_markdown(bloecke, links)}")
        if pdf:
            # Bilder als eigene Objekte deckungsgleich an die Originalstelle legen
            for _, rect, png in bilder.values():
                seite.insert_image(rect, stream=png)
            textebene_einfuegen(seite, bloecke)
        if bilder:
            melden(f"   {len(bilder)} Bild(er) gespeichert in {bild_ordner}")
    if fortschritt:
        fortschritt(doc.page_count, doc.page_count)

    md_ziel = Path(basis + ".md")
    md_ziel.write_text("\n\n".join(teile), encoding="utf-8")
    melden(f"Gespeichert: {md_ziel}")
    erzeugt.append(md_ziel)

    if pdf:
        pdf_ziel = Path(basis + ".pdf")
        if SCHRIFT_DATEI:
            doc.subset_fonts()   # nur benutzte Zeichen einbetten -> kleine Datei
        doc.save(pdf_ziel, garbage=3, deflate=True)
        melden(f"Gespeichert: {pdf_ziel}")
        erzeugt.append(pdf_ziel)

    if formate:
        erzeugt += office_export(md_ziel, formate, melden)
    return erzeugt


def main():
    args = sys.argv[1:]
    schalter = {a for a in args if a in ("--pdf", "--docx", "--odt")}
    args = [a for a in args if a not in schalter]
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)
    modell, datei = args[0], Path(args[1])
    # Standard: grounding-Modus von LightOnOCR-3 – liefert Positionen für
    # Textebene und Bilder. Für andere Modelle eigenen Prompt angeben.
    prompt = args[2] if len(args) > 2 else "grounding"
    formate = [f for f in ("docx", "odt") if f"--{f}" in schalter]
    erzeugt = verarbeiten(modell, datei, prompt, pdf="--pdf" in schalter, formate=formate)
    print(erzeugt[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
