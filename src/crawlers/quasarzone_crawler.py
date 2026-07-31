from __future__ import annotations

import re
from typing import Dict, List

from bs4 import Tag

try:
    from .base_crawler import BaseArticle, BaseCrawler, make_soup
except ImportError:
    from base_crawler import BaseArticle, BaseCrawler, make_soup

try:
    from ..deal_key import encode_deal_key
except ImportError:
    from deal_key import encode_deal_key


QUASAR_DEFAULT_BOARD_ID = "qb_saleinfo"
QUASAR_SITE_CODE = "quasarzone"


def _normalize_category_token(raw: str) -> str:
    return re.sub(r"\s+", "", str(raw or "")).casefold()


_QUASAR_CATEGORY_MAP_RAW = {
    "PC/\ud558\ub4dc\uc6e8\uc5b4": "PC",
    "\uc0c1\ud488\uad8c/\ucfe0\ud3f0": "\uc0c1\ud488\uad8c",
    "\uac8c\uc784/SW": "\uac8c\uc784",
    "SW/\uac8c\uc784": "\uac8c\uc784",
    "\ub178\ud2b8\ubd81/\ubaa8\ubc14\uc77c": "\uc804\uc790\uc81c\ud488",
    "\uac00\uc804/TV": "\uac00\uc804",
    "\uc0dd\ud65c/\uc2dd\ud488": "\uc0dd\ud65c\uc6a9\ud488",
    "\ud328\uc158/\uc758\ub958": "\uc758\ub958",
    "\ud3ec\uc778\ud2b8/\ub798\ud50c": "\uae30\ud0c0",
    "\uc2dd\ud488": "\uc2dd\ud488",
    "\uc0dd\ud65c\uc6a9\ud488": "\uc0dd\ud65c\uc6a9\ud488",
    "\uac8c\uc784": "\uac8c\uc784",
    "PC": "PC",
    "\uac00\uc804": "\uac00\uc804",
    "\uc804\uc790\uc81c\ud488": "\uc804\uc790\uc81c\ud488",
    "\uc758\ub958": "\uc758\ub958",
    "\ud654\uc7a5\ud488": "\ud654\uc7a5\ud488",
    "\uc0c1\ud488\uad8c": "\uc0c1\ud488\uad8c",
    "\uae30\ud0c0": "\uae30\ud0c0",
}

QUASAR_CATEGORY_MAP = {
    _normalize_category_token(raw): canonical
    for raw, canonical in _QUASAR_CATEGORY_MAP_RAW.items()
}


def normalize_quasar_category(raw_category: str) -> str:
    text = str(raw_category or "").strip()
    if not text:
        return ""
    return QUASAR_CATEGORY_MAP.get(_normalize_category_token(text), "\uae30\ud0c0")


def _upgrade_quasar_image_url(url: str) -> str:
    """
    The list page serves tiny blurred previews named 'thumb_<hash>.<ext>'.
    The full-resolution image lives at the same CDN path without the
    'thumb_' prefix, so upgrade the URL when the pattern matches.
    """
    text = str(url or "").strip().rstrip("?")
    if not text:
        return ""
    if "quasarzone.com" in text:
        head, sep, tail = text.rpartition("/")
        if sep and tail.startswith("thumb_"):
            return f"{head}/{tail[len('thumb_'):]}"
    return text


