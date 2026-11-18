"""Container / PaaS deployment behaviour (Render, Fly.io, Railway, Docker)."""

from __future__ import annotations

import http.client
import json
import os
import threading

import yaml

from hermes import config

from tests.helpers import HermesTestCase

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - 3.10 has no TOML parser in the stdlib
    tomllib = None


class DeployTests(HermesTestCase):
    def test_env_seeds_defaults_but_config_wins(self):
        os.environ.update(HERMES_PROVIDER="anthropic", HERMES_MODEL="claude-opus-5-5", HERMES_CWD="/data/workspace")
        cfg = config.load_config()
        self.assertEqual((cfg.get_path("model.provider"), cfg.get_path("model.default")), ("anthropic", "claude-opus-5-5"))
        self.assertEqual(cfg.get_path("terminal.cwd"), "/data/workspace")
        cfg.set_path("model.default", "claude-sonnet-5-5")  # e.g. changed later in the dashboard
        config.save_config(cfg)
        self.assertEqual(config.load_config().get_path("model.default"), "claude-sonnet-5-5")
        self.assertEqual(config.DEFAULT_CONFIG["model"]["provider"], "openrouter")  # defaults not mutated

    def test_platform_hosts_secure_cookie_and_token_not_printed(self):
        from hermes.web.dashboard import Dashboard, platform_hosts

        os.environ.update(RENDER_EXTERNAL_HOSTNAME="hermes.onrender.com", FLY_APP_NAME="my-hermes",
                          HERMES_ALLOWED_HOSTS="agent.example.com, other.example.com", HERMES_WEB_TOKEN="secret-from-env-123")
        self.assertEqual(sorted(platform_hosts()), ["agent.example.com", "hermes.onrender.com", "my-hermes.fly.dev", "other.example.com"])
        d = Dashboard(self.cfg)
        self.assertTrue(d.check_host("hermes.onrender.com"))
        self.assertTrue(d.check_host("my-hermes.fly.dev:443"))
        self.assertFalse(d.check_host("evil.example"))
        url = d.login_url("127.0.0.1", 8080)
        self.assertTrue(url.startswith("https://agent.example.com/"))
        self.assertNotIn("secret-from-env-123", url)
        self.assertEqual(d.token, "secret-from-env-123")

    def test_one_port_serves_dashboard_api_and_webhooks(self):
        from hermes.agent import AIAgent
        from hermes.gateway.runner import GatewayRunner
        from hermes.power.api_server import make_server
        from hermes.providers.mock import MockProvider
        from hermes.web.dashboard import Dashboard

        os.environ.update(WEBHOOK_SECRET="hook-1", API_SERVER_KEY="api-1", HERMES_WEB_TOKEN="web-1")
        prov = MockProvider()
        runner = GatewayRunner(self.cfg, agent_factory=lambda **kw: AIAgent(provider=prov, **kw))
        runner.build_adapters()
        self.assertIn("webhook", runner.adapters)
        runner.start(block=False, cron=False, own_http=False)
        self.addCleanup(runner.stop)
        self.assertIsNone(runner.http)  # no second port
        got = []
        runner.adapters["webhook"].send = lambda chat, text, thread_id="", reply_to="": got.append((chat, text))
        dash = Dashboard(self.cfg, agent_factory=lambda **kw: AIAgent(provider=prov, **kw))
        srv = make_server(self.cfg, "127.0.0.1", 0, dashboard=dash, gateway=runner)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]

        def call(method, path, body=None, headers=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request(method, path, body=body, headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
            r = c.getresponse()
            data = r.read()
            c.close()
            return r.status, data, r

        self.assertEqual(call("GET", "/health")[0], 200)
        self.assertEqual(call("GET", "/v1/models", headers={"Authorization": "Bearer api-1"})[0], 200)
        self.assertEqual(call("GET", "/")[0], 200)  # dashboard page
        self.assertEqual(call("POST", "/webhook/wrong", b"{}")[0], 403)
        status, data, _ = call("POST", "/webhook/hook-1", json.dumps({"text": "ping", "chat_id": "ops"}).encode())
        self.assertEqual(status, 202, data)
        for _ in range(100):
            if got:
                break
            threading.Event().wait(0.05)
        self.assertEqual(got, [("ops", "Echo: ping")])
        _, _, resp = call("GET", "/?token=web-1", headers={"X-Forwarded-Proto": "https"})
        self.assertIn("; Secure", resp.getheader("Set-Cookie"))

    def test_deploy_files_are_consistent(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        render = yaml.safe_load(open(os.path.join(root, "render.yaml")))["services"][0]
        self.assertEqual(render["disk"]["mountPath"], "/data")
        self.assertEqual(render["healthCheckPath"], "/health")
        keys = {e["key"]: e for e in render["envVars"]}
        self.assertTrue(keys["HERMES_WEB_TOKEN"]["generateValue"])
        self.assertTrue(keys["API_SERVER_KEY"]["generateValue"])
        if tomllib is not None:
            fly = tomllib.load(open(os.path.join(root, "fly.toml"), "rb"))
            self.assertEqual(fly["mounts"][0]["destination"], "/data")
            self.assertEqual(fly["http_service"]["internal_port"], int(fly["env"]["PORT"]))
            self.assertEqual(fly["http_service"]["auto_stop_machines"], "off")
        dockerfile = open(os.path.join(root, "Dockerfile")).read()
        self.assertIn("HERMES_HOME=/data/hermes", dockerfile)
        self.assertIn('CMD ["hermes", "serve", "--gateway", "--host", "0.0.0.0"]', dockerfile)
        entry = open(os.path.join(root, "deploy", "docker-entrypoint.sh")).read()
        self.assertIn("setpriv --reuid=hermes", entry)
