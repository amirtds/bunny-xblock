"""
Helpers for talking to the Bunny.net Stream API.

Direct Python port of ``cubite/app/lib/bunny.ts`` — kept line-for-line where
possible so anyone who has worked on the upstream Cubite integration recognises
the shapes. Notable differences:

- Single-tenant. The Cubite version is per-site; here a singleton
  :class:`BunnyConfiguration` holds the one Bunny library credentials per
  Open edX instance.
- Uses ``requests`` (sync) instead of ``fetch`` (async). API call sites are
  rare enough that a thread pool isn't worth the complexity.
- Status-code-to-HTTP translation happens in ``views.py`` / ``webhooks.py``
  rather than here — keeps this module Django-independent enough to unit-test.

API docs: https://docs.bunny.net/reference/api-overview
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import requests

from .models import BunnyConfiguration

# --- Constants ----------------------------------------------------------------

BUNNY_BASE = "https://video.bunnycdn.com"
BUNNY_EMBED_BASE = "https://iframe.mediadelivery.net/embed"

# Default embed-URL TTL. Long enough that a cached LMS page keeps playing for a
# normal session but short enough that a leaked URL has a narrow window. 6h
# matches Bunny's "Media Cage" recommended range.
EMBED_TTL_SECONDS = 6 * 60 * 60

# TUS upload signature TTL — 1 hour is plenty for any single-file upload and
# the signature is per-video, so concurrent uploads each get their own.
TUS_TTL_SECONDS = 60 * 60

# Internal status strings. Match Cubite's BunnyVideoStatus union.
STATUS_PENDING = "pending"
STATUS_UPLOADED = "uploaded"
STATUS_ENCODING = "encoding"
STATUS_READY = "ready"
STATUS_FAILED = "failed"


# --- Errors -------------------------------------------------------------------


class BunnyNotConfiguredError(Exception):
    """Raised when the BunnyConfiguration singleton is missing or incomplete."""

    def __init__(self) -> None:
        super().__init__("Bunny Stream has not been configured for this Open edX instance.")


class BunnyKeyUndecryptableError(Exception):
    """
    Raised when the encrypted API key fails to decrypt — typically a
    rotated ``SECRET_KEY`` or a corrupted ciphertext blob. Distinct from
    :class:`BunnyNotConfiguredError` so the UI can show "re-enter your key"
    instead of the misleading "not configured".
    """

    def __init__(self) -> None:
        super().__init__("Stored Bunny API key could not be decrypted. Re-enter it in Django admin.")


class BunnyAPIError(Exception):
    """Generic upstream Bunny error. Holds the HTTP status for the view layer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# --- Config -------------------------------------------------------------------


@dataclass(frozen=True)
class BunnyConfig:
    """Plaintext-decrypted Bunny credentials. Never persisted."""

    library_id: str
    api_key: str
    cdn_hostname: Optional[str]
    security_key: Optional[str]


def load_config() -> BunnyConfig:
    """
    Read the BunnyConfiguration singleton and return a decrypted config.

    Raises :class:`BunnyNotConfiguredError` when the row is missing or
    library_id/api_key aren't set. Raises :class:`BunnyKeyUndecryptableError`
    when the API key ciphertext can't be decrypted (most likely cause is a
    rotated Django ``SECRET_KEY``).
    """
    cfg_row = BunnyConfiguration.load()
    if not cfg_row.library_id or not cfg_row.has_api_key:
        raise BunnyNotConfiguredError()

    api_key = cfg_row.get_api_key()
    if not api_key:
        raise BunnyKeyUndecryptableError()

    # Security key is optional. If decryption fails, treat as absent and fall
    # back to unsigned URLs — refusing to play would be worse UX than degrading
    # to "works only if token auth is off in Bunny's dashboard".
    security_key = cfg_row.get_security_key() or None

    return BunnyConfig(
        library_id=cfg_row.library_id,
        api_key=api_key,
        cdn_hostname=cfg_row.cdn_hostname or None,
        security_key=security_key,
    )


# --- HTTP helpers -------------------------------------------------------------


