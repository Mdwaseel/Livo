"""The 5 MB ceiling, image compression, and the Drive-link fallback."""
import io

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings

from .uploads import (compress_image, human_size, is_image, normalise_drive_link,
                      process_upload)


def photo(width=4000, height=3000, name="shot.jpg"):
    """A JPEG big enough that downscaling has something to do. Random-ish noise
    rather than flat colour, so it doesn't compress to nothing and the size
    assertions stay meaningful."""
    from PIL import Image

    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for x in range(0, width, 7):
        for y in range(0, height, 7):
            pixels[x, y] = ((x * 7) % 256, (y * 13) % 256, (x * y) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=98)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/jpeg")


class SizeTests(SimpleTestCase):
    def test_human_size_reads_naturally(self):
        self.assertEqual(human_size(5 * 1024 * 1024), "5.0 MB")
        self.assertEqual(human_size(512), "512 B")

    @override_settings(MAX_UPLOAD_BYTES=1024)
    def test_oversized_non_image_is_refused_with_drive_advice(self):
        blob = SimpleUploadedFile("dump.pdf", b"x" * 5000, content_type="application/pdf")
        with self.assertRaises(ValidationError) as caught:
            process_upload(blob)
        self.assertIn("Drive link", caught.exception.messages[0])

    @override_settings(MAX_UPLOAD_BYTES=10 * 1024 * 1024)
    def test_small_file_passes_through_untouched(self):
        blob = SimpleUploadedFile("note.txt", b"hello", content_type="text/plain")
        self.assertIs(process_upload(blob), blob)

    def test_unlisted_extension_is_refused(self):
        blob = SimpleUploadedFile("payload.exe", b"MZ", content_type="application/exe")
        with self.assertRaises(ValidationError):
            process_upload(blob)


class CompressionTests(SimpleTestCase):
    def test_large_photo_is_shrunk(self):
        original = photo()
        result = compress_image(original)
        self.assertLess(result.size, original.size)

    @override_settings(IMAGE_MAX_EDGE=800)
    def test_longest_edge_is_capped(self):
        from PIL import Image

        result = compress_image(photo())
        self.assertEqual(max(Image.open(result).size), 800)

    @override_settings(MAX_UPLOAD_BYTES=400 * 1024, IMAGE_MAX_EDGE=1200)
    def test_photo_over_the_limit_is_accepted_once_compressed(self):
        """The point of compressing before checking: a 4000px phone photo is
        well over the ceiling raw and comfortably under it re-encoded."""
        original = photo()
        self.assertGreater(original.size, 400 * 1024)
        result = process_upload(original)
        self.assertLessEqual(result.size, 400 * 1024)

    def test_transparency_survives(self):
        from PIL import Image

        image = Image.new("RGBA", (2400, 2400), (0, 0, 0, 0))
        image.putpixel((5, 5), (255, 0, 0, 255))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        upload = SimpleUploadedFile("logo.png", buffer.getvalue(), content_type="image/png")

        result = compress_image(upload)
        self.assertIn(Image.open(result).mode, ("RGBA", "LA", "P"))
        self.assertTrue(result.name.endswith(".png"))

    def test_corrupt_image_is_returned_untouched_not_crashed(self):
        junk = SimpleUploadedFile("broken.jpg", b"not really a jpeg",
                                  content_type="image/jpeg")
        self.assertIs(compress_image(junk), junk)

    def test_pdf_is_not_treated_as_an_image(self):
        blob = SimpleUploadedFile("brief.pdf", b"%PDF-1.4", content_type="application/pdf")
        self.assertFalse(is_image(blob))
        self.assertIs(compress_image(blob), blob)


class DriveLinkTests(SimpleTestCase):
    def test_plain_url_passes(self):
        url = "https://drive.google.com/file/d/abc123/view"
        self.assertEqual(normalise_drive_link(url), url)

    def test_missing_scheme_is_added(self):
        self.assertEqual(
            normalise_drive_link("drive.google.com/file/d/abc/view"),
            "https://drive.google.com/file/d/abc/view")

    def test_javascript_url_is_refused(self):
        """It would be rendered into an href — a stored XSS otherwise."""
        with self.assertRaises(ValidationError):
            normalise_drive_link("javascript:alert(1)")

    def test_data_url_is_refused(self):
        with self.assertRaises(ValidationError):
            normalise_drive_link("data:text/html;base64,PHNjcmlwdD4=")

    def test_blank_stays_blank(self):
        self.assertEqual(normalise_drive_link("  "), "")
