"""
Startprogramm: startet den Lemonade Server (falls er noch nicht läuft), wartet,
bis er antwortet, und öffnet dann die Oberfläche ocr_gui.pyw.

Start:   Doppelklick auf starten.pyw   (Windows)
         python3 starten.pyw            (Linux, oder über die Verknüpfung, siehe unten)

Optionen:
  --stoppen          Server beim Schließen der Oberfläche wieder beenden
                     (nur, wenn das Startprogramm ihn gestartet hat)
  --warten SEK       höchstens so lange auf den Server warten (Standard 120)
  --url URL          Server-Adresse (sonst aus den Einstellungen der Oberfläche,
                     LEMONADE_BASE_URL oder http://localhost:13305/api/v1)
  --server-befehl B  eigener Befehl zum Starten des Servers (auch: LEMONADE_START)
  --verknuepfung     Linux: Eintrag „OCR mit Lemonade“ im Anwendungsmenü anlegen
                     (übernimmt --stoppen und den aktuellen Python, z. B. aus einer venv)

So wird der Server gestartet (das Erste, was passt):
  1. eigener Befehl (--server-befehl bzw. LEMONADE_START)
  2. Linux: systemd-Benutzerdienst lemond   (systemctl --user start lemond, Fedora-RPM)
  3. Linux: systemd-Systemdienst lemond     (systemctl start lemond, Ubuntu/Mint-PPA,
                                             fragt ggf. nach dem Passwort)
  4. Programm lemond bzw. „lemonade-server serve“ (Windows, Snap, ältere Versionen)
Einen Autostart richtet das Startprogramm nicht ein.

Läuft der Server auf einem anderen Rechner, wird nichts gestartet, nur kurz
geprüft, ob er erreichbar ist.
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ORDNER = Path(__file__).resolve().parent
GUI = ORDNER / "ocr_gui.pyw"
EINSTELLUNGEN = Path.home() / ".ocr-gui.json"   # dieselbe Datei wie in ocr_gui.pyw
STANDARD_URL = "http://localhost:13305/api/v1"  # wie ocr.BASE_URL
WARTEN = 120          # Sekunden, bis das Warten aufgegeben wird
DESKTOP_DATEI = Path.home() / ".local/share/applications/local-lemon-ocr.desktop"
LOKAL = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


# ------------------------------------------------------------ Server-Adresse ---
def server_url(url: str | None = None) -> str:
    """Adresse wie in der Oberfläche: Option, sonst gespeicherte Einstellung,
    sonst LEMONADE_BASE_URL, sonst der Standard."""
    if url:
        return url
    try:
        gespeichert = json.loads(EINSTELLUNGEN.read_text(encoding="utf-8")).get("url", "").strip()
    except Exception:
        gespeichert = ""
    return gespeichert or os.environ.get("LEMONADE_BASE_URL", STANDARD_URL)


def ist_lokal(url: str) -> bool:
    return (urlparse(url).hostname or "localhost").lower() in LOKAL


def erreichbar(url: str, zeit: float = 2) -> bool:
    """True, sobald der Server überhaupt antwortet (auch mit 401 o. ä.)."""
    anfrage = urllib.request.Request(url.rstrip("/") + "/models")
    key = os.environ.get("LEMONADE_API_KEY")
    if key:
        anfrage.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(anfrage, timeout=zeit):
            return True
    except urllib.error.HTTPError:
        return True          # Server antwortet, nur nicht mit 200
    except Exception:
        return False


# ------------------------------------------------------------ Server starten ---
def _ausfuehren(befehl: list[str], zeit: float = 10) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(befehl, capture_output=True, text=True, timeout=zeit)
    except (OSError, subprocess.SubprocessError):
        return None


def _systemd_dienst(benutzer: bool) -> bool:
    if not shutil.which("systemctl"):
        return False
    befehl = ["systemctl"] + (["--user"] if benutzer else []) + ["cat", "lemond.service"]
    ergebnis = _ausfuehren(befehl)
    return ergebnis is not None and ergebnis.returncode == 0


def _programm_finden() -> list[str] | None:
    """Server-Programm ohne systemd: lemond oder „lemonade-server serve“."""
    for name in ("lemond", "/opt/bin/lemond"):
        pfad = shutil.which(name)
        if pfad:
            return [pfad]
    kandidaten = [shutil.which("lemonade-server")]
    if os.name == "nt":
        lokal = os.environ.get("LOCALAPPDATA", "")
        kandidaten += [os.path.join(lokal, "lemonade_server", "bin", n)
                       for n in ("lemonade-server.exe", "lemonade-server.bat")]
    for pfad in kandidaten:
        if pfad and os.path.exists(pfad):
            return [pfad, "serve"]
    return None


def startweg_finden(eigener_befehl: str | None = None) -> dict | None:
    """Wie der Server gestartet und gestoppt wird.
    art „systemd“: start/stopp sind kurze Befehle. art „prozess“: start läuft
    als Server weiter und wird zum Stoppen beendet."""
    eigener_befehl = eigener_befehl or os.environ.get("LEMONADE_START")
    if eigener_befehl:
        befehl = shlex.split(eigener_befehl, posix=os.name != "nt")
        return dict(art="prozess", name=eigener_befehl, start=befehl)
    if sys.platform.startswith("linux"):
        for benutzer in (True, False):
            if _systemd_dienst(benutzer):
                basis = ["systemctl"] + (["--user"] if benutzer else [])
                return dict(art="systemd", name=" ".join(basis + ["start", "lemond"]),
                            start=basis + ["start", "lemond"], stopp=basis + ["stop", "lemond"])
    befehl = _programm_finden()
    if befehl:
        return dict(art="prozess", name=" ".join(befehl), start=befehl)
    return None


def server_starten(weg: dict) -> subprocess.Popen:
    """Startbefehl im Hintergrund ausführen, ohne Konsolenfenster."""
    optionen = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    if weg["art"] == "systemd":
        optionen["stderr"] = subprocess.PIPE      # für die Fehlermeldung
        optionen["text"] = True
    else:
        optionen["stderr"] = subprocess.DEVNULL
        if os.name == "nt":
            optionen["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            optionen["start_new_session"] = True   # läuft weiter, wenn das Startprogramm endet
    return subprocess.Popen(weg["start"], **optionen)


def startfehler(weg: dict, prozess: subprocess.Popen) -> str | None:
    """Fehlermeldung, falls der Start schon gescheitert ist, sonst None."""
    code = prozess.poll()
    if code is None or (weg["art"] == "systemd" and code == 0):
        return None
    text = f"„{weg['name']}“ ist mit Fehlercode {code} beendet worden."
    if weg["art"] == "systemd" and prozess.stderr:
        meldung = prozess.stderr.read().strip()
        if meldung:
            text += "\n\n" + meldung
    return text


def server_stoppen(weg: dict, prozess: subprocess.Popen | None):
    if weg["art"] == "systemd":
        _ausfuehren(weg["stopp"], zeit=30)
    elif prozess and prozess.poll() is None:
        prozess.terminate()
        try:
            prozess.wait(15)
        except subprocess.TimeoutExpired:
            prozess.kill()


# --------------------------------------------------------------- Oberfläche ---
def gui_befehl(dateien=()) -> list[str]:
    python = sys.executable
    if os.name == "nt":   # pythonw, damit kein Konsolenfenster aufgeht
        fenster = Path(python).with_name("pythonw.exe")
        if fenster.exists():
            python = str(fenster)
    return [python, str(GUI), *map(str, dateien)]


def _desktop_argument(text: str) -> str:
    """Ein Argument für die Exec-Zeile einer .desktop-Datei (Freedesktop-Regeln:
    doppelte Anführungszeichen, \\ " ` $ maskieren, % verdoppeln; Backslashes
    werden in .desktop-Werten noch einmal maskiert)."""
    for zeichen in '\\"`$':
        text = text.replace(zeichen, "\\" + zeichen)
    return '"' + text.replace("\\", "\\\\").replace("%", "%%") + '"'


def verknuepfung_anlegen(stoppen: bool) -> Path:
    """Linux: .desktop-Datei für das Anwendungsmenü schreiben."""
    befehl = [sys.executable, str(ORDNER / "starten.pyw")] + (["--stoppen"] if stoppen else [])
    exec_zeile = " ".join(map(_desktop_argument, befehl)) + " %F"
    inhalt = "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=OCR mit Lemonade",
        "Comment=Texterkennung für PDFs und Bilder mit dem lokalen Lemonade Server",
        f"Exec={exec_zeile}",
        f"Path={ORDNER}",
        "Icon=scanner",
        "Terminal=false",
        "Categories=Office;Graphics;OCR;",
        "MimeType=application/pdf;image/png;image/jpeg;image/tiff;image/bmp;image/webp;",
        "",
    ])
    DESKTOP_DATEI.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP_DATEI.write_text(inhalt, encoding="utf-8")
    DESKTOP_DATEI.chmod(0o755)
    return DESKTOP_DATEI


def warten(url: str, weg: dict | None, prozess, frist: float) -> str | None:
    """Ohne Qt warten (z. B. im Test oder ohne Bildschirm). Liefert eine
    Fehlermeldung oder None, wenn der Server antwortet."""
    ende = time.monotonic() + frist
    while time.monotonic() < ende:
        if erreichbar(url):
            return None
        if weg and prozess:
            fehler = startfehler(weg, prozess)
            if fehler:
                return fehler
        time.sleep(1)
    return f"Der Server antwortet nach {int(frist)} Sekunden noch nicht."


def mit_fenster_warten(url: str, weg: dict | None, prozess, frist: float) -> str | None:
    """Kleines Wartefenster mit „Ohne Server weiter“. Liefert eine Fehlermeldung,
    "abgebrochen" oder None, wenn der Server antwortet."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QProgressDialog

    app = QApplication.instance() or QApplication(sys.argv)
    dialog = QProgressDialog("Lemonade Server wird gestartet …", "Ohne Server weiter", 0, 0)
    dialog.setWindowTitle("OCR mit Lemonade")
    dialog.setMinimumDuration(0)
    dialog.setMinimumWidth(420)
    beginn = time.monotonic()
    ergebnis = {"fehler": "abgebrochen"}

    def pruefen():
        sekunden = int(time.monotonic() - beginn)
        dialog.setLabelText(f"Lemonade Server wird gestartet … ({sekunden} s)\n{url}")
        if erreichbar(url, zeit=1):
            ergebnis["fehler"] = None
        elif weg and prozess and (fehler := startfehler(weg, prozess)):
            ergebnis["fehler"] = fehler
        elif sekunden >= frist:
            ergebnis["fehler"] = f"Der Server antwortet nach {int(frist)} Sekunden noch nicht."
        else:
            return
        dialog.canceled.disconnect()
        dialog.reset()
        app.quit()

    takt = QTimer(interval=1000, timeout=pruefen)
    dialog.canceled.connect(app.quit)
    takt.start()
    dialog.show()
    QTimer.singleShot(0, pruefen)
    app.exec()
    takt.stop()
    dialog.close()
    return ergebnis["fehler"]


