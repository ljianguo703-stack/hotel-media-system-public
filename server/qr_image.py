"""Minimal QR Code (ISO/IEC 18004) encoder for short texts such as Wi-Fi join codes, plus a PNG writer.

Byte mode, error-correction level M, versions 1-10 (up to 213 bytes), automatic mask selection.
Written for this project so the server needs no third-party package.
"""
import struct
import zlib

# Error-correction level M: (EC codewords per block, number of blocks) by version.
_EC_M = {1: (10, 1), 2: (16, 1), 3: (26, 1), 4: (18, 2), 5: (24, 2), 6: (16, 4), 7: (18, 4), 8: (22, 4), 9: (22, 5), 10: (26, 5)}
_FORMAT_ECL_M = 0  # format-information bits for level M


def _gf_tables():
    exp, log = [0] * 512, [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x; log[x] = i
        x <<= 1
        if x & 0x100: x ^= 0x11D
    for i in range(255, 512): exp[i] = exp[i - 255]
    return exp, log


_EXP, _LOG = _gf_tables()


def _rs_remainder(data, degree):
    gen = [1]
    for i in range(degree):  # generator polynomial (x - a^0)(x - a^1)...(x - a^(degree-1))
        nxt = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            nxt[j] ^= c
            if c: nxt[j + 1] ^= _EXP[_LOG[c] + i]
        gen = nxt
    rem = [0] * degree
    for b in data:
        factor = b ^ rem[0]
        rem = rem[1:] + [0]
        if factor:
            for j in range(degree): rem[j] ^= _EXP[_LOG[gen[j + 1]] + _LOG[factor]] if gen[j + 1] else 0
    return rem


def _raw_modules(ver):
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7: result -= 36
    return result


def _alignment_positions(ver):
    if ver == 1: return []
    n = ver // 7 + 2
    size = ver * 4 + 17
    step = (ver * 4 + n * 2 + 1) // (n * 2 - 2) * 2
    return [6] + sorted(size - 7 - i * step for i in range(n - 1))


def _bch(value, poly, bits):
    v = value << bits
    top = poly.bit_length() - 1
    while v.bit_length() > top:
        v ^= poly << (v.bit_length() - poly.bit_length())
    return (value << bits) | v


class QrMatrix:
    def __init__(self, data: bytes):
        for ver in range(1, 11):
            ec, blocks = _EC_M[ver]
            capacity = _raw_modules(ver) // 8 - ec * blocks
            count_bits = 8 if ver <= 9 else 16
            if 4 + count_bits + 8 * len(data) <= capacity * 8: break
        else:
            raise ValueError('内容过长，无法生成二维码')
        self.version, self.size = ver, ver * 4 + 17
        bits = []
        put = lambda value, n: bits.extend((value >> i) & 1 for i in reversed(range(n)))
        put(0b0100, 4); put(len(data), count_bits)
        for b in data: put(b, 8)
        put(0, min(4, capacity * 8 - len(bits)))
        put(0, (-len(bits)) % 8)
        codewords = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
        pad = 0xEC
        while len(codewords) < capacity: codewords.append(pad); pad ^= 0xEC ^ 0x11
        self.codewords = self._interleave(codewords, ver)
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]
        self._draw_function_patterns()
        self._draw_codewords()
        best = None
        for mask in range(8):
            self._apply_mask(mask); self._draw_format(mask)
            score = self._penalty()
            if best is None or score < best[0]: best = (score, mask)
            self._apply_mask(mask)  # undo (XOR)
        self.mask = best[1]
        self._apply_mask(self.mask); self._draw_format(self.mask)

    @staticmethod
    def _interleave(data, ver):
        ec, blocks = _EC_M[ver]
        raw = _raw_modules(ver) // 8
        short_len, num_short = raw // blocks, blocks - raw % blocks
        out_blocks, k = [], 0
        for i in range(blocks):
            length = short_len - ec + (0 if i < num_short else 1)
            block = data[k:k + length]; k += length
            out_blocks.append((block, _rs_remainder(block, ec)))
        result = []
        for i in range(short_len - ec + 1):
            for j, (block, _) in enumerate(out_blocks):
                if i < len(block): result.append(block[i])
        for i in range(ec):
            for _, rem in out_blocks: result.append(rem[i])
        return result

    def _set(self, x, y, dark):
        self.modules[y][x] = dark; self.function[y][x] = True

    def _draw_function_patterns(self):
        size = self.size
        for i in range(size):
            self._set(6, i, i % 2 == 0); self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        dist = max(abs(dx), abs(dy))
                        self._set(x, y, dist not in (2, 4))
        positions = _alignment_positions(self.version)
        for i, ax in enumerate(positions):
            for j, ay in enumerate(positions):
                if (i == 0 and j == 0) or (i == 0 and j == len(positions) - 1) or (i == len(positions) - 1 and j == 0): continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3): self._set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format(0)  # reserve format areas
        if self.version >= 7:
            bits = _bch(self.version, 0x1F25, 12)
            for i in range(18):
                dark = (bits >> i) & 1 == 1
                a, b = size - 11 + i % 3, i // 3
                self._set(a, b, dark); self._set(b, a, dark)

    def _draw_format(self, mask):
        bits = _bch(_FORMAT_ECL_M << 3 | mask, 0x537, 10) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1
        size = self.size
        for i in range(6): self._set(8, i, bit(i))
        self._set(8, 7, bit(6)); self._set(8, 8, bit(7)); self._set(7, 8, bit(8))
        for i in range(9, 15): self._set(14 - i, 8, bit(i))
        for i in range(8): self._set(size - 1 - i, 8, bit(i))
        for i in range(8, 15): self._set(8, size - 15 + i, bit(i))
        self._set(8, size - 8, True)  # dark module

    def _draw_codewords(self):
        size, i = self.size, 0
        total = len(self.codewords) * 8
        right = size - 1
        while right >= 1:
            if right == 6: right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = size - 1 - vert if upward else vert
                    if not self.function[y][x] and i < total:
                        self.modules[y][x] = (self.codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def _apply_mask(self, mask):
        rules = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0, lambda x, y: (x + y) % 3 == 0,
                 lambda x, y: (x // 3 + y // 2) % 2 == 0, lambda x, y: x * y % 2 + x * y % 3 == 0,
                 lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0, lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0]
        rule = rules[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and rule(x, y): self.modules[y][x] = not self.modules[y][x]

    def _penalty(self):
        m, size, score = self.modules, self.size, 0
        lines = [row for row in m] + [[m[y][x] for y in range(size)] for x in range(size)]
        for line in lines:
            run, prev = 0, None
            for v in line:
                run = run + 1 if v == prev else 1; prev = v
                if run == 5: score += 3
                elif run > 5: score += 1
            text = ''.join('1' if v else '0' for v in line)
            score += 40 * (text.count('10111010000') + text.count('00001011101'))
        for y in range(size - 1):
            for x in range(size - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]: score += 3
        dark = sum(v for row in m for v in row)
        score += abs(dark * 20 - size * size * 10) // (size * size) * 10
        return score


def _png(width, height, rows):
    raw = b''.join(b'\x00' + bytes(row) for row in rows)
    chunk = lambda kind, body: struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xFFFFFFFF)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 0, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b'')


def qr_png(text: str, scale: int = 10, border: int = 4) -> bytes:
    """PNG (8-bit grayscale) of the QR code for text, with the standard 4-module quiet zone."""
    q = QrMatrix(text.encode('utf-8'))
    n = (q.size + border * 2) * scale
    rows = []
    for y in range(n):
        my = y // scale - border
        rows.append([0 if 0 <= my < q.size and 0 <= x // scale - border < q.size and q.modules[my][x // scale - border] else 255 for x in range(n)])
    return _png(n, n, rows)


def wifi_payload(ssid: str, password: str, security: str, hidden: bool) -> str:
    """Standard Wi-Fi join string understood by iOS and Android camera apps."""
    esc = lambda s: ''.join('\\' + c if c in '\\;,:"' else c for c in s)
    sec = {'WPA': 'WPA', 'WEP': 'WEP', 'nopass': 'nopass'}[security]
    parts = ['WIFI:T:' + sec, 'S:' + esc(ssid)]
    if sec != 'nopass': parts.append('P:' + esc(password))
    if hidden: parts.append('H:true')
    return ';'.join(parts) + ';;'
