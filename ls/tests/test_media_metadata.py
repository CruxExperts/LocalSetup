"""Metadata cleanup must preserve pixels and originals, not inherited identity."""
import hashlib
import io
import os
import zlib

import pytest

from ls.core.media_metadata.png import prepare_png, write_copy

Image = pytest.importorskip("PIL.Image")
PngImagePlugin = pytest.importorskip("PIL.PngImagePlugin")
RIGHTS = {"Copyright": "Copyright © 2026 Crux Experts LLC",
          "Author": "Crux Experts LLC", "Website": "https://www.cruxexperts.com/",
          "Usage Terms": "Private asset."}


def test_cleanup_removes_inherited_metadata_and_preserves_original(tmp_path):
    source = tmp_path / "original.png"
    image = Image.new("RGBA", (8, 5), (17, 88, 150, 130))
    tags = PngImagePlugin.PngInfo()
    tags.add_text("Comment", "private camera information")
    exif = Image.Exif()
    exif[271] = "private device"
    image.save(source, pnginfo=tags, exif=exif)
    before = hashlib.sha256(source.read_bytes()).digest()
    result = prepare_png(source, RIGHTS)
    with Image.open(io.BytesIO(result)) as output:
        assert output.info == RIGHTS
        assert not output.getexif()
        assert output.tobytes() == image.tobytes()
    assert hashlib.sha256(source.read_bytes()).digest() == before
    destination = tmp_path / "clean.png"
    write_copy(destination, result)
    with pytest.raises(FileExistsError):
        write_copy(destination, b"replacement")
    assert destination.read_bytes() == result


def test_conflicting_attribution_requires_explicit_replacement(tmp_path):
    source = tmp_path / "third-party.png"
    tags = PngImagePlugin.PngInfo()
    tags.add_text("Copyright", "Another owner")
    Image.new("RGB", (2, 2)).save(source, pnginfo=tags)
    with pytest.raises(ValueError, match="copyright differs"):
        prepare_png(source, RIGHTS)
    assert prepare_png(source, RIGHTS, replace_copyright=True)
    tags = PngImagePlugin.PngInfo()
    tags.add_text("Author", "Another creator")
    Image.new("RGB", (2, 2)).save(source, pnginfo=tags)
    with pytest.raises(ValueError, match="creator differs"):
        prepare_png(source, RIGHTS)


@pytest.mark.parametrize("field", ["Usage Terms", "Website"])
def test_existing_usage_rights_require_explicit_replacement(tmp_path, field):
    source = tmp_path / "restricted.png"
    def save(value):
        tags = PngImagePlugin.PngInfo()
        for key, text in RIGHTS.items():
            tags.add_text(key, value if key == field else text)
        Image.new("RGB", (2, 2)).save(source, pnginfo=tags)
    save(RIGHTS[field])
    assert prepare_png(source, RIGHTS)
    save("Original restricted usage or attribution URL")
    before = source.read_bytes()
    with pytest.raises(ValueError, match="usage rights differ"):
        prepare_png(source, RIGHTS)
    result = prepare_png(source, RIGHTS, replace_copyright=True)
    assert source.read_bytes() == before
    with Image.open(io.BytesIO(result)) as output:
        assert output.info == RIGHTS


def test_unsupported_image_and_symlink_are_preserved(tmp_path):
    source = tmp_path / "original.jpg"
    Image.new("RGB", (2, 2)).save(source)
    with pytest.raises(ValueError, match="Only PNG"):
        prepare_png(source, RIGHTS)
    link = tmp_path / "alias.png"
    link.symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        prepare_png(link, RIGHTS)


def test_late_copyright_and_xmp_require_review(tmp_path):
    source = tmp_path / "late.png"
    tags = PngImagePlugin.PngInfo()
    tags.add(b"tEXt", b"Copyright\0Another owner", after_idat=True)
    Image.new("RGB", (2, 2)).save(source, pnginfo=tags)
    with pytest.raises(ValueError, match="copyright differs"):
        prepare_png(source, RIGHTS)
    tags = PngImagePlugin.PngInfo()
    tags.add_text("XML:com.adobe.xmp", "<dc:rights>Another owner</dc:rights>")
    Image.new("RGB", (2, 2)).save(source, pnginfo=tags)
    with pytest.raises(ValueError, match="rights carrier"):
        prepare_png(source, RIGHTS)


def test_high_depth_and_gamma_need_normalization(tmp_path):
    source = tmp_path / "depth.png"
    Image.new("I;16", (2, 2), 10000).save(source)
    with pytest.raises(ValueError, match="High-depth"):
        prepare_png(source, RIGHTS)
    tags = PngImagePlugin.PngInfo()
    tags.add(b"gAMA", (45455).to_bytes(4, "big"))
    Image.new("RGB", (2, 2)).save(source, pnginfo=tags)
    with pytest.raises(ValueError, match="normalization"):
        prepare_png(source, RIGHTS)
    Image.new("RGB", (2, 2)).save(source)
    original = source.read_bytes()
    chunk = b"cICP" + bytes((9, 16, 0, 1))
    encoded = (4).to_bytes(4, "big") + chunk + zlib.crc32(chunk).to_bytes(4, "big")
    source.write_bytes(original[:33] + encoded + original[33:])
    with pytest.raises(ValueError, match="normalization"):
        prepare_png(source, RIGHTS)


def test_fifo_is_refused_without_waiting_for_a_writer(tmp_path):
    source = tmp_path / "pipe.png"
    os.mkfifo(source)
    with pytest.raises(ValueError, match="regular PNG"):
        prepare_png(source, RIGHTS)
