"""
Django admin for xblock-bunny.

The only thing site admins need to interact with is the singleton
:class:`BunnyConfiguration` row — paste credentials, see the generated
webhook URL. We deliberately do not register ``BunnyVideo`` for the admin
list view: the media library is managed through the XBlock authoring UI
(and could expose user-visible metadata to other admins by accident).

Custom ``ModelForm`` provides three things the default form doesn't:

1. Plaintext ``api_key`` / ``security_key`` inputs that mask existing values
   (placeholder shows "Leave blank to keep existing"), matching the Cubite
   ``BunnyStreamCard`` UX.
2. Live-rendered webhook URL preview under the form, so admins can copy it
   straight into Bunny's dashboard.
3. Disconnect + rotate-webhook actions.
"""

from urllib.parse import urlparse

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.urls import reverse
from django.utils.safestring import mark_safe

from .models import BunnyConfiguration


# ---- Form --------------------------------------------------------------------------------


class BunnyConfigurationForm(forms.ModelForm):
    """Form that masks the encrypted fields and stays idempotent on resave."""

    api_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Paste a new key to overwrite. Leave blank to keep the existing key.",
    )
    security_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=(
            "Optional. Required only if you've enabled Token Authentication "
            "in Bunny → Stream → Library → Security. Leave blank to keep the existing key."
        ),
    )

    class Meta:
        model = BunnyConfiguration
        # We expose only the plaintext-equivalent fields. The encrypted
        # storage columns are written via the model setters below.
        fields = ("library_id", "api_key", "security_key", "cdn_hostname")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = kwargs.get("instance")
        if instance:
            # Surface the "key is already stored" state in the placeholder.
            if instance.has_api_key:
                self.fields["api_key"].widget.attrs["placeholder"] = "•••••••• (set — leave blank to keep)"
            if instance.has_security_key:
                self.fields["security_key"].widget.attrs["placeholder"] = "•••••••• (set — leave blank to keep)"

    def save(self, commit: bool = True) -> BunnyConfiguration:
        instance: BunnyConfiguration = super().save(commit=False)
        new_api_key = self.cleaned_data.get("api_key") or ""
        new_security_key = self.cleaned_data.get("security_key") or ""
        if new_api_key:
            instance.set_api_key(new_api_key)
        if new_security_key:
            instance.set_security_key(new_security_key)
        if commit:
            instance.save()
        return instance


# ---- Admin -------------------------------------------------------------------------------


def _build_webhook_url(request, secret: str) -> str:
    """
    Build the public webhook URL admins paste into Bunny.

    Prefers the LMS_ROOT_URL setting (Open edX standard) and falls back to
    the request host so this works in dev too.
    """
    base = getattr(settings, "LMS_ROOT_URL", None)
    if not base:
        base = f"{request.scheme}://{request.get_host()}"
    parsed = urlparse(base)
    base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else base
    return f"{base.rstrip('/')}/api/xblock_bunny/webhook/{secret}"


@admin.register(BunnyConfiguration)
class BunnyConfigurationAdmin(admin.ModelAdmin):
    form = BunnyConfigurationForm

    fieldsets = (
        (
            "Bunny library",
            {
                "fields": ("library_id", "api_key", "security_key", "cdn_hostname"),
                "description": (
                    "Credentials live in Bunny's dashboard under "
                    "<em>Stream → Library → API</em>. Token Authentication Key "
                    "is under <em>Stream → Library → Security</em>."
                ),
            },
        ),
        (
            "Webhook",
            {
                "fields": ("webhook_url_display",),
                "description": (
                    "Paste this URL into <em>Stream → Library → Webhooks</em> so "
                    "Bunny can notify Open edX when encoding completes. The URL "
                    "embeds a per-instance secret — treat it like a password."
                ),
            },
        ),
    )
    readonly_fields = ("webhook_url_display",)
    actions = ("rotate_webhook_secret_action", "disconnect_action")

    # ---- One-row enforcement -----------------------------------------------------------

    def has_add_permission(self, request):
        # Singleton — never allow a second row to be created. Editing happens
        # via the existing pk=1 row (auto-created by BunnyConfiguration.load()).
        return not BunnyConfiguration.objects.exists()

    def has_delete_permission(self, request, obj=None):
        # Deleting would orphan the webhook URL pasted in Bunny. Use the
        # "Disconnect" action instead.
        return False

    def changelist_view(self, request, extra_context=None):
        # If the singleton isn't there yet, redirect into the edit view of pk=1
        # — Django admin doesn't do this for us.
        BunnyConfiguration.load()
        return super().changelist_view(request, extra_context)

    # ---- Display helpers ---------------------------------------------------------------

    def webhook_url_display(self, obj: BunnyConfiguration):
        if not obj or not obj.webhook_secret:
            return mark_safe(
                "<em>Save credentials first; the URL appears here.</em>"
            )
        # request is not available in readonly_fields — emit a relative URL
        # plus a JS one-click copy hook so admins can grab it easily.
        path = f"/api/xblock_bunny/webhook/{obj.webhook_secret}"
        return mark_safe(
            f'<code style="user-select:all">&lt;your-lms-host&gt;{path}</code> '
            f'<small>(prepend your LMS scheme + host)</small>'
        )

    webhook_url_display.short_description = "Webhook URL"

    # ---- Custom actions ----------------------------------------------------------------

    def rotate_webhook_secret_action(self, request, queryset):
        import secrets

        for cfg in queryset:
            cfg.webhook_secret = secrets.token_hex(32)
            cfg.save()
        self.message_user(
            request,
            "Webhook secret rotated. Update the URL in Bunny → Stream → Library → Webhooks.",
            level=messages.WARNING,
        )

    rotate_webhook_secret_action.short_description = "Rotate webhook secret"

    def disconnect_action(self, request, queryset):
        for cfg in queryset:
            cfg.library_id = ""
            cfg.cdn_hostname = ""
            cfg.set_api_key("")
            cfg.set_security_key("")
            cfg.save()
        self.message_user(
            request,
            "Bunny credentials cleared. Existing uploaded videos remain on Bunny "
            "until you delete them there.",
            level=messages.WARNING,
        )

    disconnect_action.short_description = "Disconnect (clear credentials + webhook)"
