"""
REST endpoints for the xblock-bunny Studio authoring UI.

All paths live under ``/api/xblock_bunny/`` (mounted by ``urls.py`` via the
Open edX plugin loader). Each endpoint mirrors one Cubite Next.js route in
``cubite/app/api/bunny/`` so the behaviour is identical:

- ``POST /upload-token`` — mint TUS signature + create BunnyVideo row.
- ``POST /videos/<guid>/finalize`` — reconcile metadata after upload.
- ``GET  /videos/<guid>`` — return cached row, refresh from Bunny only when stale.
- ``DELETE /videos/<guid>`` — Bunny DELETE + local row remove.
- ``GET  /embed-url`` — return a freshly signed embed URL for the author preview.

Auth: Django session + ``IsStaffUser``. Coarse but adequate for v0.1 — see
``permissions.py`` for the rationale.

Rate limiting: small in-process map (60/min mint, 120/min delete) matching the
Cubite M4 fix. Resets on process restart; sufficient for one Open edX node, and
upgrading to Redis is a v0.2 question.
"""

from __future__ import annotations

import logging
import time

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import bunny_api
from .models import BunnyVideo, TERMINAL_STATUSES
from .permissions import IsStaffUser

log = logging.getLogger(__name__)

# ---- Tiny in-process rate limiter --------------------------------------------------------


_RL_WINDOW_S = 60
_RL_MINT_MAX = 60
_RL_DELETE_MAX = 120
_rl_state: dict[str, tuple[int, int]] = {}  # key → (count, reset_at)


def _rate_limit(key: str, max_in_window: int) -> bool:
    now = int(time.time())
    count, reset_at = _rl_state.get(key, (0, 0))
    if now >= reset_at:
        _rl_state[key] = (1, now + _RL_WINDOW_S)
        return True
    if count >= max_in_window:
        return False
    _rl_state[key] = (count + 1, reset_at)
    return True


# ---- Error helper ---------------------------------------------------------------------


