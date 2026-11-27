# Museum Guide Prompt API

Prompts for museum hand-held guide devices, with per-firmware variable
capabilities. Firmware rolls out in batches, so old and new devices may send
different variables for the same prompt; this service stores the catalogue in
SQLite and only ever hands a device a sentence it can fully fill.

## Run

```bash
docker compose up --build      # http://localhost:8000/docs
```

The SQLite database lives in the `prompt-data` volume at `/data/prompts.db`.
Locally without Docker:

```bash
pip install -r requirements.txt
DATABASE_PATH=./data/prompts.db uvicorn app.main:app --port 8000
python -m pytest -q
BASE=http://127.0.0.1:8000 bash scripts/demo.sh   # against a fresh DB
```

## Model

- **Template** — a sentence for a `prompt_no` in `zh` or `en`, written with
  `{变量名}` placeholders. `{{` and `}}` are literal braces. Lifecycle:
  `draft → published → withdrawn`, and `withdrawn → published` again.
- **Firmware** — a batch of devices. Enabling it records, for each prompt
  number it will play, the **language** and the **variable names** its build
  sends. Firmware starts disabled.
- **Playback** — `POST /api/firmwares/{firmware_id}/prompts/{prompt_no}/play`
  with `{"variables": {...}}` returns the filled sentence.

## Selection rules

Only **published** templates are considered, and only those whose variables are
all declared by the firmware. Then:

1. Use the requested language.
2. For English, fall back to Chinese if no English template qualifies.
   (Chinese never falls back to English.)
3. Among the chosen language, prefer the template using the **most distinct
   variables**.
4. Break ties with the **most recently published** template.

## Supply invariant

> Every prompt configured on an **enabled** firmware always has a playable
> template.

- **Enable** replaces the firmware's configuration and enables it in one
  transaction. If any listed prompt is unplayable, the whole request is refused
  with `409 prompt_missing`, listing `{firmware_id, prompt_no, lang}` for every
  lost prompt. Nothing is written: a disabled firmware stays disabled, and an
  already-enabled firmware keeps its old configuration.
- **Withdraw** first applies the status change in the same transaction, then
  re-checks only the enabled firmwares configured with the **same prompt
  number**. If any would lose its last playable template, the transaction rolls
  back and the response lists the affected firmware/prompt pairs.
- **Edit** (`PATCH`) of a published template is checked the same way; editing a
  draft or withdrawn template is always allowed. Publishing only adds supply.
- **Playback** uses a read snapshot, so it never sees a half-applied change, and
  an enable/withdraw that is refused leaves the old supply serving new
  requests. Writers serialise on `BEGIN IMMEDIATE`; readers run in WAL mode
  without blocking.

## Recording orders & deliveries (制单 / 交件)

Several firmwares sometimes play the very same sentence, so the studio records
each distinct sentence only once. Two API groups implement the hand-off:

- **制单 — `POST /api/recording-batches`** (alias `POST /api/orders`,
  `POST /api/recording-orders`). Body:

  ```json
  {
    "batch_no": "BATCH-20261127-01",
    "note": "一楼展厅",
    "samples": [
      {"sample_no": "A1", "firmware_id": "fw-rec-a",
       "prompt_no": "REC-W1", "variables": {}},
      {"sample_no": "A2", "firmware_id": "fw-rec-b",
       "prompt_no": "REC-W2", "variables": {}}
    ]
  }
  ```

  The whole order is resolved inside **one transaction snapshot of the latest
  committed catalogue**, using exactly the playback rules
  (`app.domain.choose` + `render`: published templates only, declared
  variables, language preference, `en→zh` fallback, most variables, latest
  publish). Resolved samples are merged into **entries (条目)** by
  **actual language (`used_lang`, after fallback) + full composed sentence**.
  Each entry carries its numbered `samples`, a global `entry_id`, and any
  already-known `recording_no` (batch first, then the studio-wide library).
  Samples that cannot be taken are returned separately in `unresolved` with a
  stable `reason_code` (`firmware_not_found`, `prompt_not_configured`,
  `missing_variables`, `invalid_variable_value`, `no_playable_template`).

  Posting the same `batch_no` again is **再次出单**: it creates a new immutable
  `revision`; booked recording numbers are still marked on matching entries.

