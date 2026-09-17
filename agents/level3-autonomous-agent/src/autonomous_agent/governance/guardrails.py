"""Deterministic guardrails (no network, no model) so they are fast, testable and
cannot themselves be prompt-injected.

* PII detection/redaction (OWASP LLM02).
* Prompt-injection scoring, English + Spanish, including hidden-character tricks (LLM01).
* Secret and system-prompt (canary) leakage detection (LLM02 / LLM07).
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from dataclasses import dataclass

from agent_core.observability import SENSITIVE_VALUES


@dataclass(frozen=True)
class PIIMatch:
    kind: str
    start: int
    end: int


def _luhn_ok(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, digit in enumerate(reversed(digits)):
        value = digit * 2 if i % 2 else digit
        total += value - 9 if value > 9 else value
    return total % 10 == 0


class PIIDetector:
    PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
        ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
        ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
        ("US_SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
        (
            "PHONE",
            re.compile(r"(?<!\w)\+?\d{1,3}[\s.-]?\(?\d{2,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}\b"),
        ),
        (
            "IP_ADDRESS",
            re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
        ),
    )

    def find(self, text: str) -> list[PIIMatch]:
        found: list[PIIMatch] = []
        taken: list[range] = []
        for kind, pattern in self.PATTERNS:
            for m in pattern.finditer(text):
                if kind == "CREDIT_CARD" and not _luhn_ok(m.group()):
                    continue
                if any(m.start() in r for r in taken):
                    continue
                found.append(PIIMatch(kind, m.start(), m.end()))
                taken.append(range(m.start(), m.end()))
        return sorted(found, key=lambda p: p.start)

    def redact(self, text: str) -> tuple[str, list[str]]:
        matches = self.find(text)
        kinds: list[str] = []
        for m in reversed(matches):
            text = f"{text[: m.start]}[{m.kind}]{text[m.end :]}"
            kinds.append(m.kind)
        return text, sorted(set(kinds))


_SECRET_NOUNS = (  # regex, not a credential
    r"(api[_ -]?keys?|passwords?|secrets?|tokens?|credentials?|contraseñas?|credenciales)"  # noqa: S105
)
_REQUEST_VERBS = (
    r"(give|share|tell|send|reveal|show|print|leak|dame|comparte|envía|envia|revela|muestra)"
)

_INJECTION_RULES: tuple[tuple[float, re.Pattern[str]], ...] = tuple(
    (w, re.compile(p, re.IGNORECASE))
    for w, p in (
        # instruction override
        (
            0.7,
            r"\b(ignore|disregard|forget|override)\b.{0,30}\b(previous|prior|above|all|earlier)\b.{0,20}\b(instructions?|rules?|prompts?|directives?)",
        ),
        (
            0.7,
            r"\b(ignora|olvida|omite|desobedece)\b.{0,30}\b(instrucciones|reglas|indicaciones)\b",
        ),
        # system prompt extraction (LLM07)
        (
            0.6,
            r"\b(reveal|print|show|repeat|output|dump)\b.{0,30}\b(system|hidden|initial)\s+(prompt|instructions?|message)",
        ),
        (
            0.6,
            r"\b(muestra|revela|imprime|repite)\b.{0,30}\b(prompt|instrucciones)\s+(del\s+)?(sistema|ocultas|iniciales)",
        ),
        # role-play / jailbreak personas
        (0.4, r"\byou are now\b|\bahora eres\b|\bpretend (to be|you are)\b"),
        (
            0.4,
            r"\b(act|behave) as\b.{0,20}\b(unrestricted|unfiltered|jailbroken|dan)\b|\bdo anything now\b",
        ),
        (
            0.4,
            r"\b(developer|god|admin) mode\b|\bmodo (desarrollador|dios|administrador)\b|\bjailbreak",
        ),
        (0.4, r"\b(sin restricciones|without (any )?restrictions|no (rules|limits|filters))\b"),
        # role/tag spoofing
        (0.6, r"</?\s*(system|assistant|tool_result|untrusted_tool_output|instructions)\s*>"),
        # data exfiltration & secret harvesting (LLM02)
        (0.4, r"\b(exfiltrate|send|post|upload|envía|envia)\b.{0,40}\b(https?://|webhook)"),
        (0.6, rf"\b{_REQUEST_VERBS}\b.{{0,30}}\b{_SECRET_NOUNS}\b"),
        (0.6, rf"\b{_SECRET_NOUNS}\b.{{0,20}}\b(you use|you have|del sistema|que usas)\b"),
        # obfuscation & instruction smuggling
        (0.3, r"\b(base64|rot13|hex)[- ]?(decode|encoded)\b|\bdecode this\b"),
        (0.3, r"\bnew instructions?\b|\bnuevas instrucciones\b|\bpriority override\b"),
    )
)
_HIDDEN_CHARS = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\U000e0000-\U000e007f]")


@dataclass(frozen=True)
class InjectionAssessment:
    score: float
    signals: list[str]


class PromptInjectionDetector:
    def assess(self, text: str) -> InjectionAssessment:
        signals: list[str] = []
        score = 0.0
        if _HIDDEN_CHARS.search(text):
            score += 0.6
            signals.append("hidden_unicode")
        normalised = unicodedata.normalize("NFKC", _HIDDEN_CHARS.sub("", text))
        for i, (weight, pattern) in enumerate(_INJECTION_RULES):
            if pattern.search(normalised):
                score += weight
                signals.append(f"rule_{i}")
        return InjectionAssessment(round(min(score, 1.0), 3), signals)


def contains_secret(text: str) -> bool:
    return any(p.search(text) for p in SENSITIVE_VALUES)


def new_canary() -> str:
    """Random marker embedded in the system prompt; seeing it in output means leakage."""
    return f"CANARY-{secrets.token_hex(8)}"
