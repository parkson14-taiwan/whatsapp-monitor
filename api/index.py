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
CONV_KEY = "whatsapp:triage:conversations"

# ── Redis ─────────────────────────────────────────────────────────────────────
try:
    from upstash_redis import Redis as UpstashRedis
    _redis = UpstashRedis(
        url=os.environ["UPSTASH_REDIS_REST_URL"],
        token=os.environ["UPSTASH_REDIS_REST_TOKEN"],
    )
    USE_REDIS = True
except Exception:
    USE_REDIS = False
    _local: dict = {}


def get_conversations() -> dict:
    if USE_REDIS:
        try:
            raw = _redis.get(CONV_KEY)
            if raw:
                return json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            print(f"[Redis get] {e}")
        return {}
    return _local.copy()


def save_conversations(convs: dict) -> None:
    if USE_REDIS:
        try:
            _redis.set(CONV_KEY, json.dumps(convs, ensure_ascii=False))
        except Exception as e:
            print(f"[Redis set] {e}")
    else:
        global _local
        _local = convs


def find_conv_by_chat_id(convs: dict, chat_id: str):
    for cid, conv in convs.items():
        if conv.get("chat_id") == chat_id:
            return cid, conv
    return None, None


# ── Claude ────────────────────────────────────────────────────────────────────
def analyze_with_claude(contact_name: str, messages: list) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"category": "other", "summary": "API key not set", "draft": "", "why": ""}

    client = anthropic.Anthropic(api_key=api_key)
    history = "\n".join([
        f"{'客戶' if m['role'] == 'customer' else '客服'}: {m['text']}"
        for m in messages[-10:]
    ])

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="""你是台灣一間遊艇租賃公司的客服助理。
分析對話後，用以下格式回答（不要有其他文字）：

CATEGORY: [inquiry/booking/payment/other]
SUMMARY: [1-2句話總結客戶需求，用中文]
DRAFT:
[用客戶相同語言撰寫的禮貌回覆]
WHY: [一句話說明建議的動作，用中文]""",
        messages=[{"role": "user", "content": f"聯絡人：{contact_name}\n\n對話記錄：\n{history}"}],
    )

    text = response.content[0].text
    result = {"category": "other", "summary": "", "draft": "", "why": ""}
    try:
        current = None
        draft_lines = []
        for line in text.strip().split("\n"):
            if line.startswith("CATEGORY:"):
                cat = line.replace("CATEGORY:", "").strip().lower()
                if cat in ("inquiry", "booking", "payment", "other"):
                    result["category"] = cat
            elif line.startswith("SUMMARY:"):
                result["summary"] = line.replace("SUMMARY:", "").strip()
            elif line.startswith("DRAFT:"):
                current = "draft"
            elif line.startswith("WHY:"):
                result["draft"] = "\n".join(draft_lines).strip()
                result["why"] = line.replace("WHY:", "").strip()
                current = None
                draft_lines = []
            elif current == "draft":
                draft_lines.append(line)
        if draft_lines and not result["draft"]:
            result["draft"] = "\n".join(draft_lines).strip()
    except Exception as e:
        print(f"[Parse] {e}")
    return result


# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True, silent=True) or {}
        event = data.get("event", "")
        if event and "message:in" not in event and "in:new" not in event and event not in ("message", ""):
            return jsonify({"status": "ignored"}), 200

        msg = data.get("data", data)
        if msg.get("fromMe") or msg.get("from_me"):
            return jsonify({"status": "ignored"}), 200

        body = msg.get("body") or msg.get("text") or msg.get("message") or ""
        if not body:
            return jsonify({"status": "ignored"}), 200

        chat_id = msg.get("from") or msg.get("chatId") or (msg.get("chat") or {}).get("id", "unknown")
        sender_name = msg.get("fromName") or msg.get("senderName") or (msg.get("chat") or {}).get("name", chat_id)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        convs = get_conversations()
        cid, conv = find_conv_by_chat_id(convs, chat_id)

        if conv is None:
            cid = str(uuid.uuid4())[:8]
            conv = {
                "id": cid,
                "chat_id": chat_id,
                "name": sender_name,
                "phone": chat_id.replace("@c.us", "").replace("@g.us", ""),
                "status": "waiting",
                "category": "other",
                "messages": [],
                "ai_summary": "",
                "draft_reply": "",
                "why_action": "",
                "last_updated": timestamp,
                "unread": True,
            }
            convs[cid] = conv

        conv["messages"].append({"role": "customer", "text": body, "timestamp": timestamp})
        conv["last_updated"] = timestamp
        conv["status"] = "waiting"
        conv["unread"] = True
        conv["name"] = sender_name

        ai = analyze_with_claude(sender_name, conv["messages"])
        conv["category"] = ai["category"]
        conv["ai_summary"] = ai["summary"]
        conv["draft_reply"] = ai["draft"]
        conv["why_action"] = ai["why"]

        save_conversations(convs)
        print(f"[NEW] {sender_name}: {body[:60]}")
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"[ERROR] {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ── API ───────────────────────────────────────────────────────────────────────
@app.route("/api/conversations")
def api_list():
    convs = get_conversations()
    result = sorted(convs.values(), key=lambda x: x.get("last_updated", ""), reverse=True)
    return jsonify(result)


