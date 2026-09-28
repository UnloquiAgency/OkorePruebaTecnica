"""APIs simuladas de expedientes, talleres y acciones (FastAPI, invocadas en proceso):
- GET  /api/claims/{claim_id}
- GET  /api/workshops/{workshop_id}   (admite simulación de error 500 / timeout)
- POST /api/claims/{claim_id}/actions (idempotente por X-Idempotency-Key)

El histórico de eventos no tiene endpoint: el agente lo lee de SQLite con una
consulta parametrizada (server/database.py).
"""

from fastapi import FastAPI, HTTPException, Header, Query, status
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import time

from server.schemas import (
    ClaimResponse,
    WorkshopResponse,
    ClaimActionRequest,
    ClaimActionResponse,
    ClaimEvent,
)
from server.database import ClaimDatabase

app = FastAPI(
    title="OKORE Mock Services API",
    description="Servicios simulados para la plataforma de gestión de expedientes y talleres de OKORE",
    version="1.0.0",
)

db = ClaimDatabase()


def set_database(database: ClaimDatabase) -> None:
    global db
    db = database

# Simulación de datos en memoria para expedientes
CLAIMS_DB: Dict[str, Dict[str, Any]] = {
    "EXP-10234": {
        "claim_id": "EXP-10234",
        "insurer_id": "ASEG-A",
        "status": "REPAIRING",
        "vehicle": {
            "plate": "1234ABC",
            "brand": "BMW",
            "model": "X1",
        },
        "workshop_id": "T-228",
        "missing_documents": ["PHOTO_PLATE"],
        "last_update": "2026-09-22T10:32:00",
    },
    "EXP-99999": {
        "claim_id": "EXP-99999",
        "insurer_id": "ASEG-B",
        "status": "COMPLETED",
        "vehicle": {
            "plate": "5678XYZ",
            "brand": "AUDI",
            "model": "A4",
        },
        "workshop_id": "T-100",
        "missing_documents": [],
        "last_update": "2026-09-15T12:00:00",
    },
}

# Simulación de datos en memoria para talleres
WORKSHOPS_DB: Dict[str, Dict[str, Any]] = {
    "T-228": {
        "workshop_id": "T-228",
        "name": "Talleres AutoMadrid Central",
        "contact": {
            "phone": "+34 912 345 678",
            "email": "recepcion@automadridcentral.com",
        },
        "status": "ACTIVE",
        "available_channels": ["EMAIL", "WHATSAPP", "API"],
    },
    "T-100": {
        "workshop_id": "T-100",
        "name": "Carrocerías Norte Express",
        "contact": {
            "phone": "+34 931 999 888",
            "email": "contacto@nortexpress.es",
        },
        "status": "ACTIVE",
        "available_channels": ["EMAIL", "API"],
    },
}

@app.get("/api/claims/{claim_id}", response_model=ClaimResponse)
def get_claim(claim_id: str):
    """Consulta los datos de un expediente."""
    claim = CLAIMS_DB.get(claim_id)
    if not claim:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Expediente '{claim_id}' no encontrado en el sistema.",
        )
    return claim


@app.get("/api/workshops/{workshop_id}", response_model=WorkshopResponse)
def get_workshop(
    workshop_id: str,
    simulate_error: Optional[str] = Query(None, description="Parámetro para pruebas de fallo: '500' o 'timeout'"),
):
    """Consulta los datos de un taller. `simulate_error` fuerza una caída para probar la degradación."""
    err_mode = simulate_error
    if err_mode == "500":
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error 500: Taller Service Unavailable / Internal Database Timeout.",
        )
    if err_mode == "timeout":
        time.sleep(2)  # Simulación de retraso
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Timeout de conexión con la API del taller.",
        )

    workshop = WORKSHOPS_DB.get(workshop_id)
    if not workshop:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Taller '{workshop_id}' no encontrado.",
        )
    return workshop


@app.post("/api/claims/{claim_id}/actions", response_model=ClaimActionResponse)
def execute_claim_action(
    claim_id: str,
    payload: ClaimActionRequest,
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    """
    Ejecuta una acción sobre el expediente (ej: solicitar documento).
    Aplica controles de:
    - Verificación de existencia del expediente
    - Validación determinista de precondición (el documento debe faltar realmente)
    - Idempotencia (evita que la misma solicitud se duplique)
    - Registro en histórico de eventos SQLite
    """
    claim = CLAIMS_DB.get(claim_id)
    if not claim:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Expediente '{claim_id}' no encontrado.",
        )

    # Validación determinista: ¿el documento realmente falta?
    if payload.action == "REQUEST_DOCUMENT":
        if payload.document not in claim.get("missing_documents", []):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"El documento '{payload.document}' no consta como pendiente para el expediente '{claim_id}'.",
            )

    # Control de Idempotencia
    idempotency_key = x_idempotency_key or f"{claim_id}:{payload.action}:{payload.document}"
    workshop_id = claim.get("workshop_id", "DESCONOCIDO")

    is_new = db.check_and_register_action(
        idempotency_key=idempotency_key,
        claim_id=claim_id,
        action=payload.action,
        document=payload.document,
        workshop_id=workshop_id,
    )

    if not is_new:
        # Acción ya ejecutada previamente (Idempotencia garantizada)
        return ClaimActionResponse(
            success=True,
            action=payload.action,
            document=payload.document,
            claim_id=claim_id,
            message=f"ACCION_YA_EJECUTADA: La solicitud de '{payload.document}' al taller {workshop_id} ya fue tramitada previamente (Idempotency Key respetada).",
            timestamp=datetime.now(timezone.utc).isoformat(),
            idempotency_key=idempotency_key,
        )

    # Registrar el evento en el histórico SQLite
    now_iso = datetime.now(timezone.utc).isoformat()
    db.add_event(
        ClaimEvent(
            claim_id=claim_id,
            event_type="DOCUMENT_REQUESTED",
            event_date=now_iso,
            description=f"Solicitud automática enviada al taller {workshop_id} para pedir documento '{payload.document}'.",
            actor="AGENT_EXECUTION",
        )
    )

    # Actualizar estado simulado
    claim["last_update"] = now_iso

    return ClaimActionResponse(
        success=True,
        action=payload.action,
        document=payload.document,
        claim_id=claim_id,
        message=f"Solicitud de '{payload.document}' registrada con éxito en el expediente {claim_id} para el taller {workshop_id}.",
        timestamp=now_iso,
        idempotency_key=idempotency_key,
    )
