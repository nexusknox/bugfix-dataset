"""A small GitHub API client: token, paging, rate limits and a disk cache.

The cache matters: mining makes thousands of requests, and a re-run (after
fixing a bug in the pipeline, say) should cost no API quota at all.
"""

import hashlib
import json
import logging
import time
from pathlib import Path

import requests

API = "https://api.github.com"
MAX_WAIT_SECONDS = 15 * 60


class GitHubError(Exception):
    """A friendly message for the person running the pipeline."""


class GitHubClient:
    def __init__(self, token: str | None, cache_dir: Path | None = Path("cache"), session=None, sleep=time.sleep):
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.cache_dir = cache_dir
        self.sleep = sleep
        self.requests_made = 0

    # ------------------------------------------------------------- caching

    def _cache_path(self, url: str, params: dict | None, accept: str) -> Path | None:
        if self.cache_dir is None:
            return None
        key = json.dumps([url, sorted((params or {}).items()), accept])
        return self.cache_dir / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")

    # ------------------------------------------------------------ requests

    def _request(self, url: str, params: dict | None = None, accept: str | None = None):
        headers = {"Accept": accept} if accept else {}
        for attempt in range(3):
            response = self.session.get(url, params=params, headers=headers, timeout=30)
            self.requests_made += 1

            if response.status_code in (403, 429) and response.headers.get("X-RateLimit-Remaining") == "0":
                reset = int(response.headers.get("X-RateLimit-Reset", "0"))
                wait = max(1, reset - int(time.time()))
                if wait > MAX_WAIT_SECONDS:
                    raise GitHubError(f"GitHub rate limit reached. It resets in {wait // 60} minutes; "
                                      "run again then (cached results won't be fetched twice).")
                logging.info("Rate limited, waiting %s s", wait)
                self.sleep(wait)
                continue
            if response.status_code in (502, 503, 504):  # GitHub hiccup: try again
                self.sleep(2 ** attempt)
                continue
            if response.status_code == 401:
                raise GitHubError("GitHub rejected the token. Check GITHUB_TOKEN in your .env file.")
            if response.status_code == 404:
                return None
            if response.status_code >= 400:
                raise GitHubError(f"GitHub returned {response.status_code} for {url}")
            return response
        raise GitHubError(f"GitHub kept failing for {url}; try again later.")

    def get_json(self, path: str, params: dict | None = None):
        """GET one API page as JSON (None for 404). Cached on disk."""
        url = path if path.startswith("http") else API + path
        cached = self._cache_path(url, params, "json")
        if cached and cached.exists():
            return json.loads(cached.read_text(encoding="utf-8"))
        response = self._request(url, params)
        data = None if response is None else response.json()
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(data), encoding="utf-8")
        return data

    def get_diff(self, path: str) -> str | None:
        """GET a pull request as a unified diff. Cached on disk."""
        url = API + path
        accept = "application/vnd.github.diff"
        cached = self._cache_path(url, None, accept)
        if cached and cached.exists():
            return json.loads(cached.read_text(encoding="utf-8"))
        response = self._request(url, accept=accept)
        text = None if response is None else response.text
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(json.dumps(text), encoding="utf-8")
        return text

    def graphql(self, query: str, variables: dict) -> dict:
        """POST a GraphQL query. Cached on disk like everything else.
        GraphQL needs a token; it's the only API that shows every way a PR
        can be linked to the issue it fixes."""
        url = API + "/graphql"
        cached = self._cache_path(url, {"q": query, "v": json.dumps(variables, sort_keys=True)}, "graphql")
        if cached and cached.exists():
            return json.loads(cached.read_text(encoding="utf-8"))
        if "Authorization" not in self.session.headers:
            raise GitHubError("Mining needs a GitHub token. Add GITHUB_TOKEN to your .env file.")
        for attempt in range(3):
            response = self.session.post(url, json={"query": query, "variables": variables}, timeout=60)
            self.requests_made += 1
            if response.status_code in (502, 503, 504):
                self.sleep(2 ** attempt)
                continue
            if response.status_code == 401:
                raise GitHubError("GitHub rejected the token. Check GITHUB_TOKEN in your .env file.")
            if response.status_code >= 400:
                raise GitHubError(f"GitHub GraphQL returned {response.status_code}")
            payload = response.json()
            if payload.get("errors"):
                raise GitHubError("GitHub GraphQL error: " + payload["errors"][0].get("message", "unknown"))
            data = payload["data"]
            if cached:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(json.dumps(data), encoding="utf-8")
            return data
        raise GitHubError("GitHub GraphQL kept failing; try again later.")

    def paginate(self, path: str, params: dict | None = None, limit: int = 1000):
        """Yield items across pages (per_page=100) until `limit` items."""
        params = dict(params or {}, per_page=100)
        page, given = 1, 0
        while given < limit:
            items = self.get_json(path, dict(params, page=page))
            if isinstance(items, dict):  # the search API wraps results
                items = items.get("items", [])
            if not items:
                return
            for item in items:
                yield item
                given += 1
                if given >= limit:
                    return
            if len(items) < 100:
                return
            page += 1