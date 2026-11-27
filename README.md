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
