import math
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

Value = StrictBool | StrictInt | StrictFloat | StrictStr
Kind = Literal["bool", "int", "float", "string", "enum"]

VALUE_TYPES = {
    "bool": (bool,),
    "int": (int,),
    "float": (float, int),
    "string": (str,),
    "enum": (str,),
}


class Parameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    flag: str = Field(pattern=r"^--[a-z][a-z0-9-]*$")
    kind: Kind
    title: str
    description: str
    group: str
    default: Value
    minimum: float | None = None
    maximum: float | None = None
    choices: list[str] = Field(default_factory=list)
    max_length: int = Field(default=80, gt=0, le=1000)
    requires: dict[str, Value] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_definition(self) -> Self:
        if self.kind == "bool" and self.default is not False:
            raise ValueError("A switch mapped to a store_true flag must default to false")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("Minimum exceeds maximum")
        self.validate_value(self.default)
        return self

    def enabled(self, values: dict[str, Value]) -> bool:
        return all(values.get(key) == expected for key, expected in self.requires.items())

    def validate_value(self, value: Value) -> Value:
        if type(value) not in VALUE_TYPES[self.kind]:
            raise ValueError(f"«{self.title}»: неверный тип значения.")
        if self.kind in ("int", "float"):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"«{self.title}»: введите конечное число.")
            if self.minimum is not None and value < self.minimum:
                raise ValueError(f"«{self.title}»: минимум {self.minimum:g}.")
            if self.maximum is not None and value > self.maximum:
                raise ValueError(f"«{self.title}»: максимум {self.maximum:g}.")
        if self.kind == "enum" and value not in self.choices:
            raise ValueError(f"«{self.title}»: выберите значение из списка.")
        if self.kind == "string" and (
            not value.strip() or len(value) > self.max_length or not value.isprintable()
        ):
            raise ValueError(f"«{self.title}»: от 1 до {self.max_length} печатных символов.")
        return value