- **交件 — `POST /api/deliveries`** or
  `POST /api/recording-batches/{batch_no}/deliveries`. The voice actor brings
  the batch number, the **original entry** (`entry_id`, or `entry_no` /
  `"E2"` optionally with `revision`/`order_id`), and a `recording_no`:

  ```json
  {"batch_no": "BATCH-20261127-01", "entry_id": 1, "recording_no": "REC-0001"}
  ```

  Only the samples merged into that entry are re-reviewed, on a **fresh
  transaction against the latest committed catalogue** — template edits,
  publish/withdraw and firmware reconfiguration committed during recording are
  therefore seen at booking time. A delivery is booked only when **at least
  one** sample still resolves to the entry's **original language and original
  sentence**. The response lists `matched` and `mismatched` samples (with the
  current language/text and a reason such as `sentence_changed`,
  `language_changed`, `missing_variables`, `no_playable_template`). With zero
  matches the API returns `409 delivery_not_booked` and writes nothing.

  **First recording number wins** for the same `(batch_no, lang, sentence)`:
  a repeat/different number comes back `status: "reused"` with
  `effective_recording_no` set to the first number. A mismatched sample that
  now resolves to a sentence already recorded carries
  `current_recording_no` so the later sample can reuse it. Booked pairs also
  enter the studio-wide `recordings` library, so a later **different** batch
  ordering the same language+sentence gets the existing number marked
  (`recording_scope: "library"`).

Read-only helpers: `POST .../review` (per-entry re-review without writing),
`GET /api/recording-batches[/{batch_no}]`, `.../orders[/{revision}]`,
`.../deliveries`, `.../recordings`, and `GET /api/recordings`.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/templates` | Create a draft template |
| `GET` | `/api/templates?prompt_no=&lang=&status=` | List templates |
| `GET` | `/api/templates/{id}` | Get one |
| `PATCH` | `/api/templates/{id}` | Edit content |
| `POST` | `/api/templates/{id}/publish` | Publish |
| `POST` | `/api/templates/{id}/withdraw` | Withdraw (guarded) |
| `POST` | `/api/firmwares` | Register firmware |
| `GET` | `/api/firmwares?enabled=` | List firmwares and configs |
| `GET` | `/api/firmwares/{id}` | Get one |
| `PUT` | `/api/firmwares/{id}/enable` | Configure + enable (guarded) |
| `POST` | `/api/firmwares/{id}/disable` | Take out of service |
| `GET` | `/api/firmwares/{id}/missing` | Audit unplayable prompts |
| `POST` | `/api/firmwares/{id}/prompts/{prompt_no}/play` | Render a sentence |
| `POST` | `/api/recording-batches` | 制单 / 再次出单 (numbered samples) |
| `GET` | `/api/recording-batches` | List batches |
| `GET` | `/api/recording-batches/{batch}` | Latest order sheet |
| `GET` | `/api/recording-batches/{batch}/orders[/{rev}]` | Order revisions |
| `POST` | `/api/recording-batches/{batch}/review` | Read-only per-entry re-review |
| `POST` | `/api/deliveries` | 交件 + review + book/reuse |
| `POST` | `/api/recording-batches/{batch}/deliveries` | Same, batch in path |
| `GET` | `/api/recording-batches/{batch}/deliveries` | Delivery history |
| `GET` | `/api/recording-batches/{batch}/recordings` | Batch recording registry |
| `GET` | `/api/recordings?lang=&text=` | Studio-wide sentence→recording library |
| `GET` | `/api/health` | Liveness |

Errors are `{"code", "message", ...}`; supply conflicts carry a `missing`
array. Playback responses include `used_lang`, `fallback`, `template_id`, and
the rendered `text`; a fallback also sets `X-Prompt-Fallback: en->zh`.

## Demo script

`scripts/demo.sh` walks through: `/docs`, English playback, English→Chinese
fallback when the English template needs a variable the firmware lacks, a
refused enable listing two missing prompts, a refused withdraw listing the
affected firmware, a replacement template making the withdraw possible, and a
refused withdrawal of the last template.

Its recording-studio act then demonstrates 制单/交件 end to end: two firmware
cases for different prompt numbers merge into one entry because their full
sentences are identical, unresolvable samples are listed with reasons, a
template sentence is changed during review, the delivery is booked with one
matching and one mismatched sample, a second recording number for the same
entry is collapsed to the first (`reused`), **再次出单** marks the original
sentence with the existing recording number while the changed sentence becomes
a new unrecorded entry, another batch reuses the number from the studio
library, and changing both sentences makes a later delivery fail with
`409 delivery_not_booked`.
