"""Adaptadores de pasarela de pago (patrón Adaptador).

El microservicio Pagos conserva la persistencia, la idempotencia por sagaId y las
"lápidas" de compensación; la pasarela solo traduce operaciones hacia el proveedor:

    iniciar(sagaId, monto)   -> cobro inmediato (simulada) o transacción PENDIENTE (Webpay)
    confirmar(token)         -> resultado de la autorización (solo pasarelas asíncronas)
    reembolsar(token, monto) -> devolución del monto total

Se elige con la variable de entorno PASARELA=simulada|webpay (por defecto: simulada).
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass


class ErrorPasarela(Exception):
    """Fallo técnico o rechazo de la API del proveedor (no es un rechazo de la tarjeta)."""

    def __init__(self, operacion: str, detalle: str, codigo: int | None = None):
        super().__init__(f"{operacion}: {detalle}")
        self.operacion = operacion
        self.detalle = detalle
        self.codigo = codigo


@dataclass(frozen=True)
class Inicio:
    estado: str                    # "COBRADO" (síncrona) o "PENDIENTE" (requiere redirección)
    referencia: str
    token: str | None = None
    url: str | None = None
    buy_order: str | None = None


@dataclass(frozen=True)
class Confirmacion:
    aprobado: bool
    estado_proveedor: str | None
    response_code: int | None
    authorization_code: str | None


@dataclass(frozen=True)
class Reembolso:
    tipo: str | None
    response_code: int | None


class Pasarela(ABC):
    nombre: str
    asincrona: bool

    @abstractmethod
    def iniciar(self, saga_id: str, monto_centavos: int) -> Inicio: ...

    @abstractmethod
    def confirmar(self, token: str) -> Confirmacion: ...

    @abstractmethod
    def reembolsar(self, token: str | None, monto_centavos: int) -> Reembolso: ...


class PasarelaSimulada(Pasarela):
    """Comportamiento histórico: cobro inmediato con referencia SIM-<sagaId>, sin red."""

    nombre = "SIMULADA"
    asincrona = False

    def iniciar(self, saga_id: str, monto_centavos: int) -> Inicio:
        return Inicio(estado="COBRADO", referencia="SIM-" + saga_id)

    def confirmar(self, token: str) -> Confirmacion:
        raise ErrorPasarela("confirmar", "La pasarela simulada no requiere confirmación")

    def reembolsar(self, token: str | None, monto_centavos: int) -> Reembolso:
        return Reembolso(tipo="SIMULADO", response_code=0)


def crear_pasarela(nombre: str | None = None) -> Pasarela:
    nombre = (nombre or os.getenv("PASARELA", "simulada")).strip().lower()
    if nombre == "simulada":
        return PasarelaSimulada()
    raise ValueError(f"PASARELA desconocida: {nombre!r} (use 'simulada' o 'webpay')")
