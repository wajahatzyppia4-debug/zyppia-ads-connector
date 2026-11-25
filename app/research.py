"""Keyword research and reading a client's website (for campaign planning)."""

from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from google.ads.googleads.errors import GoogleAdsException

from .ads import Account, AdsError, AdsGateway, describe_ads_exception, error_codes

MAX_PAGE_BYTES = 2_000_000
USER_AGENT = "Mozilla/5.0 (compatible; ZyppiaAdsConnector/1.0)"

NEEDS_BASIC = (
    "Keyword Planner isn't available yet: it needs Basic API access, and this connector's Google Cloud project is on "
    "Explorer access. Finish brand verification in Google Cloud, then re-apply for Basic."
)


def is_access_level_error(e: GoogleAdsException) -> bool:
    codes = error_codes(e)
    text = describe_ads_exception(e).lower()
    return any(c.startswith(("authorization_error.", "access_level_error.")) or "DEVELOPER_TOKEN" in c for c in codes) or (
        "access" in text and "level" in text
    )


def keyword_ideas(ads: AdsGateway, grt: str, acct: Account, seeds: list[str] | None, page_url: str | None,
                  location_ids: list[str] | None, language_id: str, search_partners: bool, max_ideas: int) -> list[dict]:
    client = ads.client(grt, acct.login_customer_id)
    svc = client.get_service("KeywordPlanIdeaService")
    req = client.get_type("GenerateKeywordIdeasRequest")
    req.customer_id = acct.customer_id
    req.language = f"languageConstants/{int(language_id)}"
    for loc in location_ids or []:
        req.geo_target_constants.append(f"geoTargetConstants/{int(loc)}")
    req.include_adult_keywords = False
    net = client.enums.KeywordPlanNetworkEnum
    req.keyword_plan_network = net.GOOGLE_SEARCH_AND_PARTNERS if search_partners else net.GOOGLE_SEARCH
    seeds = (seeds or [])[:20]
    if seeds and page_url:
        req.keyword_and_url_seed.url = page_url
        req.keyword_and_url_seed.keywords.extend(seeds)
    elif seeds:
        req.keyword_seed.keywords.extend(seeds)
    else:
        req.url_seed.url = page_url
    try:
        ideas = []
        for idea in svc.generate_keyword_ideas(request=req):
            m = idea.keyword_idea_metrics
            ideas.append({
                "keyword": idea.text,
                "avg_monthly_searches": m.avg_monthly_searches,
                "competition": m.competition.name,
                "competition_index": m.competition_index,
                "top_of_page_bid_low": round(m.low_top_of_page_bid_micros / 1e6, 2),
                "top_of_page_bid_high": round(m.high_top_of_page_bid_micros / 1e6, 2),
            })
            if len(ideas) >= max_ideas:
                break
    except GoogleAdsException as e:
        if is_access_level_error(e):
            raise AdsError(NEEDS_BASIC + " Details: " + describe_ads_exception(e)) from None
        raise
    ideas.sort(key=lambda d: d["avg_monthly_searches"] or 0, reverse=True)
    return ideas


# --------------------------------------------------------------------- website


def _public_host(host: str) -> bool:
    """True only if every address the host resolves to is on the public internet."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


def check_url(url: str) -> str:
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise AdsError("Give a full website address starting with https://")
    if u.port not in (None, 80, 443):
        raise AdsError("Only normal web addresses (ports 80 and 443) can be read.")
    host = u.hostname.lower()
    if host in ("localhost", "metadata", "metadata.google.internal") or host.endswith((".internal", ".local")):
        raise AdsError("That address isn't a public website.")
    if not _public_host(host):
        raise AdsError("That address isn't a public website, or it doesn't exist.")
    return u.geturl()


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.headings: list[str] = []
        self.text: list[str] = []
        self.links: list[str] = []
        self._stack: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "svg", "nav", "footer"):
            self._skip += 1
        if tag == "meta" and (a.get("name") or a.get("property") or "").lower() in ("description", "og:description"):
            self.description = self.description or (a.get("content") or "").strip()
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        self._stack.append(tag)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "nav", "footer") and self._skip:
            self._skip -= 1
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()

    def handle_data(self, data):
        t = " ".join(data.split())
        if not t:
            return
        cur = self._stack[-1] if self._stack else ""
        if cur == "title":
            self.title = self.title or t
        elif self._skip:
            return
        elif cur in ("h1", "h2", "h3"):
            if len(self.headings) < 40:
                self.headings.append(t)
        elif sum(len(x) for x in self.text) < 6000:
            self.text.append(t)


def fetch_page(url: str, timeout: float = 10.0) -> dict[str, Any]:
    """Read a public web page safely: public addresses only, redirects checked, size and time limited."""
    current = check_url(url)
    with httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as http:
        for _ in range(5):
            with http.stream("GET", current) as resp:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    current = check_url(urljoin(current, resp.headers["location"]))
                    continue
                if resp.status_code >= 400:
                    raise AdsError(f"The website answered with error {resp.status_code}.")
                ctype = resp.headers.get("content-type", "")
                if "html" not in ctype:
                    raise AdsError(f"That address isn't a web page ({ctype or 'unknown type'}).")
                body = b""
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES:
                        break
                html = body.decode(resp.encoding or "utf-8", errors="replace")
                break
        else:
            raise AdsError("Too many redirects.")
    p = _PageParser()
    p.feed(html)
    host = urlparse(current).hostname
    internal = []
    for href in p.links:
        full = urljoin(current, href).split("#")[0]
        if urlparse(full).hostname == host and full not in internal and full.rstrip("/") != current.rstrip("/"):
            internal.append(full)
    phones = sorted(set(re.findall(r"(?:\+|00)\d[\d\s().-]{7,16}\d", " ".join(p.text))))[:3]
    return {
        "url": current,
        "title": p.title,
        "description": p.description,
        "headings": p.headings,
        "text": " ".join(p.text)[:6000],
        "phone_numbers_seen": phones,
        "pages_on_site": internal[:25],
    }
