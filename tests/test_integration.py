"""End-to-end tests over real loopback sockets, driving the shim the way a
Last.fm client library does (signed form POSTs, XML and JSON parsing)."""

import hashlib
import http.client
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlencode

from lfmshim.server import serve_in_thread

from . import fixtures as F
from .helpers import NOW, FakeTransport, make_app

KEY, SECRET = "clientapikey", "clientsecret"
ROOT = Path(__file__).resolve().parents[1]


def sign(params):
    raw = "".join(k + params[k] for k in sorted(params) if k not in ("format", "callback"))
    return hashlib.md5((raw + SECRET).encode()).hexdigest()


class Client:
    """Minimal Last.fm client in the style of pylast/most scrobblers."""

    def __init__(self, port, host_header, context=None):
        self.port, self.host_header, self.context = port, host_header, context

    def _conn(self):
        if self.context:
            return http.client.HTTPSConnection("127.0.0.1", self.port, timeout=10, context=self.context)
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def raw(self, verb, path, body=None, headers=None):
        conn = self._conn()
        h = {"Host": self.host_header, "User-Agent": "test-client/1.0"}
        h.update(headers or {})
        conn.request(verb, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r, data

    def call(self, method, signed=False, post=False, fmt="json", **params):
        p = {"method": method, "api_key": KEY, **{k: str(v) for k, v in params.items()}}
        if signed:
            p["api_sig"] = sign(p)
        if fmt:
            p["format"] = fmt
        if post:
            r, data = self.raw("POST", "/2.0/", urlencode(p),
                               {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
        else:
            r, data = self.raw("GET", "/2.0/?" + urlencode(p))
        return r.status, (json.loads(data) if fmt == "json" else ET.fromstring(data))


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = F.install_believe(F.install_cher(FakeTransport()))
        cls.sent = []
        t.add(f"{F.LB}/1/submit-listens",
              lambda m, u, h, b: (cls.sent.append(json.loads(b)), (200, {"status": "ok"}))[1], method="POST")
        cls.app = make_app(t, server={"port": 0}, api={"clients": {KEY: SECRET}},
                           listenbrainz={"scrobble": True, "token": "tok"}, upstream={"enrich_scrobbles": False})
        cls.app.store.add_user("alice", "correct horse battery")
        cls.srv, _ = serve_in_thread(cls.app)
        port = cls.srv.server_address[1]
        cls.client = Client(port, f"localhost:{port}")
        cls.port = port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_full_desktop_auth_and_scrobble_session(self):
        c = self.client
        _, body = c.call("auth.getToken", signed=True)
        token = body["token"]
        # The user opens the auth page in a browser and approves.
        r, page = c.raw("GET", f"/api/auth/?api_key={KEY}&token={token}")
        self.assertEqual(r.status, 200)
        csrf = page.decode().split('name="csrf" value="')[1].split('"')[0]
        form = urlencode({"token": token, "csrf": csrf, "api_key": KEY, "cb": "", "username": "alice",
                          "password": "correct horse battery", "action": "approve"})
        r, page = c.raw("POST", "/api/auth/", form, {"Content-Type": "application/x-www-form-urlencoded",
                                                     "Origin": f"http://localhost:{self.port}"})
        self.assertEqual(r.status, 200, page)
        _, body = c.call("auth.getSession", signed=True, token=token)
        sk = body["session"]["key"]

        _, root = c.call("track.updateNowPlaying", signed=True, post=True, fmt=None, sk=sk, artist="Cher",
                         track="The Power", album="Believe", duration=236)
        self.assertEqual(root.get("status"), "ok")
        params = {"sk": sk}
        for i, (tr, ts) in enumerate([("Believe", NOW - 600), ("Strong Enough", NOW - 300)]):
            params.update({f"artist[{i}]": "Cher", f"track[{i}]": tr, f"timestamp[{i}]": ts,
                           f"album[{i}]": "Believe"})
        _, root = c.call("track.scrobble", signed=True, post=True, fmt=None, **params)
        self.assertEqual(root.find("scrobbles").get("accepted"), "2")
        _, body = c.call("user.getRecentTracks", user="alice", limit=10)
        tracks = body["recenttracks"]["track"]
        self.assertEqual(tracks[0]["@attr"], {"nowplaying": "true"})
        self.assertEqual([t["name"] for t in tracks[1:]], ["Strong Enough", "Believe"])
        self.app.sync.flush()
        self.assertEqual([s["listen_type"] for s in self.sent], ["playing_now", "import"])

        # Wrong signature and GET writes are refused.
        bad = {"method": "track.love", "api_key": KEY, "sk": sk, "artist": "Cher", "track": "Believe",
               "api_sig": "0" * 32, "format": "json"}
        r, data = c.raw("POST", "/2.0/", urlencode(bad), {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(json.loads(data)["error"], 13)
        _, body = c.call("track.love", signed=True, post=False, sk=sk, artist="Cher", track="Believe")
        self.assertEqual(body["error"], 3)
        _, body = c.call("track.love", signed=True, post=True, sk=sk, artist="Cher", track="Believe")
        self.assertEqual(body, {})

    def test_metadata_over_http_xml_and_json(self):
        c = self.client
        status, root = c.call("artist.getInfo", fmt=None, artist="Cher")
        self.assertEqual((status, root.get("status")), (200, "ok"))
        self.assertEqual(root.findtext("artist/mbid"), F.CHER)
        status, body = c.call("album.getInfo", artist="Cher", album="Believe")
        self.assertEqual(body["album"]["tracks"]["track"][0]["name"], "Believe")
        status, body = c.call("artist.getInfo", artist="no such artist anywhere")
        self.assertEqual((status, body["error"]), (200, 6))
        status, body = c.call("no.suchMethod")
        self.assertEqual((status, body["error"]), (400, 3))

    def test_browser_csrf_blocked(self):
        p = {"method": "track.love", "api_key": KEY, "sk": "x", "artist": "a", "track": "b"}
        p["api_sig"] = sign(p)
        r, data = self.client.raw("POST", "/2.0/?format=json", urlencode(p), {
            "Content-Type": "application/x-www-form-urlencoded", "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(json.loads(data)["error"], 4)
        r, _ = self.client.raw("GET", "/2.0/?method=artist.getInfo", headers={"Host": "rebind.attacker.example"})
        self.assertEqual(r.status, 421)


@unittest.skipUnless(shutil.which("openssl"), "openssl CLI needed to mint a throwaway test certificate")
class HttpsFixedHostTests(unittest.TestCase):
    """HTTPS deployment answering for a fixed public host name (e.g. a client
    that hardcodes ws.audioscrobbler.com). The certificate is a throwaway in a
    temp dir and is trusted only by this test's SSL context; no system trust
    store or hosts file is touched."""

    HOST = "ws.audioscrobbler.com"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cert, key = os.path.join(cls.tmp, "cert.pem"), os.path.join(cls.tmp, "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", f"/CN={cls.HOST}", "-addext", f"subjectAltName=DNS:{cls.HOST}",
                        "-keyout", key, "-out", cert], check=True, capture_output=True)
        t = F.install_cher(FakeTransport())
        cls.app = make_app(t, server={"port": 0, "tls_cert": cert, "tls_key": key,
                                      "public_url": f"https://{cls.HOST}"})
        cls.srv, _ = serve_in_thread(cls.app)
        ctx = ssl.create_default_context(cafile=cert)
        ctx.check_hostname = False  # we connect to 127.0.0.1; SNI/host checked below
        cls.client = Client(cls.srv.server_address[1], cls.HOST, ctx)
        cls.cert = cert

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_https_with_fixed_host(self):
        status, body = self.client.call("artist.getInfo", artist="Cher")
        self.assertEqual((status, body["artist"]["name"]), (200, "Cher"))
        _, body = self.client.call("auth.getToken")
        self.assertEqual(len(body["token"]), 32)

    def test_certificate_matches_host(self):
        ctx = ssl.create_default_context(cafile=self.cert)
        import socket
        with socket.create_connection(("127.0.0.1", self.srv.server_address[1]), timeout=5) as sock:
            with ctx.wrap_socket(sock, server_hostname=self.HOST) as tls:
                self.assertEqual(tls.version()[:3], "TLS")


class CliTests(unittest.TestCase):
    def run_cli(self, *args, stdin=None, data_dir=None):
        env = dict(os.environ, LFMSHIM_DATA_DIR=data_dir or tempfile.mkdtemp(), PYTHONPATH=str(ROOT))
        env.pop("LFMSHIM_CONFIG", None)
        return subprocess.run([sys.executable, "-m", "lfmshim", *args], input=stdin, capture_output=True,
                              text=True, env=env, cwd=str(ROOT), timeout=60)

    def test_methods_and_adduser(self):
        out = self.run_cli("methods")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("track.scrobble", out.stdout)
        d = tempfile.mkdtemp()
        out = self.run_cli("adduser", "carol", "--password-stdin", stdin="a long password\n", data_dir=d)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("a long password", out.stdout + out.stderr)
        out = self.run_cli("users", data_dir=d)
        self.assertIn("carol (password set)", out.stdout)
        shutil.rmtree(d, ignore_errors=True)

    def test_bad_config_exit_code(self):
        env_bad = self.run_cli("--config", "does-not-exist.toml", "users")
        self.assertEqual(env_bad.returncode, 2)
        self.assertIn("config file not found", env_bad.stderr)


class DocsInSyncTests(unittest.TestCase):
    def test_compatibility_matrix_lists_every_method(self):
        from lfmshim.api.registry import REGISTRY

        doc = (ROOT / "docs" / "COMPATIBILITY.md").read_text(encoding="utf-8")
        for spec in REGISTRY.values():
            if spec.name.startswith("test."):
                continue
            self.assertIn(f"`{spec.name}`", doc, f"{spec.name} missing from docs/COMPATIBILITY.md")


if __name__ == "__main__":
    unittest.main()
