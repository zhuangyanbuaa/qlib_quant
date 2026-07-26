from quant_system.storage.sqlite import OperationsRegistry


def test_operations_registry_records_model_summary(tmp_path) -> None:
    summary = {
        "run_id": "run-1",
        "model": "ranking_baseline_v1",
        "status": "COMPLETED",
        "promotion_status": "NOT_ELIGIBLE_BASELINE_NOT_BEATEN",
        "config_hash": "abc",
        "report_directory": str(tmp_path / "reports"),
    }

    with OperationsRegistry(tmp_path / "db" / "operations.sqlite") as registry:
        registry.record_model_run(summary)
        runs = registry.latest_model_runs()

    assert runs == [summary]
