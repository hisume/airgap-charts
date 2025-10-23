import os
import logging
import argparse
from typing import Dict, List, Any, Tuple
from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

def _iter_dicts(node: Any):
    """
    Recursively yield all dict objects from a YAML-loaded Python structure.
    """
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _iter_dicts(v)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dicts(item)

def _split_chart(raw_chart: str) -> Tuple[str, str]:
    """
    Split a chart string into (chart_name, oci_namespace).
    If there are slashes, everything before the last slash is the OCI namespace.
    """
    if not raw_chart:
        return "", ""
    parts = [p for p in str(raw_chart).split("/") if p]
    if len(parts) <= 1:
        return parts[0], ""
    chart_name = parts[-1]
    oci_namespace = "/".join(parts[:-1])
    return chart_name, oci_namespace

def _looks_like_addon(d: Dict[str, Any]) -> bool:
    """
    Heuristic to determine if a dict looks like an addon chart spec.
    Requires at least chart and repository.
    Supports multiple key aliases for robustness.
    """
    if not isinstance(d, dict):
        return False
    raw_chart = d.get("chart") or d.get("addonChart")
    repo = d.get("repoUrl") or d.get("addonChartRepository") or d.get("repository")
    return bool(raw_chart and repo)

def _normalize(d: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize various key names into a canonical addon spec dict.
    - chart: normalized to bare chart name (last path segment)
    - oci_namespace: derived from chart path segments (all but last)
    - repository: repoUrl | addonChartRepository | repository
    - version: targetRevision | addonChartVersion | version (optional)
    - release: releaseName | addonChartReleaseName | release (optional)
    """
    raw_chart = d.get("chart") or d.get("addonChart")
    chart_name, oci_namespace = _split_chart(raw_chart)

    return {
        "chart": chart_name,
        "oci_namespace": oci_namespace or "",
        "repository": d.get("repoUrl") or d.get("addonChartRepository") or d.get("repository"),
        "version": d.get("targetRevision") or d.get("addonChartVersion") or d.get("version"),
        "release": d.get("releaseName") or d.get("addonChartReleaseName") or d.get("release") or "",
    }

def _dedupe(addons: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Dedupe addon specs by (chart, version, repository, oci_namespace).
    """
    seen = set()
    unique: List[Dict[str, Any]] = []
    for a in addons:
        key = (a.get("chart"), a.get("version"), a.get("repository"), a.get("oci_namespace"))
        if key not in seen:
            seen.add(key)
            unique.append(a)
    return unique

def discover_addons_in_values(values_path: str = "./values.yaml") -> List[Dict[str, Any]]:
    """
    Discover all addon chart specs from a values.yaml file. This supports:
    - A canonical top-level 'addons' key containing either a list or a map.
    - A heuristic recursive discovery over the entire document for objects that
      contain chart/repository (and optionally version/release).
    Returns a list of normalized addon spec dicts with keys:
      chart, oci_namespace, repository, version (optional), release (optional)

    Logs a warning for any top-level addons entry missing chart or repoUrl.
    """
    if not os.path.exists(values_path):
        raise FileNotFoundError(f"values file not found: {values_path}")

    yaml = YAML()
    with open(values_path, "r") as f:
        data = yaml.load(f) or {}

    addons: List[Dict[str, Any]] = []

    # Strategy A: canonical 'addons' key
    addons_node = data.get("addons")
    if addons_node:
        if isinstance(addons_node, list):
            for idx, item in enumerate(addons_node):
                if isinstance(item, dict):
                    # Warn if incomplete
                    has_chart = bool(item.get("chart") or item.get("addonChart"))
                    has_repo = bool(item.get("repoUrl") or item.get("addonChartRepository") or item.get("repository"))
                    if not (has_chart and has_repo):
                        rel = item.get("releaseName") or item.get("addonChartReleaseName") or ""
                        logger.warning(f"Skipping addon due to missing chart/repoUrl: index={idx}, releaseName='{rel}'")
                        continue
                    if _looks_like_addon(item):
                        addons.append(_normalize(item))
        elif isinstance(addons_node, dict):
            for name, item in addons_node.items():
                if isinstance(item, dict):
                    # Warn if incomplete
                    has_chart = bool(item.get("chart") or item.get("addonChart"))
                    has_repo = bool(item.get("repoUrl") or item.get("addonChartRepository") or item.get("repository"))
                    if not (has_chart and has_repo):
                        rel = item.get("releaseName") or item.get("addonChartReleaseName") or ""
                        logger.warning(f"Skipping addon due to missing chart/repoUrl: name='{name}', releaseName='{rel}'")
                        continue
                    if _looks_like_addon(item):
                        addons.append(_normalize(item))

    # Strategy B: recursive heuristic discovery (kept for flexibility)
    for d in _iter_dicts(data):
        if _looks_like_addon(d):
            addons.append(_normalize(d))

    addons = _dedupe(addons)
    # Filter invalid ones that are missing required keys after normalization
    addons = [a for a in addons if a.get("chart") and a.get("repository")]

    return addons

def write_catalog(values_path: str, out_path: str) -> list[dict]:
    """
    Extract and normalize addons from a values.yaml and write a catalog YAML.

    Catalog schema:
    addons:
      - chart: <str>
        repository: <str>
        oci_namespace: <str>
        version: <str|None>
        release: <str|None>
    """
    yaml = YAML()
    addons = discover_addons_in_values(values_path)
    data = {"addons": addons}
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f)
    return addons

