# Public video-link evidence

If the primary Bilibili detail API returns HTTP 412, public part lists and
archive descriptions can still be available. The player API reports whether
subtitles require login. The reader uses public metadata alternatives once, without account
cookies, proxy changes, WAF challenges, or full video downloads. When a
public subtitle track is available, it reads a bounded excerpt from an approved
public Bilibili CDN host. Metadata is never treated as an utterance. A single
video-summary request without subtitles or user-supplied evidence gets an
immediate factual limitation message, without calling the model to guess.

Other link turns carry explicit evidence status. Their prompt omits the
sleepy scene cue and tells the model to handle the current request without
deferring it. Existing group personas, response pacing, and mention-only scope
are preserved. See `tests/test_bilibili_public.py` for mocked HTTP fixtures.