def _bunny_config_or_error():
    """
    Load BunnyConfig or return a (response,) tuple to short-circuit the view.

    Returning a tuple is the cheapest way to signal "render this Response and
    stop" without raising — keeps the view bodies linear.
    """
    try:
        return bunny_api.load_config(), None
    except bunny_api.BunnyNotConfiguredError:
        return None, Response(
            {"error": "Bunny Stream is not configured. Set credentials in Django admin."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except bunny_api.BunnyKeyUndecryptableError:
        log.error("[bunny:lib] api_key_undecryptable")
        return None, Response(
            {"error": "Stored Bunny API key could not be decrypted. Re-enter it in Django admin."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# ---- Endpoints ------------------------------------------------------------------------


class UploadTokenView(APIView):
    """``POST /upload-token`` — body ``{title?}``."""

    permission_classes = [IsAuthenticated, IsStaffUser]

    def post(self, request):
        title = (request.data.get("title") or "Untitled video").strip()[:250]

        if not _rate_limit(f"mint:{request.user.id}", _RL_MINT_MAX):
            return Response(
                {"error": "Too many uploads. Try again in a minute."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        cfg, err = _bunny_config_or_error()
        if err is not None:
            return err

        try:
            guid = bunny_api.create_bunny_video(cfg, title)
        except bunny_api.BunnyAPIError as exc:
            log.error(
                "[bunny:upload-token] createBunnyVideo_failed",
                extra={"status": exc.status, "err": str(exc)},
            )
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        # Persist the row before returning the signature. If insert fails, roll
        # the Bunny-side video back so we don't leak a billed orphan (Cubite H1).
        try:
            BunnyVideo.objects.create(
                guid=guid,
                library_id=cfg.library_id,
                title=title,
                status=bunny_api.STATUS_PENDING,
                created_by_id=request.user.id,
            )
        except Exception as exc:  # pragma: no cover - rare
            log.error(
                "[bunny:upload-token] prisma_insert_failed",
                extra={"guid": guid, "err": str(exc)},
            )
            try:
                bunny_api.delete_bunny_video(cfg, guid)
            except bunny_api.BunnyAPIError as rollback_exc:
                log.error(
                    "[bunny:upload-token] rollback_delete_failed cleanup_required_manual",
                    extra={"guid": guid, "err": str(rollback_exc)},
                )
            return Response(
                {"error": "Could not record upload. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        signature = bunny_api.sign_tus_upload(cfg, guid)
        return Response(
            {
                "guid": guid,
                "library_id": cfg.library_id,
                "expires": signature["expires"],
                "signature": signature["signature"],
            }
        )


class VideoFinalizeView(APIView):
    """``POST /videos/<guid>/finalize`` — reconcile metadata after a successful upload."""

    permission_classes = [IsAuthenticated, IsStaffUser]

    def post(self, request, guid: str):
        row = get_object_or_404(BunnyVideo, guid=guid)
        cfg, err = _bunny_config_or_error()
        if err is not None:
            return err

        try:
            meta = bunny_api.get_bunny_video(cfg, guid)
        except bunny_api.BunnyAPIError as exc:
            log.error("[bunny:finalize] getBunnyVideo_failed", extra={"guid": guid, "err": str(exc)})
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        if meta is None:
            return Response({"error": "Video not found on Bunny"}, status=status.HTTP_404_NOT_FOUND)

        row.status = bunny_api.map_bunny_status(meta.get("status"))
        thumb = bunny_api.bunny_thumbnail_url(cfg.cdn_hostname, guid, meta.get("thumbnailFileName"))
        if thumb:
            row.thumbnail_url = thumb
        length = meta.get("length")
        if isinstance(length, (int, float)) and length > 0:
            row.duration_sec = int(round(length))
        if meta.get("title"):
            row.title = meta["title"][:250]
        row.save()

        return Response(_serialize(row))


class VideoDetailView(APIView):
    """``GET`` / ``DELETE`` on ``/videos/<guid>``."""

    permission_classes = [IsAuthenticated, IsStaffUser]

    REFRESH_FLOOR_S = 30

    def get(self, request, guid: str):
        row = get_object_or_404(BunnyVideo, guid=guid)
        # Mirror Cubite H3: skip the Bunny round-trip if we updated recently.
        if row.status not in TERMINAL_STATUSES:
            age = (time.time()) - row.updated_at.timestamp()
            if age > self.REFRESH_FLOOR_S:
                cfg, err = _bunny_config_or_error()
                if err is not None:
                    return err
                try:
                    meta = bunny_api.get_bunny_video(cfg, guid)
                except bunny_api.BunnyAPIError as exc:
                    log.error(
                        "[bunny:videos.get] sync_failed_returning_cached",
                        extra={"guid": guid, "err": str(exc)},
                    )
                    meta = None
                if meta:
                    row.status = bunny_api.map_bunny_status(meta.get("status"))
                    thumb = bunny_api.bunny_thumbnail_url(
                        cfg.cdn_hostname, guid, meta.get("thumbnailFileName")
                    )
                    if thumb:
                        row.thumbnail_url = thumb
                    length = meta.get("length")
                    if isinstance(length, (int, float)) and length > 0:
                        row.duration_sec = int(round(length))
                    row.save()
        return Response(_serialize(row))

    def delete(self, request, guid: str):
        row = get_object_or_404(BunnyVideo, guid=guid)
        if not _rate_limit(f"delete:{request.user.id}", _RL_DELETE_MAX):
            return Response(
                {"error": "Too many delete requests. Try again in a minute."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        cfg, err = _bunny_config_or_error()
        if err is not None:
            return err

        try:
            bunny_api.delete_bunny_video(cfg, guid)
        except bunny_api.BunnyAPIError as exc:
            log.error("[bunny:videos.delete] deleteBunnyVideo_failed", extra={"guid": guid, "err": str(exc)})
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        row.delete()
        return Response({"ok": True})


class EmbedUrlView(APIView):
    """``GET /embed-url?guid=...`` — return a freshly signed embed URL."""

    permission_classes = [IsAuthenticated, IsStaffUser]

    def get(self, request):
        guid = (request.query_params.get("guid") or "").strip()
        if not guid:
            return Response({"error": "Missing guid"}, status=status.HTTP_400_BAD_REQUEST)
        url = bunny_api.get_embed_url_for_video(
            guid,
            extra_query={"autoplay": "true", "preload": "true", "responsive": "true"},
        )
        if not url:
            return Response({"error": "Video not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response({"url": url})


# ---- Serialization --------------------------------------------------------------------


def _serialize(row: BunnyVideo) -> dict:
    return {
        "guid": row.guid,
        "library_id": row.library_id,
        "title": row.title,
        "status": row.status,
        "duration_sec": row.duration_sec,
        "thumbnail_url": row.thumbnail_url or None,
    }
