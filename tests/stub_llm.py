"""Doble de pruebas del motor de lenguaje.

Fija la salida del modelo para probar el resto del flujo: la suite corre sin red
y sin clave. main.py y run_demo.py lo usan como modo simulador cuando no hay
clave, avisandolo en pantalla; los controles del orquestador son los mismos.

Cubre frases simples. No pretende interpretar lenguaje natural real.
"""

import re
from typing import Optional, Dict, Any

from agent.llm_provider import BaseLLMProvider, ParsedIntent

_PEDIR = ("pide", "pedi", "solicita", "reclama", "requiere", "manda", "envia")
_DOCS = {
    "matricula": "PHOTO_PLATE", "matrícula": "PHOTO_PLATE", "placa": "PHOTO_PLATE",
    "patente": "PHOTO_PLATE", "photo_plate": "PHOTO_PLATE",
    "atestado": "POLICE_REPORT", "ficha": "TECHNICAL_SHEET", "identidad": "ID_CARD",
}


class StubBrain(BaseLLMProvider):
    """Interpreta con reglas mínimas y redacta con plantilla."""

    def parse_intent_and_entities(
        self, prompt: str, state_context: Optional[Dict[str, Any]] = None
    ) -> ParsedIntent:
        text = prompt.lower()

        claim_id = None
        m = re.search(r"\bexp[-\s]?(\d{4,6})\b", text)
        if m:
            claim_id = f"EXP-{m.group(1)}"

        doc = next((code for word, code in _DOCS.items() if word in text), None)

        if any(v in text for v in _PEDIR) and doc:
            return ParsedIntent(
                intent="REQUEST_DOCUMENT", claim_id=claim_id, target_action="REQUEST_DOCUMENT",
                target_document=doc, needs=["workshop"], reasoning="stub: verbo de peticion + documento",
            )
        if claim_id or "expediente" in text or "taller" in text:
            needs = ["workshop"] if "taller" in text or "contacto" in text else ["workshop", "events"]
            return ParsedIntent(
                intent="CONSULT_CLAIM", claim_id=claim_id, needs=needs, reasoning="stub: consulta",
            )
        return ParsedIntent(intent="OTHER", reasoning="stub: sin expediente")

    def synthesize_response(
        self,
        prompt: str,
        parsed_intent: ParsedIntent,
        tool_results: Dict[str, Any],
    ) -> str:
        claim = tool_results.get("claim") or {}
        if not claim:
            return "Indícame el número de expediente (ejemplo: EXP-10234)."

        veh = claim.get("vehicle") or {}
        partes = [
            f"El expediente {claim.get('claim_id')} está en estado {claim.get('status')}.",
            f"Vehículo: {veh.get('brand')} {veh.get('model')} ({veh.get('plate')}).",
            f"Taller asignado: {claim.get('workshop_id')}.",
        ]
        faltan = claim.get("missing_documents") or []
        partes.append(
            f"Documentación pendiente: {', '.join(faltan)}." if faltan
            else "No hay documentación pendiente."
        )

        taller = tool_results.get("workshop") or {}
        if taller.get("fallback"):
            partes.append(
                f"[AVISO DEL SISTEMA] No se pudieron consultar los datos del taller "
                f"{taller.get('workshop_id')}: el servicio no respondió."
            )
        elif taller:
            partes.append(f"Taller: {taller.get('name')}.")

        eventos = tool_results.get("events") or []
        if eventos:
            partes.append(f"Último evento registrado: {eventos[-1].get('event_type')}.")
        return " ".join(partes)
