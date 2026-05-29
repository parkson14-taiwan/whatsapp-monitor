import os
import uuid
import json
from datetime import datetime
from flask import Flask, request, jsonify
import anthropic
import requests

app = Flask(__name__)

WASSENGER_API_KEY = os.environ.get(
    "WASSENGER_API_KEY",
    "829ec285c20ba06feadd191e42e39b47bdb1165b0b08d0fea1ab869e51dac168707390d65cf41e07",
)
WASSENGER_SEND_URL = "https://api.wassenger.com/v1/messages"
DRAFTS_KEY = "whatsapp:pending_drafts"

# ── Storage: Vercel KV (Redis) ────────────────────────────────────────────────
try:
    from upstash_redis import Redis as UpstashRedis
    _redis = UpstashRedis(
        url=os.environ["KV_REST_API_URL"],
        token=os.environ["KV_REST_API_TOKEN"],
    )
    USE_REDIS = True
except Exception:
    USE_REDIS = False
    _local: dict = {}


def get_drafts() -> dict:
    if USE_REDIS:
        try:
            raw = _redis.get(DRAFTS_KEY)
            if raw:
                return json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            print(f"[Redis get] {e}")
        return {}
    return _local


def save_drafts(drafts: dict) -> None:
    if USE_REDIS:
        try:
            _redis.set(DRAFTS_KEY, json.dumps(drafts, ensure_ascii=False))
        except Exception as e:
            print(f"[Redis set] {e}")
    else:
        global _local
        _local = drafts


