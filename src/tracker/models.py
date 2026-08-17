"""Pydantic models shared across pipeline stages."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Tracked topic areas (readme §Search): the judge scores relevance 0-100 for
# each. "ai" is generic AI (high-recall aid, never sufficient alone).
TOPICS = ("ai", "agi", "asi", "rsi", "x_risk", "regulation")

# a quote engages a topic when its relevance score clears that topic's bar.
# The frontier topics use low bars (any genuine engagement counts); x_risk a
# medium bar; generic AI regulation must be a clear, central focus (60).
RELEVANT = {"agi": 5, "asi": 5, "rsi": 5, "x_risk": 30, "regulation": 60}


class KeywordMatch(BaseModel):
    keyword: str
    lang: str
    start: int
    end: int


class XRiskSubscores(BaseModel):
    """x_risk subcategory relevance 0-100 (readme §Search, after Project Synesis)."""

    misuse: int = Field(0, ge=0, le=100, description="misuse risks (deliberate harmful use)")
    loss_of_control: int = Field(
        0,
        ge=0,
        le=100,
        description="loss of control / uncontrollable AI / alignment failure",
    )
    natsec_stability: int = Field(
        0, ge=0, le=100, description="national security & (geo)strategic stability"
    )
    cbrn: int = Field(
        0, ge=0, le=100, description="chemical/biological/radiological/nuclear uplift"
    )
    socioeconomic: int = Field(
        0, ge=0, le=100, description="civilization-scale socioeconomic disruption"
    )


class RegulationSubscores(BaseModel):
    """regulation subcategory relevance 0-100 (readme §Search, partially after Project Synesis)."""

    export_controls: int = Field(0, ge=0, le=100, description="export controls on e.g. chips")
    standards_certification: int = Field(0, ge=0, le=100, description="standards & certifications")
    auditing: int = Field(0, ge=0, le=100, description="auditing / evaluations of AI systems")
    international_coordination: int = Field(
        0, ge=0, le=100, description="international agreements & coordination"
    )
    military_defense: int = Field(0, ge=0, le=100, description="military & defense uses")
    surveillance: int = Field(0, ge=0, le=100, description="surveillance")
    alignment: int = Field(0, ge=0, le=100, description="mandated alignment / safety requirements")
    adversarial_robustness: int = Field(0, ge=0, le=100, description="adversarial robustness")


class TopicRelevance(BaseModel):
    """Relevance 0-100 per tracked topic area (readme §Search)."""

    ai: int = Field(ge=0, le=100, description="AI generically")
    agi: int = Field(ge=0, le=100, description="AGI / artificial general intelligence")
    asi: int = Field(ge=0, le=100, description="ASI / superintelligence")
    rsi: int = Field(ge=0, le=100, description="recursive self-improvement / singularity / takeoff")
    x_risk: int = Field(
        ge=0,
        le=100,
        description="AI existential / catastrophic risk, loss of control, alignment",
    )
    regulation: int = Field(
        ge=0,
        le=100,
        description="AI regulation / governance / treaties / export controls",
    )
    # subcategory scores (v5 prompt; None on verdicts from earlier prompt versions)
    x_risk_sub: XRiskSubscores | None = None
    regulation_sub: RegulationSubscores | None = None


class AdjudicationVerdict(BaseModel):
    """Structured verdict; field definitions mirror CODEBOOK.md (prompt is the authority)."""

    relevance: TopicRelevance
    rationale: str = Field(description="Two-sentence rationale for the scores")
    quote_span: str = Field(
        description="Verbatim substring of the passage capturing the relevant statement"
    )
    quote_en: str | None = Field(
        default=None,
        description="English translation of quote_span; null when the original is English",
    )
    is_substantive: bool = Field(
        description="≥1 sentence of the speaker's own engagement; not a joke, "
        "bill-title recitation, or pure quotation of a third party"
    )
    speaker_owns_statement: bool = Field(
        description="The statement expresses the speaker's own view, not a quote of someone else"
    )
    quote_type: Literal["direct", "official_paraphrase", "reported"]
    speaker_in_scope: bool = Field(
        description="Speaker is in the frozen per-jurisdiction scope table (lawmaker or senior executive official)"
    )
    trigger_phrases: list[str] = Field(default_factory=list)
    stance: Literal["concerned", "dismissive", "optimistic", "mixed", "neutral"]
    context_note: str = Field(description="One-sentence neutral description of the setting")
    speaker_name: str | None = Field(
        default=None,
        description="Extracted speaker (name + role) when the record metadata has none",
    )

    @property
    def topics(self) -> list[str]:
        """Specific topics this passage engages (generic 'ai' never qualifies alone).

        Each topic clears its own bar in RELEVANT: low for the frontier topics
        (agi/asi/rsi), medium for x_risk, high for generic regulation.
        """
        r = self.relevance
        return [
            t for t in ("agi", "asi", "rsi", "x_risk", "regulation") if getattr(r, t) >= RELEVANT[t]
        ]

    # storage/display name kept from the v3 schema ("concepts" column in quotes)
    @property
    def concepts(self) -> list[str]:
        return self.topics

    @property
    def accept(self) -> bool:
        return (
            bool(self.topics)
            and self.is_substantive
            and self.speaker_owns_statement
            and self.speaker_in_scope
        )


# Refined topic taxonomy (refine stage; the live prompt is whatever
# refine.load_refine_prompt returns). The category *structure* is taken from two
# published classifications rather than invented here; the slugs are our own
# short identifiers, and the definitions the judge sees are the sources' own
# wording as of refine_v3 (LABELS.md §3, §4, and the §11 fidelity audit):
#  - RISK_SUBDOMAINS: the 24 subdomains of the MIT AI Risk Repository domain
#    taxonomy (Slattery et al., https://airisk.mit.edu), Table 2, in source
#    order (1.1 … 7.6). NB 4.2 is cyberattacks/weapons and 4.3 is fraud/scams;
#    v1 and v2 of the prompt had these two numbers transposed;
#  - POLICY_INSTRUMENTS: the governance-strategies tags of the AGORA thematic
#    taxonomy (CSET/ETO, https://eto.tech/dataset-docs/agora-dataset). AGORA's
#    "Input controls" is one tag with four subtags (data/compute × use/
#    circulation); we split it two ways by resource only, and `risk_tiering` is
#    AGORA's broader "Tiering" — both deliberate deviations, see LABELS.md §11c.
RISK_SUBDOMAINS = (
    "unfair_discrimination",  # 1.1
    "toxic_content",  # 1.2
    "unequal_performance",  # 1.3
    "privacy_compromise",  # 2.1
    "ai_system_vulnerabilities",  # 2.2
    "false_information",  # 3.1
    "information_ecosystem_pollution",  # 3.2
    "disinformation_influence_at_scale",  # 4.1
    "cyberattacks_and_weapons",  # 4.2
    "fraud_and_manipulation",  # 4.3
    "overreliance_unsafe_use",  # 5.1
    "loss_of_human_agency",  # 5.2
    "power_centralization",  # 6.1
    "inequality_employment",  # 6.2
    "devaluation_of_human_effort",  # 6.3
    "competitive_dynamics",  # 6.4
    "governance_failure",  # 6.5
    "environmental_harm",  # 6.6
    "misalignment_loss_of_control",  # 7.1
    "dangerous_capabilities",  # 7.2
    "lack_of_robustness",  # 7.3
    "lack_of_transparency",  # 7.4
    "ai_welfare_rights",  # 7.5
    "multi_agent_risks",  # 7.6
)
POLICY_INSTRUMENTS = (
    "convening",
    "disclosure_requirements",
    "evaluation_auditing",
    "governance_development",
    "government_study",
    "government_support",
    "compute_controls",
    "data_controls",
    "licensing_and_registration",
    "new_institution",
    "performance_requirements",
    "pilots_and_testbeds",
    "risk_tiering",
)
# frontier-capability labels kept from the first-stage topics: a quote chiefly
# about AGI/ASI/RSI as such (timelines, capability claims) rather than a
# specific risk or instrument keeps that as its primary display topic.
FRONTIER_TOPICS = ("agi", "asi", "rsi")

# The five facets readers filter the tracker by. The first stage derives them
# from 0-100 relevance scores against RELEVANT's thresholds; from refine_v4 the
# refine judge decides them directly from written definitions instead, so the
# published labels no longer turn on a score sitting one point either side of a
# 5/100 bar. Same names, so both stages' labels stay comparable.
COARSE_TOPICS = ("agi", "asi", "rsi", "x_risk", "regulation")

# Every jurisdiction a quote can be stored under, mapped to its display name
# (LABELS.md §9). This lived in export/viewer.py as a JS literal until that
# module was retired with the site split; the site repo now owns the rendering,
# but the *list* is still ours, so it belongs here beside the other taxonomies.
# tests/test_quote_export.py pins the site's jurisdiction selector against this
# table -- adding a country here means adding it there in the same sitting.
# "WORLD" is deliberately absent: it is a UI-only aggregate, never stored.
JURISDICTION_NAMES = {
    "US": "United States",
    "UK": "United Kingdom",
    "EU": "European Union",
    "DE": "Germany",
    "FR": "France",
    "CN": "China",
    "CA": "Canada",
    "CH": "Switzerland",
    "JP": "Japan",
    "SG": "Singapore",
    "BR": "Brazil",
    "ZA": "South Africa",
    "AU": "Australia",
    "NL": "Netherlands",
    "RU": "Russia",
    "TW": "Taiwan",
    "MX": "Mexico",
    "NATO": "NATO",
    "UN": "United Nations",
}


class RefinementVerdict(BaseModel):
    """Second-stage verdict over an accepted quote (refine.load_refine_prompt).

    Re-decides the coarse topics, classifies the statement against the
    published taxonomies above, and extracts a display_quote that reads
    standalone. The display quote is a splice of verbatim substrings joined by
    " [...] "; refine.py verifies each segment against the source utterance and
    enforces the word cap mechanically, mirroring promote's quote_span guard.

    coarse_topics is absent on verdicts from refine_v1..v3, which did not ask
    for it — readers must treat None as "not judged", not as "none apply".
    """

    coarse_topics: list[str] | None = Field(
        default=None,
        description="Coarse filter facets the statement engages (refine_v4+); "
        "None on verdicts from earlier prompt versions",
    )
    risk_subdomains: list[str] = Field(
        description="MIT AI Risk Repository subdomains the statement substantively engages"
    )
    policy_instruments: list[str] = Field(
        description="AGORA governance strategies the statement discusses or demands"
    )
    primary_topic: str = Field(
        description="Single most central label: a selected tag, a frontier topic, or 'other'"
    )
    rationale: str = Field(
        description="Three sentences: coarse-topic choice; refined-tag choice; "
        "display-quote choice"
    )
    display_quote: str = Field(
        min_length=1,
        description='Verbatim substrings of the source joined by " [...] ", <= 150 words',
    )
    display_quote_en: str | None = Field(
        default=None,
        description="English translation of display_quote; null when the original is English",
    )

    @field_validator("risk_subdomains")
    @classmethod
    def _risks_known(cls, v: list[str]) -> list[str]:
        unknown = [t for t in v if t not in RISK_SUBDOMAINS]
        if unknown:
            raise ValueError(f"unknown risk_subdomains {unknown}; use the slugs from the prompt")
        return v

    @field_validator("policy_instruments")
    @classmethod
    def _instruments_known(cls, v: list[str]) -> list[str]:
        unknown = [t for t in v if t not in POLICY_INSTRUMENTS]
        if unknown:
            raise ValueError(f"unknown policy_instruments {unknown}; use the slugs from the prompt")
        return v

    @field_validator("coarse_topics")
    @classmethod
    def _coarse_known(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        unknown = [t for t in v if t not in COARSE_TOPICS]
        if unknown:
            raise ValueError(f"unknown coarse_topics {unknown}; use only {list(COARSE_TOPICS)}")
        # canonical order + dedup: comparing two judges is set equality, so
        # normalise here instead of at every call site
        return [t for t in COARSE_TOPICS if t in set(v)]

    @model_validator(mode="after")
    def _primary_selected(self):
        # `x_risk` / `regulation` are coarse topics but deliberately NOT part of
        # the primary_topic vocabulary (LABELS.md §5): primary prefers the
        # specific over the generic, and each of these two has a specific
        # counterpart among the subdomains and instruments. Judges reach for them
        # anyway now that refine_v4 puts the slugs in front of them, so demote to
        # the specific tag the judge itself chose rather than lose the verdict.
        if self.primary_topic == "x_risk":
            self.primary_topic = self.risk_subdomains[0] if self.risk_subdomains else "other"
        elif self.primary_topic == "regulation":
            self.primary_topic = (
                self.policy_instruments[0]
                if self.policy_instruments
                else (self.risk_subdomains[0] if self.risk_subdomains else "other")
            )
        # naming a valid slug as primary implies membership in its list — judges
        # regularly do this, so coerce instead of failing the whole verdict
        if self.primary_topic in RISK_SUBDOMAINS and self.primary_topic not in self.risk_subdomains:
            self.risk_subdomains.append(self.primary_topic)
        elif (
            self.primary_topic in POLICY_INSTRUMENTS
            and self.primary_topic not in self.policy_instruments
        ):
            self.policy_instruments.append(self.primary_topic)
        elif self.primary_topic in FRONTIER_TOPICS and self.coarse_topics is not None:
            # same coercion for the frontier labels: calling a statement chiefly
            # about AGI/ASI/RSI while leaving that tag out of coarse_topics is a
            # contradiction the judges make often enough to be worth absorbing
            if self.primary_topic not in self.coarse_topics:
                self.coarse_topics = [
                    t for t in COARSE_TOPICS if t in {*self.coarse_topics, self.primary_topic}
                ]
        allowed = (
            *self.risk_subdomains,
            *self.policy_instruments,
            *FRONTIER_TOPICS,
            "other",
        )
        if self.primary_topic not in allowed:
            raise ValueError(
                f"primary_topic {self.primary_topic!r} must be a taxonomy slug, "
                f"a frontier topic {FRONTIER_TOPICS}, or 'other'"
            )
        return self


class SkepticVerdict(BaseModel):
    """Adversarial re-judgement: argue the quote does NOT meet the codebook."""

    refutation_attempt: str = Field(description="Strongest argument that this fails the codebook")
    refuted: bool = Field(description="True if the refutation succeeds")
    failed_criteria: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
