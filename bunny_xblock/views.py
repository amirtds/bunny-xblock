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
import re
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
        raw_title = (request.data.get("title") or "Untitled video").strip()
        # Strip the trailing file extension from filename-style titles
        # (`promo-video-XYZ.mp4` → `promo-video-XYZ`). Hyphens and underscores
        # are preserved on purpose — they're often intentional file refs and
        # authors rename freely afterwards.
        if "." in raw_title:
            stem, _, ext = raw_title.rpartition(".")
            # Only strip if the extension looks like a real file extension
            # (1–6 alphanumeric chars, no spaces). Otherwise leave the dot
            # alone — e.g. a title like "Mr. Robinson's lecture" should not
            # become "Mr".
            if stem and ext and ext.isalnum() and 1 <= len(ext) <= 6:
                raw_title = stem
        title = raw_title[:250] or "Untitled video"

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


class CaptionsView(APIView):
    """
    ``GET /videos/<guid>/captions`` — list attached caption tracks.
    ``POST /videos/<guid>/captions`` — upload a VTT file as a new caption.

    POST is multipart: ``vtt`` file, plus ``srclang`` and ``label`` form
    fields. We pass the file bytes through to Bunny as a base64-encoded
    payload (Bunny's wire shape for this endpoint).
    """

    permission_classes = [IsAuthenticated, IsStaffUser]
    parser_classes = []

    ALLOWED_VTT = {"text/vtt", "text/plain", "application/octet-stream", ""}
    MAX_BYTES = 1 * 1024 * 1024  # 1 MB — VTT is text, this is plenty
    LANG_RE = re.compile(r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})?$")

    def get(self, request, guid: str):
        get_object_or_404(BunnyVideo, guid=guid)
        cfg, err = _bunny_config_or_error()
        if err is not None:
            return err
        try:
            captions = bunny_api.list_bunny_captions(cfg, guid)
        except bunny_api.BunnyAPIError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"captions": captions})

    def post(self, request, guid: str):
        get_object_or_404(BunnyVideo, guid=guid)
        upload = request.FILES.get("vtt")
        srclang = (request.data.get("srclang") or "").strip().lower()
        label = (request.data.get("label") or "").strip()[:60]

        if not upload:
            return Response({"error": "Missing 'vtt' file."}, status=status.HTTP_400_BAD_REQUEST)
        if not self.LANG_RE.match(srclang):
            return Response(
                {"error": "Language code must look like 'en' or 'pt-BR'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if upload.size and upload.size > self.MAX_BYTES:
            return Response(
                {"error": f"VTT file too large ({upload.size} bytes). Limit is {self.MAX_BYTES} bytes."},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
        content_type = (upload.content_type or "").lower()
        if content_type and content_type not in self.ALLOWED_VTT:
            return Response(
                {"error": f"Unsupported caption type ({content_type}). Upload a .vtt file."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cfg, err = _bunny_config_or_error()
        if err is not None:
            return err
        try:
            bunny_api.upload_bunny_caption(cfg, guid, srclang, label or srclang.upper(), upload.read())
            captions = bunny_api.list_bunny_captions(cfg, guid)
        except bunny_api.BunnyAPIError as exc:
            log.error("[bunny:captions] upload_failed", extra={"guid": guid, "err": str(exc)})
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({"ok": True, "captions": captions})


class CaptionDeleteView(APIView):
    """``DELETE /videos/<guid>/captions/<srclang>`` — remove a caption."""

    permission_classes = [IsAuthenticated, IsStaffUser]

    def delete(self, request, guid: str, srclang: str):
        get_object_or_404(BunnyVideo, guid=guid)
        srclang = (srclang or "").strip().lower()
        if not srclang:
            return Response({"error": "Missing srclang"}, status=status.HTTP_400_BAD_REQUEST)
        cfg, err = _bunny_config_or_error()
        if err is not None:
            return err
        try:
            bunny_api.delete_bunny_caption(cfg, guid, srclang)
            captions = bunny_api.list_bunny_captions(cfg, guid)
        except bunny_api.BunnyAPIError as exc:
            log.error("[bunny:captions] delete_failed", extra={"guid": guid, "err": str(exc)})
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"ok": True, "captions": captions})


class TranscribeView(APIView):
    """
    ``POST /videos/<guid>/transcribe`` — kick off Bunny's auto-transcription.

    Body: optional ``language`` (default ``en``). Async on Bunny's side —
    the caller polls ``GET /captions`` to discover when the new track lands.
    """

    permission_classes = [IsAuthenticated, IsStaffUser]

    LANG_RE = re.compile(r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})?$")

    def post(self, request, guid: str):
        get_object_or_404(BunnyVideo, guid=guid)
        language = (request.data.get("language") or "en").strip().lower()
        force = bool(request.data.get("force"))
        if not self.LANG_RE.match(language):
            return Response(
                {"error": "Language code must look like 'en' or 'pt-BR'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        cfg, err = _bunny_config_or_error()
        if err is not None:
            return err
        try:
            bunny_api.transcribe_bunny_video(cfg, guid, language=language, force=force)
        except bunny_api.BunnyAPIError as exc:
            log.error("[bunny:transcribe] failed", extra={"guid": guid, "err": str(exc)})
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"ok": True, "language": language})


class ChaptersView(APIView):
    """
    ``GET /videos/<guid>/chapters`` — return current chapter list.
    ``PUT /videos/<guid>/chapters`` — replace chapter list.

    PUT body: ``{ chapters: [{ title, start, end }, ...] }`` with seconds
    for ``start`` and ``end``. Validation enforces non-negative integers and
    monotonic order so authors can't ship overlapping markers that confuse
    Bunny's player.
    """

    permission_classes = [IsAuthenticated, IsStaffUser]

    MAX_CHAPTERS = 50
    MAX_TITLE = 120

    def get(self, request, guid: str):
        get_object_or_404(BunnyVideo, guid=guid)
        cfg, err = _bunny_config_or_error()
        if err is not None:
            return err
        try:
            chapters = bunny_api.get_bunny_chapters(cfg, guid)
        except bunny_api.BunnyAPIError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"chapters": chapters})

    def put(self, request, guid: str):
        row = get_object_or_404(BunnyVideo, guid=guid)
        raw = request.data.get("chapters")
        if not isinstance(raw, list):
            return Response(
                {"error": "Body must include a 'chapters' array."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(raw) > self.MAX_CHAPTERS:
            return Response(
                {"error": f"Too many chapters ({len(raw)}). Max is {self.MAX_CHAPTERS}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate + normalize. Sort by start time, derive `end` for chapters
        # missing one by using the next chapter's `start` (Bunny will accept
        # this; matches how the player visually segments).
        cleaned = []
        for i, ch in enumerate(raw):
            if not isinstance(ch, dict):
                return Response(
                    {"error": f"Chapter {i} is not an object."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            title = str(ch.get("title") or "").strip()[: self.MAX_TITLE]
            try:
                start = max(0, int(ch.get("start") or 0))
                end = max(0, int(ch.get("end") or 0))
            except (TypeError, ValueError):
                return Response(
                    {"error": f"Chapter {i} has non-numeric start/end."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not title:
                return Response(
                    {"error": f"Chapter {i} needs a title."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if end and end < start:
                return Response(
                    {"error": f"Chapter {i} has end < start."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            cleaned.append({"title": title, "start": start, "end": end})

        cleaned.sort(key=lambda c: c["start"])

        # Backfill missing/zero ends with the next chapter's start (or video
        # duration if known) so Bunny's player can paint the timeline.
        duration = row.duration_sec or 0
        for i, ch in enumerate(cleaned):
            if not ch["end"]:
                if i + 1 < len(cleaned):
                    ch["end"] = cleaned[i + 1]["start"]
                elif duration:
                    ch["end"] = duration
                else:
                    # Best guess — 60s segment if we have neither.
                    ch["end"] = ch["start"] + 60

        cfg, err = _bunny_config_or_error()
        if err is not None:
            return err
        try:
            bunny_api.set_bunny_chapters(cfg, guid, cleaned)
        except bunny_api.BunnyAPIError as exc:
            log.error("[bunny:chapters] save_failed", extra={"guid": guid, "err": str(exc)})
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({"ok": True, "chapters": cleaned})


class ThumbnailView(APIView):
    """
    ``POST /videos/<guid>/thumbnail`` — replace the video's poster.

    Body is multipart form-data with a single ``thumbnail`` file (JPG, PNG,
    or WebP, ≤ 5 MB). The file is forwarded to Bunny as the raw body of
    ``POST /library/{libraryId}/videos/{videoId}/thumbnail``. After Bunny
    accepts, we refresh our cached ``BunnyVideo.thumbnail_url`` from the
    re-fetched video metadata so the Studio UI shows the new image without
    a hard refresh.
    """

    permission_classes = [IsAuthenticated, IsStaffUser]
    parser_classes = []  # let DRF auto-negotiate (multipart)

    ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
    MAX_BYTES = 5 * 1024 * 1024  # 5 MB

    def post(self, request, guid: str):
        row = get_object_or_404(BunnyVideo, guid=guid)
        upload = request.FILES.get("thumbnail")
        if upload is None:
            return Response(
                {"error": "Missing 'thumbnail' file in multipart body."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        content_type = (upload.content_type or "").lower()
        if content_type not in self.ALLOWED_MIME:
            return Response(
                {"error": f"Unsupported image type ({content_type or 'unknown'}). Use JPG, PNG, or WebP."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if upload.size and upload.size > self.MAX_BYTES:
            return Response(
                {"error": f"Image too large ({upload.size} bytes). Limit is {self.MAX_BYTES} bytes."},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        cfg, err = _bunny_config_or_error()
        if err is not None:
            return err

        try:
            image_bytes = upload.read()
            bunny_api.set_bunny_thumbnail(cfg, guid, image_bytes, content_type)
        except bunny_api.BunnyAPIError as exc:
            log.error(
                "[bunny:thumbnail] set_thumbnail_failed",
                extra={"guid": guid, "err": str(exc)},
            )
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        # Re-fetch so we pick up Bunny's new thumbnail filename (if any) and
        # the cdn_hostname-derived URL. Cheap — one Bunny GET. Cached on row.
        try:
            meta = bunny_api.get_bunny_video(cfg, guid)
            if meta:
                thumb = bunny_api.bunny_thumbnail_url(
                    cfg.cdn_hostname, guid, meta.get("thumbnailFileName")
                )
                if thumb:
                    row.thumbnail_url = thumb
                    row.save(update_fields=["thumbnail_url", "updated_at"])
        except bunny_api.BunnyAPIError:
            # Bunny rejected the meta refresh — the thumbnail upload itself
            # succeeded, so report success. UI re-polls on its next tick.
            pass

        return Response({"ok": True, "thumbnail_url": row.thumbnail_url or None})


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
