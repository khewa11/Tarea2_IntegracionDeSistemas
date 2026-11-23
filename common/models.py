"""Contratos de entrada compartidos por los microservicios."""
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing import Annotated

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReservaIn(StrictModel):
    sagaId: Identifier
    espacioId: Identifier


class CobroIn(StrictModel):
    sagaId: Identifier
    monto: Decimal = Field(gt=0, max_digits=14, decimal_places=2)


class HabilitacionIn(StrictModel):
    sagaId: Identifier
    patente: Identifier
    espacioId: Identifier


class FallaIn(StrictModel):
    modo: Literal["ok", "lento", "caido", "error"]
    delayMs: int = Field(default=0, ge=0, le=30000)
