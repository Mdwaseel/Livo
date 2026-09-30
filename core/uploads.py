"""Upload gatekeeping: a hard size ceiling, an extension allow-list, and
lossy re-encoding of images.

The VPS holds the media directory, so every byte accepted here is disk that
never comes back on its own. Three things guard it:

1. `CappedFileUploadHandler` aborts the HTTP transfer as soon as a single part
   goes past the ceiling. Without it Django buffers the whole body to temp
   storage first and only then hands it to a view — a 2 GB POST would already
   have hit the disk by the time any of our code could object.
2. `compress_image` re-encodes photos. A phone shot is typically 3-6 MB of JPEG
   at 4000px; nothing in this CRM displays wider than ~1200px, so downscaling
   and re-encoding routinely takes 90% off with no visible loss.
3. `process_upload` refuses what is still too big *after* compression, and the
   callers turn that refusal into "paste a Drive link instead".

Note the files live on disk under MEDIA_ROOT, not inside the database — the
database only ever stores the path string. Keeping media small is about the
VPS's own free space and its backups.
"""
from __future__ import annotations

import io
import os
from functools import wraps

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.core.files.uploadhandler import FileUploadHandler, StopUpload

# Formats Pillow can open and we are willing to re-encode. SVG is intentionally
# absent: it is XML, Pillow can't read it, and it can carry script — it is
# size-checked and stored as-is.
COMPRESSIBLE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp")
# Formats that must keep an alpha channel, so they can't become JPEG.
TRANSPARENT_EXTENSIONS = (".png", ".gif", ".webp")


# Distinguishes "caller said nothing" from "caller said no limit" — `None` and
# `0` both legitimately mean unlimited, so neither can be the default.
_DEFAULT = object()


def max_upload_bytes():
    return getattr(settings, "MAX_UPLOAD_BYTES", 5 * 1024 * 1024)


def human_size(num_bytes):
    """"4.2 MB" — for messages a non-technical user reads."""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def max_upload_label():
    return human_size(max_upload_bytes())


def empty_upload_message():
    """What to say when request.FILES came back empty.

    That happens for two very different reasons — nothing was picked, or
    CappedFileUploadHandler killed an oversized transfer — and the view can't
    tell them apart, so the message has to cover both.
    """
    return (
        f"No file was received. Files over {max_upload_label()} are rejected — "
        "upload it to Google Drive and paste the share link in the “Drive link” "
        "box instead."
    )


# --------------------------------------------------------------------------
# 1. abort oversized transfers mid-stream
# --------------------------------------------------------------------------

class CappedFileUploadHandler(FileUploadHandler):
    """Kill the request once any single file part exceeds the ceiling.

    `StopUpload(connection_reset=True)` stops Django reading the rest of the
    body, so the remaining gigabytes are never written anywhere. The trade-off
    is that the browser sees a reset rather than a tidy error page, which is why
    the upload forms also check `file.size` client-side and explain the limit
    before the user ever presses submit — this handler is the backstop for
    anything that bypasses the form.
    """

    def __init__(self, request=None):
        super().__init__(request)
        self._seen = 0

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self._seen = 0

    def receive_data_chunk(self, raw_data, start):
        self._seen += len(raw_data)
        if self._seen > max_upload_bytes():
            raise StopUpload(connection_reset=True)
        return raw_data  # pass through to the next handler in the chain

    def file_complete(self, file_size):
        return None  # this handler never produces the file, it only polices it


# --------------------------------------------------------------------------
# 2. shrink images
# --------------------------------------------------------------------------

def _extension(upload):
    return os.path.splitext(upload.name or "")[1].lower()


def is_image(upload):
    return _extension(upload) in COMPRESSIBLE_EXTENSIONS


