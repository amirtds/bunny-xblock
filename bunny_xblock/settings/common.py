"""
Settings hook applied by Open edX's plugin loader (see apps.py `plugin_app`).

Adds `iframe.mediadelivery.net` to `CSP_FRAME_SRC` so the Bunny Stream embed
iframe isn't blocked on instances that enforce Content Security Policy.

Stays a no-op on instances without a CSP setting — most Open edX deployments
don't ship strict CSP by default, so this only kicks in where it's needed.
"""

BUNNY_EMBED_HOST = "https://iframe.mediadelivery.net"


def plugin_settings(settings):  # pragma: no cover — side-effect on a settings module
    """
    Open edX calls this with the live settings module. We only need to extend
    CSP_FRAME_SRC if it exists; everything else this XBlock needs is wired
    via INSTALLED_APPS and url_config, both of which Open edX handles for us.
    """
    existing = getattr(settings, "CSP_FRAME_SRC", None)
    if existing is None:
        # No CSP setting in this deployment; nothing to do.
        return

    if isinstance(existing, (list, tuple)):
        if BUNNY_EMBED_HOST not in existing:
            settings.CSP_FRAME_SRC = list(existing) + [BUNNY_EMBED_HOST]
    elif isinstance(existing, set):
        existing.add(BUNNY_EMBED_HOST)
    # else: unrecognised shape — leave alone rather than break the boot.
