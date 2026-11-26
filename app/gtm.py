"""Phase 4: Google Tag Manager (tags, triggers, variables, publishing) and one-step conversion tracking.

Tag Manager has no "check only" mode like Google Ads' validate_only, so a
preview here means: the connector reads the container, checks the change makes
sense (names, IDs, references), and seals the exact API calls into a confirm
token. apply_change then makes those calls, in order. Changes go into a GTM
workspace and don't touch the website until the workspace is published
(gtm_publish, or publish=true on setup_conversion_tracking), which always needs
extra confirmation.

Undo: created items are deleted, changed items get their old settings back,
deleted items are re-created, and a publish is reversed by republishing the
version that was live before.

Each step is a small dict, sealed as JSON:
  m: HTTP method, p: path under tagmanager/v2/, b: JSON body, q: query params,
  d: what it does (shown to the person), k: kind (tag, trigger, variable,
  built_in, version, publish, raw), s/f: save field f of the response as ${s}
  for later steps. "${name}" anywhere in p or b is filled in when it runs
  (e.g. ${conversion_label} of a conversion action made in the same change).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Annotated, Any, Literal

from google.auth.transport.requests import AuthorizedSession
from pydantic import Field

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .ads import AdsError, AdsGateway
from .conversions import Conversions
from .writes import CID, PREVIEW, Change, PartialChange, Writer

API = "https://tagmanager.googleapis.com/tagmanager/v2/"
CACHE_SECONDS = 600
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

ALL_PAGES = "2147479553"  # GTM's built-in "All Pages" trigger
INIT_ALL_PAGES = "2147479573"  # built-in "Initialization - All Pages" trigger
BUILT_IN_TRIGGERS = {ALL_PAGES: "All Pages", INIT_ALL_PAGES: "Initialization - All Pages",
                     "2147479572": "Consent Initialization - All Pages"}

TAG_TYPES = {
    "google_ads_conversion": "awct",   # Google Ads Conversion Tracking
    "conversion_linker": "gclidw",     # Conversion Linker
    "google_tag": "googtag",           # Google tag (AW-... or G-...)
    "google_ads_remarketing": "sp",    # Google Ads Remarketing
    "custom_html": "html",             # Custom HTML
}
TRIGGER_TYPES = {
    "page_view": "pageview", "dom_ready": "domReady", "window_loaded": "windowLoaded",
    "custom_event": "customEvent", "all_clicks": "click", "link_click": "linkClick",
    "form_submission": "formSubmission", "history_change": "historyChange",
}
VARIABLE_TYPES = {
    "data_layer": "v", "constant": "c", "url": "u", "first_party_cookie": "k", "javascript_variable": "j",
    "custom_javascript": "jsm", "dom_element": "d",
}
OPERATORS = {
    "equals": "equals", "contains": "contains", "starts_with": "startsWith", "ends_with": "endsWith",
    "matches_regex": "matchRegex", "matches_css_selector": "cssSelector", "less_than": "less", "greater_than": "greater",
}
# Built-in variables that are off in a new container, by their name in conditions.
BUILT_IN_VARS = {
    "Click URL": "clickUrl", "Click Text": "clickText", "Click ID": "clickId", "Click Classes": "clickClasses",
    "Click Element": "clickElement", "Click Target": "clickTarget", "Form ID": "formId", "Form Classes": "formClasses",
    "Form URL": "formUrl", "Form Text": "formText", "Form Element": "formElement", "Form Target": "formTarget",
    "Page URL": "pageUrl", "Page Path": "pagePath", "Page Hostname": "pageHostname", "Referrer": "referrer",
    "Event": "event", "Scroll Depth Threshold": "scrollDepthThreshold", "Video Title": "videoTitle",
}
_ID_KEYS = {"path", "accountId", "containerId", "workspaceId", "tagId", "triggerId", "variableId", "fingerprint",
            "tagManagerUrl"}
_PATH_RE = re.compile(r"^accounts/\d+(/[A-Za-z0-9_:\-]+)*$")
_VAR_RE = re.compile(r"\{\{([^}]+)\}\}")
# Only these are filled in, so code in custom HTML/JavaScript (e.g. `${x}`) is left alone.
_PLACEHOLDER = re.compile(r"\$\{(conversion_id|conversion_label|trigger_id|version)\}")


# --------------------------------------------------------------------- helpers


def param(key: str, value: Any) -> dict[str, Any]:
    """One GTM parameter. Strings are templates, booleans and numbers get their own types,
    a dict that already has a 'type' is used as is (lists, maps)."""
    if isinstance(value, dict) and "type" in value:
        return {**value, "key": key}
    if isinstance(value, bool):
        return {"type": "boolean", "key": key, "value": "true" if value else "false"}
    if isinstance(value, int):
        return {"type": "integer", "key": key, "value": str(value)}
    return {"type": "template", "key": key, "value": "" if value is None else str(value)}


def merge_params(current: list[dict[str, Any]] | None, updates: dict[str, Any]) -> list[dict[str, Any]]:
    out = [dict(p) for p in current or []]
    for key, value in updates.items():
        out = [p for p in out if p.get("key") != key]
        if value is not None:
            out.append(param(key, value))
    return out


def get_param(obj: dict[str, Any], key: str) -> str | None:
    for p in obj.get("parameter") or []:
        if p.get("key") == key:
            return p.get("value")
    return None


def var_ref(name: str) -> str:
    name = str(name).strip()
    return name if name.startswith("{{") else "{{" + name + "}}"


def condition(c: dict[str, Any]) -> dict[str, Any]:
    """{variable: 'Page URL', operator: 'contains', value: '/thank-you'} -> a GTM condition.
    Operators: equals, contains, starts_with, ends_with, matches_regex, matches_css_selector, less_than,
    greater_than, each also as not_<operator>."""
    op = str(c.get("operator") or "contains").lower()
    negate = op.startswith("not_") or op.startswith("does_not_")
    op = re.sub(r"^(not_|does_not_)", "", op)
    op = {"contain": "contains", "equal": "equals", "start_with": "starts_with", "end_with": "ends_with",
          "match_regex": "matches_regex"}.get(op, op)
    if op not in OPERATORS:
        raise AdsError(f"Unknown operator '{c.get('operator')}'. Use: {', '.join(OPERATORS)} (or not_...).")
    if not c.get("variable") or c.get("value") is None:
        raise AdsError("Each condition needs a variable (e.g. 'Page URL') and a value.")
    params = [param("arg0", var_ref(c["variable"])), param("arg1", str(c["value"]))]
    if negate:
        params.append(param("negate", True))
    if c.get("ignore_case") and OPERATORS[op] == "matchRegex":
        params.append(param("ignore_case", True))
    return {"type": OPERATORS[op], "parameter": params}


def describe_condition(c: dict[str, Any]) -> str:
    vals = {p.get("key"): p.get("value") for p in c.get("parameter") or []}
    neg = "not " if vals.get("negate") == "true" else ""
    return f"{vals.get('arg0')} {neg}{c.get('type')} '{vals.get('arg1')}'"


def vars_used(obj: Any) -> set[str]:
    return set(_VAR_RE.findall(json.dumps(obj)))


def fill(value: Any, ctx: dict[str, Any]) -> Any:
    """Fill ${name} placeholders from ctx (results of earlier steps)."""
    if isinstance(value, str):
        def sub(m):
            if m.group(1) not in ctx:
                raise AdsError(f"Internal: no value for ${{{m.group(1)}}}.")
            return str(ctx[m.group(1)])
        return _PLACEHOLDER.sub(sub, value)
    if isinstance(value, list):
        return [fill(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, ctx) for k, v in value.items()}
    return value


def _dig(obj: dict[str, Any], dotted: str) -> Any:
    for part in dotted.split("."):
        obj = (obj or {}).get(part)
    return obj


def clean_for_create(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in obj.items() if k not in _ID_KEYS}


def clean_for_put(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in obj.items() if k not in ("fingerprint", "tagManagerUrl")}


def parent_path(path: str) -> str:
    return path.rsplit("/", 1)[0]


def _summary_tag(t: dict[str, Any]) -> dict[str, Any]:
    out = {"id": t.get("tagId"), "name": t.get("name"), "type": t.get("type"),
           "firing_triggers": t.get("firingTriggerId") or [], "paused": bool(t.get("paused"))}
    if t.get("blockingTriggerId"):
        out["blocking_triggers"] = t["blockingTriggerId"]
    keep = {p.get("key"): p.get("value") for p in t.get("parameter") or []
            if p.get("key") in ("conversionId", "conversionLabel", "conversionValue", "currencyCode", "orderId", "tagId")}
    if keep:
        out["settings"] = keep
    return out


def _summary_trigger(t: dict[str, Any]) -> dict[str, Any]:
    conds = [describe_condition(c) for c in (t.get("customEventFilter") or []) + (t.get("filter") or [])
             + (t.get("autoEventFilter") or [])]
    return {"id": t.get("triggerId"), "name": t.get("name"), "type": t.get("type"), "conditions": conds}


def _summary_variable(v: dict[str, Any]) -> dict[str, Any]:
    out = {"id": v.get("variableId"), "name": v.get("name"), "type": v.get("type")}
    keep = {p.get("key"): p.get("value") for p in v.get("parameter") or [] if p.get("key") in ("name", "value", "component")}
    if keep:
        out["settings"] = keep
    return out


# --------------------------------------------------------------------- API gateway


class GtmGateway:
    """Calls the Tag Manager API with the signed-in person's own Google login."""

    def __init__(self, ads: AdsGateway):
        self.ads = ads
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    def call(self, grt: str, method: str, path: str, params: dict[str, Any] | None = None,
             body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not _PATH_RE.match(path) and path != "accounts":
            raise AdsError(f"'{path}' isn't a Tag Manager path (they start with accounts/...).")
        session = AuthorizedSession(self.ads._credentials(grt))
        for attempt in range(4):
            r = session.request(method, API + path, params=params, json=body, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 * 2 ** attempt)  # Tag Manager allows only a few calls per second
                continue
            break
        if r.status_code >= 400:
            raise AdsError(self._explain(r))
        return r.json() if r.content else {}

    @staticmethod
    def _explain(r) -> str:
        try:
            err = r.json().get("error", {})
            msg = err.get("message") or r.text[:300]
        except ValueError:
            msg = r.text[:300]
        low = msg.lower()
        if r.status_code in (401, 403) and ("scope" in low or "insufficient" in low):
            return ("Your sign-in doesn't include Tag Manager yet. In Claude, go to Settings > Connectors > Zyppia "
                    "Google Ads, click Disconnect, then Connect again and tick the Tag Manager boxes.")
        if r.status_code == 403 and ("has not been used" in low or "disabled" in low):
            return ("The Tag Manager API is turned off in the Google Cloud project. Turn it on at "
                    "https://console.cloud.google.com/apis/library/tagmanager.googleapis.com (Enable), then try again.")
        if r.status_code == 403:
            return f"Tag Manager refused: {msg} (your Google login may need Edit or Publish access to this container)."
        if r.status_code == 404:
            return f"Tag Manager couldn't find that: {msg}"
        return f"Tag Manager API error ({r.status_code}): {msg}"

    def list_all(self, grt: str, path: str, key: str) -> list[dict[str, Any]]:
        out, token = [], None
        while True:
            resp = self.call(grt, "GET", path, params={"pageToken": token} if token else None)
            out += resp.get(key) or []
            token = resp.get("nextPageToken")
            if not token:
                return out

    def containers(self, grt: str, refresh: bool = False) -> list[dict[str, Any]]:
        key = hashlib.sha256(grt.encode()).hexdigest()
        with self._lock:
            hit = self._cache.get(key)
        if hit and not refresh and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        out = []
        for acc in self.list_all(grt, "accounts", "account"):
            for c in self.list_all(grt, f"{acc['path']}/containers", "container"):
                out.append({"account": acc.get("name"), "account_id": acc.get("accountId"), "name": c.get("name"),
                            "public_id": c.get("publicId"), "container_id": c.get("containerId"), "path": c["path"],
                            "type": ", ".join(c.get("usageContext") or []), "domains": c.get("domainName") or []})
        with self._lock:
            self._cache[key] = (time.time(), out)
        return out

    def container(self, grt: str, ref: str) -> dict[str, Any]:
        ref = str(ref or "").strip()
        if not ref:
            raise AdsError("Give the Tag Manager container: its ID like GTM-ABC123 (gtm_list_containers shows them).")

        def match(c):
            return ref.upper() == (c["public_id"] or "").upper() or ref == c["path"] or ref == c["container_id"] \
                or ref.lower() == (c["name"] or "").lower()

        for refresh in (False, True):
            found = [c for c in self.containers(grt, refresh) if match(c)]
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise AdsError(f"More than one container matches '{ref}': "
                               + ", ".join(f"{c['public_id']} ({c['account']})" for c in found) + ". Use its GTM-... ID.")
        raise AdsError(f"Your Google login can't see Tag Manager container '{ref}'. Check the GTM-... ID, or ask for "
                       "your login to be added to the container (Admin > User Management) with Publish access.")

    def workspace(self, grt: str, container: dict[str, Any], ref: str | None) -> dict[str, Any]:
        spaces = self.list_all(grt, f"{container['path']}/workspaces", "workspace")
        if ref:
            found = [w for w in spaces if str(ref) in (w.get("workspaceId"), w.get("path"))
                     or str(ref).lower() == (w.get("name") or "").lower()]
            if not found:
                raise AdsError(f"No workspace '{ref}' in {container['public_id']}. It has: "
                               + ", ".join(f"{w.get('name')} ({w.get('workspaceId')})" for w in spaces))
            return found[0]
        if not spaces:
            raise AdsError(f"Container {container['public_id']} has no workspace. Make one with gtm_advanced_change.")
        default = [w for w in spaces if (w.get("name") or "").lower() == "default workspace"]
        return (default or spaces)[0]

    # ---- running a sealed change

    def execute(self, grt: str, steps: list[dict[str, Any]], ctx: dict[str, Any]) -> tuple[list[str], list[dict], set[str]]:
        results: list[str] = []
        undo: list[dict[str, Any]] = []
        notes: set[str] = set()
        for i, raw_step in enumerate(steps):
            try:
                st = fill(raw_step, ctx)
                kind, method, path = st.get("k"), st["m"], st["p"]
                before = None
                if method in ("PUT", "DELETE") and kind != "built_in":
                    before = self.call(grt, "GET", path)
                live = None
                if kind == "publish":
                    try:
                        live = self.call(grt, "GET", st["c"] + "/versions:live")
                    except AdsError:
                        live = None
                resp = self.call(grt, method, path, params=st.get("q"), body=st.get("b"))
                if kind == "version" and resp.get("compilerError"):
                    raise AdsError("Tag Manager found errors in the workspace and didn't save a version. "
                                   "Open the workspace in Tag Manager to see them.")
            except AdsError as e:
                raise PartialChange(f"Tag Manager step {i + 1} ({raw_step.get('d')}) failed: {e}",
                                    results, list(reversed(undo)), notes) from None
            if st.get("s"):
                ctx[st["s"]] = _dig(resp, st["f"])
            results.append("Tag Manager: " + st["d"] + (f" ({resp['path']})" if resp.get("path") else ""))
            # how to reverse it
            if kind == "version":
                undo = []  # the workspace became a version, its items can no longer be edited there
                notes.add("Tag Manager workspace edits once saved as a version (undo republishes the old version instead)")
            elif kind == "publish":
                if live and live.get("path"):
                    undo.append({"m": "POST", "p": live["path"] + ":publish", "k": "publish", "c": st["c"],
                                 "d": f"republish the version that was live before ({live.get('name') or live.get('containerVersionId')})"})
                else:
                    notes.add("publishing (no earlier live version to go back to)")
            elif kind == "built_in":
                back = "DELETE" if method == "POST" else "POST"
                undo.append({**st, "m": back, "d": ("turn off" if back == "DELETE" else "turn back on")
                             + " built-in variables " + ", ".join(st["q"]["type"])})
            elif method == "POST" and resp.get("path") and ":" not in path.rsplit("/", 1)[-1]:
                undo.append({"m": "DELETE", "p": resp["path"], "k": kind, "d": f"delete {resp.get('name') or resp['path']}"})
            elif method == "PUT" and before:
                undo.append({"m": "PUT", "p": path, "b": clean_for_put(before), "k": kind,
                             "d": f"put back the old settings of {before.get('name') or path}"})
            elif method == "DELETE" and before:
                undo.append({"m": "POST", "p": parent_path(path), "b": clean_for_create(before), "k": kind,
                             "d": f"re-create {before.get('name') or path} (it gets a new ID)"})
            else:
                notes.add(f"Tag Manager step '{st['d']}'")
        return results, list(reversed(undo)), notes


# --------------------------------------------------------------------- building changes


class GtmTools:
    def __init__(self, w: Writer, gtm: GtmGateway, conv: Conversions):
        self.w, self.gtm, self.conv = w, gtm, conv

    def target(self, grt: str, container: str, workspace: str | None) -> tuple[dict, dict, str]:
        c = self.gtm.container(grt, container)
        ws = self.gtm.workspace(grt, c, workspace)
        return c, ws, f"Tag Manager {c['public_id']} '{c['name']}', workspace '{ws.get('name')}'"

    def contents(self, grt: str, container: str, workspace: str | None) -> dict[str, Any]:
        c, ws, where = self.target(grt, container, workspace)
        p = ws["path"]
        tags = self.gtm.list_all(grt, f"{p}/tags", "tag")
        triggers = self.gtm.list_all(grt, f"{p}/triggers", "trigger")
        variables = self.gtm.list_all(grt, f"{p}/variables", "variable")
        built_ins = self.gtm.list_all(grt, f"{p}/built_in_variables", "builtInVariable")
        status = self.gtm.call(grt, "GET", f"{p}/status")
        try:
            live = self.gtm.call(grt, "GET", f"{c['path']}/versions:live")
            live_info = {"version_id": live.get("containerVersionId"), "name": live.get("name"),
                         "tags": len(live.get("tag") or [])}
        except AdsError:
            live_info = None
        pending = []
        for ch in status.get("workspaceChange") or []:
            ent = next(((k, v) for k, v in ch.items() if k != "changeStatus"), (None, {}))
            pending.append(f"{ch.get('changeStatus')} {ent[0]} '{(ent[1] or {}).get('name')}'")
        spaces = self.gtm.list_all(grt, f"{c['path']}/workspaces", "workspace")
        problems = []
        types = {t.get("type") for t in tags}
        if "awct" in types and "gclidw" not in types:
            problems.append("There are Google Ads conversion tags but no Conversion Linker tag, so some conversions "
                            "(Safari, iOS) are missed. Add one with gtm_tag kind conversion_linker on All Pages.")
        for t in tags:
            if not t.get("firingTriggerId") and not t.get("paused"):
                problems.append(f"Tag '{t.get('name')}' has no firing trigger, so it never runs.")
            if t.get("type") == "awct" and not (get_param(t, "conversionLabel") and get_param(t, "conversionId")):
                problems.append(f"Tag '{t.get('name')}' is missing its conversion ID or label.")
        if status.get("mergeConflict"):
            problems.append("The workspace has merge conflicts; fix them in Tag Manager before publishing.")
        return {
            "container": {k: c[k] for k in ("public_id", "name", "account", "path", "type", "domains")},
            "workspace": {"id": ws.get("workspaceId"), "name": ws.get("name"), "path": p},
            "other_workspaces": [f"{w_.get('name')} ({w_.get('workspaceId')})" for w_ in spaces if w_["path"] != p],
            "live_version": live_info,
            "unpublished_changes": pending,
            "tags": [_summary_tag(t) for t in tags],
            "triggers": [_summary_trigger(t) for t in triggers]
            + [{"id": k, "name": v, "type": "built-in"} for k, v in BUILT_IN_TRIGGERS.items()],
            "variables": [_summary_variable(v) for v in variables],
            "built_in_variables_on": sorted(b.get("name") for b in built_ins),
            "problems": problems,
            "where": where,
        }

    # ---- shared pieces

    def _built_in_step(self, grt: str, ws: dict, objs: list[Any]) -> dict[str, Any] | None:
        """Turn on the built-in variables (Click URL, Form ID, ...) the new items refer to, if they're off."""
        wanted = {BUILT_IN_VARS[n] for o in objs for n in vars_used(o) if n in BUILT_IN_VARS}
        if not wanted:
            return None
        on = {b.get("type") for b in self.gtm.list_all(grt, f"{ws['path']}/built_in_variables", "builtInVariable")}
        missing = sorted(wanted - on)
        if not missing:
            return None
        return {"m": "POST", "p": f"{ws['path']}/built_in_variables", "q": {"type": missing}, "k": "built_in",
                "d": "turn on built-in variables " + ", ".join(missing)}

    def _check_vars(self, grt: str, ws: dict, objs: list[Any], ch: Change, extra: set[str] = frozenset()) -> None:
        names = {v.get("name") for v in self.gtm.list_all(grt, f"{ws['path']}/variables", "variable")} | set(extra)
        unknown = sorted(n for o in objs for n in vars_used(o) if n not in names and n not in BUILT_IN_VARS
                         and not n.startswith("_"))
        if unknown:
            ch.warnings.append("These variables aren't in the workspace yet: " + ", ".join(unknown)
                               + ". Create them with gtm_variable first, or the tag/trigger won't work.")

    def _check_triggers(self, grt: str, ws: dict, ids: list[str]) -> dict[str, str]:
        found = {t["triggerId"]: t.get("name") for t in self.gtm.list_all(grt, f"{ws['path']}/triggers", "trigger")}
        found.update(BUILT_IN_TRIGGERS)
        missing = [i for i in ids if str(i) not in found and not str(i).startswith("${")]
        if missing:
            raise AdsError(f"No trigger with ID {', '.join(map(str, missing))} in this workspace. "
                           "gtm_workspace lists the trigger IDs.")
        return found

    @staticmethod
    def _unique(items: list[dict], name: str, what: str, skip_id: str | None = None, id_key: str = "") -> None:
        for it in items:
            if (it.get("name") or "").lower() == name.lower() and it.get(id_key) != skip_id:
                raise AdsError(f"There's already a {what} called '{name}' in this workspace. Pick another name "
                               "or update that one.")

    def tag_body(self, kind: str, a: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
        body = dict(base or {})
        if not base:
            if kind == "custom":
                if not a.get("type"):
                    raise AdsError("A custom tag needs its GTM type (e.g. 'awcc', 'gaawe', or a template ID 'cvt_...').")
                body["type"] = a["type"]
            else:
                body["type"] = TAG_TYPES[kind]
            body["parameter"] = []
        t = body["type"]
        if a.get("name"):
            body["name"] = " ".join(a["name"].split())
        p: dict[str, Any] = {}
        if t in ("awct", "sp"):
            if a.get("conversion_id"):
                p["conversionId"] = re.sub(r"^AW-", "", str(a["conversion_id"]).strip(), flags=re.I)
            if a.get("conversion_label"):
                p["conversionLabel"] = str(a["conversion_label"]).strip()
        if t == "awct":
            if a.get("value") is not None:
                p["conversionValue"] = str(a["value"])
            if a.get("currency"):
                p["currencyCode"] = str(a["currency"]).upper() if not str(a["currency"]).startswith("{{") else a["currency"]
            if a.get("transaction_id"):
                p["orderId"] = str(a["transaction_id"])
        if t == "googtag" and a.get("tag_id"):
            p["tagId"] = str(a["tag_id"]).strip()
        if t == "html" and a.get("html") is not None:
            p["html"] = a["html"]
            p.setdefault("supportDocumentWrite", False)
        p.update(a.get("parameters") or {})
        body["parameter"] = merge_params(body.get("parameter"), p)
        if a.get("firing_trigger_ids") is not None:
            body["firingTriggerId"] = [str(x) for x in a["firing_trigger_ids"]]
        if a.get("blocking_trigger_ids") is not None:
            body["blockingTriggerId"] = [str(x) for x in a["blocking_trigger_ids"]]
        if a.get("paused") is not None:
            body["paused"] = bool(a["paused"])
        if not base:
            body.setdefault("tagFiringOption", "oncePerEvent")
        if t == "awct" and not (get_param(body, "conversionId") and get_param(body, "conversionLabel")):
            raise AdsError("A Google Ads conversion tag needs conversion_id and conversion_label "
                           "(list_conversion_actions shows them), or use setup_conversion_tracking.")
        if t == "googtag" and not get_param(body, "tagId"):
            raise AdsError("A Google tag needs tag_id, e.g. AW-123456789 or G-ABC123.")
        if t == "html" and not get_param(body, "html"):
            raise AdsError("A custom HTML tag needs html.")
        return body

    def trigger_body(self, kind: str, a: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
        body = dict(base or {})
        if not base:
            if kind == "custom":
                if not a.get("type"):
                    raise AdsError("A custom trigger needs its GTM type (e.g. 'elementVisibility', 'scrollDepth', 'timer').")
                body["type"] = a["type"]
            else:
                body["type"] = TRIGGER_TYPES[kind]
        if a.get("name"):
            body["name"] = " ".join(a["name"].split())
        if body["type"] == "customEvent" and (a.get("event_name") or not base):
            if not a.get("event_name"):
                raise AdsError("A custom event trigger needs event_name (the dataLayer event, e.g. 'generate_lead').")
            body["customEventFilter"] = [condition({"variable": "_event", "operator": "equals", "value": a["event_name"]})]
        if a.get("conditions") is not None:
            body["filter"] = [condition(c) for c in a["conditions"]]
        if body["type"] in ("linkClick", "formSubmission") and not base:
            body.setdefault("waitForTags", param("waitForTags", False))
            body.setdefault("checkValidation", param("checkValidation", body["type"] == "formSubmission"))
        for k, v in (a.get("fields") or {}).items():  # any other trigger field, in GTM's own format
            body[k] = v
        return body

    def variable_body(self, kind: str, a: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
        body = dict(base or {})
        if not base:
            if kind == "custom":
                if not a.get("type"):
                    raise AdsError("A custom variable needs its GTM type.")
                body["type"] = a["type"]
            else:
                body["type"] = VARIABLE_TYPES[kind]
            body["parameter"] = []
        if a.get("name"):
            body["name"] = " ".join(a["name"].split())
        t, v, p = body["type"], a.get("value"), {}
        if v is not None:
            if t == "v":
                p.update({"name": v, "dataLayerVersion": 2, "setDefaultValue": False})
            elif t == "c":
                p["value"] = v
            elif t == "u":
                p["component"] = str(v).upper()
            elif t in ("k", "j"):
                p["name"] = v
            elif t == "jsm":
                p["javascript"] = v
            elif t == "d":
                p.update({"selectorType": "CSS", "elementSelector": v})
            else:
                raise AdsError("For a custom variable, give its settings in parameters instead of value.")
        if t == "d" and a.get("attribute"):
            p["attributeName"] = a["attribute"]
        if t == "u" and a.get("query_key"):
            p["queryKey"] = a["query_key"]
            p["component"] = "QUERY"
        p.update(a.get("parameters") or {})
        body["parameter"] = merge_params(body.get("parameter"), p)
        if not base and t != "c" and t != "jsm" and not body["parameter"]:
            raise AdsError("Give the variable's value (e.g. the dataLayer key, cookie name, CSS selector, or URL part).")
        return body

    # ---- tools

    def tag(self, grt: str, container: str, workspace: str | None, action: str, a: dict[str, Any]) -> Change:
        c, ws, where = self.target(grt, container, workspace)
        tags = self.gtm.list_all(grt, f"{ws['path']}/tags", "tag")
        ch = Change(None, "", where=where)
        if action == "create":
            kind = a.get("kind") or "google_ads_conversion"
            if kind not in TAG_TYPES and kind != "custom":
                raise AdsError(f"Unknown tag kind '{kind}'. Use: {', '.join(TAG_TYPES)}, custom.")
            if not a.get("name"):
                raise AdsError("The tag needs a name.")
            if kind == "conversion_linker" and a.get("firing_trigger_ids") is None:
                a["firing_trigger_ids"] = [ALL_PAGES]
            if kind == "google_tag" and a.get("firing_trigger_ids") is None:
                a["firing_trigger_ids"] = [INIT_ALL_PAGES]
            body = self.tag_body(kind, a)
            self._unique(tags, body["name"], "tag")
            if not body.get("firingTriggerId"):
                raise AdsError("Give firing_trigger_ids (gtm_workspace lists triggers; All Pages is 2147479553), "
                               "or the tag never runs.")
            trig = self._check_triggers(grt, ws, body["firingTriggerId"] + body.get("blockingTriggerId", []))
            ch.title = f"Create tag '{body['name']}' in Tag Manager"
            ch.lines.append(f"tag '{body['name']}' ({body['type']}), fires on: "
                            + ", ".join(f"'{trig.get(t)}'" for t in body["firingTriggerId"]))
            ch.lines += [f"  {p['key']} = {p.get('value')}" for p in body["parameter"] if "value" in p]
            step = self._built_in_step(grt, ws, [body])
            if step:
                ch.gtm.append(step)
            ch.gtm.append({"m": "POST", "p": f"{ws['path']}/tags", "b": body, "k": "tag", "d": f"create tag '{body['name']}'"})
            if body["type"] == "html":
                ch.large.append("a custom HTML tag runs its own code on every page it fires on; check the code is safe")
            self._check_vars(grt, ws, [body], ch)
        else:
            if not a.get("id"):
                raise AdsError("Give the tag's id (gtm_workspace lists them).")
            cur = next((t for t in tags if t.get("tagId") == str(a["id"])), None)
            if not cur:
                raise AdsError(f"No tag with ID {a['id']} in this workspace.")
            if action == "delete":
                ch.title = f"Delete tag '{cur.get('name')}' in Tag Manager"
                ch.lines.append(f"delete tag '{cur.get('name')}' ({cur.get('type')})")
                ch.gtm.append({"m": "DELETE", "p": cur["path"], "k": "tag", "d": f"delete tag '{cur.get('name')}'"})
                ch.large.append("deleting a tag stops what it tracks once the workspace is published")
            else:
                body = self.tag_body(cur.get("type"), a, base=cur)
                if a.get("name"):
                    self._unique(tags, body["name"], "tag", skip_id=cur["tagId"], id_key="tagId")
                self._check_triggers(grt, ws, (body.get("firingTriggerId") or []) + (body.get("blockingTriggerId") or []))
                ch.title = f"Change tag '{cur.get('name')}' in Tag Manager"
                ch.lines += self._diff(cur, body)
                if body["type"] == "html" and get_param(cur, "html") != get_param(body, "html"):
                    ch.large.append("this changes the code a custom HTML tag runs on the website")
                step = self._built_in_step(grt, ws, [body])
                if step:
                    ch.gtm.append(step)
                ch.gtm.append({"m": "PUT", "p": cur["path"], "b": clean_for_put(body) | {"fingerprint": cur.get("fingerprint")},
                               "k": "tag", "d": f"update tag '{body.get('name')}'"})
                self._check_vars(grt, ws, [body], ch)
        ch.warnings.append("This changes the workspace only. The website changes when the workspace is published "
                           "(gtm_publish).")
        return ch

    def trigger(self, grt: str, container: str, workspace: str | None, action: str, a: dict[str, Any]) -> Change:
        c, ws, where = self.target(grt, container, workspace)
        triggers = self.gtm.list_all(grt, f"{ws['path']}/triggers", "trigger")
        ch = Change(None, "", where=where)
        if action == "create":
            kind = a.get("kind") or "page_view"
            if kind not in TRIGGER_TYPES and kind != "custom":
                raise AdsError(f"Unknown trigger kind '{kind}'. Use: {', '.join(TRIGGER_TYPES)}, custom.")
            if not a.get("name"):
                raise AdsError("The trigger needs a name.")
            body = self.trigger_body(kind, a)
            self._unique(triggers, body["name"], "trigger")
            ch.title = f"Create trigger '{body['name']}' in Tag Manager"
            ch.lines.append(f"trigger '{body['name']}' ({body['type']})" + self._cond_text(body))
            step = self._built_in_step(grt, ws, [body])
            if step:
                ch.gtm.append(step)
            ch.gtm.append({"m": "POST", "p": f"{ws['path']}/triggers", "b": body, "k": "trigger",
                           "d": f"create trigger '{body['name']}'"})
            self._check_vars(grt, ws, [body], ch)
            if body["type"] in ("pageview", "domReady", "windowLoaded", "click", "linkClick") and not body.get("filter"):
                ch.warnings.append("No conditions, so this fires on every page or click.")
        else:
            if not a.get("id"):
                raise AdsError("Give the trigger's id (gtm_workspace lists them).")
            cur = next((t for t in triggers if t.get("triggerId") == str(a["id"])), None)
            if not cur:
                raise AdsError(f"No trigger with ID {a['id']} in this workspace (built-in triggers can't be changed).")
            if action == "delete":
                users = [t.get("name") for t in self.gtm.list_all(grt, f"{ws['path']}/tags", "tag")
                         if cur["triggerId"] in (t.get("firingTriggerId") or []) + (t.get("blockingTriggerId") or [])]
                if users:
                    raise AdsError(f"Trigger '{cur.get('name')}' is used by tag(s) {', '.join(users)}. Change or delete "
                                   "those tags first.")
                ch.title = f"Delete trigger '{cur.get('name')}' in Tag Manager"
                ch.lines.append(f"delete trigger '{cur.get('name')}' ({cur.get('type')})")
                ch.gtm.append({"m": "DELETE", "p": cur["path"], "k": "trigger", "d": f"delete trigger '{cur.get('name')}'"})
                ch.large.append("deleting a trigger can't be fully undone (a re-created one gets a new ID)")
            else:
                body = self.trigger_body(cur.get("type"), a, base=cur)
                if a.get("name"):
                    self._unique(triggers, body["name"], "trigger", skip_id=cur["triggerId"], id_key="triggerId")
                ch.title = f"Change trigger '{cur.get('name')}' in Tag Manager"
                ch.lines += self._diff(cur, body)
                step = self._built_in_step(grt, ws, [body])
                if step:
                    ch.gtm.append(step)
                ch.gtm.append({"m": "PUT", "p": cur["path"], "b": clean_for_put(body) | {"fingerprint": cur.get("fingerprint")},
                               "k": "trigger", "d": f"update trigger '{body.get('name')}'"})
                self._check_vars(grt, ws, [body], ch)
        ch.warnings.append("This changes the workspace only. The website changes when the workspace is published "
                           "(gtm_publish).")
        return ch

    def variable(self, grt: str, container: str, workspace: str | None, action: str, a: dict[str, Any]) -> Change:
        c, ws, where = self.target(grt, container, workspace)
        variables = self.gtm.list_all(grt, f"{ws['path']}/variables", "variable")
        ch = Change(None, "", where=where)
        if action == "create":
            kind = a.get("kind") or "data_layer"
            if kind not in VARIABLE_TYPES and kind != "custom":
                raise AdsError(f"Unknown variable kind '{kind}'. Use: {', '.join(VARIABLE_TYPES)}, custom.")
            if not a.get("name"):
                raise AdsError("The variable needs a name.")
            body = self.variable_body(kind, a)
            self._unique(variables, body["name"], "variable")
            ch.title = f"Create variable '{body['name']}' in Tag Manager"
            ch.lines.append(f"variable '{body['name']}' ({body['type']})")
            ch.lines += [f"  {p['key']} = {p.get('value')}" for p in body["parameter"] if "value" in p]
            ch.gtm.append({"m": "POST", "p": f"{ws['path']}/variables", "b": body, "k": "variable",
                           "d": f"create variable '{body['name']}'"})
            if body["type"] == "jsm":
                ch.large.append("a custom JavaScript variable runs its own code on the website; check the code is safe")
        else:
            if not a.get("id"):
                raise AdsError("Give the variable's id (gtm_workspace lists them).")
            cur = next((v for v in variables if v.get("variableId") == str(a["id"])), None)
            if not cur:
                raise AdsError(f"No variable with ID {a['id']} in this workspace.")
            if action == "delete":
                ch.title = f"Delete variable '{cur.get('name')}' in Tag Manager"
                ch.lines.append(f"delete variable '{cur.get('name')}'")
                users = [x.get("name") for x in self.gtm.list_all(grt, f"{ws['path']}/tags", "tag")
                         + self.gtm.list_all(grt, f"{ws['path']}/triggers", "trigger")
                         if cur.get("name") in vars_used(x)]
                if users:
                    raise AdsError(f"Variable '{cur.get('name')}' is used by {', '.join(users)}. Change those first.")
                ch.gtm.append({"m": "DELETE", "p": cur["path"], "k": "variable", "d": f"delete variable '{cur.get('name')}'"})
                ch.large.append("deleting a variable can't be fully undone (a re-created one gets a new ID)")
            else:
                body = self.variable_body(cur.get("type"), a, base=cur)
                if a.get("name"):
                    self._unique(variables, body["name"], "variable", skip_id=cur["variableId"], id_key="variableId")
                    ch.warnings.append("Tags and triggers refer to variables by name; renaming breaks them unless "
                                       "they're updated too.")
                ch.title = f"Change variable '{cur.get('name')}' in Tag Manager"
                ch.lines += self._diff(cur, body)
                if body["type"] == "jsm" and get_param(cur, "javascript") != get_param(body, "javascript"):
                    ch.large.append("this changes code that runs on the website")
                ch.gtm.append({"m": "PUT", "p": cur["path"], "b": clean_for_put(body) | {"fingerprint": cur.get("fingerprint")},
                               "k": "variable", "d": f"update variable '{body.get('name')}'"})
        ch.warnings.append("This changes the workspace only. The website changes when the workspace is published "
                           "(gtm_publish).")
        return ch

    def publish(self, grt: str, container: str, workspace: str | None, version_name: str | None, notes: str | None,
                publish_live: bool) -> Change:
        c, ws, where = self.target(grt, container, workspace)
        status = self.gtm.call(grt, "GET", f"{ws['path']}/status")
        if status.get("mergeConflict"):
            raise AdsError("This workspace has merge conflicts with the live version. Resolve them in Tag Manager first.")
        changes = status.get("workspaceChange") or []
        ch = Change(None, f"{'Publish' if publish_live else 'Save a version of'} {c['public_id']} workspace "
                          f"'{ws.get('name')}'", where=where)
        for x in changes:
            ent = next(((k, v) for k, v in x.items() if k != "changeStatus"), (None, {}))
            ch.lines.append(f"{x.get('changeStatus')} {ent[0]} '{(ent[1] or {}).get('name')}'")
        if not changes:
            ch.warnings.append("The workspace has no changes compared to the live version.")
        name = version_name or f"Zyppia connector {time.strftime('%Y-%m-%d %H:%M')}"
        ch.lines.append(f"version name: '{name}'")
        ch.gtm.append({"m": "POST", "p": f"{ws['path']}:create_version", "b": {"name": name, "notes": notes or ""},
                       "k": "version", "s": "version", "f": "containerVersion.path", "d": f"save version '{name}'"})
        if publish_live:
            ch.gtm.append({"m": "POST", "p": "${version}:publish", "k": "publish", "c": c["path"],
                           "d": f"publish version '{name}' to the live website"})
            ch.large.append(f"publishing makes these Tag Manager changes live on the website ({', '.join(c['domains']) or c['public_id']})")
        ch.warnings.append("Saving a version closes this workspace; Tag Manager starts a fresh one for the next changes.")
        return ch

    def raw(self, grt: str, steps: list[dict[str, Any]], description: str) -> Change:
        if not steps or len(steps) > 50:
            raise AdsError("Give 1 to 50 Tag Manager API calls.")
        ch = Change(None, description or "Custom Tag Manager change", where="Tag Manager (custom API calls)")
        for i, s in enumerate(steps):
            m = str(s.get("method") or "").upper()
            path = str(s.get("path") or "").strip().removeprefix(API).strip("/")
            if m not in ("POST", "PUT", "DELETE"):
                raise AdsError(f"Call {i + 1}: method must be POST, PUT or DELETE (use gtm_get to read).")
            if not _PATH_RE.match(path):
                raise AdsError(f"Call {i + 1}: path must look like accounts/123/containers/456/...")
            kind = "publish" if path.endswith(":publish") else "version" if path.endswith(":create_version") else "raw"
            st = {"m": m, "p": path, "b": s.get("body"), "q": s.get("params"), "k": kind, "d": f"{m} {path}"}
            if kind == "publish":
                st["c"] = "/".join(path.split("/")[:4])
            ch.gtm.append(st)
            ch.lines.append(st["d"])
        ch.large.append("custom Tag Manager API calls aren't checked by the connector's own safety rules")
        return ch

    # ---- one step: conversion action + its Tag Manager tag

    def setup(self, grt: str, user: str, customer_id: str, a: dict[str, Any]) -> Change:
        acct = self.w.ads.account(user, grt, customer_id)
        c, ws, where = self.target(grt, a["container"], a.get("workspace"))
        p = ws["path"]
        tags = self.gtm.list_all(grt, f"{p}/tags", "tag")
        triggers = self.gtm.list_all(grt, f"{p}/triggers", "trigger")
        tracking = self.conv.tracking_setting(grt, acct)
        if a.get("conversion_action_id"):
            ids = self.conv.tag_ids(grt, acct, a["conversion_action_id"])
            name = ids["name"]
            ch = Change(acct, f"Track '{name}' with Tag Manager", where=where)
            conv_id, label = ids["conversion_id"], ids["conversion_label"]
            ch.lines.append(f"Google Ads: use the existing conversion action '{name}' (AW-{conv_id}/{label})")
        else:
            name = " ".join(str(a.get("conversion_name") or "").split())
            if not name:
                raise AdsError("Give conversion_name for a new conversion action, or conversion_action_id for an existing one.")
            if tracking["managed_by"]:
                raise AdsError(f"{acct.name} uses conversion actions from manager {tracking['managed_by']} "
                               "(cross-account conversion tracking). Create the conversion there instead.")
            ch = Change(acct, f"Set up conversion tracking for '{name}' (Google Ads + Tag Manager)", where=where)
            ch.lines.append("Google Ads:")
            value = a.get("value")
            if value is None and a.get("value_variable"):
                value = 1  # Google needs a fallback value when the tag sends its own
            self.conv.create(grt, acct, {
                "name": name, "type": a.get("type") or "website", "category": a.get("category"), "value": value,
                "currency": a.get("currency"), "use_value_from_tag": bool(a.get("value_variable")),
                "counting_type": a.get("counting_type"), "primary": a.get("primary", True),
                "click_lookback_days": a.get("click_lookback_days"),
            }, ch)
            ch.warnings = [x for x in ch.warnings if "setup_conversion_tracking" not in x]
            ch.extra["label"] = True
            conv_id, label = "${conversion_id}", "${conversion_label}"
        tag_name = a.get("tag_name") or f"Google Ads - {name}"
        self._unique(tags, tag_name, "tag")
        ch.lines.append(f"Tag Manager ({c['public_id']}, workspace '{ws.get('name')}'):")

        # the trigger
        new_objs = []
        if a.get("existing_trigger_id"):
            found = self._check_triggers(grt, ws, [str(a["existing_trigger_id"])])
            fire = [str(a["existing_trigger_id"])]
            ch.lines.append(f"fire on the existing trigger '{found[fire[0]]}'")
        else:
            t = dict(a.get("trigger") or {})
            if not t:
                raise AdsError("Say when it counts: trigger {kind: page_view, conditions: [{variable: 'Page URL', "
                               "operator: contains, value: '/thank-you'}]}, or {kind: custom_event, event_name: ...}, "
                               "or existing_trigger_id.")
            kind = t.get("kind") or "page_view"
            if kind not in TRIGGER_TYPES:
                raise AdsError(f"Unknown trigger kind '{kind}'. Use: {', '.join(TRIGGER_TYPES)}.")
            t.setdefault("name", f"{name} - trigger")
            tb = self.trigger_body(kind, t)
            self._unique(triggers, tb["name"], "trigger")
            if kind in ("page_view", "all_clicks", "link_click", "dom_ready", "window_loaded") and not tb.get("filter"):
                raise AdsError("Give the trigger conditions (e.g. Page URL contains /thank-you), or the conversion would "
                               "count on every page or click.")
            new_objs.append(tb)
            fire = ["${trigger_id}"]
            trig_step = {"m": "POST", "p": f"{p}/triggers", "b": tb, "k": "trigger", "s": "trigger_id",
                         "f": "triggerId", "d": f"create trigger '{tb['name']}'"}
            ch.lines.append(f"new trigger '{tb['name']}' ({tb['type']})" + self._cond_text(tb))

        tag = self.tag_body("google_ads_conversion", {
            "name": tag_name, "conversion_id": conv_id, "conversion_label": label,
            "value": a.get("value_variable"), "currency": a.get("currency_variable"),
            "transaction_id": a.get("transaction_id_variable"), "firing_trigger_ids": fire,
        })
        new_objs.append(tag)
        step = self._built_in_step(grt, ws, new_objs)
        if step:
            ch.gtm.append(step)
            ch.lines.append(step["d"])
        if not a.get("existing_trigger_id"):
            ch.gtm.append(trig_step)
        types = {t_.get("type") for t_ in tags}
        if a.get("add_conversion_linker", True) and "gclidw" not in types:
            ch.gtm.append({"m": "POST", "p": f"{p}/tags", "k": "tag", "d": "create tag 'Conversion Linker' (All Pages)",
                           "b": {"name": "Conversion Linker", "type": "gclidw", "parameter": [],
                                 "firingTriggerId": [ALL_PAGES], "tagFiringOption": "oncePerEvent"}})
            ch.lines.append("new tag 'Conversion Linker' on All Pages (needed so clicks are matched to conversions)")
        aw = f"AW-{tracking['conversion_id'] or conv_id}"
        has_gtag = any(t_.get("type") == "googtag" and (get_param(t_, "tagId") or "").upper() == aw.upper() for t_ in tags)
        if a.get("add_google_tag", True) and not has_gtag:
            ch.gtm.append({"m": "POST", "p": f"{p}/tags", "k": "tag", "d": f"create tag 'Google tag {aw}'",
                           "b": {"name": f"Google tag {aw}", "type": "googtag", "parameter": [param("tagId", aw)],
                                 "firingTriggerId": [INIT_ALL_PAGES], "tagFiringOption": "oncePerEvent"}})
            ch.lines.append(f"new tag 'Google tag {aw}' on Initialization - All Pages")
        ch.gtm.append({"m": "POST", "p": f"{p}/tags", "b": tag, "k": "tag", "d": f"create tag '{tag_name}'"})
        line = f"new tag '{tag_name}' (Google Ads conversion, AW-{conv_id}/{label})".replace(
            "AW-${conversion_id}/${conversion_label}", "with the new conversion's ID and label")
        if a.get("value_variable"):
            line += f", value from {a['value_variable']}"
        ch.lines.append(line)
        self._check_vars(grt, ws, new_objs, ch)
        if a.get("publish"):
            pub = self.publish(grt, a["container"], ws.get("workspaceId"), a.get("version_name") or f"Add {name} tracking",
                               None, True)
            ch.gtm += pub.gtm
            ch.large += pub.large
            ch.lines.append("then publish the workspace so it goes live on the website")
            others = [x for x in pub.lines if not x.startswith("version name")]
            if others:
                ch.warnings.append("Publishing also makes these other unpublished workspace changes live: "
                                   + "; ".join(others))
        else:
            ch.warnings.append("The Tag Manager changes stay in the workspace until published. Publish with gtm_publish "
                               "after testing in Tag Manager's Preview mode.")
        ch.warnings.append("After it's live, do a test conversion. Google Ads shows the status as 'Recording "
                           "conversions' within a few hours.")
        return ch

    @staticmethod
    def _cond_text(body: dict[str, Any]) -> str:
        conds = [describe_condition(x) for x in (body.get("customEventFilter") or []) + (body.get("filter") or [])]
        return (" when " + " and ".join(conds)) if conds else ""

    @staticmethod
    def _diff(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
        out = []
        for k in sorted(set(old) | set(new)):
            if k in _ID_KEYS or k == "parameter":
                continue
            if old.get(k) != new.get(k):
                out.append(f"{k}: {json.dumps(old.get(k))} -> {json.dumps(new.get(k))}")
        op = {p.get("key"): p.get("value", p.get("list") or p.get("map")) for p in old.get("parameter") or []}
        np_ = {p.get("key"): p.get("value", p.get("list") or p.get("map")) for p in new.get("parameter") or []}
        for k in sorted(set(op) | set(np_)):
            if op.get(k) != np_.get(k):
                out.append(f"{k}: {json.dumps(op.get(k))} -> {json.dumps(np_.get(k))}")
        return out or ["no settings change"]


# --------------------------------------------------------------------- tools

Container = Annotated[str, Field(description="Tag Manager container: its ID like GTM-ABC123 (from gtm_list_containers).")]
Workspace = Annotated[str | None, Field(description="Workspace ID or name. Default: 'Default Workspace'.")]
Action = Literal["create", "update", "delete"]
Conditions = Annotated[list[dict[str, Any]] | None, Field(description=(
    "All must match: [{variable: 'Page URL'|'Page Path'|'Click URL'|'Click Text'|'Form ID'|any variable name, "
    "operator: equals|contains|starts_with|ends_with|matches_regex|matches_css_selector (or not_...), value}]."))]


def register_gtm_tools(mcp: FastMCP, w: Writer, conv: Conversions) -> GtmTools:
    gtm = GtmGateway(w.ads)
    w.gtm = gtm
    g = GtmTools(w, gtm, conv)

    def gtm_change(tool: str, build):
        def go(user, grt):
            return w.preview(user, grt, build(grt, user))
        return w.run(tool, None, go)

    @mcp.tool(annotations=READ_ONLY)
    async def gtm_list_containers(
        refresh: Annotated[bool, Field(description="Re-read from Google instead of the 10-minute cache.")] = False,
    ) -> dict[str, Any]:
        """List every Google Tag Manager account and container your Google login can access, with the GTM-... IDs
        and website domains. Start here for any Tag Manager work."""
        def go(user, grt):
            items = gtm.containers(grt, refresh)
            return {"count": len(items), "containers": items}
        return await w.run("gtm_list_containers", None, go)

    @mcp.tool(annotations=READ_ONLY)
    async def gtm_workspace(container: Container, workspace: Workspace = None) -> dict[str, Any]:
        """Everything in a Tag Manager workspace: tags (with their triggers and Google Ads IDs), triggers (with
        conditions), variables, built-in variables that are on, unpublished changes, the live version, and likely
        problems (no Conversion Linker, tags that never fire, missing labels)."""
        return await w.run("gtm_workspace", None, lambda user, grt: g.contents(grt, container, workspace))

    @mcp.tool(annotations=READ_ONLY)
    async def gtm_get(
        path: Annotated[str, Field(description=(
            "Any Tag Manager API v2 path to read, e.g. accounts/1/containers/2/workspaces/3/tags/4 or "
            "accounts/1/containers/2/versions:live or accounts/1/containers/2/version_headers."))],
    ) -> dict[str, Any]:
        """Read any Tag Manager item in full (Tag Manager API v2 GET), for details gtm_workspace leaves out."""
        clean = path.strip().removeprefix(API).strip("/")
        return await w.run("gtm_get", None, lambda user, grt: gtm.call(grt, "GET", clean))

    @mcp.tool(annotations=PREVIEW)
    async def gtm_tag(
        container: Container,
        action: Action,
        workspace: Workspace = None,
        id: Annotated[str | None, Field(description="update/delete: the tag ID (from gtm_workspace).")] = None,
        kind: Annotated[Literal["google_ads_conversion", "conversion_linker", "google_tag", "google_ads_remarketing",
                                "custom_html", "custom"] | None, Field(description="create: what kind of tag.")] = None,
        name: str | None = None,
        firing_trigger_ids: Annotated[list[str] | None, Field(description=(
            "Trigger IDs that fire it. All Pages = 2147479553, Initialization - All Pages = 2147479573. "
            "Default: All Pages for the conversion linker, Initialization for the Google tag."))] = None,
        blocking_trigger_ids: list[str] | None = None,
        paused: bool | None = None,
        conversion_id: Annotated[str | None, Field(description="Google Ads conversion ID (AW-... number).")] = None,
        conversion_label: Annotated[str | None, Field(description="Google Ads conversion label.")] = None,
        value: Annotated[str | None, Field(description="Conversion value, a number or a variable like {{DLV - value}}.")] = None,
        currency: Annotated[str | None, Field(description="Currency code or variable.")] = None,
        transaction_id: Annotated[str | None, Field(description="Order ID variable, e.g. {{DLV - transaction_id}}.")] = None,
        tag_id: Annotated[str | None, Field(description="google_tag: AW-123456789 or G-ABC123.")] = None,
        html: Annotated[str | None, Field(description="custom_html: the code.")] = None,
        type: Annotated[str | None, Field(description="custom: the GTM tag type, e.g. 'awcc', 'gaawe', 'cvt_...'.")] = None,
        parameters: Annotated[dict[str, Any] | None, Field(description=(
            "Any other tag settings by GTM key: strings, true/false, numbers, or GTM's own {type, list/map} format. "
            "null removes a setting."))] = None,
    ) -> dict[str, Any]:
        """Preview creating, changing or deleting a Tag Manager tag: Google Ads conversion, Conversion Linker,
        Google tag, remarketing, custom HTML, or any other type. Goes into the workspace; publish with gtm_publish."""
        a = dict(kind=kind, name=name, id=id, firing_trigger_ids=firing_trigger_ids, blocking_trigger_ids=blocking_trigger_ids,
                 paused=paused, conversion_id=conversion_id, conversion_label=conversion_label, value=value,
                 currency=currency, transaction_id=transaction_id, tag_id=tag_id, html=html, type=type, parameters=parameters)
        return await gtm_change("gtm_tag", lambda grt, user: g.tag(grt, container, workspace, action, a))

    @mcp.tool(annotations=PREVIEW)
    async def gtm_trigger(
        container: Container,
        action: Action,
        workspace: Workspace = None,
        id: Annotated[str | None, Field(description="update/delete: the trigger ID.")] = None,
        kind: Annotated[Literal["page_view", "dom_ready", "window_loaded", "custom_event", "all_clicks", "link_click",
                                "form_submission", "history_change", "custom"] | None,
                        Field(description="create: when it fires.")] = None,
        name: str | None = None,
        conditions: Conditions = None,
        event_name: Annotated[str | None, Field(description="custom_event: the dataLayer event name.")] = None,
        type: Annotated[str | None, Field(description="custom: GTM trigger type, e.g. elementVisibility, scrollDepth, timer.")] = None,
        fields: Annotated[dict[str, Any] | None, Field(description="Any other trigger fields in GTM's own format.")] = None,
    ) -> dict[str, Any]:
        """Preview creating, changing or deleting a Tag Manager trigger (page views, thank-you pages, custom
        dataLayer events, clicks, link clicks such as tel: links, form submissions, and more)."""
        a = dict(kind=kind, name=name, id=id, conditions=conditions, event_name=event_name, type=type, fields=fields)
        return await gtm_change("gtm_trigger", lambda grt, user: g.trigger(grt, container, workspace, action, a))

    @mcp.tool(annotations=PREVIEW)
    async def gtm_variable(
        container: Container,
        action: Action,
        workspace: Workspace = None,
        id: Annotated[str | None, Field(description="update/delete: the variable ID.")] = None,
        kind: Annotated[Literal["data_layer", "constant", "url", "first_party_cookie", "javascript_variable",
                                "custom_javascript", "dom_element", "custom"] | None,
                        Field(description="create: what kind of variable.")] = None,
        name: str | None = None,
        value: Annotated[str | None, Field(description=(
            "data_layer: the key (e.g. ecommerce.value); constant: the value; url: the part (PATH, HOST, QUERY...); "
            "cookie/javascript_variable: its name; custom_javascript: the function code; dom_element: CSS selector."))] = None,
        attribute: Annotated[str | None, Field(description="dom_element: attribute to read (empty = text).")] = None,
        query_key: Annotated[str | None, Field(description="url: read this query parameter.")] = None,
        type: Annotated[str | None, Field(description="custom: the GTM variable type.")] = None,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Preview creating, changing or deleting a Tag Manager variable. Use names like 'DLV - value' and refer to
        them in tags and triggers as {{DLV - value}}."""
        a = dict(kind=kind, name=name, id=id, value=value, attribute=attribute, query_key=query_key, type=type,
                 parameters=parameters)
        return await gtm_change("gtm_variable", lambda grt, user: g.variable(grt, container, workspace, action, a))

    @mcp.tool(annotations=PREVIEW)
    async def gtm_publish(
        container: Container,
        workspace: Workspace = None,
        version_name: str | None = None,
        notes: str | None = None,
        publish_live: Annotated[bool, Field(description="False = only save a version, don't put it live.")] = True,
    ) -> dict[str, Any]:
        """Preview publishing a Tag Manager workspace: lists every unpublished change, then saves a version and
        makes it live on the website. Always needs extra confirmation. Undo republishes the version live before."""
        return await gtm_change("gtm_publish", lambda grt, user: g.publish(grt, container, workspace, version_name, notes,
                                                                           publish_live))

    @mcp.tool(annotations=PREVIEW)
    async def gtm_advanced_change(
        calls: Annotated[list[dict[str, Any]], Field(description=(
            "Tag Manager API v2 calls: [{method: POST|PUT|DELETE, path: 'accounts/1/containers/2/workspaces/3/...', "
            "body: {...}, params: {...}}]. E.g. create a workspace, folders, templates, zones, environments, "
            "user permissions, or revert a workspace (POST .../workspaces/3:sync)."))],
        description: Annotated[str, Field(description="Plain-language summary of what this does, shown to the person.")],
    ) -> dict[str, Any]:
        """Preview any other Tag Manager change with raw API calls. Use the dedicated gtm_ tools when one fits.
        Always needs extra confirmation."""
        return await gtm_change("gtm_advanced_change", lambda grt, user: g.raw(grt, calls, description))

    @mcp.tool(annotations=PREVIEW)
    async def setup_conversion_tracking(
        customer_id: CID,
        container: Container,
        conversion_name: Annotated[str | None, Field(description="Name for a NEW conversion action, e.g. 'Contact form'.")] = None,
        conversion_action_id: Annotated[str | None, Field(description="Or an EXISTING website conversion action's ID.")] = None,
        trigger: Annotated[dict[str, Any] | None, Field(description=(
            "When it counts: {kind: page_view, conditions: [{variable: 'Page URL', operator: contains, value: '/thank-you'}]}, "
            "{kind: custom_event, event_name: 'generate_lead'}, {kind: link_click, conditions: [{variable: 'Click URL', "
            "operator: starts_with, value: 'tel:'}]} or {kind: form_submission, conditions: [...]}."))] = None,
        existing_trigger_id: Annotated[str | None, Field(description="Or fire on a trigger that already exists.")] = None,
        workspace: Workspace = None,
        type: Annotated[Literal["website", "phone_number_click"], Field(description=(
            "New conversion's type: website, or phone_number_click for taps on the number."))] = "website",
        category: Annotated[str | None, Field(description="e.g. SUBMIT_LEAD_FORM (default), PURCHASE, CONTACT, BOOK_APPOINTMENT.")] = None,
        value: Annotated[float | None, Field(description="Value per conversion in the account's currency.")] = None,
        value_variable: Annotated[str | None, Field(description="Send a value from the site, e.g. {{DLV - value}}.")] = None,
        currency_variable: Annotated[str | None, Field(description="Currency from the site, e.g. {{DLV - currency}}.")] = None,
        transaction_id_variable: Annotated[str | None, Field(description="Order ID from the site, to avoid double counting.")] = None,
        counting_type: Annotated[Literal["ONE_PER_CLICK", "MANY_PER_CLICK"] | None, Field(description=(
            "Default: one per click, or every one for PURCHASE."))] = None,
        primary: bool = True,
        tag_name: str | None = None,
        add_conversion_linker: Annotated[bool, Field(description="Add a Conversion Linker tag if there's none.")] = True,
        add_google_tag: Annotated[bool, Field(description="Add a Google tag for this AW- ID if there's none.")] = True,
        publish: Annotated[bool, Field(description="Also publish the workspace so it goes live (extra confirmation).")] = False,
        version_name: str | None = None,
    ) -> dict[str, Any]:
        """Set up conversion tracking end to end in one preview: create the Google Ads conversion action (or use an
        existing one) AND in Tag Manager the trigger, the Google Ads conversion tag with the right ID and label, the
        Conversion Linker and Google tag if missing, and optionally publish. One apply_change makes it all; the
        undo token reverses it all."""
        a = dict(conversion_name=conversion_name, conversion_action_id=conversion_action_id, container=container,
                 workspace=workspace, trigger=trigger, existing_trigger_id=existing_trigger_id, type=type,
                 category=category, value=value, value_variable=value_variable, currency_variable=currency_variable,
                 transaction_id_variable=transaction_id_variable, counting_type=counting_type, primary=primary,
                 tag_name=tag_name, add_conversion_linker=add_conversion_linker, add_google_tag=add_google_tag,
                 publish=publish, version_name=version_name)
        return await w.run("setup_conversion_tracking", customer_id,
                           lambda user, grt: w.preview(user, grt, g.setup(grt, user, customer_id, a)))

    return g
