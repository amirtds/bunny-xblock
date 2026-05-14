"""Initial schema for xblock-bunny: BunnyConfiguration singleton + BunnyVideo."""

from django.db import migrations, models

import bunny_xblock.models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="BunnyConfiguration",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "library_id",
                    models.CharField(
                        blank=True,
                        help_text="Bunny.net Stream library ID (numeric).",
                        max_length=64,
                        validators=[bunny_xblock.models.validate_library_id],
                    ),
                ),
                (
                    "api_key_ciphertext",
                    models.TextField(
                        blank=True,
                        help_text="Fernet-encrypted Bunny API key. Never returned to clients.",
                    ),
                ),
                (
                    "security_key_ciphertext",
                    models.TextField(
                        blank=True,
                        help_text=(
                            "Fernet-encrypted Bunny Token Authentication key "
                            "(Stream → Library → Security). Optional; "
                            "without it embeds use unsigned URLs."
                        ),
                    ),
                ),
                (
                    "cdn_hostname",
                    models.CharField(
                        blank=True,
                        help_text="Pull-zone hostname Bunny assigned to this library.",
                        max_length=255,
                        validators=[bunny_xblock.models.validate_cdn_hostname],
                    ),
                ),
                (
                    "webhook_secret",
                    models.CharField(
                        blank=True,
                        help_text=(
                            "Random token embedded in the public webhook URL. "
                            "Minted on first credential save; rotated when the "
                            "library ID changes; cleared on disconnect."
                        ),
                        max_length=64,
                        null=True,
                        unique=True,
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Bunny Stream configuration",
                "verbose_name_plural": "Bunny Stream configuration",
            },
        ),
        migrations.CreateModel(
            name="BunnyVideo",
            fields=[
                (
                    "guid",
                    models.CharField(max_length=64, primary_key=True, serialize=False),
                ),
                ("library_id", models.CharField(max_length=64)),
                ("title", models.CharField(blank=True, max_length=250)),
                ("duration_sec", models.IntegerField(blank=True, null=True)),
                ("thumbnail_url", models.TextField(blank=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("uploaded", "Uploaded"),
                            ("encoding", "Encoding"),
                            ("ready", "Ready"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("created_by_id", models.IntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [
                    models.Index(fields=["status"], name="bunny_xblock_status_idx"),
                    models.Index(fields=["library_id"], name="bunny_xblock_library_idx"),
                ],
            },
        ),
    ]
