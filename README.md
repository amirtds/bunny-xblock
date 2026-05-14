# xblock-bunny

Open edX XBlock that embeds [Bunny.net Stream](https://bunny.net/stream/) videos with **Token Authentication**, direct-to-Bunny TUS upload from Studio, and encoding-status webhooks.

> Status: 0.1.0 — early. Issues / PRs welcome at https://github.com/amirtds/bunny-xblock.

## Compatibility

| Component        | Supported versions                    | Notes                                                                                  |
| ---------------- | ------------------------------------- | -------------------------------------------------------------------------------------- |
| **Open edX**     | Sumac (`open-release/sumac.master`) and newer, including Teak | Earlier releases (Quince, Redwood, Palm) haven't been tested — likely works, no guarantee. |
| **Tutor**        | 19.x (Sumac) and 20.x (Teak)          | The `OPENEDX_EXTRA_PIP_REQUIREMENTS` install path is identical on both.                |
| **Python**       | 3.11 (matches Tutor Sumac / Teak)     | The package itself declares `python>=3.8` so it's also installable in older runtimes.  |
| **Django**       | 4.2 LTS                               | Same Django Open edX ships with.                                                       |
| **Bunny.net**    | Stream — any plan                     | DRM (MediaCage Premium) is *not* required. Token Authentication is in the free tier.  |

Authoring is browser-based: any browser that supports `fetch` + `Promise` + `URLSearchParams` (effectively Chrome / Firefox / Safari / Edge from the last 5 years). The LMS player uses a vanilla `<iframe>` so it inherits Bunny's playback compatibility — including iOS Safari, where the embedded player falls back to AirPlay-friendly HLS.

## Why

The default Open edX `VideoBlock` doesn't know how to play `iframe.mediadelivery.net` URLs and the dominant community alternative (`appsembler/xblock-video`) ships only Brightcove / HTML5 / Vimeo / Wistia / YouTube backends — no Bunny. This XBlock fills the gap: it lets course authors upload directly to a configured Bunny library from inside a Studio unit and renders Token-Authenticated signed iframes in the LMS so videos can't be hot-linked.

## Install

The XBlock self-wires via Open edX's Django plugin entry points — **no `edx-platform` edits**, no `INSTALLED_APPS` patching, no `lms.env.json` / `cms.env.json` changes.

### On Tutor (Sumac / Teak)

```bash
tutor config save --append OPENEDX_EXTRA_PIP_REQUIREMENTS=xblock-bunny
tutor local launch
```

That's it. Open the LMS as a global admin, paste credentials at:

```
https://<your-lms>/admin/xblock_bunny/bunnyconfiguration/
```

Then in Studio: course → Advanced Settings → "Advanced Module List" → add `bunny_video`. The "Bunny Video" component now appears in the unit "Add New Component" menu.

### Configuration fields

| Field | Where to find it in Bunny |
| --- | --- |
| **Library ID** | Stream → Library → API |
| **API Key** | Stream → Library → API |
| **Security Key** | Stream → Library → Security → "Token Authentication Key" (enable Token Authentication first) |
| **CDN Hostname** | Stream → Library → API → "Pull Zone Hostname" — looks like `vz-xxxxxxxx-xxx.b-cdn.net` |

After saving, the page displays a **Webhook URL**. Paste it into Bunny → Stream → Library → Webhooks so the XBlock receives encoding-status updates.

## Authoring (inline UX)

In a Studio unit, "Bunny Video" gives you a single drop zone:

- **Drag a file** or click "Choose video".
- Watch the **upload progress bar** (TUS direct to Bunny — bytes never touch your Open edX server).
- The block shows **"Processing on Bunny…"** while encoding.
- The block flips to the **signed-iframe player** when Bunny reports `ready`. The webhook drives this — no page reload required.

You can replace or delete the video from the same surface.

## Student playback

The LMS renders a click-to-load poster (using Bunny's auto thumbnail). On click, the iframe is mounted with a freshly signed URL:

```
https://iframe.mediadelivery.net/embed/<library>/<guid>?token=<sha256(securityKey+guid+expires)>&expires=<unix+6h>
```

Hot-linking the URL from another origin returns 401 (when Token Authentication is enabled on the Bunny library).

## Known limitations

- **Mobile apps**: the official Open edX iOS / Android apps don't render custom video XBlocks natively — they degrade to a webview. v2 candidate.
- **CSP**: if your Open edX instance enforces `Content-Security-Policy: frame-src`, the package's settings hook adds `iframe.mediadelivery.net` to `CSP_FRAME_SRC` when that setting exists. On stricter setups, add it manually.
- **Cubite-style analytics**: `courseware.video.played` events are hardcoded to `VideoBlock` and don't fire for this XBlock. v2 candidate.

## Architecture

- One Bunny library per Open edX instance — credentials live in a singleton `BunnyConfiguration` row managed via Django admin. API key and Security key are Fernet-encrypted (key derived from `SECRET_KEY` via HKDF).
- Upload uses TUS direct to `https://video.bunnycdn.com/tusupload`; the server only mints a per-upload signature (`sha256(libraryId + apiKey + expires + guid)`, 1 h TTL).
- Embed URLs are signed at render time per the Bunny scheme (`sha256(securityKey + guid + expires)`, 6 h TTL).
- Webhook auth is URL-token-only (Bunny Stream doesn't sign payloads), with library-mismatch and lifecycle-regression guards to mitigate URL leaks.
- Django app auto-discovered via `lms.djangoapp` / `cms.djangoapp` entry points (Open edX `edx_django_utils.plugins`).

## License

Apache-2.0. See `LICENSE`.

This XBlock is maintained by [Cubite](https://cubite.io) and works with any Bunny.net account — no Cubite tenancy required.
