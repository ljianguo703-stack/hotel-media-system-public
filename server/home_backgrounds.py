"""Bundled home images share the existing authenticated media and home settings contract."""
from pathlib import Path
import logging
import os
import shutil
import tempfile

SOURCE = Path(__file__).resolve().parents[1] / 'admin' / 'home-backgrounds'
IMAGES = (
    ('coast', '暮色海岸', '01-coast.png'),
    ('lake', '云山静湖', '02-lake.png'),
    ('dunes', '星夜沙丘', '03-dunes.png'),
    ('gold', '暖金光影', '04-gold.png'),
)


def available_backgrounds(media):
    root = Path(media).resolve()
    folder = root / '_home' / 'defaults'
    if not folder.resolve().is_relative_to(root):
        return []
    return [{'id': ident, 'name': name, 'path': '/media/_home/defaults/' + filename}
            for ident, name, filename in IMAGES
            if (folder / filename).is_file() and not (folder / filename).is_symlink()]


def prepare_backgrounds(media):
    """Copy only missing public assets; existing hotel images/settings are never replaced."""
    root = Path(media).resolve()
    folder = root / '_home' / 'defaults'
    if not folder.resolve().is_relative_to(root):
        return []
    for _, _, filename in IMAGES:
        source, target = SOURCE / filename, folder / filename
        if not source.is_file() or source.is_symlink() or target.exists():
            continue
        temporary = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.background-', dir=folder)
            temporary = Path(name)
            with os.fdopen(fd, 'wb') as output, source.open('rb') as image:
                shutil.copyfileobj(image, output)
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(0o644)
            if not target.exists():
                temporary.replace(target)
        except OSError as error:
            logging.getLogger(__name__).warning('Cannot prepare bundled background %s: %s', filename, error)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
    return available_backgrounds(root)
