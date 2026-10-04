"""clm-lib: agents that edit their own working context (experimental).

Minimal usage (offline, scripted model, Docker sandbox)::

    from pathlib import Path
    from clm_lib import Config, DockerExecutor, Ledger, Runner, ScriptedProvider, generate

    provider = ScriptedProvider([{"action": "final", "answer": {
        "root_cause": "DB_POOL_EXHAUSTED: ...", "required_value": "12",
        "remedy": "RAISE_DB_POOL_LIMIT: ...", "evidence_refs": []}}])
    runner = Runner(Config(), provider, DockerExecutor(), Ledger.open(Path("runs/l.json"), 0.0),
                    price=None, live=False, runs_dir=Path("runs"))
    result = runner.run(generate("dev"), mode="clm")
"""

from .budget import Ledger, ModelPrice, load_prices
from .config import Config, load_config
from .context import ContextLimits, ContextStore, Entry, parse_candidate
from .executor import DockerExecutor, ExecutorUnavailable
from .provider import AnthropicProvider, ModelRequest, ProviderError, ScriptedProvider
from .runner import MODES, Runner, RunResult
from .tasks import INSTANCES, generate, score_answer

__version__ = "0.1.0"

__all__ = [
    "INSTANCES",
    "MODES",
    "AnthropicProvider",
    "Config",
    "ContextLimits",
    "ContextStore",
    "DockerExecutor",
    "Entry",
    "ExecutorUnavailable",
    "Ledger",
    "ModelPrice",
    "ModelRequest",
    "ProviderError",
    "RunResult",
    "Runner",
    "ScriptedProvider",
    "__version__",
    "generate",
    "load_config",
    "load_prices",
    "parse_candidate",
    "score_answer",
]
