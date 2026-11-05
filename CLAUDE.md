# CLAUDE.md – Projektkontext für local-lemon-ocr

Sprache im Projekt: **Deutsch** (Code-Kommentare, Ausgaben, Doku).

## Was das Projekt macht
Lokale OCR für PDFs und Bilder über einen **Lemonade Server** (AMD, OpenAI-kompatible API, llama.cpp-Backend) mit dem Vision-Modell **LightOnOCR-3** (GGUF, z. B. `prithivMLmods/LightOnOCR-3-4B-GGUF`, Q8_0 + mmproj).

- `ocr.py`: Kernlogik und Kommandozeile mit `argparse` (`python ocr.py <Modell> [Datei ...] [-e EINGABE] [-o AUSGABE] [--ueberwachen] [-p PROMPT] [--pdf] [--docx] [--odt]`). Die alte Form mit dem Prompt als drittem Positionsargument bleibt erlaubt, wenn es keine Datei ist und nicht wie ein Dateiname aussieht.
- `ocr_gui.pyw`: Tkinter-Oberfläche, importiert `ocr.py` aus demselben Ordner und nutzt `ocr.verbinden()`, `ocr.modelle_auflisten()`, `ocr.verarbeiten()`, `ocr.offene_dateien()` und `ocr.Wartezimmer`
- `tests/`: `unittest`-Tests ohne Server (`testseite.py` = Testseite, Scan und Fake-grounding-Antwort; `test_ocr.py`; `test_gui.py` braucht Display)
- `docs/Anleitung.docx`: ausführliche Anleitung für Endnutzer (Windows 11)

Abhängigkeiten: `openai`, `pymupdf` (Import als `import pymupdf as fitz`), Python ≥ 3.10, optional Pandoc.

