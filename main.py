"""
HQRay VPN — Root Launcher
Redirects to builder/main.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILDER_DIR = ROOT / "builder"

if __name__ == "__main__":
    sys.path.insert(0, str(BUILDER_DIR))
    import builder.main as builder_main
    builder_main.build()
