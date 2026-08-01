from __future__ import annotations

import os
import re
import time
from typing import Dict, List
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests

try:
    from .base_crawler import BaseArticle, BaseCrawler, make_soup
except ImportError:
    from base_crawler import BaseArticle, BaseCrawler, make_soup

try:
    from ..deal_key import encode_deal_key
except ImportError:
    from deal_key import encode_deal_key


FMKOREA_BOARD_ID = "hotdeal"
FMKOREA_SITE_CODE = "fmkorea"
FMKOREA_BASE_URL = "https://www.fmkorea.com"
FMKOREA_LIST_URL = "https://www.fmkorea.com/index.php?mid=hotdeal&listStyle=list"
FMKOREA_BLOCK_STATUS_CODES = {403, 429, 430}
FMKOREA_SECURITY_MARKERS = (
    "에펨코리아 보안 시스템",
    "/mc/mc.php",
    "자동 차단",
)
FMKOREA_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
# Serve the icon PNG directly from gstatic (no redirect hop for Discord).
FMKOREA_LOGO_URL = (
    "https://t1.gstatic.com/faviconV2"
    "?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL"
    "&url=https://www.fmkorea.com&size=64"
)
PLAYWRIGHT_COOLDOWN_SECONDS = 300.0
DETAIL_CACHE_MAX_ENTRIES = 500


def _normalize_category_token(raw: str) -> str:
    return re.sub(r"\s+", "", str(raw or "")).casefold()


def _strip_leading_bracket_tags(title: str) -> str:
    return re.sub(r"^(?:\[[^\]]+\]\s*)+", "", str(title or "").strip()).strip()


def _extract_price_from_title(title: str) -> str:
    chunks = re.findall(r"\(([^()]*)\)", str(title or ""))
    for chunk in chunks:
        text = str(chunk).strip()
        if not text:
            continue
        if any(token in text for token in ("원", "$", "€", "¥", "무료", "공짜", "무배")):
            return text
    return ""


def normalize_fmkorea_category(raw_category: str, title: str) -> str:
    merged = f"{raw_category} {title}"
    token = _normalize_category_token(merged)
    if not token:
        return "기타"

    if any(k in token for k in ("식품", "먹거리", "음료", "커피", "건강식품")):
        return "식품"
    if any(k in token for k in ("생활", "주방", "욕실", "청소", "세제", "리빙")):
        return "생활용품"
    if any(k in token for k in ("게임", "steam", "스팀", "닌텐도", "xbox", "ps5", "ps4")):
        return "게임"
    if any(k in token for k in ("pc", "cpu", "그래픽", "ssd", "메모리", "하드웨어")):
        return "PC"
    if any(k in token for k in ("휴대폰", "폰", "스마트폰", "태블릿", "이어폰", "헤드폰", "전자")):
        return "전자제품"
    if any(k in token for k in ("가전", "tv", "모니터", "냉장고", "세탁기", "청소기")):
        return "가전"
    if any(k in token for k in ("패션", "의류", "신발", "운동화", "맨투맨", "후드", "바지")):
        return "의류"
    if any(k in token for k in ("화장품", "뷰티", "스킨", "로션", "선크림", "향수")):
        return "화장품"
    if any(k in token for k in ("상품권", "쿠폰", "기프티콘", "포인트", "적립")):
        return "상품권"
    return "기타"


