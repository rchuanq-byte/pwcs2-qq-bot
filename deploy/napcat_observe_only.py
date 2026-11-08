"""Prepare the pinned official NapCat Shell archive without automatic restarts.

This is deliberately NOT a generic minified-JavaScript patcher. A different
release, source digest, or anchor count must be reviewed before it can be used.
Manual WebUI/OneBot restart and QR refresh routes are kept intact.
"""
# Exact upstream minified anchors must remain byte-for-byte unchanged.
# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path

VERSION = "4.18.38"
OFFICIAL_ARCHIVE_SHA256 = "735f2e633247df3ffd6e8ee8b84d7039c315b2655f5807e5f0029f93d2c30016"
OFFICIAL_BUNDLE_SHA256 = "f80e4be3e8640369d23846cf3e8bcfd2a5b6dc5920f50d7ad733d7cca18179e6"
MANIFEST_NAME = "niuniu-observe-only.json"

OFFLINE_ORIGINAL = '''function UAe({
  isOnline: t,
  onOffline: e,
  onRecovered: n,
  restart: r,
  kickedDelayMs: i = 3500,
  selfOfflineDelayMs: o = 6e4
}) {
  let s = !1, a;
  return (c, u) => {
    if (e(c), s) {
      if (u || !a) return;
      clearTimeout(a);
    }
    s = !0;
    const l = setTimeout(() => {
      if (a = void 0, u && t()) {
        s = !1, n();
        return;
      }
      r(c).then((d) => {
        d || (s = !1);
      }, () => {
        s = !1;
      });
    }, u ? o : i);
    a = u ? l : void 0;
  };
}'''

OFFLINE_PATCHED = '''function UAe({
  isOnline: t,
  onOffline: e,
  onRecovered: n,
  selfOfflineDelayMs: o = 6e4
}) {
  // NIUNIU_OBSERVE_ONLY_V1: observe natural recovery, never restart QQ.
  let observationTimer;
  const observeRecovery = () => {
    observationTimer = setTimeout(() => {
      observationTimer = undefined;
      if (t()) n();
      else observeRecovery();
    }, o);
    observationTimer.unref?.();
  };
  return (tips, allowSelfRecovery) => {
    e(tips);
    if (observationTimer !== undefined) clearTimeout(observationTimer);
    observationTimer = undefined;
    if (allowSelfRecovery) observeRecovery();
  };
}'''

PHASE_ORIGINAL = (
    'pe.setQQLoginStatus(!1), pe.setQQLoginQrcodeURL(""), '
    'pe.setQQLoginError(i), pe.setQQLoginPhase("reconnecting");'
)
PHASE_PATCHED = (
    'pe.setQQLoginStatus(!1), pe.setQQLoginQrcodeURL(""), '
    'pe.setQQLoginError(i), pe.setQQLoginPhase("offline");\n'
    '        this.context.logger.logWarn('
    '"[Niuniu observe-only] QQ offline; automatic restart disabled");'
)

EXIT_ORIGINAL = '''      const u = Date.now();
      for (; Gc.length > 0 && u - Gc[0] > O8; )
        Gc.shift();
      Gc.push(u), Gc.length >= Bw && (en.logError(`[NapCat] [${i}] Worker进程在 ${O8 / 1e3} 秒内异常退出 ${Bw} 次，主进程退出`), process.exit(1)), en.logWarn(`[NapCat] [${i}] Worker进程意外退出 (${Gc.length}/${Bw})，正在尝试重新拉起...`), Bg(!0).catch((l) => {
        en.logError(`[NapCat] [${i}] 重新拉起Worker进程失败:`, l);
      });'''

EXIT_PATCHED = '''      // NIUNIU_OBSERVE_ONLY_V1: do not respawn or exit into Docker restart policy.
      en.logWarn(`[Niuniu observe-only] Worker exited (${c}); manual recovery required`);
      globalThis.__niuniuObserveOnlyHold ??= setInterval(() => {}, 3600000);'''

PATCHES = (
    ("offline_recovery", OFFLINE_ORIGINAL, OFFLINE_PATCHED),
    ("offline_ui_phase", PHASE_ORIGINAL, PHASE_PATCHED),
    ("worker_exit", EXIT_ORIGINAL, EXIT_PATCHED),
)


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def patch_bundle(bundle: bytes) -> bytes:
    if sha256(bundle) != OFFICIAL_BUNDLE_SHA256:
        raise ValueError("Unexpected napcat.mjs SHA256; refusing to patch")
    source = bundle.decode("utf-8")
    # Validate every anchor before producing any candidate output.
    for name, original, _replacement in PATCHES:
        count = source.count(original)
        if count != 1:
            raise ValueError(f"Unexpected {name} anchor count: {count}; expected exactly 1")
    for _name, original, replacement in PATCHES:
        source = source.replace(original, replacement, 1)
    return source.encode("utf-8")


def prepare_archive(input_path: Path, output_path: Path) -> dict[str, str]:
    if input_path.resolve() == output_path.resolve():
        raise ValueError("Input and output must be different files")
    if output_path.exists():
        raise FileExistsError("Output already exists; refusing to overwrite")
    original = input_path.read_bytes()
    if sha256(original) != OFFICIAL_ARCHIVE_SHA256:
        raise ValueError("Unexpected official archive SHA256; refusing to patch")
    with zipfile.ZipFile(input_path) as archive:
        names = archive.namelist()
        if names.count("napcat.mjs") != 1 or MANIFEST_NAME in names:
            raise ValueError("Unexpected archive layout; refusing to patch")
        patched = patch_bundle(archive.read("napcat.mjs"))
        manifest = {
            "napcat_version": VERSION,
            "policy": "observe_only",
            "patch": "NIUNIU_OBSERVE_ONLY_V1",
            "official_archive_sha256": OFFICIAL_ARCHIVE_SHA256,
            "official_bundle_sha256": OFFICIAL_BUNDLE_SHA256,
            "patched_bundle_sha256": sha256(patched),
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".napcat-observe-only-", suffix=".zip", dir=output_path.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with zipfile.ZipFile(temporary, "w") as output:
                for entry in archive.infolist():
                    data = patched if entry.filename == "napcat.mjs" else archive.read(entry)
                    output.writestr(entry, data)
                output.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2) + "\n")
            # Link atomically without replacing an output created meanwhile.
            os.link(temporary, output_path)
        finally:
            temporary.unlink(missing_ok=True)
    return {**manifest, "prepared_archive_sha256": sha256(output_path.read_bytes())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = prepare_archive(args.input, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"FAIL {exc}\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
