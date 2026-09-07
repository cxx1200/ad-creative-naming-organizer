from __future__ import annotations

import math
import os
import struct
from pathlib import Path


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp"}

_CONTAINER_BOXES = {"moov", "trak", "mdia", "minf", "stbl", "edts", "dinf", "udta"}
_VIDEO_CODEC_TYPES = {
    "av01",
    "avc1",
    "avc3",
    "dvhe",
    "dvsd",
    "encv",
    "hev1",
    "hvc1",
    "mp4v",
    "s263",
    "vp09",
}


def _box_header(handle, offset: int, file_size: int) -> tuple[str, int, int] | None:
    handle.seek(offset)
    prefix = handle.read(8)
    if len(prefix) < 8:
        return None
    box_size, raw_type = struct.unpack(">I4s", prefix)
    box_type = raw_type.decode("latin1")
    header_size = 8
    if box_size == 1:
        extended = handle.read(8)
        if len(extended) < 8:
            return None
        box_size = struct.unpack(">Q", extended)[0]
        header_size = 16
    elif box_size == 0:
        box_size = file_size - offset
    return box_type, box_size, header_size


def _iter_children(handle, start: int, end: int):
    file_size = os.fstat(handle.fileno()).st_size
    first = _box_header(handle, start, file_size)
    if first is None:
        return
    _, _, first_header_size = first
    offset = start + first_header_size
    while offset < end:
        header = _box_header(handle, offset, file_size)
        if header is None:
            break
        box_type, box_size, header_size = header
        if box_size < header_size or offset + box_size > end:
            break
        yield box_type, offset, offset + box_size
        offset += box_size


def _read_dimensions_from_stsd(handle, stsd_start: int, stsd_end: int) -> tuple[int, int] | None:
    header = _box_header(handle, stsd_start, os.fstat(handle.fileno()).st_size)
    if header is None:
        return None
    _, _, header_size = header
    payload_start = stsd_start + header_size
    handle.seek(payload_start)
    prefix = handle.read(8)
    if len(prefix) < 8:
        return None
    entry_count = struct.unpack(">I", prefix[4:])[0]
    offset = payload_start + 8
    for _ in range(entry_count):
        header = _box_header(handle, offset, stsd_end)
        if header is None:
            break
        entry_type, entry_size, entry_header_size = header
        if entry_type in _VIDEO_CODEC_TYPES and entry_size >= entry_header_size + 36:
            handle.seek(offset + 32)
            raw = handle.read(4)
            if len(raw) == 4:
                width, height = struct.unpack(">HH", raw)
                return width, height
        if entry_size < entry_header_size:
            break
        offset += entry_size
    return None


def _find_stsd(handle, start: int, end: int) -> tuple[int, int] | None:
    for box_type, box_start, box_end in _iter_children(handle, start, end):
        if box_type == "stsd":
            return box_start, box_end
        if box_type in _CONTAINER_BOXES and box_end > box_start:
            found = _find_stsd(handle, box_start, box_end)
            if found:
                return found
    return None


def read_video_dimensions(path: str | Path) -> tuple[int, int] | None:
    with Path(path).open("rb") as handle:
        file_size = os.fstat(handle.fileno()).st_size
        offset = 0
        while offset < file_size:
            header = _box_header(handle, offset, file_size)
            if header is None:
                break
            box_type, box_size, header_size = header
            if box_size < header_size or offset + box_size > file_size:
                break
            if box_type == "moov":
                stsd = _find_stsd(handle, offset, offset + box_size)
                if stsd:
                    dimensions = _read_dimensions_from_stsd(handle, *stsd)
                    if dimensions:
                        return dimensions
            offset += box_size
    return None


def read_png_dimensions(path: str | Path) -> tuple[int, int] | None:
    with Path(path).open("rb") as handle:
        header = handle.read(24)
        if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        return struct.unpack(">II", header[16:24])


def read_gif_dimensions(path: str | Path) -> tuple[int, int] | None:
    with Path(path).open("rb") as handle:
        header = handle.read(10)
        if len(header) < 10 or header[:6] not in {b"GIF87a", b"GIF89a"}:
            return None
        return struct.unpack("<HH", header[6:10])