class QuasarzoneCrawler(BaseCrawler):
    def parsing(self, html: str) -> Dict[int, BaseArticle]:
        soup = make_soup(html)

        if (_board_name := soup.select_one(".l-title h2")) is None:
            self.logger.error("Can't find board name, skip parsing")
            return {}

        board_name = _board_name.text.strip()

        if (table := soup.select_one(".market-info-type-list > table > tbody")) is None:
            self.logger.error("Can't find article list, skip parsing")
            return {}

        rows = table.select("tr")
        data: Dict[int, BaseArticle] = {}

        for row in rows:
            if (_url_tag := row.select_one(".subject-link")) is None or (
                _url := _url_tag.attrs.get("href")
            ) is None:
                self.logger.warning("Cannot find article url tag")
                continue

            if (_re_url := re.search(r"/bbs/([\w\d_]+)/views/(\d+)", _url)) is None:
                self.logger.warning("Cannot find board id and article id")
                continue

            if row.select_one(".fa-lock"):
                self.logger.debug("Locked article, skip.")
                continue

            if (_title_tag := row.select_one(".ellipsis-with-reply-cnt")) is None or (
                not _title_tag.text
            ):
                self.logger.warning("Cannot find article title tag")
                continue
            raw_title = _title_tag.text.strip()
            title = re.sub(r"^\[.*?\]\s*", "", raw_title)

            image_url = None
            candidate_tags = row.select("img.maxImg") or row.select("img")
            for _image_tag in candidate_tags:
                _src = _image_tag.attrs.get("src") or _image_tag.attrs.get("data-src")
                if not _src or "tangerine.png" in _src:
                    continue
                # Skip site chrome: store badges and user-level icons.
                if "/store/" in _src or "/level/" in _src:
                    continue
                image_url = _upgrade_quasar_image_url(_src)
                break

            if (_nick_tag := row.select_one(".nick")) is None or (
                not _nick_tag.attrs.get("data-nick")
            ):
                self.logger.warning("Cannot find article writer tag")
                continue

            if (_recommend_tag := row.select_one("td .num")) is None or (
                not _recommend_tag.text
            ):
                self.logger.warning("Cannot get recommend value tag")
                continue
            if (_view_tag := row.select_one(".count")) is None or not _view_tag.text:
                self.logger.warning("Cannot get view count tag")
                continue

            if (_info_tag := row.select_one(".market-info-sub p:first-child")) is None:
                self.logger.warning("Cannot get sub info tag")
                continue

            is_end = False
            if (_is_end_tag := row.select_one(".label")) and _is_end_tag.text.strip() in {
                "\uc885\ub8cc",
                "End",
            }:
                is_end = True

            _board_id = _re_url.group(1)
            _id = int(_re_url.group(2))

            _info = self.info_tag_parser(_info_tag)
            _category = str(_info.pop("category", "") or "").strip()
            if not _category:
                self.logger.warning("Cannot get category")

            data[_id] = {
                "article_id": _id,
                "board_id": _board_id,
                "title": title,
                "category": _category,
                "site_name": "퀘이사존",
                "site_color": "ff9900",
                "board_name": board_name,
                "writer_name": _nick_tag.text.strip(),
                "crawler_name": self.name,
                "logo": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcSbxUBUWksXWUh0hKjndR29gmjbxdF2yVxFQg&s",
                "url": f"https://quasarzone.com/bbs/{_board_id}/views/{_id}",
                "is_end": is_end,
                "extra": {
                    "recommend": _recommend_tag.text.strip(),
                    "view": _view_tag.text.strip(),
                    "image_url": image_url,
                    **_info,
                },
            }
        return data

    def info_tag_parser(self, el: Tag) -> dict:
        data = {}
        for e in el.find_all("span", recursive=False):
            if not isinstance(e, Tag):
                continue
            text = e.get_text(" ", strip=True)

            if "category" in e.attrs.get("class", []):
                data["category"] = text
                continue

            if e.find("span") and ("\uac00\uaca9" in text or "\uc6d0" in text):
                price_el = e.find("span")
                data["price"] = price_el.text.strip() if price_el else ""
                continue

            if "\uc9c1\ubc30" in text:
                data["direct_delivery"] = "\uac00\ub2a5" in text
                continue

            if "\ubc30\uc1a1\ube44" in text:
                data["delivery"] = (
                    text.replace("\ubc30\uc1a1\ube44", "")
                    .replace(":", "")
                    .strip()
                )
        return data


_CRAWLER: QuasarzoneCrawler | None = None


def _get_crawler() -> QuasarzoneCrawler:
    global _CRAWLER
    if _CRAWLER is None:
        _CRAWLER = QuasarzoneCrawler(
            name="Quasarzone",
            url_list=["https://quasarzone.com/bbs/qb_saleinfo"],
        )
    return _CRAWLER


def fetch_hot_deals() -> List[dict]:
    crawler = _get_crawler()
    articles_dict = crawler.get()

    deals_list = []
    for article_id, article in articles_dict.items():
        board_id = str(article.get("board_id", QUASAR_DEFAULT_BOARD_ID)).strip().lower()
        if not board_id:
            board_id = QUASAR_DEFAULT_BOARD_ID
        extra = article.get("extra", {})
        if not isinstance(extra, dict):
            extra = {}

        title = str(article.get("title", "") or "").strip()
        if not title:
            continue

        try:
            deal_id = encode_deal_key("qr", board_id, article_id)
        except ValueError as e:
            crawler.logger.warning(f"Skipping article with invalid key parts: {e}")
            continue

        deals_list.append(
            {
                "id": deal_id,
                "site_code": QUASAR_SITE_CODE,
                "title": title,
                "category": normalize_quasar_category(article.get("category", "")),
                "price": str(extra.get("price", "") or "").strip(),
                "url": str(article.get("url", "") or "").strip(),
                "logo": str(article.get("logo", "") or "").strip(),
                "site_color": str(article.get("site_color", "ff9900") or "ff9900"),
                "site_name": str(article.get("site_name", "퀘이사존") or "퀘이사존"),
                "image_url": extra.get("image_url"),
            }
        )

    return deals_list
