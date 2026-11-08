# Vendored Dafeiyu group-conversation modules

Upstream: https://github.com/nanbengxian-cyber/dafeiyu-qq-bot

Pinned commit: `8009d1036e5a0a6fa2a2490429375ff4735c5b67`.

Original copyright and MIT license are preserved in LICENSE. These are real upstream
implementations, not similarly named replacement prompts. The complete 41-module personality
and conversation chain is included. The machine-readable inventory and activation policy is
`src/pwcs2_bot/dafeiyu_persona.py`; see `deploy/ASTRBOT-PERSONA-SUITE.md` for scope.
Nine capability modules from the same pin are also included: web, video, voice, sticker,
steal, poke, welcome, guard and joinguard. Payment, autonomous moderation, arbitrary desktop
control and paid media generation remain disabled. Image input and native speech
transcription use bounded Niuniu bridges. See `deploy/ASTRBOT-UPSTREAM-CAPABILITIES.md`.

Local compatibility changes are recorded here and in Git:

- 2026-10-29: replace the prefabricated food/whale-interest template with current-group
  discussion context. Retire periodic cravings and the old appetite drive, equalize
  topic base scores and prevent cross-group heat injection. Keep growth, genuine member
  memories and normal food discussions. See `deploy/ASTRBOT-TOPIC-PERSONA.md`.

- Persona/topic aliases use 牛牛精灵 and CS2 rather than adopting the upstream whale identity.
- Synthetic user 0 is not collected as a real person's memory or persona profile.
- Deployment config supplies target group, owners and model IDs through upstream environment
  settings; no upstream example IDs become trusted production administrators.
- A separate AstrBot output policy enforces the existing group allowlist, quiet window,
  automatic-turn authorization and read-only AI tool set, including nonstandard plugin send
  paths. `aiCompanionUseUpstreamLimits` optionally delegates frequency to the pinned modules
  without the extra shared deployment budget; see deploy/ASTRBOT-UPSTREAM-FREQUENCY.md.
- `/档案记住` is the personal-profile command, avoiding a collision with the existing
  `/记住` / `#记住` shared group-memory feature. `/我和牛牛` retains `/我和鱼` as an alias.
- The local quiet bridge suppresses idle perception/automatic triggering before the deadline.
- Growth modules are explicitly live, including interest, emotion, slang research/auto-review,
  follow-up, agency and will. `dsh-mind` retains the upstream's only supported P0 observe mode.
- All 41 modules get the real group/operator/alias mapping. The original whale's female gender,
  birth date, US server, administrator status, payment and media generation capabilities are
  not asserted as Niuniu facts. Self-awareness's closed capability list matches this deployment.
- Claim evidence is a transient TextPart so the final immutable system prompt does not discard
  it. Successful current-image observation precedes self-awareness. Quotes use verified
  same-group author IDs; untrusted Reply components cannot make core fetch arbitrary media.
- Merge's managed path submits a fresh event through AstrBot's full hooks/tools/output policy,
  never a bare background `llm_generate` -> direct send. Synthetic turns do not become human
  learning samples. Follow-up is armed after an actual send, not an unsent model draft.
- Humanization/temperament output effects skip explicit tasks, tool results, code, links and
  long answers. Factual/task input avoids fatigue/style/drift interference. Diagnostic and
  configuration commands are centrally restricted to the trusted operator; personal profile
  and opt-out commands remain public. Capability tools are separately scoped by group.
- Seed dictionaries are copied once. Plugin JSON/SQLite learning state survives restarts and
  image updates. Upstream tests remain in source, not the production plugin directory.
- Portable upstream test fixtures include the added emoji helper in AST extraction, honor
  configurable emotion intensity/reset counts, set explicit test group/operator IDs, and use
  local source paths/current decorator signatures. Capability expectations reflect Niuniu's
  deliberately smaller authority rather than assuming payment/moderation providers exist.
- Managed merge excludes factual/tool tasks, preserving the existing concurrent question
  workflow. aiocqhttp's late/duplicate API echo guard preserves first-result/timeout semantics
  instead of closing a whole reverse WebSocket on an already-completed Future.
- Empty model environment entries fall back to the configured default rather than sending
  an invalid blank model name. Explicit AI failures get a short safe acknowledgment;
  uninvited infrastructure errors remain silent and SDK details never go to the group.
- Web uses public Bing RSS first when no paid search key is configured, retaining the upstream
  webpage/SSRF/content-moderation pipeline. Secret-bearing searches are refused.
- Voice has an explicit free Edge-only path, using edge-tts 7.3.1 (older handshakes failed).
  No stored Fish/other paid provider key is activated. Existing QQ transcription stays native.
- Raw OneBot video segments are bridged to the real upstream four-frame extractor, fixing
  AstrBot's missing Video components. Downloads, decoded dimensions, ffmpeg protocols,
  process time and concurrency are bounded. Video generation is disabled; no audio inferred.
- Sticker paths are canonicalized inside the configured library, local file harvesting is
  disabled, and downloaded images are public-only/size-bounded. An existing user-authorized
  xh image seeds the library. Welcome/poke/stickers/management stay in the original group.
- Guard and joinguard return through the human-approval bridge before every mutation,
  even if upstream configuration says auto. The trusted operator must confirm an exact,
  expiring single-use proposal; fresh QQ roles are checked and bans cannot exceed 600 s.
  Native API policy independently rejects approval-less management and cross-group output.
