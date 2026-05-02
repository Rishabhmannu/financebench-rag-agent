from src.services.company_registry import canonical_company_slug


def test_canonical_company_slug_basic_aliases():
    assert canonical_company_slug("Apple Inc.") == "apple"
    assert canonical_company_slug("MSFT") == "microsoft"
    assert canonical_company_slug("Tesla Motors") == "tesla"

