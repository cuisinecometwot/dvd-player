"""Type and pixel size of a JPEG or PNG from its bytes (for the FLAC/Opus picture block), without decoding it."""

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Start-of-frame markers carry the size; C4 (DHT), C8 (JPG) and CC (DAC) share the range but don't.
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_JPEG_NO_LENGTH = {0x01, *range(0xD0, 0xDA)}  # TEM, RSTn, SOI, EOI


def image_info(data: bytes) -> tuple[str, int, int] | None:
    """(mime type, width, height), or None for anything that isn't a readable JPEG or PNG."""
    if data.startswith(PNG_SIGNATURE) and data[12:16] == b"IHDR" and len(data) >= 24:
        return "image/png", int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i + 3 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte before a marker
            i += 1
            continue
        if marker in _JPEG_NO_LENGTH:
            i += 2
            continue
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if marker in _JPEG_SOF:
            if i + 9 > len(data):
                return None
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return "image/jpeg", width, height
        i += 2 + length
    return None
