import os
import logging
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
