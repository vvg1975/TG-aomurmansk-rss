#!/usr/bin/env python3
"""Build an RSS 2.0 feed from a public Telegram channel preview."""

from __future__ import annotations

import argparse
import html as html_module
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from lxml import etree, html


TELEGRAM_BASE = "https://t.me"
USER_AGENT = (
    "Mozilla/5.0 (compatible; TelegramToRSS/1.0; "
    "+https://t.me/AoMurmansk)"
)
MEDIA_NS = "http://search.yahoo.com/mrss/"
ATOM_NS = "http://www.w3.org/2005/Atom"

ET.register_namespace("media", MEDIA_NS)
ET.register_namespace("atom", ATOM_NS)


def clean_channel(value: str) -> str:
    value = value.strip().rstrip("/")
    value = re.sub(r"^https?://t\.me/(?:s/)?", "", value, flags=re.I)
    value = value.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,}", value):
        raise argparse.ArgumentTypeError("Некорректное имя Telegram-канала")
    return value


def fetch_page(channel: str, before: int | None = None) -> str:
    url = f"{TELEGRAM_BASE}/s/{channel}"
    if before:
        url += "?" + urlencode({"before": str(before)})
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
    )
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def has_class(name: str) -> str:
    return (
        "contains(concat(' ', normalize-space(@class), ' '), "
        f"' {name} ')"
    )


def inner_html(node: etree._Element) -> str:
    parts = [html_module.escape(node.text)] if node.text else []
    parts.extend(
        html.tostring(child, encoding="unicode", method="html") for child in node
    )
    return "".join(parts)


def visible_text(node: etree._Element) -> str:
    parts: list[str] = []
    break_tags = {"br", "p", "div", "blockquote", "li"}

    def add_break() -> None:
        if parts and not parts[-1].endswith("\n"):
            parts.append("\n")

    def walk(element: etree._Element) -> None:
        tag = element.tag.lower() if isinstance(element.tag, str) else ""
        if tag in break_tags:
            add_break()
        if element.text:
            parts.append(element.text)
        for child in element:
            walk(child)
            if child.tail:
                parts.append(child.tail)
        if tag in break_tags:
            add_break()

    walk(node)
    return "".join(parts).strip()


def media_urls(message: etree._Element) -> list[str]:
    urls: list[str] = []
    media_nodes = message.xpath(
        f".//*[{has_class('tgme_widget_message_photo_wrap')} or "
        f"{has_class('tgme_widget_message_video_thumb')}]"
    )
    for node in media_nodes:
        style = node.get("style", "") or ""
        match = re.search(r"background-image\s*:\s*url\(['\"]?(.*?)['\"]?\)", style)
        if match:
            urls.append(urljoin(TELEGRAM_BASE, match.group(1)))
    for node in message.xpath(".//video[@poster] | .//img[@src]"):
        if any(
            "link_preview" in (ancestor.get("class", "") or "")
            for ancestor in node.iterancestors()
        ):
            continue
        value = node.get("poster") or node.get("src")
        if value:
            urls.append(urljoin(TELEGRAM_BASE, value))
    return list(dict.fromkeys(urls))


