"""Configuraciones y fixtures compartidos para pytest."""

import pytest
import server.mock_services
from server.database import ClaimDatabase
from agent.orchestrator import ClaimAgentOrchestrator
from agent.tools import AgentTools
from tests.stub_llm import StubBrain


@pytest.fixture
def fresh_agent(tmp_path):
    """Proporciona una instancia limpia del agente con base de datos SQLite temporal y aislada."""
    test_db_path = tmp_path / "test_okore.db"
    db = ClaimDatabase(test_db_path)
    server.mock_services.set_database(db)

    tools = AgentTools()
    tools.db = db

    # Los tests no salen a la red: se inyecta el doble del motor de lenguaje para que
    # la suite sea reproducible y corra sin clave de API.
    agent_brain = StubBrain()

    agent = ClaimAgentOrchestrator(tools=tools, llm_provider=agent_brain)
    agent.reset_state(user_id="USR-TEST-01", role="OPERATOR")
    return agent
