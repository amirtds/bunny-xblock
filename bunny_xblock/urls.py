"""
URL routes for xblock-bunny's companion Django app.

Mounted at ``/api/xblock_bunny/`` in both LMS and CMS by the Open edX plugin
loader (see ``apps.BunnyXBlockAppConfig.plugin_app["url_config"]``).
"""

from django.urls import path

from .views import (
    EmbedUrlView,
    ThumbnailView,
    UploadTokenView,
    VideoDetailView,
    VideoFinalizeView,
)
from .webhooks import webhook

app_name = "xblock_bunny"

urlpatterns = [
    # Authoring (Studio-side, session-authenticated, staff-only)
    path("upload-token", UploadTokenView.as_view(), name="upload_token"),
    path("videos/<str:guid>", VideoDetailView.as_view(), name="video_detail"),
    path("videos/<str:guid>/finalize", VideoFinalizeView.as_view(), name="video_finalize"),
    path("videos/<str:guid>/thumbnail", ThumbnailView.as_view(), name="video_thumbnail"),
    path("embed-url", EmbedUrlView.as_view(), name="embed_url"),
    # Bunny → Cubite (public, token-in-URL auth)
    path("webhook/<str:token>", webhook, name="webhook"),
]
