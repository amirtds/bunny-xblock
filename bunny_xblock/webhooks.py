"""
Bunny.net Stream webhook receiver.

Bunny POSTs encoding-status events to a per-instance public URL of the form::

    https://<lms-host>/api/xblock_bunny/webhook/<webhook_secret>

The secret in the URL is the only authentication Bunny supports for Stream
webhooks (Bunny does not sign payloads). Defense-in-depth mirrors Cubite's
``app/api/bunny/webhook/[token]/route.ts`` exactly:

  1. Constant-time-equivalent secret lookup (single DB index hit on a UNIQUE
     column — the attacker can't time-side-channel which prefix matched).
  2. Library mismatch check: event's ``VideoLibraryId`` must equal the
     configured library_id.
  3. Lifecycle-regression guard: rows already in a terminal status
     (``ready``/``failed``) can't be downgraded — protects against forged
     events from a leaked URL.
  4. Structured ``[bunny:webhook]`` logs on every rejection so credential
     leaks become visible to ops.

Bunny payload shape (Stream):
    { VideoLibraryId: int, VideoGuid: str, Status: int }
"""

from __future__ import annotations

import hmac
import json
import logging

from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import bunny_api
from .models import BunnyConfiguration, BunnyVideo, TERMINAL_STATUSES

log = logging.getLogger(__name__)


def _reject(reason: str, status: int, **ctx) -> HttpResponse:
    log.warning("[bunny:webhook] reject:%s %s", reason, ctx)
    return HttpResponse(
        json.dumps({"error": reason}),
        status=status,
        content_type="application/json",
    )


def _ok(note: str | None = None) -> HttpResponse:
    body = {"ok": True}
    if note:
        body["note"] = note
    return HttpResponse(json.dumps(body), content_type="application/json")


@csrf_exempt
@require_POST
def webhook(request, token: str):
    """Public endpoint — token-only auth, no session."""
    if not token or len(token) < 16:
        return _reject("malformed_token", 401, token_len=len(token or ""))

    cfg_row = BunnyConfiguration.objects.filter(webhook_secret=token).first()
    if cfg_row is None:
        return _reject("unknown_token", 401)

    # Defense in depth — even though we found a config row matching the URL
    # token, also constant-time-compare in case of any future codepath that
    # hits this without the unique-index guarantee.
    if not hmac.compare_digest(cfg_row.webhook_secret or "", token):
        return _reject("token_compare_mismatch", 401)

    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return _reject("invalid_json", 400)

    guid = (body.get("VideoGuid") or "").strip()
    event_library_id = "" if body.get("VideoLibraryId") is None else str(body["VideoLibraryId"])
    status_code = body.get("Status")
    if not guid or not event_library_id or not isinstance(status_code, int):
        return _reject(
            "malformed_payload",
            400,
            has_guid=bool(guid),
            has_lib=bool(event_library_id),
            status_type=type(status_code).__name__,
        )

    # Library mismatch: stale webhook from a recycled library, or a forged
    # event from someone with the URL but pointed at a foreign library.
    if not cfg_row.library_id or not hmac.compare_digest(cfg_row.library_id, event_library_id):
        return _reject(
            "library_mismatch",
            403,
            event_library_id=event_library_id,
            configured=cfg_row.library_id,
        )

    try:
        row = BunnyVideo.objects.get(guid=guid)
    except BunnyVideo.DoesNotExist:
        # H1-style orphan or admin purge. Acknowledge so Bunny stops retrying,
        # but log so ops can spot leaked-budget videos.
        return _ok("unknown_video")

    if row.library_id != event_library_id:
        return _reject(
            "tenant_mismatch",
            403,
            event_library_id=event_library_id,
            row_library_id=row.library_id,
        )

    new_status = bunny_api.map_bunny_status(status_code)

    # Reject lifecycle regressions. Bunny's state machine only walks forward —
    # any attempt to flip ready/failed back to a non-terminal state is either
    # a stale retry from before a manual reset or a forged event.
    if row.status in TERMINAL_STATUSES and new_status != row.status:
        return _reject(
            "terminal_state_regression",
            200,
            guid=guid,
            current=row.status,
            incoming=new_status,
        )

    # On material transitions, refresh metadata from Bunny so the studio UI
    # has duration + thumbnail. Skipped when status hasn't changed to avoid
    # hammering Bunny on noisy webhooks.
    if new_status != row.status and new_status in (bunny_api.STATUS_READY, bunny_api.STATUS_ENCODING):
        try:
            cfg = bunny_api.load_config()
            meta = bunny_api.get_bunny_video(cfg, guid)
        except (bunny_api.BunnyNotConfiguredError, bunny_api.BunnyKeyUndecryptableError) as exc:
            log.error("[bunny:webhook] config_unavailable_for_meta_refresh", extra={"guid": guid, "err": str(exc)})
            meta = None
        except bunny_api.BunnyAPIError as exc:
            log.error("[bunny:webhook] meta_refresh_failed", extra={"guid": guid, "err": str(exc)})
            meta = None
        if meta:
            thumb = bunny_api.bunny_thumbnail_url(
                cfg.cdn_hostname, guid, meta.get("thumbnailFileName")
            )
            if thumb:
                row.thumbnail_url = thumb
            length = meta.get("length")
            if isinstance(length, (int, float)) and length > 0:
                row.duration_sec = int(round(length))

    row.status = new_status
    row.save()

    log.info("[bunny:webhook] status_update", extra={"guid": guid, "status": new_status})
    return _ok()
