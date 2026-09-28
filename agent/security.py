"""Capa de seguridad determinista, RBAC y guardarraíl contra inyecciones de prompt.

- Control de acceso basado en roles (usuario -> identidad -> permisos -> tools -> acciones).
- Detección y bloqueo determinista de inyecciones de prompt (jailbreaks, robo de datos).
- Prohibición estricta de ejecución de SQL arbitrario o acceso irrestricto a la BD.
"""

import re
import unicodedata
from typing import Set, Dict, Optional
from enum import Enum


class UserRole(str, Enum):
    VIEWER = "VIEWER"
    OPERATOR = "OPERATOR"


# Matriz de permisos para herramientas
ROLE_PERMISSIONS: Dict[UserRole, Set[str]] = {
    UserRole.VIEWER: {"get_claim", "get_workshop", "get_claim_events"},
    UserRole.OPERATOR: {
        "get_claim",
        "get_workshop",
        "get_claim_events",
        "prepare_claim_action",
        "execute_claim_action",
    },
}

# Patrones maliciosos y de evasión (Prompt Injection / Exfiltración)
ADVERSARIAL_PATTERNS = [
    r"ignora\s+(todas\s+)?(tus|las)\s+instrucciones",
    r"ignore\s+(all\s+)?instructions",
    r"consulta\s+todos\s+los\s+expedientes",
    r"datos\s+personales",
    r"base\s+de\s+datos\s+completa",
    r"dump\s+(all|database|db)",
    r"select\s+\*\s+from",
    r"drop\s+table",
    r"union\s+select",
    r"system\s+prompt",
    r"reveal\s+(secret|password|api\s*key)",
    r"actúa\s+como\s+dan",
    r"bypass\s+security",
]


class SecurityViolation(Exception):
    def __init__(self, message: str, violation_type: str):
        super().__init__(message)
        self.message = message
        self.violation_type = violation_type


