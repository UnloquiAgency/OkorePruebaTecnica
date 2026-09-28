"""Herramientas controladas del agente. El modelo no las invoca: las llama el orquestador.
- get_claim
- get_workshop (con reintentos y respuesta de contingencia ante errores 500/timeout)
- get_claim_events (consulta SQL parametrizada por expediente)
- execute_claim_action (única escritura: solo tras confirmación humana, con clave de idempotencia)
"""

import sqlite3
import time
import httpx
from typing import Dict, Any, List, Optional
from fastapi.testclient import TestClient
from server.mock_services import app
from server.database import ClaimDatabase
from agent.config import config
from agent.security import SecurityManager


class ToolExecutionError(Exception):
    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class AgentTools:
    def __init__(self):
        self.db = ClaimDatabase(config.db_file)
        # Cliente TestClient en proceso para garantizar ejecución sin necesidad de levantar servidor externo
        self.client = TestClient(app, raise_server_exceptions=False)

    def get_claim(self, claim_id: str, insurer_id: Optional[str] = None) -> Dict[str, Any]:
        """Consulta los datos del expediente mediante la API.

        Si se pasa insurer_id, un expediente de otra aseguradora devuelve el MISMO
        error que uno inexistente, para no revelar que existe.
        """
        SecurityManager.sanitize_identifier(claim_id)
        response = self.client.get(f"/api/claims/{claim_id}")
        if response.status_code == 404:
            raise ToolExecutionError(f"Expediente '{claim_id}' no encontrado.", status_code=404)
        if response.status_code != 200:
            raise ToolExecutionError(f"Error al consultar expediente: {response.text}", status_code=response.status_code)
        data = response.json()
        if insurer_id and data.get("insurer_id") != insurer_id:
            raise ToolExecutionError(f"Expediente '{claim_id}' no encontrado.", status_code=404)
        return data

    def get_workshop(self, workshop_id: str, simulate_error: Optional[str] = None) -> Dict[str, Any]:
        """
        Consulta información del taller con mecanismo de resiliencia:
        - Reintentos controlados con retroceso exponencial.
        - Si el servicio sigue caído, devuelve una respuesta de contingencia marcada
          (`fallback: True`) en lugar de lanzar: el resto de la consulta sigue siendo
          útil, y el orquestador añade el aviso y registra el error.
        """
        SecurityManager.sanitize_identifier(workshop_id)
        params = {}
        if simulate_error:
            params["simulate_error"] = simulate_error

        last_error_msg = ""
        max_attempts = config.max_retries + 1

        for attempt in range(1, max_attempts + 1):
            try:
                response = self.client.get(f"/api/workshops/{workshop_id}", params=params)
                if response.status_code == 200:
                    data = response.json()
                    data["available"] = True
                    return data
                elif response.status_code == 404:
                    raise ToolExecutionError(f"Taller '{workshop_id}' no existe en el sistema.", status_code=404)
                else:
                    last_error_msg = f"HTTP {response.status_code}: {response.text}"
            except httpx.RequestError as exc:
                last_error_msg = f"Fallo de conexión de red: {str(exc)}"

            # Si falla, aplicar retroceso exponencial antes de reintentar
            if attempt < max_attempts:
                sleep_time = config.retry_backoff_factor * (2 ** (attempt - 1))
                time.sleep(sleep_time)

        # Reintentos agotados: respuesta de contingencia, sin datos inventados
        return {
            "workshop_id": workshop_id,
            "name": f"Taller {workshop_id} (Servicio no disponible)",
            "available": False,
            "status": "UNAVAILABLE",
            "fallback": True,
            "error_detail": f"La API de talleres no responde tras {max_attempts} intentos. Detalle: {last_error_msg}",
            "available_channels": [],
        }

    def get_claim_events(self, claim_id: str) -> List[Dict[str, Any]]:
        """Consulta determinista de eventos en SQLite. El LLM no puede inyectar SQL."""
        SecurityManager.sanitize_identifier(claim_id)
        try:
            return self.db.get_events_for_claim(claim_id)
        except sqlite3.Error as exc:
            raise ToolExecutionError(f"Histórico de eventos no disponible: {exc}", status_code=503) from exc

    def execute_claim_action(
        self,
        claim_id: str,
        action: str,
        document: str,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Ejecuta la mutación sobre el expediente.
        Solo debe llamarse tras la confirmación explícita del usuario.
        """
        SecurityManager.sanitize_identifier(claim_id)
        key = idempotency_key or f"{claim_id}:{action}:{document}"
        headers = {"X-Idempotency-Key": key}
        payload = {"action": action, "document": document}

        response = self.client.post(
            f"/api/claims/{claim_id}/actions",
            json=payload,
            headers=headers,
        )

        if response.status_code != 200:
            raise ToolExecutionError(
                f"Error al ejecutar acción sobre expediente: {response.text}",
                status_code=response.status_code,
            )

        return response.json()
