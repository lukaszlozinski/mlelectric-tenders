"""Token counting and cost estimation with user approval gate."""
import io
import sys
import logging

# Fix Windows console encoding
if sys.platform == "win32":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logger = logging.getLogger(__name__)

# Pricing per 1M tokens (USD) - as of March 2026
PRICING = {
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0},
}

ESTIMATED_OUTPUT_TOKENS = 12000  # larger output with detailed material summaries per category


def estimate_tokens_simple(text: str) -> int:
    """Rough token estimate: ~4 chars per token for PL/EN mixed text."""
    return len(text) // 4


def estimate_cost(model_name: str, input_tokens: int, output_tokens: int = ESTIMATED_OUTPUT_TOKENS) -> float:
    """Calculate estimated cost in USD."""
    if model_name not in PRICING:
        return 0.0  # local model

    pricing = PRICING[model_name]
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    return input_cost + output_cost


def display_cost_table(providers, system_prompt: str, user_prompt: str) -> list:
    """Display cost table and ask for user approval per model.

    Returns list of approved provider instances.
    """
    print("\n" + "=" * 65)
    print("  SZACUNKOWY KOSZT PRZETWARZANIA")
    print("=" * 65)
    print(f"  {'Model':<20} {'Input tokens':>14} {'Est. output':>12} {'Est. cost':>12}")
    print("  " + "-" * 58)

    estimates = []

    for provider in providers:
        # Get token count
        if hasattr(provider, "count_tokens"):
            try:
                input_tokens = provider.count_tokens(system_prompt, user_prompt)
            except Exception:
                input_tokens = estimate_tokens_simple(system_prompt + user_prompt)
        else:
            input_tokens = estimate_tokens_simple(system_prompt + user_prompt)

        cost = estimate_cost(provider.model_name, input_tokens)
        cost_str = f"${cost:.4f}" if cost > 0 else "$0.00 (local)"

        print(f"  {provider.display_name:<20} {input_tokens:>14,} {ESTIMATED_OUTPUT_TOKENS:>12,} {cost_str:>12}")
        estimates.append((provider, input_tokens, cost))

    print("=" * 65)

    # Ask for approval per model
    approved = []
    for provider, input_tokens, cost in estimates:
        if cost == 0:
            prompt_text = f"\n  Uruchomić {provider.display_name} (local, bez kosztu)? [T/n]: "
        else:
            prompt_text = f"\n  Uruchomić {provider.display_name} (~${cost:.4f})? [T/n]: "

        answer = input(prompt_text).strip().lower()
        if answer in ("", "t", "y", "tak", "yes"):
            approved.append(provider)
            print(f"    ✓ {provider.display_name} — zatwierdzono")
        else:
            print(f"    ✗ {provider.display_name} — pominięto")

    if not approved:
        print("\n  Nie zatwierdzono żadnego modelu. Zakończenie.")

    print()
    return approved


def display_cost_table_pdf(providers, system_prompt: str, user_prompt: str, pdf_paths) -> list:
    """Display cost table for native PDF mode with accurate token counting.

    Returns list of approved provider instances.
    """
    from pathlib import Path

    total_kb = sum(p.stat().st_size for p in pdf_paths) / 1024
    total_pages_est = int(total_kb / 30)  # ~30KB per page rough estimate

    print("\n" + "=" * 65)
    print("  SZACUNKOWY KOSZT PRZETWARZANIA (NATIVE PDF)")
    print(f"  PDFs: {len(pdf_paths)} plikow, {total_kb:.0f} KB, ~{total_pages_est} stron")
    print("=" * 65)
    print(f"  {'Model':<20} {'Input tokens':>14} {'Est. output':>12} {'Est. cost':>12}")
    print("  " + "-" * 58)

    estimates = []

    for provider in providers:
        # Try accurate token counting with PDFs
        if hasattr(provider, "count_tokens_with_pdfs"):
            try:
                input_tokens = provider.count_tokens_with_pdfs(system_prompt, user_prompt, pdf_paths)
            except Exception as e:
                logger.warning(f"PDF token counting failed: {e}")
                # Fallback: ~1500 tokens per page + prompt
                input_tokens = total_pages_est * 1500 + estimate_tokens_simple(system_prompt + user_prompt)
        else:
            input_tokens = total_pages_est * 1500 + estimate_tokens_simple(system_prompt + user_prompt)

        cost = estimate_cost(provider.model_name, input_tokens)
        cost_str = f"${cost:.4f}" if cost > 0 else "$0.00 (local)"

        print(f"  {provider.display_name:<20} {input_tokens:>14,} {ESTIMATED_OUTPUT_TOKENS:>12,} {cost_str:>12}")
        estimates.append((provider, input_tokens, cost))

    print("=" * 65)

    # Ask for approval per model
    approved = []
    for provider, input_tokens, cost in estimates:
        if cost == 0:
            prompt_text = f"\n  Uruchomić {provider.display_name} (local, bez kosztu)? [T/n]: "
        else:
            prompt_text = f"\n  Uruchomić {provider.display_name} (~${cost:.4f})? [T/n]: "

        answer = input(prompt_text).strip().lower()
        if answer in ("", "t", "y", "tak", "yes"):
            approved.append(provider)
            print(f"    ✓ {provider.display_name} — zatwierdzono")
        else:
            print(f"    ✗ {provider.display_name} — pominięto")

    if not approved:
        print("\n  Nie zatwierdzono żadnego modelu. Zakończenie.")

    print()
    return approved
