"""
server.py — Servidor de Chat Distribuído
----------------------------------------
Características implementadas:
  - gRPC bidirecional (Comunicação/Middleware).
  - Multithreaded (Concorrência).
  - SQLite para histórico (Camada de Persistência).
  - Lock distribuído no broadcast (Sincronização).
  - Se registra no Name Server ao iniciar (Descoberta de Serviços).
  - Circuit Breaker no registro (Tolerância a Falhas).
"""

"""
    - O ChatService.ChatStream é um stream bidirecional gRPC. 
    Todo cliente (CLI e web) se comunica exclusivamente via gRPC — 
    nenhum socket é manipulado diretamente.
"""

"""
    - Concorrência: pythonserver = grpc.server(futures.ThreadPoolExecutor(max_workers=20)). Cada cliente 
    conectado ganha uma thread separada via threading.Thread(target=receive, daemon=True).start().
    O servidor atende até 20 clientes simultaneamente.
"""

"""
    - Tolerância a Falhas: Circuit Breaker em server.py (classe CircuitBreaker):Estado CLOSED → OPEN → HALF_OPEN.
    Após 3 falhas consecutivas abrindo o circuito, bloqueia chamadas por 15s antes de tentar novamente.
"""

import grpc
from concurrent import futures
import time
import threading
import sqlite3
import os

import chat_pb2
import chat_pb2_grpc

# ─── Configuração ─────────────────────────────────────────────────────────────
SERVER_PORT    = 50051
NAME_SERVER    = "localhost:50052"
DB_PATH        = "chat_history.db"
MY_ADDRESS     = f"localhost:{SERVER_PORT}"  # troque pelo IP real em produção