@app.route("/api/conversation/<cid>")
def api_get(cid):
    convs = get_conversations()
    conv = convs.get(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    conv["unread"] = False
    save_conversations(convs)
    return jsonify(conv)


@app.route("/api/conversation/<cid>/update", methods=["POST"])
def api_update(cid):
    convs = get_conversations()
    conv = convs.get(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404
    payload = request.get_json(silent=True) or {}
    for field in ("status", "category", "draft_reply"):
        if field in payload:
            conv[field] = payload[field]
    save_conversations(convs)
    return jsonify({"status": "ok"})


@app.route("/api/conversation/<cid>/send", methods=["POST"])
def api_send(cid):
    convs = get_conversations()
    conv = convs.get(cid)
    if not conv:
        return jsonify({"error": "not found"}), 404

    message = (request.get_json(silent=True) or {}).get("message", conv.get("draft_reply", ""))
    phone = conv["phone"]

    try:
        resp = requests.post(
            WASSENGER_SEND_URL,
            json={"phone": phone, "message": message},
            headers={"Content-Type": "application/json", "Token": WASSENGER_API_KEY},
            timeout=15,
        )
        resp.raise_for_status()
        conv["messages"].append({
            "role": "admin", "text": message,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        conv["draft_reply"] = ""
        save_conversations(convs)
        return jsonify({"status": "sent"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/test")
def test():
    msgs = [
        ("Test Customer", "Hi, I'd like to rent a yacht for this Saturday. What are the prices?"),
        ("王小明", "你好，我想預訂下週六的遊艇，大概10個人，請問有什麼選擇？"),
        ("陳美麗", "請問我上次的訂金什麼時候可以退款？"),
    ]
    idx = int(request.args.get("i", 0)) % len(msgs)
    sender, body = msgs[idx]
    chat_id = f"test_{uuid.uuid4().hex[:6]}@c.us"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    convs = get_conversations()
    cid = str(uuid.uuid4())[:8]
    conv = {
        "id": cid, "chat_id": chat_id, "name": sender,
        "phone": "0900000000", "status": "waiting", "category": "other",
        "messages": [{"role": "customer", "text": body, "timestamp": timestamp}],
        "ai_summary": "", "draft_reply": "", "why_action": "",
        "last_updated": timestamp, "unread": True,
    }
    ai = analyze_with_claude(sender, conv["messages"])
    conv.update({"category": ai["category"], "ai_summary": ai["summary"],
                 "draft_reply": ai["draft"], "why_action": ai["why"]})
    convs[cid] = conv
    save_conversations(convs)
    return jsonify({"status": "ok", "id": cid, "go_to": "/"})


# ── Frontend ──────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WhatsApp Triage</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f6fa;height:100vh;overflow:hidden}
.app{display:grid;grid-template-columns:300px 1fr 320px;height:100vh}
/* ── Sidebar ── */
.sidebar{background:#fff;border-right:1px solid #e5e7eb;display:flex;flex-direction:column;overflow:hidden}
.sidebar-header{padding:14px 16px;border-bottom:1px solid #e5e7eb}
.sidebar-title{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.sidebar-title h1{font-size:15px;font-weight:700;color:#111}
.chat-count{background:#25d366;color:#fff;border-radius:10px;padding:1px 8px;font-size:11px}
.sync-bar{display:flex;align-items:center;gap:8px;font-size:11px;color:#6b7280}
.sync-btn{background:#f3f4f6;border:1px solid #d1d5db;border-radius:5px;padding:3px 10px;font-size:11px;cursor:pointer}
.sync-btn:hover{background:#e5e7eb}
.filter-bar{display:flex;gap:6px;margin-top:10px}
.filter-bar select{flex:1;padding:5px 8px;border:1px solid #d1d5db;border-radius:6px;font-size:12px;background:#fff;cursor:pointer}
.conv-list{flex:1;overflow-y:auto}
.conv-item{padding:12px 16px;border-bottom:1px solid #f3f4f6;cursor:pointer;transition:background .15s}
.conv-item:hover{background:#f9fafb}
.conv-item.active{background:#eff6ff;border-left:3px solid #3b82f6}
.conv-item.unread .conv-name{font-weight:700}
.conv-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}
.conv-name{font-size:13px;color:#111;font-weight:500}
.conv-time{font-size:11px;color:#9ca3af}
.conv-preview{font-size:12px;color:#6b7280;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:5px}
.tags{display:flex;gap:4px;flex-wrap:wrap}
.tag{font-size:10px;padding:1px 7px;border-radius:10px;font-weight:500}
.tag-waiting{background:#fef3c7;color:#92400e}
.tag-done{background:#d1fae5;color:#065f46}
.tag-escalated{background:#fee2e2;color:#991b1b}
.tag-inquiry{background:#dbeafe;color:#1e40af}
.tag-booking{background:#ede9fe;color:#5b21b6}
.tag-payment{background:#d1fae5;color:#065f46}
.tag-other{background:#f3f4f6;color:#374151}
.unread-dot{width:8px;height:8px;background:#3b82f6;border-radius:50%;flex-shrink:0}
/* ── Thread ── */
.thread-pane{display:flex;flex-direction:column;background:#f5f6fa;overflow:hidden}
.thread-header{padding:12px 20px;background:#fff;border-bottom:1px solid #e5e7eb;display:flex;align-items:center;justify-content:space-between}
.contact-name{font-size:16px;font-weight:700;color:#111}
.contact-phone{font-size:12px;color:#6b7280;margin-top:2px}
.header-selects{display:flex;gap:8px}
.header-selects select{padding:4px 10px;border:1px solid #d1d5db;border-radius:6px;font-size:12px;background:#fff;cursor:pointer}
.messages{flex:1;overflow-y:auto;padding:16px 20px;display:flex;flex-direction:column;gap:8px}
.msg{max-width:75%;padding:10px 14px;border-radius:12px;font-size:13px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.msg-customer{background:#fff;border:1px solid #e5e7eb;align-self:flex-start;border-bottom-left-radius:4px}
.msg-admin{background:#dcf8c6;align-self:flex-end;border-bottom-right-radius:4px}
.msg-time{font-size:10px;color:#9ca3af;margin-top:3px}
.empty-thread{flex:1;display:flex;align-items:center;justify-content:center;color:#9ca3af;font-size:14px;flex-direction:column;gap:8px}
.empty-thread .icon{font-size:40px}
/* ── Right Panel ── */
.ai-panel{background:#fff;border-left:1px solid #e5e7eb;display:flex;flex-direction:column;overflow:hidden}
.panel-header{padding:14px 16px;border-bottom:1px solid #e5e7eb}
.panel-header h3{font-size:14px;font-weight:700;color:#111}
.draft-label{font-size:10px;color:#6b7280;background:#f3f4f6;padding:2px 8px;border-radius:4px;margin-top:4px;display:inline-block}
.panel-body{flex:1;overflow-y:auto;padding:14px 16px}
.panel-section{margin-bottom:16px}
.panel-section h4{font-size:11px;font-weight:700;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}
.panel-section p{font-size:13px;color:#374151;line-height:1.5}
.draft-textarea{width:100%;min-height:100px;padding:10px;border:1px solid #d1d5db;border-radius:6px;font-size:13px;resize:vertical;font-family:inherit;line-height:1.5}
.draft-textarea:focus{outline:none;border-color:#3b82f6}
.action-btns{display:flex;flex-direction:column;gap:8px;padding:14px 16px;border-top:1px solid #e5e7eb}
.btn{padding:9px 14px;border:none;border-radius:6px;font-size:13px;font-weight:500;cursor:pointer;transition:background .15s;width:100%;text-align:center}
.btn-copy{background:#f3f4f6;color:#374151;border:1px solid #d1d5db}
.btn-copy:hover{background:#e5e7eb}
.btn-send{background:#25d366;color:#fff}
.btn-send:hover{background:#1da851}
.btn-waiting{background:#fef3c7;color:#92400e;border:1px solid #fcd34d}
.btn-waiting:hover{background:#fde68a}
.btn-done{background:#d1fae5;color:#065f46;border:1px solid #6ee7b7}
.btn-done:hover{background:#a7f3d0}
.btn-escalate{background:#fee2e2;color:#991b1b;border:1px solid #fca5a5}
.btn-escalate:hover{background:#fecaca}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#1f2937;color:#fff;padding:10px 20px;border-radius:8px;font-size:13px;display:none;z-index:999}
/* ── Empty state when nothing selected ── */
.no-selection{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:#9ca3af;gap:10px}
.no-selection .icon{font-size:48px}
/* ── Mobile ── */
@media(max-width:768px){
  .app{grid-template-columns:1fr}
  .thread-pane,.ai-panel{display:none}
  .thread-pane.mobile-show,.ai-panel.mobile-show{display:flex}
  .sidebar.mobile-hide{display:none}
}
</style>
</head>
<body>
<div class="app">
  <!-- Sidebar -->
  <div class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <div class="sidebar-title">
        <h1>WhatsApp Triage</h1>
        <span class="chat-count" id="chat-count">0</span>
      </div>
      <div class="sync-bar">
        <span id="last-sync">正在載入...</span>
        <button class="sync-btn" onclick="loadConversations()">重新整理</button>
      </div>
      <div class="filter-bar">
        <select id="filter-status" onchange="renderList()">
          <option value="">全部狀態</option>
          <option value="waiting">Waiting</option>
          <option value="done">Done</option>
          <option value="escalated">Escalated</option>
        </select>
        <select id="filter-category" onchange="renderList()">
          <option value="">全部分類</option>
          <option value="inquiry">詢價</option>
          <option value="booking">預訂</option>
          <option value="payment">付款</option>
          <option value="other">其他</option>
        </select>
      </div>
    </div>
    <div class="conv-list" id="conv-list"></div>
  </div>

  <!-- Thread -->
  <div class="thread-pane" id="thread-pane">
    <div class="no-selection" id="no-selection">
      <div class="icon">💬</div>
      <div>選擇一個對話開始</div>
      <small><a href="/test" target="_blank">新增測試訊息</a></small>
    </div>
    <div id="conv-detail" style="display:none;flex-direction:column;height:100%">
      <div class="thread-header">
        <div>
          <div class="contact-name" id="detail-name"></div>
          <div class="contact-phone" id="detail-phone"></div>
        </div>
        <div class="header-selects">
          <select id="sel-status" onchange="updateField('status',this.value)">
            <option value="waiting">Waiting</option>
            <option value="done">Done</option>
            <option value="escalated">Escalated</option>
          </select>
          <select id="sel-category" onchange="updateField('category',this.value)">
            <option value="inquiry">詢價</option>
            <option value="booking">預訂</option>
            <option value="payment">付款</option>
            <option value="other">其他</option>
          </select>
        </div>
      </div>
      <div class="messages" id="messages"></div>
    </div>
  </div>

  <!-- AI Panel -->
  <div class="ai-panel" id="ai-panel">
    <div class="no-selection" id="no-ai">
      <div class="icon">🤖</div>
      <div>AI 分析將顯示於此</div>
    </div>
    <div id="ai-detail" style="display:none;flex-direction:column;height:100%">
      <div class="panel-header">
        <h3>Suggested Reply</h3>
        <span class="draft-label">COPY-ONLY DRAFT</span>
      </div>
      <div class="panel-body" style="flex:1;overflow-y:auto">
        <div class="panel-section">
          <textarea class="draft-textarea" id="draft-text" placeholder="草稿回覆..."></textarea>
        </div>
        <div class="panel-section" id="summary-section">
          <h4>Summary</h4>
          <p id="ai-summary">—</p>
        </div>
        <div class="panel-section" id="why-section">
          <h4>Why this action</h4>
          <p id="ai-why">—</p>
        </div>
      </div>
      <div class="action-btns">
        <button class="btn btn-copy" onclick="copyDraft()">📋 Copy draft</button>
        <button class="btn btn-send" onclick="sendReply()">✈️ Send reply</button>
        <div style="display:flex;gap:8px">
          <button class="btn btn-waiting" style="flex:1" onclick="setStatus('waiting')">Mark waiting</button>
          <button class="btn btn-done" style="flex:1" onclick="setStatus('done')">Mark done</button>
        </div>
        <button class="btn btn-escalate" onclick="setStatus('escalated')">🚨 Escalate</button>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let allConvs = [];
let selectedId = null;

const CATEGORY_LABELS = {inquiry:'詢價', booking:'預訂', payment:'付款', other:'其他'};
const STATUS_LABELS   = {waiting:'Waiting', done:'Done', escalated:'Escalated'};

function flash(msg, dur=2000){
  const t=document.getElementById('toast');
  t.textContent=msg; t.style.display='block';
  setTimeout(()=>t.style.display='none', dur);
}

function esc(s){ const d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }

function timeFmt(ts){
  if(!ts) return '';
  const d=new Date(ts.replace(' ','T'));
  const now=new Date();
  const diff=now-d;
  if(diff<60000) return '剛剛';
  if(diff<3600000) return Math.floor(diff/60000)+'分鐘前';
  if(diff<86400000) return d.toLocaleTimeString('zh-TW',{hour:'2-digit',minute:'2-digit'});
  return d.toLocaleDateString('zh-TW',{month:'numeric',day:'numeric'});
}

async function loadConversations(){
  try {
    const r = await fetch('/api/conversations');
    allConvs = await r.json();
    document.getElementById('last-sync').textContent = '上次同步 ' + new Date().toLocaleTimeString('zh-TW',{hour:'2-digit',minute:'2-digit'});
    renderList();
    if(selectedId) {
      const conv = allConvs.find(c=>c.id===selectedId);
      if(conv) renderDetail(conv);
    }
  } catch(e) { console.error(e); }
}

function renderList(){
  const statusF = document.getElementById('filter-status').value;
  const catF    = document.getElementById('filter-category').value;
  const filtered = allConvs.filter(c =>
    (!statusF || c.status===statusF) && (!catF || c.category===catF)
  );
  document.getElementById('chat-count').textContent = filtered.length;
  const waitingCount = allConvs.filter(c=>c.status==='waiting').length;
  document.getElementById('chat-count').style.background = waitingCount > 0 ? '#ef4444' : '#25d366';

  const html = filtered.map(c => `
    <div class="conv-item${c.id===selectedId?' active':''}${c.unread?' unread':''}" onclick="selectConv('${c.id}')">
      <div class="conv-top">
        <span class="conv-name">${esc(c.name)}</span>
        <div style="display:flex;align-items:center;gap:4px">
          ${c.unread?'<div class="unread-dot"></div>':''}
          <span class="conv-time">${timeFmt(c.last_updated)}</span>
        </div>
      </div>
      <div class="conv-preview">${esc((c.messages||[]).slice(-1)[0]?.text||'')}</div>
      <div class="tags">
        <span class="tag tag-${c.status}">${STATUS_LABELS[c.status]||c.status}</span>
        <span class="tag tag-${c.category}">${CATEGORY_LABELS[c.category]||c.category}</span>
      </div>
    </div>
  `).join('');
  document.getElementById('conv-list').innerHTML = html || '<div style="padding:20px;text-align:center;color:#9ca3af;font-size:13px">沒有對話</div>';
}

async function selectConv(id){
  selectedId = id;
  renderList();
  try {
    const r = await fetch('/api/conversation/'+id);
    const conv = await r.json();
    const idx = allConvs.findIndex(c=>c.id===id);
    if(idx>=0){ allConvs[idx]=conv; }
    renderDetail(conv);
  } catch(e){ console.error(e); }
}

function renderDetail(conv){
  document.getElementById('no-selection').style.display='none';
  document.getElementById('no-ai').style.display='none';

  const det = document.getElementById('conv-detail');
  const aid = document.getElementById('ai-detail');
  det.style.display='flex';
  aid.style.display='flex';

  document.getElementById('detail-name').textContent = conv.name;
  document.getElementById('detail-phone').textContent = conv.phone ? '+'+conv.phone : '';
  document.getElementById('sel-status').value   = conv.status || 'waiting';
  document.getElementById('sel-category').value = conv.category || 'other';
  document.getElementById('draft-text').value   = conv.draft_reply || '';
  document.getElementById('ai-summary').textContent = conv.ai_summary || '—';
  document.getElementById('ai-why').textContent     = conv.why_action || '—';

  const msgs = (conv.messages||[]).map(m=>`
    <div>
      <div class="msg msg-${m.role}">${esc(m.text)}</div>
      <div class="msg-time" style="text-align:${m.role==='admin'?'right':'left'}">${m.timestamp||''}</div>
    </div>
  `).join('');
  const mBox = document.getElementById('messages');
  mBox.innerHTML = msgs;
  mBox.scrollTop = mBox.scrollHeight;
}

async function updateField(field, value){
  if(!selectedId) return;
  await fetch('/api/conversation/'+selectedId+'/update', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({[field]: value})
  });
  const idx = allConvs.findIndex(c=>c.id===selectedId);
  if(idx>=0) allConvs[idx][field]=value;
  renderList();
}

async function setStatus(s){
  await updateField('status', s);
  document.getElementById('sel-status').value=s;
  flash('狀態已更新：'+STATUS_LABELS[s]);
}

async function sendReply(){
  if(!selectedId) return;
  const msg = document.getElementById('draft-text').value;
  if(!msg.trim()){ flash('草稿是空的'); return; }
  const r = await fetch('/api/conversation/'+selectedId+'/send', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({message: msg})
  });
  const d = await r.json();
  if(d.status==='sent'){
    flash('✅ 已發送！');
    document.getElementById('draft-text').value='';
    selectConv(selectedId);
  } else {
    flash('❌ 發送失敗：'+JSON.stringify(d));
  }
}

function copyDraft(){
  const t = document.getElementById('draft-text');
  if(!t.value.trim()){ flash('草稿是空的'); return; }
  navigator.clipboard.writeText(t.value).then(()=>flash('✅ 已複製到剪貼簿！'));
}

loadConversations();
setInterval(loadConversations, 5000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return HTML