## Wichtige Erkenntnisse (nicht wieder rückgängig machen)
- **Denkmodus:** LightOnOCR-3 basiert auf Qwen3.5. Ohne Gegenmaßnahme landet die Ausgabe in `reasoning_content`, und `content` bleibt leer. Deshalb wird bei jeder Anfrage `extra_body={"chat_template_kwargs": {"enable_thinking": False}}` gesendet und notfalls auf `reasoning_content` zurückgegriffen. Serverseitig empfehlen wir in Lemonade `--reasoning off` (bzw. `--reasoning-format none`) und Kontext 16384.
- **Port:** Neuere Lemonade-Versionen nutzen standardmäßig `13305` (ältere 8000). Die Basis-URL ist `http://localhost:13305/api/v1`, änderbar über `LEMONADE_BASE_URL` oder `--url`.
- **Prompts:** Bei LightOnOCR-3 liefert `""` reine Transkription (Markdown), `"grounding"` Blöcke mit Positionen. Andere Prompts sind laut LightOn „out of distribution“. PaddleOCR-VL braucht `"OCR:"`, `"Table Recognition:"` usw. Der Standard im Skript ist `grounding`.
- **Grounding-Format:** `![label](x0,y0,x1,y1)` mit Koordinaten normiert auf 0–1000. Der Inhalt folgt **in derselben Zeile oder darunter**, deshalb parst `bloecke_parsen()` per `finditer` über den ganzen Text. Labels: text, title, list, header, footer, page_number, footnote, caption, formula, code, table, image, chart, header_image, footer_image, aside_text; ein `+`-Suffix steht für Fortsetzung. Tabellen und Diagramme kommen oft als HTML (`<table>`).
- **Bildauflösung:** längste Kante 2048 px (Empfehlung von LightOn). Sampling: temperature 0.2, top_p 0.9.
- **Textebene im PDF** (`textebene_einfuegen`): Die Boxen des Modells sind ungenau. Deshalb werden pro Box die echten Textzeilen über ein Zeilenprofil dunkler Pixel im Graustufenrender gesucht (`zeilen_finden`). Der Text wird auf die Zeilen verteilt (`text_verteilen`) und als `render_mode=3` mit horizontalem `morph` exakt auf die Zeilenbreite gestreckt. Die Schriftgröße beträgt ≈ Median-Zeilenhöhe / 0,95, die Grundlinie liegt bei Zeilenmitte + 0,255 × Schriftgröße. Notlösung ohne erkannte Zeilen: `_block_einpassen`.
- **Schrift:** eine Unicode-TTF (Windows: Arial, Linux: DejaVu), sonst Helvetica. Helvetica kann kein €. Vor dem Speichern läuft `doc.subset_fonts()`.
- **Bilder:** Blöcke vom Typ image/chart/header_image/footer_image werden mit 300 dpi und 4 pt Rand ausgeschnitten, in `<name>.ocr-bilder/` gespeichert, im PDF deckungsgleich als eigenes Bildobjekt eingefügt und in Markdown als `![Beschreibung](<pfad>)` referenziert. Die spitzen Klammern sind nötig wegen Leerzeichen.
- **Office-Export:** Markdown → HTML (`--mathml`) → DOCX/ODT über Pandoc, mit `cwd` und `--resource-path` = Ordner der Markdown-Datei. Der Umweg über HTML erhält HTML-Tabellen, die ein direkter Markdown→DOCX-Weg verwerfen würde.
- **Ausgabenamen:** `<stem>.ocr.md/.pdf/.docx/.odt`. Nicht `with_suffix()` auf `.ocr` anwenden, das ersetzt `.ocr`. Bei gleichem Stamm im selben Durchlauf vergibt `ausgabe_name()` über die Menge `belegt` `<stem>-<endung>`, danach `<stem>-2` usw. (Vergleich ohne Groß-/Kleinschreibung wegen Windows). Dateien, deren Stamm auf `.ocr` endet, gelten als eigene Ergebnisse und werden im Eingabe-Ordner ignoriert.
- **Gedrehte Seiten:** `seite.rect`, `get_pixmap()` und `clip` arbeiten in Anzeige-Koordinaten, `insert_text`/`insert_textbox`/`insert_image` in ungedrehten. Deshalb Punkte/Rechtecke mit `seite.derotation_matrix` umrechnen und `rotate=seite.rotation` setzen. Das `morph`-Strecken läuft bei 90°/270° über `Matrix(1, f)` statt `Matrix(f, 1)`. Ausprobiert, Test in `test_gedrehte_seiten`.
- **Textebene, Klartext:** `als_klartext()` entfernt `*`, `_`, `` ` `` nur, wenn sie ein Wort umschließen (Lookarounds). Sonst würden `datei_name.txt` und `5 * 3` verstümmelt.
- **Vorhandener Text:** Seiten, die schon Text haben (`hat_textebene`), bekommen keine zweite Textebene.
- **Fehler pro Seite:** Der OpenAI-Client wiederholt selbst (`max_retries=2`). `seitenfehler()` entscheidet: Zeitüberschreitung und andere HTTP-Statusfehler betreffen nur die Seite, also weiter mit `[FEHLER: Seite nicht erkannt …]` im Markdown und am Ende `SeitenFehler`. Verbindungsfehler, 401/403/404 brechen die Datei ab. Bei Abbruch/Fehler (auch Strg+C) werden fertige Seiten mit `[UNVOLLSTÄNDIG …]` gespeichert. Beide Markierungen sorgen dafür, dass `ist_erledigt()` die Datei nicht als fertig ansieht.
- **Eingabe-Ordner/Überwachung:** `offene_dateien()` liefert (Datei, Name) ohne Unterordner. Erledigt heißt: vollständiges `<name>.ocr.md` neuer als die Datei. Beim Überwachen wird eine neue Datei erst genommen, wenn `Wartezimmer` zweimal dieselbe Größe und Änderungszeit sieht. Fehlgeschlagene Dateien werden pro Sitzung nur erneut versucht, wenn sich ihre Änderungszeit ändert.
- **Pandoc-Fehler:** `_pandoc()` gibt Pandocs stderr als `RuntimeError("Pandoc-Fehler: …")` weiter.
- **Kommandozeile:** stdout/stderr werden auf UTF-8 umgestellt (Windows-Umleitung in Dateien). Rückgabewerte: 0 ok, 1 Fehler, 130 Strg+C.

## Testen ohne Lemonade
Es gibt keinen echten Server in CI. Die Tests liegen in `tests/` und laufen mit `python -m unittest discover -s tests -v` (GUI-Test: `xvfb-run` davor). Getestet wird, indem man `ocr.ocr` durch eine Funktion ersetzt, die eine feste grounding-Antwort liefert:
1. Testseite mit bekanntem Text erzeugen, als Bild rastern und als Bild-PDF speichern („Scan“).
2. Absichtlich ungenaue Boxen in der Fake-Antwort verwenden.
3. Wortpositionen im Ergebnis-PDF (`get_text("words")`) per IoU mit dem Original vergleichen. Aktueller Stand: ≈ 0,85 im Mittel (auch bei 90/180/270° gedrehten Seiten), waagerecht ±1–2 pt. Achtung: `get_text("words")` liefert ungedrehte Koordinaten, für den Vergleich mit `rotation_matrix` umrechnen.
4. Die GUI headless testen: `xvfb-run` mit einem Python, das tkinter hat (z. B. `apt install python3-tk`, dann `/usr/bin/python3` statt eines eigenen Python ohne tkinter).
5. openai-Fehler für Tests ohne HTTP-Anfrage erzeugen (`klasse.__new__` + `Exception.__init__`), weil die Konstruktoren je nach openai-Version unterschiedliche `request`-Typen erwarten.

## Grenzen / mögliche nächste Schritte
- Die Textebene ist zeilen-, nicht wortgenau.
- Der Eingabe-Ordner wird ohne Unterordner durchsucht. Verarbeitete Originale bleiben liegen (kein Verschieben).
- `docs/Anleitung.docx` beschreibt die neuen Ordner-Optionen noch nicht.
- Mögliche Erweiterungen: Paperless-Integration, englische Übersetzung von README und Oberfläche.

## Datenschutz
Keine persönlichen Daten in Code, Doku, Tests oder Beispielen. Beispieldateien nur neutral benennen (z. B. `Beispiel.pdf`). Die `.gitignore` schließt PDFs, Bilder, Office-Dateien, `*.ocr.md`, `*.ocr-bilder/`, `Eingang/`, `Ergebnisse/` und `.ocr-gui.json` aus, erlaubt aber alles in `docs/`. Tests erzeugen ihre Dateien nur in temporären Ordnern.
