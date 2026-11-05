# CLAUDE.md – Projektkontext für local-lemon-ocr

Sprache im Projekt: **Deutsch** (Code-Kommentare, Ausgaben, Doku).

## Was das Projekt macht
Lokale OCR für PDFs und Bilder über einen **Lemonade Server** (AMD, OpenAI-kompatible API, llama.cpp-Backend) mit dem Vision-Modell **LightOnOCR-3** (GGUF, z. B. `prithivMLmods/LightOnOCR-3-4B-GGUF`, Q8_0 + mmproj).

- `ocr.py`: Kernlogik und Kommandozeile (`python ocr.py <Modell> <Datei> [Prompt] [--pdf] [--docx] [--odt]`)
- `ocr_gui.pyw`: Tkinter-Oberfläche, importiert `ocr.py` aus demselben Ordner und nutzt `ocr.verbinden()`, `ocr.modelle_auflisten()` und `ocr.verarbeiten()`
- `docs/Anleitung.docx`: ausführliche Anleitung für Endnutzer (Windows 11)

Abhängigkeiten: `openai`, `pymupdf` (Import als `import pymupdf as fitz`), Python ≥ 3.10, optional Pandoc.

## Wichtige Erkenntnisse (nicht wieder rückgängig machen)
- **Denkmodus:** LightOnOCR-3 basiert auf Qwen3.5. Ohne Gegenmaßnahme landet die Ausgabe in `reasoning_content`, und `content` bleibt leer. Deshalb wird bei jeder Anfrage `extra_body={"chat_template_kwargs": {"enable_thinking": False}}` gesendet und notfalls auf `reasoning_content` zurückgegriffen. Serverseitig empfehlen wir in Lemonade `--reasoning off` (bzw. `--reasoning-format none`) und Kontext 16384.
- **Port:** Neuere Lemonade-Versionen nutzen standardmäßig `13305` (ältere 8000). Die Basis-URL ist `http://localhost:13305/api/v1`.
- **Prompts:** Bei LightOnOCR-3 liefert `""` reine Transkription (Markdown), `"grounding"` Blöcke mit Positionen. Andere Prompts sind laut LightOn „out of distribution“. PaddleOCR-VL braucht `"OCR:"`, `"Table Recognition:"` usw. Der Standard im Skript ist `grounding`.
- **Grounding-Format:** `![label](x0,y0,x1,y1)` mit Koordinaten normiert auf 0–1000. Der Inhalt folgt **in derselben Zeile oder darunter**, deshalb parst `bloecke_parsen()` per `finditer` über den ganzen Text. Labels: text, title, list, header, footer, page_number, footnote, caption, formula, code, table, image, chart, header_image, footer_image, aside_text; ein `+`-Suffix steht für Fortsetzung. Tabellen und Diagramme kommen oft als HTML (`<table>`).
- **Bildauflösung:** längste Kante 2048 px (Empfehlung von LightOn). Sampling: temperature 0.2, top_p 0.9.
- **Textebene im PDF** (`textebene_einfuegen`): Die Boxen des Modells sind ungenau. Deshalb werden pro Box die echten Textzeilen über ein Zeilenprofil dunkler Pixel im Graustufenrender gesucht (`zeilen_finden`). Der Text wird auf die Zeilen verteilt (`text_verteilen`) und als `render_mode=3` mit horizontalem `morph` exakt auf die Zeilenbreite gestreckt. Die Schriftgröße beträgt ≈ Median-Zeilenhöhe / 0,95, die Grundlinie liegt bei Zeilenmitte + 0,255 × Schriftgröße. Notlösung ohne erkannte Zeilen: `_block_einpassen`.
- **Schrift:** eine Unicode-TTF (Windows: Arial, Linux: DejaVu), sonst Helvetica. Helvetica kann kein €. Vor dem Speichern läuft `doc.subset_fonts()`.
- **Bilder:** Blöcke vom Typ image/chart/header_image/footer_image werden mit 300 dpi und 4 pt Rand ausgeschnitten, in `<name>.ocr-bilder/` gespeichert, im PDF deckungsgleich als eigenes Bildobjekt eingefügt und in Markdown als `![Beschreibung](<pfad>)` referenziert. Die spitzen Klammern sind nötig wegen Leerzeichen.
- **Office-Export:** Markdown → HTML (`--mathml`) → DOCX/ODT über Pandoc, mit `cwd` und `--resource-path` = Ordner der Markdown-Datei. Der Umweg über HTML erhält HTML-Tabellen, die ein direkter Markdown→DOCX-Weg verwerfen würde.
- **Ausgabenamen:** `<stem>.ocr.md/.pdf/.docx/.odt`. Nicht `with_suffix()` auf `.ocr` anwenden, das ersetzt `.ocr`.

## Testen ohne Lemonade
Es gibt keinen echten Server in CI. Getestet wird, indem man `ocr.ocr` durch eine Funktion ersetzt, die eine feste grounding-Antwort liefert:
1. Testseite mit bekanntem Text erzeugen, als Bild rastern und als Bild-PDF speichern („Scan“).
2. Absichtlich ungenaue Boxen in der Fake-Antwort verwenden.
3. Wortpositionen im Ergebnis-PDF (`get_text("words")`) per IoU mit dem Original vergleichen. Aktueller Stand: ≈ 0,80 im Mittel, waagerecht ±1–2 pt.
4. Die GUI headless testen: `xvfb-run` mit einem Python, das tkinter hat.

## Grenzen / mögliche nächste Schritte
- Gedrehte PDF-Seiten (`page.rotation`) werden nicht behandelt.
- Die Textebene ist zeilen-, nicht wortgenau.
- Mögliche Erweiterungen: Ordnerüberwachung (Hotfolder), Paperless-Integration, englische Übersetzung von README und Oberfläche.

## Datenschutz
Keine persönlichen Daten in Code, Doku, Tests oder Beispielen. Beispieldateien nur neutral benennen (z. B. `Beispiel.pdf`). Die `.gitignore` schließt PDFs und Bilder außerhalb von `docs/` aus.
