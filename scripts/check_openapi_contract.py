#!/usr/bin/env python3
"""Check OpenAPI contract coverage against CherryPy exposed endpoints.

This check enforces both directions:
- every OpenAPI path must exist in code
- every API endpoint in code must exist in OpenAPI OR be explicitly allowlisted

Method checks are warning-only by default and can be made strict.
"""

from __future__ import annotations

import argparse
import ast
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "repeater" / "web"
OPENAPI_PATH = WEB_DIR / "openapi.yaml"
ALLOWLIST_PATH = ROOT / "scripts" / "openapi_contract_allowlist.yaml"


@dataclass
class RouteInfo:
    methods: set[str]
    confident: bool


@dataclass
class Allowlist:
    exact: set[str]
    prefixes: tuple[str, ...]


@dataclass(frozen=True)
class ClassRouteSpec:
    """Describe where one CherryPy endpoint class is mounted.

    ``path_params`` is needed only for classes using ``_cp_dispatch``.  It
    maps handler parameter names to the collection segment that precedes
    them.  ``None`` means the parameter is the collection's own member:

    - ``companion_name: None`` -> ``/{}``
    - ``contact_pubkey: "contacts"`` -> ``/contacts/{}``

    The exposed handler name is then treated as an action unless it names
    that member resource (``contact``/``channel``), or is the HTTP verb
    itself (``delete``).  This mirrors the small, readable dispatch pattern
    used by the v1 API without copying every route into this checker.
    """

    module_path: Path
    class_name: str
    prefixes: tuple[str, ...]
    path_params: dict[str, str | None] | None = None


def _normalize_path(path: str) -> str:
    path = path.strip()
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    path = re.sub(r"/{2,}", "/", path)
    path = re.sub(r"\{[^/}]+\}", "{}", path)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return path


def _load_openapi() -> dict[str, set[str]]:
    with OPENAPI_PATH.open("r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}

    paths = doc.get("paths", {})
    out: dict[str, set[str]] = {}
    for raw_path, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        methods = {
            m.lower()
            for m in ops.keys()
            if m.lower() in {"get", "post", "put", "delete", "patch", "options", "head"}
        }
        # We do not enforce OPTIONS/HEAD in this checker.
        methods.discard("options")
        methods.discard("head")
        out[_normalize_path(str(raw_path))] = methods
    return out


def _load_allowlist() -> Allowlist:
    if not ALLOWLIST_PATH.exists():
        return Allowlist(exact=set(), prefixes=tuple())

    with ALLOWLIST_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    exact_raw = data.get("exact_paths", [])
    prefix_raw = data.get("path_prefixes", [])

    exact = {_normalize_path(str(p)) for p in exact_raw if str(p).strip()}
    prefixes = tuple(_normalize_path(str(p)) for p in prefix_raw if str(p).strip())
    return Allowlist(exact=exact, prefixes=prefixes)


def _is_allowlisted(path: str, allowlist: Allowlist) -> bool:
    if path in allowlist.exact:
        return True
    for prefix in allowlist.prefixes:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _is_cherrypy_request_method_expr(node: ast.AST) -> bool:
    # cherrypy.request.method
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "method"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "request"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "cherrypy"
    )


def _extract_method_strings(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value.upper()}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        vals: set[str] = set()
        for e in node.elts:
            vals |= _extract_method_strings(e)
        return vals
    return set()


def _is_request_method_ref(node: ast.AST, aliases: set[str]) -> bool:
    return _is_cherrypy_request_method_expr(node) or (
        isinstance(node, ast.Name) and node.id in aliases
    )


