"""
``BunnyVideoXBlock`` — the Studio-installable content block.

Per-instance scope: each XBlock holds one Bunny video. Authors drop or pick a
file in Studio, watch the upload + encoding progress inline, and the same
surface flips to the signed-iframe player when Bunny reports ``ready``. The
LMS-side ``student_view`` is a click-to-load poster that mounts the signed
iframe on click.

Credentials are global to the Open edX instance (managed at
``/admin/xblock_bunny/bunnyconfiguration/``) — this XBlock doesn't carry any
secrets itself, only video identifiers + display state.
"""

from __future__ import annotations

import html
import json
import logging
import os

import pkg_resources
from web_fragments.fragment import Fragment
from webob import Response
from xblock.core import XBlock
from xblock.fields import Integer, Scope, String

from . import bunny_api
from .models import BunnyVideo, TERMINAL_STATUSES

log = logging.getLogger(__name__)


def _resource(path: str) -> str:
    """Load a packaged template / static file as text."""
    return pkg_resources.resource_string(__name__, path).decode("utf-8")


VIDEO_STYLE_CHOICES = ("default", "rounded", "padded", "cinema", "compact")


@XBlock.needs("i18n")
class BunnyVideoXBlock(XBlock):
    """Embeds a Bunny.net Stream video with Token Authentication."""

    display_name = String(
        display_name="Display Name",
        default="Bunny Video",
        scope=Scope.settings,
        help="The display name for this video block in Studio's outline.",
    )

    # --- Video identity (filled in by author_view's upload flow) ----------------------

    guid = String(default="", scope=Scope.content)
    library_id = String(default="", scope=Scope.content)
    title = String(default="", scope=Scope.content)
    duration_sec = Integer(default=0, scope=Scope.content)
    thumbnail_url = String(default="", scope=Scope.content)
    status = String(default="", scope=Scope.content)

    # --- Presentation (v0.1 ships only "default" — kept for v0.2 picker) --------------

    video_style = String(
        default="default",
        scope=Scope.content,
        values=list(VIDEO_STYLE_CHOICES),
    )

    # Studio uses the live `author_view` (rendered inline on the unit page)
    # instead of falling back to the modal `studio_view`.
    has_author_view = True

    editable_fields = ("display_name",)

    # ---- LMS student view -----------------------------------------------------------

    def student_view(self, context=None) -> Fragment:
        """Renders the click-to-load poster + signed iframe player."""
        embed_url = ""
        if self.guid and self.library_id:
            try:
                embed_url = bunny_api.get_embed_url_for_video(
                    self.guid,
                    extra_query={"autoplay": "true", "preload": "true", "responsive": "true"},
                ) or ""
            except Exception as exc:  # pragma: no cover - defensive
                log.warning(
                    "[bunny:xblock] student_view embed signing failed",
                    extra={"guid": self.guid, "err": str(exc)},
                )

        # HTML-escape any user-controlled string before substitution. The
        # template uses str.format() (no jinja escaping) so a title containing
        # an unescaped `"` would break the surrounding attribute.
        safe_title = html.escape(self.title or "Bunny video", quote=True)
        safe_poster = html.escape(self.thumbnail_url or "", quote=True)
        safe_embed = html.escape(embed_url, quote=True)
        rendered = _resource("templates/bunny_xblock/student_view.html").format(
            self=self,
            has_video=bool(self.guid and self.library_id),
            poster_url=safe_poster,
            embed_url=safe_embed,
            title=safe_title,
        )
        fragment = Fragment(rendered)
        fragment.add_css(_resource("static/css/student_view.css"))
        fragment.add_javascript(_resource("static/js/student_view.js"))
        fragment.initialize_js(
            "BunnyStudentView",
            {"embedUrl": embed_url, "title": self.title or "Bunny video"},
        )
        return fragment

    # ---- Studio author view (inline) ------------------------------------------------

    def author_view(self, context=None) -> Fragment:
        """
        The inline Studio authoring UI.

        Renders three states purely on the server (drives the initial UI before
        JS hydrates): empty / processing / ready. Failed plus the upload-progress
        transient are JS-managed.
        """
        state = self._compute_view_state()
        embed_url = ""
        if state == "ready" and self.guid and self.library_id:
            try:
                embed_url = bunny_api.get_embed_url_for_video(
                    self.guid,
                    extra_query={"autoplay": "false", "preload": "false", "responsive": "true"},
                ) or ""
            except Exception as exc:  # pragma: no cover
                log.warning(
                    "[bunny:xblock] author_view embed signing failed",
                    extra={"guid": self.guid, "err": str(exc)},
                )

        # Same escape discipline as the student view above — substitution is
        # str.format() so values that flow into HTML attributes must be safe.
        safe_title = html.escape(self.title or "", quote=True)
        safe_poster = html.escape(self.thumbnail_url or "", quote=True)
        safe_embed = html.escape(embed_url, quote=True)
        rendered = _resource("templates/bunny_xblock/author_view.html").format(
            self=self,
            state=state,
            poster_url=safe_poster,
            embed_url=safe_embed,
            title=safe_title,
        )
        fragment = Fragment(rendered)
        fragment.add_css(_resource("static/css/author_view.css"))
        fragment.add_javascript(_resource("static/js/vendor/tus.min.js"))
        fragment.add_javascript(_resource("static/js/author_view.js"))
        fragment.initialize_js(
            "BunnyAuthorView",
            {
                "guid": self.guid,
                "libraryId": self.library_id,
                "status": self.status,
                "title": self.title,
                "embedUrl": embed_url,
                "endpoints": {
                    "uploadToken": "/api/xblock_bunny/upload-token",
                    "finalize": "/api/xblock_bunny/videos/{guid}/finalize",
                    "videoDetail": "/api/xblock_bunny/videos/{guid}",
                    "embedUrl": "/api/xblock_bunny/embed-url",
                    "tusEndpoint": "https://video.bunnycdn.com/tusupload",
                },
            },
        )
        return fragment

    # If Studio ever falls back to studio_view (e.g. older runtimes), give
    # it the same inline UI rather than a stub edit form.
    def studio_view(self, context=None) -> Fragment:  # pragma: no cover
        return self.author_view(context)

    # ---- JSON handlers (called by author_view.js) -----------------------------------

    @XBlock.json_handler
    def set_video(self, data, suffix=""):
        """
        Persist a freshly-uploaded video into this block.

        Called from author_view.js after the TUS upload + ``/finalize`` chain
        completes. Body shape: ``{guid, library_id, title?, duration_sec?,
        thumbnail_url?, status?}``.
        """
        guid = (data.get("guid") or "").strip()
        library_id = (data.get("library_id") or "").strip()
        if not guid or not library_id:
            return {"ok": False, "error": "guid and library_id are required"}

        self.guid = guid
        self.library_id = library_id
        new_title = (data.get("title") or self.title or "")[:250]
        self.title = new_title
        # Mirror the title into display_name so Studio's outline shows the
        # video's actual name (otherwise every block reads "Bunny Video"
        # forever). Only overwrite the default — respect any explicit
        # display name the author already set in Settings.
        if new_title and self.display_name in ("Bunny Video", ""):
            self.display_name = new_title
        if isinstance(data.get("duration_sec"), int):
            self.duration_sec = data["duration_sec"]
        self.thumbnail_url = (data.get("thumbnail_url") or "")[:1000]
        self.status = data.get("status") or self.status or "pending"
        return {"ok": True}

    @XBlock.json_handler
    def update_status(self, data, suffix=""):
        """Sync polled status into the block's field so a Studio reload reflects it."""
        new_status = (data.get("status") or "").strip()
        if new_status:
            self.status = new_status
        if isinstance(data.get("duration_sec"), int):
            self.duration_sec = data["duration_sec"]
        if data.get("thumbnail_url"):
            self.thumbnail_url = data["thumbnail_url"][:1000]
        return {"ok": True, "status": self.status}

    @XBlock.json_handler
    def update_title(self, data, suffix=""):
        title = (data.get("title") or "").strip()[:250]
        self.title = title
        # Same outline-sync behaviour as set_video: track the title in
        # display_name so the Studio outline doesn't stay generic.
        if title and self.display_name in ("Bunny Video", ""):
            self.display_name = title
        return {"ok": True, "title": title}

    @XBlock.json_handler
    def clear_video(self, data, suffix=""):
        """
        Detach the video from this block (does NOT delete it from Bunny — the
        author_view JS calls DELETE /api/xblock_bunny/videos/<guid> directly).
        """
        self.guid = ""
        self.library_id = ""
        self.title = ""
        self.duration_sec = 0
        self.thumbnail_url = ""
        self.status = ""
        return {"ok": True}

    # ---- Helpers --------------------------------------------------------------------

    def _compute_view_state(self) -> str:
        if not self.guid or not self.library_id:
            return "empty"
        if self.status in TERMINAL_STATUSES:
            return "ready" if self.status == "ready" else "failed"
        if self.status:
            return "processing"
        # We have a guid but no status — probably just-set. Treat as processing
        # so the JS polls for the real state instead of jumping to ready blank.
        return "processing"

    # ---- Studio workbench scenarios -------------------------------------------------
    #
    # These let `workbench` (the standalone XBlock test harness) load the
    # block without an Open edX runtime — handy for local development.

    @staticmethod
    def workbench_scenarios():  # pragma: no cover
        return [
            (
                "Empty Bunny Video block",
                "<bunny_video />",
            ),
        ]
