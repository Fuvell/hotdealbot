from __future__ import annotations

import re
from typing import Dict, List

try:
    from .base_crawler import BaseArticle, BaseCrawler, make_soup
except ImportError:
    from base_crawler import BaseArticle, BaseCrawler, make_soup

try:
    from ..deal_key import encode_deal_key
except ImportError:
    from deal_key import encode_deal_key


ARCA_BOARD_ID = "hotdeal"
ARCA_SITE_CODE = "arcalive"


def _normalize_category_token(raw: str) -> str:
    return re.sub(r"\s+", "", str(raw or "")).casefold()


_ARCA_CATEGORY_MAP_RAW = {
    "\uc804\uccb4": "\uae30\ud0c0",
    "all": "\uae30\ud0c0",
    "\uc2dd\ud488": "\uc2dd\ud488",
    "food": "\uc2dd\ud488",
    "\uc0dd\ud65c\uc6a9\ud488": "\uc0dd\ud65c\uc6a9\ud488",
    "living": "\uc0dd\ud65c\uc6a9\ud488",
    "\uc804\uc790\uc81c\ud488": "\uc804\uc790\uc81c\ud488",
    "elec": "\uc804\uc790\uc81c\ud488",
    "pc/\ud558\ub4dc\uc6e8\uc5b4": "PC",
    "pc": "PC",
    "sw/\uac8c\uc784": "\uac8c\uc784",
    "\uac8c\uc784/sw": "\uac8c\uc784",
    "sw": "\uac8c\uc784",
    "\uc758\ub958": "\uc758\ub958",
    "wear": "\uc758\ub958",
    "\ud654\uc7a5\ud488": "\ud654\uc7a5\ud488",
    "cosmetic": "\ud654\uc7a5\ud488",
    "\uc0c1\ud488\uad8c/\ucfe0\ud3f0": "\uc0c1\ud488\uad8c",
    "gift": "\uc0c1\ud488\uad8c",
    "\uc784\ubc15": "\uae30\ud0c0",
    "thirty": "\uae30\ud0c0",
    "\uc751\ubaa8": "\uae30\ud0c0",
    "draw": "\uae30\ud0c0",
    "\uae30\ud0c0": "\uae30\ud0c0",
    "etc": "\uae30\ud0c0",
    "\ubc14\uc774\ub7f4": "\uae30\ud0c0",
    "\ubc14\uc774\ub7f4\U0001f92c": "\uae30\ud0c0",
    "viral": "\uae30\ud0c0",
}

ARCA_CATEGORY_MAP = {
    _normalize_category_token(raw): canonical
    for raw, canonical in _ARCA_CATEGORY_MAP_RAW.items()
}


def normalize_arca_category(raw_category: str) -> str:
    text = str(raw_category or "").strip()
    if not text:
        return ""
    return ARCA_CATEGORY_MAP.get(_normalize_category_token(text), "\uae30\ud0c0")


