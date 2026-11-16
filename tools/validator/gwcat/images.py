"""Width and height of PNG / JPEG / WebP from their headers (stdlib only; the pixels are not decoded)."""

import struct


def image_info(data):
    """bytes -> (format, width, height) or (None, 0, 0) for an unknown / broken image"""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24 and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return "png", w, h
    if data[:3] == b"\xff\xd8\xff":
        return _jpeg(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp(data)
    return None, 0, 0


def _jpeg(data):
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > n:
            break
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        # SOF0..SOF15 except DHT (C4), JPG (C8), DAC (CC)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 <= n:
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return "jpeg", w, h
            break
        i += 2 + length
    return None, 0, 0


def _webp(data):
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30:
        w, h = struct.unpack("<HH", data[26:30])
        return "webp", w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        b = data[21:25]
        w = 1 + (((b[1] & 0x3F) << 8) | b[0])
        h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
        return "webp", w, h
    if chunk == b"VP8X" and len(data) >= 30:
        w = 1 + int.from_bytes(data[24:27], "little")
        h = 1 + int.from_bytes(data[27:30], "little")
        return "webp", w, h
    return None, 0, 0


EXT_FORMATS = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp"}
