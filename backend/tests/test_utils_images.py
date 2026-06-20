"""
Unit tests for :mod:`backend.utils.images` — avatar image processing utilities.

Covers the two public entry points:

  - :func:`validate_image` — file-size + format validation
  - :func:`process_avatar` — resize + format conversion + EXIF orientation

Tests use real PIL ``Image`` objects written to ``tmp_path`` so the I/O paths
(``Image.open``, ``img.save``) are exercised end-to-end. No first-party module
mocks are used; the only test doubles are tiny in-memory PIL images.
"""

from __future__ import annotations

import io
import struct
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.images import (  # noqa: E402
    ALLOWED_FORMATS,
    MAX_FILE_SIZE,
    MAX_SIZE,
    process_avatar,
    validate_image,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_image(
    tmp_path: Path,
    name: str,
    size: tuple[int, int] = (64, 64),
    mode: str = "RGB",
    color=(255, 0, 0),
    fmt: str | None = None,
) -> Path:
    """Create a real image file in ``tmp_path`` and return its path."""
    img = Image.new(mode, size, color)
    path = tmp_path / name
    save_kwargs = {}
    if fmt is not None:
        save_kwargs["format"] = fmt
    img.save(path, **save_kwargs)
    return path


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


def test_max_file_size_is_five_megabytes():
    assert MAX_FILE_SIZE == 5 * 1024 * 1024


def test_max_size_is_512_pixels():
    assert MAX_SIZE == 512


def test_allowed_formats_covers_jpeg_png_webp_and_mpo_jpg_aliases():
    # The module advertises both the canonical formats and the JPEG aliases
    # the camera multi-picture format / .jpg extension report as.
    assert {"PNG", "JPEG", "WEBP", "MPO", "JPG"} <= ALLOWED_FORMATS


# ---------------------------------------------------------------------------
# validate_image
# ---------------------------------------------------------------------------


def test_validate_image_accepts_a_real_png(tmp_path):
    path = _write_image(tmp_path, "ok.png")
    ok, err = validate_image(str(path))
    assert ok is True
    assert err is None


def test_validate_image_accepts_a_real_jpeg(tmp_path):
    path = _write_image(tmp_path, "ok.jpg", fmt="JPEG")
    ok, err = validate_image(str(path))
    assert ok is True
    assert err is None


def test_validate_image_accepts_a_real_webp(tmp_path):
    path = _write_image(tmp_path, "ok.webp", fmt="WEBP")
    ok, err = validate_image(str(path))
    assert ok is True
    assert err is None


def test_validate_image_rejects_files_larger_than_the_max(tmp_path, monkeypatch):
    # Create a very small file but tell the module the max is even smaller —
    # this exercises the size-rejection branch without writing 5MB of data.
    path = _write_image(tmp_path, "small.png", size=(32, 32))
    monkeypatch.setattr("utils.images.MAX_FILE_SIZE", 10)  # 10 bytes
    ok, err = validate_image(str(path))
    assert ok is False
    assert "exceeds maximum" in err


def test_validate_image_size_error_message_reports_megabytes(tmp_path, monkeypatch):
    path = _write_image(tmp_path, "small.png", size=(32, 32))
    # 2 MiB ceiling — the error message divides by 1MiB integer floor.
    monkeypatch.setattr("utils.images.MAX_FILE_SIZE", 2 * 1024 * 1024)
    # Pad the file beyond 2MiB to trigger the branch.
    with open(path, "ab") as fh:
        fh.write(b"\x00" * (3 * 1024 * 1024))
    ok, err = validate_image(str(path))
    assert ok is False
    assert "2MB" in err


def test_validate_image_rejects_unsupported_format(tmp_path):
    # BMP is a real image format PIL understands but the module doesn't allow.
    path = tmp_path / "bad.bmp"
    Image.new("RGB", (32, 32), (0, 255, 0)).save(path, format="BMP")
    ok, err = validate_image(str(path))
    assert ok is False
    assert "Invalid format" in err
    assert "BMP" in err
    assert "PNG, JPEG, WEBP" in err


def test_validate_image_rejects_non_image_bytes(tmp_path):
    path = tmp_path / "garbage.png"
    path.write_bytes(b"this is not an image, at all")
    ok, err = validate_image(str(path))
    assert ok is False
    assert "Invalid image file" in err


def test_validate_image_rejects_missing_file(tmp_path):
    # No such file → Path.stat() raises FileNotFoundError, which the function
    # is NOT documented to handle; calling code is expected to ensure the
    # path exists. But we still want to lock in that observable behavior so
    # a refactor doesn't silently change it.
    missing = tmp_path / "nope.png"
    with pytest.raises(FileNotFoundError):
        validate_image(str(missing))


# ---------------------------------------------------------------------------
# process_avatar — resizing
# ---------------------------------------------------------------------------


def test_process_avatar_resizes_oversize_images_to_fit_max_size(tmp_path):
    src = _write_image(tmp_path, "big.png", size=(1024, 768))
    dst = tmp_path / "out.png"
    process_avatar(str(src), str(dst), max_size=512)
    with Image.open(dst) as out:
        w, h = out.size
    assert max(w, h) <= 512
    # Aspect ratio preserved (1024/768 == 4/3) → 512x384
    assert (w, h) == (512, 384)


def test_process_avatar_preserves_portrait_aspect_ratio(tmp_path):
    src = _write_image(tmp_path, "portrait.png", size=(600, 1200))
    dst = tmp_path / "out.png"
    process_avatar(str(src), str(dst), max_size=400)
    with Image.open(dst) as out:
        w, h = out.size
    # 600/1200 = 1/2 → height-bound resize to 400 tall, 200 wide.
    assert (w, h) == (200, 400)


def test_process_avatar_leaves_already_small_images_unchanged_in_size(tmp_path):
    # thumbnail() only shrinks; an already-small image stays the same.
    src = _write_image(tmp_path, "small.png", size=(64, 48))
    dst = tmp_path / "out.png"
    process_avatar(str(src), str(dst), max_size=512)
    with Image.open(dst) as out:
        assert out.size == (64, 48)


def test_process_avatar_default_max_size_is_module_constant(tmp_path):
    src = _write_image(tmp_path, "huge.png", size=(2000, 2000))
    dst = tmp_path / "out.png"
    process_avatar(str(src), str(dst))  # no max_size argument
    with Image.open(dst) as out:
        assert max(out.size) == MAX_SIZE


# ---------------------------------------------------------------------------
# process_avatar — output format selection from extension
# ---------------------------------------------------------------------------


def test_process_avatar_writes_png_for_png_extension(tmp_path):
    src = _write_image(tmp_path, "in.png")
    dst = tmp_path / "out.png"
    process_avatar(str(src), str(dst))
    with Image.open(dst) as out:
        assert out.format == "PNG"


def test_process_avatar_writes_jpeg_for_jpg_extension(tmp_path):
    src = _write_image(tmp_path, "in.png")
    dst = tmp_path / "out.jpg"
    process_avatar(str(src), str(dst))
    with Image.open(dst) as out:
        assert out.format == "JPEG"


def test_process_avatar_writes_jpeg_for_jpeg_extension(tmp_path):
    src = _write_image(tmp_path, "in.png")
    dst = tmp_path / "out.jpeg"
    process_avatar(str(src), str(dst))
    with Image.open(dst) as out:
        assert out.format == "JPEG"


def test_process_avatar_writes_webp_for_webp_extension(tmp_path):
    src = _write_image(tmp_path, "in.png")
    dst = tmp_path / "out.webp"
    process_avatar(str(src), str(dst))
    with Image.open(dst) as out:
        assert out.format == "WEBP"


def test_process_avatar_defaults_to_png_for_unknown_extension(tmp_path):
    src = _write_image(tmp_path, "in.png")
    dst = tmp_path / "out.tiff"
    process_avatar(str(src), str(dst))
    with Image.open(dst) as out:
        assert out.format == "PNG"


def test_process_avatar_format_selection_is_case_insensitive(tmp_path):
    src = _write_image(tmp_path, "in.png")
    dst = tmp_path / "out.JPG"
    process_avatar(str(src), str(dst))
    with Image.open(dst) as out:
        assert out.format == "JPEG"


# ---------------------------------------------------------------------------
# process_avatar — mode conversion (RGBA / CMYK / P → RGB)
# ---------------------------------------------------------------------------


def test_process_avatar_flattens_rgba_onto_white_background(tmp_path):
    # Fully-transparent RGBA image — after flatten with white background,
    # the entire result should be white.
    src_img = Image.new("RGBA", (64, 64), (0, 255, 0, 0))  # transparent green
    src = tmp_path / "in.png"
    src_img.save(src)

    # Write as PNG so the result is lossless and the alpha-flatten is the
    # only observable transformation.
    dst = tmp_path / "out.png"
    process_avatar(str(src), str(dst))

    with Image.open(dst) as out:
        # The output PNG-from-RGB save should be RGB.
        assert out.mode == "RGB"
        out_rgb = out.convert("RGB")
        # Center pixel should be pure white from the flattened background.
        r, g, b = out_rgb.getpixel((32, 32))
    assert (r, g, b) == (255, 255, 255)


def test_process_avatar_converts_palette_mode_to_rgb(tmp_path):
    src_img = Image.new("P", (32, 32))
    src_img.putpalette([0, 0, 0] * 256)
    src = tmp_path / "in.png"
    src_img.save(src)

    dst = tmp_path / "out.jpg"
    process_avatar(str(src), str(dst))

    with Image.open(dst) as out:
        # P-mode through the RGB branch lands as RGB (JPEG decode mode).
        assert out.mode in ("RGB", "L")


def test_process_avatar_converts_cmyk_to_rgb(tmp_path):
    src_img = Image.new("CMYK", (32, 32), (0, 100, 100, 0))
    src = tmp_path / "in.jpg"
    src_img.save(src, format="JPEG")

    dst = tmp_path / "out.jpg"
    process_avatar(str(src), str(dst))

    with Image.open(dst) as out:
        assert out.mode == "RGB"


def test_process_avatar_preserves_grayscale_mode_l(tmp_path):
    # 'L' is in the no-convert allow-list — should pass straight through.
    src_img = Image.new("L", (32, 32), 128)
    src = tmp_path / "in.png"
    src_img.save(src)

    dst = tmp_path / "out.png"
    process_avatar(str(src), str(dst))

    with Image.open(dst) as out:
        # L-mode PNG round-trips as L.
        assert out.mode == "L"


def test_process_avatar_converts_unusual_modes_via_generic_branch(tmp_path):
    # '1' (1-bit) hits the else branch (not RGB/L/RGBA/CMYK/P).
    src_img = Image.new("1", (32, 32), 1)
    src = tmp_path / "in.png"
    src_img.save(src)

    dst = tmp_path / "out.png"
    process_avatar(str(src), str(dst))

    with Image.open(dst) as out:
        # After convert('RGB') and PNG save, the result is RGB.
        assert out.mode in ("RGB", "L")


# ---------------------------------------------------------------------------
# process_avatar — EXIF orientation
# ---------------------------------------------------------------------------


def _make_jpeg_with_exif_orientation(path: Path, orientation: int, size=(20, 40)) -> None:
    """Save a JPEG with a minimal valid EXIF block carrying the Orientation tag."""
    img = Image.new("RGB", size, (200, 100, 50))
    # PIL >= 9.1 supports Image.Exif() directly.
    exif = Image.Exif()
    exif[0x0112] = orientation  # 0x0112 == 274 == Orientation tag.
    img.save(path, format="JPEG", exif=exif.tobytes())


def test_process_avatar_rotates_180_for_exif_orientation_3(tmp_path):
    src = tmp_path / "rot3.jpg"
    _make_jpeg_with_exif_orientation(src, orientation=3, size=(20, 40))
    dst = tmp_path / "out.png"
    process_avatar(str(src), str(dst))
    with Image.open(dst) as out:
        # 180-degree rotation preserves the (W, H) of the source.
        assert out.size == (20, 40)


def test_process_avatar_rotates_270_for_exif_orientation_6(tmp_path):
    src = tmp_path / "rot6.jpg"
    _make_jpeg_with_exif_orientation(src, orientation=6, size=(20, 40))
    dst = tmp_path / "out.png"
    process_avatar(str(src), str(dst))
    with Image.open(dst) as out:
        # 270-degree rotation with expand=True swaps width and height.
        assert out.size == (40, 20)


def test_process_avatar_rotates_90_for_exif_orientation_8(tmp_path):
    src = tmp_path / "rot8.jpg"
    _make_jpeg_with_exif_orientation(src, orientation=8, size=(20, 40))
    dst = tmp_path / "out.png"
    process_avatar(str(src), str(dst))
    with Image.open(dst) as out:
        # 90-degree rotation with expand=True swaps width and height.
        assert out.size == (40, 20)


def test_process_avatar_ignores_exif_orientation_1_normal(tmp_path):
    src = tmp_path / "rot1.jpg"
    _make_jpeg_with_exif_orientation(src, orientation=1, size=(20, 40))
    dst = tmp_path / "out.png"
    process_avatar(str(src), str(dst))
    with Image.open(dst) as out:
        # Orientation 1 == "normal", no rotation applied.
        assert out.size == (20, 40)


def test_process_avatar_handles_image_without_exif(tmp_path):
    # PNGs have no EXIF block — the function must still complete cleanly.
    src = _write_image(tmp_path, "in.png", size=(30, 50))
    dst = tmp_path / "out.png"
    process_avatar(str(src), str(dst))
    with Image.open(dst) as out:
        assert out.size == (30, 50)


def test_process_avatar_swallows_exif_extraction_errors(tmp_path, monkeypatch):
    # If _getexif() itself raises (e.g. AttributeError on an Image subclass that
    # has no such method, or a corrupt EXIF block raising TypeError), the
    # function must swallow it and still produce a valid output file.
    src = _write_image(tmp_path, "in.png", size=(40, 30))
    dst = tmp_path / "out.png"

    real_open = Image.open

    def _patched_open(path, *args, **kwargs):
        im = real_open(path, *args, **kwargs)

        def _boom():
            raise AttributeError("simulated EXIF failure")

        im._getexif = _boom
        return im

    monkeypatch.setattr("utils.images.Image.open", _patched_open)
    process_avatar(str(src), str(dst))
    with Image.open(dst) as out:
        assert out.size == (40, 30)


# ---------------------------------------------------------------------------
# process_avatar — file produced is non-empty and re-openable
# ---------------------------------------------------------------------------


def test_process_avatar_writes_a_readable_image_file(tmp_path):
    src = _write_image(tmp_path, "in.png", size=(100, 100))
    dst = tmp_path / "out.png"
    process_avatar(str(src), str(dst))
    assert dst.exists()
    assert dst.stat().st_size > 0
    # And it round-trips through validate_image as a valid image.
    ok, err = validate_image(str(dst))
    assert ok is True, err
