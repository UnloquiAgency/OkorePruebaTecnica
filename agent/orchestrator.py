"""Orquestador central del agente de automatización de OKORE.

Ciclo de cada mensaje:
Guardarraíl → consentimiento pendiente → LLM (intención) → permisos → lecturas →
validación determinista → confirmación humana → acción → traza (JSONL)
"""

import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple
from pydantic import BaseModel

from agent.security import SecurityManager, UserRole, SecurityViolation
from agent.state import AgentState
from agent.tools import AgentTools, ToolExecutionError
from agent.audit import audit_logger, ExecutionAuditLog
from agent.llm_provider import (
    DOCUMENT_CATALOG,
    ParsedIntent,
    BaseLLMProvider,
    LLMUnavailableError,
    get_default_llm_provider,
)


class AgentResponse(BaseModel):
    response: str
    status: str  # SUCCESS, PENDING_CONFIRMATION, SECURITY_BLOCKED, RBAC_DENIED, ERROR
    request_id: str
    has_pending_action: bool = False


# Lecturas opcionales que el modelo puede declarar en `needs`, y la herramienta
# que las sirve. El permiso se comprueba sobre la herramienta.
READ_TOOLS = {"workshop": "get_workshop", "events": "get_claim_events"}


class ClaimAgentOrchestrator:
    def __init__(self, llm_provider: Optional[BaseLLMProvider] = None, tools: Optional[AgentTools] = None):
        self.llm = llm_provider or get_default_llm_provider()
        self.tools = tools or AgentTools()
        self.state = AgentState()

    def reset_state(self, user_id: str = "USR-OPERATOR-01", role: UserRole = UserRole.OPERATOR) -> None:
        self.state = AgentState(user_id=user_id, user_role=role)

    @staticmethod
    def _resumen_deterministico(tool_results: Dict[str, Any]) -> str:
        """Respuesta compuesta por codigo, para cuando no se puede confiar en la del modelo."""
        claim = tool_results.get("claim") or {}
        if not claim:
            return "No he podido completar la respuesta. Indícame el número de expediente e inténtalo de nuevo."
        veh = claim.get("vehicle") or {}
        faltan = claim.get("missing_documents") or []
        partes = [
            f"Expediente {claim.get('claim_id')}: estado {claim.get('status')}.",
            f"Vehículo {veh.get('brand')} {veh.get('model')} ({veh.get('plate')}), taller {claim.get('workshop_id')}.",
            f"Documentación pendiente: {', '.join(faltan)}." if faltan else "No hay documentación pendiente.",
            "No se ha realizado ninguna acción sobre el expediente en esta consulta.",
        ]
        return " ".join(partes)

    def _redactar(
        self, user_prompt: str, parsed: ParsedIntent, tool_results: Dict[str, Any], errors: List[str]
    ) -> Tuple[str, str]:
        """Redacta con el modelo y devuelve (respuesta, decision).

        No se ha ejecutado ninguna accion en los caminos que redacta el modelo. Si
        afirma lo contrario, el texto lo sustituye el codigo: una respuesta que miente
        sobre lo que hizo el sistema es peor que una respuesta seca.
        """
        try:
            reply = self.llm.synthesize_response(user_prompt, parsed, tool_results)
        except LLMUnavailableError as exc:
            errors.append(str(exc))
            return (
                "No puedo redactar la respuesta ahora mismo porque el modelo no está "
                "disponible. Inténtalo de nuevo en unos minutos.",
                "LLM_UNAVAILABLE",
            )
        if SecurityManager.claims_execution(reply):
            errors.append("La redaccion afirmaba una ejecucion que no ocurrio; sustituida por resumen deterministico.")
            return self._resumen_deterministico(tool_results), "RESPONSE_CORRECTED"
        return reply, "QUERY_ANSWERED"

    def _record_turn_and_audit(
        self,
        request_id: str,
        user_prompt: str,
        reply: str,
        detected_intent: str,
        tools_called: List[str],
        tool_params: Dict[str, Any],
        tool_results: Dict[str, Any],
        final_decision: str,
        action_requested: Optional[str],
        action_executed: bool,
        errors: List[str],
        t0: float,
    ) -> None:
        latency = (time.time() - t0) * 1000
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            self.tools.db.record_chat_message(self.state.session_id, request_id, "USER", user_prompt, now_iso)
            self.tools.db.record_chat_message(self.state.session_id, request_id, "AGENT", reply, now_iso)
        except Exception:
            pass

        # Turnos en memoria: solo para que el interprete resuelva referencias.
        self.state.add_turn(user_prompt, reply)

        audit_logger.record(
            ExecutionAuditLog(
                request_id=request_id,
                conversation_id=self.state.session_id,
                user_id=self.state.user_id,
                user_role=self.state.user_role.value,
                user_request=user_prompt,
                detected_intent=detected_intent,
                tools_called=tools_called,
                tool_parameters=tool_params,
                tool_results=tool_results,
                final_decision=final_decision,
                action_requested=action_requested,
                action_executed=action_executed,
                errors=errors,
                latency_ms=round(latency, 2),
            )
        )

    def process(
        self,
        user_prompt: str,
        user_id: Optional[str] = None,
        user_role: Optional[str] = None,
        simulate_workshop_error: Optional[str] = None,
    ) -> AgentResponse:
        t0 = time.time()
        request_id = str(uuid.uuid4())

        # Configuración de identidad y permisos para esta petición
        if user_id:
            self.state.user_id = user_id
        if user_role:
            self.state.user_role = SecurityManager.validate_user_role(user_role)

        tools_called: List[str] = []
        tool_params: Dict[str, Any] = {}
        tool_results: Dict[str, Any] = {}
        errors: List[str] = []
        action_requested: Optional[str] = None
        action_executed = False
        final_decision = "PROCESS_COMPLETED"
        detected_intent = "UNKNOWN"

        # ---------------------------------------------------------
        # PASO 1: Guardarraíl de seguridad (inyección y exfiltración)
        # ---------------------------------------------------------
        threat_msg = SecurityManager.inspect_prompt_safety(user_prompt)
        if threat_msg:
            errors.append(threat_msg)
            detected_intent = "MALICIOUS_PROMPT_BLOCKED"
            final_decision = "SECURITY_VIOLATION_BLOCKED"
            reply = (
                "[SEGURIDAD] ACCESO DENEGADO: La solicitud contiene instrucciones no permitidas "
                "(intento de evasión de directivas o solicitud de exfiltración de base de datos completa/datos personales). "
                "El sistema opera bajo principio de menor privilegio y no expone consultas directas ni datos no autorizados."
            )
            self._record_turn_and_audit(
                request_id, user_prompt, reply, detected_intent, [], {}, {}, final_decision, None, False, errors, t0
            )
            return AgentResponse(
                response=reply,
                status="SECURITY_BLOCKED",
                request_id=request_id,
                has_pending_action=False,
            )

        # ---------------------------------------------------------
        # PASO 2: Respuesta a una acción pendiente de confirmación
        # ---------------------------------------------------------
        if self.state.has_pending_action():
            pending = self.state.pending_action
            # La decision de consentimiento no la toma el LLM. El modelo redacta la
            # pregunta; el codigo interpreta la respuesta, y ante la duda no ejecuta.
            consent = SecurityManager.classify_consent(user_prompt)
            parsed = ParsedIntent(intent=f"CONSENT_{consent}")
            detected_intent = parsed.intent

            if consent == "AMBIGUOUS":
                # Se conserva la accion pendiente y se repregunta. No se ejecuta nada.
                final_decision = "CONFIRMATION_UNCLEAR"
                reply = (
                    "No he entendido si confirmas o cancelas, así que no he ejecutado nada.\n"
                    f"{pending.confirmation_prompt}\n"
                    "Responde 'sí' para ejecutar o 'no' para cancelar."
                )
                self._record_turn_and_audit(
                    request_id, user_prompt, reply, detected_intent, [], {}, {}, final_decision, pending.action, False, errors, t0
                )
                return AgentResponse(
                    response=reply,
                    status="PENDING_CONFIRMATION",
                    request_id=request_id,
                    has_pending_action=True,
                )

            if consent == "CONFIRM":
                # Verificación de permisos de mutación (RBAC)
                if not SecurityManager.check_tool_permission(self.state.user_role, "execute_claim_action"):
                    final_decision = "RBAC_DENIED"
                    reply = "No dispones de los permisos necesarios (rol actual no autorizado) para ejecutar modificaciones."
                    self.state.clear_pending_action()
                    self._record_turn_and_audit(
                        request_id, user_prompt, reply, detected_intent, [], {}, {}, final_decision, None, False, ["Permisos insuficientes"], t0
                    )
                    return AgentResponse(response=reply, status="RBAC_DENIED", request_id=request_id)

                # Ejecución de la acción con control de idempotencia
                tools_called.append("execute_claim_action")
                tool_params["execute_claim_action"] = {
                    "claim_id": pending.claim_id,
                    "action": pending.action,
                    "document": pending.document,
                    "workshop_id": pending.workshop_id,
                    "idempotency_key": pending.idempotency_key,
                }
                action_requested = pending.action

                try:
                    action_res = self.tools.execute_claim_action(
                        claim_id=pending.claim_id,
                        action=pending.action,
                        document=pending.document,
                        idempotency_key=pending.idempotency_key,
                    )
                    tool_results["action_result"] = action_res
                    action_executed = True
                    final_decision = "ACTION_EXECUTED"
                    # El texto de una escritura lo pone el codigo, no el modelo: es el
                    # unico mensaje que afirma que algo se ejecuto, y tiene que ser exacto.
                    reply = action_res.get("message") or (
                        f"Acción ejecutada con éxito: solicitud de '{pending.document}' "
                        f"registrada para el taller {pending.workshop_id}."
                    )
                except ToolExecutionError as e:
                    errors.append(str(e))
                    final_decision = "ACTION_EXECUTION_FAILED"
                    reply = f"Error al ejecutar la acción: {e.message}"
                finally:
                    self.state.clear_pending_action()

                self._record_turn_and_audit(
                    request_id, user_prompt, reply, detected_intent, tools_called, tool_params, tool_results, final_decision, action_requested, action_executed, errors, t0
                )
                return AgentResponse(
                    response=reply,
                    status="SUCCESS" if action_executed else "ERROR",
                    request_id=request_id,
                    has_pending_action=False,
                )

            else:  # consent == "CANCEL"
                final_decision = "ACTION_CANCELLED"
                self.state.clear_pending_action()
                reply = "Acción cancelada. No se ha enviado nada al taller."
                self._record_turn_and_audit(
                    request_id, user_prompt, reply, detected_intent, [], {}, {}, final_decision, pending.action, False, errors, t0
                )
                return AgentResponse(
                    response=reply,
                    status="SUCCESS",
                    request_id=request_id,
                    has_pending_action=False,
                )

        # ---------------------------------------------------------
        # PASO 3: Comprensión del LLM (Intención y Entidades)
        # ---------------------------------------------------------
        try:
            parsed = self.llm.parse_intent_and_entities(
                user_prompt,
                state_context={
                    "turnos_previos": self.state.turns,
                    "expediente_activo": self.state.active_claim_id,
                },
            )
        except LLMUnavailableError as exc:
            # El modelo es el interprete: sin el no se adivina la peticion.
            # Error controlado, registrado, y ninguna accion.
            errors.append(str(exc))
            final_decision = "LLM_UNAVAILABLE"
            reply = ("No puedo interpretar la petición ahora mismo porque el modelo no está "
                     "disponible. Inténtalo de nuevo en unos minutos.")
            self._record_turn_and_audit(
                request_id, user_prompt, reply, "LLM_UNAVAILABLE", tools_called, tool_params,
                tool_results, final_decision, None, False, errors, t0
            )
            return AgentResponse(response=reply, status="ERROR", request_id=request_id)
        detected_intent = parsed.intent

        # Si el usuario menciona un expediente explícito en este turno, se actualiza el contexto de la conversación
        if parsed.claim_id:
            self.state.active_claim_id = parsed.claim_id

        claim_id = self.state.active_claim_id

        # Si el usuario no ha indicado ningún expediente ni hay ninguno activo en la sesión:
        if not claim_id:
            if parsed.intent in ["CONSULT_CLAIM", "REQUEST_DOCUMENT"]:
                reply = (
                    "Para poder consultar o gestionar el expediente, por favor indícame su número "
                    "(por ejemplo: EXP-10234)."
                )
                final_decision = "MISSING_CLAIM_ID"
            else:
                reply, final_decision = self._redactar(user_prompt, parsed, {}, errors)
                if final_decision == "QUERY_ANSWERED":
                    final_decision = "CONVERSATION_ANSWERED"

            self._record_turn_and_audit(
                request_id, user_prompt, reply, detected_intent, [], {}, {}, final_decision, None, False, errors, t0
            )
            return AgentResponse(
                response=reply,
                status="ERROR" if final_decision == "LLM_UNAVAILABLE" else "SUCCESS",
                request_id=request_id,
                has_pending_action=False,
            )

        # ---------------------------------------------------------
        # PASO 4: Control de Acceso RBAC
        # ---------------------------------------------------------
        if parsed.intent == "REQUEST_DOCUMENT":
            if not SecurityManager.check_tool_permission(self.state.user_role, "prepare_claim_action"):
                final_decision = "RBAC_DENIED"
                errors.append(f"El rol {self.state.user_role.value} no tiene autorización para solicitar acciones.")
                reply = (
                    f"[PERMISOS] Permiso denegado: Tu perfil ({self.state.user_role.value}) tiene acceso de solo lectura "
                    f"y no puede ordenar la solicitud de documentación a talleres."
                )
                self._record_turn_and_audit(
                    request_id, user_prompt, reply, detected_intent, [], {}, {}, final_decision, parsed.target_action, False, errors, t0
                )
                return AgentResponse(response=reply, status="RBAC_DENIED", request_id=request_id)

        # ---------------------------------------------------------
        # PASO 5: Consulta de Información (Herramientas de Lectura)
        # ---------------------------------------------------------
        try:
            # Consulta API de expediente
            tools_called.append("get_claim")
            claim_data = self.tools.get_claim(claim_id, insurer_id=self.state.insurer_id)
            tool_params["get_claim"] = {"claim_id": claim_id}
            tool_results["claim"] = claim_data

            # Si el expediente tiene documentacion pendiente, el historial deja de
            # ser opcional: sin el no se puede afirmar si ya se solicito o no, y el
            # modelo tenderia a decir "no consta" sin haberlo mirado.
            if claim_data.get("missing_documents") and "events" not in parsed.needs:
                parsed.needs.append("events")

            # El modelo declara que lecturas necesita; el codigo concede solo las que
            # el rol tiene permitidas, y deja constancia de las que deniega.
            concedidas = set()
            for need in parsed.needs:
                tool = READ_TOOLS.get(need)
                if tool and SecurityManager.check_tool_permission(self.state.user_role, tool):
                    concedidas.add(need)
                elif tool:
                    errors.append(f"Lectura '{tool}' no permitida para el rol {self.state.user_role.value}.")

            # Consulta API de taller: solo si el modelo la considera necesaria para
            # responder. Para REQUEST_DOCUMENT el codigo la impone igual, porque hace
            # falta saber a que taller se le solicita.
            workshop_id = claim_data.get("workshop_id")
            if workshop_id and "workshop" in concedidas:
                tools_called.append("get_workshop")
                tool_params["get_workshop"] = {"workshop_id": workshop_id, "simulate_error": simulate_workshop_error}
                workshop_data = self.tools.get_workshop(workshop_id, simulate_error=simulate_workshop_error)
                tool_results["workshop"] = workshop_data
                if workshop_data.get("fallback"):
                    errors.append(workshop_data.get("error_detail", "Fallo de comunicación con taller"))

            # Consulta del historico: solo si el modelo la considera necesaria para
            # responder (por ejemplo, para explicar desde cuando falta un documento).
            if "events" in concedidas:
                tools_called.append("get_claim_events")
                events_data = self.tools.get_claim_events(claim_id)
                tool_params["get_claim_events"] = {"claim_id": claim_id}
                tool_results["events"] = events_data

        except (ToolExecutionError, SecurityViolation) as e:
            # SecurityViolation: identificador con caracteres no permitidos.
            errors.append(str(e))
            final_decision = "READ_TOOL_ERROR"
            reply = f"Error al consultar la información del expediente: {e.message}"
            self._record_turn_and_audit(
                request_id, user_prompt, reply, detected_intent, tools_called, tool_params, tool_results, final_decision, None, False, errors, t0
            )
            return AgentResponse(response=reply, status="ERROR", request_id=request_id)

        # ---------------------------------------------------------
        # PASO 6: Validación Determinista y Compuerta de Confirmación
        # ---------------------------------------------------------
        if parsed.intent == "REQUEST_DOCUMENT":
            action_requested = parsed.target_action
            target_doc = parsed.target_document
            missing_docs = claim_data.get("missing_documents", [])

            # Si no se identifico que documento se pide, se pregunta en vez de
            # rechazar un documento inexistente.
            if not target_doc:
                final_decision = "MISSING_DOCUMENT"
                etiquetas = [DOCUMENT_CATALOG.get(d, d) for d in missing_docs]
                reply = (
                    f"No he identificado qué documento quieres solicitar. "
                    + (f"En el expediente {claim_id} está pendiente: {', '.join(etiquetas)}."
                       if etiquetas else f"El expediente {claim_id} no tiene documentación pendiente.")
                )
                self._record_turn_and_audit(
                    request_id, user_prompt, reply, detected_intent, tools_called, tool_params,
                    tool_results, final_decision, action_requested, False, errors, t0
                )
                return AgentResponse(response=reply, status="SUCCESS", request_id=request_id)

            # Validación 1: ¿Falta realmente el documento?
            if target_doc not in missing_docs:
                final_decision = "PRECONDITION_FAILED"
                reply = (
                    f"El documento '{target_doc}' no figura como pendiente en el expediente {claim_id}. "
                    f"Documentación pendiente registrada: {missing_docs or 'Ninguna'}. No se requiere ninguna acción."
                )
                status_code = "SUCCESS"
            else:
                # Validación 2: Comprobación de que no se ejecute dos veces (Idempotencia previa)
                if self.tools.db.is_action_already_executed(claim_id, "REQUEST_DOCUMENT", target_doc):
                    final_decision = "ACTION_ALREADY_SUBMITTED"
                    etiqueta = DOCUMENT_CATALOG.get(target_doc, target_doc)
                    fecha = ""
                    for ev in (tool_results.get("events") or []):
                        if ev.get("event_type") == "DOCUMENT_REQUESTED":
                            iso = str(ev.get("event_date", ""))[:10].split("-")
                            fecha = "/".join(reversed(iso)) if len(iso) == 3 else ""
                            break
                    reply = (
                        f"La solicitud de la {etiqueta} ya se envió al taller {workshop_id}"
                        + (f" el {fecha}" if fecha else "")
                        + ". No la duplico para no generar una segunda petición al taller; "
                        "sigue pendiente de que la aporten."
                    )
                    status_code = "SUCCESS"
                elif (tool_results.get("workshop") or {}).get("status") != "ACTIVE":
                    # Validación 3: el taller tiene que poder verificarse y estar activo.
                    # Si su API no responde, no se propone la acción: ante la duda, no
                    # se prepara una escritura.
                    taller = tool_results.get("workshop") or {}
                    final_decision = "WORKSHOP_UNVERIFIED"
                    motivo = (
                        "el servicio de talleres no responde" if taller.get("fallback")
                        else f"el taller figura como {taller.get('status') or 'desconocido'}"
                    )
                    errors.append(f"Taller {workshop_id} no verificable: {motivo}.")
                    reply = (
                        f"No puedo preparar la solicitud: no he podido verificar el taller {workshop_id} "
                        f"({motivo}). No se ha enviado nada. Inténtalo de nuevo en unos minutos."
                    )
                    status_code = "ERROR"
                else:
                    # PREPARAR ACCIÓN Y SOLICITAR CONFIRMACIÓN EXPLÍCITA (Human-in-the-Loop)
                    final_decision = "CONFIRMATION_REQUIRED"
                    etiqueta = DOCUMENT_CATALOG.get(target_doc, target_doc)
                    prompt_confirm = (
                        f"He comprobado que falta la {etiqueta}. "
                        f"Se va a solicitar al taller {workshop_id}. "
                        f"¿Confirmas que ejecute la acción?"
                    )
                    self.state.set_pending_action(
                        claim_id=claim_id,
                        action="REQUEST_DOCUMENT",
                        document=target_doc,
                        workshop_id=workshop_id,
                        confirmation_prompt=prompt_confirm,
                    )
                    reply = prompt_confirm
                    status_code = "PENDING_CONFIRMATION"

            self._record_turn_and_audit(
                request_id, user_prompt, reply, detected_intent, tools_called, tool_params, tool_results, final_decision, action_requested, False, errors, t0
            )
            return AgentResponse(
                response=reply,
                status=status_code,
                request_id=request_id,
                has_pending_action=self.state.has_pending_action(),
            )

        # ---------------------------------------------------------
        # PASO 7: Redacción de la respuesta de consulta
        # ---------------------------------------------------------
        reply, final_decision = self._redactar(user_prompt, parsed, tool_results, errors)

        self._record_turn_and_audit(
            request_id, user_prompt, reply, detected_intent, tools_called, tool_params, tool_results, final_decision, None, False, errors, t0
        )

        return AgentResponse(
            response=reply,
            status="ERROR" if final_decision == "LLM_UNAVAILABLE" else "SUCCESS",
            request_id=request_id,
            has_pending_action=False,
        )
