# Changelog

All notable changes to VideoRec will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v10.14.16] - 2026-10-13

### Fixed

- **Critical: Silent exception swallow in recorder creation (4-cam 'randomly records 3' bug)** -- `_start_recording` in `ui/main_window.py` had a `try/except` block that silently swallowed exceptions when creating `Recorder` instances. Symptom: 1 of 4 cameras randomly produced no recording file while still showing live preview; log gave no hint. All exceptions are now logged to `videorec_error.log` with full traceback and surfaced to the user via a dialog.
- **Duration auto-stop never triggered** -- `_rec_duration_limit` was never initialized from `self.duration_spin.value()`. Added the assignment in `_start_recording` so the user-configured duration limit now takes effect (10s/30s auto-stop verified in test7).
- **MSMF backend fallback on Win10** -- Removed the MSMF (Microsoft Media Foundation) fallback in `Camera.open()`. MSMF on Win10 had a multi-camera contention bug (4 same-model USB cameras randomly interfering, 1 of 4 stream dead). DSHOW is now forced on all Windows versions; the multi-process architecture already handles DSHOW's same-process same-model limitation.

### Added

- Pre-recording `rec-check` log line showing each slot's status before recording starts (the v10.14.14 `_wait_for_pending_slots` helper is preserved and integrated)
- Detailed `recorder create failed` log line with full traceback when a slot's recorder creation fails
- Real-fps probe button in the resolution panel (`ui/settings_panel.py:_probe_real_fps`) -- detects firmware-locked vs transport-dropped frame loss and suggests a sustainable fps target

### Known Limitations

- **4 cameras at 1920x1080@30fps** -- USB bandwidth physically limits this to ~10 fps per channel on both Win10 and Win7 (verified in test7 session 3). Use 1280x720@30fps for 4-cam full-rate recording.

### Verified

- 4-cam simultaneous recording at 800x600@30fps (test7 session 1: 300 frames each at ~29.6 fps, 10s auto-stop)
- 4-cam simultaneous recording at 1280x720@30fps (test7 session 2: 300 frames each at ~29.6 fps, 10s auto-stop)
- Duration limit: 10s/30s settings both stop recording at exact time (rec-stop log shows exact frames captured)

## [v10.14.11] - 2026-08-13

### Initial Public Release

- 4-channel simultaneous USB camera recording (multi-process capture, one worker subprocess per channel)
- Per-channel reference-frame ghost overlay + ORB feature matching (cross-session rig repeatability)
- Mouse-region click trigger (software-level soft sync, no hardware TTL)
- Per-channel software frame timestamps (sidecar CSV for cross-device alignment)
- In-recording notes (F1-F9 behavioral markers, written to per-channel meta.json)
- Offline Win7 SP1 build (PyInstaller onedir, UCRT/KB2999226)