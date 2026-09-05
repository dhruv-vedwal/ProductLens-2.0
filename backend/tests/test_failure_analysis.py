from productlens.quality.failure_analysis import aggregate_failure_reports


def test_failure_analysis_groups_codes_owners_and_retry_boundaries():
    result = aggregate_failure_reports([
        {"hard_failures": ["CURSOR_PATH_TARGET_MISMATCH"], "owner_by_failure": {"CURSOR_PATH_TARGET_MISMATCH": "presentation"}, "repair_decision": {"retry_boundary": "rendered-trace"}},
        {"hard_failures": ["CURSOR_PATH_TARGET_MISMATCH", "MISSING_PAGE"], "owner_by_failure": {"MISSING_PAGE": "discovery"}, "repair_decision": {"retry_boundary": "targeted-exploration"}},
    ])
    assert result["failure_count"] == 3
    assert result["failures_by_code"]["CURSOR_PATH_TARGET_MISMATCH"] == 2
    assert result["retries_by_boundary"]["rendered-trace"] == 1
