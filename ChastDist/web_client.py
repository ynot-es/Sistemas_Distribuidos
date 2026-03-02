"""
web_client.py — Cliente Web do Chat Distribuído
------------------------------------------------
Arquitetura simples e robusta:
- Flask recebe mensagens do browser via POST /send
- Envia ao servidor gRPC via chamada unária simples (SendMessage)
- Recebe broadcast do servidor via stream separado (ReceiveMessages)  
- Entrega ao browser via SSE (Server-Sent Events)
"""

import grpc
import threading
import time
import json

from flask import Flask, Response, request, jsonify, render_template_string

import chat_pb2
import chat_pb2_grpc

app = Flask(__name__)

# ─── Estado global ────────────────────────────────────────────────────────────
# Limpa ao iniciar — histórico começa vazio a cada execução do web_client
messages = []
messages_lock = threading.Lock()
new_msg_event = threading.Event()  # sinaliza quando chega msg nova

# ─── Conexão gRPC ─────────────────────────────────────────────────────────────
SERVER_ADDR = "localhost:50051"
channel = grpc.insecure_channel(SERVER_ADDR)
stub = chat_pb2_grpc.ChatServiceStub(channel)

# ─── Thread que recebe mensagens do servidor via stream ───────────────────────
def grpc_receive_loop():
    """
    Stream único com o servidor:
    - Recebe todas as mensagens (broadcast)
    - Envia mensagens da send_queue (injetadas pelo /send do Flask)
    """
    def generator():
        # Abre o stream imediatamente
        yield chat_pb2.ChatMessage(user="__web__", text="__join__", timestamp=int(time.time()))
        # Fica enviando mensagens da fila + mantém stream vivo
        while True:
            try:
                msg = send_queue.get(timeout=0.5)
                yield msg
            except:
                continue  # timeout, volta a esperar

    while True:
        try:
            print(f"[gRPC] Conectando ao servidor {SERVER_ADDR}...")
            for message in stub.ChatStream(generator()):
                if message.text in ("__join__", "__done__"):
                    continue
                with messages_lock:
                    messages.append({
                        "user": message.user,
                        "text": message.text,
                        "timestamp": message.timestamp
                    })
                new_msg_event.set()  # acorda todos os SSE listeners
                print(f"[gRPC] Recebido: [{message.user}]: {message.text}")
        except Exception as e:
            print(f"[gRPC] Stream perdido: {e}. Reconectando em 3s...")
            time.sleep(3)

# Inicia thread de recebimento
threading.Thread(target=grpc_receive_loop, daemon=True).start()

# ─── Rotas Flask ──────────────────────────────────────────────────────────────

@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return r


@app.route("/")
def index():
    return render_template_string(HTML)


# Fila de envio compartilhada — web_client injeta mensagens no stream global
send_queue = __import__('queue').Queue()

@app.route("/send", methods=["POST", "OPTIONS"])
def send():
    if request.method == "OPTIONS":
        return "", 204
    data     = request.get_json(force=True)
    text     = (data.get("text") or "").strip()
    username = (data.get("username") or "").strip()
    if not text or not username:
        return jsonify({"error": "bad request"}), 400

    # Injeta na fila — o stream global de recebimento vai enviar ao servidor
    send_queue.put(chat_pb2.ChatMessage(
        user=username,
        text=text,
        timestamp=int(time.time())
    ))
    print(f"[Flask] Enfileirado: [{username}]: {text}")
    return jsonify({"ok": True})


@app.route("/stream")
def stream():
    """SSE — browser se conecta aqui e recebe mensagens em tempo real."""
    def event_gen():
        # Envia histórico atual imediatamente
        with messages_lock:
            snapshot = list(messages)
        for msg in snapshot:
            yield f"data: {json.dumps(msg)}\n\n"

        # Fica escutando novas mensagens
        last = len(snapshot)
        while True:
            new_msg_event.wait(timeout=30)  # espera até 30s
            new_msg_event.clear()
            with messages_lock:
                current = list(messages)
            for msg in current[last:]:
                yield f"data: {json.dumps(msg)}\n\n"
            last = len(current)

    return Response(
        event_gen(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        }
    )


@app.route("/debug")
def debug():
    with messages_lock:
        return jsonify({"mensagens": len(messages), "ultimas": messages[-5:]})


