"""Motor de lenguaje del agente.

Un solo cerebro: el LLM. Se usa para las dos cosas que requieren juicio:

  1. `parse_intent_and_entities` — interpretar la peticion: que intencion tiene,
     de que expediente habla, que documento pide, y **que herramientas necesita**
     consultar para responder. El modelo declara que datos hacen falta; el codigo
     decide si se los da (ver orchestrator.py).
  2. `synthesize_response` — redactar la respuesta con los datos que trajeron las
     herramientas, sin inventar nada.

Lo que el LLM NO hace, y vive en codigo determinista: permisos (`security.py`),
precondiciones e idempotencia (`orchestrator.py`, `mock_services.py`),
consentimiento humano (`SecurityManager.classify_consent`) y traza (`audit.py`).

Si el modelo no responde, la peticion termina en error controlado y queda
registrada. No hay un segundo cerebro de reglas que la conteste por lo bajo.
La unica alternativa al modelo es el modo simulador de main.py/run_demo.py sin
clave (tests/stub_llm.py), que se anuncia en pantalla: cambia quien interpreta,
no los controles del orquestador.
"""

import json
import re
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List

import httpx
from pydantic import BaseModel, Field

from agent.config import config

# Catalogo cerrado de documentos. El LLM traduce lenguaje natural a estos codigos;
# el codigo valida despues que el documento pedido este realmente pendiente.
DOCUMENT_CATALOG: Dict[str, str] = {
    "PHOTO_PLATE": "fotografía de matrícula",
    "POLICE_REPORT": "atestado policial",
    "ID_CARD": "documento de identidad",
    "TECHNICAL_SHEET": "ficha técnica",
}

INTENTS = ("CONSULT_CLAIM", "REQUEST_DOCUMENT", "OTHER")

# Herramientas de lectura que el LLM puede pedir. `claim` no esta en la lista:
# se consulta siempre, porque es la fuente de la comprobacion de acceso.
OPTIONAL_TOOLS = ("workshop", "events")

CLAIM_ID_FORMAT = re.compile(r"^EXP-\d{4,6}$")


class LLMUnavailableError(Exception):
    """El modelo no respondio o devolvio algo que no cumple el esquema."""


class ParsedIntent(BaseModel):
    intent: str = Field(default="OTHER", description=" | ".join(INTENTS))
    claim_id: Optional[str] = None
    target_action: Optional[str] = None
    target_document: Optional[str] = None
    needs: List[str] = Field(
        default_factory=list,
        description="Herramientas de lectura que el modelo considera necesarias: workshop, events",
    )
    reasoning: str = ""


class BaseLLMProvider(ABC):
    @abstractmethod
    def parse_intent_and_entities(
        self, prompt: str, state_context: Optional[Dict[str, Any]] = None
    ) -> ParsedIntent:
        ...

    @abstractmethod
    def synthesize_response(
        self,
        prompt: str,
        parsed_intent: ParsedIntent,
        tool_results: Dict[str, Any],
    ) -> str:
        ...


# --- Esquema de salida estructurada -------------------------------------------
# DeepSeek V4 Flash soporta `response_format` con json_schema. Pedirlo asi evita
# tener que parsear prosa y hace que un incumplimiento sea un error, no un dato malo.
_INTENT_SCHEMA = {
    "name": "peticion_okore",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["intent", "claim_id", "target_document", "needs", "reasoning"],
        "properties": {
            "intent": {"type": "string", "enum": list(INTENTS)},
            "claim_id": {
                "type": ["string", "null"],
                "description": "Normalizado a EXP-NNNNN. null si el usuario no menciona ninguno.",
            },
            "target_document": {
                "type": ["string", "null"],
                "enum": list(DOCUMENT_CATALOG.keys()) + [None],
                "description": "Solo si intent es REQUEST_DOCUMENT.",
            },
            "needs": {
                "type": "array",
                "items": {"type": "string", "enum": list(OPTIONAL_TOOLS)},
                "description": "Que consultas adicionales hacen falta para responder.",
            },
            "reasoning": {"type": "string", "description": "Una frase, para la traza."},
        },
    },
}

