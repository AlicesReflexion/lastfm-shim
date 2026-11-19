import http.client
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from lfmshim.api import registry
from lfmshim.api.dispatch import handle, signature
from lfmshim.config import ConfigError, load_config
from lfmshim.render import to_json_obj, to_xml
from lfmshim.server import serve_in_thread

from .helpers import API_KEY, SECRET, call, make_app


def _register_test_methods():
    @registry.method("test.echo")
    def echo(req):
        return {"echo": {"@attr": {"value": req.require("value")}, "image": [
            {"#text": "http://x/s.jpg", "size": "small"}, {"#text": "http://x/l.jpg", "size": "large"}],
            "items": {"item": [{"name": "a"}]}}}

    @registry.method("test.write", auth="session", write=True)
    def write(req):
        return {"user": req.user}

    @registry.method("test.signed", auth="signed")
    def signed(req):
        return {}


class RenderTests(unittest.TestCase):
    def test_xml_attr_text_list(self):
        tree = {"artist": {"name": "Cher & co", "image": [{"#text": "u1", "size": "small"}],
                           "stats": {"listeners": 5}, "@attr": {"rank": 1}, "empty": None}}
        root = ET.fromstring(to_xml(tree))
        self.assertEqual(root.get("status"), "ok")
        a = root.find("artist")
        self.assertEqual(a.get("rank"), "1")
        self.assertEqual(a.findtext("name"), "Cher & co")
        self.assertEqual(a.find("image").get("size"), "small")
        self.assertEqual(a.find("image").text, "u1")
        self.assertEqual(a.findtext("stats/listeners"), "5")
        self.assertIsNotNone(a.find("empty"))

    def test_json_stringifies_and_keeps_arrays(self):
        out = to_json_obj({"x": {"n": 5, "flag": True, "list": [{"a": 1}], "none": None}})
        self.assertEqual(out, {"x": {"n": "5", "flag": "1", "list": [{"a": "1"}], "none": ""}})

    def test_json_collapse_single(self):
        out = to_json_obj({"x": {"list": [{"a": 1}], "two": [1, 2], "zero": []}}, collapse_single=True)
        self.assertEqual(out["x"]["list"], {"a": "1"})
        self.assertEqual(out["x"]["two"], ["1", "2"])
        self.assertEqual(out["x"]["zero"], [])

    def test_xml_strips_invalid_chars(self):
        ET.fromstring(to_xml({"a": "bad\x01char"}))


class DispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _register_test_methods()

    @classmethod
    def tearDownClass(cls):
        for n in ("test.echo", "test.write", "test.signed"):
            registry.REGISTRY.pop(n, None)

    def setUp(self):
        self.app = make_app(api={"clients": {API_KEY: SECRET}})

    def test_default_xml(self):
        status, body = call(self.app, "test.echo", fmt=None, value="v")
        self.assertEqual(status, 200)
        root = ET.fromstring(body)
        self.assertEqual(root.find("echo").get("value"), "v")

    def test_json(self):
        status, body = call(self.app, "test.echo", value="v")
        self.assertEqual(body["echo"]["@attr"], {"value": "v"})
        self.assertEqual(body["echo"]["image"][1], {"#text": "http://x/l.jpg", "size": "large"})

    def test_method_case_insensitive(self):
        status, _ = call(self.app, "TEST.ECHO", value="v")
        self.assertEqual(status, 200)

    def test_unknown_method_error3(self):
        # Observed live: HTTP 400 with only message/error (no links).
        status, body = call(self.app, "nope.nothing")
        self.assertEqual(body, {"message": "Invalid Method - No method with that name in this package", "error": 3})
        self.assertEqual(status, 400)

    def test_error_xml(self):
        status, body = call(self.app, "nope.nothing", fmt=None)
        root = ET.fromstring(body)
        self.assertEqual(root.get("status"), "failed")
        self.assertEqual(root.find("error").get("code"), "3")

    def test_missing_param_error6(self):
        status, body = call(self.app, "test.echo")
        self.assertEqual((status, body["error"], body["links"]), (200, 6, []))  # observed: HTTP 200

    def test_missing_api_key_error6_http400(self):
        # Observed live: error 6, HTTP 400, only message/error -- also for an
        # unknown method, because the key is checked first.
        for name in ("test.echo", "nope.nothing"):
            resp = handle(self.app, "GET", {"method": name, "value": "x", "format": "json"})
            self.assertEqual(resp.status, 400)
            self.assertEqual(json.loads(resp.body), {
                "message": "Invalid parameters - Your request is missing a required parameter", "error": 6})
        resp = handle(self.app, "GET", {"method": "test.echo", "value": "x"})
        self.assertEqual((resp.status, ET.fromstring(resp.body).find("error").get("code")), (400, "6"))

    def test_api_key_allowlist(self):
        # Observed live for an invalid key: error 10, HTTP 403, no links.
        app = make_app(api={"api_keys": ["other"]})
        status, body = call(app, "test.echo", value="v")
        self.assertEqual((status, body),
                         (403, {"message": "Invalid API key - You must be granted a valid key by last.fm",
                                "error": 10}))

    def test_jsonp_callback(self):
        resp = handle(self.app, "GET", {"method": "test.echo", "api_key": API_KEY, "format": "json",
                                        "value": "v", "callback": "cb.fn"})
        self.assertTrue(resp.body.startswith(b"cb.fn({"))
        resp = handle(self.app, "GET", {"method": "test.echo", "api_key": API_KEY, "format": "json",
                                        "value": "v", "callback": "alert(1)//"})
        self.assertTrue(resp.body.startswith(b"{"))

    def test_signature_algorithm(self):
        # Example from the Last.fm auth spec: api_key + method + token, sorted.
        p = {"api_key": "xxxxxxxx", "method": "auth.getSession", "token": "yyyyyy", "format": "json"}
        import hashlib
        expected = hashlib.md5(b"api_keyxxxxxxxxmethodauth.getSessiontokenyyyyyy" + b"sec").hexdigest()
        self.assertEqual(signature(p, "sec"), expected)

    def test_write_requires_post_session_signature(self):
        sk = self.app.store.create_session("alice")
        self.app.store.add_user("alice")
        _, body = call(self.app, "test.write", sk=sk, sign=True)
        self.assertEqual(body["error"], 3)  # GET not allowed
        _, body = call(self.app, "test.write", http="POST", sk=sk)
        self.assertEqual(body["error"], 13)  # no signature
        _, body = call(self.app, "test.write", http="POST", sk=sk, sign=True, secret="wrong")
        self.assertEqual(body["error"], 13)
        _, body = call(self.app, "test.write", http="POST", sk="bogus", sign=True)
        self.assertEqual(body["error"], 9)
        status, body = call(self.app, "test.write", http="POST", sk=sk, sign=True)
        self.assertEqual((status, body), (200, {"user": "alice"}))

    def test_unknown_api_key_signature_policies(self):
        app = make_app()  # no known secrets
        app.store.add_user("bob")
        sk = app.store.create_session("bob")
        _, body = call(app, "test.write", http="POST", sk=sk, sign=True, secret="anything")
        self.assertEqual(body, {"user": "bob"})
        strict = make_app(api={"signature_policy": "strict"})
        sk = strict.store.create_session("bob")
        _, body = call(strict, "test.write", http="POST", sk=sk, sign=True, secret="anything")
        self.assertEqual(body["error"], 13)

    def test_cross_origin_writes_blocked(self):
        self.app.store.add_user("alice")
        sk = self.app.store.create_session("alice")
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            _, body = call(self.app, "test.write", http="POST", headers=headers, sk=sk, sign=True)
            self.assertEqual(body["error"], 4, headers)
        ok = make_app(api={"clients": {API_KEY: SECRET}, "allowed_origins": ["https://player.example"]})
        ok.store.add_user("alice")
        sk = ok.store.create_session("alice")
        _, body = call(ok, "test.write", http="POST", headers={"Origin": "https://player.example"}, sk=sk, sign=True)
        self.assertEqual(body, {"user": "alice"})

    def test_adopt_unknown_session(self):
        app = make_app(api={"accept_unknown_sessions": True, "default_user": "me"})
        _, body = call(app, "test.write", http="POST", sk="realLastfmSessionKey", sign=True)
        self.assertEqual(body, {"user": "me"})
        self.assertEqual(app.store.session_user("realLastfmSessionKey"), "me")


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = load_config(env={})
        self.assertEqual((cfg.server.host, cfg.server.port), ("127.0.0.1", 9549))
        self.assertEqual(cfg.server.effective_public_url(), "http://localhost:9549")

    def test_toml_and_env(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.toml")
            with open(path, "w") as fh:
                fh.write('[server]\nport = 1234\n[api]\nclients = { k = "s" }\n')
            cfg = load_config(path, env={"LFMSHIM_SERVER_PORT": "4321", "LFMSHIM_LISTENBRAINZ_TOKEN": "zq9SECRETzq9"})
            self.assertEqual(cfg.server.port, 4321)
            self.assertEqual(cfg.api.clients, {"k": "s"})
            self.assertNotIn("zq9SECRETzq9", repr(cfg))
            self.assertNotIn("'s'", repr(cfg))

    def test_rejects_remote_bind_and_bad_keys(self):
        with self.assertRaises(ConfigError):
            load_config(env={"LFMSHIM_SERVER_HOST": "0.0.0.0"})
        load_config(env={"LFMSHIM_SERVER_HOST": "0.0.0.0", "LFMSHIM_SERVER_ALLOW_REMOTE": "true"})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.toml")
            with open(path, "w") as fh:
                fh.write("[server]\nprot = 1\n")
            with self.assertRaises(ConfigError):
                load_config(path, env={})

    def test_https_public_url(self):
        cfg = load_config(env={"LFMSHIM_SERVER_TLS_CERT": "c", "LFMSHIM_SERVER_TLS_KEY": "k",
                               "LFMSHIM_SERVER_PORT": "443", "LFMSHIM_SERVER_PUBLIC_URL": "https://ws.example.test/"})
        self.assertEqual(cfg.server.effective_public_url(), "https://ws.example.test")


class HttpServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _register_test_methods()
        cls.app = make_app(server={"port": 0})
        cls.srv, cls.thread = serve_in_thread(cls.app)
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        for n in ("test.echo", "test.write", "test.signed"):
            registry.REGISTRY.pop(n, None)

    def request(self, verb, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"localhost:{self.port}"}
        h.update(headers or {})
        conn.request(verb, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r, data

    def test_get_and_post_form(self):
        r, data = self.request("GET", f"/2.0/?method=test.echo&value=a%20b&api_key={API_KEY}&format=json")
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(data)["echo"]["@attr"]["value"], "a b")
        self.assertEqual(r.getheader("Access-Control-Allow-Origin"), "*")
        form = urlencode({"method": "test.echo", "value": "ü", "api_key": API_KEY})
        r, data = self.request("POST", "/2.0", form, {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status, 200)
        self.assertIn("text/xml", r.getheader("Content-Type"))
        self.assertEqual(ET.fromstring(data).find("echo").get("value"), "ü")

    def test_bad_host_rejected(self):
        r, _ = self.request("GET", "/2.0/?method=test.echo", headers={"Host": "attacker.example"})
        self.assertEqual(r.status, 421)

    def test_health_and_404(self):
        self.assertEqual(self.request("GET", "/healthz")[0].status, 200)
        self.assertEqual(self.request("GET", "/nope")[0].status, 404)

    def test_body_limit(self):
        # Announce an oversized body without sending it: the server must refuse
        # before reading (sending 2 MB first races the early close on Windows).
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/2.0/", skip_host=True)
        conn.putheader("Host", f"localhost:{self.port}")
        conn.putheader("Content-Type", "application/x-www-form-urlencoded")
        conn.putheader("Content-Length", "2000000")
        conn.endheaders()
        r = conn.getresponse()
        r.read()
        conn.close()
        self.assertEqual(r.status, 413)


if __name__ == "__main__":
    unittest.main()