class ArcaLiveCrawler(BaseCrawler):
    def __init__(self, name: str, url_list: list[str]) -> None:
        super().__init__(name=name, url_list=url_list)
        self.headers["Referer"] = "https://arca.live/"

    def parsing(self, html: str) -> Dict[int, BaseArticle]:
        soup = make_soup(html)

        if (_board_name := soup.select_one(".board-title .title")) is None or (
            board_name := _board_name.attrs.get("data-channel-name")
        ) is None:
            self.logger.error("Can't find board name, skip parsing")
            return {}

        if (table := soup.select_one(".list-table")) is None:
            self.logger.error("Can't find article list, skip parsing")
            return {}

        rows = table.select(".vrow.hybrid")
        data: Dict[int, BaseArticle] = {}

        for row in rows:
            _title_tag = row.select_one(".title")
            if not _title_tag:
                self.logger.warning("No title tag")
                continue

            _title_strings = _title_tag.find_all(string=True, recursive=False)
            if not _title_strings:
                self.logger.warning("No title strings")
                continue
            title = "".join(_title_strings).strip()

            image_url = None
            preview_image = row.select_one(".vrow-preview img")
            if preview_image and preview_image.attrs.get("src"):
                image_url = preview_image.attrs.get("src")
                if image_url.startswith("//"):
                    image_url = "https:" + image_url

            _url = _title_tag.attrs.get("href")
            if not _url:
                self.logger.warning("No href in title tag")
                continue
            re_id = re.match(r"/b/([\w\d]+)/(\d+)\??.*", _url)
            if not re_id:
                self.logger.warning("Cannot parse article id from url")
                continue

            _board_id = re_id.group(1)
            _id = int(re_id.group(2))

            _category_tag = row.select_one(".badge")
            _store_name_tag = row.select_one(".deal-store")
            _writer_tag = row.select_one(".user-info span:first-child")
            _recommend_tag = row.select_one(".col-rate")
            _view_tag = row.select_one(".col-view")
            _price_tag = row.select_one(".deal-price")
            _delivery_tag = row.select_one(".deal-delivery")

            if not all(
                (
                    _store_name_tag,
                    _writer_tag,
                    _recommend_tag,
                    _view_tag,
                    _price_tag,
                    _delivery_tag,
                )
            ):
                self.logger.warning("Missing required tags, skip row.")
                continue

            is_end = bool(row.select_one(".deal-close"))
            category_text = _category_tag.text.strip() if _category_tag else ""

            data[_id] = {
                "article_id": _id,
                "title": title,
                "category": category_text,
                "site_name": "아카라이브",
                "site_color": "ffffff",
                "board_name": board_name,
                "writer_name": _writer_tag.text.strip(),
                "crawler_name": self.name,
                "logo": "https://play-lh.googleusercontent.com/_ZARgTL9fJ1kh0_EFrTx0jAxWNPjWQCvhev5zM-qgzXc-yP99b4e0851wRDHdM3QTA=w240-h480-rw",
                "url": f"https://arca.live/b/{_board_id}/{_id}",
                "is_end": is_end,
                "extra": {
                    "recommend": _recommend_tag.text.strip(),
                    "view": _view_tag.text.strip(),
                    "price": _price_tag.text.strip(),
                    "delivery": _delivery_tag.text.strip(),
                    "image_url": image_url,
                },
            }
        return data


_CRAWLER: ArcaLiveCrawler | None = None


def _get_crawler() -> ArcaLiveCrawler:
    global _CRAWLER
    if _CRAWLER is None:
        _CRAWLER = ArcaLiveCrawler(
            name="ArcaLive",
            url_list=["https://arca.live/b/hotdeal"],
        )
    return _CRAWLER


def fetch_hot_deals_arca() -> List[dict]:
    crawler = _get_crawler()
    articles = crawler.get()
    deals_list = []
    for article_id, article in articles.items():
        extra = article.get("extra", {})
        if not isinstance(extra, dict):
            extra = {}

        title = str(article.get("title", "") or "").strip()
        if not title:
            continue

        try:
            deal_id = encode_deal_key("al", ARCA_BOARD_ID, article_id)
        except ValueError as e:
            crawler.logger.warning(f"Skipping article with invalid key parts: {e}")
            continue

        deals_list.append(
            {
                "id": deal_id,
                "site_code": ARCA_SITE_CODE,
                "title": title,
                "category": normalize_arca_category(article.get("category", "")),
                "price": str(extra.get("price", "") or "").strip(),
                "url": str(article.get("url", "") or "").strip(),
                "logo": str(article.get("logo", "") or "").strip(),
                "site_color": str(article.get("site_color", "ffffff") or "ffffff"),
                "site_name": str(article.get("site_name", "아카라이브") or "아카라이브"),
                "image_url": extra.get("image_url"),
            }
        )
    return deals_list
