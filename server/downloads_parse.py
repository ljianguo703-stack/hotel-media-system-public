"""D02: offline parser for one-sentence Chinese download requests, plus release-name attribute extraction.

Every recognised field records where it came from; anything the parser is unsure of is reported as a question instead
of being guessed, and a request without a recognisable title is never sent to automatic download."""
import re

CN_DIGITS = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
SERIES_WORDS = ('电视剧', '剧集', '连续剧', '美剧', '韩剧', '日剧', '英剧', '国产剧', '番剧', '动漫', '动画剧', '纪录片剧集', '综艺')
MOVIE_WORDS = ('电影', '影片', '片子', '大片', '动画电影', '纪录片')
FILLER = r'(?:请|麻烦|帮忙|帮我|给我|替我|我要|我想|想要|想看|能不能|可以|一下|下载|下|找|搜索|搜|来一?部|来个|弄一?部|安排|部|个|一部|一个|的|吧|呢|啊|谢谢)'
RESOLUTIONS = [('2160p', r'(?<![0-9])(?:4k|uhd|2160p?)(?![0-9])|超高清|蓝光原盘4k'), ('1080p', r'(?<![0-9])(?:1080[pi]?|fhd)(?![0-9])|全高清|蓝光'),
               ('720p', r'(?<![0-9])720p?(?![0-9])|标清高清')]
AUDIO = {'国语': ('国语', '普通话', 'mandarin'), '粤语': ('粤语', 'cantonese'), '英语': ('英语', '英文原声', 'english'), '日语': ('日语', 'japanese')}
SUBS = {'中文字幕': ('中字', '中文字幕', '简中', '繁中', '中英字幕', '双语字幕', 'chs', 'cht'), '英文字幕': ('英文字幕', 'eng sub')}


def cn_number(text):
    text = text.strip()
    if text.isdigit(): return int(text)
    if not text or any(c not in CN_DIGITS for c in text): return None
    if text == '十': return 10
    if '十' in text:
        head, _, tail = text.partition('十')
        return (CN_DIGITS.get(head, 1) if head else 1) * 10 + (CN_DIGITS.get(tail, 0) if tail else 0)
    value = 0
    for c in text: value = value * 10 + CN_DIGITS[c]
    return value


def parse_request(sentence):
    """Return {'original', 'title', 'kind', 'year', 'resolution', 'season', 'episodes', 'audio', 'subtitle', 'max_size_gb',
    'fields': {name: source}, 'questions': [...], 'warnings': [...]}."""
    original = str(sentence or '').strip()[:500]
    text = re.sub(r'\s+', ' ', original)
    low = text.lower()
    result = {'original': original, 'title': '', 'kind': '', 'year': None, 'resolution': '', 'season': None, 'episodes': None,
              'audio': [], 'subtitle': [], 'max_size_gb': None, 'fields': {}, 'questions': [], 'warnings': []}
    if not text: result['questions'].append('请说明要下载的作品'); return result
    quoted = re.search(r'[《「“"\'『](.+?)[》」”"\'』]', text)
    if quoted: result['title'] = quoted.group(1).strip(); result['fields']['title'] = '书名号/引号'
    for kind, words in (('series', SERIES_WORDS), ('movie', MOVIE_WORDS)):
        if any(w in text for w in words): result['kind'] = kind; result['fields']['kind'] = '关键词'; break
    m = re.search(r'(?<!\d)((?:19|20)\d{2})\s*年?(?:版|上映)?(?!\d|p|P|集|季)', text)
    if m: result['year'] = int(m.group(1)); result['fields']['year'] = '年份'
    for name, pattern in RESOLUTIONS:
        if re.search(pattern, low, re.I): result['resolution'] = name; result['fields']['resolution'] = '画质关键词'; break
    if not result['resolution'] and re.search(r'高清', text):
        result['resolution'] = '1080p'; result['fields']['resolution'] = '“高清”按 1080p 理解'; result['warnings'].append('“高清”不够具体，按 1080p 处理；如需 4K 请说明')
    season = re.search(r'第\s*([0-9一二三四五六七八九十两]+)\s*季|(?<![a-z])s(\d{1,2})(?![0-9])', low)
    if season:
        value = cn_number(season.group(1) or season.group(2)); result['season'] = value; result['fields']['season'] = '季数'; result['kind'] = 'series'; result['fields']['kind'] = '季数推断'
    ep = re.search(r'第\s*([0-9一二三四五六七八九十两]+)\s*(?:到|至|-|~|—)\s*第?\s*([0-9一二三四五六七八九十两]+)\s*集|第\s*([0-9一二三四五六七八九十两]+)\s*集|(?<![a-z])e(\d{1,3})(?![0-9])', low)
    if ep:
        if ep.group(1): start, end = cn_number(ep.group(1)), cn_number(ep.group(2))
        else: start = end = cn_number(ep.group(3) or ep.group(4))
        if start and end and start <= end: result['episodes'] = [start, end]; result['fields']['episodes'] = '集数'
        else: result['warnings'].append('集数范围无法识别')
        result['kind'] = 'series'; result['fields'].setdefault('kind', '集数推断')
    if re.search(r'全集|整季|全季|完整版?季', text): result['episodes'] = None; result['fields']['episodes'] = '全集'
    for name, words in AUDIO.items():
        if any(w in low for w in words): result['audio'].append(name)
    for name, words in SUBS.items():
        if any(w in low for w in words): result['subtitle'].append(name)
    if result['audio']: result['fields']['audio'] = '音轨关键词'
    if result['subtitle']: result['fields']['subtitle'] = '字幕关键词'
    size = re.search(r'(?:不超过|小于|不要超过|以内|最多)\s*(\d+(?:\.\d+)?)\s*(?:g|gb)|(\d+(?:\.\d+)?)\s*(?:g|gb)\s*(?:以内|以下)', low)
    if size: result['max_size_gb'] = float(size.group(1) or size.group(2)); result['fields']['max_size_gb'] = '体积限制'
    if not result['title']:
        rest = text
        for pattern in (r'第\s*[0-9一二三四五六七八九十两]+\s*(?:季|集)(?:\s*(?:到|至|-)\s*第?\s*[0-9一二三四五六七八九十两]+\s*集)?', r'(?:19|20)\d{2}\s*年?(?:版|上映)?',
                        r'4k|uhd|2160p?|1080[pi]?|720p?|超高清|全高清|蓝光|高清', r'国语|普通话|粤语|英语|日语|中文字幕|中字|双语字幕?', r'(?:不超过|小于|以内|最多)\s*\d+(?:\.\d+)?\s*(?:g|gb)',
                        '|'.join(SERIES_WORDS + MOVIE_WORDS), r'全集|整季|全季', r'版本|画质|格式|资源|的|要|是'):
            rest = re.sub(pattern, ' ', rest, flags=re.I)
        rest = re.sub(r'^(?:\s*' + FILLER + r')+', ' ', rest)
        rest = re.sub(r'[，,。.!！?？、：:；;（）()\[\]【】]+', ' ', rest)
        candidate = re.sub(r'\s+', ' ', rest).strip()
        candidate = re.sub(r'^(?:' + FILLER + r'\s*)+|(?:\s*' + FILLER + r')+$', '', candidate).strip()
        if candidate and len(candidate) <= 60:
            result['title'] = candidate; result['fields']['title'] = '句子剩余部分（建议用《》标注片名）'
            result['warnings'].append('片名“%s”由句子推断，确认无误后再下载' % candidate)
    if not result['title']: result['questions'].append('没有识别出片名，请用《》标注，例如：帮我下载电影《片名》，要4K')
    if not result['kind']:
        result['questions'].append('请说明是电影还是电视剧（或其他类型）')
    if result['kind'] == 'series' and result['season'] is None and result['episodes'] is not None:
        result['season'] = 1; result['fields']['season'] = '未说明季数，按第 1 季'; result['warnings'].append('未说明季数，按第 1 季处理')
    return result


