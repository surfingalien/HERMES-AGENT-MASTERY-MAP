"""Shared test scaffolding: every test gets an isolated HERMES_HOME and the mock provider."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from hermes import config


class HermesTestCase(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        self.tmp = Path(tempfile.mkdtemp(prefix="hermes-test-"))
        self.home = self.tmp / "home"
        self.work = self.tmp / "work"
        self.work.mkdir()
        os.environ["HERMES_HOME"] = str(self.home)
        os.environ["HERMES_BASE_HOME"] = str(self.tmp / "base")
        config.ensure_home()
        self.cfg = config.load_config()
        self.cfg.set_path("model.provider", "mock")
        self.cfg.set_path("model.default", "mock-1")
        self.cfg.set_path("terminal.cwd", str(self.work))
        self.cfg.set_path("approvals.mode", "manual")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def agent(self, provider=None, **kw):
        from hermes.agent import AIAgent
        from hermes.providers.mock import MockProvider

        kw.setdefault("approval_mode", "deny-dangerous")
        return AIAgent(config=self.cfg, provider=provider or MockProvider(), **kw)
