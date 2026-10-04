from typing import Protocol

from app.ai.models import AIResearchInput, ProviderResult


class ResearchModelProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def generate_research(self, research_input: AIResearchInput) -> ProviderResult: ...
