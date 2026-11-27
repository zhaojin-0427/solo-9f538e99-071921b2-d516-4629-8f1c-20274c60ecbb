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

############################################################################
# 录音棚：制单 -> 复核期改句 -> 交件 -> 首个录音号 -> 再次出单复用
############################################################################
echo
echo "############################################################"
echo "# 录音棚 制单 / 交件 demo (recording orders & deliveries)"
echo "############################################################"

echo
echo "== seed recording catalogue: REC-W1/REC-W2 不同提示号、同一句 =="
rw1=$(curl -fsS -X POST "$BASE/api/templates" -H 'content-type: application/json' \
  -d '{"prompt_no":"REC-W1","lang":"en","content":"Welcome to the recording studio."}')
rw1_id=$(echo "$rw1" | json "d['id']")
curl -fsS -X POST "$BASE/api/templates/$rw1_id/publish" >/dev/null
rw2=$(curl -fsS -X POST "$BASE/api/templates" -H 'content-type: application/json' \
  -d '{"prompt_no":"REC-W2","lang":"en","content":"Welcome to the recording studio."}')
rw2_id=$(echo "$rw2" | json "d['id']")
curl -fsS -X POST "$BASE/api/templates/$rw2_id/publish" >/dev/null
num=$(curl -fsS -X POST "$BASE/api/templates" -H 'content-type: application/json' \
  -d '{"prompt_no":"REC-NUM","lang":"zh","content":"第{num}条提示录音"}')
num_id=$(echo "$num" | json "d['id']")
curl -fsS -X POST "$BASE/api/templates/$num_id/publish" >/dev/null

curl -fsS -X POST "$BASE/api/firmwares" -H 'content-type: application/json' \
  -d '{"firmware_id":"fw-rec-a","name":"rec-a"}' >/dev/null
curl -fsS -X PUT "$BASE/api/firmwares/fw-rec-a/enable" -H 'content-type: application/json' \
  -d '{"prompts":[
        {"prompt_no":"REC-W1","lang":"en","variables":[]},
        {"prompt_no":"REC-NUM","lang":"zh","variables":["num"]}
      ]}' >/dev/null
curl -fsS -X POST "$BASE/api/firmwares" -H 'content-type: application/json' \
  -d '{"firmware_id":"fw-rec-b","name":"rec-b"}' >/dev/null
curl -fsS -X PUT "$BASE/api/firmwares/fw-rec-b/enable" -H 'content-type: application/json' \
  -d '{"prompts":[{"prompt_no":"REC-W2","lang":"en","variables":[]}]}' >/dev/null

cat > /tmp/order-req.json <<'JSON'
{
  "batch_no": "BATCH-DEMO-001",
  "note": "一楼展厅预录批次",
  "samples": [
    {"sample_no": "A1", "firmware_id": "fw-rec-a", "prompt_no": "REC-W1", "variables": {}},
    {"sample_no": "A2", "firmware_id": "fw-rec-b", "prompt_no": "REC-W2", "variables": {}},
    {"sample_no": "B1", "firmware_id": "fw-rec-a", "prompt_no": "REC-NUM", "variables": {"num": 3}},
    {"sample_no": "X1", "firmware_id": "fw-rec-b", "prompt_no": "REC-MISSING", "variables": {}},
    {"sample_no": "X2", "firmware_id": "fw-ghost", "prompt_no": "REC-W1", "variables": {}}
  ]
}
JSON

echo
echo "== 制单 POST /api/recording-batches (同句 A1/A2 合并；X1/X2 另列原因) =="
curl -fsS -X POST "$BASE/api/recording-batches" -H 'content-type: application/json' \
  --data @/tmp/order-req.json > /tmp/order1.json
python3 - <<'PY'
import json
d = json.load(open('/tmp/order1.json'))
print(f"order_id={d['order_id']} revision={d['revision']} "
      f"samples={d['sample_count']} entries={d['entry_count']} "
      f"unresolved={d['unresolved_count']}")
for e in d["entries"]:
    print(f"  条目 {e['entry_no']} (entry_id={e['entry_id']}) "
          f"[{e['lang']}] {e['text']}  samples={e['sample_numbers']} "
          f"recording_no={e['recording_no']}")
for u in d["unresolved"]:
    print(f"  无法取句 {u['sample_no']}: {u['reason_code']} - {u['reason_message']}")
