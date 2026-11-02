# Security policy

Aura runs with live credentials: a Telegram bot token, a Gemini API key, and
optionally You.com and Pollinations keys. It also stores conversation history
and long-term memory for real users. Please treat security reports accordingly.

## Supported versions

Only the current `main` branch is supported. Fixes are not backported to older
commits; self-hosted deployments should track `main`.

## Reporting a vulnerability

**Do not open a public issue.** Use GitHub's private reporting:

1. Go to the repository's **Security** tab.
2. Choose **Report a vulnerability** (private vulnerability reporting).
3. Describe the issue, the affected file or feature, and a reproduction if you
   have one.

If private reporting is unavailable, open a public issue that only asks the
maintainer for a private channel — without technical details.

You can expect an initial reply within about a week. This is a volunteer-run
project, so please allow reasonable time before disclosing publicly.

## What is in scope

- Credential leakage through logs, error messages, prompts, or repository files.
- Prompt-injection bypasses: web pages, documents, images, or group messages that
  make Aura call tools, change identity, or disclose secrets.
- Authorization problems: reading another user's memory, history or reminders;
  group allowlist bypasses; reminder actions performed for the wrong chat.
- Path traversal, SSRF, or unbounded resource use in the file, image, web or
  weather tools.
- Denial of service that a single Telegram user can trigger (quota exhaustion,
  memory growth, unbounded queues).

## What is out of scope

- Issues that require the attacker to already hold your `.env`, database, or
  server access.
- Model mistakes with no security impact (a wrong fact, a bad joke, rude tone).
- Rate limits or outages of third-party providers.
- Telegram account security and Gemini/You.com account security.

## If a secret was exposed

Rotate first, then investigate:

1. Revoke and reissue the token/key at the provider (Telegram/BotFather, Google AI
   Studio, You.com, Pollinations).
2. Replace it in `.env` and restart the service.
3. If it was committed, assume it is public: rotate immediately and remove it from
   history. Deleting the file in a later commit is not enough.
4. Check logs for unexpected usage before the rotation.

## Hardening checklist for operators

- Run the bot as an unprivileged user, with `.env` readable only by that user.
- Keep `bot.db` and logs outside the repository and out of backups you do not
  control.
- Enable only the optional features you need; every disabled feature is a smaller
  attack surface and a smaller bill.
- Keep dependencies current; Dependabot proposes updates weekly.
- Restrict who can add the bot to groups, and review the group allowlist.
