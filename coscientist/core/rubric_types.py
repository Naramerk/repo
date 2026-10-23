"""Rubric / final_type types (no core imports — safe for domain.py)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FinalType = Literal["verdict", "artifact", "both"]
VALID_FINAL_TYPES: frozenset[str] = frozenset({"verdict", "artifact", "both"})

RubricAggregate = Literal["all_required", "k_of_n"]
RubricItemCheck = Literal["code", "judge"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RubricItem(_Frozen):
    """Atomic binary acceptance item (code check or judge/review)."""

    id: str
    text: str
    required: bool = True
    check: RubricItemCheck = "judge"
    code_kind: str | None = None

    @model_validator(mode="after")
    def _code_kind_required(self) -> RubricItem:
        if self.check == "code" and not self.code_kind:
            raise ValueError(
                f"rubric item {self.id!r}: code_kind required when check=code"
            )
        if self.check == "judge" and self.code_kind is not None:
            raise ValueError(
                f"rubric item {self.id!r}: code_kind only valid when check=code"
            )
        return self


class Rubric(_Frozen):
    """Frozen qualitative acceptance criteria (DESIGN_M1 §6.2)."""

    items: list[RubricItem] = Field(default_factory=list)
    aggregate: RubricAggregate = "all_required"
    k: int | None = None
    digest: str = ""

    @model_validator(mode="after")
    def _aggregate_k(self) -> Rubric:
        if not self.items:
            raise ValueError("rubric must have ≥1 item")
        ids = [it.id for it in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("rubric item ids must be unique")
        if self.aggregate == "k_of_n":
            if self.k is None or self.k < 1:
                raise ValueError("aggregate=k_of_n requires k ≥ 1")
            # k_of_n counts MET among required items only.
            n_req = sum(1 for it in self.items if it.required)
            limit = n_req if n_req else len(self.items)
            if self.k > limit:
                raise ValueError(
                    f"k={self.k} exceeds required item count {limit}"
                )
        return self
