from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote
from import_media import scan


class MediaImportTest(unittest.TestCase):
    def test_chinese_paths_stable_ids_series_order_and_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = ['电影/自然/山 海.mp4', '电视剧/城市来信/Season02/S02E10.mkv', '电视剧/城市来信/Season02/S02E02.mkv', '广告/ad.mp4']
            for name in files:
                p = root / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'unchanged')
            first = scan(root); second = scan(root)
            self.assertEqual(first, second)
            self.assertEqual(len(first['items']), 2)
            movie = next(v for v in first['items'] if v['kind'] == 'movie')
            self.assertEqual(movie['category'], '自然')
            self.assertIn('%20', movie['path'])
            self.assertEqual(unquote(movie['path']), '/media/电影/自然/山 海.mp4')
            series = next(v for v in first['items'] if v['kind'] == 'series')
            self.assertEqual([e['number'] for e in series['episodes']], [2, 10])
            self.assertEqual([e['season'] for e in series['episodes']], [2, 2])
            self.assertTrue(all((root / f).read_bytes() == b'unchanged' for f in files))


if __name__ == '__main__':
    unittest.main()
