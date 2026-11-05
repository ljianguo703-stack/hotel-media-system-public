"""One release version shared by packaging and verification scripts."""
import re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REPO=ROOT.parent
VERSION=re.search(r'const version = "([0-9]+\.[0-9]+\.[0-9]+)"',(ROOT/'config.go').read_text())[1]
ART=REPO/f'artifacts/tv-installer-{VERSION}'