def compress_image(upload, *, max_edge=None):
    """Downscale to IMAGE_MAX_EDGE and re-encode. Returns a new upload, or the
    original when Pillow is missing, the file isn't really an image, or the
    re-encode came out no smaller (already-optimised PNGs often do).

    `max_edge` overrides the global longest-edge cap for one call; `0` skips
    the downscale entirely and only re-encodes.
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:  # Pillow is in requirements, but never break uploads over it
        return upload

    extension = _extension(upload)
    if extension not in COMPRESSIBLE_EXTENSIONS:
        return upload

    try:
        upload.seek(0)
        image = Image.open(upload)
        # Animated GIF/WebP would be flattened to a single frame by the save
        # below, silently destroying the animation. Leave them alone.
        if getattr(image, "n_frames", 1) > 1:
            upload.seek(0)
            return upload
        image.load()
        # Phone cameras store orientation in EXIF instead of rotating pixels;
        # without this, re-encoding (which drops EXIF) turns portraits sideways.
        image = ImageOps.exif_transpose(image)

        keeps_alpha = extension in TRANSPARENT_EXTENSIONS and (
            image.mode in ("RGBA", "LA") or "transparency" in image.info)
        if keeps_alpha:
            image = image.convert("RGBA")
            target_format, target_extension = "PNG", ".png"
            save_kwargs = {"optimize": True}
        else:
            image = image.convert("RGB")
            target_format, target_extension = "JPEG", ".jpg"
            save_kwargs = {
                "quality": getattr(settings, "IMAGE_QUALITY", 80),
                "optimize": True,
                "progressive": True,
            }

        if max_edge is None:
            max_edge = getattr(settings, "IMAGE_MAX_EDGE", 2000)
        if max_edge and max(image.size) > max_edge:
            image.thumbnail((max_edge, max_edge), Image.LANCZOS)

        buffer = io.BytesIO()
        image.save(buffer, format=target_format, **save_kwargs)
        size = buffer.tell()
    except Exception:
        # Corrupt or unsupported payload — hand it back untouched and let the
        # size check and the extension allow-list decide its fate.
        upload.seek(0)
        return upload

    if size >= upload.size:
        upload.seek(0)
        return upload

    buffer.seek(0)
    base = os.path.splitext(os.path.basename(upload.name or "image"))[0]
    return InMemoryUploadedFile(
        buffer, field_name=getattr(upload, "field_name", None),
        name=f"{base}{target_extension}",
        content_type="image/png" if target_format == "PNG" else "image/jpeg",
        size=size, charset=None,
    )


# --------------------------------------------------------------------------
# 3. the single entry point views call
# --------------------------------------------------------------------------

def check_extension(upload):
    allowed = getattr(settings, "ALLOWED_UPLOAD_EXTENSIONS", None)
    if not allowed:
        return
    extension = _extension(upload).lstrip(".")
    if extension not in allowed:
        raise ValidationError(
            f"“{upload.name}” is a .{extension or '?'} file, which isn't an "
            "accepted type. Convert it, zip it, or share a Drive link instead."
        )


def process_upload(upload, *, compress=True, max_bytes=_DEFAULT, max_edge=None):
    """Validate and (for images) shrink an uploaded file.

    Returns the file to store — possibly a re-encoded replacement. Raises
    ValidationError with a message written for the person uploading, including
    the instruction to fall back to a Drive link.

    `max_bytes` overrides the global ceiling for one call; `0` or `None` means
    no ceiling at all. `max_edge` does the same for the downscale. Both exist
    for the document-template pages, which are A4 print designs rather than
    attachments — see TEMPLATE_UPLOAD_MAX_BYTES in settings.

    The extension allow-list is NOT overridable and runs on every path. Size is
    a disk-space policy; the allow-list is the security control, and lifting a
    ceiling must never quietly lift that too.
    """
    if not upload:
        return upload

    check_extension(upload)

    if max_bytes is _DEFAULT:
        max_bytes = max_upload_bytes()

    if compress and is_image(upload):
        upload = compress_image(upload, max_edge=max_edge)

    if max_bytes and upload.size > max_bytes:
        raise ValidationError(
            f"“{upload.name}” is {human_size(upload.size)}. The limit is "
            f"{human_size(max_bytes)} per file — upload it to Google Drive and "
            "paste the share link in the “Drive link” box instead."
        )
    return upload


def normalise_drive_link(raw):
    """Accept a pasted share URL; reject anything that isn't http(s).

    A bare `javascript:` or `data:` value here would be rendered into an href
    and become stored XSS the moment a colleague clicks it.
    """
    url = (raw or "").strip()
    if not url:
        return ""
    if not url.lower().startswith(("http://", "https://")):
        if "." in url.split("/")[0]:
            url = f"https://{url}"  # "drive.google.com/..." pasted without a scheme
        else:
            raise ValidationError("That doesn't look like a link — paste the full https:// URL.")
    if len(url) > 500:
        raise ValidationError("That link is too long to store.")
    return url


# --------------------------------------------------------------------------
# 4. lifting the ceiling for one view
# --------------------------------------------------------------------------

def uncapped_uploads(view):
    """Let one view accept files past the global ceiling.

    `CappedFileUploadHandler` aborts the transfer mid-stream, long before any
    view runs, so a per-view exemption has to replace `request.upload_handlers`
    — and Django only allows that BEFORE the request body is read.

    That is the whole difficulty. `CsrfViewMiddleware` reads `request.POST` to
    find the token, which parses the body, which locks the handlers. So by the
    time a normal view is entered it is already too late. The documented way
    out is the one used here: exempt the view from the middleware's check, swap
    the handlers while the body is still unread, then run the check ourselves.

    `csrf_protect` below is not decoration. Without it this decorator would
    turn a state-changing view into an unauthenticated one, which is a far
    worse bug than the upload limit it exists to lift.

    It does NOT lift the extension allow-list, and it does not change what any
    other view accepts.
    """
    from django.core.files.uploadhandler import (MemoryFileUploadHandler,
                                                 TemporaryFileUploadHandler)
    from django.views.decorators.csrf import csrf_exempt, csrf_protect

    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.method == "POST":
            # The built-ins only: same behaviour as stock Django, minus the cap.
            request.upload_handlers = [
                MemoryFileUploadHandler(request),
                TemporaryFileUploadHandler(request),
            ]
        return csrf_protect(view)(request, *args, **kwargs)
    return wrapper
