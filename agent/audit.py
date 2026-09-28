"""Módulo de trazabilidad y auditoría estructurada (JSONL).

Una entrada por mensaje procesado, suficiente para reconstruir qué pasó:
- request_id / conversation_id
- timestamp
- user_id / user_role
- user_request
- detected_intent
- tools_called
- tool_parameters
- tool_results
- final_decision
- action_requested
- action_executed
- errors
- latency
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field, computed_field

from agent.config import config


class ExecutionAuditLog(BaseModel):
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id: str = Field(default="", description="Identificador único de la sesión conversacional")
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    user_id: str
    user_role: str
    user_request: str
    detected_intent: str
    tools_called: List[str] = Field(default_factory=list)
    tool_parameters: Dict[str, Any] = Field(default_factory=dict)
    tool_results: Dict[str, Any] = Field(default_factory=dict)
    final_decision: str
    action_requested: Optional[str] = None
    action_executed: bool = False
    errors: List[str] = Field(default_factory=list)
    latency_ms: float = 0.0

    @computed_field
    @property
    def latency(self) -> float:
        """Expone la latencia con el nombre `latency`. Se serializa junto a latency_ms."""
        return self.latency_ms


class AuditLogger:
    def __init__(self, log_file: Optional[Path] = None):
        self.log_file = log_file or config.audit_log_file
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.in_memory_logs: List[ExecutionAuditLog] = []

    def record(self, log_entry: ExecutionAuditLog) -> None:
        """Guarda la entrada de auditoría en memoria y la persiste en audit_trail.jsonl."""
        self.in_memory_logs.append(log_entry)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry.model_dump(), ensure_ascii=False) + "\n")
        except Exception as e:
            # Fallback en caso de error de I/O en disco
            print(f"[AUDIT LOG ERROR] No se pudo escribir en el archivo de log: {e}")

    def get_latest_log(self) -> Optional[ExecutionAuditLog]:
        return self.in_memory_logs[-1] if self.in_memory_logs else None


# Instancia singleton del logger de auditoria
audit_logger = AuditLogger()
