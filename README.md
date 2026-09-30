# BOOP — your body, your data

Open **BOOP** on your desktop or **Start BOOP.cmd**. The dashboard is at [127.0.0.1:8765](http://127.0.0.1:8765/). It reconnects to your saved strap and keeps recording when you close the browser.

BOOP runs on Windows, reads your BOOP strap directly over Bluetooth, and stores its readings and your entries on this laptop. No account is required. The optional Coach also runs locally using Ollama.

## Install

Use Windows with Bluetooth LE and Python 3.11 or newer on your PATH. In PowerShell:

```powershell
git clone https://github.com/Bodhiswan/boop.git
cd boop
.\setup.ps1
.\start.ps1
```

`setup.ps1` creates the isolated Python environment and BOOP desktop shortcut. To install the optional local Coach and its model, run `.\tools\setup_coach.ps1`. The application, fonts and charts are local; dependency and model installation initially require internet.

The interface uses a compact reading column, grouped live readings and small metric rows. Open a reading's details for its source, missing-data reason and coverage. Light/dark appearance, units and layout preferences are available in Settings.

## Using the dashboard

| Page | Available functions |
| --- | --- |
| Today | Live heart rate, battery, artifact-checked live HRV, saved readings, selectable heart-rate chart, daily Charge/Effort/Rest, dated history and durable history sync. |
| Sleep | Motion/HR sleep detection, V2 or independent V1 staging, primary sleep and naps, recorded sleep corrections, nightly HRV windows, sleep need/debt/consistency, sleep planning, wake alarms and independent wind-down reminders. Motion-aware wake refinement requires dense actual gravity and step samples. |
| Activity | Live workouts, detected-workout merge/dismiss with undo, source-guarded HR recovery, HR zones, calorie estimates and training load. Live lifting programs support typed sets, value provenance, pause/resume, interrupted-session recovery and atomic finish/undo. Steps and imported GPS routes support GPX/FIT exports. |
| Health | Nightly HRV, resting HR, respiratory estimates, temperature, imported oxygen, fitness age, vitality, stress/recovery context, mood and voluntary cycle logs. Descriptive overnight rhythm uses actual RR and motion coverage; personal vital bands and lab-marker history/correlations disclose missing data. Nutrition, body measurements and hydration are saved locally. |
| Insights | Trends and reports, exact range/source comparisons, journal calendar and custom questions, behaviour effects and numeric dose response, before-and-after experiments, weekly comparisons, streaks, circadian rhythm and timing plans. |
| Tools | All 22 NOOP breathing presets, custom pacing, resonance sweeps using real RR observations, below-HR relaxation, live HR guidance, interval timers, stillness HRV capture, decoded sensors and raw waveform plots. Pause/resume sessions; optional sound and strap haptics start only with your action. |
| Device | Connection/history controls, saved-device registry, battery/clock/firmware, alarm readback, HR broadcast, rename and opt-in tap/wrist/HR/inactivity automations. Sustained live RR check-ins and their optional haptics are off by default. Bounded device investigations and five experimental restart candidates are behind explicit confirmations. |
| Data | Local file preview/import, deduplication, per-source deletion, source winners/conflicts, backups/restore, storage inspection and diagnostic cleanup. Exports include readings/sensors/RR, BOOP-compatible tables, nutrition, lifting spreadsheets, routes, reports, recap images and metadata-only support bundles. Scheduled diagnostic exports have run-now, download, retention and clear controls. |
| Settings | Profile/DOB/body measures, units and appearance, sync and capture cadence, custom HR zones, HRV and effort methods, day boundaries, calibration, baseline reset, quiet hours, notification opt-in and Windows startup/tray controls. |
| Coach | Streamed local questions with cancel, completed-only atomic conversation saves, clear/undo, a master switch and optional scheduled local morning brief. Ollama defaults to `qwen3:4b-instruct`; external OpenAI/Anthropic/Gemini/compatible requests need explicit consent per question and a request-only key. Compatible servers on exact loopback HTTP(S) can use optional request authentication and a custom header/prefix. |

Most nightly scores need sufficient covered sleep. Charge needs nightly HRV and at least four usable baseline nights; other estimates have their own sample and profile gates. A blank value means unknown. Coverage and evidence disclosures explain why. Imported scores retain their identity alongside computed NOOP estimates.

V2 staging is enabled by default. Switching it off selects the separate source V1 pipeline, including its explicitly labelled light-stage fallback when gravity is insufficient. Wake refinement defaults off and leaves BOOP output unchanged without actual step samples; estimated daily steps are never substituted. Rhythm descriptions and stress check-ins do not diagnose illness or arrhythmia. Restarted live lifting sessions restore paused and require your resume action; set values distinguish typed, planned and previous-session suggestions.

## Recording and connecting

Wear the charged strap near the laptop. Keep your phone's Bluetooth off if the another phone app takes over its connection. If BOOP cannot find it, remove it briefly and tap it until the blue light flashes, then use Scan or Reconnect. BOOP uses native Windows pairing.

By default, Windows receives an app-scoped request to stay awake while the strap is connected. The display can still sleep. You can turn this off in Settings; quitting BOOP releases the hold. Closing the lid, manual sleep, shutdown or a lost Bluetooth link can still interrupt recording. History sync retrieves banked readings after reconnection. Windows sessions, reminders and repeating alarm maintenance require BOOP to remain running.

History sync commits and fsyncs each complete chunk and a second raw archive **before** acknowledging it. The strap may clear acknowledged history as in ordinary strap sync. Corrupt or incomplete chunks are not acknowledged. The clock is corrected only after an observed drift greater than 30 seconds and verified against subsequent device timestamps. Old incorrectly dated packets retain their original timestamp and remain in raw exports.

Alarms and motor controls are manual features. BOOP distinguishes a Bluetooth write, a firmware reply and a verified stored wake time. Physical vibration and the actual wake event need a wearer check; an unfamiliar BOOP firmware reply does not become a success claim. Ordinary BOOP restart is unconfirmed upstream; experimental candidates report only observed link transitions. No DFU, factory reset, setters for experimental flags/configuration, or force-trim controls are exposed.

## Import and export

Data accepts BOOP CSV/ZIP; Apple Health XML/ZIP; Mi Fitness history; Oura, Fitbit Takeout and Garmin exports; GPX/TCX/supported FIT activities; nutrition CSV; Hevy/Liftosaur logs; program XLSX; and lab/biomarker CSV. These require your own export files. File imports are the Windows counterpart to Apple/Android health integrations.

Preview shows detected records, warnings and skipped content. Apply saves a recovery snapshot first and deduplicates repeated imports. Importing/restoring a saved alarm or automation never arms hardware. Ordinary imports are bounded to 64 MiB, 100,000 parsed records and 256 archive entries. Backups have separate 1 GiB/10-million-row bounds. Unsupported layouts stay visibly unsupported.

A **BOOP .boopbak** archive preserves the supported local data schema and safe profile preferences. Restore merges without overwriting existing IDs, settings or local control state. It restores decoded sensors with remapped frame IDs, import/device registries, workout dismissals and edit history, and paused lifting sheets. Restored Coach messages remain readable archives and are excluded from inference context. Conflicting workout history cannot undo over a later local edit; imported alarms/reminders remain disabled. Execution-control settings and credential tables/fields are removed from exports, with secure deletion and compaction of the copied SQLite so removed values do not remain in free pages. Native NOOP `.noopbak`/SQLite databases are read through their known tables as source records; BOOP does not pretend its database is a native NOOP database.

**Backup & Sync defaults off.** Choose an absolute local folder and enable daily copies if wanted; the default retention is 7, with choices 1/3/5/7/10/14. Manual and daily archives are verified, fsynced and atomically published. Daily copies catch up while BOOP runs and deduplicate successful local days; manual copies do not consume the daily schedule. Listing/download/restore selects verified scheduler-owned filenames. Retention removes only those generated copies; old fixed-folder daily backups and other files are preserved. A folder already synced by another app can upload these unencrypted archives through that app—BOOP does not perform cloud transmission. Store an additional downloaded backup on a drive you control.

Scheduled diagnostic export defaults **off**, at 07:00 in your configured timezone, retaining 14 generations (choices 3/7/14/30/60). While BOOP runs, it writes one successful local support ZIP per day and catches up if today's chosen time has passed. Failures retry without marking success. Run now works independently of the schedule; Clear removes only scheduled copies. Files stay under `data/diagnostics/backups/`; they contain scrubbed support metadata/logs, not the raw health database, identity handshake, credentials or action preferences. No upload or automatic notification occurs.

BOOP-compatible export contains saved/imported facts at the archive root and computed tables in `computed/`, marked **boop (APPROXIMATE)**. Computed Charge/Effort/Rest are mapped to compatible columns, not official proprietary scores. Missing values stay blank, actual sleep/workout UTC instants are preserved, and the cycle key follows NOOP's display-day export convention. The computed range defaults to 30 days; `days=1..366` and `date=YYYY-MM-DD` select another window. Raw readings and sensor exports include the saved device history.

## Your files and privacy

Everything is beside the app:

- `data/*.sqlite`: readings, original packets, decoded sensors, entries and settings; SQLite WAL with synchronous FULL.
- `data/history-archive.jsonl`: second raw copy of acknowledged chunks.
- `data/*sync-backup.sqlite`: consistent sync snapshots.
- `data/backups/`: import/restore recovery snapshots.
- `data/daily-backups/`: default Backup & Sync destination and preserved legacy daily copies; a different absolute local destination may be chosen.
- `data/diagnostics/backups/`: opt-in scheduled/manual diagnostic support ZIPs, with bounded retention.
- `data/settings.json`: the strap to reconnect to.
- `data/boop.log` and server logs: local diagnostics; secret-bearing hello responses are discarded before persistence.
- `.runtime/`: installed local Ollama CLI and model weights.

Download a database snapshot rather than copying a live SQLite file without its WAL. The app server binds only `127.0.0.1` and rejects foreign-origin mutations. Assets, charts and fonts are local. No telemetry or automatic health-data uploads are present. Remote Coach requests send your question, recent conversation and seven days of allowlisted metrics/coverage with explicit per-request consent. Provider keys are not stored or exported. Scheduled briefs use the offline local provider, require both the Coach master and schedule switches, and need BOOP running; notification delivery also respects notification opt-in and quiet hours.

Coach remote endpoints require HTTPS and requests never follow redirects. Compatible-server local mode is restricted to `127.0.0.1`, `localhost` or `::1`; a LAN endpoint is external and requires HTTPS, consent and a request-only key. Custom authentication and supplemental system prompts apply only to the request and cannot override metric/missing-data rules. Cancel closes the active stream; failed or cancelled partial replies are not saved as completed conversation turns.

Model refresh is an explicit provider GET request that sends authentication, without your health summary or conversation; it is bounded to 200 model IDs, a 1 MB response and 15 seconds, with no redirects. On a strictly forward change of calendar day in your profile timezone, Coach starts with fresh conversational context while keeping the bounded saved transcript readable; failed questions leave it unchanged.

## Measurement and platform limits

Computed scores are source-based estimates, not clinical measurements or proprietary strap algorithms. BOOP raw red/IR optical ADC is retained but does not establish a calibrated oxygen percentage. Temperature candidates remain identified as estimates. Rhythm screening uses actual RR intervals and refuses incomplete/banked timing; no arrhythmia verdict is emitted. Imported measured oxygen can be shown. Calories require a sufficient profile; lifting volume is entered weight × reps, not measured strain. HR alone cannot measure distance, and steps require reference calibration or an explicit coefficient. The lab catalog provides marker names/units, not invented clinical normal ranges; report-supplied ranges remain attached to your records.

MG ECG, 5/MG-specific raw collectors, Apple Watch/HealthKit, Health Connect, phone GPS/background alarms, widgets and Siri require their own hardware/operating systems. Optional Oura OAuth/cloud import and Android HTTP push require user-owned credentials/endpoints; local export imports are available without them. The pinned NOOP desktop notification-to-wrist watcher is itself unshipped. BOOP provides Windows tray notifications, a desktop shortcut and optional startup instead.

BOOP currently uses an English Windows interface. Appearance, compact layout, reduced motion and measurement units are supported; native mobile/watch surfaces and multilingual app catalogs are not reproduced by this local browser interface.

This is scoped local BOOP/Windows feature parity, not a claim of every native NOOP feature or physically verified actuator. Native avatars/update inboxes, mobile/watch ecosystem features and native NOOP backup export remain outside the implemented local counterpart. Supported local file imports do not guarantee every provider export variant. The pinned NOOP chart-attachment composer is not shipped, and its Gemini helper/inert toggle does not establish a required vision flow; BOOP's text Gemini adapter is available by explicit request.

## Local CLI/API and repair

The app has a local CLI and HTTP API; no extra MCP or login is needed. Optional Coach setup installs Ollama's CLI at `.runtime/ollama/ollama.exe`, with its API at `127.0.0.1:11434`. The non-thinking default model is `qwen3:4b-instruct`; model inference stays on this computer.

```powershell
.\start.ps1 -NoBrowser
.\.venv\Scripts\python.exe boop.py --help
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

`setup.ps1` repairs the isolated Python environment and desktop shortcut. `tools/setup_coach.ps1` installs/verifies the official Ollama distribution and local model if missing. Initial dependency/model downloads need internet; ordinary recording, imported data, reports and the default Coach do not.

For development, `tools/qa_server.py` opens a disposable preview at port 8766 with Bluetooth and OS actions disabled. It never modifies your actual recordings. Feature API contracts are in `api.py`; source-engine fixtures are in `tests/`.

## Source and attribution

Protocol and analytics guidance: [ryanbr/noop](https://github.com/ryanbr/noop), pinned to `7f396e98ed9d259df08e3a0a58cfac05fc70615c`. The bundled framing helper in `vendor/noop/` is unmodified. Required Notice: Copyright 2026 NoopApp. The PolyForm Noncommercial License, source attribution and third-party notices are included in `vendor/noop/`. This installation is for personal local use. The reference clone is not needed to run BOOP.

`FEATURE_PARITY.md` records the original audit, implementation and remaining platform limits. The full regression run completed 284 tests: 283 passed and one Windows symlink-privilege test was skipped. Python compilation and JavaScript syntax checks passed. Tests use temporary databases and simulated devices; the required pinned, licensed example fixtures are bundled in `vendor/noop/test-fixtures/`, so the reference clone is unnecessary. Browser reviews cover responsive layouts and local record, backup, import, timer, Coach and lifting flows. Physical alarm, vibration and reboot delivery remain separate wearer checks.

Local integration checks covered automatic reconnection, continued recording, clock verification, local model refresh and streamed Coach replies. A real backup was restored into a separate temporary database and verified for row integrity and repeat-restore idempotence. Diagnostic export and cleanup preserved health records. Recording files, credentials, downloaded models, private QA screenshots and reports are excluded from this public repository; `output/` contains local-only verification artifacts.