_SYSTEM_PARSE = f"""Eres el interprete de peticiones del agente de expedientes de OKORE.
Tu unico trabajo es convertir lo que escribe un empleado en un objeto estructurado.
No respondes al usuario y no inventas datos: solo interpretas.

INTENCIONES
- CONSULT_CLAIM: quiere saber algo de un expediente (estado, taller, que falta, historico).
- REQUEST_DOCUMENT: quiere que se solicite un documento al taller. Verbos tipo
  pide, solicita, reclama, mandale, pedile, requiere.
- OTHER: cualquier otra cosa.

EXPEDIENTE
Normaliza siempre a EXP-NNNNN. "exp 10234", "EXP10234", "el 10234" y "expediente
10234" son todos EXP-10234. Si no menciona ninguno, devuelve null.

DOCUMENTOS (catalogo cerrado, usa el codigo exacto)
{chr(10).join(f'- {k}: {v}' for k, v in DOCUMENT_CATALOG.items())}
Traduce sinonimos del habla real: "foto de la patente", "imagen de la placa",
"la matricula" y "foto del vehiculo con la placa" son todos PHOTO_PLATE.
Si pide un documento que no esta en el catalogo, devuelve null en target_document.

QUE CONSULTAR (campo needs)
El expediente se consulta siempre, no lo pidas. Ademas puedes pedir:
- "workshop": si la respuesta necesita datos del taller (nombre, contacto, canales)
  o si hay que solicitarle un documento.
- "events": si la respuesta necesita el historico (que paso, desde cuando, cronologia).
Pide solo lo que haga falta. Para "en que estado esta y que estamos esperando"
el historico ayuda a explicar desde cuando; para "que taller lo lleva" no hace falta."""

_SYSTEM_WRITE = (
    "Eres el Asistente de Operaciones e IA de OKORE, el copiloto inteligente para tramitadores de siniestros y talleres.\n\n"
    "1. AUDIENCIA Y ROL:\n"
    "- Hablas con un TRAMITADOR INTERNO de la aseguradora (nunca con el cliente asegurado ni con el taller).\n"
    "- El tramitador NO tiene la documentación física ni el vehículo; los documentos pendientes (missing_documents) debe enviarlos el TALLER mecánico asignado.\n\n"
    "2. SALUDOS Y MENSAJES GENERALES (cuando no hay expediente en contexto):\n"
    "- Saluda con calidez y profesionalismo como el asistente de operaciones de OKORE.\n"
    "- Explica brevemente y con claridad qué puedes hacer para ayudarle:\n"
    "  • Consultar el estado y documentación de un expediente de reparación (ej. EXP-10234).\n"
    "  • Verificar información de contacto y canales del taller mecánico asignado.\n"
    "  • Gestionar la solicitud formal de documentos pendientes al taller.\n"
    "  • Revisar la cronología e historial de eventos del siniestro.\n"
    "- Invítale cordialmente a indicar el número de expediente con el que desea trabajar.\n\n"
    "3. CONSULTAS DE EXPEDIENTES (cuando hay datos de siniestro):\n"
    "- Resume con claridad el estado del vehículo (marca, modelo, matrícula) y el taller asignado.\n"
    "- Si hay documentos pendientes en 'missing_documents', revisa si en el historial de eventos (events) ya figura que fueron solicitados al taller (DOCUMENT_REQUESTED):\n"
    "  • Si YA fueron solicitados previamente: aclara que la solicitud ya fue enviada al taller y se está a la espera de su respuesta. NO vuelvas a ofrecer solicitarlo de nuevo si ya se pidió.\n"
    "  • Si AÚN NO han sido solicitados: ofrécele proactivamente al tramitador enviar la solicitud al taller (ej. 'Falta que el taller aporte la fotografía de la matrícula. Si lo deseas, puedo solicitársela al taller').\n"
    "- Si la información de taller o eventos viene con fallback o error, dilo con total transparencia y cortesía en lugar de omitirlo.\n\n"
    "4. REGLAS ESTRICTAS DE SEGURIDAD Y ESTILO:\n"
    "- NUNCA uses jerga técnica informática (prohibido decir 'JSON', 'bloque de información autorizada', 'null', 'array', 'base de datos'). Habla siempre como un asistente profesional de seguros.\n"
    "- NUNCA afirmes haber ejecutado o enviado una acción por tu cuenta: tú solo consultas, propones e informas. Las acciones técnicas las confirma el usuario y las procesa el sistema.\n"
    "- Formato: Español profesional, claro, con viñetas cuando convenga para facilitar la lectura. Fechas en formato dd/mm/aaaa."
)


