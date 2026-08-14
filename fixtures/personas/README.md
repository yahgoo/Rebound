# Persona fixtures

Pre-recorded multimodal inputs for Interpreter (Task 16+) and the demo reset in `ops/demo.sh`. Not real passenger data — reviewers supply recordings under the naming below.

## Naming

For each persona slug `{slug}`:

| File | Role |
|---|---|
| `{slug}_voice.m4a` | Short voice note (traveller language) |
| `{slug}_board.jpg` | Departure-board / gate photo (JPEG) |

## Personas

| Slug | Persona | Notes |
|---|---|---|
| `tan` | **Mrs. Tan** (71) | Mandarin, mobility limits (e.g. walks with a cane). Rehearsed demo path. |
| `biz` | Business traveller | English; time-critical connection / meeting deadline. |
| `family` | Family of four | Multi-passenger recovery; English or mixed. |

## Present in this tree

- `tan_voice.m4a` — Mandarin voice note (disruption constraints: Singapore arrival deadline, budget, cane/mobility). Synthesized via macOS `say -v Tingting` (offline TTS), not a human recording; genuine Mandarin audio content for transcription/language detection.
- `tan_board.jpg` — **real iPhone 15 Pro Max photo** (genuine EXIF: Make/Model/timestamp) of a **departure-board screen** showing cancelled CGK→SIN content aligned with the voice narrative. Used both as Interpreter photo evidence and to prove `redact_image_metadata` strips EXIF before Zone C. Replaces earlier fixtures (synthetic JFIF with no EXIF; unrelated boarding-pass capture).

Supply `biz_voice.m4a`, `biz_board.jpg`, `family_voice.m4a`, and `family_board.jpg` the same way when seeding the other two demo orders.
