"""Google Tag Manager install.

GTM is the only snippet on the site. GA4 (page views, sources) is added
as a tag inside the GTM container — not a second gtag.js on the page.

The container ID comes from GTM_CONTAINER_ID (GTM-XXXX). Empty = no
snippet, so local/dev does not phone home. Injection happens in
_render() so a new template cannot forget the tag.
"""
from __future__ import annotations

import re

_GTM_RE = re.compile(r"^GTM-[A-Z0-9]+$")


def normalize_container_id(raw: str) -> str:
    cid = (raw or "").strip().upper()
    return cid if _GTM_RE.match(cid) else ""


def _head_snippet(container_id: str) -> str:
    return (
        "\n<!-- Google Tag Manager -->\n"
        "<script>(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':"
        "new Date().getTime(),event:'gtm.js'});var f=d.getElementsByTagName(s)[0],"
        "j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src="
        "'https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);"
        f"}})(window,document,'script','dataLayer','{container_id}');</script>\n"
        "<!-- End Google Tag Manager -->\n"
    )


def _body_snippet(container_id: str) -> str:
    return (
        "\n<!-- Google Tag Manager (noscript) -->\n"
        f'<noscript><iframe src="https://www.googletagmanager.com/ns.html?id={container_id}" '
        'height="0" width="0" style="display:none;visibility:hidden"></iframe></noscript>\n'
        "<!-- End Google Tag Manager (noscript) -->\n"
    )


def inject(html: str, container_id: str) -> str:
    """Insert official GTM snippets into a full HTML document.

    Head script goes immediately after <head>. Noscript goes immediately
    after <body>. Fragments (HTMX pieces without a document) are left
    alone. Already-tagged pages are not double-injected.
    """
    cid = normalize_container_id(container_id)
    if not cid or not html:
        return html
    lead = html.lstrip()[:16].lower()
    if not (lead.startswith("<!doctype") or lead.startswith("<html")):
        return html
    if "googletagmanager.com/gtm.js" in html:
        return html

    out = html
    lower = out.lower()
    hi = lower.find("<head")
    if hi >= 0:
        hj = out.find(">", hi)
        if hj >= 0:
            out = out[: hj + 1] + _head_snippet(cid) + out[hj + 1 :]

    lower = out.lower()
    bi = lower.find("<body")
    if bi >= 0:
        bj = out.find(">", bi)
        if bj >= 0:
            out = out[: bj + 1] + _body_snippet(cid) + out[bj + 1 :]
    return out