def load_catalog(catalog_path: str) -> list[dict]:
    """
    Load a catalog YAML and return the normalized addons list.
    Validates required fields (chart, repository) and drops invalid entries.
    """
    if not os.path.exists(catalog_path):
        raise FileNotFoundError(f"catalog file not found: {catalog_path}")
    yaml = YAML()
    with open(catalog_path, "r", encoding="utf-8") as f:
        data = yaml.load(f) or {}
    addons = data.get("addons") or []
    # Defensive normalization: ensure keys exist and types are dicts
    norm: list[dict] = []
    for a in addons:
        if isinstance(a, dict) and a.get("chart") and a.get("repository"):
            norm.append({
                "chart": (a.get("chart") or "").strip(),
                "repository": (a.get("repository") or "").strip(),
                "oci_namespace": (a.get("oci_namespace") or "").strip(),
                "version": a.get("version"),
                "release": a.get("release") or "",
            })
    return norm

if __name__ == "__main__":
    """
    CLI entrypoint to generate a catalog from a values.yaml.
    Usage:
      python values_parser.py --values ./values.yaml --out ./catalog.yaml [--only-addon "a,b"] [--exclude-addons "x,y"]
    """
    parser = argparse.ArgumentParser(description="Generate a normalized addons catalog from values.yaml")
    parser.add_argument('--values', default='./values.yaml', help='Path to a values.yaml to discover addons from')
    parser.add_argument('--out', required=True, help='Output path for the generated catalog YAML (e.g., ./catalog.yaml)')
    parser.add_argument('--only-addon', required=False, help='Comma-separated chart names to include (exact match on chart, case-insensitive)')
    parser.add_argument('--exclude-addons', required=False, help='Comma-separated chart names to exclude (exact match on chart, case-insensitive)')
    args = parser.parse_args()

    try:
        addons = discover_addons_in_values(args.values)
        if not addons:
            logger.warning(f"No addons discovered in {args.values}")

        # Apply include filter
        if getattr(args, "only_addon", None):
            selectors = {s.strip().lower() for s in str(args.only_addon).split(",") if s.strip()}
            if selectors:
                before = len(addons)
                addons = [a for a in addons if (a.get("chart") or "").strip().lower() in selectors]
                logger.info(f"Selected {len(addons)}/{before} addons via --only-addon: {', '.join(sorted(selectors))}")

        # Apply exclude filter
        if getattr(args, "exclude_addons", None):
            excludes = {s.strip().lower() for s in str(args.exclude_addons).split(",") if s.strip()}
            if excludes:
                before = len(addons)
                addons = [a for a in addons if (a.get("chart") or "").strip().lower() not in excludes]
                logger.info(f"Excluded {before - len(addons)} addons via --exclude-addons: {', '.join(sorted(excludes))}")

        # Write catalog
        yaml = YAML()
        out_data = {"addons": addons}
        with open(args.out, "w", encoding="utf-8") as f:
            yaml.dump(out_data, f)
        logger.info(f"Wrote catalog with {len(addons)} addons to {args.out}")
    except Exception as e:
        logger.error(f"Failed to generate catalog: {e}")
        raise SystemExit(1)
