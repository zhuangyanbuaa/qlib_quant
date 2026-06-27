import json
from io import StringIO

from quant_system.logging import configure_logging, get_logger


def test_structured_log_contains_context() -> None:
    stream = StringIO()
    configure_logging("INFO", stream=stream)

    get_logger("test").info("pipeline_started", extra={"run_id": "run-123"})

    payload = json.loads(stream.getvalue())
    assert payload["event"] == "pipeline_started"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert payload["run_id"] == "run-123"
    assert payload["timestamp"].endswith("+00:00")