# ─── HTML ─────────────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DistChat</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0a0a0f;--surface:#111118;--surface2:#1a1a24;--border:#2a2a3a;
  --accent:#7c6af7;--accent2:#f06292;--text:#e8e8f0;--muted:#6b6b82;
  --own:#1e1a3a;--other:#141420;--green:#4ade80;
}
body{font-family:'Syne',sans-serif;background:var(--bg);color:var(--text);height:100dvh;display:flex;flex-direction:column;overflow:hidden}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 60% 40% at 20% 10%,rgba(124,106,247,.08) 0%,transparent 60%),radial-gradient(ellipse 40% 50% at 80% 90%,rgba(240,98,146,.06) 0%,transparent 60%);pointer-events:none;z-index:0}

/* LOGIN */
#login{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;z-index:100;background:var(--bg);transition:opacity .4s,transform .4s}
#login.gone{opacity:0;transform:scale(.96);pointer-events:none}
.box{background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:48px 40px;width:100%;max-width:400px;text-align:center;position:relative;overflow:hidden}
.box::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--accent),var(--accent2))}
.logo{font-size:2.2rem;font-weight:800;letter-spacing:-1px;margin-bottom:6px}
.logo span{color:var(--accent)}
.sub{font-size:.75rem;color:var(--muted);font-family:'JetBrains Mono',monospace;margin-bottom:36px;letter-spacing:2px}
.box input{width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:14px 16px;color:var(--text);font-family:'Syne',sans-serif;font-size:1rem;outline:none;transition:border-color .2s;margin-bottom:14px;text-align:center}
.box input:focus{border-color:var(--accent)}
.box input::placeholder{color:var(--muted)}
.btn-enter{width:100%;padding:14px;background:linear-gradient(135deg,var(--accent),#9c8bf9);border:none;border-radius:10px;color:#fff;font-family:'Syne',sans-serif;font-size:1rem;font-weight:700;cursor:pointer;transition:transform .15s,opacity .15s}
.btn-enter:hover{transform:translateY(-2px)}
.btn-enter:active{transform:translateY(0)}

/* CHAT */
#chat{display:flex;flex-direction:column;height:100dvh;position:relative;z-index:1}
header{display:flex;align-items:center;justify-content:space-between;padding:16px 24px;border-bottom:1px solid var(--border);background:rgba(10,10,15,.8);backdrop-filter:blur(12px);flex-shrink:0}
.hl{display:flex;align-items:center;gap:12px}
.hlogo{font-size:1.3rem;font-weight:800;letter-spacing:-.5px}
.hlogo span{color:var(--accent)}
.dot{width:8px;height:8px;background:var(--green);border-radius:50%;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(74,222,128,.4)}50%{box-shadow:0 0 0 5px rgba(74,222,128,0)}}
.stxt{font-size:.75rem;color:var(--muted);font-family:'JetBrains Mono',monospace}
.badge{background:var(--surface2);border:1px solid var(--border);border-radius:20px;padding:6px 14px;font-size:.8rem;font-weight:600;color:var(--accent)}

#msgs{flex:1;overflow-y:auto;padding:24px;display:flex;flex-direction:column;gap:4px;scroll-behavior:smooth}
#msgs::-webkit-scrollbar{width:4px}
#msgs::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}

.msg{display:flex;flex-direction:column;max-width:68%;animation:in .2s ease}
@keyframes in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.msg.own{align-self:flex-end;align-items:flex-end}
.msg.other{align-self:flex-start;align-items:flex-start}
.meta{font-size:.7rem;color:var(--muted);margin-bottom:4px;font-family:'JetBrains Mono',monospace;padding:0 4px}
.msg.own .meta{color:var(--accent);opacity:.8}
.bubble{padding:10px 16px;border-radius:16px;font-size:.92rem;line-height:1.5;word-break:break-word}
.msg.own .bubble{background:var(--own);border:1px solid rgba(124,106,247,.3);border-bottom-right-radius:4px;color:#d4d0ff}
.msg.other .bubble{background:var(--other);border:1px solid var(--border);border-bottom-left-radius:4px}
.ts{font-size:.65rem;color:var(--muted);margin-top:3px;padding:0 4px;font-family:'JetBrains Mono',monospace}

.empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;color:var(--muted);gap:12px;padding-bottom:60px}
.empty-icon{font-size:2.5rem;opacity:.3}
.empty-txt{font-size:.85rem;font-family:'JetBrains Mono',monospace}

footer{padding:16px 24px 20px;border-top:1px solid var(--border);background:rgba(10,10,15,.8);backdrop-filter:blur(12px);flex-shrink:0}
.irow{display:flex;gap:10px;align-items:center;background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:6px 6px 6px 16px;transition:border-color .2s}
.irow:focus-within{border-color:var(--accent)}
#inp{flex:1;background:transparent;border:none;outline:none;color:var(--text);font-family:'Syne',sans-serif;font-size:.95rem;padding:8px 0}
#inp::placeholder{color:var(--muted)}
.bsend{background:linear-gradient(135deg,var(--accent),#9c8bf9);border:none;border-radius:10px;width:42px;height:42px;display:flex;align-items:center;justify-content:center;cursor:pointer;transition:transform .15s;flex-shrink:0}
.bsend:hover{transform:scale(1.08)}
.bsend:active{transform:scale(.96)}
.bsend svg{width:18px;height:18px;fill:white}
.hint{font-size:.68rem;color:var(--muted);text-align:center;margin-top:8px;font-family:'JetBrains Mono',monospace}
.hint span{color:var(--accent);opacity:.7}
</style>
</head>
<body>

<div id="login">
  <div class="box">
    <div class="logo">Dist<span>Chat</span></div>
    <div class="sub">SISTEMA DISTRIBUÍDO · gRPC</div>
    <input id="login-name" type="text" placeholder="Seu nome de usuário" maxlength="20" autocomplete="off">
    <button class="btn-enter" id="btn-entrar">Entrar no Chat →</button>
  </div>
</div>

<div id="chat">
  <header>
    <div class="hl">
      <div class="hlogo">Dist<span>Chat</span></div>
      <div class="dot"></div>
      <div class="stxt" id="status">CONECTANDO...</div>
    </div>
    <div class="badge" id="badge">—</div>
  </header>
  <div id="msgs">
    <div class="empty" id="empty">
      <div class="empty-icon">💬</div>
      <div class="empty-txt">nenhuma mensagem ainda...</div>
    </div>
  </div>
  <footer>
    <div class="irow">
      <input id="inp" type="text" placeholder="Escreva uma mensagem..." autocomplete="off">
      <button class="bsend" id="btn-send">
        <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
      </button>
    </div>
    <div class="hint">Pressione <span>Enter</span> para enviar</div>
  </footer>
</div>

<script>
let me = null;

// LOGIN
function doLogin() {
  const name = document.getElementById('login-name').value.trim();
  if (!name) return;
  me = name;
  document.getElementById('badge').textContent = name;
  document.getElementById('login').classList.add('gone');
  connectSSE();
}

document.getElementById('btn-entrar').onclick = doLogin;
document.getElementById('login-name').addEventListener('keydown', e => {
  if (e.key === 'Enter') doLogin();
});

// SSE — recebe mensagens em tempo real
function connectSSE() {
  document.getElementById('status').textContent = 'CONECTANDO...';
  const es = new EventSource('/stream');

  es.onopen = () => {
    document.getElementById('status').textContent = 'CONECTADO · gRPC';
  };

  es.onmessage = e => {
    const msg = JSON.parse(e.data);
    addMessage(msg);
  };

  es.onerror = () => {
    document.getElementById('status').textContent = 'RECONECTANDO...';
    es.close();
    setTimeout(connectSSE, 2000);
  };
}

// Renderiza uma mensagem nova (incremental — não re-renderiza tudo)
function addMessage(m) {
  const container = document.getElementById('msgs');
  document.getElementById('empty').style.display = 'none';

  const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 80;

  const wrap = document.createElement('div');
  wrap.className = 'msg ' + (m.user === me ? 'own' : 'other');

  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = m.user === me ? 'Você' : m.user;

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = m.text;

  const ts = document.createElement('div');
  ts.className = 'ts';
  const d = new Date(m.timestamp * 1000);
  ts.textContent = d.toLocaleTimeString('pt-BR', {hour:'2-digit', minute:'2-digit'});

  wrap.appendChild(meta);
  wrap.appendChild(bubble);
  wrap.appendChild(ts);
  container.appendChild(wrap);

  if (atBottom) container.scrollTop = container.scrollHeight;
}

// ENVIO
function sendMsg() {
  const inp = document.getElementById('inp');
  const text = inp.value.trim();
  if (!text || !me) return;
  inp.value = '';

  fetch('/send', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text, username: me})
  }).catch(e => console.error('Erro ao enviar:', e));
}

document.getElementById('btn-send').onclick = sendMsg;
document.getElementById('inp').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
});
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("=" * 50)
    print("  DistChat Web Client")
    print("  Acesse: http://localhost:8080")
    print("=" * 50)
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
