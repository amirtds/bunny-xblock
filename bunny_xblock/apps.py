"""
Django AppConfig for the xblock-bunny package.

This is the entry point that Open edX's plugin system (`edx_django_utils.plugins`)
discovers via the `lms.djangoapp` and `cms.djangoapp` entry points in
`pyproject.toml`. Declaring `plugin_app` here is what makes Open edX:

  1. Add this Django app to INSTALLED_APPS for both LMS and CMS,
  2. Mount our URLConf at /api/xblock_bunny/ in both processes,
  3. Apply our settings module (settings/common.py) on boot.

No `edx-platform` edits are required — the platform reads the entry points on
startup and wires the app up automatically. The package can be pip-installed
on top of any modern Open edX (Sumac / Teak) and "just work".

References:
  - https://github.com/openedx/edx-django-utils/tree/master/edx_django_utils/plugins
  - https://docs.openedx.org/projects/edx-platform/en/latest/concepts/extension_points.html
"""

from django.apps import AppConfig


class BunnyXBlockAppConfig(AppConfig):
    name = "bunny_xblock"
    verbose_name = "Bunny.net Stream XBlock"
    default_auto_field = "django.db.models.BigAutoField"

    # Open edX plugin metadata. Each key under `plugin_app` is read by
    # edx_django_utils.plugins at process startup.
    plugin_app = {
        # Auto-mount URL routes under /api/xblock_bunny/ in both LMS and CMS.
        "url_config": {
            "lms.djangoapp": {
                "namespace": "xblock_bunny",
                "regex": "^api/xblock_bunny/",
                "relative_path": "urls",
            },
            "cms.djangoapp": {
                "namespace": "xblock_bunny",
                "regex": "^api/xblock_bunny/",
                "relative_path": "urls",
            },
        },
        # Apply settings/common.py during settings load (e.g. to extend
        # CSP_FRAME_SRC so the iframe.mediadelivery.net iframe is allowed).
        "settings_config": {
            "lms.djangoapp": {
                "common": {"relative_path": "settings.common"},
            },
            "cms.djangoapp": {
                "common": {"relative_path": "settings.common"},
            },
        },
    }
