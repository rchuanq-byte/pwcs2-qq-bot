# Campus and Singapore split exits

Production uses the official `yyy1mu/ustc-iwan` **v26.11.1** Linux x86_64 musl
binary. Release archive SHA-256:
`774d9d7140d3d7d608338fca4041350cba40ce96c7e5697281e1516c91c09bfc`.
Extracted binary SHA-256:
`649656d3cdbc808f74580f13dc725e96d3804a33257aadb817490316086594ff`.

The new rootless `qqbot-campus-proxy.service` reads the existing protected
`/home/admin/.config/iwan/servers.json`, binds the physical eth0 source IPv4,
and listens only on the server's Docker bridge `172.18.0.1:18081`.
It needs no TUN, routing changes, network-administration capability or Windows
helper. `/etc/qqbot-campus-proxy.env` contains only nonsecret connection options.
The running executable is pinned through `IWAN_CLIENT` in that environment file
to `/usr/local/lib/qqbot-iwan/v26.11.1/iwan-client-oidc`. The wrapper's older
default and `/usr/local/bin/iwan-client-oidc` alias are retained for rollback;
use the explicit current executable path for future manual operations.

The existing authenticated, Docker-internal Mihomo proxy has two explicit rules:

- `api.llm.ustc.edu.cn` → `USTC-IWAN` → private campus SOCKS proxy.
- `api.steampowered.com` → `STEAM-SG` → Singapore-only provider fallback.
- Other targets are rejected; this is not a public or whole-host proxy.

Only AI reads `USTC_LLM_PROXY_URL`. Steam keeps its existing
`STEAM_PRESENCE_PROXY_URL`. Never print either URL, subscription nodes, `.env`
or the campus credentials. Do not configure global HTTP(S) proxies.

The obsolete `qqbot-iwan.service`, its TUN watchdog timer/launcher and `iwan0`
are retired only after campus and Steam probes succeed. Do not start the old
TUN installer or watchdog on this production configuration.

Checks:

```bash
systemctl is-active qqbot-campus-proxy.service
./deploy/check-iwan.sh
./deploy/check-ai.sh
./deploy/check-steam-proxy.sh
docker compose exec -T bot python -m pwcs2_bot.healthcheck
```

If the saved campus login needs renewal, run
`/usr/local/lib/qqbot-iwan/v26.11.1/iwan-client-oidc --fetch` as
`admin` in an interactive SSH terminal, then restart only
`qqbot-campus-proxy.service`. `deploy/configure_ustc_iwan.sh` also detects the
SOCKS service and never reinstalls TUN during reconfiguration. OIDC callbacks
and the saved encrypted credentials must not be pasted into group chat or Git.

The original TUN binary, services and private network configuration are retained
in the root-only `/var/backups/qqbot-network-20261025-v26.9.1/` rollback archive.
Rollback must restore only the network files and AI proxy configuration; never
reset the QQ profile, bot database, TeamSpeak data, model or Windows recording.

## 2026-11-04 recovery

The old process kept running while its inner TCP connects timed out in
`SynSent`. After a controlled reconnect, v26.9.1 received an ACK without a
tunnel IP and exited with `server returned invalid tunnel IPv4 address` on
both the education and telecom lines. The verified stable v26.11.1 release
completed the missing-IP legacy handshake fallback using the SAME saved
credentials; its isolated loopback proxy reached the campus API over verified
TLS (HTTP 401 without an API key). No SSO renewal was needed.

Only the campus client pin was changed. The previous environment file is in
the private `/var/backups/qqbot-campus-recovery-20261104-114847/` directory.
The QQ and Singapore-router containers, physical route/DNS settings, bot model,
API key, group scopes and campus credential file were left untouched.

Before and after a single AstrBot-only restart, `deploy/check-ai.sh` passed
both the configured `deepseek-flash` text request and native image-color
recognition. The restart cleared in-memory pending AI turns without resetting
conversation/business databases. Post-restart readiness verified QQ online,
41 persona modules plus 9 capability modules, zero hook errors, and unchanged
private business config (including the small group's native-mention-only gate).
NapCat and the Singapore router retained their original container identities
and start times. No real group test messages were sent by this recovery.
