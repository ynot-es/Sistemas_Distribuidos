"""
name_server.py — Servidor de Nomes (Descoberta de Serviços)
-----------------------------------------------------------
Características implementadas:
  - Registro dinâmico de serviços (servidores se registram ao iniciar)
  - Consulta de endereço por nome de serviço
  - Transparência de localização: o cliente não precisa conhecer o IP do servidor
"""

"""
    - name_server.py mantém um registry {nome -> endereço} server.py se registra dinamicamente
    ao iniciar: stub.Register(RegisterRequest(service_name="chat", address=MY_ADDRESS)) client.py 
    consulta antes de conectar: stub.GetServer(ServerRequest(service_name="chat"). O cliente nunca 
    precisa saber o IP do servidor — transparência de localização total.
"""

import grpc
from concurrent import futures
import threading
import time

import chat_pb2
import chat_pb2_grpc

NAME_SERVER_PORT = 50052


class NameService(chat_pb2_grpc.NameServiceServicer):

    def __init__(self):
        # Dicionário: nome_do_serviço -> endereço
        self._registry = {}
        self._lock     = threading.Lock()

    def Register(self, request, context):
        """Servidor se registra informando seu nome e endereço."""
        with self._lock:
            self._registry[request.service_name] = request.address
        print(f"[NameServer] Registrado: '{request.service_name}' → {request.address}")
        return chat_pb2.RegisterResponse(success=True)

    def GetServer(self, request, context):
        """Cliente consulta o endereço de um serviço pelo nome."""
        with self._lock:
            address = self._registry.get(request.service_name)

        if address:
            print(f"[NameServer] Consulta '{request.service_name}' => {address}")
            return chat_pb2.ServerResponse(address=address, found=True)
        else:
            print(f"[NameServer] Serviço '{request.service_name}' não encontrado!")
            return chat_pb2.ServerResponse(address="", found=False)


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    chat_pb2_grpc.add_NameServiceServicer_to_server(NameService(), server)
    server.add_insecure_port(f"[::]:{NAME_SERVER_PORT}")
    server.start()
    print(f"[NameServer] Rodando na porta {NAME_SERVER_PORT}")
    print(f"[NameServer] Aguardando registros de serviços...")
    server.wait_for_termination()


if __name__ == "__main__":
    serve()