class OpenRouterBrain(BaseLLMProvider):
    """Cliente compatible con la API de OpenAI. Sirve OpenRouter, DeepSeek directo,
    Ollama o vLLM cambiando `base_url` y `model`: no hay ramas por proveedor."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
    ):
        self.api_key = api_key if api_key is not None else config.openrouter_api_key
        self.base_url = (base_url or config.openrouter_base_url).rstrip("/")
        self.model_name = model_name or config.openrouter_model

    # --- transporte ----------------------------------------------------------
    def _chat(self, payload: Dict[str, Any], timeout: float) -> str:
        if not self.api_key:
            raise LLMUnavailableError(
                "No hay clave de LLM configurada (OPENROUTER_API_KEY). "
                "El agente necesita el modelo para interpretar peticiones."
            )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model_name,
            "temperature": 0,
            # Sin cadena de razonamiento: ninguna de las dos llamadas resuelve un
            # problema, clasifican y redactan. Con razonamiento activo el modelo
            # gastaba mas tokens pensando que respondiendo, y cada llamada se iba
            # a 20 s. Desactivarlo la deja en torno a 2 s sin perder calidad.
            "reasoning": {"enabled": False},
            # Solo proveedores que soportan todo lo que pedimos (salida estructurada
            # incluida) y que no retienen los datos enviados.
            "provider": {"require_parameters": True, "data_collection": "deny"},
            **payload,
        }
        try:
            with httpx.Client(timeout=timeout) as client:
                res = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
        except httpx.RequestError as exc:
            raise LLMUnavailableError(f"Fallo de red hablando con el modelo: {exc}") from exc
        if res.status_code != 200:
            raise LLMUnavailableError(f"El modelo respondio HTTP {res.status_code}: {res.text[:200]}")
        try:
            return res.json()["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMUnavailableError(f"Respuesta del modelo con forma inesperada: {exc}") from exc

    # --- 1. interpretar ------------------------------------------------------
    def parse_intent_and_entities(
        self, prompt: str, state_context: Optional[Dict[str, Any]] = None
    ) -> ParsedIntent:
        contexto = ""
        if state_context:
            previos = state_context.get("turnos_previos") or []
            activo = state_context.get("expediente_activo")
            if previos:
                lineas = [f"  usuario: {t['usuario']}\n  agente: {t['agente']}" for t in previos[-3:]]
                contexto += "TURNOS PREVIOS (para resolver referencias como \"esa foto\" o \"pedila\"):\n"
                contexto += "\n".join(lineas) + "\n\n"
            if activo:
                contexto += f"EXPEDIENTE ACTIVO EN LA CONVERSACION: {activo}\n\n"

        content = self._chat(
            {
                "messages": [
                    {"role": "system", "content": _SYSTEM_PARSE},
                    {"role": "user", "content": contexto + "MENSAJE ACTUAL: " + prompt},
                ],
                "response_format": {"type": "json_schema", "json_schema": _INTENT_SCHEMA},
                "max_tokens": 300,
            },
            timeout=15.0,
        )
        try:
            raw = json.loads(content)
        except ValueError as exc:
            raise LLMUnavailableError(f"El modelo no devolvio JSON valido: {content[:200]}") from exc

        # El esquema ya restringe los valores, pero el codigo no depende de que el
        # proveedor lo respete: todo campo se filtra contra su lista cerrada o formato.
        claim_id = (raw.get("claim_id") or "").strip().upper()
        parsed = ParsedIntent(
            intent=raw.get("intent") if raw.get("intent") in INTENTS else "OTHER",
            claim_id=claim_id if CLAIM_ID_FORMAT.match(claim_id) else None,
            target_document=raw.get("target_document") if raw.get("target_document") in DOCUMENT_CATALOG else None,
            needs=[n for n in (raw.get("needs") or []) if n in OPTIONAL_TOOLS],
            reasoning=(raw.get("reasoning") or "")[:300],
        )
        if parsed.intent == "REQUEST_DOCUMENT":
            parsed.target_action = "REQUEST_DOCUMENT"
            # Para solicitar hace falta saber a que taller: el codigo lo impone,
            # no depende de que el modelo se acuerde de pedirlo.
            if "workshop" not in parsed.needs:
                parsed.needs.append("workshop")
        return parsed

    # --- 2. redactar ---------------------------------------------------------
    def synthesize_response(
        self,
        prompt: str,
        parsed_intent: ParsedIntent,
        tool_results: Dict[str, Any],
    ) -> str:
        datos = json.dumps(tool_results, ensure_ascii=False, default=str)
        content = self._chat(
            {
                "messages": [
                    {"role": "system", "content": _SYSTEM_WRITE},
                    {"role": "user", "content": f"Pregunta: {prompt}\nDatos autorizados: {datos}"},
                ],
            },
            timeout=20.0,
        )
        # Garantia determinista: si el taller no respondio, el aviso lo pone el codigo,
        # no se confia en que el modelo se acuerde de mencionarlo.
        taller = tool_results.get("workshop") or {}
        if taller.get("fallback") and "[AVISO DEL SISTEMA]" not in content:
            content += (
                f"\n[AVISO DEL SISTEMA] No se pudieron consultar los datos del taller "
                f"{taller.get('workshop_id')}: el servicio no respondió tras "
                f"{config.max_retries + 1} intentos. La información de taller de esta "
                f"respuesta está incompleta."
            )
        return content


def get_default_llm_provider() -> BaseLLMProvider:
    """El agente tiene un solo cerebro. Los tests inyectan un doble (tests/stub_llm.py)."""
    return OpenRouterBrain()
