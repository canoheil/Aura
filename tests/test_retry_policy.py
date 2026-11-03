from retry_policy import call_with_retry, extract_status_code, is_transient_error


class Err(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def test_transient_statuses_are_retryable():
    for status in (408, 429, 500, 502, 503, 504):
        assert is_transient_error(Err("temporary", status))


def test_permanent_status_is_not_retryable():
    assert not is_transient_error(Err("bad request", 400))
    assert not is_transient_error(Err("unauthorized", 401))


def test_timeout_text_is_retryable():
    assert is_transient_error(TimeoutError("request timed out"))


def test_retry_stops_after_success():
    calls = []
    def operation():
        calls.append(1)
        if len(calls) < 3:
            raise Err("503 unavailable", 503)
        return "ok"
    assert call_with_retry(operation, operation_name="test", logger=_Logger(), initial_delay=0, max_retries=2) == "ok"
    assert len(calls) == 3


def test_non_transient_error_is_not_retried():
    calls = []
    def operation():
        calls.append(1)
        raise Err("invalid", 400)
    try:
        call_with_retry(operation, operation_name="test", logger=_Logger(), initial_delay=0, max_retries=2)
    except Err:
        pass
    assert len(calls) == 1


def test_status_extraction_supports_response_objects():
    class Response:
        status_code = 503
    class Wrapped(Exception):
        response = Response()
    assert extract_status_code(Wrapped("x")) == 503


class _Logger:
    def warning(self, *args, **kwargs):
        pass
