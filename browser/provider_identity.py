"""Best-effort provider identity from the selected Chrome page URL.

This is display metadata only. Detecting a provider does not imply that RelayPilot
has a supported or validated chat adapter for that provider.
"""

from __future__ import annotations

from urllib.parse import urlparse


_KNOWN_DOMAINS: tuple[tuple[str, str], ...] = (
    ("chatgpt.com", "ChatGPT"),
    ("chat.openai.com", "ChatGPT"),
    ("claude.ai", "Claude"),
    ("gemini.google.com", "Gemini"),
    ("deepseek.com", "DeepSeek"),
    ("perplexity.ai", "Perplexity"),
    ("mistral.ai", "Mistral"),
    ("poe.com", "Poe"),
    ("copilot.microsoft.com", "Copilot"),
    ("grok.com", "Grok"),
    ("qwen.ai", "Qwen"),
)

_IGNORED_HOST_LABELS = {"www", "chat", "app", "ai", "beta", "labs", "www2"}


def provider_name_from_url(url: str) -> str:
    """Return a display name for a known provider or a best-effort host label."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        return "Unknown provider"

    for domain, label in _KNOWN_DOMAINS:
        if host == domain or host.endswith(f".{domain}"):
            return label

    labels = [part for part in host.split(".") if part and part not in _IGNORED_HOST_LABELS]
    if not labels:
        return "Unknown provider"

    # For unknown chat subdomains (e.g. chat.example.ai), prefer the brand label
    # over generic host prefixes such as "chat" or "app".
    candidate = labels[0]
    return candidate.replace("-", " ").replace("_", " ").title()
