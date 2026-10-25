import json
import logging

logger = logging.getLogger("personal_agent.audit")


def audit(event: str, **fields) -> None:
    """Solo metadatos permitidos; nunca prompts, argumentos, cookies o excepciones."""
    allowed = {"run_id", "code", "duration_ms", "input_tokens", "output_tokens", "tool_count"}
    record = {"event": event, **{key: value for key, value in fields.items() if key in allowed}}
    logger.info(json.dumps(record, ensure_ascii=True))