def read_bmp_dimensions(path: str | Path) -> tuple[int, int] | None:
    with Path(path).open("rb") as handle:
        header = handle.read(26)
        if len(header) < 26 or header[:2] != b"BM":
            return None
        width, height = struct.unpack("<ii", header[18:26])
        return (width, abs(height)) if width > 0 and height != 0 else None


def read_jpeg_dimensions(path: str | Path) -> tuple[int, int] | None:
    with Path(path).open("rb") as handle:
        if handle.read(2) != b"\xff\xd8":
            return None
        while True:
            marker = handle.read(2)
            if len(marker) < 2 or marker[0] != 0xFF:
                return None
            marker_code = marker[1]
            while marker_code == 0xFF:
                marker_code = handle.read(1)
                if not marker_code:
                    return None
                marker_code = marker_code[0]
            if marker_code in {0xD8, 0xD9}:
                continue
            size_raw = handle.read(2)
            if len(size_raw) < 2:
                return None
            segment_size = struct.unpack(">H", size_raw)[0]
            if segment_size < 2:
                return None
            if marker_code in range(0xC0, 0xCF) and marker_code not in {0xC4, 0xC8, 0xCC}:
                payload = handle.read(5)
                if len(payload) >= 5:
                    height, width = struct.unpack(">HH", payload[1:5])
                    return width, height
            handle.seek(segment_size - 2, os.SEEK_CUR)


def read_image_dimensions(path: str | Path) -> tuple[int, int] | None:
    suffix = Path(path).suffix.lower()
    if suffix == ".png":
        return read_png_dimensions(path)
    if suffix in {".jpg", ".jpeg"}:
        return read_jpeg_dimensions(path)
    if suffix == ".gif":
        return read_gif_dimensions(path)
    if suffix == ".bmp":
        return read_bmp_dimensions(path)
    return None


def read_dimensions(path: str | Path) -> tuple[int, int] | None:
    suffix = Path(path).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return read_video_dimensions(path)
    if suffix in IMAGE_EXTENSIONS:
        return read_image_dimensions(path)
    return None


RATIO_FORMATS = {
    "三码": {
        (1, 1): "11",
        (4, 5): "45",
        (9, 16): "916",
        (16, 9): "169",
    },
    "x": {
        (1, 1): "1x1",
        (4, 5): "4x5",
        (9, 16): "9x16",
        (16, 9): "16x9",
    },
    "冒号": {
        (1, 1): "1:1",
        (4, 5): "4:5",
        (9, 16): "9:16",
        (16, 9): "16:9",
    },
}


def normalize_ratio_format(ratio_format: str | None) -> str:
    return ratio_format.strip() if ratio_format and ratio_format.strip() in RATIO_FORMATS else "三码"


def ratio_code(
    width: int | None,
    height: int | None,
    ratio_format: str | None = "三码",
) -> str | None:
    if not width or not height:
        return None
    divisor = math.gcd(width, height)
    ratio_width, ratio_height = width // divisor, height // divisor
    return RATIO_FORMATS[normalize_ratio_format(ratio_format)].get(
        (ratio_width, ratio_height)
    )


def normalize_ratio(value: str | None, ratio_format: str | None = "三码") -> str | None:
    if not value:
        return value
    normalized = str(value).strip().lower().replace("：", ":")
    aliases = {
        "三码": {
            "1x1": "11",
            "4x5": "45",
            "9x16": "916",
            "16x9": "169",
            "1:1": "11",
            "4:5": "45",
            "9:16": "916",
            "16:9": "169",
        },
        "x": {
            "11": "1x1",
            "45": "4x5",
            "916": "9x16",
            "169": "16x9",
            "1:1": "1x1",
            "4:5": "4x5",
            "9:16": "9x16",
            "16:9": "16x9",
        },
        "冒号": {
            "11": "1:1",
            "45": "4:5",
            "916": "9:16",
            "169": "16:9",
            "1x1": "1:1",
            "4x5": "4:5",
            "9x16": "9:16",
            "16x9": "16:9",
            "1:1": "1:1",
            "4:5": "4:5",
            "9:16": "9:16",
            "16:9": "16:9",
        },
    }
    return aliases.get(normalize_ratio_format(ratio_format), {}).get(normalized, value)


def dimension_text(width: int | None, height: int | None) -> str | None:
    if width and height:
        return f"{width}x{height}"
    return None
