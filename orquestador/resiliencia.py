"""Patrones de resiliencia: Circuit Breaker y Bulkhead.

Implementados a mano (sin librerías) para que su comportamiento sea fácil de
explicar y observar en /metrics. Todo corre en un único event loop de asyncio,
por lo que no hacen falta locks: no hay `await` entre la comprobación y el cambio
de estado.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable


class CircuitoAbierto(Exception):
    """El circuito está ABIERTO: se rechaza la llamada sin contactar al servicio."""

    def __init__(self, servicio: str):
        super().__init__(f"Circuito abierto hacia {servicio}")
        self.servicio = servicio


class BulkheadLleno(Exception):
    """No quedan cupos de concurrencia para el servicio protegido."""

    def __init__(self, servicio: str):
        super().__init__(f"Bulkhead lleno hacia {servicio}")
        self.servicio = servicio


class CircuitBreaker:
    CERRADO = "CERRADO"
    ABIERTO = "ABIERTO"
    SEMI_ABIERTO = "SEMI_ABIERTO"

    def __init__(self, servicio: str, umbral_fallos: int, espera_s: float,
                 reloj: Callable[[], float] = time.monotonic,
                 al_cambiar: Callable[[str, str, str], None] | None = None):
        self.servicio = servicio
        self.umbral_fallos = umbral_fallos
        self.espera_s = espera_s
        self._reloj = reloj
        self._al_cambiar = al_cambiar
        self._estado = self.CERRADO
        self._abierto_desde = 0.0
        self._prueba_en_curso = False
        self.fallos_consecutivos = 0
        self.rechazos = 0
        self.aperturas = 0

    @property
    def estado(self) -> str:
        # ABIERTO pasa a SEMI_ABIERTO automáticamente cuando vence la espera.
        if self._estado == self.ABIERTO and self._reloj() - self._abierto_desde >= self.espera_s:
            self._cambiar(self.SEMI_ABIERTO)
        return self._estado

    def rechaza_llamadas(self) -> bool:
        """True si una llamada nueva sería rechazada ahora mismo (para fallar rápido)."""
        estado = self.estado
        return estado == self.ABIERTO or (estado == self.SEMI_ABIERTO and self._prueba_en_curso)

    def antes_de_llamar(self) -> None:
        estado = self.estado
        if estado == self.ABIERTO:
            self.rechazos += 1
            raise CircuitoAbierto(self.servicio)
        if estado == self.SEMI_ABIERTO:
            # Solo una llamada de prueba a la vez en SEMI_ABIERTO.
            if self._prueba_en_curso:
                self.rechazos += 1
                raise CircuitoAbierto(self.servicio)
            self._prueba_en_curso = True

    def registrar_exito(self) -> None:
        self._prueba_en_curso = False
        self.fallos_consecutivos = 0
        if self._estado != self.CERRADO:
            self._cambiar(self.CERRADO)

    def registrar_fallo(self) -> None:
        self._prueba_en_curso = False
        self.fallos_consecutivos += 1
        if self._estado == self.SEMI_ABIERTO or self.fallos_consecutivos >= self.umbral_fallos:
            if self._estado != self.ABIERTO:
                self.aperturas += 1
            self._abierto_desde = self._reloj()
            self._cambiar(self.ABIERTO)

    def liberar_prueba(self) -> None:
        """Libera la llamada de prueba si terminó sin éxito ni fallo (p. ej. error 4xx)."""
        self._prueba_en_curso = False

    def _cambiar(self, nuevo: str) -> None:
        anterior, self._estado = self._estado, nuevo
        if anterior != nuevo and self._al_cambiar:
            self._al_cambiar(self.servicio, anterior, nuevo)

    def snapshot(self) -> dict:
        return {
            "estado": self.estado,
            "fallosConsecutivos": self.fallos_consecutivos,
            "umbralFallos": self.umbral_fallos,
            "esperaSegundos": self.espera_s,
            "aperturas": self.aperturas,
            "rechazos": self.rechazos,
        }


class Bulkhead:
    """Limita las llamadas simultáneas a un servicio. Sin cola: si está lleno, rechaza."""

    def __init__(self, servicio: str, maximo: int):
        self.servicio = servicio
        self.maximo = maximo
        self.en_curso = 0
        self.pico = 0
        self.rechazos = 0

    @asynccontextmanager
    async def cupo(self) -> AsyncIterator[None]:
        if self.en_curso >= self.maximo:
            self.rechazos += 1
            raise BulkheadLleno(self.servicio)
        self.en_curso += 1
        self.pico = max(self.pico, self.en_curso)
        try:
            yield
        finally:
            self.en_curso -= 1

    def snapshot(self) -> dict:
        return {
            "maximo": self.maximo,
            "enCurso": self.en_curso,
            "pico": self.pico,
            "rechazos": self.rechazos,
        }
