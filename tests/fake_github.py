"""A pretend GitHub for tests: answers requests from a dictionary of routes."""

import json


class FakeResponse:
    def __init__(self, status=200, data=None, text=None, headers=None):
        self.status_code = status
        self._data = data
        self.text = text if text is not None else json.dumps(data)
        self.headers = headers or {}

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, routes: dict):
        self.routes = routes          # "/repos/x/y" -> data, FakeResponse, or list of responses
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        path = url.replace("https://api.github.com", "")
        accept = (headers or {}).get("Accept", "")
        page = (params or {}).get("page", 1)
        self.calls.append((path, page))
        key = f"{path}#diff" if "diff" in accept else path
        if (key, page) in self.routes:
            key = (key, page)
        answer = self.routes.get(key)
        if answer is None:
            return FakeResponse(404, {"message": "Not Found"})
        if isinstance(answer, list) and answer and isinstance(answer[0], FakeResponse):
            return answer.pop(0)
        if isinstance(answer, FakeResponse):
            return answer
        if isinstance(answer, str):
            return FakeResponse(200, None, text=answer)
        return FakeResponse(200, answer)