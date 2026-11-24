"""Placeholder de Paso 0 para integrantes B/C.

No implementa auth, enrutamiento, Saga ni BFF: todas las rutas de negocio devuelven 501.
Su ÚNICO propósito es levantar 6 contenedores con el Compose común.
El encargado del servicio debe reemplazar su Dockerfile y aplicación.
"""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from urllib.parse import urlsplit

SERVICE = os.environ.get("SERVICE", "sin-configurar")
PORT = int(os.environ.get("PORT", "3000"))


def log_event(paso: str, resultado: str, saga_id=None):
    print(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "sagaId": saga_id,
        "servicio": SERVICE,
        "paso": paso,
        "resultado": resultado,
    }, ensure_ascii=False), flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        # Evitar access logs libres: los eventos relevantes se registran como JSON.
        pass

    def respond(self, status: int, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_request(self):
        route = urlsplit(self.path).path
        saga_id = None
        if self.headers.get("Content-Type", "").startswith("application/json"):
            size = int(self.headers.get("Content-Length", "0"))
            if 0 < size <= 65536:
                try:
                    saga_id = json.loads(self.rfile.read(size)).get("sagaId")
                except (ValueError, AttributeError):
                    pass
        if self.command == "GET" and route == "/health":
            self.respond(200, {"servicio": SERVICE, "estado": "stub", "implementado": False})
            log_event("health", "STUB_OK")
            return
        self.respond(501, {
            "error": "PENDIENTE_IMPLEMENTACION",
            "servicio": SERVICE,
            "ruta": route,
            "detalle": "Placeholder del Paso 0. Responsable B/C debe reemplazar este servicio."
        })
        log_event(route, "STUB_501", saga_id)

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_PUT(self):
        self.handle_request()

    def do_DELETE(self):
        self.handle_request()


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log_event("inicio", "STUB_LISTO")
    server.serve_forever()
