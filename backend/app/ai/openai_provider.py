from datetime import datetime
from time import perf_counter
from zoneinfo import ZoneInfo

from app.ai.models import (
    AIResearchInput,
    AIResearchModelOutput,
    ProviderResult,
    ProviderUsage,
)
from app.ai.prompts import system_prompt, user_payload

IST = ZoneInfo("Asia/Kolkata")


class OpenAIResearchProvider:
    def __init__(self, api_key: str, model: str, max_retries: int = 1) -> None:
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY is required")
        if not model.strip():
            raise ValueError("AI_RESEARCH_MODEL is required")
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, max_retries=0)
        self._model = model
        self._max_retries = max_retries

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def model_name(self) -> str:
        return self._model

    def generate_research(self, research_input: AIResearchInput) -> ProviderResult:
        requested_at = datetime.now(IST)
        started = perf_counter()
        last_error: Exception | None = None
        for _ in range(self._max_retries + 1):
            try:
                response = self._client.responses.parse(
                    model=self._model,
                    input=[
                        {"role": "system", "content": system_prompt(research_input)},
                        {"role": "user", "content": user_payload(research_input)},
                    ],
                    text_format=AIResearchModelOutput,
                )
                parsed = response.output_parsed
                if parsed is None:
                    raise RuntimeError("OpenAI returned no structured research output")
                usage = getattr(response, "usage", None)
                input_tokens = getattr(usage, "input_tokens", None)
                output_tokens = getattr(usage, "output_tokens", None)
                total_tokens = getattr(usage, "total_tokens", None)
                return ProviderResult(
                    output=AIResearchModelOutput.model_validate(parsed),
                    provider="openai",
                    model=self._model,
                    requested_at=requested_at,
                    responded_at=datetime.now(IST),
                    latency_ms=round((perf_counter() - started) * 1000),
                    usage=ProviderUsage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        total_tokens=total_tokens,
                    ),
                )
            except Exception as exc:
                last_error = exc
        raise RuntimeError("OpenAI structured research generation failed") from last_error
