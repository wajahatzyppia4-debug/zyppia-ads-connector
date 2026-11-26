"""Phase 5: Google Merchant Center (product listings for online shops), with the Merchant API v1.

Reading: accounts, products with their approval status and problems, and a
product performance report. Changing: product overrides (title, price,
availability, pausing ads...) go into a supplemental data source called
"Zyppia overrides", so the shop's own feed stays untouched and undo simply
removes the override. Everything is previewed first, like other changes.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .ads import AdsError
from .google_apis import GoogleApis, raw_change
from .writes import PREVIEW, Change, Writer

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
OVERRIDE_SOURCE = "Zyppia overrides"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ENUMS = {
    "availability": {"in_stock", "out_of_stock", "preorder", "backorder"},
    "condition": {"new", "refurbished", "used"},
}
# override name -> Merchant API productAttributes field
FIELDS = {
    "title": "title", "description": "description", "link": "link", "image_link": "imageLink", "brand": "brand",
    "availability": "availability", "condition": "condition", "price": "price", "sale_price": "salePrice",
    "google_product_category": "googleProductCategory", "product_type": "productTypes",
    "custom_label_0": "customLabel0", "custom_label_1": "customLabel1", "custom_label_2": "customLabel2",
    "custom_label_3": "customLabel3", "custom_label_4": "customLabel4",
}


def acct_id(ref: str) -> str:
    d = re.sub(r"^accounts/", "", str(ref or "").strip())
    if not d.isdigit():
        raise AdsError("Give the Merchant Center account ID (a number, merchant_list_accounts shows them).")
    return d


def attrs(p: dict[str, Any]) -> dict[str, Any]:
    return p.get("productAttributes") or p.get("attributes") or {}


def price_text(pr: dict[str, Any] | None) -> str | None:
    if not pr or pr.get("amountMicros") is None:
        return None
    return f"{Decimal(int(pr['amountMicros'])) / 1_000_000:.2f} {pr.get('currencyCode', '')}".strip()


def product_status(p: dict[str, Any]) -> dict[str, Any]:
    st = p.get("productStatus") or {}
    dests = {}
    for d in st.get("destinationStatuses") or []:
        ctx = d.get("reportingContext") or d.get("destination")
        if d.get("disapprovedCountries"):
            dests[ctx] = "disapproved in " + ", ".join(d["disapprovedCountries"])
        elif d.get("pendingCountries"):
            dests[ctx] = "pending in " + ", ".join(d["pendingCountries"])
        elif d.get("approvedCountries"):
            dests[ctx] = "approved"
    issues = [{"problem": i.get("description"), "detail": i.get("detail"), "severity": i.get("severity"),
               "attribute": i.get("attribute"), "fix": i.get("resolution"), "where": i.get("reportingContext"),
               "help": i.get("documentation")} for i in st.get("itemLevelIssues") or []]
    return {"destinations": dests, "issues": issues}


def summary(p: dict[str, Any]) -> dict[str, Any]:
    a = attrs(p)
    out = {"id": p.get("name", "").split("/")[-1], "offer_id": p.get("offerId"), "title": a.get("title"),
           "price": price_text(a.get("price")), "sale_price": price_text(a.get("salePrice")),
           "availability": a.get("availability"), "link": a.get("link"), "brand": a.get("brand"),
           "feed": f"{p.get('contentLanguage')}/{p.get('feedLabel')}"}
    out.update(product_status(p))
    return {k: v for k, v in out.items() if v not in (None, {}, [])}


def money_obj(value: Any, currency: str | None) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]{1,2})?)\s*([A-Za-z]{3})?\s*$", str(value))
    if not m:
        raise AdsError(f"Price '{value}' should look like 49.95 or '49.95 AUD'.")
    cur = (m.group(2) or currency or "").upper()
    if not cur:
        raise AdsError("Give the currency with the price, e.g. '49.95 AUD'.")
    return {"amountMicros": str(int(Decimal(m.group(1)) * 1_000_000)), "currencyCode": cur}


class Merchant:
    def __init__(self, w: Writer, apis: GoogleApis):
        self.w, self.apis = w, apis

    def accounts(self, grt: str) -> list[dict[str, Any]]:
        items = self.apis.list_all(grt, "merchant", "accounts/v1/accounts", "accounts", {"pageSize": 250})
        return [{"account_id": a.get("accountId") or a.get("name", "").split("/")[-1], "name": a.get("accountName"),
                 "time_zone": (a.get("timeZone") or {}).get("id"), "language": a.get("languageCode")} for a in items]

    def products_raw(self, grt: str, aid: str, limit: int = 5000) -> list[dict[str, Any]]:
        return self.apis.list_all(grt, "merchant", f"products/v1/accounts/{aid}/products", "products",
                                  {"pageSize": 1000}, limit=limit)

    def products(self, grt: str, ref: str, search: str | None, only_problems: bool, max_items: int) -> dict[str, Any]:
        aid = acct_id(ref)
        raw = self.products_raw(grt, aid)
        items = [summary(p) for p in raw]
        dests: Counter = Counter()
        issue_count: Counter = Counter()
        issue_examples: dict[str, list[str]] = {}
        for s in items:
            for ctx, state in (s.get("destinations") or {}).items():
                dests[f"{ctx}: {state.split(' in ')[0]}"] += 1
            for i in s.get("issues") or []:
                key = f"{i['problem']} ({i.get('severity')})"
                issue_count[key] += 1
                issue_examples.setdefault(key, [])
                if len(issue_examples[key]) < 5:
                    issue_examples[key].append(s.get("offer_id"))
        if search:
            q = search.lower()
            items = [s for s in items if q in (s.get("title") or "").lower() or q in str(s.get("offer_id")).lower()]
        if only_problems:
            items = [s for s in items if s.get("issues") or any("disapproved" in v for v in (s.get("destinations") or {}).values())]
        return {
            "account_id": aid, "product_count": len(raw),
            "status_counts": dict(sorted(dests.items())),
            "top_problems": [{"problem": k, "products": n, "examples": issue_examples[k]}
                             for k, n in issue_count.most_common(15)],
            "shown": min(len(items), max_items), "products": items[:max_items],
            "tip": "Fix feed problems at the source (the shop or feed). For quick fixes, merchant_product_override "
                   "changes a product's title, price, availability and more without touching the feed.",
        }

    def report(self, grt: str, ref: str, start: str, end: str, query: str | None, limit: int) -> dict[str, Any]:
        aid = acct_id(ref)
        if not query:
            if not (_DATE_RE.match(start) and _DATE_RE.match(end)):
                raise AdsError("Dates are YYYY-MM-DD.")
            query = ("SELECT offer_id, title, brand, clicks, impressions, click_through_rate, conversions, "
                     f"conversion_value FROM product_performance_view WHERE date BETWEEN '{start}' AND '{end}' "
                     f"ORDER BY clicks DESC LIMIT {limit}")
        elif not query.strip().lower().startswith("select"):
            raise AdsError("Only SELECT queries are allowed.")
        resp = self.apis.call(grt, "merchant", "POST", f"reports/v1/accounts/{aid}/reports:search",
                              body={"query": query, "pageSize": min(limit, 1000)})
        rows = []
        for r in resp.get("results") or []:
            view = next(iter(r.values()), {}) if r else {}
            row = {}
            for k, v in (view or {}).items():
                row[k] = price_text(v) if isinstance(v, dict) and "amountMicros" in v else v
            rows.append(row)
        return {"account_id": aid, "query": query, "row_count": len(rows), "rows": rows,
                "note": "Clicks and impressions here include free listings. For ad cost per product use run_gaql on "
                        "the Google Ads account with shopping_performance_view."}

    def find_product(self, grt: str, aid: str, ref: str) -> dict[str, Any]:
        ref = str(ref or "").strip()
        if not ref:
            raise AdsError("Give the product's offer ID (merchant_products shows them).")
        if ref.count("~") >= 2:
            try:
                return self.apis.call(grt, "merchant", "GET", f"products/v1/accounts/{aid}/products/{ref}")
            except AdsError:
                pass
        found = [p for p in self.products_raw(grt, aid) if p.get("offerId") == ref or p.get("name", "").endswith("/" + ref)]
        if not found:
            raise AdsError(f"No product with offer ID '{ref}' in Merchant Center account {aid}.")
        if len(found) > 1:
            raise AdsError(f"'{ref}' is in more than one feed: " + ", ".join(p["name"].split("/")[-1] for p in found)
                           + ". Use that full ID (language~feed~offer).")
        return found[0]

    def override(self, grt: str, ref: str, product_ref: str, a: dict[str, Any], clear: bool) -> Change:
        aid = acct_id(ref)
        prod = self.find_product(grt, aid, product_ref)
        cur = attrs(prod)
        sources = self.apis.list_all(grt, "merchant", f"datasources/v1/accounts/{aid}/dataSources", "dataSources")
        ours = next((s for s in sources if s.get("displayName") == OVERRIDE_SOURCE
                     and "supplementalProductDataSource" in s), None)
        title = f"product '{cur.get('title') or prod.get('offerId')}'"
        ch = Change(None, "", where=f"Merchant Center account {aid}")
        key = {"offerId": prod["offerId"], "contentLanguage": prod.get("contentLanguage"), "feedLabel": prod.get("feedLabel")}
        input_name = f"accounts/{aid}/productInputs/{prod['contentLanguage']}~{prod['feedLabel']}~{prod['offerId']}"

        if clear:
            if not ours:
                raise AdsError("There are no Zyppia overrides in this account.")
            ch.title = f"Remove Zyppia overrides from {title}"
            ch.lines.append(f"remove the Zyppia override for {title}; it goes back to what the shop's feed says")
            ch.gtm.append({"a": "merchant", "m": "DELETE", "p": f"products/v1/{input_name}",
                           "q": {"dataSource": ours["name"]}, "d": f"remove the Zyppia override for {title}",
                           "n": "removing an override (preview a new override to put it back)"})
            return ch

        new: dict[str, Any] = {}
        for k, field in FIELDS.items():
            v = a.get(k)
            if v is None:
                continue
            if k in ENUMS:
                v = str(v).lower().replace(" ", "_")
                if v not in ENUMS[k]:
                    raise AdsError(f"{k} must be one of: {', '.join(sorted(ENUMS[k]))}.")
                v = v.upper()  # Merchant API v1 enums
            elif k in ("price", "sale_price"):
                v = money_obj(v, (cur.get("price") or {}).get("currencyCode"))
            elif k == "product_type":
                v = [v] if isinstance(v, str) else list(v)
            elif k == "title" and len(str(v)) > 150:
                raise AdsError("Titles can be at most 150 characters.")
            elif k == "description" and len(str(v)) > 5000:
                raise AdsError("Descriptions can be at most 5000 characters.")
            new[field] = v
        if a.get("pause_ads") is not None:
            new["pause"] = "ads" if a["pause_ads"] else ""
        new.update(a.get("attributes") or {})
        if not new:
            raise AdsError("Say what to change: title, description, price, sale_price, availability, pause_ads, ...")

        ch.title = f"Override {title} in Merchant Center"
        for field, v in new.items():
            old = cur.get(field)
            shown_old = price_text(old) if isinstance(old, dict) else old
            shown_new = price_text(v) if isinstance(v, dict) and "amountMicros" in v else v
            if field == "pause":
                ch.lines.append("pause this product in Shopping ads and Performance Max" if v else "un-pause its ads")
            else:
                ch.lines.append(f"{field}: {shown_old!r} -> {shown_new!r}")
        if not ours:
            ch.gtm.append({"a": "merchant", "m": "POST", "p": f"datasources/v1/accounts/{aid}/dataSources",
                           "b": {"displayName": OVERRIDE_SOURCE, "supplementalProductDataSource": {}},
                           "s": "source", "f": "name", "d": f"create the '{OVERRIDE_SOURCE}' supplemental data source",
                           "u": {"a": "merchant", "m": "DELETE", "p": "datasources/v1/${source}",
                                 "d": f"delete the '{OVERRIDE_SOURCE}' data source"}})
            source = "${source}"
            ch.lines.append(f"(first time in this account: creates a supplemental data source '{OVERRIDE_SOURCE}' "
                            "and attaches it to the product's feed)")
        else:
            source = ours["name"]
        primary = next((s for s in sources if s.get("name") == prod.get("dataSource")), None)
        if primary and "primaryProductDataSource" in primary:
            rule = (primary["primaryProductDataSource"].get("defaultRule") or {}).get("takeFromDataSources") \
                or [{"self": True}]
            linked = ours and any(r.get("supplementalDataSourceName") == ours["name"] for r in rule)
            if not linked:
                new_rule = [{"supplementalDataSourceName": source}] + rule  # first = wins over the feed
                ch.gtm.append({"a": "merchant", "m": "PATCH", "p": f"datasources/v1/{primary['name']}",
                               "q": {"updateMask": "primaryProductDataSource.defaultRule"},
                               "b": {"primaryProductDataSource": {"defaultRule": {"takeFromDataSources": new_rule}}},
                               "d": f"let '{OVERRIDE_SOURCE}' override the feed '{primary.get('displayName')}'",
                               "u": {"a": "merchant", "m": "PATCH", "p": f"datasources/v1/{primary['name']}",
                                     "q": {"updateMask": "primaryProductDataSource.defaultRule"},
                                     "b": {"primaryProductDataSource": {"defaultRule": {"takeFromDataSources": rule}}},
                                     "d": f"put the feed '{primary.get('displayName')}' rules back"}})
        elif not ours:
            ch.warnings.append("Couldn't find this product's main feed, so the override may need attaching to it in "
                               "Merchant Center (Products > Data sources).")
        ch.gtm.append({"a": "merchant", "m": "POST", "p": f"products/v1/accounts/{aid}/productInputs:insert",
                       "q": {"dataSource": source}, "b": {**key, "productAttributes": new},
                       "d": f"save the override for {title}",
                       "u": {"a": "merchant", "m": "DELETE", "p": f"products/v1/{input_name}",
                             "q": {"dataSource": source}, "d": f"remove the override for {title}"}})
        ch.warnings.append("This replaces any earlier Zyppia override on this product: fields overridden before and "
                           "not listed here go back to the feed. Merchant Center can take up to an hour to update, "
                           "and changed products may be reviewed again.")
        if "price" in new or "salePrice" in new:
            ch.large.append("changing a price: it must match the price on the website, or Google disapproves the product")
            ch.warnings.append("The price on the website must match, or Google disapproves the product.")
        return ch

    def register_project(self, grt: str, user: str, ref: str) -> Change:
        aid = acct_id(ref)
        ch = Change(None, "Register this connector with Merchant Center", where=f"Merchant Center account {aid}")
        ch.lines.append(f"register this connector's Google Cloud project with Merchant Center (contact: {user})")
        ch.gtm.append({"a": "merchant", "m": "POST", "p": f"accounts/v1/accounts/{aid}/developerRegistration:registerGcp",
                       "b": {"developerEmail": user}, "d": "register the Google Cloud project",
                       "n": "registering the project (it's harmless and only needed once)"})
        return ch


Account = Annotated[str, Field(description="Merchant Center account ID (a number from merchant_list_accounts).")]


def register_merchant_tools(mcp: FastMCP, w: Writer, apis: GoogleApis) -> Merchant:
    mc = Merchant(w, apis)

    def change(tool: str, build):
        return w.run(tool, None, lambda user, grt: w.preview(user, grt, build(grt, user)))

    @mcp.tool(annotations=READ_ONLY)
    async def merchant_list_accounts() -> dict[str, Any]:
        """List every Google Merchant Center account your Google login can access (online shops' product feeds)."""
        def go(user, grt):
            items = mc.accounts(grt)
            return {"count": len(items), "accounts": items}
        return await w.run("merchant_list_accounts", None, go)

    @mcp.tool(annotations=READ_ONLY)
    async def merchant_products(
        account_id: Account,
        search: Annotated[str | None, Field(description="Only products whose title or offer ID contains this.")] = None,
        only_problems: Annotated[bool, Field(description="Only products with problems or disapprovals.")] = False,
        max_items: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        """A shop's products in Merchant Center: title, price, availability, approval status for Shopping ads and
        free listings, and every problem with Google's suggested fix. Also counts the most common problems."""
        return await w.run("merchant_products", None, lambda user, grt: mc.products(
            grt, account_id, search, only_problems, max_items))

    @mcp.tool(annotations=READ_ONLY)
    async def merchant_report(
        account_id: Account,
        start_date: Annotated[str | None, Field(description="YYYY-MM-DD. Default: 30 days ago.")] = None,
        end_date: Annotated[str | None, Field(description="YYYY-MM-DD. Default: yesterday.")] = None,
        query: Annotated[str | None, Field(description=(
            "Optional Merchant Reports query, e.g. SELECT offer_id, title, clicks FROM product_performance_view "
            "WHERE date BETWEEN '2026-11-01' AND '2026-11-25'. Other views: product_view, price_competitiveness_product_view, "
            "best_sellers_product_cluster_view."))] = None,
        max_rows: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        """Product performance from Merchant Center: clicks, impressions, click rate and conversions per product
        (Shopping ads and free listings), or any other Merchant report with a query."""
        s = start_date or (date.today() - timedelta(days=30)).isoformat()
        e = end_date or (date.today() - timedelta(days=1)).isoformat()
        return await w.run("merchant_report", None, lambda user, grt: mc.report(grt, account_id, s, e, query, max_rows))

    @mcp.tool(annotations=PREVIEW)
    async def merchant_product_override(
        account_id: Account,
        product: Annotated[str, Field(description="The product's offer ID (merchant_products shows them).")],
        title: str | None = None,
        description: str | None = None,
        price: Annotated[str | None, Field(description="e.g. '49.95 AUD' (must match the website).")] = None,
        sale_price: Annotated[str | None, Field(description="e.g. '39.95 AUD'.")] = None,
        availability: Annotated[str | None, Field(description="in_stock, out_of_stock, preorder or backorder.")] = None,
        condition: Annotated[str | None, Field(description="new, refurbished or used.")] = None,
        brand: str | None = None,
        link: str | None = None,
        image_link: str | None = None,
        google_product_category: str | None = None,
        product_type: str | None = None,
        custom_label_0: Annotated[str | None, Field(description="Custom labels group products in Shopping/PMax campaigns.")] = None,
        custom_label_1: str | None = None,
        custom_label_2: str | None = None,
        custom_label_3: str | None = None,
        custom_label_4: str | None = None,
        pause_ads: Annotated[bool | None, Field(description="true = stop showing it in ads (free listings continue).")] = None,
        attributes: Annotated[dict[str, Any] | None, Field(description=(
            "Any other Merchant API productAttributes fields, e.g. {gtins: ['...'], shipping: [...]}."))] = None,
        clear: Annotated[bool, Field(description="true = remove the Zyppia override so the feed's values come back.")] = False,
    ) -> dict[str, Any]:
        """Preview changing a product in Merchant Center without touching the shop's feed: title, description,
        price, sale price, availability, custom labels, pausing its ads and more. Undo removes the override."""
        a = dict(title=title, description=description, price=price, sale_price=sale_price, availability=availability,
                 condition=condition, brand=brand, link=link, image_link=image_link,
                 google_product_category=google_product_category, product_type=product_type,
                 custom_label_0=custom_label_0, custom_label_1=custom_label_1, custom_label_2=custom_label_2,
                 custom_label_3=custom_label_3, custom_label_4=custom_label_4, pause_ads=pause_ads, attributes=attributes)
        return await change("merchant_product_override", lambda grt, user: mc.override(grt, account_id, product, a, clear))

    @mcp.tool(annotations=PREVIEW)
    async def merchant_register_project(account_id: Account) -> dict[str, Any]:
        """Preview the one-time registration Merchant Center needs before this connector can use it. Run it only
        when a Merchant tool says the project isn't registered."""
        return await change("merchant_register_project", lambda grt, user: mc.register_project(grt, user, account_id))

    @mcp.tool(annotations=PREVIEW)
    async def merchant_advanced_change(
        calls: Annotated[list[dict[str, Any]], Field(description=(
            "Merchant API v1 calls: [{method: POST|PATCH|DELETE, path: 'products/v1/accounts/123/productInputs:insert', "
            "params: {dataSource: 'accounts/123/dataSources/456'}, body: {...}}]. E.g. data sources, shipping, "
            "promotions, local inventory, or linking to Google Ads."))],
        description: Annotated[str, Field(description="Plain-language summary of what this does, shown to the person.")],
    ) -> dict[str, Any]:
        """Preview any other Merchant Center change with raw Merchant API calls. Use merchant_product_override when it
        fits. Always needs extra confirmation."""
        return await change("merchant_advanced_change", lambda grt, user: raw_change("merchant", "Merchant Center",
                                                                                     calls, description))

    return mc
