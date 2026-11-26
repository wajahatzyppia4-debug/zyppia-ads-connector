"""Phase 5: website tags. Make the install code for a client's developer, and check a live site to see
which Google tags (Tag Manager GTM-, Analytics G-, Google Ads AW-) are really on it, what's missing, and
what's doubled (which makes conversions count twice).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Annotated, Any, Literal

from pydantic import Field

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import research
from .ads import AdsError
from .writes import Writer

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

_ID = r"(?:GTM-[A-Z0-9]{4,10}|G-[A-Z0-9]{6,12}|AW-\d{6,12}|GT-[A-Z0-9]{6,12}|DC-\d{6,12})"
RE_ANY = re.compile(rf"\b({_ID})\b")
RE_GTM_LOADER = re.compile(r"googletagmanager\.com/gtm\.js\?id=(GTM-[A-Z0-9]{4,10})")
RE_GTM_SNIPPET = re.compile(r"['\"]dataLayer['\"]\s*,\s*['\"](GTM-[A-Z0-9]{4,10})['\"]\s*\)")
RE_GTM_NOSCRIPT = re.compile(r"googletagmanager\.com/ns\.html\?id=(GTM-[A-Z0-9]{4,10})")
RE_GTAG_LOADER = re.compile(rf"googletagmanager\.com/gtag/js\?id=({_ID})")
RE_GTAG_CONFIG = re.compile(rf"gtag\(\s*['\"]config['\"]\s*,\s*['\"]({_ID})['\"]")
RE_CONVERSION = re.compile(r"['\"]send_to['\"]\s*:\s*['\"](AW-\d{6,12}/[A-Za-z0-9_-]+)['\"]")
RE_UA = re.compile(r"\b(UA-\d{4,10}-\d{1,4})\b")
RE_CONSENT = re.compile(r"gtag\(\s*['\"]consent['\"]\s*,\s*['\"]default['\"]")
CMPS = {"cookiebot": "Cookiebot", "onetrust": "OneTrust", "cookieyes": "CookieYes", "complianz": "Complianz",
        "iubenda": "iubenda", "termly": "Termly", "usercentrics": "Usercentrics", "osano": "Osano"}
PLATFORMS = [("cdn.shopify.com", "Shopify"), ("wp-content", "WordPress"), ("static.wixstatic.com", "Wix"),
             ("squarespace", "Squarespace"), ("webflow", "Webflow"), ("bigcommerce", "BigCommerce"),
             ("godaddy", "GoDaddy Website Builder")]
TIPS = {
    "wordpress": ("WordPress: install the free plugin 'Site Kit by Google' (connects GA4 and Tag Manager without code), "
                  "or a header-code plugin such as 'WPCode': paste part 1 in Header and part 2 in Body. Don't do both."),
    "shopify": ("Shopify: Online Store > Themes > ... > Edit code > theme.liquid: paste part 1 right after <head> and "
                "part 2 right after <body>. For purchases, the checkout needs Shopify's 'Google & YouTube' app or a "
                "custom pixel (Settings > Customer events), because theme code doesn't run on checkout."),
    "wix": ("Wix: Settings > Custom Code > + Add Custom Code: paste part 1 in Head (all pages); paste part 2 as a "
            "second snippet in Body - start. Wix also has a built-in Google Tag Manager setting under Marketing Integrations."),
    "squarespace": ("Squarespace: Settings > Advanced > Code Injection: part 1 in Header, part 2 in Footer is fine "
                    "if Body isn't available. (Needs a Business plan or higher.)"),
    "webflow": "Webflow: Site settings > Custom code: part 1 in Head code, part 2 in Footer code. Then Publish.",
    "other": ("Paste part 1 as high as possible inside <head> on every page, and part 2 right after the opening "
              "<body> tag on every page."),
}


def gtm_snippet(cid: str) -> tuple[str, str]:
    head = (f"<!-- Google Tag Manager -->\n<script>(function(w,d,s,l,i){{w[l]=w[l]||[];w[l].push({{'gtm.start':\n"
            f"new Date().getTime(),event:'gtm.js'}});var f=d.getElementsByTagName(s)[0],\n"
            f"j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src=\n"
            f"'https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);\n"
            f"}})(window,document,'script','dataLayer','{cid}');</script>\n<!-- End Google Tag Manager -->")
    body = (f"<!-- Google Tag Manager (noscript) -->\n<noscript><iframe src=\"https://www.googletagmanager.com/ns.html?id={cid}\"\n"
            f"height=\"0\" width=\"0\" style=\"display:none;visibility:hidden\"></iframe></noscript>\n"
            f"<!-- End Google Tag Manager (noscript) -->")
    return head, body


def gtag_snippet(ids: list[str]) -> str:
    configs = "\n".join(f"  gtag('config', '{i}');" for i in ids)
    return (f"<!-- Google tag (gtag.js) -->\n<script async src=\"https://www.googletagmanager.com/gtag/js?id={ids[0]}\"></script>\n"
            f"<script>\n  window.dataLayer = window.dataLayer || [];\n  function gtag(){{dataLayer.push(arguments);}}\n"
            f"  gtag('js', new Date());\n{configs}\n</script>")


def install_code(ids: list[str], platform: str) -> dict[str, Any]:
    clean = []
    for i in ids:
        i = str(i).strip().upper()
        if re.fullmatch(r"\d{6,12}", i):
            i = "AW-" + i
        if not re.fullmatch(_ID, i):
            raise AdsError(f"'{i}' isn't a tag ID. Use GTM-XXXXXXX (Tag Manager), G-XXXXXXX (Analytics) or AW-123456789 "
                           "(Google Ads).")
        if i not in clean:
            clean.append(i)
    if not clean:
        raise AdsError("Give at least one ID: GTM-..., G-... or AW-...")
    gtm = [i for i in clean if i.startswith("GTM-")]
    direct = [i for i in clean if not i.startswith("GTM-")]
    out: dict[str, Any] = {}
    if gtm:
        if len(gtm) > 1:
            out["warning"] = "More than one Tag Manager container. Usually a site needs only one."
        heads, bodies = zip(*(gtm_snippet(c) for c in gtm))
        out["part_1_head"] = "\n".join(heads)
        out["part_2_body"] = "\n".join(bodies)
        if direct:
            out["advice"] = (f"{', '.join(direct)} should go inside Tag Manager (gtm_tag kind google_tag), not on the "
                             "site as well, or visits and conversions count twice. So only the Tag Manager code is here.")
        what = "Google Tag Manager"
    else:
        out["part_1_head"] = gtag_snippet(direct)
        out["advice"] = ("Tag Manager is easier to manage later (no developer needed for new tags). If the site gets "
                         "Tag Manager, use that instead of this code.")
        what = "the Google tag"
    out["where_to_paste"] = TIPS.get(platform, TIPS["other"])
    out["message_for_developer"] = (
        f"Hi, please add {what} ({', '.join(gtm or direct)}) to every page of the website. "
        + ("Paste part 1 as high as possible in the <head>, and part 2 right after the opening <body> tag. "
           if gtm else "Paste the code as high as possible in the <head> of every page. ")
        + "Please remove any other copy of the same code so nothing loads twice, and let me know when it's live. Thanks!"
    )
    out["after"] = "When the developer says it's done, run check_site_tags on the website to confirm."
    return out


def scan(html: str) -> dict[str, Any]:
    low = html.lower()
    gtm_loads = Counter(RE_GTM_LOADER.findall(html))
    for c in RE_GTM_SNIPPET.findall(html):  # the standard snippet builds the gtm.js URL itself
        gtm_loads[c] += 1
    gtag_loads = Counter(RE_GTAG_LOADER.findall(html))
    configs = Counter(RE_GTAG_CONFIG.findall(html))
    platform = next((name for key, name in PLATFORMS if key in low), None)
    gen = re.search(r"<meta[^>]+name=['\"]generator['\"][^>]+content=['\"]([^'\"]+)", html, re.I)
    return {
        "tag_manager": dict(gtm_loads),
        "tag_manager_noscript": sorted(set(RE_GTM_NOSCRIPT.findall(html))),
        "google_tag_loaded": dict(gtag_loads),
        "google_tag_config": dict(configs),
        "conversion_events_in_code": sorted(set(RE_CONVERSION.findall(html))),
        "old_universal_analytics": sorted(set(RE_UA.findall(html))),
        "all_ids_seen": sorted(set(RE_ANY.findall(html))),
        "consent_mode_default": bool(RE_CONSENT.search(html)),
        "cookie_banner": sorted({name for key, name in CMPS.items() if key in low}),
        "platform": platform or (gen.group(1) if gen else None),
        "page_size_kb": round(len(html) / 1024),
    }


class SiteTags:
    def __init__(self, w: Writer):
        self.w = w

    def container_ids(self, grt: str, public_id: str) -> dict[str, Any] | None:
        """What a GTM container we can access loads when it's live: Google tag IDs and Ads conversion IDs."""
        gtm = self.w.gtm
        if gtm is None:
            return None
        try:
            found = [c for c in gtm.containers(grt) if (c.get("public_id") or "").upper() == public_id]
            if not found:
                return {"access": False}
            live = gtm.call(grt, "GET", found[0]["path"] + "/versions:live")
        except AdsError:
            return None
        ids: set[str] = set()
        for t in live.get("tag") or []:
            if t.get("paused"):
                continue
            ps = {p.get("key"): p.get("value") for p in t.get("parameter") or []}
            if t.get("type") == "googtag" and ps.get("tagId"):
                ids.add(ps["tagId"].upper())
            if t.get("type") in ("awct", "sp") and ps.get("conversionId"):
                ids.add("AW-" + re.sub(r"^AW-", "", ps["conversionId"], flags=re.I))
            if t.get("type") in ("gaawe", "gaawc") and (ps.get("measurementIdOverride") or ps.get("measurementId")):
                ids.add((ps.get("measurementIdOverride") or ps.get("measurementId")).upper())
        return {"access": True, "name": found[0].get("name"), "live_version": live.get("containerVersionId"),
                "loads": sorted(i for i in ids if not i.startswith("{{")), "tags_live": len(live.get("tag") or [])}

    def check(self, grt: str, url: str, extra_pages: list[str] | None, expected: list[str] | None) -> dict[str, Any]:
        urls = [url] + list(extra_pages or [])[:5]
        pages, problems, good = [], [], []
        seen_ids: set[str] = set()
        direct_ids: set[str] = set()
        gtm_ids: set[str] = set()
        for u in urls:
            try:
                final, html = research.fetch_html(u)
            except AdsError as e:
                pages.append({"url": u, "error": str(e)})
                continue
            s = scan(html)
            s["url"] = final
            pages.append(s)
            seen_ids |= set(s["all_ids_seen"])
            gtm_ids |= set(s["tag_manager"])
            direct_ids |= set(s["google_tag_config"]) | set(s["google_tag_loaded"])
            where = f" on {final}"
            for c, n in s["tag_manager"].items():
                if n > 1:
                    problems.append(f"Tag Manager {c} is loaded {n} times{where}, so every tag in it fires {n} times "
                                    "(conversions counted twice). Remove the extra copy.")
            if len(s["tag_manager"]) > 1:
                problems.append(f"{len(s['tag_manager'])} different Tag Manager containers{where}: "
                                f"{', '.join(s['tag_manager'])}. Check they don't both track the same things.")
            for i, n in s["google_tag_config"].items():
                if n > 1:
                    problems.append(f"{i} is set up {n} times in the page code{where} (double counting).")
            if s["old_universal_analytics"]:
                problems.append(f"Old Universal Analytics code ({', '.join(s['old_universal_analytics'])}){where}. "
                                "It stopped working in 2024; remove it and use GA4 (G-...).")
            if not s["all_ids_seen"]:
                problems.append(f"No Google tag found{where}. "
                                + ("The page is very small, so it may be built by JavaScript; tags added that way "
                                   "can't be seen here. Check with Google Tag Assistant (tagassistant.google.com)."
                                   if s["page_size_kb"] < 15 else
                                   "Give the developer tag_install_code."))
        containers = {}
        for c in sorted(gtm_ids):
            info = self.container_ids(grt, c)
            if info is None:
                continue
            containers[c] = info
            if info.get("access"):
                both = sorted(direct_ids & set(info["loads"]))
                for i in both:
                    problems.append(f"{i} is in the page code AND in Tag Manager {c}, so it loads twice and can count "
                                    "visits and conversions twice. Remove it from the page code (keep it in Tag Manager).")
                seen_ids |= set(info["loads"])
                good.append(f"Tag Manager {c} is live (version {info.get('live_version')}) and loads: "
                            + (", ".join(info["loads"]) or "no Google Ads or Analytics tags yet"))
            else:
                good.append(f"Tag Manager {c} is on the site, but your Google login can't open it, so what it loads "
                            "can't be checked. Ask for access in Tag Manager.")
        out: dict[str, Any] = {"pages": pages, "containers": containers}
        if expected:
            want = {str(e).strip().upper() for e in expected}
            want = {("AW-" + e) if e.isdigit() else e for e in want}
            missing = sorted(want - seen_ids)
            found = sorted(want & seen_ids)
            out["expected_found"] = found
            out["expected_missing"] = missing
            for m in missing:
                problems.append(f"{m} isn't on the site (not in the page code"
                                + (" or in the Tag Manager containers it loads)." if containers else ")."))
        ok = [p for p in pages if "error" not in p]
        if ok and not any(p["consent_mode_default"] or p["cookie_banner"] for p in ok):
            out["note_consent"] = ("No consent mode or cookie banner seen. Needed for visitors in the EU/UK; elsewhere "
                                   "it's optional.")
        out["problems"] = problems
        out["good"] = good
        out["verdict"] = "Looks good" if ok and not problems else ("Problems found" if problems else "Couldn't read the site")
        out["limits"] = ("This reads the page code. Tags added later by JavaScript, plugins or Shopify pixels may not "
                         "show; Google Tag Assistant (tagassistant.google.com) shows what really fires.")
        return out


