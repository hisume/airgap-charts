import argparse
import logging
import os
import sys
import shutil
from typing import Optional
from image_yaml import extract_chart_values_image, convert_dict_to_yaml
from chart import HelmChart, configure_colored_logging, set_log_context, clear_log_context
from values_parser import discover_addons_in_values, load_catalog
from colorama import Fore, Style

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# Enable colored, contextual logging
configure_colored_logging()

def check_dependencies(will_push: bool) -> None:
    """
    Ensure required CLI tools are available on PATH.
    Always require: helm, yq, crane
    Require when pushing: aws
    Exits the program with an error if any are missing.
    """
    required = ["helm", "yq", "crane"]
    if will_push:
        required += ["aws"]
    missing = [cmd for cmd in required if shutil.which(cmd) is None]
    if missing:
        logger.error(f"Missing required CLI tools: {', '.join(missing)}. Install them and ensure they are on PATH.")
        sys.exit(1)

def process_helm_chart(helm_chart: HelmChart, downloaded_chart_folder: str, scan_only: bool, push_images: bool, pull_latest_flag: bool, target_registry: Optional[str], repository_prefix: Optional[str], include_dependencies: bool):
    """
    Core pipeline to resolve version, download chart, extract and push images, and optionally push chart.
    In appspec mode, also updates and copies the original appspec YAML.
    """
    # Set addon log context (top-level)
    set_log_context(helm_chart.addon_chart, 0)
    try:
        remote_version = helm_chart.get_remote_version(pull_latest_flag)
        if remote_version is None:
            logger.error(f"Failed to get the version for {helm_chart.addon_chart}")
            clear_log_context()
            return None, f"Failed to get version for {helm_chart.addon_chart}"
        else:
            logger.info(f"Using version {remote_version} for {helm_chart.addon_chart}")
    except Exception as e:
        logger.error(f"Failed to get the version for {helm_chart.addon_chart}: {e}")
        clear_log_context()
        return None, str(e)

    try:
        chart_file = helm_chart.download_chart(downloaded_chart_folder, remote_version)
        if not chart_file:
            msg = f"Failed to download chart for {helm_chart.addon_chart}"
            logger.error(msg)
            return helm_chart, msg

        logger.info(f"Chart downloaded to: {chart_file}")
        helm_chart.get_private_ecr_url()
        if target_registry:
            helm_chart.private_ecr_url = target_registry
        helm_chart.repository_prefix = repository_prefix or ""
        logger.info(f"Private registry: {helm_chart.private_ecr_url} (prefix='{helm_chart.repository_prefix}')")
        # Indent nested operations for this addon
        set_log_context(helm_chart.addon_chart, 1)
        # Log dependency graph for better visibility of subcharts
        chart_root = f"{os.path.dirname(chart_file)}/{helm_chart.addon_chart}"
        helm_chart.log_chart_dependencies(chart_root)
        # Extract images from chart; include or exclude vendored subcharts/dependencies
        helm_chart.get_chart_images(chart_file, exclude_dependencies=not include_dependencies)
        logger.info(f"Images extracted from the chart: {helm_chart.public_addon_chart_images}")
        logger.info("Pulling chart Images...")
        helm_chart.pulling_chart_images()

        # Inject private image mapping into the chart's packaged values.yaml and repack before pushing
        # Compute private refs first (mirror-source layout), then overlay so packaged defaults match pushed images
        try:
            values_folder_from_chart_file = f"{os.path.dirname(chart_file)}/{helm_chart.addon_chart}/values.yaml"
            public_images = helm_chart.public_addon_chart_images
            private_refs = helm_chart.compute_private_refs()
            # Make private refs available downstream as well
            helm_chart.private_addon_chart_images = private_refs
            chart_values_overlay = extract_chart_values_image(values_folder_from_chart_file, public_images, private_refs)
            chart_root = f"{os.path.dirname(chart_file)}/{helm_chart.addon_chart}"
            helm_chart.apply_values_overlay(chart_root, chart_values_overlay)
            helm_chart.repack_chart(chart_root, chart_file)
        except Exception as e:
            logger.warning(f"Failed to apply private image overlay/repack for {helm_chart.addon_chart}: {e}")

        if push_images or (not scan_only and not push_images):
            logger.info("Pushing Images to ECR...")
            helm_chart.push_images_to_ecr()
            logger.info("Pushing Chart to ECR...")
            helm_chart.push_chart_to_ecr(chart_file)

        # Build values.yaml mapping with private images for future use
        # Path to the chart's own values.yaml extracted from the tgz:
        values_folder_from_chart_file = f"{os.path.dirname(chart_file)}/{helm_chart.addon_chart}/values.yaml"
        public_images = helm_chart.public_addon_chart_images
        private_images = helm_chart.private_addon_chart_images
        chart_values = extract_chart_values_image(values_folder_from_chart_file, public_images, private_images)
        output_values_folder = f"{os.path.dirname(chart_file)}/values.yaml"
        convert_dict_to_yaml(chart_values, output_values_folder)


        # Build summary status
        # Drop benign helm dependency lock sync failures from command errors
        if helm_chart.failed_commands:
            helm_chart.failed_commands = [
                fc for fc in helm_chart.failed_commands
                if "Failed to build chart dependencies" not in str(fc[1])
                and "Failed to build chart dependencies (after update)" not in str(fc[1])
            ]
        error_msgs = []
        if helm_chart.failed_pull_addon_chart_images:
            error_msgs.append(f"Failed pulls: {helm_chart.failed_pull_addon_chart_images}")
        if helm_chart.failed_push_addon_chart_images:
            error_msgs.append(f"Failed pushes: {helm_chart.failed_push_addon_chart_images}")
        if helm_chart.failed_commands:
            last_cmd = helm_chart.failed_commands[-1] if helm_chart.failed_commands else None
            if last_cmd:
                error_msgs.append(f"Cmd error: {last_cmd[1]} -> {str(last_cmd[2])[:200]}")
        error_text = "; ".join(error_msgs) if error_msgs else ""
        # Clear context before returning
        clear_log_context()
        return helm_chart, (error_text or None)
    except Exception as e:
        logger.error(f"Failed to download or push chart: {e}")
        clear_log_context()
        return None, str(e)

