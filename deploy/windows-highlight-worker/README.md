# Windows CS2 Demo Highlight Worker

This worker turns leased `.dem` jobs into real videos with CS Demo Manager
3.20.x. It verifies the downloaded demo, runs `csdm analyze` with the job's
`context.source` (`perfectworld` or `valve`), writes a documented CS:DM JSON
video config, runs `csdm video --config-file`, and uploads the one new MP4.
Long analysis/render commands renew their server lease in the background.

The generated video is 1280x720 at 30 FPS, MP4 with libx264/AAC, game audio
enabled, player voices disabled, and all requested tick sequences concatenated.
Every sequence starts in the tracked SteamID's POV. Sequence duration is capped
at 60 seconds per job and FFmpeg uses a controlled 3 Mbps maximum rate. The
final file must fit the server's 24 MiB OneBot inline-video limit; a larger file
fails explicitly before upload.

## Prerequisites and verification

Install these yourself before enabling the worker:

- Windows 10/11 and 64-bit CPython 3.10-3.13 (3.12 recommended)
- CS Demo Manager 3.20.x with its PostgreSQL database configured
- PostgreSQL client tools (`psql.exe`; the runner detects standard installs)
- Counter-Strike 2 and Steam logged in on the rendering Windows account
- FFmpeg downloaded/configured through CS Demo Manager

The official CLI documentation is at
<https://cs-demo-manager.com/docs/cli>.

Run setup once from this directory. Setup creates a private `.venv`; package
installation happens only when the explicit `-InstallDependencies` switch is
present. The pinned wheel is installed from `requirements.txt` and then its
exact version is verified:

```powershell
# Use a known Python 3.12 executable. Quotes are required around paths with spaces.
.\setup-worker.ps1 `
  -PythonPath "C:\path\to\Python312\python.exe" `
  -InstallDependencies

# Non-default Steam library:
.\setup-worker.ps1 `
  -PythonPath "C:\path\to\Python312\python.exe" `
  -InstallDependencies `
  -Cs2Path "D:\SteamLibrary\steamapps\common\Counter-Strike Global Offensive\game\bin\win64\cs2.exe"
```

If `-PythonPath` is omitted, setup probes the Python launcher in this order:
3.12, 3.13, 3.11, then 3.10, and finally `python` from `PATH`. Python 3.14 is
rejected so a changed system default cannot silently create an unsupported
environment. On later checks, omit `-InstallDependencies`; setup will not
contact a package index and only verifies the existing `.venv`.

Do not activate the environment. `run-worker.ps1` always invokes
`.venv\Scripts\python.exe` directly and refuses to fall back to `py`, Conda,
or the system `python`. If `.venv` is incomplete, rerun setup with the explicit
install switch. Setup never replaces or deletes an existing `.venv`.

Copy `config.example.json` to `config.json`. If `csdm` is not in `PATH`, set
`csdm_executable` to the absolute `csdm.cmd` path reported by the checker. The
worker validates that launcher and invokes CSDM's packaged Electron/Node CLI
directly, without passing demo or config paths through `cmd.exe`.
If FFmpeg is not installed through CSDM itself, set `ffmpeg_executable` to the
absolute `ffmpeg.exe` path. It is passed through CSDM's documented custom
FFmpeg location fields; the worker never searches or executes a shell alias.
The repository ignores `.venv`, `config.json`, and `worker-secret.dpapi`; keep
them on this Windows account and never move secrets into Git.

## Network and credentials

The server API must listen only on `127.0.0.1:18791`. Reach it through SSH:

```powershell
ssh -NT -i "$HOME\.ssh\example_bot_key" `
  -L 127.0.0.1:18791:127.0.0.1:18791 admin@192.0.2.10
```

Every request is independently signed with timestamp, nonce and body SHA-256.
Copy `config.example.json` to `config.json`, choose a unique `worker_id`, then
run `setup-secret.ps1`. The DPAPI file can only be decrypted by the same
Windows user and must never be committed or copied to the server.

Run one diagnostic cycle with:

```powershell
.\run-worker.ps1 -Once
```

Do not create a Scheduled Task. Disable any older `Niuniu CS2` logon or
five-minute tasks in Task Scheduler, then double-click
`open-recording-switch.vbs`. The window starts in the OFF state and does not
contact the server. Click **开启高光录制** to create a local enable marker and
start console-free polling. Only while the switch is ON can `start-worker.ps1`
open the loopback-only SSH tunnel and claim one server-created job per cycle.
Click **关闭高光录制** or close the switch window to stop all new claims. An
already-running render is allowed to finish safely.

If one claim fails because CS2 cannot be activated, the game needs attention,
or the tunnel/API is unavailable, the switch automatically returns to OFF.
This prevents a background desktop problem from exhausting every retry in the
server queue; inspect `recording-switch.log` before explicitly enabling it
again.

