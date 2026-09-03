# base_crawler.py

from __future__ import annotations

import datetime
import gzip
import hashlib
import logging
import urllib.error
import urllib.request
from abc import ABCMeta, abstractmethod
from pathlib import Path
from typing import Any, TypedDict, Union

from bs4 import BeautifulSoup

PROJECT_BASE_DIR = Path(__file__).resolve().parent.parent.parent
ERROR_DUMP_DIR = PROJECT_BASE_DIR / "error"
ERROR_DUMP_MAX_FILES = 20
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

try:
    import lxml  # noqa: F401

    SOUP_PARSER = "lxml"
except ImportError:
    SOUP_PARSER = "html.parser"

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # optional dependency; crawlers fall back to urllib
    curl_requests = None


def make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, SOUP_PARSER)


class BaseArticle(TypedDict):
    article_id: int
    title: str
    category: str
    site_name: str
    board_name: str
    writer_name: str
    crawler_name: str
    url: str
    is_end: bool
    extra: dict[str, Any]


class ArticleCollection(dict[int, BaseArticle]):
    def __init__(self, data: dict[int, BaseArticle] | None = None):
        super().__init__()
        if data is None:
            data = {}
        for k, v in data.items():
            self[k] = v

    def __setitem__(self, __key: Union[int, str], __value: BaseArticle) -> None:
        return super().__setitem__(int(__key), __value)

    def __getitem__(self, __key: int) -> BaseArticle:
        return super().__getitem__(__key)


class BaseCrawler(metaclass=ABCMeta):
    """
    A synchronous crawler using urllib.request with gzip support.

    Instances are meant to live for the whole process so that per-URL
    parse caches (and, in subclasses, HTTP sessions/cookies) survive
    across fetch cycles.
    """

    # Some anti-bot systems (quasarzone) fingerprint the TLS handshake and
    # block Python's default stack from datacenter IPs while letting real
    # browsers through. Subclasses set this to fetch via curl_cffi, which
    # impersonates Chrome's TLS fingerprint. Falls back to urllib silently
    # when curl_cffi is not installed.
    USE_BROWSER_TLS = False
    BROWSER_TLS_IMPERSONATE = "chrome"

    def __init__(self, name: str, url_list: list[str]) -> None:
        self.url_list = url_list
        self.name = name
        self.logger = logging.getLogger(f"crawler.{self.__class__.__name__}")
        self._prev_status = 200  # track repeated error codes
        self._impersonated_session = None

        # Cache of (html hash, parse result) per list URL: most 1-minute
        # polls hit an unchanged page, so parsing can be skipped entirely.
        self._page_hash_by_url: dict[str, str] = {}
        self._page_result_by_url: dict[str, dict[int, BaseArticle]] = {}

        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Encoding": "gzip",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        }

    def get(self) -> "ArticleCollection":
        data = ArticleCollection()
        for url in self.url_list:
            html = self.request(url)
            if not html:
                continue

            digest = hashlib.blake2s(html.encode("utf-8", "replace")).hexdigest()
            if digest == self._page_hash_by_url.get(url):
                cached = self._page_result_by_url.get(url)
                if cached is not None:
                    data.update(cached)
                    continue

            parsed = self.parsing(html)
            self._page_hash_by_url[url] = digest
            self._page_result_by_url[url] = parsed
            data.update(parsed)
        return data

    def request(self, url: str) -> str | None:
        if self.USE_BROWSER_TLS and curl_requests is not None:
            return self._request_impersonated(url)

        req = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status_code = resp.getcode()
                if status_code != 200:
                    if status_code != self._prev_status:
                        self.logger.error(f"Client response error: {status_code} ({url})")
                        self.dump_http_response(resp)
                    else:
                        self.logger.info(f"Client response error [skip]: {status_code} ({url})")
                    self._prev_status = status_code
                    return None
                else:
                    self._prev_status = status_code

                raw_data = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(raw_data) > MAX_RESPONSE_BYTES:
                    self.logger.error(f"Response exceeds {MAX_RESPONSE_BYTES} bytes; discarding ({url})")
                    return None

                content_encoding = str(resp.headers.get("Content-Encoding", "")).lower()
                if "gzip" in content_encoding:
                    try:
                        raw_data = gzip.decompress(raw_data)
                    except OSError:
                        self.logger.error(f"Failed to gunzip response ({url})")
                        return None

                return raw_data.decode("utf-8", errors="replace")

        except urllib.error.HTTPError as e:
            self.logger.error(f"HTTPError: {e.code} {e.reason} ({url})")
            return None
        except urllib.error.URLError as e:
            self.logger.error(f"URLError: {e.reason} ({url})")
            return None
        except Exception as e:
            self.logger.error(f"Exception: {e} ({url})")
            return None

    def _request_impersonated(self, url: str) -> str | None:
        """Fetch with a real-browser TLS fingerprint via curl_cffi."""
        try:
            if self._impersonated_session is None:
                self._impersonated_session = curl_requests.Session(
                    impersonate=self.BROWSER_TLS_IMPERSONATE
                )
            # Let curl_cffi supply the User-Agent/Accept-Encoding that match
            # the impersonated browser; a mismatch is itself a bot signal.
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in ("user-agent", "accept-encoding")
            }
            response = self._impersonated_session.get(
                url, headers=headers, timeout=15, allow_redirects=True
            )
        except Exception as e:
            self.logger.error(f"Impersonated request error: {e} ({url})")
            return None

        status_code = int(response.status_code)
        if status_code != 200:
            if status_code != self._prev_status:
                self.logger.error(f"Client response error: {status_code} ({url})")
            else:
                self.logger.info(f"Client response error [skip]: {status_code} ({url})")
            self._prev_status = status_code
            return None

        self._prev_status = status_code
        if len(response.content) > MAX_RESPONSE_BYTES:
            self.logger.error(f"Response exceeds {MAX_RESPONSE_BYTES} bytes; discarding ({url})")
            return None
        return response.text

    @abstractmethod
    def parsing(self, html: str) -> dict[int, BaseArticle]:
        """Parse HTML => {article_id: BaseArticle}."""
        pass

    def dump_http_response(self, resp: urllib.request.addinfourl):
        """Save raw response to the project-level 'error/' folder for debugging."""
        current_datetime = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            ERROR_DUMP_DIR.mkdir(parents=True, exist_ok=True)

            filename = ERROR_DUMP_DIR / f"{current_datetime}_{self.name}.html"
            content = resp.read(MAX_RESPONSE_BYTES)
            with open(filename, "wb") as f:
                f.write(content)
            self.logger.debug(f"Dumped response binary to {filename}")

            dumps = sorted(
                ERROR_DUMP_DIR.glob("*.html"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for old_dump in dumps[ERROR_DUMP_MAX_FILES:]:
                try:
                    old_dump.unlink()
                except OSError:
                    pass
        except Exception:
            self.logger.debug("Could not dump HTTP response")
