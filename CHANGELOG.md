# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`.
- Hosted CI (`.github/workflows/ci.yml`): installs `requirements.txt`, compiles source and
  tests, runs the full `pytest` suite on every push and pull request.
- Issue templates (bug report, feature request) and a pull-request template.
- `pytest` pinned in `requirements.txt`, previously undeclared despite `tests/` depending on it.

## [3.0.0] — 2026-08-05

### Changed

- Migrated production from the Cloudflare Worker to a FastAPI service on FORGE (loopback
  `:8465`, public boundary `live-chat.echo-op.com`), preserving the original 37-route
  compatibility contract while moving state to PostgreSQL.
- Imported all eleven rescued D1 application exports as nullable `legacy_*_text_v1` tables
  (ten empty, one recovered `widgets` row) without data loss, revoking runtime access to the
  legacy tables and creating a labeled structural parent for the untenanted recovered row.
- Made the runtime independently attestable: canonicalized Git-normalized legacy source
  identity, converged on the canonical attested release root, and preflighted the bind-mounted
  runtime executable.
- Wired `npm` verification to the Python runtime test suite.

## [1.0.0] — 2026-03-25 to 2026-03-27

### Added

- Initial release: Echo Live Chat, an Intercom/Drift-alternative embeddable support-chat
  widget with AI chat, running as a Cloudflare Worker.
- Fixed a fail-open authentication defect and added Bearer token support.
- Added a global error handler.