The graphical switch launches the worker with `CREATE_NO_WINDOW`; it does not
flash PowerShell or Command Prompt windows. Its local enable marker and log are
ignored by Git. `start-worker.ps1` is default-deny and exits before SSH/CSDM if
the marker is absent, so an accidental manual invocation cannot start a render.
The switch and runner contain no API secret; `run-worker.ps1` decrypts the
DPAPI file only inside the current Windows user session.

This command does not install or update packages. It fails before reading the
DPAPI secret if the private environment, pinned dependency, or `config.json`
is missing.

The default `renderer_mode` is `csdm`. CS:DM requires an analyzed demo in its
database, so the worker always invokes this exact shape before rendering:

```text
csdm analyze <demo> --source <perfectworld|valve> --force
csdm video --config-file <generated-csdm-video.json>
```

CS:DM writes into a fresh per-job output folder. The worker rejects zero or
multiple new MP4 files and atomically promotes the unique file to
`highlight.mp4` before hashing and upload. Successful work folders are removed
unless `keep_successful_jobs` is enabled; failed work folders are always
removed because the verified server-side Demo remains available for retries.

When the local switch is enabled, each job claim reports the current CS2 patch
from the configured `steam_inf_path` using the authenticated API. This replaces
the old manual version update: worker reports take precedence over the optional
bootstrap setting `PWCS2_HIGHLIGHT_RENDERER_PATCH`. Closing the switch still
stops all polling; reporting does not install a background task or watcher.

The controller skips Demos older than all reported renderers. Queued jobs with
known incompatible versions are not downloaded; newer Demos remain queued until
a compatible PC connects. Legacy jobs without a version are checked locally;
an obsolete Demo is reported once and skipped without turning off the switch.
Real recording errors or a failed error report still stop the switch. Empty
bootstrap configuration disables filtering only until the first worker report.

CS:DM 3.20 may begin its first `+playdemo` before the in-game plugin installs
`FrameStageNotify`. During an active render only, the worker watches the new
plugin log, focuses the CS2 window, and replays the already validated Demo once
when that race occurs. It confirms that the hook and a non-empty sequence are
active before continuing. This is not a directory watcher or resident helper;
it runs inside the same scheduled job and exits with it. Because CS2 briefly
comes to the foreground, do not play another game in the rendering Windows
session while a highlight job is running.

For an existing private renderer, set `renderer_mode` to `custom` and configure
`renderer_command` as an argv array. Supported placeholders are `{demo}`,
`{manifest}`, `{commands}`, and `{output}`. It is executed with `shell=False`
and receives the same lease heartbeat. Custom mode is optional and does not run
CS:DM.

## Local demo dry run

Parse a local demo with demoparser2 and print conservative 3K/multi-headshot
candidate ranges without opening CS2:

```powershell
.\run-worker.ps1 -LocalDemo "D:\Demos\match.dem" `
  -SteamId 76561197960266101 -Source perfectworld -NoRender
```

Remove `-NoRender` to analyze and render those candidates through CS:DM. Use
`-Source valve` for official matchmaking demos. Local mode never reads the
server HMAC secret or uploads a file.

## Server integration

The repository's `compose.yaml` already contains the `highlight-controller`
and `highlight-api` services under the `highlights` profile. The bot and these
services share only `./data/bot/highlights`, so the intent and worker queues live
at `intents.db` and `jobs.db` there. The controller additionally mounts only
`./data/bot/demos` read/write and `./data/bot/demo-import` read-only; it cannot
see the main bot database or AI memory. The API listens on the container bridge
but is published on the server only as `127.0.0.1:18791`; do not add
`network_mode: host`, a broad `./data/bot:/data` mount, or a
`0.0.0.0:18791` port mapping.

On the server, run the interactive configurator once. It enables the profile,
writes the PWA credential and a per-worker HMAC secret to `.env`, and keeps the
API's video limit aligned with the worker's default 24 MiB limit:

```bash
cd ~/pwcs2-qq-bot
python3 deploy/configure_highlights.py
docker compose config --quiet
docker compose up -d --build --force-recreate bot highlight-controller highlight-api
docker compose ps
```

Paste the one-time generated worker secret into `setup-secret.ps1` on Windows,
not into `config.json`. If `.env` is maintained manually instead, set
`PWCS2_HIGHLIGHT_WORKER_SECRETS_JSON` to a JSON object mapping each worker ID to
an independent random secret of at least 32 bytes. Never put a secret in a URL.

The Windows tunnel is still required because the host port is loopback-only.
Start the SSH command shown above before `run-worker.ps1`; an SSH disconnect
only pauses new claims and does not expose the API publicly.

Completed jobs remain in the store until the bot sends them using
`HighlightVideoDelivery` and calls `mark_delivered`. Files up to the configured
inline limit are sent as a real OneBot `video` segment; larger files require a
trusted HTTPS object URL.
