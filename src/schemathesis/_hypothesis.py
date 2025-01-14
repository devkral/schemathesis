"""High-level API for creating Hypothesis tests."""
from __future__ import annotations
import asyncio
import warnings
from typing import Any, Callable, Optional

import hypothesis
from hypothesis import Phase
from hypothesis import strategies as st
from hypothesis.errors import HypothesisWarning, Unsatisfiable
from hypothesis.internal.reflection import proxies
from hypothesis_jsonschema._canonicalise import HypothesisRefResolutionError

from .auths import get_auth_storage_from_test
from .generation import DataGenerationMethod, GenerationConfig
from .constants import DEFAULT_DEADLINE
from .exceptions import OperationSchemaError
from .hooks import GLOBAL_HOOK_DISPATCHER, HookContext, HookDispatcher
from .models import APIOperation, Case
from .utils import GivenInput, combine_strategies


def create_test(
    *,
    operation: APIOperation,
    test: Callable,
    settings: hypothesis.settings | None = None,
    seed: int | None = None,
    data_generation_methods: list[DataGenerationMethod],
    generation_config: GenerationConfig | None = None,
    as_strategy_kwargs: dict[str, Any] | None = None,
    _given_args: tuple[GivenInput, ...] = (),
    _given_kwargs: dict[str, GivenInput] | None = None,
) -> Callable:
    """Create a Hypothesis test."""
    hook_dispatcher = getattr(test, "_schemathesis_hooks", None)
    auth_storage = get_auth_storage_from_test(test)
    strategies = []
    for data_generation_method in data_generation_methods:
        strategies.append(
            operation.as_strategy(
                hooks=hook_dispatcher,
                auth_storage=auth_storage,
                data_generation_method=data_generation_method,
                generation_config=generation_config,
                **(as_strategy_kwargs or {}),
            )
        )
    strategy = combine_strategies(strategies)
    _given_kwargs = (_given_kwargs or {}).copy()
    _given_kwargs.setdefault("case", strategy)

    # Each generated test should be a unique function. It is especially important for the case when Schemathesis runs
    # tests in multiple threads because Hypothesis stores some internal attributes on function objects and re-writing
    # them from different threads may lead to unpredictable side-effects.

    @proxies(test)  # type: ignore
    def test_function(*args: Any, **kwargs: Any) -> Any:
        __tracebackhide__ = True
        return test(*args, **kwargs)

    wrapped_test = hypothesis.given(*_given_args, **_given_kwargs)(test_function)
    if seed is not None:
        wrapped_test = hypothesis.seed(seed)(wrapped_test)
    if asyncio.iscoroutinefunction(test):
        wrapped_test.hypothesis.inner_test = make_async_test(test)  # type: ignore
    setup_default_deadline(wrapped_test)
    if settings is not None:
        wrapped_test = settings(wrapped_test)
    existing_settings = _get_hypothesis_settings(wrapped_test)
    if existing_settings is not None:
        existing_settings = remove_explain_phase(existing_settings)
        wrapped_test._hypothesis_internal_use_settings = existing_settings  # type: ignore
        if Phase.explicit in existing_settings.phases:
            wrapped_test = add_examples(wrapped_test, operation, hook_dispatcher=hook_dispatcher)
    return wrapped_test


def setup_default_deadline(wrapped_test: Callable) -> None:
    # Quite hacky, but it is the simplest way to set up the default deadline value without affecting non-Schemathesis
    # tests globally
    existing_settings = _get_hypothesis_settings(wrapped_test)
    if existing_settings is not None and existing_settings.deadline == hypothesis.settings.default.deadline:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", HypothesisWarning)
            new_settings = hypothesis.settings(existing_settings, deadline=DEFAULT_DEADLINE)
        wrapped_test._hypothesis_internal_use_settings = new_settings  # type: ignore


def remove_explain_phase(settings: hypothesis.settings) -> hypothesis.settings:
    # The "explain" phase is not supported
    if Phase.explain in settings.phases:
        phases = tuple(phase for phase in settings.phases if phase != Phase.explain)
        return hypothesis.settings(settings, phases=phases)
    return settings


def _get_hypothesis_settings(test: Callable) -> hypothesis.settings | None:
    return getattr(test, "_hypothesis_internal_use_settings", None)


def make_async_test(test: Callable) -> Callable:
    def async_run(*args: Any, **kwargs: Any) -> None:
        loop = asyncio.get_event_loop()
        coro = test(*args, **kwargs)
        future = asyncio.ensure_future(coro, loop=loop)
        loop.run_until_complete(future)

    return async_run


def add_examples(test: Callable, operation: APIOperation, hook_dispatcher: HookDispatcher | None = None) -> Callable:
    """Add examples to the Hypothesis test, if they are specified in the schema."""
    try:
        examples: list[Case] = [get_single_example(strategy) for strategy in operation.get_strategies_from_examples()]
    except (OperationSchemaError, HypothesisRefResolutionError, Unsatisfiable) as exc:
        # Invalid schema:
        # In this case, the user didn't pass `--validate-schema=false` and see an error in the output anyway,
        # and no tests will be executed. For this reason, examples can be skipped
        # Recursive references: This test will be skipped anyway
        # Unsatisfiable:
        # The underlying schema is not satisfiable and test will raise an error for the same reason.
        # Skipping this exception here allows us to continue the testing process for other operations.
        # Still, we allow running user-defined hooks
        examples = []
        if isinstance(exc, Unsatisfiable):
            add_unsatisfied_example_mark(test, exc)
    context = HookContext(operation)  # context should be passed here instead
    GLOBAL_HOOK_DISPATCHER.dispatch("before_add_examples", context, examples)
    operation.schema.hooks.dispatch("before_add_examples", context, examples)
    if hook_dispatcher:
        hook_dispatcher.dispatch("before_add_examples", context, examples)
    for example in examples:
        test = hypothesis.example(case=example)(test)
    return test


def add_unsatisfied_example_mark(test: Callable, exc: Unsatisfiable) -> None:
    test._schemathesis_unsatisfied_example = exc  # type: ignore


def has_unsatisfied_example_mark(test: Callable) -> bool:
    return hasattr(test, "_schemathesis_unsatisfied_example")


def get_unsatisfied_example_mark(test: Callable) -> Optional[Unsatisfiable]:
    return getattr(test, "_schemathesis_unsatisfied_example", None)


def get_single_example(strategy: st.SearchStrategy[Case]) -> Case:
    examples: list[Case] = []
    add_single_example(strategy, examples)
    return examples[0]


def add_single_example(strategy: st.SearchStrategy[Case], examples: list[Case]) -> None:
    @hypothesis.given(strategy)  # type: ignore
    @hypothesis.settings(  # type: ignore
        database=None,
        max_examples=1,
        deadline=None,
        verbosity=hypothesis.Verbosity.quiet,
        phases=(hypothesis.Phase.generate,),
        suppress_health_check=list(hypothesis.HealthCheck),
    )
    def example_generating_inner_function(ex: Case) -> None:
        examples.append(ex)

    example_generating_inner_function()
