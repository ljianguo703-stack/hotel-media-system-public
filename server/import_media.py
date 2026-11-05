#!/usr/bin/env python3
"""Index an existing media folder without moving or changing any source media."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import quote

VIDEO = {'.mp4', '.mkv', '.m4v', '.ts', '.mov', '.avi', '.webm'}
SERIES = {'series', 'tv', '电视剧', '剧集'}
MOVIES = {'movies', 'films', '电影'}


def scan(root, hotel='酒店影院', excluded_roots=()):
    root = Path(root).resolve()
    entries, shows = [], {}
    excluded = {Path(p).resolve() for p in excluded_roots}

    def identity(path):
        return hashlib.sha256(path.relative_to(root).as_posix().encode()).hexdigest()[:20]

    def url(path):
        return '/media/' + quote(path.relative_to(root).as_posix(), safe='/')

    def metadata(folder):
        try:
            value = json.loads((folder / 'metadata.json').read_text())
            return {k: str(v) for k, v in value.items() if k in ('id', 'title', 'category', 'year', 'description', 'accent') and isinstance(v, (str, int))}
        except (OSError, ValueError, AttributeError):
            return {}

    def poster(folder):
        return next((url(p) for p in sorted(folder.iterdir()) if p.is_file() and p.stem.lower() in ('poster', 'cover', '海报')
                     and p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp') and root in p.resolve().parents), '')

    def scan_paths():
        def fail(error): raise error
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail):
            if '.hotel-download-task' in files:
                dirs[:] = []; continue
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d.lower() not in ('ads', 'advertisements', '广告', '_posters', '_compat', '_home', 'lost+found') and (Path(directory) / d).resolve() not in excluded)
            for filename in sorted(files):
                yield Path(directory) / filename

    for path in scan_paths():
        if not path.is_file() or path.suffix.lower() not in VIDEO or root not in path.resolve().parents:
            continue
        rel = path.relative_to(root)
        dirs = list(rel.parts[:-1])
        if any(d.lower() in ('ads', 'advertisements', '广告') for d in dirs) or path.stem.startswith('ad-'):
            continue
        series_at = next((i for i, d in enumerate(dirs) if d.lower() in SERIES), None)
        if series_at is not None and len(dirs) > series_at + 1:
            show_folder = root.joinpath(*dirs[:series_at + 2])
            key = show_folder.relative_to(root).as_posix()
            if key not in shows:
                shows[key] = dict(id=identity(show_folder), title=show_folder.name, kind='series', category='电视剧', year='', description='',
                                  poster=poster(show_folder), accent='#477D85', episodes=[])
                shows[key].update(metadata(show_folder))
            match = re.search(r'S(\d+)[ ._-]*E(\d+)', path.stem, re.I)
            season_folder = next((re.search(r'(?:season|s|第)\s*(\d+)', d, re.I) for d in reversed(dirs) if re.search(r'(?:season|s|第)\s*(\d+)', d, re.I)), None)
            season = int(match.group(1)) if match else int(season_folder.group(1)) if season_folder else 1
            digits = re.findall(r'\d+', path.stem)
            number = int(match.group(2)) if match else int(digits[-1]) if digits else len(shows[key]['episodes']) + 1
            shows[key]['episodes'].append(dict(id=identity(path), title='第 %s 集' % number, season=season, number=number, path=url(path)))
        else:
            movie_at = next((i for i, d in enumerate(dirs) if d.lower() in MOVIES), None)
            # Native downloaders can keep episodes flat, in place for continued seeding.
            episode = re.match(r'^(.+?)[ ._-]+S(\d{1,3})[ ._-]*E(\d{1,4})(?!\d)', path.stem, re.I)
            if movie_at is None and episode:
                title = re.sub(r'[._]+', ' ', episode.group(1)).strip(' ._-')
                key = '@flat:' + path.parent.relative_to(root).as_posix() + '/' + title.casefold()
                if key not in shows:
                    shows[key] = dict(id=hashlib.sha256(key.encode()).hexdigest()[:20], title=title, kind='series',
                                      category='电视剧', year='', description='', poster=poster(path.parent), accent='#477D85', episodes=[])
                season, number = int(episode.group(2)), int(episode.group(3))
                shows[key]['episodes'].append(dict(id=identity(path), title='第 %s 集' % number, season=season, number=number, path=url(path)))
                continue
            category = dirs[movie_at + 1] if movie_at is not None and len(dirs) > movie_at + 1 else '精选'
            title = path.parent.name if movie_at is not None and len(dirs) > movie_at + 2 else path.stem
            item = dict(id=identity(path), title=title, kind='movie', category=category, year='', description='', poster=poster(path.parent), path=url(path), accent='#477D85')
            item.update(metadata(path.parent))
            entries.append(item)
    for show in shows.values():
        show['episodes'].sort(key=lambda e: (e['season'], e['number'], e['path']))
        entries.append(show)
    return {'hotel': hotel, 'items': sorted(entries, key=lambda e: (e['kind'], e['title'], e['id']))}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--media', required=True, type=Path)
    parser.add_argument('--catalog', default=Path(__file__).parent / 'data/catalog.json', type=Path)
    parser.add_argument('--hotel', default='酒店影院')
    args = parser.parse_args()
    if not args.media.is_dir():
        parser.error('media folder does not exist')
    result = scan(args.media, args.hotel)
    args.catalog.parent.mkdir(parents=True, exist_ok=True)
    # Replace our own index atomically; never touch any media, poster or sidecar source.
    temp = args.catalog.with_suffix('.tmp')
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(args.catalog)
    print('已建立片库索引：%s 部作品；原影片文件未改动。' % len(result['items']))
