from __future__ import annotations
import json
import os

from .db import resolve_db_url


class Config:
    def __init__(self, script_dir: str):
        self.script_dir = script_dir
        cfg_path = os.path.join(script_dir, "config.json")
        self.cfg = {}
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                self.cfg = json.load(f)
        self.output_dir = os.path.join(script_dir, "output")
        os.makedirs(self.output_dir, exist_ok=True)
        data_dir = os.path.join(script_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        # Use the shared DB resolver so Config matches app.models and dashboard.
        self.db_url = resolve_db_url(script_dir)
        self.steps_path = os.path.join(script_dir, "steps.json")
        self.test_steps_path = os.path.join(script_dir, "test.json")

    def steps_file(self, test: bool) -> str:
        return self.test_steps_path if test else self.steps_path
