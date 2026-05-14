# Changelog

## 0.1.0 — unreleased

Initial release.

- `BunnyVideoXBlock` with inline `author_view`: drag-drop / click-to-upload, TUS progress, encoding-status polling, in-place signed-iframe player.
- `student_view` with click-to-load poster and Token-Authenticated signed iframe.
- Singleton `BunnyConfiguration` model managed via Django admin (API key + security key encrypted at rest using Fernet derived from `SECRET_KEY`).
- REST endpoints under `/api/xblock_bunny/` for upload-token, finalize, video CRUD, embed-url, and Bunny webhook receiver.
- Auto-wires via `lms.djangoapp` / `cms.djangoapp` entry points — no `edx-platform` patches required.
- Optional `CSP_FRAME_SRC` extension for sites with strict Content Security Policy.