def main(scan_only: bool, push_images: bool, latest: bool = False, values_path: Optional[str] = None):
    """
    Main function to process Helm charts either from:
    - AppSet mode: scanning ./application-sets for a matching appspec file, or
    - Values mode: parsing a local values.yaml that lists multiple addons.
    """
    downloaded_chart_folder = './helm-charts'
    processed_charts = []
    summaries = []

    # Determine push behavior and verify dependencies upfront
    will_push = push_images or (not scan_only and not push_images)
    check_dependencies(will_push)

    # Determine mode
    values_mode = bool(values_path) and os.path.exists(values_path)

    # Catalog mode: allow multiple catalog files (repeat flag or comma-separated)
    catalog_paths = []
    if getattr(args, "catalog", None):
        inputs = args.catalog if isinstance(args.catalog, list) else [args.catalog]
        for c in inputs:
            parts = [p.strip() for p in str(c).split(",") if p.strip()]
            catalog_paths.extend(parts)

    if catalog_paths:
        for catalog_path in catalog_paths:
            if not os.path.exists(catalog_path):
                logger.warning(f"Catalog not found: {catalog_path}; skipping.")
                continue
            logger.info(f"Loading addons from catalog: {catalog_path}")
            try:
                addons = load_catalog(catalog_path)
            except Exception as e:
                logger.error(f"Failed to load catalog from {catalog_path}: {e}")
                continue

            if not addons:
                logger.warning(f"No addons discovered in catalog {catalog_path}")
                continue

            logger.info(f"Loaded {len(addons)} addons from catalog")
            # Optional filter to process only specific addons by exact release name (catalog mode)
            if getattr(args, "only_addon", None):
                selectors = {s.strip().lower() for s in str(args.only_addon).split(",") if s.strip()}
                if selectors:
                    before = len(addons)
                    addons = [a for a in addons if (a.get("release") or "").strip().lower() in selectors]
                    selected_names = ", ".join([a.get("release") or "" for a in addons])
                    logger.info(f"Selected {len(addons)}/{before} addons via --only-addon: {selected_names}")
                    if not addons:
                        logger.warning("No addons matched --only-addon filter; skipping this catalog.")
                        continue
            # Optional exclusion filter by release name (catalog mode)
            if getattr(args, "exclude_addons", None):
                excludes = {s.strip().lower() for s in str(args.exclude_addons).split(",") if s.strip()}
                if excludes:
                    before = len(addons)
                    addons = [a for a in addons if (a.get("release") or "").strip().lower() not in excludes]
                    logger.info(f"Excluded {before - len(addons)} addons via --exclude-addons (by release): {', '.join(sorted(excludes))}")
                    if not addons:
                        logger.warning("All addons excluded by --exclude-addons for this catalog; skipping.")
                        continue

            for spec in addons:
                helm_chart = HelmChart(
                    addon_chart=spec.get('chart'),
                    addon_chart_version=spec.get('version'),
                    addon_chart_repository=spec.get('repository'),
                    addon_chart_repository_namespace=spec.get('oci_namespace') or "",
                    addon_chart_release_name=spec.get('release') or ""
                )
                # Attach optional ECR password overrides (CLI flags or env vars)
                public_pw = args.public_ecr_password or os.getenv("ECR_PUBLIC_PASSWORD", "")
                private_pw = args.private_ecr_password or os.getenv("ECR_PRIVATE_PASSWORD", "")
                helm_chart.public_ecr_password = public_pw
                helm_chart.private_ecr_password = private_pw
                # Platform preference
                helm_chart.platform = args.platform
                # Docker Hub credentials (optional)
                helm_chart.dockerhub_username = args.dockerhub_username or os.getenv("DOCKERHUB_USERNAME", "")
                helm_chart.dockerhub_token = args.dockerhub_token or os.getenv("DOCKERHUB_TOKEN", "")

                # If version is not specified in catalog, force pull_latest for this chart
                pull_latest_flag = latest or (not bool(spec.get('version')))

                result = process_helm_chart(
                    helm_chart=helm_chart,
                    downloaded_chart_folder=downloaded_chart_folder,
                    scan_only=scan_only,
                    push_images=push_images,
                    pull_latest_flag=pull_latest_flag,
                    target_registry=args.target_registry,
                    repository_prefix=args.target_prefix,
                    include_dependencies=args.include_dependencies,
                )
                if result:
                    hc, err = result
                    if hc:
                        processed_charts.append(hc)
                        summaries.append({
                            "name": hc.addon_chart,
                            "version": hc.addon_chart_version,
                            "error": err or ""
                        })
                    else:
                        summaries.append({
                            "name": spec.get('chart') or "unknown",
                            "version": spec.get('version') or "",
                            "error": err or "unknown error"
                        })

    elif values_mode:
        logger.info(f"Parsing addons from values file: {values_path}")
        try:
            addons = discover_addons_in_values(values_path)
        except Exception as e:
            logger.error(f"Failed to parse addons from {values_path}: {e}")
            return

        if not addons:
            logger.warning(f"No addons discovered in {values_path}")
            return

        logger.info(f"Discovered {len(addons)} addons in {values_path}")
        # Optional filter to process only specific addons by exact chart name
        if getattr(args, "only_addon", None):
            selectors = {s.strip().lower() for s in str(args.only_addon).split(",") if s.strip()}
            if selectors:
                before = len(addons)
                addons = [a for a in addons if (a.get("chart") or "").strip().lower() in selectors]
                selected_names = ", ".join([a.get("chart") or "" for a in addons])
                logger.info(f"Selected {len(addons)}/{before} addons via --only-addon: {selected_names}")
                if not addons:
                    logger.warning("No addons matched --only-addon filter; exiting.")
                    return
        # Optional exclusion filter by exact chart name
        if getattr(args, "exclude_addons", None):
            excludes = {s.strip().lower() for s in str(args.exclude_addons).split(",") if s.strip()}
            if excludes:
                before = len(addons)
                addons = [a for a in addons if (a.get("chart") or "").strip().lower() not in excludes]
                logger.info(f"Excluded {before - len(addons)} addons via --exclude-addons: {', '.join(sorted(excludes))}")
                if not addons:
                    logger.warning("All addons excluded by --exclude-addons; exiting.")
                    return
        for spec in addons:
            helm_chart = HelmChart(
                addon_chart=spec.get('chart'),
                addon_chart_version=spec.get('version'),
                addon_chart_repository=spec.get('repository'),
                addon_chart_repository_namespace=spec.get('oci_namespace') or "",
                addon_chart_release_name=spec.get('release') or ""
            )
            # Attach optional ECR password overrides (CLI flags or env vars)
            public_pw = args.public_ecr_password or os.getenv("ECR_PUBLIC_PASSWORD", "")
            private_pw = args.private_ecr_password or os.getenv("ECR_PRIVATE_PASSWORD", "")
            helm_chart.public_ecr_password = public_pw
            helm_chart.private_ecr_password = private_pw
            # Platform preference
            helm_chart.platform = args.platform
            # Docker Hub credentials (optional)
            helm_chart.dockerhub_username = args.dockerhub_username or os.getenv("DOCKERHUB_USERNAME", "")
            helm_chart.dockerhub_token = args.dockerhub_token or os.getenv("DOCKERHUB_TOKEN", "")

            # If version is not specified in values, force pull_latest for this chart
            pull_latest_flag = latest or (not bool(spec.get('version')))

            result = process_helm_chart(
                helm_chart=helm_chart,
                downloaded_chart_folder=downloaded_chart_folder,
                scan_only=scan_only,
                push_images=push_images,
                pull_latest_flag=pull_latest_flag,
                target_registry=args.target_registry,
                repository_prefix=args.target_prefix,
                include_dependencies=args.include_dependencies,
            )
            if result:
                hc, err = result
                if hc:
                    processed_charts.append(hc)
                    summaries.append({
                        "name": hc.addon_chart,
                        "version": hc.addon_chart_version,
                        "error": err or ""
                    })
                else:
                    summaries.append({
                        "name": spec.get('chart') or "unknown",
                        "version": spec.get('version') or "",
                        "error": err or "unknown error"
                    })
    else:
        logger.error("No valid mode selected. Supply --catalog for catalog mode, or --values pointing to a values.yaml file containing addons.")
        return

    # Summary (name, version, status in green/red, error text if any)
    if summaries:
        logger.info("Summary:")
        summaries = sorted(summaries, key=lambda s: (s.get("name") or "").lower())
    for s in summaries:
        name = s.get("name") or "unknown"
        version = s.get("version") or ""
        err = (s.get("error") or "").strip()
        if not err:
            logger.info(f"{Fore.GREEN}SUCCESS{Style.RESET_ALL} {name} v{version}")
        else:
            logger.error(f"{Fore.RED}ERROR{Style.RESET_ALL} {name} v{version} - {err}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Process Helm charts and update them with new versions and image repositories.')
    parser.add_argument('--values', default='./values.yaml', help='Path to a values.yaml that lists addons (Values mode)')
    parser.add_argument('--catalog', action='append', required=False, help='Path(s) to one or more addons catalog YAML files (repeat flag or comma-separated). Alternate input to --values')
    parser.add_argument('--latest', action='store_true', help='Flag that pulls the latest images/versions when available')
    parser.add_argument('--scan-only', action='store_true', help='Flag to only scan images and not push them')
    parser.add_argument('--push-images', action='store_true', help='Flag to push images to ECR')
    parser.add_argument('--target-registry', required=False, help='Override target OCI registry (e.g., 123456789012.dkr.ecr.us-west-2.amazonaws.com)')
    parser.add_argument('--target-prefix', default='', help='Optional repository prefix path under the registry (e.g., team/x)')
    # Optional ECR password overrides (fall back to env vars ECR_PUBLIC_PASSWORD / ECR_PRIVATE_PASSWORD)
    parser.add_argument('--public-ecr-password', required=False, help='Override token/password for public.ecr.aws (or set env ECR_PUBLIC_PASSWORD)')
    parser.add_argument('--private-ecr-password', required=False, help='Override token/password for private ECR (or set env ECR_PRIVATE_PASSWORD)')
    # Dependency rendering flags (default: include dependencies)
    parser.add_argument('--include-dependencies', dest='include_dependencies', action='store_true', help='Render and include chart dependencies when extracting images (default)')
    parser.add_argument('--exclude-dependencies', dest='include_dependencies', action='store_false', help='Do not render chart dependencies when extracting images')
    parser.set_defaults(include_dependencies=True)
    # Platform selection for pulling images (multi-arch handling)
    parser.add_argument('--platform', choices=['auto', 'linux/amd64', 'linux/arm64'], default='auto',
                        help="Platform to pull images for. 'auto' tries no platform then linux/amd64 then linux/arm64")
    # Docker Hub auth (optional; increases rate limits and allows private pulls)
    parser.add_argument('--dockerhub-username', required=False, help='Docker Hub username for authenticated pulls (or set DOCKERHUB_USERNAME)')
    parser.add_argument('--dockerhub-token', required=False, help='Docker Hub access token/password for the username (or set DOCKERHUB_TOKEN)')
    # ECR preflight controls
    parser.add_argument('--skip-existing', action='store_true', default=True, help='Skip pushing images that already exist in private ECR (default: true)')
    parser.add_argument('--verify-existing-digest', action='store_true', default=False, help='When skipping existing, verify digest matches source digest for selected platform')
    parser.add_argument('--overwrite-existing', action='store_true', default=False, help='When digest differs in ECR, delete and overwrite the remote tag')
    # Only process specific addons by exact chart name (comma-separated, case-insensitive), values mode only
    parser.add_argument('--only-addon', required=False, help='Filter: Values mode = chart names; Catalog mode = release names (comma-separated, case-insensitive, exact match)')
    parser.add_argument('--exclude-addons', required=False, help='Filter: Values mode = chart names; Catalog mode = release names (comma-separated, case-insensitive, exact match)')
    args = parser.parse_args()
    if not os.path.exists(args.values or "./values.yaml"):
        logger.error(f"Values file not found: {args.values}")
        sys.exit(1)
    main(args.scan_only, args.push_images, args.latest, args.values)
