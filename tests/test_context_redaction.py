from newcode.context.redaction import redact_value


def test_redaction_masks_known_values_and_sensitive_keys():
    value = {"Authorization": "Bearer abc", "nested": {"api-key": "x", "text": "abc"}}
    assert redact_value(value, ("abc",)) == {"Authorization": "[REDACTED]", "nested": {"api-key": "[REDACTED]", "text": "[REDACTED]"}}
