#!/usr/bin/env python3
"""Verify integrated portable bundles; pass --output and --build-tools (see --help)."""
from pathlib import Path
import runpy
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools'))
runpy.run_path(str(Path(__file__).resolve().parents[2]/'tools/verify_tv_bundle.py'),run_name='__main__')
