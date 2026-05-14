"""
Database models for xblock-bunny.

Two tables, both lightweight:

- :class:`BunnyConfiguration` — singleton row holding the Bunny library
  credentials. Managed via Django admin. API key and security key are
  stored as Fernet ciphertext (see ``crypto.py``).

- :class:`BunnyVideo` — one row per video uploaded through the XBlock.
  Mirrors Cubite's ``BunnyVideo`` Prisma model so the schema is familiar
  to anyone who's worked on the upstream Cubite integration.
"""

import re
import secrets

from django.core.exceptions import ValidationError
from django.db import models

from . import crypto

# --- Validation regexes (match the Cubite app/api/site-builder/update-bunny-stream route) -----

LIBRARY_ID_RE = re.compile(r"^\d+$")
CDN_HOSTNAME_RE = re.compile(r"^vz-[a-z0-9-]+\.b-cdn\.net$", re.IGNORECASE)


def validate_library_id(value: str) -> None:
    if value and not LIBRARY_ID_RE.match(value):
        raise ValidationError("Library ID must be numeric.")


def validate_cdn_hostname(value: str) -> None:
    if value and not CDN_HOSTNAME_RE.match(value):
        raise ValidationError(
            "CDN Hostname must look like vz-xxxxxxxx-xxx.b-cdn.net."
        )


# --- Status enum (matches app/lib/bunny.ts BunnyVideoStatus) ---------------------------------

STATUS_CHOICES = [
    ("pending", "Pending"),
    ("uploaded", "Uploaded"),
    ("encoding", "Encoding"),
    ("ready", "Ready"),
    ("failed", "Failed"),
]

TERMINAL_STATUSES = frozenset({"ready", "failed"})


# --- Models -----------------------------------------------------------------------------------


class BunnyConfiguration(models.Model):
    """
    Singleton: one Bunny library per Open edX instance.

    The singleton invariant is enforced in :meth:`save` (we always coerce
    ``pk=1``). Django admin then naturally shows a single editable row.
    """

    library_id = models.CharField(
        max_length=64,
        blank=True,
        validators=[validate_library_id],
        help_text="Bunny.net Stream library ID (numeric).",
    )
    api_key_ciphertext = models.TextField(
        blank=True,
        help_text="Fernet-encrypted Bunny API key. Never returned to clients.",
    )
    security_key_ciphertext = models.TextField(
        blank=True,
        help_text=(
            "Fernet-encrypted Bunny Token Authentication key (Stream → Library "
            "→ Security). Optional; without it embeds use unsigned URLs."
        ),
    )
    cdn_hostname = models.CharField(
        max_length=255,
        blank=True,
        validators=[validate_cdn_hostname],
        help_text="Pull-zone hostname Bunny assigned to this library.",
    )
    webhook_secret = models.CharField(
        max_length=64,
        blank=True,
        unique=True,
        null=True,
        help_text=(
            "Random token embedded in the public webhook URL. Minted on first "
            "credential save; rotated when the library ID changes; cleared on "
            "disconnect."
        ),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Bunny Stream configuration"
        verbose_name_plural = "Bunny Stream configuration"

    # ---- Singleton machinery -----------------------------------------------------------

    def save(self, *args, **kwargs):
        # Coerce the singleton: there is always exactly one row, with pk=1.
        self.pk = 1
        # Rotate the webhook secret when the library changes — old URLs were
        # advertised against a different library and shouldn't keep working.
        if self.pk:
            try:
                prior = BunnyConfiguration.objects.get(pk=1)
            except BunnyConfiguration.DoesNotExist:
                prior = None
            if prior and prior.library_id and prior.library_id != self.library_id:
                self.webhook_secret = secrets.token_hex(32)
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "BunnyConfiguration":
        """Return the singleton instance, creating it on demand."""
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj

    # ---- Credential helpers ------------------------------------------------------------

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key_ciphertext)

    @property
    def has_security_key(self) -> bool:
        return bool(self.security_key_ciphertext)

    def set_api_key(self, plaintext: str) -> None:
        """Encrypt and store. Empty string clears."""
        if plaintext:
            self.api_key_ciphertext = crypto.encrypt(plaintext)
            if not self.webhook_secret:
                self.webhook_secret = secrets.token_hex(32)
        else:
            self.api_key_ciphertext = ""
            self.webhook_secret = None

    def set_security_key(self, plaintext: str) -> None:
        if plaintext:
            self.security_key_ciphertext = crypto.encrypt(plaintext)
        else:
            self.security_key_ciphertext = ""

    def get_api_key(self) -> str:
        """Decrypt and return the API key, or empty string on failure."""
        return crypto.decrypt(self.api_key_ciphertext) if self.api_key_ciphertext else ""

    def get_security_key(self) -> str:
        return crypto.decrypt(self.security_key_ciphertext) if self.security_key_ciphertext else ""


class BunnyVideo(models.Model):
    """
    A video Cubite knows about in Bunny. Each row corresponds 1:1 to a
    Bunny video resource (``guid`` is Bunny's identifier).
    """

    guid = models.CharField(primary_key=True, max_length=64)
    library_id = models.CharField(max_length=64)
    title = models.CharField(max_length=250, blank=True)
    duration_sec = models.IntegerField(null=True, blank=True)
    thumbnail_url = models.TextField(blank=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default="pending"
    )
    # Soft FK so deleting the User doesn't cascade-delete the media row.
    created_by_id = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["library_id"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin convenience
        return f"{self.title or '(untitled)'} [{self.guid}]"

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES
