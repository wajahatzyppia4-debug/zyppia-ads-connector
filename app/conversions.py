"""Phase 4: Google Ads conversion actions (list, create, update, remove).

Same preview -> apply_change flow as writes.py. Conversion actions are what
Google Ads counts as a result: a form sent, a call, a purchase, or an
offline sale imported later.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .ads import Account, AdsError, pretty_id
from .writes import CID, MAX_ITEMS, PREVIEW, Change, Writer, _ids_clause, _num, send_to_ids

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

# Plain names for the conversion types the connector can create -> Google's type.
TYPES = {
    "website": "WEBPAGE",                    # a page view or event on the site, tracked with a tag (e.g. via GTM)
    "phone_call_from_ads": "AD_CALL",        # calls from call assets / call-only ads
    "phone_call_from_website": "WEBSITE_CALL",  # calls to a Google forwarding number shown on the site
    "phone_number_click": "CLICK_TO_CALL",   # taps on the phone number on the mobile site
    "import_offline_clicks": "UPLOAD_CLICKS",  # offline sales/leads imported later (by GCLID)
    "import_offline_calls": "UPLOAD_CALLS",    # call conversions imported later
}
CALL_TYPES = {"AD_CALL", "WEBSITE_CALL"}
DEFAULT_CATEGORY = {
    "WEBPAGE": "SUBMIT_LEAD_FORM", "AD_CALL": "PHONE_CALL_LEAD", "WEBSITE_CALL": "PHONE_CALL_LEAD",
    "CLICK_TO_CALL": "PHONE_CALL_LEAD", "UPLOAD_CLICKS": "IMPORTED_LEAD", "UPLOAD_CALLS": "PHONE_CALL_LEAD",
}
CATEGORIES = [
    "DEFAULT", "PAGE_VIEW", "PURCHASE", "SIGNUP", "DOWNLOAD", "ADD_TO_CART", "BEGIN_CHECKOUT", "SUBSCRIBE_PAID",
    "PHONE_CALL_LEAD", "IMPORTED_LEAD", "SUBMIT_LEAD_FORM", "BOOK_APPOINTMENT", "REQUEST_QUOTE", "GET_DIRECTIONS",
    "OUTBOUND_CLICK", "CONTACT", "ENGAGEMENT", "QUALIFIED_LEAD", "CONVERTED_LEAD", "MESSAGE_LEAD",
]
LEAD_CATEGORIES = {"PHONE_CALL_LEAD", "IMPORTED_LEAD", "SUBMIT_LEAD_FORM", "BOOK_APPOINTMENT", "REQUEST_QUOTE",
                   "CONTACT", "SIGNUP", "QUALIFIED_LEAD", "CONVERTED_LEAD", "MESSAGE_LEAD"}
ATTRIBUTION = {"data_driven": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN", "last_click": "GOOGLE_ADS_LAST_CLICK"}

ConvType = Literal["website", "phone_call_from_ads", "phone_call_from_website", "phone_number_click",
                   "import_offline_clicks", "import_offline_calls"]
Category = Annotated[str | None, Field(description=(
    "What it counts: " + ", ".join(CATEGORIES) + ". Default: SUBMIT_LEAD_FORM for website, PHONE_CALL_LEAD for calls, "
    "IMPORTED_LEAD for imports."))]
Counting = Annotated[Literal["ONE_PER_CLICK", "MANY_PER_CLICK"] | None, Field(description=(
    "ONE_PER_CLICK ('one') suits leads and calls; MANY_PER_CLICK ('every') suits purchases."))]

FIELDS = [
    "conversion_action.id", "conversion_action.name", "conversion_action.status", "conversion_action.type",
    "conversion_action.category", "conversion_action.origin", "conversion_action.primary_for_goal",
    "conversion_action.counting_type", "conversion_action.value_settings.default_value",
    "conversion_action.value_settings.default_currency_code",
    "conversion_action.value_settings.always_use_default_value",
    "conversion_action.click_through_lookback_window_days", "conversion_action.view_through_lookback_window_days",
    "conversion_action.attribution_model_settings.attribution_model",
    "conversion_action.phone_call_duration_seconds", "conversion_action.owner_customer",
]


def _quote(text: str) -> str:
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _category(value: str | None, ctype: str) -> str:
    cat = (value or DEFAULT_CATEGORY.get(ctype, "DEFAULT")).strip().upper().replace(" ", "_")
    if cat not in CATEGORIES:
        raise AdsError(f"Unknown category '{value}'. Use one of: {', '.join(CATEGORIES)}.")
    return cat


class Conversions:
    def __init__(self, w: Writer):
        self.w = w

    # ------------------------------------------------------------- reading

    def tracking_setting(self, grt: str, acct: Account) -> dict[str, Any]:
        rows = self.w.rows(grt, acct, "SELECT customer.conversion_tracking_setting.conversion_tracking_id, "
                                      "customer.conversion_tracking_setting.google_ads_conversion_customer, "
                                      "customer.conversion_tracking_setting.conversion_tracking_status FROM customer")
        r = rows[0] if rows else {}
        owner = (r.get("customer.conversion_tracking_setting.google_ads_conversion_customer") or "").split("/")[-1]
        out = {
            "conversion_id": str(r.get("customer.conversion_tracking_setting.conversion_tracking_id") or "") or None,
            "status": r.get("customer.conversion_tracking_setting.conversion_tracking_status"),
            "managed_by": pretty_id(owner) if owner and owner != acct.customer_id and len(owner) == 10 else None,
        }
        return out

    def find(self, grt: str, acct: Account, ids: list[str]) -> dict[int, dict[str, Any]]:
        nums = [_num(i, "conversion action ID") for i in ids]
        rows = self.w.rows(grt, acct, f"SELECT {', '.join(FIELDS)}, conversion_action.tag_snippets FROM conversion_action "
                                      f"WHERE conversion_action.id IN {_ids_clause(nums)}")
        got = {r["conversion_action.id"]: r for r in rows}
        missing = [str(n) for n in nums if n not in got]
        if missing:
            raise AdsError(f"Couldn't find conversion action(s) {', '.join(missing)} in {acct.name}. "
                           "Use list_conversion_actions to see the IDs.")
        return {n: got[n] for n in nums}

    def tag_ids(self, grt: str, acct: Account, conversion_action_id: str) -> dict[str, str]:
        r = self.find(grt, acct, [conversion_action_id])[_num(conversion_action_id)]
        ids = send_to_ids(r.get("conversion_action.tag_snippets"))
        if not ids:
            raise AdsError(f"Conversion action '{r['conversion_action.name']}' has no website tag "
                           f"(it's a {r['conversion_action.type']} conversion), so it can't be set up in Tag Manager.")
        return {**ids, "name": r["conversion_action.name"]}

    def list(self, grt: str, acct: Account, include_removed: bool) -> dict[str, Any]:
        where = "" if include_removed else " WHERE conversion_action.status != 'REMOVED'"
        rows = self.w.rows(grt, acct, f"SELECT {', '.join(FIELDS)}, conversion_action.tag_snippets "
                                      f"FROM conversion_action{where} ORDER BY conversion_action.name")
        stats: dict[int, dict[str, float]] = {}
        stats_note = None
        try:
            for r in self.w.rows(grt, acct, "SELECT conversion_action.id, metrics.all_conversions, "
                                            "metrics.all_conversions_value FROM conversion_action "
                                            "WHERE segments.date DURING LAST_30_DAYS"):
                stats[r["conversion_action.id"]] = {"conversions_30d": round(r.get("metrics.all_conversions") or 0, 2),
                                                    "value_30d": round(r.get("metrics.all_conversions_value") or 0, 2)}
        except Exception:  # noqa: BLE001 - stats are a bonus
            stats_note = "Couldn't read the last 30 days of conversions."
        items = []
        primaries_by_cat: dict[str, list[str]] = {}
        for r in rows:
            cid = r["conversion_action.id"]
            st = stats.get(cid, {"conversions_30d": 0, "value_30d": 0})
            ids = send_to_ids(r.get("conversion_action.tag_snippets"))
            item = {
                "id": str(cid), "name": r["conversion_action.name"], "status": r["conversion_action.status"],
                "type": r["conversion_action.type"], "category": r["conversion_action.category"],
                "origin": r.get("conversion_action.origin"), "primary": r.get("conversion_action.primary_for_goal"),
                "counting": r.get("conversion_action.counting_type"),
                "value": r.get("conversion_action.value_settings.default_value"),
                "currency": r.get("conversion_action.value_settings.default_currency_code"),
                "always_use_this_value": r.get("conversion_action.value_settings.always_use_default_value"),
                "click_lookback_days": r.get("conversion_action.click_through_lookback_window_days"),
                "view_lookback_days": r.get("conversion_action.view_through_lookback_window_days"),
                "attribution": r.get("conversion_action.attribution_model_settings.attribution_model"),
                "call_length_seconds": r.get("conversion_action.phone_call_duration_seconds") or None,
                **st,
            }
            if ids:
                item["gtm"] = {"conversion_id": ids["conversion_id"], "conversion_label": ids["conversion_label"],
                               "send_to": f"AW-{ids['conversion_id']}/{ids['conversion_label']}"}
            problems = []
            if item["status"] == "ENABLED" and item["primary"] and not st["conversions_30d"]:
                problems.append("no conversions in the last 30 days: check the tag fires (gtm_workspace) "
                                "or the import is running")
            if item["counting"] == "MANY_PER_CLICK" and item["category"] in LEAD_CATEGORIES:
                problems.append("counts every conversion; leads usually count one per click (update counting_type)")
            if item["status"] == "ENABLED" and item["primary"]:
                primaries_by_cat.setdefault(item["category"], []).append(item["name"])
            if problems:
                item["problems"] = problems
            items.append({k: v for k, v in item.items() if v is not None})
        warnings = [f"{len(n)} primary conversion actions count the same thing ({cat}): {', '.join(n)}. "
                    "If they track the same action, results are counted twice; make one secondary (primary=false)."
                    for cat, n in primaries_by_cat.items() if len(n) > 1 and cat not in ("PURCHASE", "DEFAULT")]
        tracking = self.tracking_setting(grt, acct)
        if tracking["managed_by"]:
            warnings.insert(0, f"This account uses conversion actions from manager {tracking['managed_by']} "
                               "(cross-account conversion tracking). Create and change them in that account.")
        out = {"account": acct.name, "customer_id": pretty_id(acct.customer_id), "currency": acct.currency,
               "conversion_tracking": tracking, "count": len(items), "conversion_actions": items, "warnings": warnings}
        if stats_note:
            out["note"] = stats_note
        return out

    # ------------------------------------------------------------- changes

    def create(self, grt: str, acct: Account, a: dict[str, Any], ch: Change | None = None) -> Change:
        e = self.w.t.enums
        name = " ".join(str(a.get("name") or "").split())
        if not name or len(name) > 100:
            raise AdsError("The conversion action needs a name (max 100 characters).")
        if self.w.rows(grt, acct, f"SELECT conversion_action.id FROM conversion_action WHERE conversion_action.name = "
                                  f"{_quote(name)} AND conversion_action.status != 'REMOVED'"):
            raise AdsError(f"There's already a conversion action called '{name}' in {acct.name}. "
                           "Pick another name, or change that one with update_conversion_action.")
        kind = a.get("type") or "website"
        if kind not in TYPES:
            raise AdsError(f"Unknown type '{kind}'. Use one of: {', '.join(TYPES)}.")
        ctype = TYPES[kind]
        cat = _category(a.get("category"), ctype)
        counting = a.get("counting_type") or ("MANY_PER_CLICK" if cat == "PURCHASE" else "ONE_PER_CLICK")
        ch = ch or Change(acct, f"Create conversion action '{name}'")
        op = self.w.op()
        c = op.conversion_action_operation.create
        c.name = name
        c.type_ = e.ConversionActionTypeEnum[ctype]
        c.category = e.ConversionActionCategoryEnum[cat]
        c.status = e.ConversionActionStatusEnum.ENABLED
        c.counting_type = e.ConversionActionCountingTypeEnum[counting]
        c.primary_for_goal = bool(a.get("primary", True))
        lines = [f"conversion action '{name}': {kind.replace('_', ' ')} ({ctype}), counts {cat}",
                 f"counting: {'one per click' if counting == 'ONE_PER_CLICK' else 'every conversion'}; "
                 + ("primary (used for bidding and the Conversions column)" if c.primary_for_goal
                    else "secondary (reported only, not used for bidding)")]
        lb = a.get("click_lookback_days")
        if lb is not None:
            if not 1 <= int(lb) <= 90:
                raise AdsError("click_lookback_days must be 1 to 90.")
            c.click_through_lookback_window_days = int(lb)
        vlb = a.get("view_lookback_days")
        if vlb is not None:
            if ctype != "WEBPAGE" or not 1 <= int(vlb) <= 30:
                raise AdsError("view_lookback_days (1 to 30) only applies to website conversions.")
            c.view_through_lookback_window_days = int(vlb)
        lines.append(f"click lookback: {lb or 30} days")
        if a.get("value") is not None:
            v = float(a["value"])
            if v < 0:
                raise AdsError("The value can't be negative.")
            c.value_settings.default_value = v
            c.value_settings.default_currency_code = (a.get("currency") or acct.currency).upper()
            c.value_settings.always_use_default_value = not a.get("use_value_from_tag", False)
            lines.append(f"value: {v:,.2f} {c.value_settings.default_currency_code}"
                         + (" (or the value the tag sends)" if a.get("use_value_from_tag") else " for each conversion"))
        else:
            lines.append("value: none set (Google's default)")
        if ctype in CALL_TYPES:
            secs = int(a.get("call_length_seconds") or 60)
            if not 0 <= secs <= 10_000:
                raise AdsError("call_length_seconds must be 0 to 10000.")
            c.phone_call_duration_seconds = secs
            lines.append(f"counts calls lasting at least {secs} seconds")
        elif a.get("call_length_seconds"):
            raise AdsError("call_length_seconds only applies to phone_call_from_ads and phone_call_from_website.")
        if a.get("attribution"):
            if a["attribution"] not in ATTRIBUTION:
                raise AdsError("attribution must be data_driven or last_click.")
            c.attribution_model_settings.attribution_model = e.AttributionModelEnum[ATTRIBUTION[a["attribution"]]]
            lines.append(f"attribution: {a['attribution'].replace('_', ' ')}")
        ch.ops.append(op)
        ch.lines += lines
        if ctype == "WEBSITE_CALL":
            ch.warnings.append("Calls from the website also need the Google forwarding number tag on the site and a "
                               "call asset. Add the 'Google Ads Calls from Website Conversion' tag with gtm_tag (kind custom, type 'awcc').")
        if ctype in ("UPLOAD_CLICKS", "UPLOAD_CALLS"):
            ch.warnings.append("Imported conversions only count once you upload them (from a CRM, a sheet, or Zapier) "
                               "with the click ID (GCLID) or caller details.")
        if ctype == "WEBPAGE":
            ch.warnings.append("A website conversion counts nothing until its tag is on the site. "
                               "setup_conversion_tracking does both in one step with Tag Manager.")
        return ch

    def update(self, grt: str, acct: Account, conversion_action_id: str, a: dict[str, Any]) -> Change:
        e = self.w.t.enums
        cid = _num(conversion_action_id, "conversion action ID")
        r = self.find(grt, acct, [conversion_action_id])[cid]
        label = f"'{r['conversion_action.name']}'"
        ch = Change(acct, f"Change conversion action {label}")
        op = self.w.op()
        c = op.conversion_action_operation.update
        c.resource_name = self.w.rn(acct, "conversionActions", cid)
        paths = op.conversion_action_operation.update_mask.paths

        def chg(path: str, line: str) -> None:
            paths.append(path)
            ch.lines.append(line)

        if a.get("name"):
            c.name = " ".join(a["name"].split())
            chg("name", f"name: {label} -> '{c.name}'")
        if a.get("status"):
            c.status = e.ConversionActionStatusEnum[a["status"]]
            chg("status", f"status: {r['conversion_action.status']} -> {a['status']}")
        if a.get("category"):
            cat = _category(a["category"], r["conversion_action.type"])
            c.category = e.ConversionActionCategoryEnum[cat]
            chg("category", f"category: {r['conversion_action.category']} -> {cat}")
        if a.get("counting_type"):
            c.counting_type = e.ConversionActionCountingTypeEnum[a["counting_type"]]
            chg("counting_type", f"counting: {r.get('conversion_action.counting_type')} -> {a['counting_type']}")
        if a.get("primary") is not None:
            c.primary_for_goal = bool(a["primary"])
            chg("primary_for_goal", f"primary: {r.get('conversion_action.primary_for_goal')} -> {bool(a['primary'])}")
            if not a["primary"]:
                ch.warnings.append("A secondary conversion no longer counts in the Conversions column or for "
                                   "Smart Bidding. Campaigns bidding on conversions may change how they spend.")
        if a.get("value") is not None:
            c.value_settings.default_value = float(a["value"])
            chg("value_settings.default_value",
                f"value: {r.get('conversion_action.value_settings.default_value')} -> {float(a['value']):,.2f}")
        if a.get("currency"):
            c.value_settings.default_currency_code = a["currency"].upper()
            chg("value_settings.default_currency_code",
                f"currency: {r.get('conversion_action.value_settings.default_currency_code')} -> {a['currency'].upper()}")
        if a.get("use_value_from_tag") is not None:
            c.value_settings.always_use_default_value = not a["use_value_from_tag"]
            chg("value_settings.always_use_default_value",
                "value: " + ("use the value the tag sends" if a["use_value_from_tag"] else "always use the default value"))
        if a.get("click_lookback_days") is not None:
            if not 1 <= int(a["click_lookback_days"]) <= 90:
                raise AdsError("click_lookback_days must be 1 to 90.")
            c.click_through_lookback_window_days = int(a["click_lookback_days"])
            chg("click_through_lookback_window_days",
                f"click lookback: {r.get('conversion_action.click_through_lookback_window_days')} -> "
                f"{int(a['click_lookback_days'])} days")
        if a.get("view_lookback_days") is not None:
            if not 1 <= int(a["view_lookback_days"]) <= 30:
                raise AdsError("view_lookback_days must be 1 to 30.")
            c.view_through_lookback_window_days = int(a["view_lookback_days"])
            chg("view_through_lookback_window_days",
                f"view lookback: {r.get('conversion_action.view_through_lookback_window_days')} -> "
                f"{int(a['view_lookback_days'])} days")
        if a.get("call_length_seconds") is not None:
            c.phone_call_duration_seconds = int(a["call_length_seconds"])
            chg("phone_call_duration_seconds",
                f"minimum call length: {r.get('conversion_action.phone_call_duration_seconds')} -> "
                f"{int(a['call_length_seconds'])} seconds")
        if a.get("attribution"):
            if a["attribution"] not in ATTRIBUTION:
                raise AdsError("attribution must be data_driven or last_click.")
            c.attribution_model_settings.attribution_model = e.AttributionModelEnum[ATTRIBUTION[a["attribution"]]]
            chg("attribution_model_settings.attribution_model",
                f"attribution: {r.get('conversion_action.attribution_model_settings.attribution_model')} -> "
                f"{ATTRIBUTION[a['attribution']]}")
        if not paths:
            raise AdsError("Say what to change: name, status, category, counting_type, primary, value, currency, "
                           "use_value_from_tag, click_lookback_days, view_lookback_days, call_length_seconds or attribution.")
        if r.get("conversion_action.origin") not in (None, "WEBSITE", "CALL_FROM_ADS", "IMPORT", "UNSPECIFIED", "UNKNOWN") \
                or str(r.get("conversion_action.type", "")).startswith("GOOGLE_ANALYTICS"):
            ch.warnings.append("This conversion comes from another product (e.g. Google Analytics), so Google may only "
                               "allow some settings to change here.")
        ch.ops.append(op)
        return ch

    def remove(self, grt: str, acct: Account, ids: list[str], confirm_names: list[str] | None) -> Change:
        if not ids or len(ids) > MAX_ITEMS:
            raise AdsError(f"Give 1 to {MAX_ITEMS} conversion action IDs.")
        found = self.find(grt, acct, ids)
        names = sorted(r["conversion_action.name"].strip().lower() for r in found.values())
        given = sorted(n.strip().lower() for n in (confirm_names or []))
        if names != given:
            raise AdsError("To remove a conversion action, the person must type its exact name. Ask them to confirm "
                           "these names and pass them as confirm_names: "
                           + ", ".join(f"'{r['conversion_action.name']}'" for r in found.values()))
        ch = Change(acct, f"Remove {len(found)} conversion action(s)")
        for cid, r in found.items():
            op = self.w.op()
            op.conversion_action_operation.remove = self.w.rn(acct, "conversionActions", cid)
            ch.ops.append(op)
            ch.lines.append(f"remove conversion action '{r['conversion_action.name']}' ({r['conversion_action.type']})")
            if r.get("conversion_action.primary_for_goal") and r["conversion_action.status"] == "ENABLED":
                ch.warnings.append(f"'{r['conversion_action.name']}' is a primary conversion: campaigns bidding on "
                                   "conversions lose it, which can change how they spend.")
        ch.large.append("removing a conversion action can't be undone; it stops counting (past data stays in reports). "
                        "Setting status HIDDEN with update_conversion_action is the softer option")
        ch.warnings.append("Also remove or pause its tag in Tag Manager (gtm_tag action 'delete'), or the website "
                           "keeps sending data for it.")
        return ch


# --------------------------------------------------------------------- tools


def register_conversion_tools(mcp: FastMCP, w: Writer) -> Conversions:
    cv = Conversions(w)

    @mcp.tool(annotations=READ_ONLY)
    async def list_conversion_actions(
        customer_id: CID,
        include_removed: bool = False,
    ) -> dict[str, Any]:
        """List an account's conversion actions (what Google Ads counts as a result) with type, category, primary
        or secondary, counting, value, lookback, attribution, conversions in the last 30 days, the Tag Manager IDs
        (conversion ID and label) for website ones, and likely problems to fix."""
        return await w.run("list_conversion_actions", customer_id,
                           lambda user, grt: cv.list(grt, w.ads.account(user, grt, customer_id), include_removed))

    @mcp.tool(annotations=PREVIEW)
    async def create_conversion_action(
        customer_id: CID,
        name: str,
        type: Annotated[ConvType, Field(description=(
            "website: a thank-you page or event on the site (tag needed, e.g. setup_conversion_tracking). "
            "phone_call_from_ads: calls from call assets. phone_call_from_website: calls to a Google forwarding "
            "number on the site. phone_number_click: taps on the number on the mobile site. "
            "import_offline_clicks / import_offline_calls: results imported later from a CRM."))] = "website",
        category: Category = None,
        value: Annotated[float | None, Field(description="Value of each conversion in the account's currency.")] = None,
        currency: Annotated[str | None, Field(description="3-letter currency code. Default: the account's.")] = None,
        use_value_from_tag: Annotated[bool, Field(description="Use the value the tag/import sends, falling back to value.")] = False,
        counting_type: Counting = None,
        primary: Annotated[bool, Field(description="Primary = counts in Conversions and for bidding. False = report only.")] = True,
        click_lookback_days: Annotated[int | None, Field(description="1-90, default 30.")] = None,
        view_lookback_days: Annotated[int | None, Field(description="Website only, 1-30, default 1.")] = None,
        call_length_seconds: Annotated[int | None, Field(description="Calls only: minimum call length, default 60.")] = None,
        attribution: Literal["data_driven", "last_click"] | None = None,
    ) -> dict[str, Any]:
        """Preview creating a conversion action (website, phone call or imported). Changes nothing until apply_change.
        For a website conversion with its Tag Manager tag in one go, use setup_conversion_tracking instead."""
        a = dict(name=name, type=type, category=category, value=value, currency=currency,
                 use_value_from_tag=use_value_from_tag, counting_type=counting_type, primary=primary,
                 click_lookback_days=click_lookback_days, view_lookback_days=view_lookback_days,
                 call_length_seconds=call_length_seconds, attribution=attribution)
        return await w.prepared("create_conversion_action", customer_id, lambda g, acct: cv.create(g, acct, a))

    @mcp.tool(annotations=PREVIEW)
    async def update_conversion_action(
        customer_id: CID,
        conversion_action_id: Annotated[str, Field(description="ID from list_conversion_actions.")],
        name: str | None = None,
        status: Annotated[Literal["ENABLED", "HIDDEN"] | None, Field(description=(
            "HIDDEN stops it showing in the conversion list but keeps its data (softer than removing)."))] = None,
        category: Category = None,
        counting_type: Counting = None,
        primary: bool | None = None,
        value: float | None = None,
        currency: str | None = None,
        use_value_from_tag: bool | None = None,
        click_lookback_days: int | None = None,
        view_lookback_days: int | None = None,
        call_length_seconds: int | None = None,
        attribution: Literal["data_driven", "last_click"] | None = None,
    ) -> dict[str, Any]:
        """Preview fixing a conversion action's settings: name, status, category, primary/secondary, counting (one
        or every), value, currency, lookback windows, minimum call length or attribution. Can be undone."""
        a = dict(name=name, status=status, category=category, counting_type=counting_type, primary=primary, value=value,
                 currency=currency, use_value_from_tag=use_value_from_tag, click_lookback_days=click_lookback_days,
                 view_lookback_days=view_lookback_days, call_length_seconds=call_length_seconds, attribution=attribution)
        return await w.prepared("update_conversion_action", customer_id,
                                lambda g, acct: cv.update(g, acct, conversion_action_id, a))

    @mcp.tool(annotations=PREVIEW)
    async def remove_conversion_action(
        customer_id: CID,
        conversion_action_ids: list[str],
        confirm_names: Annotated[list[str] | None, Field(
            description="Their exact names, as the person typed them to confirm.")] = None,
    ) -> dict[str, Any]:
        """Preview permanently removing conversion actions. Can't be undone; status HIDDEN
        (update_conversion_action) is the softer option. Needs the exact names and extra confirmation."""
        return await w.prepared("remove_conversion_action", customer_id,
                                lambda g, acct: cv.remove(g, acct, conversion_action_ids, confirm_names))

    return cv

