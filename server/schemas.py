"""Esquemas de datos Pydantic para APIs de expedientes, talleres y acciones."""

from typing import List, Optional
from pydantic import BaseModel, Field


class VehicleInfo(BaseModel):
    plate: str = Field(..., description="Matrícula del vehículo")
    brand: str = Field(..., description="Marca del vehículo")
    model: str = Field(..., description="Modelo del vehículo")


class ClaimResponse(BaseModel):
    claim_id: str = Field(..., description="Identificador único del expediente")
    insurer_id: str = Field(..., description="Aseguradora propietaria del expediente (aislamiento de datos)")
    status: str = Field(..., description="Estado actual del expediente (e.g. REPAIRING, PENDING, COMPLETED)")
    vehicle: VehicleInfo = Field(..., description="Información del vehículo")
    workshop_id: str = Field(..., description="Identificador del taller asignado")
    missing_documents: List[str] = Field(default_factory=list, description="Lista de documentos pendientes")
    last_update: str = Field(..., description="Fecha y hora de última actualización ISO 8601")


class WorkshopContact(BaseModel):
    phone: str
    email: str


class WorkshopResponse(BaseModel):
    workshop_id: str = Field(..., description="Identificador del taller")
    name: str = Field(..., description="Nombre comercial del taller")
    contact: WorkshopContact = Field(..., description="Datos de contacto")
    status: str = Field(..., description="Estado del taller (e.g. ACTIVE, INACTIVE)")
    available_channels: List[str] = Field(..., description="Canales de comunicación disponibles")


class ClaimActionRequest(BaseModel):
    action: str = Field(..., description="Tipo de acción a ejecutar, ej: REQUEST_DOCUMENT")
    document: str = Field(..., description="Nombre técnico del documento, ej: PHOTO_PLATE")


class ClaimActionResponse(BaseModel):
    success: bool
    action: str
    document: str
    claim_id: str
    message: str
    timestamp: str
    idempotency_key: Optional[str] = None


class ClaimEvent(BaseModel):
    id: Optional[int] = None
    claim_id: str
    event_type: str
    event_date: str
    description: str
    actor: Optional[str] = "SYSTEM"
