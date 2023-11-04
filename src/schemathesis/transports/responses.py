from __future__ import annotations

from copy import deepcopy, copy
from json import JSONDecodeError
from typing import Union, TYPE_CHECKING, NoReturn
from .._compat import JSONMixin
from werkzeug.wrappers import Response as BaseResponse

if TYPE_CHECKING:
    from requests import Response, PreparedRequest


class WSGIResponse(BaseResponse, JSONMixin):
    # We store "requests" request to build a reproduction code
    request: PreparedRequest

    def on_json_loading_failed(self, e: JSONDecodeError) -> NoReturn:
        # We don't need a werkzeug-specific exception when JSON parsing error happens
        raise e


def get_payload(response: GenericResponse) -> str:
    from requests import Response

    if isinstance(response, Response):
        return response.text
    return response.get_data(as_text=True)


def copy_response(response: GenericResponse) -> GenericResponse:
    """Create a copy of the given response as far as it makes sense."""
    from requests import Response

    if isinstance(response, Response):
        # Hooks are not copyable. Keep them out and copy the rest
        hooks = None
        if response.request is not None:
            hooks = response.request.hooks["response"]
            response.request.hooks["response"] = []
        copied_response = deepcopy(response)
        if hooks is not None:
            copied_response.request.hooks["response"] = hooks
        copied_response.raw = response.raw
        copied_response.verify = getattr(response, "verify", True)  # type: ignore[union-attr]
        return copied_response
    # Can't deepcopy WSGI response due to generators inside (`response.freeze` doesn't completely help)
    response.freeze()
    copied_response = copy(response)
    copied_response.request = deepcopy(response.request)
    return copied_response


GenericResponse = Union["Response", WSGIResponse]