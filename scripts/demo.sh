#!/usr/bin/env bash
# Live smoke test against a running server. Use a fresh database:
#   DATABASE_PATH=/tmp/demo/prompts.db uvicorn app.main:app --port 8011
#   BASE=http://127.0.0.1:8011 bash scripts/demo.sh
set -euo pipefail
BASE=${BASE:-http://127.0.0.1:8000}
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
status_of() { curl -sS -o /tmp/demo-body -w "%{http_code}" "$@"; }

echo "== /docs =="
curl -fsS -o /dev/null -w "openapi.json %{http_code}\n" "$BASE/openapi.json"
curl -fsS -o /dev/null -w "swagger  /docs  %{http_code}\n" "$BASE/docs"

echo
echo "== catalogue: P-101 zh(地点) / en(地点,时间); P-102 en(分钟) =="
zh=$(curl -fsS -X POST "$BASE/api/templates" -H 'content-type: application/json' \
  -d '{"prompt_no":"P-101","lang":"zh","content":"请到{地点}集合。"}')
zh_id=$(echo "$zh" | json "d['id']")
curl -fsS -X POST "$BASE/api/templates/$zh_id/publish" >/dev/null
en=$(curl -fsS -X POST "$BASE/api/templates" -H 'content-type: application/json' \
  -d '{"prompt_no":"P-101","lang":"en","content":"Please gather at {地点} by {时间}."}')
en_id=$(echo "$en" | json "d['id']")
curl -fsS -X POST "$BASE/api/templates/$en_id/publish" >/dev/null
closing=$(curl -fsS -X POST "$BASE/api/templates" -H 'content-type: application/json' \
  -d '{"prompt_no":"P-102","lang":"en","content":"The hall closes in {分钟} minutes."}')
closing_id=$(echo "$closing" | json "d['id']")
curl -fsS -X POST "$BASE/api/templates/$closing_id/publish" >/dev/null

echo
echo "== enable fw-2.3.0 with P-101(en: 地点,时间) + P-102(en: 分钟) =="
curl -fsS -X POST "$BASE/api/firmwares" -H 'content-type: application/json' \
  -d '{"firmware_id":"fw-2.3.0","name":"Hall A"}' >/dev/null
curl -fsS -X PUT "$BASE/api/firmwares/fw-2.3.0/enable" -H 'content-type: application/json' \
  -d '{"prompts":[
        {"prompt_no":"P-101","lang":"en","variables":["地点","时间"]},
        {"prompt_no":"P-102","lang":"en","variables":["分钟"]}
      ]}' >/dev/null

echo "-- English plays:"
curl -fsS -X POST "$BASE/api/firmwares/fw-2.3.0/prompts/P-101/play" \
  -H 'content-type: application/json' -d '{"variables":{"地点":"north gate","时间":"10:00"}}'
echo

echo
echo "== English fallback: fw-1.8.0 declares only 地点, so English (地点,时间) is ineligible =="
curl -fsS -X POST "$BASE/api/firmwares" -H 'content-type: application/json' \
  -d '{"firmware_id":"fw-1.8.0","name":"Hall B"}' >/dev/null
curl -fsS -X PUT "$BASE/api/firmwares/fw-1.8.0/enable" -H 'content-type: application/json' \
  -d '{"prompts":[{"prompt_no":"P-101","lang":"en","variables":["地点"]}]}' >/dev/null
curl -sS -D /tmp/play-headers -X POST "$BASE/api/firmwares/fw-1.8.0/prompts/P-101/play" \
  -H 'content-type: application/json' -d '{"variables":{"地点":"北门"}}'; echo
grep -i 'x-prompt-fallback' /tmp/play-headers

echo
echo "== enable refused: P-103 has no template; P-102 needs 分钟 the firmware lacks =="
curl -fsS -X POST "$BASE/api/firmwares" -H 'content-type: application/json' \
  -d '{"firmware_id":"fw-beta","name":"Beta"}' >/dev/null
code=$(status_of -X PUT "$BASE/api/firmwares/fw-beta/enable" -H 'content-type: application/json' \
  -d '{"prompts":[
        {"prompt_no":"P-103","lang":"en","variables":[]},
        {"prompt_no":"P-102","lang":"zh","variables":[]}
      ]}')
echo "status $code"; cat /tmp/demo-body; echo
echo "fw-beta after refusal: $(curl -fsS "$BASE/api/firmwares/fw-beta" | json "d['enabled']")"

echo
echo "== withdraw refused: only English template for P-102, already in use by fw-2.3.0 =="
code=$(status_of -X POST "$BASE/api/templates/$closing_id/withdraw")
echo "status $code"; cat /tmp/demo-body; echo
echo "template still: $(curl -fsS "$BASE/api/templates/$closing_id" | json "d['status']")"
echo "fw-2.3.0 missing after refusal: $(curl -fsS "$BASE/api/firmwares/fw-2.3.0/missing")"

echo
echo "== add a replacement P-102 template, then withdraw the old one =="
closing2=$(curl -fsS -X POST "$BASE/api/templates" -H 'content-type: application/json' \
  -d '{"prompt_no":"P-102","lang":"en","content":"Closing in {分钟} min, please head to the exit."}')
closing2_id=$(echo "$closing2" | json "d['id']")
curl -fsS -X POST "$BASE/api/templates/$closing2_id/publish" >/dev/null
curl -fsS -X POST "$BASE/api/templates/$closing_id/withdraw" >/dev/null
echo "old withdrawn; new request plays:"
curl -fsS -X POST "$BASE/api/firmwares/fw-2.3.0/prompts/P-102/play" \
  -H 'content-type: application/json' -d '{"variables":{"分钟":5}}'; echo

echo
echo "== English withdrawn is fine while Chinese fallback covers P-101 =="
curl -fsS -X POST "$BASE/api/templates/$en_id/withdraw" >/dev/null
echo "fw-2.3.0 P-101 now:"
curl -sS -D /tmp/play-headers -X POST "$BASE/api/firmwares/fw-2.3.0/prompts/P-101/play" \
  -H 'content-type: application/json' -d '{"variables":{"地点":"north gate","时间":"10:00"}}'; echo
grep -i 'x-prompt-fallback' /tmp/play-headers

echo
echo "== withdrawing the last template for P-101 is refused =="
code=$(status_of -X POST "$BASE/api/templates/$zh_id/withdraw")
echo "status $code"; cat /tmp/demo-body; echo