def body_without_title(body_html: str, plain_text: str) -> str:
    """Remove Telegram's first text line because RSS <title> already contains it."""
    parts = re.split(r"<br\s*/?>", body_html, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        body = parts[1]
        body = re.sub(r"^(?:\s|&nbsp;|<br\s*/?>)+", "", body, flags=re.I)
        return body.strip()

    lines = [line.strip() for line in plain_text.splitlines() if line.strip()]
    if len(lines) <= 1:
        return ""
    return "<br><br>".join(html_module.escape(line) for line in lines[1:])


def make_title(text: str, post_id: int) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    title = next((line for line in lines if line), f"Публикация №{post_id}")
    return title if len(title) <= 140 else title[:137].rstrip() + "…"


def parse_page(html_text: str, channel: str) -> list[dict]:
    document = html.fromstring(html_text)
    posts: list[dict] = []

    messages = document.xpath(f"//*[{has_class('tgme_widget_message')} and @data-post]")
    for message in messages:
        data_post = message.get("data-post", "")
        match = re.fullmatch(r"([^/]+)/(\d+)", data_post)
        if not match or match.group(1).lower() != channel.lower():
            continue

        post_id = int(match.group(2))
        link = f"{TELEGRAM_BASE}/{channel}/{post_id}"
        text_nodes = message.xpath(f".//*[{has_class('tgme_widget_message_text')}]")
        text_node = text_nodes[0] if text_nodes else None
        text = visible_text(text_node) if text_node is not None else ""

        if text_node is not None:
            etree.strip_elements(text_node, "script", "style", with_tail=False)
            text_node.make_links_absolute(TELEGRAM_BASE)
            body_html = inner_html(text_node)
        else:
            body_html = ""

        body_html = body_without_title(body_html, text)
        media = media_urls(message)
        images_html = "".join(
            f'<p><img src="{url}" alt="Изображение из публикации"></p>'
            for url in media
        )
        body_block = body_html or ""
        source_block = f'<p><a href="{link}">{link}</a></p>'
        description = images_html + "<p>&nbsp;</p>" + body_block + source_block

        time_nodes = message.xpath(".//time[@datetime]")
        time_node = time_nodes[0] if time_nodes else None
        published = None
        if time_node is not None and time_node.get("datetime"):
            try:
                published = datetime.fromisoformat(
                    time_node.get("datetime", "").replace("Z", "+00:00")
                )
            except ValueError:
                pass
        if published is None:
            published = datetime.now(timezone.utc)
        elif published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)

        posts.append(
            {
                "id": post_id,
                "title": make_title(text, post_id),
                "link": link,
                "description": description,
                "published": published,
                "media": media,
            }
        )

    return posts


def collect_posts(channel: str, limit: int) -> list[dict]:
    found: dict[int, dict] = {}
    before: int | None = None

    while len(found) < limit:
        page_posts = parse_page(fetch_page(channel, before), channel)
        if not page_posts:
            break
        for post in page_posts:
            found[post["id"]] = post
        oldest = min(post["id"] for post in page_posts)
        if before == oldest:
            break
        before = oldest
        if len(found) < limit:
            time.sleep(0.4)

    return sorted(found.values(), key=lambda item: item["id"], reverse=True)[:limit]


def sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    node = ET.SubElement(parent, tag, attrs)
    if text is not None:
        node.text = text
    return node


def build_rss(channel: str, posts: list[dict], output: Path, feed_url: str | None) -> None:
    rss = ET.Element("rss", {"version": "2.0"})
    channel_node = sub(rss, "channel")
    sub(channel_node, "title", "Арктический обозреватель — Мурманск")
    sub(channel_node, "link", f"{TELEGRAM_BASE}/{channel}")
    sub(channel_node, "description", "Ежедневное обозрение событий в Арктике")
    sub(channel_node, "language", "ru")
    sub(channel_node, "generator", "telegram_to_rss.py")
    sub(channel_node, "lastBuildDate", format_datetime(datetime.now(timezone.utc)))
    sub(channel_node, "ttl", "10")
    if feed_url:
        sub(
            channel_node,
            f"{{{ATOM_NS}}}link",
            href=feed_url,
            rel="self",
            type="application/rss+xml",
        )

    for post in posts:
        item = sub(channel_node, "item")
        sub(item, "title", post["title"])
        sub(item, "link", post["link"])
        sub(item, "guid", post["link"], isPermaLink="true")
        sub(item, "pubDate", format_datetime(post["published"]))
        sub(item, "description", post["description"])
        for media_url in post["media"]:
            sub(item, f"{{{MEDIA_NS}}}content", url=media_url, medium="image")

    ET.indent(rss, space="  ")
    xml = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(xml + b"\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Создать RSS 2.0 из публичного Telegram-канала"
    )
    parser.add_argument("channel", nargs="?", default="AoMurmansk", type=clean_channel)
    parser.add_argument("-o", "--output", default="AoMurmansk.xml", type=Path)
    parser.add_argument("-n", "--max-items", default=30, type=int)
    parser.add_argument("--feed-url", help="Публичный URL будущего RSS-файла")
    args = parser.parse_args()

    if not 1 <= args.max_items <= 200:
        parser.error("--max-items должен быть от 1 до 200")

    try:
        posts = collect_posts(args.channel, args.max_items)
        if not posts:
            raise RuntimeError("Telegram не вернул ни одной доступной публикации")
        build_rss(args.channel, posts, args.output, args.feed_url)
    except (HTTPError, URLError, TimeoutError, OSError, RuntimeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1

    print(f"Готово: {args.output} ({len(posts)} публикаций)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