# ─── Banco de Dados (Camada de Persistência) ──────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    # Garante schema correto — recria se necessário
    conn.execute("DROP TABLE IF EXISTS messages")
    conn.execute("""
        CREATE TABLE messages (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user      TEXT NOT NULL,
            text      TEXT NOT NULL,
            timestamp INTEGER NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    print("[DB] Banco de dados inicializado.")

# Salva mensagem no banco (chamado pelo ChatService).
def save_message(user: str, text: str, timestamp: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO messages (user, text, timestamp) VALUES (?, ?, ?)",
        (user, text, timestamp)
    )
    conn.commit()
    conn.close()

# Carrega histórico do banco (chamado ao conectar um cliente).
def load_history(limit: int = 50):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT user, text, timestamp FROM messages ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return list(reversed(rows))


# ─── Circuit Breaker simples ───────────────────────────────────────────────────
class CircuitBreaker:
    """
    3 estados: CLOSED (normal) -> OPEN (falhou, bloqueia) -> HALF_OPEN (testando)
    """

    # Configurações: número máximo de falhas antes de abrir, tempo para resetar, etc.
    def __init__(self, max_failures=3, reset_timeout=10):
        self.max_failures  = max_failures
        self.reset_timeout = reset_timeout
        self.failures      = 0
        self.state         = "CLOSED"
        self.opened_at     = None
        self._lock         = threading.Lock()

    # Método para chamar uma função protegida pelo Circuit Breaker.
    def call(self, func, *args, **kwargs):
        with self._lock:
            if self.state == "OPEN":
                elapsed = time.time() - self.opened_at
                if elapsed >= self.reset_timeout:
                    self.state = "HALF_OPEN"
                    print("[CircuitBreaker] → HALF_OPEN, testando...")
                else:
                    raise Exception(f"[CircuitBreaker] OPEN — aguarde {self.reset_timeout - elapsed:.0f}s")
        # Setado HALF_OPEN: tenta a função, se falhar volta para OPEN, se passar vai para CLOSED.
        try:
            result = func(*args, **kwargs)
            with self._lock:
                self.failures = 0
                self.state    = "CLOSED"
            return result
        except Exception as e:
            with self._lock:
                self.failures += 1
                print(f"[CircuitBreaker] Falha {self.failures}/{self.max_failures}: {e}")
                if self.failures >= self.max_failures:
                    self.state     = "OPEN"
                    self.opened_at = time.time()
                    print("[CircuitBreaker] → OPEN")
            raise


# ─── Registro no Name Server ──────────────────────────────────────────────────
_ns_breaker = CircuitBreaker(max_failures=3, reset_timeout=15)

def register_with_name_server():
    def _do_register():
        channel = grpc.insecure_channel(NAME_SERVER)
        stub    = chat_pb2_grpc.NameServiceStub(channel)
        resp    = stub.Register(
            chat_pb2.RegisterRequest(service_name="chat", address=MY_ADDRESS),
            timeout=3
        )
        if resp.success:
            print(f"[NameServer] Registrado como 'chat' em {MY_ADDRESS}")
        else:
            raise Exception("Name Server recusou o registro")

    def _retry_loop():
        while True:
            try:
                _ns_breaker.call(_do_register)
                return  # sucesso — para o loop
            except Exception as e:
                print(f"[NameServer] {e} — tentando novamente em 5s...")
                time.sleep(5)

    threading.Thread(target=_retry_loop, daemon=True).start()


# ─── Serviço de Chat ──────────────────────────────────────────────────────────
class ChatService(chat_pb2_grpc.ChatServiceServicer):

    # Lock para proteger acesso à lista de clientes (broadcast).
    def __init__(self):
        self.clients = []       # lista de filas, uma por cliente conectado.
        self.lock    = threading.Lock()

    # Método gRPC para stream bidirecional de mensagens.
    def ChatStream(self, request_iterator, context):
        """Stream bidirecional: recebe e distribui mensagens."""

        client_queue = []

        with self.lock:
            self.clients.append(client_queue)

        print("[Server] Novo cliente conectado")

        # Envia histórico apenas para clientes reais (não para o receiver interno do web_client).
        # O web_client usa user="__web__" para sua thread interna de recebimento.
        first_msg_user = None
        for row in load_history():
            user, text, ts = row
            client_queue.append(chat_pb2.ChatMessage(user=user, text=text, timestamp=ts))

        # Thread que escuta mensagens vindas deste cliente.
        def receive():
            try:
                for message in request_iterator:
                    # Ignora mensagens de controle do web_client.
                    if message.text in ("__join__", "__done__"):
                        if message.text == "__join__" and message.user != "__web__":
                            print(f"[Server] '{message.user}' entrou no chat")
                        continue
                    print(f"[{message.user}]: {message.text}")
                    try:
                        save_message(message.user, message.text, message.timestamp)
                    except Exception as db_err:
                        print(f"[DB] Erro ao salvar mensagem: {db_err}")
                    self._broadcast(message)
            except Exception as e:
                print(f"[Server] Erro no receive: {e}")
            finally:
                with self.lock:
                    if client_queue in self.clients:
                        self.clients.remove(client_queue)
                print("[Server] Cliente desconectado")

        threading.Thread(target=receive, daemon=True).start()

        # Yield de mensagens da fila deste cliente.
        while context.is_active():
            if client_queue:
                yield client_queue.pop(0)
            else:
                time.sleep(0.05)

    """
    - Sincronização: O threading.Lock() garante que dois clientes não escrevem simultaneamente na 
    lista compartilhada de filas — evitando condição de corrida.
    """

    def _broadcast(self, message):
        """Distribui mensagem para todos os clientes conectados (com lock)."""
        with self.lock:
            for client in self.clients:
                client.append(message)


# ─── Main ──────────────────────────────────────────────────────────────────────
def serve():
    init_db()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=20))
    chat_pb2_grpc.add_ChatServiceServicer_to_server(ChatService(), server)
    server.add_insecure_port(f"[::]:{SERVER_PORT}")
    server.start()

    print(f"[Server] Servidor de chat rodando na porta {SERVER_PORT}")

    # Tenta registrar no Name Server (com Circuit Breaker + retry).
    register_with_name_server()

    server.wait_for_termination()


if __name__ == "__main__":
    serve()