from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

REFER_SHARE_OPTIONS = (0.2, 0.3, 0.4)

class Pregnancy(BaseModel):
    status: Literal["no", "yes", "unknown"] = "no"
    trimester: Optional[Literal[1, 2, 3]] = None

class LabValue(BaseModel):
    value: float
    unit: Optional[str] = None

class ReferenceRangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    low: Optional[float] = None
    high: Optional[float] = None
    unit: Optional[str] = None

class AnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sex: Literal["F", "M"]
    age_years: int = Field(ge=18, le=120, description="Только взрослые: модель и пороги не рассчитаны на детей")
    pregnancy: Pregnancy = Field(default_factory=Pregnancy)
    values: dict[str, Union[LabValue, float]] = Field(description="Код показателя -> значение (и единица)")
    norms: Literal["who", "ru"] = "who"
    mode: Literal["clinical", "benchmark", "auto"] = "clinical"
    lab_reference: str = "default"
    refer_share: Optional[float] = None
    patient_ref: Optional[str] = None
    provenance: Optional[dict[str, Any]] = None
    reference_ranges: Optional[dict[str, ReferenceRangeInput]] = None

    @field_validator("refer_share")
    @classmethod
    def _refer_share_option(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        for option in REFER_SHARE_OPTIONS:
            if abs(float(v) - option) < 1e-9:
                return option
        raise ValueError("refer_share: 0.2, 0.3 или 0.4")

class NormalizationNote(BaseModel):
    analyte: str
    kind: Literal["converted", "unit_assumed", "soft_range", "derived", "ignored"]
    text: str
    original: Optional[float] = None
    value: Optional[float] = None
    from_unit: Optional[str] = None
    to_unit: Optional[str] = None

class UrgentItem(BaseModel):
    code: str
    text: str
    source: str

class Level1(BaseModel):
    anemia: bool
    hemoglobin: float
    threshold_g_l: float
    severity: Optional[Literal["mild", "moderate", "severe"]] = None
    severity_ru: Optional[str] = None
    source: str
    note: Optional[str] = None
    urgent: list[UrgentItem] = Field(default_factory=list)

class ClassProb(BaseModel):
    code: str
    name_ru: str
    p: float

class Level2(BaseModel):
    enabled: bool
    disabled_reason: Optional[str] = None
    mode: Optional[Literal["clinical", "benchmark"]] = None
    completeness: Optional[Literal["cbc", "cbc_iron_crp", "standard", "extended"]] = None
    completeness_ru: Optional[str] = None
    case_group: Optional[str] = None
    case_group_ru: Optional[str] = None
    anemia_class: Optional[str] = None
    anemia_class_ru: Optional[str] = None
    deficiency_cause: Optional[str] = None
    deficiency_cause_ru: Optional[str] = None
    probabilities: dict[str, dict[str, float]] = Field(default_factory=dict)
    plausible: list[ClassProb] = Field(default_factory=list)
    ambiguous: bool = False
    confidence: Optional[Literal["low", "moderate", "high"]] = None
    confidence_ru: Optional[str] = None
    class_note_ru: Optional[str] = None
    case_group_note_ru: Optional[str] = None

class RuleSignal(BaseModel):
    code: str
    text: str
    source: str

class Screening(BaseModel):
    applicable: bool
    reason_not_applicable: Optional[str] = None
    model: Optional[str] = None
    p_ferritin_lt15: Optional[float] = None
    p_ferritin_lt30: Optional[float] = None
    risk: Optional[Literal["usual", "elevated", "high"]] = None
    risk_ru: Optional[str] = None
    explanation_ru: Optional[str] = None
    features: Optional[Literal["full_cbc", "hb_only"]] = None
    group: Optional[str] = None
    validated_group: Optional[bool] = None
    lab_reference: Optional[str] = None
    recommend_ferritin: bool = False
    refer_share: Optional[float] = None

class HiddenDeficiency(BaseModel):
    applicable: bool
    detected: bool = False
    p_any: Optional[float] = None
    by_nutrient: dict[str, float] = Field(default_factory=dict)
    rule_signals: list[RuleSignal] = Field(default_factory=list)
    screening: Optional[Screening] = None
    summary_ru: str = ""

class Flag(BaseModel):
    code: str
    title: str
    text: str
    source: str
    severity: Literal["info", "attention"] = "attention"

class ChecklistItem(BaseModel):
    code: str
    title: str
    status: Literal["excluded", "suspected", "not_checked"]
    detail: str
    analyte: Optional[str] = None

class NextTest(BaseModel):
    analyte: str
    name_ru: str
    reason: str
    kind: Literal["rule", "information"]
    information_gain: Optional[float] = None
    price_rub: Optional[int] = None
    available: bool = True
    source: Optional[str] = None

class Contribution(BaseModel):
    analyte: str
    name_ru: str
    value: float
    unit: str
    direction: Literal["for", "against"]
    weight: float
    text: str

class Explanation(BaseModel):
    top_class: Optional[str] = None
    runner_up: Optional[str] = None
    items_for: list[Contribution] = Field(default_factory=list)
    items_against: list[Contribution] = Field(default_factory=list)
    note: Optional[str] = None

class NormInfo(BaseModel):
    status: Literal["low", "normal", "high", "borderline", "unknown"]
    threshold_text: str = ""
    source: str = ""

class ValueRow(BaseModel):
    analyte: str
    name_ru: str
    value: float
    unit: str
    norm: NormInfo
    contribution: Optional[str] = None
    warning: Optional[str] = None

class ReportSection(BaseModel):
    title: str
    lines: list[str]

class Report(BaseModel):
    headline: str
    sections: list[ReportSection]

class Reports(BaseModel):
    doctor: Report
    patient: Report

class VersionInfo(BaseModel):
    engine: str
    case_model: str
    screen_model: str
    norms: str

class ReferencesInfo(BaseModel):
    set: Literal["default", "lab"] = "default"
    lab_name: Optional[str] = None
    local_analytes: list[str] = Field(default_factory=list)
    warning: Optional[str] = None

class AnalysisResult(BaseModel):
    version: VersionInfo
    patient_ref: Optional[str] = None
    level1: Level1
    level2: Level2
    hidden_deficiency: HiddenDeficiency
    flags: list[Flag] = Field(default_factory=list)
    checklist: list[ChecklistItem] = Field(default_factory=list)
    next_tests: list[NextTest] = Field(default_factory=list)
    explanation: Explanation = Field(default_factory=Explanation)
    values: list[ValueRow] = Field(default_factory=list)
    reports: Reports
    normalization: list[NormalizationNote] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    processing_ms: float = 0.0
    references: Optional[ReferencesInfo] = None

class ErrorBody(BaseModel):
    code: str
    message: str
    field: Optional[str] = None

class InputError(Exception):

    def __init__(self, code: str, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "field": self.field}}

class BatchItem(BaseModel):
    index: int
    patient_ref: Optional[str] = None
    status: Literal["done", "rejected"]
    errors: list[ErrorBody] = Field(default_factory=list)
    result: Optional[AnalysisResult] = None

class BatchSummary(BaseModel):
    total: int
    done: int
    rejected: int
    processing_ms: float