def _infer_methods(fn: ast.FunctionDef) -> tuple[set[str], bool]:
    methods: set[str] = set()
    confidence = False
    positive_method_branches: set[str] = set()

    # Handlers commonly bind ``method = cherrypy.request.method`` before
    # branching.  Record those aliases once, then treat them exactly like the
    # full CherryPy attribute expression below.
    method_aliases = {
        target.id
        for node in ast.walk(fn)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, ast.Name)
        and _is_cherrypy_request_method_expr(node.value)
    }

    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            # self._require_get() / self._require_post()
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"_require_get", "_require_post"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            ):
                methods.add(node.func.attr.removeprefix("_require_").upper())
                confidence = True

        if not isinstance(node, ast.Compare):
            continue
        if not node.ops or not node.comparators:
            continue

        left_is_method = _is_request_method_ref(node.left, method_aliases)
        right_is_method = _is_request_method_ref(node.comparators[0], method_aliases)
        if not left_is_method and not right_is_method:
            continue

        op = node.ops[0]
        value_node = node.comparators[0] if left_is_method else node.left
        compared_methods = _extract_method_strings(value_node)
        if not compared_methods:
            continue

        # Treat equality and inequality guards as declared allowed methods.
        if isinstance(op, (ast.Eq, ast.In, ast.NotEq, ast.NotIn)):
            methods |= compared_methods
            confidence = True
            if isinstance(op, (ast.Eq, ast.In)):
                positive_method_branches |= compared_methods

    methods.discard("OPTIONS")
    methods.discard("HEAD")

    # A common two-mode handler handles POST in an early branch and lets the
    # rest of the function be its GET implementation.  Count GET only when
    # there is an unconditional top-level return after that positive branch;
    # unlike the old blanket heuristic, this does not mistake
    # ``if method != "POST": raise 405`` for GET support.
    if "POST" in positive_method_branches and methods == {"POST"}:
        branch_indexes = [
            index
            for index, statement in enumerate(fn.body)
            if isinstance(statement, ast.If)
            and any(
                isinstance(node, ast.Compare)
                and any(
                    method in positive_method_branches
                    for method in _extract_method_strings(
                        node.comparators[0] if node.comparators else node
                    )
                )
                for node in ast.walk(statement.test)
            )
        ]
        if branch_indexes and any(
            isinstance(statement, ast.Return)
            for statement in fn.body[min(branch_indexes) + 1 :]
        ):
            methods.add("GET")

    if not methods:
        return {"GET"}, False
    return methods, confidence


def _has_expose_decorator(fn: ast.FunctionDef) -> bool:
    for d in fn.decorator_list:
        # @cherrypy.expose
        if isinstance(d, ast.Attribute):
            if d.attr == "expose" and isinstance(d.value, ast.Name) and d.value.id == "cherrypy":
                return True
    return False


def _fn_params(fn: ast.FunctionDef) -> list[str]:
    params = [a.arg for a in fn.args.args if a.arg != "self"]
    return [p for p in params if p not in {"kwargs", "args"}]


def _candidate_suffixes(
    fn: ast.FunctionDef,
    methods: set[str],
    path_params: dict[str, str | None] | None,
) -> list[str]:
    name = fn.name
    params = _fn_params(fn)

    if name == "index":
        return [""]

    if name == "default":
        if params:
            return ["/{}"]
        return ["/{path}"]

    if path_params:
        member_params = [param for param in params if param in path_params]
        if member_params:
            parts: list[str] = []
            last_collection: str | None = None
            for param in member_params:
                collection = path_params[param]
                if collection is not None:
                    parts.append(collection)
                    last_collection = collection
                parts.append("{}")

            singular_resource = (
                last_collection[:-1]
                if last_collection and last_collection.endswith("s")
                else last_collection
            )
            is_member_handler = name == singular_resource
            is_http_verb_handler = name.upper() in methods
            if not is_member_handler and not is_http_verb_handler:
                parts.append(name)
            return ["/" + "/".join(parts)]

    base = f"/{name}"
    # Keep named endpoints canonical. Parameters are often query parameters,
    # so adding path-segment variants here produces false positives.
    return [base]


def _collect_class_routes(
    module_path: Path,
    class_name: str,
    prefixes: tuple[str, ...],
    path_params: dict[str, str | None] | None = None,
) -> dict[str, RouteInfo]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    cls = next(
        (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name),
        None,
    )
    if cls is None:
        return {}

    routes: dict[str, RouteInfo] = {}
    for node in cls.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if class_name == "APIEndpoints" and node.name == "default":
            # Catch-all handlers are not part of API contract surface.
            continue
        if not _has_expose_decorator(node):
            continue

        methods, confident = _infer_methods(node)
        suffixes = _candidate_suffixes(node, methods, path_params)

        for prefix in prefixes:
            for suffix in suffixes:
                path = _normalize_path(prefix + suffix)
                cur = routes.get(path)
                if cur is None:
                    routes[path] = RouteInfo(methods=set(methods), confident=confident)
                else:
                    cur.methods |= methods
                    cur.confident = cur.confident or confident
    return routes