def needs_clarification(parsed):
    return bool(parsed['questions']) or parsed['fields'].get('title', '').startswith('句子剩余部分')


def release_attributes(name):
    """Attributes guessed from a release name. All are marked as coming from the name, never from probing."""
    low = name.lower()
    attrs = {'resolution': '', 'year': None, 'season': None, 'episodes': None, 'codec': '', 'source': '', 'audio': [], 'subtitle': []}
    for res, pattern in RESOLUTIONS:
        if re.search(pattern, low, re.I): attrs['resolution'] = res; break
    m = re.search(r'(?<![0-9])((?:19|20)\d{2})(?![0-9p])', name)
    if m: attrs['year'] = int(m.group(1))
    m = re.search(r'(?<![a-z])s(\d{1,2})(?:[ ._-]?e(\d{1,3})(?:[ ._-]?(?:e|-)(\d{1,3}))?)?(?![0-9])', low) or re.search(r'第\s*([0-9一二三四五六七八九十]+)\s*季', name)
    if m:
        attrs['season'] = cn_number(m.group(1)) if m.lastindex else None
        if m.lastindex and m.lastindex >= 2 and m.group(2): attrs['episodes'] = [int(m.group(2)), int(m.group(3) or m.group(2))]
    m = re.search(r'第\s*(\d+)\s*(?:-|到|至)\s*(\d+)\s*集|e(\d{1,3})-e?(\d{1,3})', low)
    if m and not attrs['episodes']: attrs['episodes'] = [int(m.group(1) or m.group(3)), int(m.group(2) or m.group(4))]
    for codec, pattern in (('hevc', r'x265|h\.?265|hevc'), ('h264', r'x264|h\.?264|avc'), ('av1', r'\bav1\b')):
        if re.search(pattern, low): attrs['codec'] = codec; break
    for source, pattern in (('remux', r'remux'), ('bluray', r'blu-?ray|bdrip'), ('web', r'web-?dl|webrip'), ('hdtv', r'hdtv')):
        if re.search(pattern, low): attrs['source'] = source; break
    for name_, words in AUDIO.items():
        if any(w in low for w in words): attrs['audio'].append(name_)
    for name_, words in SUBS.items():
        if any(w in low for w in words): attrs['subtitle'].append(name_)
    return attrs


def normalize_title(text):
    text = re.sub(r'[\s._\-:：·,，!！?？\'"’“”《》()（）\[\]【】]+', '', str(text).lower())
    return text
