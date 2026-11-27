import logging
import re


_DOLLAR_AMOUNT = re.compile(r"\$\s*[+-]?\d[\d,]*(?:\.\d+)?")
_KEYED_AMOUNT = re.compile(
    r"((?:equity|cash|balance|session_pnl)[\"']?\s*[:=]\s*)[+-]?\d[\d,]*(?:\.\d+)?",
    re.IGNORECASE,
)
_LABEL = (
    r"(?:[a-z0-9]+[ _-])*(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|"
    r"api[ _-]?token|token|secret|password|passwd|credentials?|private[ _-]?key|"
    r"key[ _-]?id|n8n[ _-]?key|account(?:[ _-]?(?:id|number))?)"
)
_VALUE = (
    r"(?:\"(?:\\[^\r\n]|[^\"\\\r\n])*\"|"
    r"'(?:\\[^\r\n]|[^'\\\r\n])*'|"
    r"(?:(?!\s+\b[a-zA-Z][\w -]*[\"']?\s*[:=])[^\r\n,;&}])+)"
)
_AUTHORIZATION = re.compile(
    r"(\b(?:authorization|proxy-authorization|x-api-key|x-n8n-api-key)\s*:\s*"
    r"(?:(?:bearer|basic)\s+)?)" + _VALUE,
    re.IGNORECASE,
)
_TRACEBACK_SOURCE = re.compile(
    r"(\braise\s+\w+\(\s*['\"][^\r\n]*\b" + _LABEL
    + r"[\"']?\s*[:=]\s*)(.*?)(['\"]\))",
    re.IGNORECASE,
)
_SENSITIVE_FIELD = re.compile(
    r"(\b" + _LABEL + r"[\"']?\s*[:=]\s*)" + _VALUE,
    re.IGNORECASE,
)


class SanitizingFormatter(logging.Formatter):
    def format(self, record):
        text = super().format(record)
        text = _TRACEBACK_SOURCE.sub(r"\1[REDACTED]\3", text)
        text = _AUTHORIZATION.sub(r"\1[REDACTED]", text)
        text = _SENSITIVE_FIELD.sub(r"\1[REDACTED]", text)
        text = _DOLLAR_AMOUNT.sub("$[REDACTED]", text)
        return _KEYED_AMOUNT.sub(r"\1[REDACTED]", text)
