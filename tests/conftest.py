import json


class FakePage:
    def __init__(self):
        self.closed = False

    def is_closed(self):
        return self.closed


class FakeSession:
    """Stand-in for browser.Session: routes backend calls to a handler.

    The handler receives (method, path, json_body) and returns a response
    dict {status, body, retry_after}. Returning a list replays the entries
    in order, repeating the last one.
    """

    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.page = FakePage()
        self.home_url = "https://chatgpt.com/"
        self.reauth_count = 0
        self.recover_count = 0
        self._replay = {}

    def backend_fetch(self, method, path, want_body=True, json_body=None):
        self.calls.append((method, path, json_body))
        response = self.handler(method, path, json_body)
        if isinstance(response, list):
            key = (method, path)
            index = self._replay.get(key, 0)
            self._replay[key] = index + 1
            response = response[min(index, len(response) - 1)]
        return response

    def clear_authorization(self):
        pass

    def prime_authorization(self, force=False):
        self.reauth_count += 1

    def recover_page(self):
        self.recover_count += 1


def ok(body):
    return {"status": 200, "retry_after": None,
            "body": json.dumps(body)}


def status_only(status, retry_after=None):
    return {"status": status, "retry_after": retry_after, "body": None}
