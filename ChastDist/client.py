"""
client.py — Cliente CLI de Chat Distribuído
-------------------------------------------
Características implementadas:
  - Descoberta de serviço via Name Server (transparência de localização).
  - Retry automático com backoff exponencial (Tolerância a Falhas).
  - gRPC stream bidirecional (sem gerenciamento manual de sockets).
"""

"""
    - Em client.py (stub = chat_pb2_grpc.ChatServiceStub(channel)) 
    e web_client.py. O cliente só chama stub.ChatStream(...). Toda a serialização,
    transporte e gerenciamento de conexão é feito pelo gRPC — o código da aplicação 
    nunca toca em socket.
"""

"""
    - Retry com backoff exponencial em client.py (função discover_with_retry): 
    Tenta 5 vezes com espera de 2, 4, 8, 16, 32 segundos entre tentativas.
"""

import grpc
import threading
import time

import chat_pb2
import chat_pb2_grpc

NAME_SERVER  = "localhost:50052"
MAX_RETRIES  = 5


# ─── Descoberta de Serviço ────────────────────────────────────────────────────
def discover_server() -> str:
    """Consulta o Name Server para obter o endereço do servidor de chat."""
    print(f"[NameServer] Consultando {NAME_SERVER}...")
    channel = grpc.insecure_channel(NAME_SERVER)
    stub    = chat_pb2_grpc.NameServiceStub(channel)
    resp    = stub.GetServer(
        chat_pb2.ServerRequest(service_name="chat"),
        timeout=3
    )
    if resp.found:
        print(f"[NameServer] Servidor encontrado: {resp.address}")
        return resp.address
    raise Exception("Serviço 'chat' não registrado no Name Server")


def discover_with_retry() -> str:
    """Retry com backoff exponencial para encontrar o servidor."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return discover_server()
        except Exception as e:
            wait = 2 ** attempt  # 2, 4, 8, 16, 32 segundos
            print(f"[Retry {attempt}/{MAX_RETRIES}] Falha: {e}. Tentando em {wait}s...")
            time.sleep(wait)
    raise Exception("Não foi possível localizar o servidor após várias tentativas.")


# ─── Chat ─────────────────────────────────────────────────────────────────────
def start_chat(username: str, server_address: str):
    channel = grpc.insecure_channel(server_address)
    stub    = chat_pb2_grpc.ChatServiceStub(channel)

    def generate_messages():
        while True:
            text = input()
            if text.strip():
                yield chat_pb2.ChatMessage(
                    user=username,
                    text=text,
                    timestamp=int(time.time())
                )

    def receive_messages(response_iterator):
        for message in response_iterator:
            # Não exibe as próprias mensagens (já visíveis pelo input).
            if message.user != username:
                print(f"\n  [{message.user}]: {message.text}")
                print(f"  {username} > ", end="", flush=True)
            else:
                # Confirma entrega das próprias mensagens.
                print(f"\r  Enviada: [{message.user}]: {message.text}")
                print(f"  {username} > ", end="", flush=True)

    print(f"\n  Conectado ao chat! Digite suas mensagens abaixo.")
    print(f"  ─────────────────────────────────────────────\n")

    responses = stub.ChatStream(generate_messages())

    # Thread para receber mensagens do servidor sem bloquear o input.
    threading.Thread(
        target=receive_messages,
        args=(responses,),
        daemon=True
    ).start()

    print(f"  {username} > ", end="", flush=True)

    # Mantém o processo vivo.
    while True:
        time.sleep(1)


# ─── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("╔══════════════════════════════════╗")
    print("║       DistChat — CLI Client       ║")
    print("╚══════════════════════════════════╝\n")

    # Solicita nome de usuário (pode ser deixado em branco para 'Anônimo').
    username = input("  Seu nome: ").strip() or "Anônimo"

    try:
        server_address = discover_with_retry()
        start_chat(username, server_address)
    except KeyboardInterrupt:
        print("\n  Saindo do chat. Até logo!")
    except Exception as e:
        print(f"\n  Erro fatal: {e}")