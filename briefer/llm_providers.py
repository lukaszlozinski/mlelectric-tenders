"""LLM provider abstractions for Ollama and Anthropic."""
import base64
import json
import os
import time
import logging
from pathlib import Path
import requests
import anthropic

# Load .env from project root if it exists
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

logger = logging.getLogger(__name__)


class LLMProvider:
    """Base class for LLM providers."""

    def __init__(self, model_name: str, display_name: str):
        self.model_name = model_name
        self.display_name = display_name
        self.supports_pdf = False

    def generate(self, system_prompt: str, user_prompt: str, pdf_paths: list[Path] | None = None) -> str:
        raise NotImplementedError


class OllamaProvider(LLMProvider):
    """Local Ollama provider."""

    def __init__(self, model_name: str, display_name: str, base_url: str = "http://localhost:11434"):
        super().__init__(model_name, display_name)
        self.base_url = base_url
        self.supports_pdf = False

    def generate(self, system_prompt: str, user_prompt: str, pdf_paths: list[Path] | None = None) -> str:
        logger.info(f"Sending request to Ollama ({self.model_name})...")
        start = time.time()

        response = requests.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "num_predict": 16000,
                },
            },
            timeout=600,
        )
        response.raise_for_status()

        elapsed = time.time() - start
        result = response.json()["message"]["content"]
        logger.info(f"Ollama ({self.model_name}) responded in {elapsed:.1f}s")
        return result

    def is_available(self) -> bool:
        """Check if Ollama is running and model is available."""
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=5)
            models = [m["name"] for m in r.json().get("models", [])]
            return any(self.model_name in m for m in models)
        except Exception:
            return False


class AnthropicProvider(LLMProvider):
    """Anthropic API provider (Claude Sonnet / Opus) with native PDF support."""

    def __init__(self, model_name: str, display_name: str):
        super().__init__(model_name, display_name)
        self.client = anthropic.Anthropic()
        self.supports_pdf = True

    def generate(self, system_prompt: str, user_prompt: str, pdf_paths: list[Path] | None = None) -> str:
        """Generate response, sending PDFs natively if provided."""
        if pdf_paths:
            return self._generate_with_pdfs(system_prompt, user_prompt, pdf_paths)
        else:
            return self._generate_text_only(system_prompt, user_prompt)

    def _generate_with_pdfs(self, system_prompt: str, user_prompt: str, pdf_paths: list[Path]) -> str:
        """Send PDFs as native document blocks — Opus reads them directly."""
        logger.info(f"Sending {len(pdf_paths)} PDFs natively to Anthropic ({self.display_name})...")
        start = time.time()

        # Build content blocks: PDFs first, then the instruction prompt
        content = []

        for pdf_path in pdf_paths:
            pdf_data = pdf_path.read_bytes()
            b64_data = base64.standard_b64encode(pdf_data).decode("utf-8")

            content.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": b64_data,
                },
                "title": pdf_path.name,
            })
            logger.info(f"  Attached: {pdf_path.name} ({len(pdf_data) / 1024:.0f} KB)")

        # Add the text instruction after the PDFs
        content.append({
            "type": "text",
            "text": user_prompt,
        })

        # Stream the response
        result_parts = []
        input_tokens = 0
        output_tokens = 0

        with self.client.messages.stream(
            model=self.model_name,
            max_tokens=16000,
            temperature=0.1,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        ) as stream:
            for text in stream.text_stream:
                result_parts.append(text)

            final = stream.get_final_message()
            input_tokens = final.usage.input_tokens
            output_tokens = final.usage.output_tokens

        elapsed = time.time() - start
        result = "".join(result_parts)
        logger.info(
            f"Anthropic ({self.display_name}) responded in {elapsed:.1f}s "
            f"(input: {input_tokens}, output: {output_tokens} tokens)"
        )
        return result

    def _generate_text_only(self, system_prompt: str, user_prompt: str) -> str:
        """Fallback: text-only generation (for extracted text mode)."""
        logger.info(f"Sending text request to Anthropic ({self.display_name}) via streaming...")
        start = time.time()

        result_parts = []
        input_tokens = 0
        output_tokens = 0

        with self.client.messages.stream(
            model=self.model_name,
            max_tokens=16000,
            temperature=0.1,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        ) as stream:
            for text in stream.text_stream:
                result_parts.append(text)

            final = stream.get_final_message()
            input_tokens = final.usage.input_tokens
            output_tokens = final.usage.output_tokens

        elapsed = time.time() - start
        result = "".join(result_parts)
        logger.info(
            f"Anthropic ({self.display_name}) responded in {elapsed:.1f}s "
            f"(input: {input_tokens}, output: {output_tokens} tokens)"
        )
        return result

    def count_tokens_with_pdfs(self, system_prompt: str, user_prompt: str, pdf_paths: list[Path]) -> int:
        """Count tokens including PDF documents."""
        try:
            content = []
            for pdf_path in pdf_paths:
                pdf_data = pdf_path.read_bytes()
                b64_data = base64.standard_b64encode(pdf_data).decode("utf-8")
                content.append({
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": b64_data,
                    },
                    "title": pdf_path.name,
                })
            content.append({"type": "text", "text": user_prompt})

            result = self.client.messages.count_tokens(
                model=self.model_name,
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
            )
            return result.input_tokens
        except Exception as e:
            logger.warning(f"Token counting with PDFs failed, estimating: {e}")
            # Rough estimate: ~1500 tokens per PDF page, ~30 pages total
            total_kb = sum(p.stat().st_size for p in pdf_paths) / 1024
            return int(total_kb * 50) + len(user_prompt) // 4

    def count_tokens(self, system_prompt: str, user_prompt: str) -> int:
        """Count input tokens using Anthropic's token counting API."""
        try:
            result = self.client.messages.count_tokens(
                model=self.model_name,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return result.input_tokens
        except Exception as e:
            logger.warning(f"Token counting failed, estimating: {e}")
            return (len(system_prompt) + len(user_prompt)) // 4


def build_providers(config: dict) -> list[LLMProvider]:
    """Build provider instances from config."""
    providers = []

    for key, model_cfg in config.get("models", {}).items():
        provider_type = model_cfg["provider"]
        model_name = model_cfg["model"]
        display_name = key

        if provider_type == "ollama":
            base_url = model_cfg.get("base_url", "http://localhost:11434")
            p = OllamaProvider(model_name, display_name, base_url)
            if not p.is_available():
                logger.warning(f"Ollama model '{model_name}' not available, skipping")
                continue
            providers.append(p)

        elif provider_type == "anthropic":
            providers.append(AnthropicProvider(model_name, display_name))

        else:
            logger.warning(f"Unknown provider type: {provider_type}")

    return providers