class FMKoreaCrawler(BaseCrawler):
    def __init__(self, name: str, url_list: list[str]) -> None:
        super().__init__(name=name, url_list=url_list)
        self._session = requests.Session()
        self._browser_fallback_enabled = (
            str(os.getenv("FMKOREA_BROWSER_FALLBACK", "1")).strip() != "0"
        )
        self._playwright_cooldown_until = 0.0
        self._warmed_up = False
        self._detail_image_cache: dict[str, str | None] = {}
        self._configure_headers()
        self._configure_proxy_from_env()
        self._configure_cookie_from_env()

    def _configure_headers(self) -> None:
        self.headers.update(
            {
                "User-Agent": FMKOREA_DESKTOP_USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
                "Connection": "keep-alive",
                "Referer": FMKOREA_BASE_URL + "/",
            }
        )
        self.headers.pop("Accept-Encoding", None)

    def _configure_proxy_from_env(self) -> None:
        proxy = str(os.getenv("FMKOREA_PROXY", "")).strip()
        if not proxy:
            return
        self._session.proxies = {"http": proxy, "https": proxy}

    def _configure_cookie_from_env(self) -> None:
        raw_cookie = str(os.getenv("FMKOREA_COOKIE", "")).strip()
        if not raw_cookie:
            return

        for chunk in raw_cookie.split(";"):
            if "=" not in chunk:
                continue
            name, value = chunk.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not name:
                continue
            self._session.cookies.set(name, value, domain=".fmkorea.com", path="/")
            self._session.cookies.set(name, value, domain="www.fmkorea.com", path="/")

    @staticmethod
    def _looks_like_security_page(html: str) -> bool:
        return any(marker in html for marker in FMKOREA_SECURITY_MARKERS)

    @staticmethod
    def _looks_like_profile_icon(url: str) -> bool:
        low = str(url or "").lower()
        return "/modules/point/icons/" in low or "/member_signature/" in low

    @staticmethod
    def _append_ddos_check_only(url: str) -> str:
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["ddosCheckOnly"] = "1"
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                urlencode(query),
                parsed.fragment,
            )
        )

    def _apply_security_cookie_hint(self, html: str) -> None:
        match = re.search(r"escape\('([0-9a-f]{32})'\)", html)
        if match is None:
            return
        value = match.group(1)
        for domain in (".fmkorea.com", "www.fmkorea.com"):
            self._session.cookies.set("lite_year", value, domain=domain, path="/")
            self._session.cookies.set("g_lite_year", value, domain=domain, path="/")

    def _request_with_session(self, url: str, timeout: float = 15.0) -> requests.Response | None:
        try:
            return self._session.get(
                url,
                headers=self.headers,
                timeout=timeout,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            self.logger.error(f"Request error: {e} ({url})")
            return None

    def _extract_html_if_ok(self, response: requests.Response, url: str) -> str | None:
        status_code = int(response.status_code)
        response.encoding = response.apparent_encoding or response.encoding or "utf-8"
        html = response.text

        if status_code == 200 and not self._looks_like_security_page(html):
            self._prev_status = status_code
            return html

        if status_code in FMKOREA_BLOCK_STATUS_CODES:
            # Keep this as info because block->fallback is expected in many environments.
            self.logger.info(f"Blocked response: {status_code} ({url})")
        elif status_code != self._prev_status:
            self.logger.error(f"Client response error: {status_code} ({url})")
        else:
            self.logger.info(f"Client response error [skip]: {status_code} ({url})")
        self._prev_status = status_code
        return None

    def _fetch_detail_html_quiet(self, url: str) -> str | None:
        response = self._request_with_session(url)
        if response is None:
            return None
        status_code = int(response.status_code)
        response.encoding = response.apparent_encoding or response.encoding or "utf-8"
        html = response.text
        if status_code == 200 and not self._looks_like_security_page(html):
            return html
        return None

    def extract_primary_image_from_detail(self, detail_url: str) -> str | None:
        if detail_url in self._detail_image_cache:
            return self._detail_image_cache[detail_url]
        if len(self._detail_image_cache) >= DETAIL_CACHE_MAX_ENTRIES:
            self._detail_image_cache.clear()

        image_url = self._extract_primary_image_uncached(detail_url)
        self._detail_image_cache[detail_url] = image_url
        return image_url

    def _extract_primary_image_uncached(self, detail_url: str) -> str | None:
        html = self._fetch_detail_html_quiet(detail_url)
        if not html:
            return None

        soup = make_soup(html)
        selectors = ("div.rd_body img", "div.xe_content img", "article img")
        for selector in selectors:
            for image_tag in soup.select(selector):
                src = (
                    image_tag.get("src")
                    or image_tag.get("data-src")
                    or image_tag.get("data-original")
                )
                if not src:
                    continue
                absolute_url = urljoin(FMKOREA_BASE_URL, str(src))
                lower = absolute_url.lower()
                if "/files/attach/" not in lower:
                    continue
                if self._looks_like_profile_icon(lower):
                    continue
                return absolute_url
        return None

    def _request_with_playwright(self, url: str) -> str | None:
        if not self._browser_fallback_enabled:
            return None

        # Chromium cold-starts are expensive; after a failed attempt, skip the
        # fallback for a while instead of paying the launch cost every cycle.
        if time.monotonic() < self._playwright_cooldown_until:
            self.logger.info("Playwright fallback on cooldown; skipping.")
            return None

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.logger.info(
                "Playwright fallback unavailable. Install with: "
                "`pip install playwright` and `playwright install chromium`."
            )
            return None

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=self.headers.get("User-Agent", FMKOREA_DESKTOP_USER_AGENT),
                    locale="ko-KR",
                )
                page = context.new_page()
                page.goto(FMKOREA_BASE_URL + "/", wait_until="domcontentloaded", timeout=20000)

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=25000)
                except Exception:
                    # Retry once with a lighter navigation path when anti-bot script blocks.
                    page.goto(url, wait_until="commit", timeout=25000)

                page.wait_for_timeout(1800)
                html = page.content()
                if self._looks_like_security_page(html):
                    page.reload(wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(1500)
                    html = page.content()
                browser.close()

            if self._looks_like_security_page(html):
                self.logger.error("Playwright fallback still landed on security page.")
                self._playwright_cooldown_until = time.monotonic() + PLAYWRIGHT_COOLDOWN_SECONDS
                return None
            return html
        except Exception as e:
            self.logger.error(f"Playwright fallback failed: {e}")
            self._playwright_cooldown_until = time.monotonic() + PLAYWRIGHT_COOLDOWN_SECONDS
            return None

    def request(self, url: str) -> str | None:
        # FM코리아는 anti-bot 차단이 있어 세션 + 쿠키 힌트 + 브라우저 fallback 순으로 시도한다.
        # The session (and its cookies) persists across cycles, so the warmup
        # request only needs to happen once per process.
        if not self._warmed_up:
            self._request_with_session(FMKOREA_BASE_URL + "/", timeout=10.0)
            self._warmed_up = True
        response = self._request_with_session(url)
        if response is None:
            return None

        html = self._extract_html_if_ok(response, url)
        if html is not None:
            return html

        status_code = int(response.status_code)
        if status_code not in FMKOREA_BLOCK_STATUS_CODES:
            return None

        self._apply_security_cookie_hint(response.text)
        time.sleep(0.25)

        ddos_url = self._append_ddos_check_only(url)
        ddos_response = self._request_with_session(ddos_url)
        if ddos_response is not None:
            html = self._extract_html_if_ok(ddos_response, ddos_url)
            if html is not None:
                return html

        time.sleep(0.4)
        retry_response = self._request_with_session(url)
        if retry_response is not None:
            html = self._extract_html_if_ok(retry_response, url)
            if html is not None:
                return html

        return self._request_with_playwright(url)

    def parsing(self, html: str) -> Dict[int, BaseArticle]:
        soup = make_soup(html)
        rows = soup.select("table tbody tr")
        if not rows:
            self.logger.error("Can't find list rows, skip parsing")
            return {}

        data: Dict[int, BaseArticle] = {}
        for row in rows:
            title_cell = row.select_one("td.title")
            if title_cell is None:
                continue

            category_cell = row.select_one("td.cate")
            category_cell_text = category_cell.get_text(" ", strip=True) if category_cell else ""
            if "공지" in category_cell_text:
                continue

            no_text = row.select_one("td.no")
            no_value = no_text.get_text(" ", strip=True) if no_text else ""
            if "공지" in no_value:
                continue

            title_link = title_cell.select_one("a[href*='document_srl=']")
            if title_link is None:
                continue

            href = str(title_link.get("href", "") or "").strip()
            id_match = re.search(r"[?&]document_srl=(\d+)", href)
            if id_match is None:
                continue
            article_id = int(id_match.group(1))

            raw_title = title_link.get_text(" ", strip=True)
            title = _strip_leading_bracket_tags(raw_title)
            if not title:
                continue
            if "통합공지" in title:
                continue

            article_url = urljoin(FMKOREA_BASE_URL, href)

            image_url = None
            image_tag = row.select_one("img")
            if image_tag is not None:
                src = image_tag.get("src") or image_tag.get("data-src")
                if src:
                    candidate_url = urljoin(FMKOREA_BASE_URL, str(src))
                    if "/files/attach/" in candidate_url.lower():
                        image_url = candidate_url

            # NOTE: detail-page image extraction intentionally does NOT happen
            # here. The service enriches only deals that survive dedup (new
            # deals), via enrich_deal_fmkorea() below.
            category = normalize_fmkorea_category(category_cell_text, title)
            price = _extract_price_from_title(raw_title) or _extract_price_from_title(title)

            data[article_id] = {
                "article_id": article_id,
                "board_id": FMKOREA_BOARD_ID,
                "title": title,
                "category": category,
                "site_name": "FM코리아",
                "site_color": "456bc0",
                "board_name": "핫딜",
                "writer_name": (
                    row.select_one("td.author").get_text(" ", strip=True)
                    if row.select_one("td.author")
                    else ""
                ),
                "crawler_name": self.name,
                "logo": FMKOREA_LOGO_URL,
                "url": article_url,
                "is_end": False,
                "extra": {
                    "price": price,
                    "image_url": image_url,
                },
            }
        return data


_CRAWLER: FMKoreaCrawler | None = None


def _get_crawler() -> FMKoreaCrawler:
    global _CRAWLER
    if _CRAWLER is None:
        _CRAWLER = FMKoreaCrawler(
            name="FMKorea",
            url_list=[FMKOREA_LIST_URL],
        )
    return _CRAWLER


def enrich_deal_fmkorea(deal: dict) -> None:
    """Fill in the detail-page image for a NEW deal (called after dedup)."""
    if deal.get("image_url"):
        return
    url = str(deal.get("url", "") or "").strip()
    if not url:
        return
    deal["image_url"] = _get_crawler().extract_primary_image_from_detail(url)


def fetch_hot_deals_fmkorea() -> List[dict]:
    crawler = _get_crawler()
    articles = crawler.get()

    deals_list: List[dict] = []
    for article_id, article in articles.items():
        extra = article.get("extra", {})
        if not isinstance(extra, dict):
            extra = {}

        title = str(article.get("title", "") or "").strip()
        if not title:
            continue

        try:
            deal_id = encode_deal_key("fm", FMKOREA_BOARD_ID, article_id)
        except ValueError as e:
            crawler.logger.warning(f"Skipping article with invalid key parts: {e}")
            continue

        deals_list.append(
            {
                "id": deal_id,
                "site_code": FMKOREA_SITE_CODE,
                "title": title,
                "category": str(article.get("category", "기타") or "기타"),
                "price": str(extra.get("price", "") or "").strip(),
                "url": str(article.get("url", "") or "").strip(),
                "logo": str(article.get("logo", "") or "").strip(),
                "site_color": str(article.get("site_color", "456bc0") or "456bc0"),
                "site_name": str(article.get("site_name", "FM코리아") or "FM코리아"),
                "image_url": extra.get("image_url"),
            }
        )
    return deals_list
