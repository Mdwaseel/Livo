"""The icon set, as SVG path data.

Inline SVG rather than an icon font or emoji: emoji render differently on every
phone a client might open the link on, and an icon font is a network request
that fails into empty squares. Outline style at a 24px grid, the same stroke
language as the rest of the app's icons. Brand marks are simplified outlines —
recognisable at 14px without reproducing anybody's trademark artwork.

Only ever static strings from this file are marked safe; nothing user-supplied
is interpolated into them.
"""
from django.utils.html import format_html
from django.utils.safestring import mark_safe

PATHS = {
    # --- activity kinds ---
    "post": '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="2"/>'
            '<path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
    "carousel": '<rect x="6" y="4" width="12" height="16" rx="2"/><path d="M2 7v10"/><path d="M22 7v10"/>',
    "reel": '<rect x="2" y="6" width="14" height="12" rx="2"/><path d="m22 8-6 4 6 4z"/>',
    "story": '<circle cx="12" cy="12" r="9" stroke-dasharray="4 2.6"/><circle cx="12" cy="12" r="3.2"/>',
    "ad_launch": '<path d="m3 11 18-5v12L3 14z"/><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6"/>',
    "ad_change": '<path d="M4 21v-7"/><path d="M4 10V3"/><path d="M12 21v-9"/><path d="M12 8V3"/>'
                 '<path d="M20 21v-5"/><path d="M20 12V3"/><path d="M1 14h6"/><path d="M9 8h6"/>'
                 '<path d="M17 16h6"/>',
    "ad_review": '<path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>',
    "creative": '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    "report": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>'
              '<path d="M14 2v6h6"/><path d="M8 13h8"/><path d="M8 17h5"/>',
    "meeting": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>'
               '<path d="M23 21v-2a4 4 0 0 0-3-3.9"/><path d="M16 3.1a4 4 0 0 1 0 7.8"/>',
    "website": '<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/>'
               '<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
    "other": '<path d="M12 3 13.9 8.8 20 11l-6.1 2.2L12 19l-1.9-5.8L4 11l6.1-2.2z"/>',

    # --- platforms ---
    "instagram": '<rect x="2" y="2" width="20" height="20" rx="5"/>'
                 '<path d="M16 11.4A4 4 0 1 1 12.6 8 4 4 0 0 1 16 11.4z"/><path d="M17.5 6.5h.01"/>',
    "facebook": '<path d="M18 2h-3a5 5 0 0 0-5 5v3H7v4h3v8h4v-8h3l1-4h-4V7a1 1 0 0 1 1-1h3z"/>',
    "linkedin": '<path d="M16 8a6 6 0 0 1 6 6v7h-4v-7a2 2 0 0 0-4 0v7h-4v-7a6 6 0 0 1 6-6z"/>'
                '<rect x="2" y="9" width="4" height="12"/><circle cx="4" cy="4" r="2"/>',
    "youtube": '<path d="M2.5 17a24 24 0 0 1 0-10 2 2 0 0 1 1.4-1.4 49.6 49.6 0 0 1 16.2 0A2 2 0 0 1 21.5 7'
               'a24 24 0 0 1 0 10 2 2 0 0 1-1.4 1.4 49.6 49.6 0 0 1-16.2 0A2 2 0 0 1 2.5 17"/>'
               '<path d="m10 15 5-3-5-3z"/>',
    "x": '<path d="M4 4l6.9 9.2L4.4 20h1.6l5.6-6.1L16 20h4l-7.3-9.7L18.8 4h-1.6l-5.2 5.6L7.9 4z"/>',
    "google_ads": '<path d="M8.5 3.5 2.6 13.8a3.3 3.3 0 1 0 5.7 3.3l5.9-10.3"/>'
                  '<path d="m14.2 6.8 5.6 9.9a3.3 3.3 0 0 1-5.7 3.3L8.5 10.1"/>',
    "meta_ads": '<path d="M2 15.5c0-4.6 2.2-8.5 4.6-8.5 3.8 0 6.9 11 10.8 11 2 0 4.6-1.6 4.6-5.5 0-3.4-1.8-5.5-3.9-5.5-3.6 0-6.4 10-10.6 10C4 17 2 16.5 2 15.5z"/>',
    "email": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 5L2 7"/>',
    "whatsapp": '<path d="M3 21l1.7-5A9 9 0 1 1 8 19.4z"/><path d="M9 10a.5.5 0 0 0 1 0V9a.5.5 0 0 0-1 0v1a5 5 0 0 0 5 5h1a.5.5 0 0 0 0-1h-1a.5.5 0 0 0 0 1"/>',

    # --- interface ---
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "alert": '<circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
    "pause": '<circle cx="12" cy="12" r="10"/><path d="M10 15V9"/><path d="M14 15V9"/>',
    "progress": '<path d="M21 12a9 9 0 1 1-6.2-8.6"/>',
    "calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4"/><path d="M8 2v4"/><path d="M3 10h18"/>',
    "list": '<path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M3 6h.01"/><path d="M3 12h.01"/><path d="M3 18h.01"/>',
    "chevron_left": '<path d="m15 18-6-6 6-6"/>',
    "chevron_right": '<path d="m9 18 6-6-6-6"/>',
    "close": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "external": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "plus": '<path d="M12 5v14"/><path d="M5 12h14"/>',
    "eye_off": '<path d="M9.9 4.2A10 10 0 0 1 12 4c7 0 10 8 10 8a13 13 0 0 1-1.7 2.7"/>'
               '<path d="M6.6 6.6A13 13 0 0 0 2 12s3 8 10 8a9.7 9.7 0 0 0 5.4-1.6"/><path d="m2 2 20 20"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-3.2-6.9"/><path d="M21 3v6h-6"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',

    # --- approvals ---
    "badge_check": '<path d="M3.85 8.62a4 4 0 0 1 4.78-4.77 4 4 0 0 1 6.74 0 4 4 0 0 1 4.78 4.78 4 4 0 0 1 0 6.74'
                   ' 4 4 0 0 1-4.77 4.78 4 4 0 0 1-6.75 0 4 4 0 0 1-4.78-4.77 4 4 0 0 1 0-6.76z"/>'
                   '<path d="m9 12 2 2 4-4"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/>'
             '<path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    "send": '<path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
    "message": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "edit": '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
    "inbox": '<path d="M22 12h-6l-2 3h-4l-2-3H2"/>'
             '<path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    "undo": '<path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/>',
    "zoom": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/><path d="M11 8v6"/><path d="M8 11h6"/>',
}


def svg(name, css_class="cc-ico"):
    """`<svg>` markup for a named icon, or an empty string for an unknown one.

    An unknown name renders nothing rather than raising: a platform added to
    the choices before an icon exists for it should cost the icon, not the page.
    """
    inner = PATHS.get(name)
    if inner is None:
        return ""
    return format_html(
        '<svg class="{}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" '
        'aria-hidden="true" focusable="false">{}</svg>',
        css_class, mark_safe(inner))
