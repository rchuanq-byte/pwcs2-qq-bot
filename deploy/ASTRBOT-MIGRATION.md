# AstrBot migration work log

Requested 2026-10-26: replace the nanobot gateway with AstrBot, preserve CS services and data,
and install the reviewed Dafeiyu group-conversation modules rather than only imitating their prompts.

Pinned inputs:

- AstrBot v4.29.3: `f61a7f42696da6df25dc7a249ec9b8d54ed9da18`.
- Official DockerHub linux/amd64 manifest: `sha256:d6f6508af78b9d0e427c0959a2fdd1016411720aad67cfcde8719870a89ee832`.
- Dafeiyu QQ plugins: `8009d1036e5a0a6fa2a2490429375ff4735c5b67`.
- Legacy application rollback: `pwcs2-qq-bot:quiet-20261026`, Git `45c2430`.

Constraints:

- Single production QQ event owner and a single set of business schedulers. Never run both
  gateways against the live group while testing. Stage with isolated SQLite copies and no QQ sends.
- Preserve Steam bindings, separate perfect/official stats, wallets, inventory, card locks,
  matches, highlight protocol, 23:30 schedules and the private campus/Singapore routing split.
- Preserve automatic-only quiet until 2026-10-27 09:16:32 +08:00, replay cutoff,
  closed predictions/content relay/removal and observe-only runtime guard.
- Do not restart or recreate NapCat, TS or network services for this migration. A QQ session
  invalidation is not proof of a gateway bug and AstrBot cannot promise to eliminate it.
- Bind the AstrBot dashboard to loopback only; authenticate it; do not mount the Docker socket.
  No payment, arbitrary command/file, browser or computer-control tools for the group AI.
- Keep voice input -> native QQ transcription -> text output; no TTS provider activation.

Implementation sequence:

1. Make the existing business application reusable without importing or starting nanobot.
   Keep a lazy legacy adapter for regression and rollback.
2. Build an AstrBot plugin owning command routing, API callbacks, lifecycle, identity-bound
   read-only tools, native media input and a common outbound policy.
3. Vendor reviewed Dafeiyu memory/persona/conversation modules with their original MIT license,
   pin metadata and document any compatibility/identity patches. Configure only the target group.
4. Run unit/regression, isolated framework/provider/SQLite/media tests, compare resources,
   make private backups and switch once. If a required acceptance fails, retain/restore the old
   gateway and report the actual blocker rather than calling a staged container a migration.

Acceptance achieved in the isolated real AstrBot 4.29.3 runtime:

- Existing help, binding lookup, real PerfectWorld data -> Chromium -> OneBot image.
- Group allowlist, replay cutoff, automatic-only quiet and explicit command replies.
- Real DeepSeek text and read-only statistics tool calls through the private campus proxy.
- Two opposite visual fixtures (BLUE/RED), actual provider-bound image parts, zero hook errors.
- Original Dafeiyu profile command. No sandbox messages went to real QQ.
- Unit regression covers 20 concurrent shared-context appends, quiet boundaries and callback cleanup.

Visual input is deliberately isolated: the configured vision model reads only the current images
without persona/history/tools, and the AstrBot group pipeline receives a bounded, untrusted
observation. The live model remains `deepseek-flash`; no new external provider is introduced.
History stores original user text and final answers, never image base64 or injected profiles;
automatic drafts enter shared history only after an actual successful send.

Deployment uses the default compose.override.yaml, so ordinary Git updates and `docker compose`
commands retain AstrBot. Dashboard: loopback-only port 6185, private generated password at
`data/astrbot/.dashboard-password` (use sudo locally; do not publish it).

QQ network connection (no process restart):

    sudo python3 deploy/configure_astrbot_connection.py enable --backup /var/backups/<snapshot>

If QQ is logged out this saves the reverse config and exits 4 (`PENDING_LOGIN`). Rerun after
QR login to apply a network-only hot reload. Do not call that pending state “QQ online”.

Rollback only the gateway, without restoring old SQLite snapshots over newer transactions:

    sudo python3 deploy/configure_astrbot_connection.py disable
    docker compose -f compose.yaml -f compose.override.yaml -f deploy/compose.nanobot-rollback.yaml up -d --no-deps --no-build bot

Status: code and isolated acceptance complete; production cutover/QQ-login acceptance recorded
in the final operational report. Old gateway image and private SQLite backups are retained.