def _collect_routes() -> dict[str, RouteInfo]:
    route_map: dict[str, RouteInfo] = {}

    class_specs = [
        # /api/* methods are described in OpenAPI as /<endpoint>
        ClassRouteSpec(WEB_DIR / "api_endpoints.py", "APIEndpoints", ("",)),
        # Nested /api/companion/* endpoints are described as /companion/*.
        ClassRouteSpec(
            WEB_DIR / "companion_endpoints.py",
            "CompanionAPIEndpoints",
            ("/companion",),
        ),
        # Nested /api/update/* endpoints are described as /update/* when documented.
        ClassRouteSpec(
            WEB_DIR / "update_endpoints.py", "UpdateAPIEndpoints", ("/update",)
        ),
        # Auth top-level endpoints are mounted at /auth/*
        ClassRouteSpec(WEB_DIR / "auth_endpoints.py", "AuthEndpoints", ("/auth",)),
        # APIEndpoints.auth mounts this below the checker's implicit /api base,
        # so the OpenAPI-relative route is /auth/tokens.
        ClassRouteSpec(
            WEB_DIR / "auth_endpoints.py",
            "TokensAPIEndpoint",
            ("/auth/tokens",),
        ),
        # Mobile companion API v1.  The root and PairV1 use ordinary
        # attribute dispatch; CompanionsV1 and DevicesV1 use _cp_dispatch
        # for collection members and actions.
        ClassRouteSpec(
            WEB_DIR / "mobile_endpoints.py", "MobileAPIEndpoints", ("/v1",)
        ),
        ClassRouteSpec(
            WEB_DIR / "mobile_endpoints.py",
            "PairV1",
            ("/v1/pair",),
        ),
        ClassRouteSpec(
            WEB_DIR / "mobile_endpoints.py",
            "CompanionsV1",
            ("/v1/companions",),
            {
                "companion_name": None,
                "contact_pubkey": "contacts",
                "channel_index": "channels",
                "message_id": "messages",
                "packet_hash": "transmissions",
            },
        ),
        ClassRouteSpec(
            WEB_DIR / "mobile_endpoints.py",
            "DevicesV1",
            ("/v1/devices",),
            {"device_id": None},
        ),
    ]

    for spec in class_specs:
        class_routes = _collect_class_routes(
            spec.module_path,
            spec.class_name,
            spec.prefixes,
            spec.path_params,
        )
        for path, info in class_routes.items():
            cur = route_map.get(path)
            if cur is None:
                route_map[path] = info
            else:
                cur.methods |= info.methods
                cur.confident = cur.confident or info.confident

    return route_map


def main() -> int:
    parser = argparse.ArgumentParser(description="Check OpenAPI contract coverage.")
    parser.add_argument(
        "--strict-methods",
        action="store_true",
        help="Fail when inferred HTTP methods differ from OpenAPI methods.",
    )
    args = parser.parse_args()

    if not OPENAPI_PATH.exists():
        print(f"ERROR: OpenAPI spec not found at {OPENAPI_PATH}")
        return 1

    spec = _load_openapi()
    allowlist = _load_allowlist()
    code_routes = _collect_routes()

    errors: list[str] = []
    warnings: list[str] = []

    for path, spec_methods in sorted(spec.items()):
        code = code_routes.get(path)
        if code is None:
            errors.append(f"Missing endpoint in code for OpenAPI path: {path}")
            continue

        if spec_methods and code.confident:
            code_methods = {m.lower() for m in code.methods}
            missing_methods = sorted(m for m in spec_methods if m not in code_methods)
            if missing_methods:
                msg = (
                    f"Method mismatch for {path}: OpenAPI has {sorted(spec_methods)}, "
                    f"code inference has {sorted(code_methods)}"
                )
                if args.strict_methods:
                    errors.append(msg)
                else:
                    warnings.append(msg)

    # Enforce code -> OpenAPI (unless allowlisted)
    for path in sorted(code_routes.keys()):
        if path in spec:
            continue
        if _is_allowlisted(path, allowlist):
            continue
        errors.append(f"Undocumented endpoint in code (not in OpenAPI and not allowlisted): {path}")

    if warnings:
        print("OpenAPI contract warnings:")
        for w in warnings:
            print(f"- {w}")

    if errors:
        print("OpenAPI contract check failed:")
        for e in errors:
            print(f"- {e}")
        return 1

    print("OpenAPI contract check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
