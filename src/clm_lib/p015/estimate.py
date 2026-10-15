"""Request sizes for the paper and summary arms (`p015-ratio-estimate/1`), independent of guarded-clm.

A request's characters are its messages (serialised as Pydantic AI stores them, without per-message
`instructions`), the instructions actually sent and the tool definitions. Tokens are characters divided by a ratio:

- scripted models (`FunctionModel` and `TestModel`, whose usage is not provider-reported) use a fixed 4 characters
  per token, so offline runs are deterministic;
- other models start at 3 characters per token (clm-lib's historical starting figure) and move halfway towards each
  main call's observed characters per reported input token, as clm-lib's historical runner did.

No provider token-counting call is made. guarded-clm keeps its own estimator; the arms' estimators differ by design.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence

from pydantic_ai.messages import (
    InstructionPart,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
)
from pydantic_ai.models import Model, ModelRequestParameters

ESTIMATE_VERSION = "p015-ratio-estimate/1"
SCRIPTED_CHARS_PER_TOKEN = 4.0
INITIAL_CHARS_PER_TOKEN = 3.0
SCRIPTED_SYSTEMS = frozenset({"function", "test"})


def request_chars(
    messages: Sequence[ModelMessage], params: ModelRequestParameters | None = None
) -> int:
    stripped = [
        dataclasses.replace(m, instructions=None) if isinstance(m, ModelRequest) else m
        for m in messages
    ]
    chars = len(ModelMessagesTypeAdapter.dump_json(stripped))
    if params is not None and params.instruction_parts:
        chars += len(InstructionPart.join(params.instruction_parts) or "")
    if params is not None:
        tools = [
            {"name": t.name, "description": t.description, "parameters": t.parameters_json_schema}
            for t in [*params.function_tools, *params.output_tools]
        ]
        chars += len(json.dumps(tools)) if tools else 0
    return chars


@dataclasses.dataclass
class RatioEstimator:
    chars_per_token: float = INITIAL_CHARS_PER_TOKEN
    observations: int = 0

    @staticmethod
    def real_usage(model: Model) -> bool:
        return model.system not in SCRIPTED_SYSTEMS

    def ratio(self, model: Model) -> float:
        return self.chars_per_token if self.real_usage(model) else SCRIPTED_CHARS_PER_TOKEN

    def tokens(self, model: Model, chars: int) -> int:
        return round(chars / self.ratio(model))

    def observe(self, model: Model, chars: int, reported_input: int) -> None:
        if self.real_usage(model) and chars > 0 and reported_input > 0:
            self.chars_per_token = round(
                0.5 * self.chars_per_token + 0.5 * chars / reported_input, 4
            )
            self.observations += 1
