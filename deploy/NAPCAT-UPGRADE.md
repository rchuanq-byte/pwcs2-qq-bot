# Controlled QQ login-end upgrade (2026-10-21)

This is a pinned compatibility trial, not a claim that Tencent session expiry is
fixed. Do not change the QQ account, reset its device identity, enable recording,
or start a second client with the same profile during this procedure.

## Candidate and provenance

- NapCat Shell **4.18.38**, official `NapNeko/NapCatQQ` release archive.
- Upstream archive SHA256:
  `735f2e633247df3ffd6e8ee8b84d7039c315b2655f5807e5f0029f93d2c30016`.
- QQ **3.2.32-52194**, amd64, downloaded from the Tencent HTTPS link in the
  upstream 4.18.30 release notes. Downloaded package SHA256:
  `d085dd89397225061eb9f194308f688129818ed445777e97a4a0a16e13d7b0e8`.
  This latter digest pins our downloaded artifact; it is not an assertion that
  Tencent published an independent checksum.
- Missing desktop-integration dependency: Ubuntu Jammy `xdg-utils`
  **1.1.3-4.1ubuntu3~22.04.1**, from `archive.ubuntu.com`, SHA256
  `ee4139344b50386d95d925b389713096538fe90ff33637ef6631c4566c4d0985`.
- Base: the existing production image, retained as
  `pwcs2-napcat:rollback-20261021` (image ID
  `sha256:b4982ae0d8abb8bb1607a6187b674b8f582b3e265d16ebd0cba08ff975f27722`).

## Observe-only policy

`napcat_observe_only.py` verifies the exact upstream archive and bundle before
applying a small version-specific patch. Changed upstream inputs abort instead
of silently weakening the policy. It disables automatic login-recovery Worker
restarts and the Master's automatic child-exit relaunch. Manual operator
recovery remains available. Do not use an unpatched newer archive in this image.

The existing Docker `unless-stopped` policy is unchanged: it still covers host
boot and a failure of the entire container process. This patch prevents the
new automatic **session-offline / QQ Worker** restart paths; it does not turn
off the existing container startup policy or ordinary TCP reconnection.

The existing host runtime guard remains **observe_only**. Its bounded history
records state transitions and an hourly diagnostic heartbeat, never messages,
login credentials or account identifiers. No new scheduler or local Windows
process is needed. Native core dumps are disabled to protect disk capacity.

## Deployment boundaries

1. Verify the account is offline and record current image IDs/start times.
2. Retain the old image. Stop only NapCat for a consistent profile backup.
3. Archive `data/napcat/{qq,config,plugins}`, `.env` and Compose files outside
   the repository in a mode-0700 directory; the archive must be mode 0600.
   Validate the archive before replacing the client.
4. Prepare the observe-only archive using the pinned preparer and run its tests.
5. Build `Dockerfile.napcat-observe-only` in a **minimal isolated context** with
   the prepared archive, `linuxqq.deb`, `xdg-utils.deb`, `napcat-load.js` and
   `napcat-observe-entrypoint.sh`. Never send the QQ profile, `.env` or business
   database into a Docker build context. Use `--pull=false --network=none`.
6. Inspect the image offline with a Python/shell entrypoint, without account
   mounts. Verify versions and patch markers before starting QQ.
7. Set only the server's `NAPCAT_IMAGE` to the verified immutable image ID or a
   pinned local release tag. Recreate **only** NapCat with `--no-deps`.
8. Confirm WebUI, expected QQ account and actual online state. A listening port
   or `isLogin=true` alone is not acceptance. Render any required QR locally;
   never print the URL, WebUI token, `.env` or raw chat logs.
9. Verify `python -m pwcs2_bot.healthcheck`, fresh transport health and unchanged
   bot/TS start times. A manual group command verifies message round-trip;
   short-term success does not establish long-term session stability.

## Rollback

Stop only the candidate NapCat. Point `NAPCAT_IMAGE` back to the retained image
and recreate only NapCat. If the old version cannot read the upgraded profile,
first move the candidate profile to a protected sibling directory and restore
**only the backed-up NapCat profile/config** with its original ownership.
Never overwrite the live bot database or all of `.env` merely to roll back QQ.
Keep the candidate data and backup until recovery is verified.
