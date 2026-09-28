"""Módulo de base de datos SQLite para el histórico de eventos de expedientes.

Cumple con el principio de seguridad:
- El LLM NO tiene acceso directo ni puede ejecutar SQL arbitrario.
- Todas las operaciones son consultas estrictamente parametrizadas y tipadas.
"""

import sqlite3
from typing import List, Optional, Dict, Any
from pathlib import Path
from server.schemas import ClaimEvent

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "okore_claims.db"


class ClaimDatabase:
    def __init__(self, db_path: Optional[str | Path] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Histórico de eventos por expediente
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS claim_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_date TEXT NOT NULL,
                    description TEXT NOT NULL,
                    actor TEXT DEFAULT 'SYSTEM',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Registro de acciones ejecutadas: la clave UNIQUE impide ejecutar dos veces
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS action_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    claim_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    document TEXT NOT NULL,
                    workshop_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Mensajes de cada conversación, enlazables con la traza por request_id
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS conversation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    message TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
            """)
            conn.commit()
        self.seed_initial_data()

    def record_chat_message(
        self, conversation_id: str, request_id: str, sender: str, message: str, timestamp: str
    ) -> None:
        """Persiste un mensaje en el historial conversacional vinculado a conversation_id y request_id."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO conversation_history (conversation_id, request_id, sender, message, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conversation_id, request_id, sender, message, timestamp),
            )
            conn.commit()

    def seed_initial_data(self) -> None:
        """Inserta eventos de prueba si la base de datos está vacía."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM claim_events WHERE claim_id = 'EXP-10234'")
            if cursor.fetchone()[0] == 0:
                events = [
                    (
                        "EXP-10234",
                        "CLAIM_CREATED",
                        "2026-09-20T09:00:00",
                        "Apertura del expediente de reparación por parte de la aseguradora.",
                        "INSURER",
                    ),
                    (
                        "EXP-10234",
                        "WORKSHOP_ASSIGNED",
                        "2026-09-21T11:15:00",
                        "Asignación del taller mecánico y chapa T-228 (Talleres AutoMadrid Central).",
                        "SYSTEM",
                    ),
                    (
                        "EXP-10234",
                        "DOC_VALIDATION_FAILED",
                        "2026-09-22T10:32:00",
                        "Peritaje detecta que falta la fotografía clara de la matrícula (PHOTO_PLATE) para aprobar el desmontaje.",
                        "APPRAISER",
                    ),
                ]
                cursor.executemany(
                    """
                    INSERT INTO claim_events (claim_id, event_type, event_date, description, actor)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    events,
                )
                conn.commit()

    def get_events_for_claim(self, claim_id: str) -> List[Dict[str, Any]]:
        """Consulta parametrizada y segura de eventos por expediente. Nunca recibe SQL dinámico."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, claim_id, event_type, event_date, description, actor
                FROM claim_events
                WHERE claim_id = ?
                ORDER BY event_date ASC
                """,
                (claim_id,),
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def add_event(self, event: ClaimEvent) -> int:
        """Registra un nuevo evento de expediente de forma segura."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO claim_events (claim_id, event_type, event_date, description, actor)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event.claim_id, event.event_type, event.event_date, event.description, event.actor),
            )
            conn.commit()
            return cursor.lastrowid

    def check_and_register_action(
        self, idempotency_key: str, claim_id: str, action: str, document: str, workshop_id: str
    ) -> bool:
        """
        Garantiza idempotencia: si la acción con este idempotency_key ya fue ejecutada, retorna False.
        Si es nueva, la registra atómicamente y retorna True.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO action_audit_log (idempotency_key, claim_id, action, document, workshop_id, status)
                    VALUES (?, ?, ?, ?, ?, 'EXECUTED')
                    """,
                    (idempotency_key, claim_id, action, document, workshop_id),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                # Ya existe esa clave de idempotencia
                return False

    def is_action_already_executed(self, claim_id: str, action: str, document: str) -> bool:
        """Verifica si una acción idéntica sobre el mismo documento ya se ejecutó con éxito."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) FROM action_audit_log
                WHERE claim_id = ? AND action = ? AND document = ? AND status = 'EXECUTED'
                """,
                (claim_id, action, document),
            )
            return cursor.fetchone()[0] > 0

    def reset_demo_state(self) -> None:
        """Devuelve la demo a su estado inicial.

        Borra el registro de idempotencia Y los eventos que escribio el agente.
        Las dos cosas juntas: si solo se borrase el registro, el historial seguiria
        mostrando la solicitud anterior y una nueva peticion generaria un duplicado
        real al taller, que es justo lo que la idempotencia existe para evitar.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM action_audit_log")
            cursor.execute("DELETE FROM claim_events WHERE actor = 'AGENT_EXECUTION'")
            conn.commit()
