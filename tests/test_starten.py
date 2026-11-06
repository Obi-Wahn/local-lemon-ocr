"""
Tests für das Startprogramm starten.pyw ohne echten Lemonade Server. Als „Server“
dient ein kleiner HTTP-Server aus der Standardbibliothek, der auf /models antwortet.

Aufruf:   python -m unittest discover -s tests -v
"""
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false")
try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None

# Wird als eigener Prozess gestartet und spielt den Lemonade Server
FAKE_SERVER = """
import sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
time.sleep(float(sys.argv[2]))          # Startzeit des Servers
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"data": []}')
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


def starten_laden():
    pfad = Path(__file__).resolve().parent.parent / "starten.pyw"
    loader = SourceFileLoader("starten", str(pfad))
    spec = importlib.util.spec_from_loader("starten", loader)
    modul = importlib.util.module_from_spec(spec)
    loader.exec_module(modul)
    return modul


def freier_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Antwort(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *a):
        pass


class StartenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = starten_laden()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr-start-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        p = mock.patch.object(self.s, "EINSTELLUNGEN", self.tmp / "einstellungen.json")
        p.start()
        self.addCleanup(p.stop)

    def _http_server(self, status=200) -> str:
        klasse = type("Antwort", (_Antwort,), {"status": status})
        server = HTTPServer(("127.0.0.1", 0), klasse)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/api/v1"

    def test_url_reihenfolge(self):
        with mock.patch.dict(os.environ, {"LEMONADE_BASE_URL": "http://a:1/api/v1"}):
            self.assertEqual(self.s.server_url(), "http://a:1/api/v1")
            self.s.EINSTELLUNGEN.write_text(json.dumps({"url": "http://b:2/api/v1"}), encoding="utf-8")
            self.assertEqual(self.s.server_url(), "http://b:2/api/v1")
            self.assertEqual(self.s.server_url("http://c:3/api/v1"), "http://c:3/api/v1")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.s.EINSTELLUNGEN.write_text("kaputt", encoding="utf-8")
            self.assertEqual(self.s.server_url(), self.s.STANDARD_URL)

    def test_lokal(self):
        self.assertTrue(self.s.ist_lokal("http://localhost:13305/api/v1"))
        self.assertTrue(self.s.ist_lokal("http://127.0.0.1:8000/api/v1"))
        self.assertFalse(self.s.ist_lokal("http://192.0.2.10:13305/api/v1"))

    def test_erreichbar(self):
        self.assertTrue(self.s.erreichbar(self._http_server(200)))
        self.assertTrue(self.s.erreichbar(self._http_server(401)))   # Key fehlt, Server läuft aber
        self.assertFalse(self.s.erreichbar(f"http://127.0.0.1:{freier_port()}/api/v1", zeit=1))

    def test_startweg_systemd_benutzer(self):
        def ausfuehren(befehl, zeit=10):
            code = 0 if befehl[:2] == ["systemctl", "--user"] else 1
            return subprocess.CompletedProcess(befehl, code)
        with mock.patch.object(self.s, "_ausfuehren", ausfuehren), \
                mock.patch.object(self.s.shutil, "which", lambda n: "/usr/bin/" + n), \
                mock.patch.object(self.s.sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {}, clear=True):
            weg = self.s.startweg_finden()
        self.assertEqual(weg["art"], "systemd")
        self.assertEqual(weg["start"], ["systemctl", "--user", "start", "lemond"])
        self.assertEqual(weg["stopp"], ["systemctl", "--user", "stop", "lemond"])

    def test_startweg_systemd_system(self):
        def ausfuehren(befehl, zeit=10):
            return subprocess.CompletedProcess(befehl, 1 if "--user" in befehl else 0)
        with mock.patch.object(self.s, "_ausfuehren", ausfuehren), \
                mock.patch.object(self.s.shutil, "which", lambda n: "/usr/bin/" + n), \
                mock.patch.object(self.s.sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {}, clear=True):
            weg = self.s.startweg_finden()
        self.assertEqual(weg["start"], ["systemctl", "start", "lemond"])

    def test_startweg_programm_und_eigener_befehl(self):
        orte = {"lemonade-server": "/usr/bin/lemonade-server"}
        with mock.patch.object(self.s.shutil, "which", orte.get), \
                mock.patch.object(self.s.os.path, "exists", lambda p: p in orte.values()), \
                mock.patch.object(self.s.sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.s.startweg_finden()["start"], ["/usr/bin/lemonade-server", "serve"])
            orte.clear()
            self.assertIsNone(self.s.startweg_finden())
            weg = self.s.startweg_finden("lemond --port 13305")
            self.assertEqual((weg["art"], weg["start"]), ("prozess", ["lemond", "--port", "13305"]))
            with mock.patch.dict(os.environ, {"LEMONADE_START": "mein-server"}):
                self.assertEqual(self.s.startweg_finden()["start"], ["mein-server"])

    def test_startfehler(self):
        weg = dict(art="prozess", name="kaputt", start=[sys.executable, "-c", "raise SystemExit(3)"])
        prozess = self.s.server_starten(weg)
        prozess.wait(10)
        self.assertIn("Fehlercode 3", self.s.startfehler(weg, prozess))
        weg = dict(art="systemd", name="systemctl", start=[sys.executable, "-c", "pass"])
        prozess = self.s.server_starten(weg)
        prozess.wait(10)
        self.assertIsNone(self.s.startfehler(weg, prozess))   # systemctl fertig mit 0 = gestartet

    def _ablauf(self, *optionen, startzeit=1.5):
        """main() mit Fake-Server als Startbefehl und einer Fake-Oberfläche."""
        port = freier_port()
        url = f"http://127.0.0.1:{port}/api/v1"
        skript = self.tmp / "fake_server.py"
        skript.write_text(FAKE_SERVER, encoding="utf-8")
        server = []
        echtes_starten = self.s.server_starten

        def starten(weg):
            server.append(echtes_starten(weg))
            return server[-1]
        gui_aufruf = []

        def gui_befehl(dateien=()):
            gui_aufruf.append(self.s.erreichbar(url))   # war der Server beim GUI-Start da?
            return [sys.executable, "-c", "pass"]
        befehl = subprocess.list2cmdline([sys.executable, str(skript), str(port), str(startzeit)]) \
            if os.name == "nt" else " ".join(map(repr, [sys.executable, str(skript), str(port), str(startzeit)]))
        with mock.patch.object(self.s, "server_starten", starten), \
                mock.patch.object(self.s, "gui_befehl", gui_befehl):
            code = self.s.main(["--url", url, "--server-befehl", befehl, "--warten", "20", *optionen])
        for p in server:
            self.addCleanup(lambda p=p: (p.poll() is None and p.kill(), p.wait()))
        return code, server, gui_aufruf, url

    def test_starten_warten_gui(self):
        code, server, gui, url = self._ablauf("--ohne-fenster")
        self.assertEqual(code, 0)
        self.assertEqual(len(server), 1)
        self.assertEqual(gui, [True])
        self.assertIsNone(server[0].poll(), "Server soll ohne --stoppen weiterlaufen")

    def test_stoppen_nach_gui(self):
        code, server, gui, url = self._ablauf("--ohne-fenster", "--stoppen")
        self.assertEqual((code, gui), (0, [True]))
        self.assertIsNotNone(server[0].poll(), "Server soll mit --stoppen beendet sein")

    def test_laufender_server_wird_nicht_gestartet(self):
        url = self._http_server()
        with mock.patch.object(self.s, "server_starten") as starten, \
                mock.patch.object(self.s, "gui_befehl", lambda d=(): [sys.executable, "-c", "pass"]):
            self.assertEqual(self.s.main(["--url", url, "--ohne-fenster", "--stoppen"]), 0)
        starten.assert_not_called()

    def test_entfernter_server_wird_nicht_gestartet(self):
        with mock.patch.object(self.s, "server_starten") as starten, \
                mock.patch.object(self.s, "erreichbar", lambda u, zeit=2: False), \
                mock.patch.object(self.s.time, "sleep", lambda s: None), \
                mock.patch.object(self.s, "gui_befehl", lambda d=(): [sys.executable, "-c", "pass"]):
            self.assertEqual(self.s.main(["--url", "http://192.0.2.10:13305/api/v1",
                                          "--ohne-fenster", "--warten", "0"]), 0)
        starten.assert_not_called()

    def test_gui_befehl(self):
        befehl = self.s.gui_befehl(["Beispiel.pdf"])
        self.assertEqual(befehl[1:], [str(self.s.GUI), "Beispiel.pdf"])
        self.assertTrue(self.s.GUI.exists())

    @unittest.skipIf(os.name == "nt", "nur Linux")
    def test_verknuepfung(self):
        ziel = self.tmp / "apps" / "local-lemon-ocr.desktop"
        with mock.patch.object(self.s, "DESKTOP_DATEI", ziel), \
                mock.patch.object(self.s.sys, "executable", "/home/x/mein venv/bin/python"):
            self.s.verknuepfung_anlegen(stoppen=True)
        inhalt = ziel.read_text(encoding="utf-8")
        self.assertIn('Exec="/home/x/mein venv/bin/python" "', inhalt)
        self.assertIn('starten.pyw" "--stoppen" %F', inhalt)
        self.assertIn("Terminal=false", inhalt)
        self.assertEqual(self.s._desktop_argument('a"b$%'), '"a\\\\"b\\\\$%%"')

    @unittest.skipUnless(QApplication, "PySide6 nicht installiert")
    def test_wartefenster(self):
        QApplication.instance() or QApplication([])
        url = self._http_server()
        self.assertIsNone(self.s.mit_fenster_warten(url, None, None, 5))
        tot = f"http://127.0.0.1:{freier_port()}/api/v1"
        weg = dict(art="prozess", name="kaputt", start=[sys.executable, "-c", "raise SystemExit(2)"])
        prozess = self.s.server_starten(weg)
        beginn = time.monotonic()
        self.assertIn("Fehlercode 2", self.s.mit_fenster_warten(tot, weg, prozess, 20))
        self.assertLess(time.monotonic() - beginn, 10)


if __name__ == "__main__":
    unittest.main()
