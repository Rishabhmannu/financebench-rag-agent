from src.services.candidate_validator import validate_candidates


def test_validator_filters_entity_year_and_relaxes_min_keep():
    candidates = [
        {"content": "A", "metadata": {"company": "apple", "fiscal_year": 2023}},
        {"content": "B", "metadata": {"company": "microsoft", "fiscal_year": 2023}},
        {"content": "C", "metadata": {"company": "apple", "fiscal_year": 2022}},
    ]
    kept, diag = validate_candidates(
        query="apple 2023 revenue",
        candidates=candidates,
        target_company="apple",
        target_fiscal_year=2023,
        min_keep=2,
    )
    # Strict filter yields 1 match, then min_keep relaxation keeps top-2
    assert len(kept) == 2
    assert any(d.get("relaxed") for d in diag)

