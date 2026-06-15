"""Pydantic schemas that constrain the LLM to machine-readable output."""
from typing import List, Optional

from pydantic import BaseModel, Field


class MethodInsight(BaseModel):
    name: str = Field(description="Method name exactly as it appears in the digest")
    description: str = Field(description="One concise sentence: what the method does and why")


class ClassInsight(BaseModel):
    qualified_name: str = Field(description="package.ClassName")
    summary: str = Field(description="1-2 sentence summary of the class's responsibility")
    methods: List[MethodInsight] = Field(default_factory=list,
                                         description="Insights for the public methods")


class ChunkInsights(BaseModel):
    """Map step: extracted from one chunk of class digests."""
    classes: List[ClassInsight]
    module_observations: List[str] = Field(
        default_factory=list,
        description="Notable patterns, risks, or design choices observed in this chunk")


class ProjectOverview(BaseModel):
    """Reduce step: synthesized from all chunk insights plus README/build files."""
    purpose: str = Field(description="What the project is and the problem it solves")
    functionality: List[str] = Field(description="Main functional capabilities")
    architecture: str = Field(description="Architectural style and layering")
    technology_stack: List[str] = Field(description="Key frameworks/libraries and their roles")
    design_patterns: List[str] = Field(description="Recognizable design patterns in use")
    noteworthy_aspects: List[str] = Field(description="Strengths, risks, and other observations")
