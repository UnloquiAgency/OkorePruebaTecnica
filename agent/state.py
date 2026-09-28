"""Gestión de estado de la sesión, historial conversacional y compuerta de confirmación humana."""

import uuid
from typing import Optional, Dict, List
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from agent.security import UserRole


class PendingAction(BaseModel):
    claim_id: str
    action: str
    document: str
    workshop_id: str
    idempotency_key: str
    confirmation_prompt: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AgentState(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = "USR-OPERATOR-01"
    user_role: UserRole = UserRole.OPERATOR
    insurer_id: str = "ASEG-A"
    active_claim_id: Optional[str] = None
    pending_action: Optional[PendingAction] = None
    # Ultimos turnos, solo para que el interprete resuelva referencias como
    # "esa foto" o "pedila". No participa en ninguna decision.
    turns: List[Dict[str, str]] = Field(default_factory=list)

    def add_turn(self, user: str, agent: str) -> None:
        self.turns.append({"usuario": user, "agente": agent[:400]})
        del self.turns[:-4]

    def set_pending_action(
        self,
        claim_id: str,
        action: str,
        document: str,
        workshop_id: str,
        confirmation_prompt: str,
    ) -> PendingAction:
        idempotency_key = f"{claim_id}:{action}:{document}"
        self.pending_action = PendingAction(
            claim_id=claim_id,
            action=action,
            document=document,
            workshop_id=workshop_id,
            idempotency_key=idempotency_key,
            confirmation_prompt=confirmation_prompt,
        )
        return self.pending_action

    def clear_pending_action(self) -> None:
        self.pending_action = None

    def has_pending_action(self) -> bool:
        return self.pending_action is not None
