"""#120: the one-line diagnostic of a failure, safe to log and to store."""
from pydantic import BaseModel, ValidationError

from almali_library_curator.diagnostics import error_diagnostic


class Row(BaseModel):
    a: int
    b: int
    c: int
    d: int
    values: dict[str, int] = {}


def refusal(payload: dict) -> ValidationError:
    try:
        Row.model_validate(payload)
    except ValidationError as error:
        return error
    raise AssertionError("the payload was accepted")


def test_any_other_error_is_its_type_name_only():
    assert error_diagnostic(ValueError("token=secret")) == "ValueError"


def test_at_most_three_errors_are_named_and_the_rest_counted():
    error = refusal({"a": "x1", "b": "x2", "c": "x3", "d": "x4"})
    assert error_diagnostic(error) == (
        "ValidationError: Row a int_parsing; b int_parsing; c int_parsing; +1 more")


def test_a_location_taken_from_the_input_is_masked():
    error = refusal({"a": 1, "b": 2, "c": 3, "d": 4, "values": {"secret key/path": "x"}})
    assert error_diagnostic(error) == "ValidationError: Row values.? int_parsing"


def test_an_identifier_shaped_dictionary_key_is_masked_too():
    error = refusal({"a": 1, "b": 2, "c": 3, "d": 4, "values": {"secret_token_123": "x"}})
    diagnostic = error_diagnostic(error)
    assert diagnostic == "ValidationError: Row values.? int_parsing"
    assert "secret_token_123" not in diagnostic
