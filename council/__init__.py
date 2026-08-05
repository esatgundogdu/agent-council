"""Plan Council — multi-model plan maturation orchestrator."""

__version__ = "0.2.0"

from pathlib import Path

# Repo root (editable install): .../agent-council/council/__init__.py -> agent-council/
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "council.yaml"
PROMPTS_DIR = PACKAGE_ROOT / "prompts"
OPENCODE_CONFIG_PATH = PACKAGE_ROOT / "opencode" / "council.opencode.json"