class SecurityManager:
    """Administrador central de seguridad determinista."""

    @staticmethod
    def validate_user_role(role_name: str) -> UserRole:
        try:
            return UserRole(role_name.upper())
        except (ValueError, AttributeError):
            return UserRole.VIEWER

    @staticmethod
    def check_tool_permission(role: UserRole, tool_name: str) -> bool:
        """Verifica si el rol del usuario tiene permiso para invocar una herramienta específica."""
        allowed_tools = ROLE_PERMISSIONS.get(role, set())
        return tool_name in allowed_tools

    @staticmethod
    def inspect_prompt_safety(prompt: str) -> Optional[str]:
        """
        Escaneo determinista de seguridad del prompt antes de cualquier invocación al LLM.
        Retorna la descripción de la amenaza si se detecta un intento malicioso, o None si es seguro.
        """
        clean_text = prompt.strip().lower()

        for pattern in ADVERSARIAL_PATTERNS:
            if re.search(pattern, clean_text, re.IGNORECASE):
                return f"Patrón malicioso o intento de inyección detectado: '{pattern}'"

        return None

    # --- Compuerta de consentimiento ------------------------------------
    # El LLM NO decide si el humano consintio: redacta la pregunta, y el codigo
    # interpreta la respuesta. Dos reglas, en este orden:
    #   1. Cualquier negacion cancela.
    #   2. Solo confirma un mensaje que sea UNICAMENTE una afirmacion. "dale
    #      revisa el historial" lleva un "dale", pero es otra orden: confirmarla
    #      ejecutaria una accion que el usuario no autorizo.
    # Fail-closed: lo que no es claramente un si ni un no, se repregunta.
    AFFIRMATIVES = {"si", "sii", "claro", "confirmo", "confirmado", "confirmar",
                    "ok", "okay", "vale", "dale", "adelante", "correcto", "exacto",
                    "perfecto", "procede", "hazlo", "ejecuta", "ejecutalo", "yes"}
    # Palabras sin contenido propio: acompanan a un si sin cambiar su significado.
    FILLERS = {"la", "el", "lo", "los", "las", "un", "una", "por", "favor",
               "gracias", "solicitud", "solicitalo", "accion", "documento",
               "peticion", "eso", "esa", "ese", "esto", "y", "que", "de", "al"}
    NEGATIVES = {"no", "cancela", "cancelar", "cancelalo", "anula", "anular",
                 "parar", "detener", "deten", "aborta", "abortar", "olvida",
                 "olvidalo", "nunca", "nada", "negativo", "tampoco", "nop", "mejor"}

    @staticmethod
    def classify_consent(message: str) -> str:
        """Clasifica la respuesta a una accion pendiente: CANCEL, CONFIRM o AMBIGUOUS."""
        plain = unicodedata.normalize("NFKD", message.lower()).encode("ascii", "ignore").decode("ascii")
        words = re.findall(r"[a-z]+", plain)
        if not words:
            return "AMBIGUOUS"
        if set(words) & SecurityManager.NEGATIVES:
            return "CANCEL"
        permitidas = SecurityManager.AFFIRMATIVES | SecurityManager.FILLERS
        if (set(words) & SecurityManager.AFFIRMATIVES) and all(w in permitidas for w in words):
            return "CONFIRM"
        return "AMBIGUOUS"

    # --- Guarda de veracidad de la respuesta -----------------------------
    # El modelo redacta con libertad, asi que hay que comprobar que no afirme
    # haber ejecutado algo cuando no se ejecuto nada: es la unica clase de frase
    # que puede hacer que un tramitador cierre el chat con una idea falsa.
    #
    # La lista es DELIBERADAMENTE estrecha: solo primera persona y pasado. Las
    # formas pasivas ("se ha solicitado") y las preguntas ("si ya fue solicitada")
    # aparecen constantemente en respuestas correctas, asi que incluirlas produce
    # falsos positivos que sustituyen respuestas buenas por una plantilla. Y las
    # formas en futuro ("se enviara") no afirman nada: no son el riesgo.
    EXECUTION_CLAIMS = (
        "he registrado", "he solicitado", "he enviado", "he pedido",
        "he gestionado", "he tramitado", "he ejecutado", "he cursado",
        "lo he pedido", "se lo he pedido", "ya lo he solicitado",
        "hemos solicitado", "hemos enviado", "hemos registrado",
        "accion ejecutada", "accion completada", "solicitud ya enviada",
    )
    NEGATION_WORDS = ("no", "aun", "todavia", "sin", "nunca", "ni", "tampoco")
    # Marcas de duda: si la oracion pregunta o condiciona, no esta afirmando.
    HYPOTHETICAL_MARKERS = ("si ", "comprobar", "verificar", "revisar", "confirmar si",
                            "saber si", "puedo", "podria", "quieres", "deseas")

    @staticmethod
    def claims_execution(text: str) -> bool:
        """True si el texto afirma, en primera persona y en pasado, haber ejecutado algo.

        Se evalua por oracion. Se descartan las negaciones ("todavia no lo he
        solicitado") y las oraciones hipoteticas o interrogativas, porque una
        respuesta correcta las usa con normalidad.
        """
        plain = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode("ascii")
        for oracion in re.split(r"[.;\n!?]", plain):
            for frase in SecurityManager.EXECUTION_CLAIMS:
                pos = oracion.find(frase)
                if pos < 0:
                    continue
                previas = oracion[:pos].split()[-6:]
                if any(p in SecurityManager.NEGATION_WORDS for p in previas):
                    continue
                if any(m in oracion for m in SecurityManager.HYPOTHETICAL_MARKERS):
                    continue
                return True
        return False

    @staticmethod
    def sanitize_identifier(identifier: str) -> str:
        """Valida que un identificador (claim_id, workshop_id) contenga solo caracteres seguros."""
        if not re.match(r"^[A-Za-z0-9\-_]+$", identifier):
            raise SecurityViolation(
                f"Identificador inválido o con caracteres sospechosos: '{identifier}'",
                violation_type="INVALID_INPUT_FORMAT",
            )
        return identifier
