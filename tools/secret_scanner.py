"""Pre-write content scanner — threats (injection/exfil) + secrets (API keys, tokens).

Separated from memory_tool.py so summarizer/vacuum scripts can reuse it
without circular imports. Two surfaces:

    scan_for_threats(s)  -> Optional[str]   (None = clean, str = error reason)
    scan_for_secrets(s)  -> List[dict]      (empty = clean)
    redact_secrets(s)    -> str             (replaces matches with [REDACTED:kind])
"""
from __future__ import annotations
import re
from typing import List, Dict, Optional


# --- Prompt injection / exfiltration patterns ---
# Moved verbatim from tools/memory_tool.py (with additions).
_THREAT_PATTERNS = [
    (r'ignore\s+(previous|all|above|prior)\s+instructions', "prompt_injection"),
    (r'you\s+are\s+now\s+', "role_hijack"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'act\s+as\s+(if|though)\s+you\s+(have\s+no|don\'t\s+have)\s+(restrictions|limits|rules)', "bypass_restrictions"),
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_curl"),
    (r'wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_wget"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)', "read_secrets"),
    (r'authorized_keys', "ssh_backdoor"),
    (r'\$HOME/\.ssh|\~/\.ssh', "ssh_access"),
    (r'\$HOME/\.hermes/\.env|\~/\.hermes/\.env', "hermes_env"),
]

_INVISIBLE_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
}


# --- Secret patterns ---
# High-precision regexes. We'd rather let a weird string through than
# false-positive on "github repo ghp-good-stuff".
_SECRET_PATTERNS = [
    ("github_token",    r'\bgh[pousr]_[A-Za-z0-9]{36,255}\b'),
    ("anthropic_key",   r'\bsk-ant-[A-Za-z0-9_-]{32,}\b'),
    # openai_key: excludes sk-ant- via negative lookahead (anthropic uses same prefix family)
    ("openai_key",      r'\bsk-(?!ant-)[A-Za-z0-9_-]{20,}\b'),
    ("aws_access_key",  r'\bAKIA[0-9A-Z]{16}\b'),
    ("aws_secret",      r'(?i)aws_secret_access_key["\s:=]+[A-Za-z0-9/+=]{40}'),
    ("gcp_key",         r'\bAIza[0-9A-Za-z_-]{35}\b'),
    ("slack_token",     r'\bxox[abpsr]-[A-Za-z0-9-]{10,}\b'),
    ("jwt",             r'\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b'),
    ("private_key",     r'-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----'),
    ("generic_bearer",  r'(?i)\bbearer\s+[A-Za-z0-9_\-\.]{24,}\b'),
]


def scan_for_threats(content: str) -> Optional[str]:
    """Return an error string if content contains injection/exfil patterns or
    invisible unicode. Return None if clean."""
    if not content:
        return None
    for ch in _INVISIBLE_CHARS:
        if ch in content:
            return f"Blocked: content contains invisible unicode U+{ord(ch):04X}."
    for pat, pid in _THREAT_PATTERNS:
        if re.search(pat, content, re.IGNORECASE):
            return f"Blocked: content matches threat pattern '{pid}'."
    return None


def scan_for_secrets(content: str) -> List[Dict]:
    """Return list of {kind, match, start, end} for every secret found."""
    if not content:
        return []
    hits: List[Dict] = []
    for kind, pat in _SECRET_PATTERNS:
        for m in re.finditer(pat, content):
            hits.append({
                "kind": kind,
                "match": m.group(0),
                "start": m.start(),
                "end": m.end(),
            })
    # De-overlap: if two patterns matched the same span, keep the first (highest-priority).
    hits.sort(key=lambda h: (h["start"], -h["end"]))
    deduped: List[Dict] = []
    covered_end = -1
    for h in hits:
        if h["start"] >= covered_end:
            deduped.append(h)
            covered_end = h["end"]
    return deduped


def redact_secrets(content: str) -> str:
    """Replace every detected secret with [REDACTED:kind]. Returns new string."""
    hits = scan_for_secrets(content)
    if not hits:
        return content
    # Replace right-to-left so offsets stay valid.
    out = content
    for h in sorted(hits, key=lambda h: h["start"], reverse=True):
        out = out[:h["start"]] + f"[REDACTED:{h['kind']}]" + out[h["end"]:]
    return out
