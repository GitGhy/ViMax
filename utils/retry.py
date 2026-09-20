import tenacity
import traceback
import logging

import requests
from utils.ghyai import contains_ghyai_error


def provider_retry(*args, **kwargs):
    """光合云聊天不自动重放；其他供应商保留原来的重试行为。"""
    original = kwargs.get("retry", tenacity.retry_if_exception_type())
    def allowed(state):
        if state.outcome is None or not state.outcome.failed:
            return False
        owner = state.args[0] if state.args else None
        model = getattr(owner, "chat_model", None)
        if getattr(model, "_llm_type", None) == "ghyai":
            return False
        return not contains_ghyai_error(state.outcome.exception()) and original(state)
    kwargs["retry"] = allowed
    return tenacity.retry(*args, **kwargs)

def after_func(retry_state: tenacity.RetryCallState) -> None:
    if retry_state.outcome.failed:
        exc = retry_state.outcome.exception()
        logging.warning(f"Retrying {retry_state.fn.__name__} due to {repr(exc)} (Attempt {retry_state.attempt_number})")
        logging.debug(traceback.format_exception(type(exc), exc, exc.__traceback__))


def is_retryable_download_error(exc: BaseException) -> bool:
    """Network errors and 5xx responses are retryable; other HTTP errors (expired
    or invalid URLs, auth failures) will never succeed and must fail fast."""
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        return response is None or response.status_code >= 500
    return isinstance(exc, requests.RequestException)


download_retry = tenacity.retry(
    stop=tenacity.stop_after_attempt(3),
    wait=tenacity.wait_exponential(multiplier=1, max=10),
    retry=tenacity.retry_if_exception(is_retryable_download_error),
    after=after_func,
    reraise=True,
)