def register_site_tag_tools(mcp: FastMCP, w: Writer) -> SiteTags:
    st = SiteTags(w)

    @mcp.tool(annotations=READ_ONLY)
    async def tag_install_code(
        ids: Annotated[list[str], Field(description="Tag IDs: GTM-XXXXXXX (Tag Manager), G-XXXXXXX (GA4), AW-123456789 (Google Ads).")],
        platform: Annotated[Literal["wordpress", "shopify", "wix", "squarespace", "webflow", "other"],
                            Field(description="What the site is built with (check_site_tags can tell).")] = "other",
    ) -> dict[str, Any]:
        """Make the exact code to put on a client's website (Tag Manager, or the Google tag for GA4/Google Ads), where
        to paste it on their platform, and a short message to send their developer."""
        return await w.run("tag_install_code", None, lambda user, grt: install_code(ids, platform))

    @mcp.tool(annotations=READ_ONLY)
    async def check_site_tags(
        url: Annotated[str, Field(description="The website, https://...")],
        extra_pages: Annotated[list[str] | None, Field(description="Up to 5 more pages to check, e.g. the thank-you page.")] = None,
        expected_ids: Annotated[list[str] | None, Field(description="IDs that should be there, e.g. ['GTM-ABC123', 'G-XYZ', 'AW-123'].")] = None,
    ) -> dict[str, Any]:
        """Check a live website for Google tags: which Tag Manager, GA4 and Google Ads tags are on it, whether any
        load twice (double counting), whether expected IDs are missing, consent mode, and the site platform. For
        Tag Manager containers your login can open, also checks what they load when live."""
        return await w.run("check_site_tags", None, lambda user, grt: st.check(grt, url, extra_pages, expected_ids))

    return st
