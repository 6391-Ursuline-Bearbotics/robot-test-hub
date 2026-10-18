"""Minimal QR Code encoder (byte mode, error correction level M, versions 1-10) to SVG.

Used to show the phone-notes URL on the admin page without network libraries.
Follows the QR Code specification (ISO/IEC 18004) as in Project Nayuki's reference.
"""
from __future__ import annotations

# (total codewords, ec codewords per block, blocks) for level M, versions 1..10
_M = {1: (26, 10, 1), 2: (44, 16, 1), 3: (70, 26, 1), 4: (100, 18, 2), 5: (134, 24, 2),
      6: (172, 16, 4), 7: (196, 18, 4), 8: (242, 22, 4), 9: (292, 22, 5), 10: (346, 26, 5)}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38],
          8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
_LEVEL_M_BITS = 0


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z & 0xFF


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _codewords(text_bytes):
    for version in range(1, 11):
        total, ec_len, blocks = _M[version]
        capacity_bits = (total - ec_len * blocks) * 8
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * len(text_bytes) <= capacity_bits:
            break
    else:
        raise ValueError('Text too long for this small QR encoder')
    bits = [0, 1, 0, 0] + [(len(text_bytes) >> i) & 1 for i in range(count_bits - 1, -1, -1)]
    for b in text_bytes:
        bits += [(b >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, capacity_bits - len(bits))
    bits += [0] * (-len(bits) % 8)
    data = [int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < capacity_bits // 8:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    # Split into blocks (short blocks first), add EC, interleave.
    data_total = total - ec_len * blocks
    short_len = data_total // blocks
    long_count = data_total % blocks
    divisor = _rs_divisor(ec_len)
    parts, k = [], 0
    for i in range(blocks):
        length = short_len + (1 if i >= blocks - long_count else 0)
        block = data[k:k + length]
        k += length
        parts.append((block, _rs_remainder(block, divisor)))
    out = []
    for i in range(short_len + 1):
        for block, _ in parts:
            if i < len(block):
                out.append(block[i])
    for i in range(ec_len):
        for _, ec in parts:
            out.append(ec[i])
    return version, out


def matrix(text):
    version, codewords = _codewords(text.encode('utf-8'))
    size = version * 4 + 17
    modules = [[False] * size for _ in range(size)]
    function = [[False] * size for _ in range(size)]

    def put(x, y, dark):
        modules[y][x] = dark
        function[y][x] = True
    for i in range(size):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    dist = max(abs(dx), abs(dy))
                    put(x, y, dist not in (2, 4))
    positions = _ALIGN[version]
    for i, ax in enumerate(positions):
        for j, ay in enumerate(positions):
            if (i, j) in ((0, 0), (0, len(positions) - 1), (len(positions) - 1, 0)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
    # Reserve format and version areas (filled after masking).
    format_bits = _format_bits(0)
    _draw_format(put, size, format_bits)
    if version >= 7:
        _draw_version(put, size, version)
    # Place data bits in the zigzag order.
    bits = [(b >> (7 - i)) & 1 for b in codewords for i in range(8)]
    index = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = size - 1 - vert if upward else vert
                if not function[y][x] and index < len(bits):
                    modules[y][x] = bool(bits[index])
                    index += 1
        right -= 2
    # Mask 0: (x + y) % 2 == 0 — any mask is valid for decoders.
    for y in range(size):
        for x in range(size):
            if not function[y][x] and (x + y) % 2 == 0:
                modules[y][x] = not modules[y][x]
    _draw_format(put, size, _format_bits(0))
    return modules


def _format_bits(mask):
    data = (_LEVEL_M_BITS << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _draw_format(put, size, bits):
    bit = lambda i: ((bits >> i) & 1) != 0
    for i in range(6):
        put(8, i, bit(i))
    put(8, 7, bit(6))
    put(8, 8, bit(7))
    put(7, 8, bit(8))
    for i in range(9, 15):
        put(14 - i, 8, bit(i))
    for i in range(8):
        put(size - 1 - i, 8, bit(i))
    for i in range(8, 15):
        put(8, size - 15 + i, bit(i))
    put(8, size - 8, True)


def _draw_version(put, size, version):
    rem = version
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    bits = (version << 12) | rem
    for i in range(18):
        dark = ((bits >> i) & 1) != 0
        a, b = size - 11 + i % 3, i // 3
        put(a, b, dark)
        put(b, a, dark)


def svg(text, *, scale=6, border=4):
    grid = matrix(text)
    size = len(grid)
    total = (size + 2 * border) * scale
    paths = ''.join(f'M{(x + border) * scale},{(y + border) * scale}h{scale}v{scale}h-{scale}z'
                    for y in range(size) for x in range(size) if grid[y][x])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total} {total}" width="{total}" height="{total}" '
            f'role="img" aria-label="QR code for {text}"><rect width="100%" height="100%" fill="#fff"/>'
            f'<path d="{paths}" fill="#000"/></svg>')
