# Agente de expedientes de reparación

Agente conversacional en terminal que consulta expedientes de reparación de vehículos y, si el usuario lo confirma, pide al taller la documentación que falta.

**Idea principal:** la IA entiende lo que pide el usuario y redacta las respuestas; el código decide qué se puede hacer y lo ejecuta. La IA no tiene acceso directo a los datos ni puede ejecutar acciones.

## Arquitectura y componentes

Esquema: [`architecture_diagram.mermaid`](architecture_diagram.mermaid).

Cada mensaje pasa por estos pasos (`agent/orchestrator.py`) y puede detenerse en cualquiera:

1. **Filtro de seguridad:** si el mensaje intenta manipular al agente o sacar datos masivos, se bloquea antes de llegar a la IA.
2. **Confirmación pendiente:** si el agente acaba de preguntar "¿confirmas?", el mensaje solo puede confirmar o cancelar, y eso lo decide el código.
3. **La IA interpreta:** convierte el mensaje en una ficha: qué quiere el usuario, de qué expediente habla, qué documento pide y qué datos necesita.
4. **El código consulta los datos:** el expediente, y según haga falta el taller y el historial, siempre respetando los permisos del usuario.
5. **El código decide:**
   - Si es una consulta, la IA redacta la respuesta con esos datos. Si la IA afirma haber hecho algo que no ocurrió, el código reemplaza la respuesta.
   - Si es una petición de documento, el código comprueba que falte de verdad, que no se haya pedido antes y que el taller esté disponible, y pide confirmación.
6. **Registro:** cada mensaje queda anotado en `logs/audit_trail.jsonl` (qué se pidió, qué se consultó, qué se decidió, errores y tiempo).

| Archivo | Qué hace |
|---|---|
| `agent/orchestrator.py` | Ordena los pasos anteriores |
| `agent/llm_provider.py` | Conexión con la IA |
| `agent/security.py` | Permisos, filtro de seguridad y lectura de la confirmación |
| `agent/tools.py` | Las 4 operaciones: consultar expediente, taller e historial, y pedir un documento |
| `server/mock_services.py` | Las 3 APIs simuladas: expediente, taller y acciones |
| `server/database.py` | Base de datos SQLite: historial, acciones ya ejecutadas y conversaciones |
| `main.py`, `run_demo.py` | Chat interactivo y demostración automática |

## Tecnologías

- **Python 3.10+**
- **DeepSeek V4 Flash**, a través de OpenRouter, para interpretar y redactar
- **FastAPI** para las APIs simuladas (corren dentro del mismo programa, sin servidor aparte)
- **SQLite** como base de datos
- **Pydantic** para validar datos, **pytest** para las pruebas y **rich** para la terminal

No se usaron frameworks de agentes (LangChain, LangGraph) ni n8n: el flujo es corto y tiene una sola pausa (la confirmación), así que no aportaban nada.

## Decisiones técnicas principales

- **La IA propone, el código decide.** La IA solo interpreta y redacta. Permisos, validaciones, confirmación y ejecución son código. Además, el código descarta cualquier valor de la IA que no esté en una lista conocida (tipos de petición, documentos, formato de expediente).
- **La confirmación la lee el código, no la IA.** Cualquier "no" cancela. Solo cuenta como sí un mensaje que sea únicamente afirmativo ("sí, confirmo"). Ante la duda, vuelve a preguntar: cancelar por error no cuesta nada, confirmar por error enviaría un pedido real.
- **Una acción no se ejecuta dos veces.** Cada pedido se registra con una clave única (expediente + acción + documento). Si se repite, no se vuelve a enviar.
- **Se ejecuta exactamente lo confirmado.** Lo que se envía es lo que se anotó al preguntar "¿confirmas?", no lo que diga el mensaje de confirmación. Al confirmar se vuelven a comprobar los permisos y que el documento siga faltando.
- **Permisos.** Hay dos roles: `VIEWER` solo consulta y `OPERATOR` además puede pedir documentos. Un expediente de otra aseguradora se trata como inexistente, sin revelar que existe.
- **Errores.** Si la API de talleres falla, se intenta hasta 3 veces. Si sigue caída, una consulta se responde con los datos disponibles y un aviso, y una petición de documento no se prepara. Si falla la IA o el historial, no se ejecuta nada y se informa del error.
- **Privacidad.** OpenRouter solo usa proveedores que no guardan los datos enviados.

## Cómo ejecutar

```bash
pip install -r requirements.txt
python main.py        # chat interactivo
python run_demo.py    # demostración: consulta, pedido con confirmación, taller caído, mensaje malicioso
pytest -q             # 9 pruebas automáticas, sin internet ni clave
```

Al arrancar, la aplicación pide una clave de OpenRouter (o la lee de `OPENROUTER_API_KEY`) y la guarda en `.env`. Si se pulsa Enter sin clave, arranca en **modo simulador**: mismos controles de seguridad, pero con una IA de prueba que solo entiende frases simples.

Comandos del chat: `/role VIEWER|OPERATOR` (cambiar rol), `/simerror 500|timeout|off` (simular caída del taller), `/audit` (ver el último registro), `/reset` (reiniciar la demo), `/help`, `/exit`.

## Limitaciones conocidas

- No hay login real: el usuario y la aseguradora son fijos.
- El filtro de seguridad busca frases concretas y se puede esquivar redactando distinto. No es la protección principal: aunque un mensaje pase, la IA no puede ejecutar nada ni listar datos.
- Un documento ya pedido no se puede volver a pedir, aunque vuelva a faltar (por ejemplo, si el taller envía una foto no válida).
- Las APIs y los errores son simulados.
- Hay una sola acción (pedir un documento) y la conversación se guarda en memoria.

## Qué mejoraría para producción

- Login corporativo real y permisos comprobados también por los sistemas de OKORE.
- Clave única por cada pedido confirmado (no por documento), para poder volver a pedir un documento cuando haga falta sin riesgo de duplicados.
- Base de datos PostgreSQL con aislamiento por aseguradora dentro de la propia base.
- Límites de tiempo de espera para APIs lentas, y cortar las llamadas a un servicio que falla repetidamente.
- Un detector de manipulación más completo que una lista de frases, y límites de uso y coste por usuario.
- Métricas de funcionamiento y pruebas automáticas de las reglas de seguridad antes de cambiar de modelo.
- Posibilidad de usar un modelo propio (vLLM, Ollama) cambiando solo la configuración.

## Uso de asistentes de IA

Mi forma de trabajar es desarrollar con IA y validar con pruebas. Definí la arquitectura y las reglas, usé asistentes de IA (Gemini y Claude) para escribir y revisar el código, y comprobé cada escenario con pruebas automáticas y manuales antes de darlo por bueno.
