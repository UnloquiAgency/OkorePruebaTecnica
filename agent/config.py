import os
from pathlib import Path
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


class AgentConfig(BaseModel):
    # Rutas
    base_dir: Path = BASE_DIR
    data_dir: Path = BASE_DIR / "data"
    logs_dir: Path = BASE_DIR / "logs"
    audit_log_file: Path = BASE_DIR / "logs" / "audit_trail.jsonl"
    db_file: Path = BASE_DIR / "data" / "okore_claims.db"

    # Reintentos de la API de talleres
    max_retries: int = 2
    retry_backoff_factor: float = 0.3

    # Modelo: cualquier endpoint compatible con la API de OpenAI
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_base_url: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    openrouter_model: str = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash-0731")


config = AgentConfig()
config.data_dir.mkdir(parents=True, exist_ok=True)
config.logs_dir.mkdir(parents=True, exist_ok=True)


def ensure_openrouter_api_key() -> str:
    """Si no hay clave de OpenRouter configurada en entorno o .env, la solicita interactivamente."""
    if config.openrouter_api_key or os.getenv("OPENROUTER_API_KEY"):
        return config.openrouter_api_key

    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.prompt import Prompt

        console = Console()
        console.print(
            Panel(
                "[yellow]No se detectó una clave de API configurada (OPENROUTER_API_KEY).[/yellow]\n\n"
                "Para probar el agente con [bold cyan]DeepSeek real en vivo[/bold cyan], "
                "pega a continuación tu API Key de OpenRouter:\n"
                "[dim](O presiona Enter para usar el simulador local sin gastar tokens)[/dim]",
                title="[bold cyan]Configuración de OpenRouter / DeepSeek[/bold cyan]",
                border_style="yellow",
            )
        )
        entered_key = Prompt.ask("[bold green]API Key de OpenRouter (Enter para simulador)[/bold green]", default="").strip()
        if entered_key:
            config.openrouter_api_key = entered_key
            os.environ["OPENROUTER_API_KEY"] = entered_key
            env_file = config.base_dir / ".env"
            try:
                lines = []
                found = False
                if env_file.exists():
                    for line in env_file.read_text(encoding="utf-8").splitlines():
                        if line.startswith("OPENROUTER_API_KEY="):
                            lines.append(f"OPENROUTER_API_KEY={entered_key}")
                            found = True
                        else:
                            lines.append(line)
                if not found:
                    lines.append(f"OPENROUTER_API_KEY={entered_key}")
                env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
                console.print("[bold green]✔ Clave configurada y guardada en .env local.[/bold green]\n")
            except Exception:
                console.print("[dim green]✔ Clave cargada en memoria para esta sesión.[/dim green]\n")
            return entered_key
    except Exception:
        pass
    return ""