def _request(cfg: BunnyConfig, method: str, path: str, *, json_body=None) -> requests.Response:
    headers = {"AccessKey": cfg.api_key, "Accept": "application/json"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    return requests.request(
        method,
        f"{BUNNY_BASE}{path}",
        headers=headers,
        json=json_body,
        timeout=15,
    )


def create_bunny_video(cfg: BunnyConfig, title: str) -> str:
    """Create a video record on Bunny and return its guid."""
    res = _request(cfg, "POST", f"/library/{cfg.library_id}/videos", json_body={"title": title})
    if not res.ok:
        raise BunnyAPIError(res.status_code, f"Bunny createVideo failed ({res.status_code}): {res.text[:200]}")
    data = res.json()
    guid = data.get("guid")
    if not guid:
        raise BunnyAPIError(502, "Bunny createVideo returned no guid")
    return guid


def get_bunny_video(cfg: BunnyConfig, guid: str) -> Optional[dict]:
    """Fetch video metadata. Returns None on 404 (Bunny doesn't know it)."""
    res = _request(cfg, "GET", f"/library/{cfg.library_id}/videos/{guid}")
    if res.status_code == 404:
        return None
    if not res.ok:
        raise BunnyAPIError(res.status_code, f"Bunny getVideo failed ({res.status_code}): {res.text[:200]}")
    return res.json()


def update_bunny_video(cfg: BunnyConfig, guid: str, *, title: Optional[str] = None) -> None:
    payload: dict = {}
    if title is not None:
        payload["title"] = title
    if not payload:
        return
    res = _request(cfg, "POST", f"/library/{cfg.library_id}/videos/{guid}", json_body=payload)
    if not res.ok:
        raise BunnyAPIError(res.status_code, f"Bunny updateVideo failed ({res.status_code}): {res.text[:200]}")


def delete_bunny_video(cfg: BunnyConfig, guid: str) -> None:
    res = _request(cfg, "DELETE", f"/library/{cfg.library_id}/videos/{guid}")
    if not res.ok and res.status_code != 404:
        raise BunnyAPIError(res.status_code, f"Bunny deleteVideo failed ({res.status_code}): {res.text[:200]}")


# --- Status mapping -----------------------------------------------------------


def map_bunny_status(code: Optional[int]) -> str:
    """
    Bunny video status code → our internal status string.

    0 Created, 1 Uploaded, 2 Processing, 3 Transcoding, 4 Finished,
    5 Error, 6 UploadFailed, 7 JitSegmenting, 8 JitPlaylistsCreated.
    """
    if code == 0:
        return STATUS_PENDING
    if code == 1:
        return STATUS_UPLOADED
    if code in (2, 3, 7):
        return STATUS_ENCODING
    if code in (4, 8):
        return STATUS_READY
    if code in (5, 6):
        return STATUS_FAILED
    return STATUS_PENDING


# --- Thumbnail URL helper -----------------------------------------------------


def bunny_thumbnail_url(cdn_hostname: Optional[str], guid: str, file_name: Optional[str] = None) -> Optional[str]:
    """
    Build the CDN thumbnail URL for a video. Returns None if no CDN hostname
    is configured.
    """
    if not cdn_hostname:
        return None
    host = cdn_hostname.replace("https://", "").replace("http://", "").rstrip("/")
    name = file_name or "thumbnail.jpg"
    return f"https://{host}/{guid}/{name}"


# --- Signing ------------------------------------------------------------------


def sign_tus_upload(cfg: BunnyConfig, video_guid: str, ttl_seconds: int = TUS_TTL_SECONDS) -> dict:
    """
    Generate a TUS upload signature for a single video.

    Bunny's TUS endpoint authenticates each upload with
        AuthorizationSignature = sha256(libraryId + apiKey + expires + videoId)

    The unseparated concatenation is dictated by Bunny's documented scheme —
    do not add separators or change field order, or uploads will 401.
    """
    expires = int(time.time()) + ttl_seconds
    raw = f"{cfg.library_id}{cfg.api_key}{expires}{video_guid}".encode("utf-8")
    signature = hashlib.sha256(raw).hexdigest()
    return {"library_id": cfg.library_id, "expires": expires, "signature": signature}


def sign_embed_url(
    library_id: str,
    guid: str,
    security_key: str,
    ttl_seconds: int = EMBED_TTL_SECONDS,
    extra_query: Optional[dict] = None,
) -> str:
    """
    Sign a Bunny Stream iframe embed URL.

    Bunny's documented Stream token format:
        token = sha256(securityKey + videoId + expirationUnix)  (hex)
    """
    expires = int(time.time()) + ttl_seconds
    raw = f"{security_key}{guid}{expires}".encode("utf-8")
    token = hashlib.sha256(raw).hexdigest()
    params = {"token": token, "expires": str(expires)}
    if extra_query:
        params.update({k: str(v) for k, v in extra_query.items()})
    return f"{BUNNY_EMBED_BASE}/{library_id}/{guid}?{urllib.parse.urlencode(params)}"


def unsigned_embed_url(library_id: str, guid: str, extra_query: Optional[dict] = None) -> str:
    """
    Construct an unsigned Bunny embed URL. Works only if the library has token
    authentication disabled in Bunny's dashboard.
    """
    base = f"{BUNNY_EMBED_BASE}/{library_id}/{guid}"
    if not extra_query:
        return base
    qs = urllib.parse.urlencode({k: str(v) for k, v in extra_query.items()})
    return f"{base}?{qs}" if qs else base


def get_embed_url_for_video(guid: str, extra_query: Optional[dict] = None) -> Optional[str]:
    """
    Server-side: pick the right URL flavour for a given video.

    Looks up the BunnyVideo row, loads the configured security key if any, and
    returns either a signed or unsigned embed URL. Returns ``None`` when the
    BunnyVideo row doesn't exist — callers render an empty state.

    Errors during config load (BunnyNotConfiguredError, BunnyKeyUndecryptableError)
    propagate to the caller, which has the context to decide whether to 503/500
    or fall back silently. For renderer code paths we recommend a try/except
    that downgrades to the unsigned URL when the security key is absent.
    """
    from .models import BunnyVideo  # local import to avoid app-loading order issues

    try:
        row = BunnyVideo.objects.get(guid=guid)
    except BunnyVideo.DoesNotExist:
        return None

    try:
        cfg = load_config()
    except (BunnyNotConfiguredError, BunnyKeyUndecryptableError):
        # No usable config — fall back to unsigned. Will only play if Bunny
        # token auth is disabled, but that's better than blank space.
        return unsigned_embed_url(row.library_id, guid, extra_query)

    if cfg.security_key:
        return sign_embed_url(row.library_id, guid, cfg.security_key, EMBED_TTL_SECONDS, extra_query)
    return unsigned_embed_url(row.library_id, guid, extra_query)