def _ausgeben(text: str):
    if sys.stderr:            # unter pythonw gibt es kein stderr
        print(text, file=sys.stderr)


def melden(titel: str, text: str, mit_fenster: bool):
    _ausgeben(f"{titel}: {text}")
    if mit_fenster:
        from PySide6.QtWidgets import QApplication, QMessageBox
        QApplication.instance() or QApplication(sys.argv)
        QMessageBox.warning(None, titel, text)


HINWEIS = ("\n\nDie Oberfläche startet trotzdem. Den Server kannst du auch selbst starten "
           "(Linux: systemctl --user start lemond, Windows: Lemonade-App) und dann "
           "„Modelle laden“ klicken.")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Lemonade Server starten und die OCR-Oberfläche öffnen.")
    p.add_argument("dateien", nargs="*", help="Dateien, die in der Oberfläche vorgemerkt werden")
    p.add_argument("--stoppen", action="store_true",
                   help="Server beim Schließen der Oberfläche beenden (nur, wenn er hier gestartet wurde)")
    p.add_argument("--warten", type=float, default=WARTEN, metavar="SEK",
                   help=f"höchstens so lange auf den Server warten (Standard {WARTEN})")
    p.add_argument("--url", help="Server-Adresse (Standard: wie in der Oberfläche)")
    p.add_argument("--server-befehl", help="eigener Startbefehl für den Server (auch LEMONADE_START)")
    p.add_argument("--ohne-fenster", action="store_true", help="ohne Wartefenster, Meldungen nur im Terminal")
    p.add_argument("--verknuepfung", action="store_true",
                   help="Linux: Eintrag im Anwendungsmenü anlegen und beenden")
    a = p.parse_args(argv)

    if a.verknuepfung:
        if os.name == "nt":
            _ausgeben("Unter Windows einfach eine Verknüpfung zu starten.pyw anlegen.")
            return 1
        print(f"Angelegt: {verknuepfung_anlegen(a.stoppen)}")
        return 0

    mit_fenster = not a.ohne_fenster
    url = server_url(a.url)
    weg = prozess = None
    gestartet = False

    if not erreichbar(url):
        if ist_lokal(url):
            weg = startweg_finden(a.server_befehl)
            if weg is None:
                melden("Lemonade nicht gefunden",
                       "Der Lemonade Server läuft nicht, und es wurde kein Weg gefunden, ihn zu starten."
                       + HINWEIS, mit_fenster)
            else:
                _ausgeben(f"Starte den Server: {weg['name']}")
                try:
                    prozess = server_starten(weg)
                    gestartet = True
                except OSError as fehler:
                    melden("Server-Start fehlgeschlagen", f"{weg['name']}: {fehler}" + HINWEIS, mit_fenster)
        if gestartet or not ist_lokal(url):
            frist = a.warten if gestartet else min(a.warten, 10)
            fehler = (mit_fenster_warten if mit_fenster else warten)(url, weg if gestartet else None,
                                                                     prozess, frist)
            if fehler and fehler != "abgebrochen":
                melden("Lemonade Server antwortet nicht", f"{fehler}\n\nAdresse: {url}" + HINWEIS, mit_fenster)

    gui = subprocess.Popen(gui_befehl(a.dateien), cwd=ORDNER)
    if not (a.stoppen and gestartet):
        return 0
    try:
        gui.wait()
    except KeyboardInterrupt:
        pass
    _ausgeben("Beende den Server …")
    server_stoppen(weg, prozess)
    return 0


if __name__ == "__main__":
    sys.exit(main())
