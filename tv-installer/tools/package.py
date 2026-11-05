#!/usr/bin/env python3
"""Build from the integrated client payload and reviewed runtime lock; see --help."""
from pathlib import Path
import runpy
runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tools/build_tv_bundle.py'), run_name='__main__')