PY
welcome_entry=$(cat /tmp/order1.json | json "next(e['entry_id'] for e in d['entries'] if e['text']=='Welcome to the recording studio.')")

echo
echo "== 复核期间改句: PATCH REC-W1 -> 'Welcome to Studio A.' (仍可提交) =="
code=$(status_of -X PATCH "$BASE/api/templates/$rw1_id" -H 'content-type: application/json' \
  -d '{"content":"Welcome to Studio A."}')
echo "PATCH status $code"

echo
echo "== 交件 POST /api/deliveries 批号+原条目(entry_id=$welcome_entry)+REC-DEMO-001 =="
echo "   只复核该条 A1/A2：A1 已改句不匹配，A2 仍是原句；至少一例匹配 -> 入账"
code=$(status_of -X POST "$BASE/api/deliveries" -H 'content-type: application/json' \
  -d "{\"batch_no\":\"BATCH-DEMO-001\",\"entry_id\":$welcome_entry,\"recording_no\":\"REC-DEMO-001\"}")
echo "status $code"
python3 - <<'PY'
import json
d = json.load(open('/tmp/demo-body'))
print(f"status={d['status']} booked={d['booked']} "
      f"effective={d['effective_recording_no']} "
      f"matched={d['matched_count']} mismatched={d['mismatched_count']}")
for m in d["mismatched"]:
    print(f"  不匹配样例 {m['sample_no']}: {m['reason_code']} -> {m['text']!r}")
PY

echo
echo "== 同条再交 REC-DEMO-002：同批号同语种同句保留首个录音号 =="
code=$(status_of -X POST "$BASE/api/deliveries" -H 'content-type: application/json' \
  -d "{\"batch_no\":\"BATCH-DEMO-001\",\"entry_id\":$welcome_entry,\"recording_no\":\"REC-DEMO-002\"}")
echo "status $code"
cat /tmp/demo-body | json "f\"delivery status={d['status']} effective_recording_no={d['effective_recording_no']}\""

echo
echo "== 再次出单（同批号同样例）：原句条目标出 REC-DEMO-001，改后句子是无录音号新条目 =="
curl -fsS -X POST "$BASE/api/recording-batches" -H 'content-type: application/json' \
  --data @/tmp/order-req.json > /tmp/order2.json
python3 - <<'PY'
import json
d = json.load(open('/tmp/order2.json'))
print(f"revision={d['revision']} reissued={d['reissued']} entries={d['entry_count']}")
for e in d["entries"]:
    print(f"  条目 {e['entry_no']} (entry_id={e['entry_id']}) [{e['lang']}] {e['text']!r} "
          f"recording_no={e['recording_no']} scope={e['recording_scope']}")
PY

echo
echo "== 另一批号出同一句：跨批号录音库复用 REC-DEMO-001 =="
code=$(status_of -X POST "$BASE/api/recording-batches" -H 'content-type: application/json' \
  -d '{"batch_no":"BATCH-DEMO-002","samples":[{"firmware_id":"fw-rec-b","prompt_no":"REC-W2"}]}')
echo "status $code"
cat /tmp/demo-body | json "f\"text={d['entries'][0]['text']!r} recording_no={d['entries'][0]['recording_no']} scope={d['entries'][0]['recording_scope']}\""

echo
echo "== 两句都改掉后再交旧条目：无一匹配 -> 409 delivery_not_booked，不入账 =="
curl -fsS -X PATCH "$BASE/api/templates/$rw2_id" -H 'content-type: application/json' \
  -d '{"content":"Welcome to Studio B too."}' >/dev/null
code=$(status_of -X POST "$BASE/api/deliveries" -H 'content-type: application/json' \
  -d "{\"batch_no\":\"BATCH-DEMO-001\",\"entry_id\":$welcome_entry,\"recording_no\":\"REC-DEMO-003\"}")
echo "status $code"
python3 - <<'PY'
import json
d = json.load(open('/tmp/demo-body'))
print(f"code={d['code']} booked={d['booked']} matched={d['matched_count']} "
      f"mismatched={d['mismatched_count']}")
for m in d["mismatched"]:
    print(f"  样例 {m['sample_no']}: {m['reason_code']} now={m['text']!r}")
PY
echo "批号录音登记仍只有首个: $(curl -fsS "$BASE/api/recording-batches/BATCH-DEMO-001/recordings" | json "[r['recording_no'] for r in d]")"
