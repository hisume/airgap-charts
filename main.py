import argparse
import logging
import os
import sys
import shutil
from typing import Optional
from yamls import get_yaml_files, extract_values, copy_and_update_yaml
from image_yaml import extract_chart_values_image, convert_dict_to_yaml
from chart import HelmChart, configure_colored_logging, set_log_context, clear_log_context
from values_parser import discover_addons_in_values

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# Enable colored, contextual logging
configure_colored_logging()

def check_dependencies(will_push: bool) -> None:
    """
    Ensure required CLI tools are available on PATH.
    Always require: helm, yq
    Require when pushing: docker, aws
    Exits the program with an error if any are missing.
    """
    required = ["helm", "yq"]
    if will_push:
        required += ["docker", "aws"]
    missing = [cmd for cmd in required if shutil.which(cmd) is None]
    if missing:
        logger.error(f"Missing required CLI tools: {', '.join(missing)}. Install them and ensure they are on PATH.")
        sys.exit(1)

def process_helm_chart(helm_chart: HelmChart, downloaded_chart_folder: str, scan_only: bool, push_images: bool, pull_latest_flag: bool, yaml_file: Optional[str], new_appspec_folder: str, base_appspec_folder: str, target_registry: Optional[str], repository_prefix: Optional[str], include_dependencies: bool):
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
            return None
        else:
            logger.info(f"Using version {remote_version} for {helm_chart.addon_chart}")
    except Exception as e:
        logger.error(f"Failed to get the version for {helm_chart.addon_chart}: {e}")
        clear_log_context()
        return None

    try:
        chart_file = helm_chart.download_chart(downloaded_chart_folder, remote_version)
        if not chart_file:
            logger.error(f"Failed to download chart for {helm_chart.addon_chart}")
            return None

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

        # If in appspec mode, update and copy the original appspec YAML
        if yaml_file:
            copy_and_update_yaml(yaml_file, new_appspec_folder, base_appspec_folder, new_version=helm_chart.addon_chart_version, private_ecr_url=helm_chart.private_ecr_url)
            logger.info(f"Updated YAML file copied to: {new_appspec_folder}")

        logger.info(f"Finalised the process\n CHART INFO:\n{helm_chart}\n END!\n")
        # Clear context before returning
        clear_log_context()
        return helm_chart
    except Exception as e:
        logger.error(f"Failed to download or push chart: {e}")
        clear_log_context()
        return None

def main(appspec_name: Optional[str], scan_only: bool, push_images: bool, latest: bool = False, values_path: Optional[str] = None):
    """
    Main function to process Helm charts either from:
    - AppSet mode: scanning ./application-sets for a matching appspec file, or
    - Values mode: parsing a local values.yaml that lists multiple addons.
    """
    base_appspec_folder = './application-sets'
    downloaded_chart_folder = './helm-charts'
    new_appspec_folder = './airgaped-application-sets'
    processed_charts = []

    # Determine push behavior and verify dependencies upfront
    will_push = push_images or (not scan_only and not push_images)
    check_dependencies(will_push)

    # Determine mode
    appspec_mode = bool(appspec_name)
    values_mode = (not appspec_mode) and bool(values_path) and os.path.exists(values_path)

    if appspec_mode:
        yaml_files = get_yaml_files(base_appspec_folder)
        # Filter the specific appspec file based on the name passed as argument
        yaml_files = [f for f in yaml_files if appspec_name in f]
        logger.info(f"Found {len(yaml_files)} YAML files in {base_appspec_folder} matching '{appspec_name}'")

        for yaml_file in yaml_files:
            chart_info = extract_values(yaml_file)
            if not chart_info:
                logger.info(f"No extractable chart info in {yaml_file}")
                continue
            logger.info(chart_info)

            # chart_info is a HelmChart instance already; we re-instantiate to keep a clean object
            helm_chart = HelmChart(
                addon_chart=chart_info.addon_chart,
                addon_chart_version=chart_info.addon_chart_version,
                addon_chart_repository=chart_info.addon_chart_repository,
                addon_chart_repository_namespace=chart_info.addon_chart_repository_namespace,
                addon_chart_release_name=chart_info.addon_chart_release_name
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

            # Use global latest flag as provided
            result = process_helm_chart(
                helm_chart=helm_chart,
                downloaded_chart_folder=downloaded_chart_folder,
                scan_only=scan_only,
                push_images=push_images,
                pull_latest_flag=latest,
                yaml_file=yaml_file,
                new_appspec_folder=new_appspec_folder,
                base_appspec_folder=base_appspec_folder,
                target_registry=args.target_registry,
                repository_prefix=args.target_prefix,
                include_dependencies=args.include_dependencies,
            )
            if result:
                processed_charts.append(result)

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
                yaml_file=None,  # no appspec copy/update in values mode
                new_appspec_folder=new_appspec_folder,
                base_appspec_folder=base_appspec_folder,
                target_registry=args.target_registry,
                repository_prefix=args.target_prefix,
                include_dependencies=args.include_dependencies,
            )
            if result:
                processed_charts.append(result)
    else:
        logger.error("No valid mode selected. Provide --appspec to process a specific appspec from application-sets "
                     "or supply --values pointing to a values.yaml file containing multiple addons.")
        return

    # Summary
    for chart in processed_charts:
        logger.info(chart)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Process Helm charts and update them with new versions and image repositories.')
    parser.add_argument('--appspec', required=False, help='The name of the appspec to process (AppSet mode)')
    parser.add_argument('--values', default='./values.yaml', help='Path to a values.yaml that lists addons (Values mode)')
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
    args = parser.parse_args()
    main(args.appspec, args.scan_only, args.push_images, args.latest, args.values)
