#!/usr/bin/env python3
"""Launch the bundled MCP bridge with Codex selected as the orchestrator."""
import os
import runpy
import sys
from pathlib import Path

plugin_root = Path(__file__).resolve().parents[1]
os.environ["AGENT_ORCHESTRATOR_HOST"] = "codex"
sys.path.insert(0, str(plugin_root))
runpy.run_path(str(plugin_root / "src" / "agent_orchestrator.py"), run_name="__main__")
