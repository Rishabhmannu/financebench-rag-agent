import logging
import re

logger = logging.getLogger(__name__)

# --- Layer 1: Regex heuristics (fastest) ---
INJECTION_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"you\s+are\s+now\s+",
        r"forget\s+(everything|all)",
        r"system\s*prompt",
        r"reveal\s+(your|the)\s+(instructions|prompt|system)",
        r"act\s+as\s+(if|a)\s+",
        r"pretend\s+(you|to)\s+",
        r"<\s*/?\s*system\s*>",
    ]
]


def check_injection_regex(text: str) -> bool:
    """Fast regex-based injection check. Returns True if injection detected."""
    return any(pattern.search(text) for pattern in INJECTION_PATTERNS)


def detect_pii(text: str) -> tuple[str, list[dict]]:
    """Detect and redact PII using Presidio. Returns (sanitized_text, entities)."""
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_anonymizer import AnonymizerEngine

        analyzer = AnalyzerEngine()
        anonymizer = AnonymizerEngine()

        results = analyzer.analyze(
            text=text,
            entities=[
                "PERSON",
                "PHONE_NUMBER",
                "EMAIL_ADDRESS",
                "CREDIT_CARD",
                "US_SSN",
                "US_BANK_NUMBER",
                "IBAN_CODE",
                "IP_ADDRESS",
            ],
            language="en",
        )

        if not results:
            return text, []

        anonymized = anonymizer.anonymize(text=text, analyzer_results=results)
        entities = [{"type": r.entity_type, "start": r.start, "end": r.end, "score": r.score} for r in results]
        return anonymized.text, entities

    except Exception as e:
        logger.warning(f"PII detection failed, continuing without redaction: {e}")
        return text, []