# ── Claude ────────────────────────────────────────────────────────────────────
def draft_reply_with_claude(sender_name: str, message_body: str):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=(
            "You are a helpful customer service assistant. "
            "When given an incoming message, respond in this exact format:\n\n"
            "SUMMARY: [1-2 sentences summarising what the customer wants]\n\n"
            "DRAFT REPLY:\n[polite, professional reply in the same language the customer used]"
        ),
        messages=[{"role": "user", "content": f"Message from {sender_name}:\n\n{message_body}"}],
    )
    text = response.content[0].text
    summary, draft = "", text
    if "SUMMARY:" in text and "DRAFT REPLY:" in text:
        parts = text.split("DRAFT REPLY:")
        summary = parts[0].replace("SUMMARY:", "").strip()
        draft = parts[1].strip()
    return summary, draft


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        print(f"[WEBHOOK] {data}")

        event = data.get("event", "")
        if event and "message:in" not in event and "in:new" not in event and event not in ("message", ""):
            return jsonify({"status": "ignored", "event": event}), 200

        msg = data.get("data", data)
        if msg.get("fromMe") or msg.get("from_me"):
            return jsonify({"status": "ignored", "reason": "own message"}), 200

        body = (
            msg.get("body")
            or msg.get("text")
            or msg.get("message")
            or (msg.get("content") or {}).get("text", "")
        )
        if not body:
            return jsonify({"status": "ignored", "reason": "no body"}), 200

        from_id = (
            msg.get("from")
            or msg.get("chatId")
            or (msg.get("chat") or {}).get("id", "unknown")
        )
        sender_name = (
            msg.get("fromName")
            or msg.get("senderName")
            or (msg.get("chat") or {}).get("name", from_id)
        )

        summary, draft = draft_reply_with_claude(sender_name, body)

        draft_id = str(uuid.uuid4())[:8]
        drafts = get_drafts()
        drafts[draft_id] = {
            "id": draft_id,
            "from": from_id,
            "sender_name": sender_name,
            "original_message": body,
            "summary": summary,
            "draft": draft,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_drafts(drafts)
        print(f"[NEW] {sender_name}: {body[:80]} → {draft_id}")
        return jsonify({"status": "ok", "draft_id": draft_id}), 200

    except Exception as e:
        print(f"[ERROR] {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/approve/<draft_id>", methods=["POST"])
def approve(draft_id):
    drafts = get_drafts()
    d = drafts.get(draft_id)
    if not d:
        return jsonify({"error": "not found"}), 404
    message = (request.get_json(silent=True) or {}).get("message", d["draft"])
    phone = d["from"].replace("@c.us", "").replace("@g.us", "").replace("+", "")
    try:
        resp = requests.post(
            WASSENGER_SEND_URL,
            json={"phone": phone, "message": message},
            headers={"Content-Type": "application/json", "Token": WASSENGER_API_KEY},
            timeout=15,
        )
        resp.raise_for_status()
        del drafts[draft_id]
        save_drafts(drafts)
        return jsonify({"status": "sent"}), 200
    except requests.HTTPError as e:
        return jsonify({"error": str(e), "response": resp.text}), 500


@app.route("/reject/<draft_id>", methods=["POST"])
def reject(draft_id):
    drafts = get_drafts()
    drafts.pop(draft_id, None)
    save_drafts(drafts)
    return jsonify({"status": "discarded"}), 200


@app.route("/drafts")
def drafts_route():
    return jsonify(list(get_drafts().values()))


@app.route("/test")
def test():
    msg = request.args.get("msg", "Hi, I'd like to rent a yacht for this Saturday. What are the prices?")
    sender = request.args.get("sender", "Test Customer")
    from_id = "886900000000@c.us"
    try:
        summary, draft = draft_reply_with_claude(sender, msg)
        draft_id = str(uuid.uuid4())[:8]
        drafts = get_drafts()
        drafts[draft_id] = {
            "id": draft_id,
            "from": from_id,
            "sender_name": sender,
            "original_message": msg,
            "summary": summary,
            "draft": draft,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_drafts(drafts)
        return jsonify({"status": "ok", "draft_id": draft_id, "go_to": "/"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WhatsApp Monitor</title>
<style>
  *{box-sizing:border-box}
  body{font-family:Arial,sans-serif;margin:0;background:#f0f2f5;padding:16px}
  h1{color:#075e54;margin:0 0 16px;font-size:22px}
  .badge{background:#25d366;color:#fff;border-radius:12px;padding:2px 10px;font-size:13px;margin-left:8px}
  .card{background:#fff;border-radius:10px;padding:18px;margin:12px 0;box-shadow:0 1px 4px rgba(0,0,0,.12)}
  .sender{font-size:17px;font-weight:bold;color:#075e54}
  .ts{float:right;font-size:12px;color:#aaa}
  .lbl{font-weight:bold;color:#555;margin:10px 0 4px;font-size:13px}
  .box{background:#f0f2f5;padding:12px;border-radius:6px;white-space:pre-wrap;font-size:14px}
  .sum{background:#e8f5e9;padding:12px;border-radius:6px;font-size:14px}
  textarea{width:100%;min-height:110px;padding:12px;border:1px solid #ddd;border-radius:6px;font-size:14px;resize:vertical}
  .btn{padding:10px 20px;border:none;border-radius:6px;cursor:pointer;font-size:14px;margin-right:8px;margin-top:10px}
  .send{background:#25d366;color:#fff}.send:hover{background:#1da851}
  .disc{background:#e53935;color:#fff}.disc:hover{background:#c62828}
  .empty{text-align:center;color:#aaa;padding:50px;font-size:16px}
  .toast{position:fixed;bottom:16px;right:16px;background:#333;color:#fff;padding:8px 16px;border-radius:6px;display:none;font-size:13px}
</style>
</head>
<body>
<h1>WhatsApp Monitor <span id="badge" class="badge">0</span></h1>
<div id="root"><div class="empty">Loading…</div></div>
<div id="toast" class="toast"></div>
<script>
function esc(t){const d=document.createElement('div');d.appendChild(document.createTextNode(t));return d.innerHTML}
function flash(m){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';setTimeout(()=>t.style.display='none',2500)}
function render(ds){
  document.getElementById('badge').textContent=ds.length;
  const r=document.getElementById('root');
  if(!ds.length){r.innerHTML='<div class="empty">No pending messages — waiting for WhatsApp…<br><small><a href="/test">Tap to send a test message</a></small></div>';return}
  r.innerHTML=ds.map(d=>`
    <div class="card" id="c${d.id}">
      <div><span class="sender">${esc(d.sender_name)}</span><span class="ts">${esc(d.timestamp)}</span></div>
      <div class="lbl">Incoming message</div>
      <div class="box">${esc(d.original_message)}</div>
      ${d.summary?`<div class="lbl">AI summary</div><div class="sum">${esc(d.summary)}</div>`:''}
      <div class="lbl">Draft reply</div>
      <textarea id="t${d.id}">${esc(d.draft)}</textarea>
      <div>
        <button class="btn send" onclick="send('${d.id}')">Send reply</button>
        <button class="btn disc" onclick="discard('${d.id}')">Discard</button>
      </div>
    </div>`).join('')
}
async function load(){const r=await fetch('/drafts');render(await r.json())}
async function send(id){
  const msg=document.getElementById('t'+id).value;
  const r=await fetch('/approve/'+id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
  const d=await r.json();
  if(d.status==='sent'){document.getElementById('c'+id).remove();flash('Sent!');load()}
  else flash('Error: '+JSON.stringify(d))
}
async function discard(id){
  await fetch('/reject/'+id,{method:'POST'});
  document.getElementById('c'+id).remove();load()
}
load();setInterval(load,4000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return HTML
