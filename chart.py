import os
import subprocess
import tarfile
import boto3
import logging
from botocore.exceptions import ClientError
from ruamel.yaml import YAML
import json
from pathlib import Path
import time
import shutil
from urllib.parse import urlparse
from colorama import Fore, Style, init as colorama_init

# Console color setup
colorama_init(autoreset=True)

# Global logging context
_CURRENT_ADDON = None
_CURRENT_INDENT = 0

class _ColorFormatter(logging.Formatter):
    def format(self, record):
        # Level-based color
        if record.levelno >= logging.ERROR:
            level_color = Fore.RED + "ERROR" + Style.RESET_ALL
        elif record.levelno >= logging.WARNING:
            level_color = Fore.YELLOW + "WARN" + Style.RESET_ALL
        elif record.levelno >= logging.INFO:
            level_color = Fore.CYAN + "INFO" + Style.RESET_ALL
        else:
            level_color = "DEBUG"

        # Add-on prefix and indentation
        addon = _CURRENT_ADDON or ""
        indent_spaces = "  " * max(0, _CURRENT_INDENT)
        addon_prefix = f"[{addon}] " if addon else ""
        # Compose message with indentation and addon
        original_msg = super().format(record)
        colored_msg = f"{indent_spaces}{addon_prefix}{original_msg}"
        # Final line with colored level
        return f"{level_color}: {colored_msg}"

def configure_colored_logging():
    """
    Configure root logger to use colored, contextual formatting.
    Safe to call multiple times.
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = _ColorFormatter("%(message)s")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(fmt)
        root.addHandler(handler)
    else:
        for h in root.handlers:
            h.setFormatter(fmt)

def set_log_context(addon: str, indent: int = 0):
    """
    Set current log context (addon name + indentation level).
    """
    global _CURRENT_ADDON, _CURRENT_INDENT
    _CURRENT_ADDON = addon
    _CURRENT_INDENT = indent

def clear_log_context():
    """
    Clear current log context.
    """
    set_log_context(None, 0)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class HelmChart:
    def __init__(self, addon_chart, addon_chart_version, addon_chart_repository, addon_chart_repository_namespace, addon_chart_release_name, latest=False):
        """
        Initializes the HelmChart with necessary chart details and AWS ECR client.

        Args:
            addon_chart (str): The name of the addon chart.
            addon_chart_version (str): The version of the addon chart.
            addon_chart_repository (str): The repository URL of the addon chart.
            addon_chart_repository_namespace (str): The namespace of the addon chart.
            addon_chart_release_name (str): The release name of the addon chart.
        """
        self.addon_chart = addon_chart
        self.addon_chart_version = addon_chart_version
        self.addon_chart_repository = addon_chart_repository
        self.addon_chart_repository_namespace = addon_chart_repository_namespace
        self.addon_chart_release_name = addon_chart_release_name
        self.public_addon_chart_images = []
        self.private_addon_chart_images = []
        self.failed_pull_addon_chart_images = []
        self.failed_push_addon_chart_images = []
        self.failed_push_addon_chart = None
        self.failed_commands = []
        self.private_ecr_url = None
        self.public_ecr_authenticated = False
        self.private_ecr_authenticated = False
        self.dockerhub_authenticated = False
        self.image_vulnerabilities = []
        # Optional path prefix under the target registry (e.g., "team/x")
        self.repository_prefix = ""
        # Captured dependency tree for logging/summary
        self.dependencies = None

        # Initialize boto3 session and clients once
        self.session = boto3.Session()
        self.region = self.session.region_name
        self.sts_client = self.session.client("sts")
        self.ecr_client = self.session.client("ecr", region_name=self.region)

    def run_command(self, command, error_message):
        """
        Executes a command using subprocess.run and handles errors appropriately.

        Args:
            command (list): The command to run as a list of strings.
            error_message (str): The error message to log in case of failure.

        Returns:
            str: The standard output from the command, or None if the command failed.
        """
        try:
            result = subprocess.run(command, capture_output=True, text=True)
        except FileNotFoundError as e:
            missing = command[0] if command else "unknown"
            logger.error(f"Missing dependency: '{missing}' not found on PATH while running: {command}. {error_message}")
            self.failed_commands.append((command, error_message, str(e)))
            return None
        if result.returncode != 0:
            stderr = result.stderr.lower()
            
            # Handle specific known errors
            if "no repo named" in stderr:
                logger.warning("Helm repository 'temp' does not exist. Skipping removal.")
            elif "repository not found" in stderr or "could not resolve host" in stderr:
                logger.warning(f"Remote repository not found or unable to resolve host: {result.stderr}")
                self.failed_commands.append((command, error_message, result.stderr))
            else:
                logger.warning(f"{error_message}: {result.stderr}")
                self.failed_commands.append((command, error_message, result.stderr))
            return None
        return result.stdout

    def _ensure_docker_sandbox(self):
        """
        Ensure a sandboxed DOCKER_CONFIG is present to avoid host credential helpers.
        """
        if getattr(self, "docker_config_dir", None):
            return
        base_dir = os.path.join(".docker-sandbox", self.addon_chart)
        os.makedirs(base_dir, exist_ok=True)
        config_path = os.path.join(base_dir, "config.json")
        # Minimal config to disable credsStore usage
        cfg = {"credsStore": ""}
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
            logger.info(f"Using sandboxed DOCKER_CONFIG at {base_dir}")
        except Exception as e:
            logger.warning(f"Unable to write sandbox docker config at {config_path}: {e}")
        self.docker_config_dir = base_dir

    def run_docker(self, command, error_message, input_text=None, timeout=120):
        """
        Run a docker command using the sandbox DOCKER_CONFIG.
        Optionally pass stdin (for --password-stdin) and a timeout to avoid hangs.
        """
        self._ensure_docker_sandbox()
        env = os.environ.copy()
        env["DOCKER_CONFIG"] = self.docker_config_dir
        try:
            result = subprocess.run(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout
            )
        except subprocess.TimeoutExpired as e:
            logger.error(f"Command timed out: {command}. {error_message}")
            self.failed_commands.append((command, error_message, f"timeout: {e}"))
            return None
        except FileNotFoundError as e:
            missing = command[0] if command else "unknown"
            logger.error(f"Missing dependency: '{missing}' not found on PATH while running: {command}. {error_message}")
            self.failed_commands.append((command, error_message, str(e)))
            return None
        if result.returncode != 0:
            logger.warning(f"{error_message}: {result.stderr}")
            self.failed_commands.append((command, error_message, result.stderr))
            return None
        return result.stdout

    def _is_dockerhub_image(self, image: str) -> bool:
        """
        Heuristic to detect Docker Hub images (explicit docker.io or implicit library/short refs).
        """
        try:
            first = image.split('/', 1)[0]
            # If an explicit registry is provided
            if '.' in first or ':' in first or first == 'localhost':
                return first in ('docker.io', 'index.docker.io', 'registry-1.docker.io')
            # No explicit registry => Docker Hub by default
            return True
        except Exception:
            return False

    def _login_dockerhub(self):
        """
        Log in to Docker Hub using provided username/token within the sandboxed DOCKER_CONFIG.
        """
        if getattr(self, "dockerhub_authenticated", False):
            return
        username = getattr(self, "dockerhub_username", "") or ""
        token = getattr(self, "dockerhub_token", "") or ""
        if not username or not token:
            logger.info("Docker Hub credentials not provided; proceeding unauthenticated")
            return
        logger.info("Logging into Docker Hub (sandboxed)")
        login_cmd = ["docker", "login", "--username", username, "--password-stdin", "docker.io"]
        self.run_docker(login_cmd, "Failed to docker login Docker Hub", input_text=token)
        self.dockerhub_authenticated = True
        logger.info("Logged into Docker Hub")

    def _ensure_helm_sandbox(self):
        """
        Ensure per-addon helm registry/repository sandbox paths exist and return them.
        """
        base = os.path.join(".helm-sandbox", self.addon_chart)
        reg = os.path.join(base, "registry.json")
        repo = os.path.join(base, "repositories.yaml")
        cache = os.path.join(base, "cache")
        cfg = os.path.join(base, "config")
        data = os.path.join(base, "data")
        os.makedirs(base, exist_ok=True)
        os.makedirs(cache, exist_ok=True)
        os.makedirs(cfg, exist_ok=True)
        os.makedirs(data, exist_ok=True)
        if not os.path.exists(reg):
            try:
                with open(reg, "w", encoding="utf-8") as f:
                    f.write("{}")
            except Exception as e:
                logger.warning(f"Unable to initialize helm registry config at {reg}: {e}")
        if not os.path.exists(repo):
            try:
                with open(repo, "w", encoding="utf-8") as f:
                    f.write("{}")
            except Exception as e:
                logger.warning(f"Unable to initialize helm repositories config at {repo}: {e}")
        return reg, repo, cache

    def run_helm(self, args, error_message, input_text=None, timeout=120, use_repo_flags=True):
        """
        Run a helm command using sandboxed registry/repository configs to avoid OS keyring issues.
        args should NOT include the 'helm' prefix, e.g., ['registry', 'login', ...] or ['show', 'chart', ...].
        """
        reg, repo, cache = self._ensure_helm_sandbox()
        cmd = ["helm"]
        if use_repo_flags:
            cmd += ["--registry-config", reg, "--repository-config", repo, "--repository-cache", cache]
        cmd += args
        try:
            env = os.environ.copy()
            # Enable OCI features for dependency update/build and registry operations
            env["HELM_EXPERIMENTAL_OCI"] = "1"
            result = subprocess.run(
                cmd,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env
            )
        except subprocess.TimeoutExpired as e:
            logger.error(f"Helm command timed out: {cmd}. {error_message}")
            self.failed_commands.append((cmd, error_message, f"timeout: {e}"))
            return None
        except FileNotFoundError as e:
            missing = cmd[0] if cmd else "unknown"
            logger.error(f"Missing dependency: '{missing}' not found on PATH while running: {cmd}. {error_message}")
            self.failed_commands.append((cmd, error_message, str(e)))
            return None
        if result.returncode != 0:
            logger.warning(f"{error_message}: {result.stderr}")
            self.failed_commands.append((cmd, error_message, result.stderr))
            return None
        return result.stdout

    def _docker_inspect_platform(self, image: str) -> str | None:
        """
        Inspect an image and return the OS/architecture string if available.
        """
        try:
            fmt = "{{.Os}}/{{.Architecture}}"
            out = self.run_docker(["docker", "inspect", "--format", fmt, image], f"Failed to inspect platform for {image}")
            if out is None:
                return None
            platform = (out or "").strip()
            return platform or None
        except Exception:
            return None

    def _get_local_image_id(self, ref: str) -> str | None:
        """
        Return the local image ID (sha256:...) for a given image reference.
        """
        try:
            out = self.run_docker(["docker", "inspect", "--format", "{{.Id}}", ref], f"Failed to get image ID for {ref}")
            if out is None:
                return None
            image_id = (out or "").strip()
            return image_id or None
        except Exception:
            return None

    def _get_repo_digests(self, ref: str) -> list[str] | None:
        """
        Return the list of repo digests (e.g., ['registry/repo@sha256:...']) for a given image reference.
        """
        try:
            out = self.run_docker(["docker", "inspect", "--format", "{{json .RepoDigests}}", ref], f"Failed to get repo digests for {ref}")
            if out is None:
                return None
            out = out.strip()
            if not out:
                return None
            # Expecting a JSON array; eval safely
            import json as _json
            digests = _json.loads(out)
            if isinstance(digests, list):
                return digests
            return None
        except Exception:
            return None

    def _docker_pull(self, image: str, platform: str | None = None) -> bool:
        """
        Pull an image, optionally specifying a platform.
        """
        cmd = ["docker", "pull"]
        if platform:
            cmd += ["--platform", platform]
        cmd += [image]
        logger.info(f"Pulling image via docker: {image} (platform={platform or 'none'})")
        res = self.run_docker(cmd, f"Failed to pull image {image} with platform={platform or 'none'}")
        return res is not None

    def _pull_image_with_platform_logic(self, image: str) -> bool:
        """
        Pull image using configured platform preference.
        platform=auto: try (none) -> linux/amd64 -> linux/arm64, logging chosen platform if found.
        platform=linux/amd64|linux/arm64: pull explicitly with that platform.
        """
        pref = getattr(self, "platform", "auto")
        sequences = [None, "linux/amd64", "linux/arm64"] if pref == "auto" else [pref]
        for plat in sequences:
            ok = self._docker_pull(image, plat)
            if ok:
                effective = self._docker_inspect_platform(image)
                if effective:
                    logger.info(f"Pulled {image} (platform={effective})")
                    return True
                else:
                    logger.warning(f"Image {image} pulled but platform metadata not available; retrying with next platform")
                    # continue to next platform
                    continue
        return False

    def _recover_push_no_platform(self, public_repo: str, private_image: str) -> bool:
        """
        Recover from 'does not provide any platform' push failures by re-pulling the source
        image with an explicit platform and retrying the tag/push. Defaults to linux/amd64
        when platform preference is 'auto'.
        """
        pref = getattr(self, "platform", "auto")
        plat = "linux/amd64" if pref == "auto" else pref
        logger.warning(f"Push failed due to missing platform; defaulting to {plat} for {public_repo}")
        # Best-effort cleanup of local references that might point at manifest lists
        try:
            self.run_docker(["docker", "image", "rm", "-f", private_image], f"Cleanup local tag {private_image}")
        except Exception:
            pass
        try:
            self.run_docker(["docker", "image", "rm", "-f", public_repo], f"Cleanup local source {public_repo}")
        except Exception:
            pass
        # Pull with explicit platform, re-tag, and retry push
        if not self._docker_pull(public_repo, plat):
            return False
        # Prefer digest-based retagging to avoid manifest-list ambiguity
        digests = self._get_repo_digests(public_repo) or []
        digest_ref = None
        for d in digests:
            # Expect strings like 'public.ecr.aws/...@sha256:abc'
            if "@sha256:" in d:
                digest_ref = d
                break
        if digest_ref:
            logger.info(f"Retagging from {digest_ref} -> {private_image}")
            tag_ok = self.run_docker(["docker", "tag", digest_ref, private_image], f"Failed to tag {digest_ref} -> {private_image}") is not None
            if not tag_ok:
                return False
        else:
            image_id = self._get_local_image_id(public_repo)
            if not image_id:
                return False
            logger.info(f"Retagging from {image_id} -> {private_image}")
            tag_ok = self.run_docker(["docker", "tag", image_id, private_image], f"Failed to tag {image_id} -> {private_image}") is not None
            if not tag_ok:
                return False
        push_ok = self.run_docker(["docker", "push", private_image], f"Failed to push after explicit platform {plat}: {private_image}") is not None
        if push_ok:
            return True
        # Save/Load fallback to force a single-arch local artifact
        try:
            tar_path = os.path.join(self.docker_config_dir or ".docker-sandbox", self.addon_chart, "tmp-image.tar")
            os.makedirs(os.path.dirname(tar_path), exist_ok=True)
            src_ref = digest_ref if digest_ref else (image_id or public_repo)
            logger.warning(f"Push still failing; attempting save/load fallback using {src_ref}")
            # Best-effort cleanup of the private tag
            try:
                self.run_docker(["docker", "image", "rm", "-f", private_image], f"Cleanup local tag {private_image}")
            except Exception:
                pass
            # Save and reload the image
            if self.run_docker(["docker", "save", "-o", tar_path, src_ref], f"Failed to save image {src_ref}") is None:
                return False
            if self.run_docker(["docker", "load", "-i", tar_path], f"Failed to load image from tar {tar_path}") is None:
                return False
            # Tag from digest or loaded ID again
            loaded_id = self._get_local_image_id(src_ref) or image_id
            if loaded_id:
                logger.info(f"Retagging (post-load) from {loaded_id} -> {private_image}")
                if self.run_docker(["docker", "tag", loaded_id, private_image], f"Failed to tag {loaded_id} -> {private_image}") is None:
                    return False
            elif digest_ref:
                logger.info(f"Retagging (post-load) from {digest_ref} -> {private_image}")
                if self.run_docker(["docker", "tag", digest_ref, private_image], f"Failed to tag {digest_ref} -> {private_image}") is None:
                    return False
            else:
                return False
            # Retry push
            push_ok2 = self.run_docker(["docker", "push", private_image], f"Failed to push after save/load fallback: {private_image}") is not None
            return push_ok2
        finally:
            try:
                if os.path.exists(tar_path):
                    os.remove(tar_path)
            except Exception:
                pass

    def _normalize_image_host(self, image: str) -> str:
        """
        Normalize known public ECR host name typos to the correct hostname.
        """
        try:
            if image.startswith("ecr-public.aws.com") or "ecr-public.aws.com/" in image:
                return image.replace("ecr-public.aws.com", "public.ecr.aws")
        except Exception:
            pass
        return image
    
    def get_aws_account_id_and_region(self):
        """
        Retrieves AWS account ID and region using STS client.

        Returns:
            tuple: AWS account ID and region.

        Raises:
            Exception: If unable to get caller identity.
        """
        try:
            identity = self.sts_client.get_caller_identity()
            account_id = identity.get("Account")
            return account_id, self.region
        except ClientError as e:
            logger.error(f"Unable to get caller identity: {e}")
            raise Exception(f"Unable to get caller identity: {e}")
        
    def get_private_ecr_url(self):
        """
        Constructs the private ECR URL using AWS account ID and region.
        """
        aws_account_id, region = self.get_aws_account_id_and_region()
        self.private_ecr_url = f"{aws_account_id}.dkr.ecr.{region}.amazonaws.com"

    def authenticate_ecr(self, is_public=False):
        """
        Authenticates Docker to an Amazon ECR registry.

        Args:
            is_public (bool): If True, authenticate to a public ECR. Otherwise, authenticate to a private ECR.

        Raises:
            subprocess.CalledProcessError: If Docker login fails.
        """
        try:
            if is_public:
                if not self.public_ecr_authenticated:
                    self._logout_ecr("public.ecr.aws")
                    self._login_ecr_public()
                    self.public_ecr_authenticated = True
            else:
                if not self.private_ecr_authenticated:
                    self._logout_ecr(self.private_ecr_url)
                    self._login_ecr_private()
                    self.private_ecr_authenticated = True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to authenticate with Amazon ECR: {e}")
            raise e

    def _logout_ecr(self, ecr_url):
        """
        Logs out from a specified ECR registry.

        Args:
            ecr_url (str): The URL of the ECR registry to log out from.
        """
        self.run_docker(["docker", "logout", ecr_url], f"Failed to docker logout {ecr_url}")

    def _login_ecr_public(self):
        """
        Logs in to the public ECR registry using an override password if provided,
        otherwise falls back to AWS CLI get-login-password.
        """
        override = getattr(self, "public_ecr_password", "")
        if override:
            login_cmd = ["docker", "login", "--username", "AWS", "--password-stdin", "public.ecr.aws"]
            logger.info("Logging into public ECR (sandboxed) with provided token")
            self.run_docker(login_cmd, "Failed to docker login public.ecr.aws with override", input_text=override)
            logger.info("Logged into public ECR")
        else:
            auth_cmd = ["aws", "ecr-public", "get-login-password", "--region", "us-east-1"]
            auth_output = subprocess.run(auth_cmd, stdout=subprocess.PIPE, check=True)
            auth_password = auth_output.stdout.decode().strip()
            login_cmd = ["docker", "login", "--username", "AWS", "--password-stdin", "public.ecr.aws"]
            # Use sandboxed docker
            logger.info("Logging into public ECR (sandboxed) with AWS token")
            self.run_docker(login_cmd, "Failed to docker login public.ecr.aws", input_text=auth_password)
            logger.info("Logged into public ECR")

    def _login_ecr_private(self):
        """
        Logs in to the private ECR registry using an override password if provided,
        otherwise falls back to AWS CLI get-login-password.
        """
        override = getattr(self, "private_ecr_password", "")
        if override:
            login_cmd = ["docker", "login", "--username", "AWS", "--password-stdin", self.private_ecr_url]
            logger.info(f"Logging into private ECR (sandboxed): {self.private_ecr_url}")
            self.run_docker(login_cmd, f"Failed to docker login {self.private_ecr_url} with override", input_text=override)
            logger.info("Logged into private ECR")
        else:
            auth_cmd = ["aws", "ecr", "get-login-password", "--region", self.region]
            auth_output = subprocess.run(auth_cmd, stdout=subprocess.PIPE, check=True)
            auth_password = auth_output.stdout.decode().strip()
            login_cmd = ["docker", "login", "--username", "AWS", "--password-stdin", self.private_ecr_url]
            logger.info(f"Logging into private ECR (sandboxed) with AWS token: {self.private_ecr_url}")
            self.run_docker(login_cmd, f"Failed to docker login {self.private_ecr_url}", input_text=auth_password)
            logger.info("Logged into private ECR")

    def _login_ecr_public_chart(self):
        """
        Logs in to the public ECR registry for Helm using an override password if provided,
        otherwise falls back to AWS CLI get-login-password.
        """
        override = getattr(self, "public_ecr_password", "")
        if override:
            login_args = ["registry", "login", "--username", "AWS", "--password-stdin", "public.ecr.aws"]
            logger.info("Helm registry login to public ECR (sandboxed) with provided token")
            self.run_helm(login_args, "Failed helm registry login to public.ecr.aws with override", input_text=override, use_repo_flags=True)
            logger.info("Helm logged into public ECR")
        else:
            auth_cmd = ["aws", "ecr-public", "get-login-password", "--region", "us-east-1"]
            auth_output = subprocess.run(auth_cmd, stdout=subprocess.PIPE, check=True)
            auth_password = auth_output.stdout.decode().strip()
            login_args = ["registry", "login", "--username", "AWS", "--password-stdin", "public.ecr.aws"]
            logger.info("Helm registry login to public ECR (sandboxed) with AWS token")
            self.run_helm(login_args, "Failed helm registry login to public.ecr.aws", input_text=auth_password, use_repo_flags=True)
            logger.info("Helm logged into public ECR")

    def _login_ecr_private_chart(self):
        """
        Logs in to the private ECR registry for Helm using an override password if provided,
        otherwise falls back to AWS CLI get-login-password.
        """
        override = getattr(self, "private_ecr_password", "")
        if override:
            login_args = ["registry", "login", "--username", "AWS", "--password-stdin", self.private_ecr_url]
            logger.info(f"Helm registry login to private ECR (sandboxed): {self.private_ecr_url}")
            self.run_helm(login_args, f"Failed helm registry login to {self.private_ecr_url} with override", input_text=override, use_repo_flags=True)
            logger.info("Helm logged into private ECR")
        else:
            auth_cmd = ["aws", "ecr", "get-login-password", "--region", self.region]
            auth_output = subprocess.run(auth_cmd, stdout=subprocess.PIPE, check=True)
            auth_password = auth_output.stdout.decode().strip()
            login_args = ["registry", "login", "--username", "AWS", "--password-stdin", self.private_ecr_url]
            logger.info(f"Helm registry login to private ECR (sandboxed) with AWS token: {self.private_ecr_url}")
            self.run_helm(login_args, f"Failed helm registry login to {self.private_ecr_url}", input_text=auth_password, use_repo_flags=True)
            logger.info("Helm logged into private ECR")

    def _is_oci_repository(self):
        """
        Determine if the chart repository should be treated as an OCI registry.
        """
        repo = self.addon_chart_repository or ""
        return repo.startswith("oci://") or "public.ecr.aws" in repo or "ghcr.io" in repo or bool(self.addon_chart_repository_namespace)

    def _build_oci_chart_ref(self):
        """
        Construct an OCI chart reference for helm show/pull, e.g.:
        oci://public.ecr.aws/karpenter/karpenter
        oci://ghcr.io/grafana/helm-charts/grafana-operator
        """
        repo = self.addon_chart_repository or ""
        if repo.startswith("oci://"):
            repo = repo[len("oci://"):]  # strip scheme, helm expects full oci:// when pulling
        ns = (self.addon_chart_repository_namespace or "").strip("/")
        if ns:
            return f"oci://{repo}/{ns}/{self.addon_chart}"
        return f"oci://{repo}/{self.addon_chart}"

    def _derive_repo_name(self, url: str) -> str:
        """
        Derive a stable helm repo name from a URL host/path.
        """
        try:
            parsed = urlparse(url)
            host = (parsed.netloc or "").replace(".", "-")
            path = (parsed.path or "").strip("/").split("/")
            suffix = path[-1] if path and path[-1] else "charts"
            base = f"{host}-{suffix}".lower()
            safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in base)
            safe = safe.strip("-")
            return safe or "repo"
        except Exception:
            return "repo"

    def _ensure_helm_repos(self, chart_root: str):
        """
        Ensure that all http(s) Chart.yaml dependency repositories are added to helm,
        and update the repo cache if any were added.
        """
        declared = self._collect_declared_dependencies(chart_root)
        urls = []
        for dep in declared:
            repo = (dep.get("repository") or "").strip()
            if repo.startswith("http://") or repo.startswith("https://"):
                if repo not in urls:
                    urls.append(repo)

        added_any = False
        for url in urls:
            name = self._derive_repo_name(url)
            logger.info(f"Ensuring helm repo '{name}' -> {url}")
            cmd_add = ["helm", "repo", "add", name, url]
            # It's fine if this fails due to 'already exists'; run_command will log it.
            self.run_command(cmd_add, f"Failed to add helm repo {url}")
            # Also add via sandboxed helm to maintain consistency when OCI is used
            self.run_helm(["repo", "add", name, url], f"Failed to add helm repo (sandboxed) {url}", use_repo_flags=True)
            added_any = True

        if added_any:
            logger.info("Updating helm repo cache...")
            # Update both standard and sandboxed caches (harmless if one is unused)
            self.run_command(["helm", "repo", "update"], "Failed to update helm repo cache")
            self.run_helm(["repo", "update"], "Failed to update helm repo cache (sandboxed)", use_repo_flags=True)

    def get_remote_version(self, pull_latest=False):
        """
        Compares the desired chart version with the latest available version.
        Adds fallback-to-latest if a requested version is unavailable.
        Returns:
            str: The version that should be used (latest or specified), or None if unavailable.
        """
        yaml = YAML()

        # Build helm show chart command depending on repo type
        use_oci = self._is_oci_repository()
        if use_oci:
            chart_ref = self._build_oci_chart_ref()
            logger.info(f"Fetching version for {self.addon_chart} from OCI registry: {chart_ref}")
            # Only login for public ECR; ghcr.io usually doesn't require login to pull public charts
            if "public.ecr.aws" in self.addon_chart_repository:
                self._login_ecr_public_chart()
            cmd_show_chart = ["helm", "show", "chart", chart_ref] if pull_latest else ["helm", "show", "chart", chart_ref, "--version", self.addon_chart_version]
        else:
            logger.info(f"Fetching version for {self.addon_chart} from standard Helm repository: {self.addon_chart_repository}")
            cmd_show_chart = ["helm", "show", "chart", self.addon_chart, "--repo", self.addon_chart_repository] if pull_latest else ["helm", "show", "chart", self.addon_chart, "--repo", self.addon_chart_repository, "--version", self.addon_chart_version]

        try:
            # Use sandboxed helm when operating against OCI/public ECR
            if use_oci:
                args_show = cmd_show_chart[1:]  # drop 'helm'
                result = self.run_helm(args_show, "Failed to fetch chart details")
            else:
                result = self.run_command(cmd_show_chart, "Failed to fetch chart details")
            if result is None:
                raise Exception("helm show returned no data")
            chart_info = yaml.load(result)
            version = chart_info.get('version')

            if pull_latest:
                logger.info(f"The latest version of {self.addon_chart} is {version}")
                if version and version != self.addon_chart_version:
                    logger.info(f"New version {version} available for {self.addon_chart}, updating...")
                    self.addon_chart_version = version
                return version

            # Not pulling latest: ensure requested version is available; else fallback to latest
            if version == self.addon_chart_version:
                logger.info(f"The specified version {self.addon_chart_version} of {self.addon_chart} is available.")
                return version

            logger.warning(f"Requested version {self.addon_chart_version} for {self.addon_chart} is unavailable or mismatched; attempting to fetch latest.")
            # Fetch latest without --version
            if use_oci:
                if "public.ecr.aws" in self.addon_chart_repository:
                    self._login_ecr_public_chart()
                cmd_latest = ["helm", "show", "chart", chart_ref]
            else:
                cmd_latest = ["helm", "show", "chart", self.addon_chart, "--repo", self.addon_chart_repository]

            if use_oci:
                args_latest = cmd_latest[1:]
                result_latest = self.run_helm(args_latest, "Failed to fetch latest chart details")
            else:
                result_latest = self.run_command(cmd_latest, "Failed to fetch latest chart details")
            if result_latest is None:
                return None
            chart_info_latest = yaml.load(result_latest)
            version_latest = chart_info_latest.get('version')
            if version_latest:
                logger.info(f"Falling back to latest version {version_latest} for {self.addon_chart}")
                self.addon_chart_version = version_latest
            return version_latest
        except Exception as e:
            logger.error(f"Failed to fetch chart details: {e}")
            # Ensure cmd_show_chart is defined for logging
            try:
                self.failed_commands.append((cmd_show_chart, "Failed to fetch chart details", str(e)))
            except Exception:
                pass
            return None



    def download_chart(self, destination_folder, version=None):
        """
        Downloads and extracts the Helm chart to the specified destination folder.

        Args:
            destination_folder (str): The folder to download the chart to.
            version (str): The version of the chart to download. Defaults to the chart's version.

        Returns:
            str: The path to the downloaded chart file.

        Raises:
            Exception: If the chart file is not found after download.
        """
        logger.info(f"Downloading and extracting chart {self.addon_chart} version {version if version else self.addon_chart_version}")
        if not version:
            version = self.addon_chart_version
        chart_dir = os.path.join(destination_folder, self.addon_chart)
        os.makedirs(chart_dir, exist_ok=True)

        chart_file = os.path.join(chart_dir, f"{self.addon_chart}-{version}.tgz")
        if os.path.exists(chart_file):
            logger.info(f"Chart file already exists at: {chart_file}")
            with tarfile.open(chart_file, 'r:gz') as tar:
                tar.extractall(path=f"{chart_dir}")
            return chart_file

        use_oci = self._is_oci_repository()
        if use_oci:
            # Only login for public ECR
            if "public.ecr.aws" in self.addon_chart_repository:
                self._login_ecr_public_chart()
            chart_ref = self._build_oci_chart_ref()
            cmd_pull_chart = ["helm", "pull", chart_ref, "--version", version, "--destination", chart_dir]
            args_pull = cmd_pull_chart[1:]
            self.run_helm(args_pull, "Failed to pull chart from OCI registry")
        else:
            cmd_pull_chart = ["helm", "pull", self.addon_chart, "--repo", self.addon_chart_repository, "--version", version, "--destination", chart_dir]
            self.run_command(cmd_pull_chart, "Failed to pull chart")

        if not os.path.exists(chart_file):
            raise Exception(f"Chart file {chart_file} not found after download")

        with tarfile.open(chart_file, 'r:gz') as tar:
            tar.extractall(path=f"{chart_dir}")

        return chart_file
    
    def _read_chart_yaml(self, chart_root):
        yaml = YAML()
        chart_yaml_path = os.path.join(chart_root, "Chart.yaml")
        try:
            with open(chart_yaml_path, "r", encoding="utf-8") as f:
                return yaml.load(f) or {}
        except Exception as e:
            logger.warning(f"Unable to read Chart.yaml at {chart_yaml_path}: {e}")
            return None

    def _collect_declared_dependencies(self, chart_root):
        meta = self._read_chart_yaml(chart_root) or {}
        deps = meta.get("dependencies") or []
        result = []
        for dep in deps:
            if isinstance(dep, dict):
                result.append({
                    "name": dep.get("name"),
                    "repository": dep.get("repository") or dep.get("repo"),
                    "version": dep.get("version"),
                    "alias": dep.get("alias") or "",
                    "condition": dep.get("condition") or "",
                })
        return result

    def _collect_vendored_tree(self, chart_root):
        node = {"name": None, "version": None, "repository": "vendored", "children": []}
        meta = self._read_chart_yaml(chart_root) or {}
        node["name"] = meta.get("name") or os.path.basename(chart_root)
        node["version"] = meta.get("version")
        charts_dir = os.path.join(chart_root, "charts")
        if os.path.isdir(charts_dir):
            for entry in os.listdir(charts_dir):
                sub = os.path.join(charts_dir, entry)
                if os.path.isdir(sub):
                    node["children"].append(self._collect_vendored_tree(sub))
        return node

    def log_chart_dependencies(self, chart_root):
        try:
            declared = self._collect_declared_dependencies(chart_root)
            vendored_tree = self._collect_vendored_tree(chart_root)
            self.dependencies = vendored_tree

            header_repo = self.addon_chart_repository
            header_version = self.addon_chart_version
            logger.info(f"Dependency graph for {self.addon_chart} (repo={header_repo}, version={header_version})")

            # Log declared dependencies (from Chart.yaml)
            for dep in declared:
                alias_txt = f", alias={dep['alias']}" if dep.get("alias") else ""
                logger.info(f"  {self.addon_chart} -> {dep.get('name')} (repo={dep.get('repository')}, version={dep.get('version')}{alias_txt})")

            # Log vendored tree edges (charts/ directory)
            def _log_tree(parent_name, node, depth=0):
                pad = "  " * depth
                for child in node.get("children", []):
                    logger.info(f"{pad}  {parent_name} -> {child.get('name')} (vendored, version={child.get('version')})")
                    _log_tree(child.get('name'), child, depth + 1)

            if vendored_tree and vendored_tree.get("children"):
                _log_tree(vendored_tree.get("name"), vendored_tree, 0)
        except Exception as e:
            logger.warning(f"Failed to log dependency graph for {self.addon_chart}: {e}")

    def get_chart_images(self, chart, exclude_dependencies=False):
        """
        Extracts images from the Helm chart templates.

        Args:
            chart (str): Path to the chart archive (.tgz) or chart directory.
            exclude_dependencies (bool): If True, do not render vendored subcharts (charts/).

        Raises:
            Exception: If extracting images fails.
        """
        logger.info(f"Getting images for {self.addon_chart} (exclude_dependencies={exclude_dependencies})")
        renamed_charts_dir = None
        chart_root = os.path.join(os.path.dirname(chart), self.addon_chart)
        template_target = Path(chart_root) if os.path.isdir(chart_root) else Path(chart)

        try:
            set_args = []

            # Include dependencies: build them if missing and enable conditional deps
            if not exclude_dependencies and os.path.isdir(chart_root):
                # Ensure required helm repos are added for dependencies
                self._ensure_helm_repos(chart_root)
                # Build dependencies (vendors subcharts referenced in Chart.yaml)
                # Pre-login if any dependency is OCI on public.ecr.aws
                decls_for_login = self._collect_declared_dependencies(chart_root)
                if any(((d.get("repository") or "").startswith("oci://") and "public.ecr.aws" in (d.get("repository") or "")) for d in decls_for_login):
                    try:
                        logger.info("Logging into public ECR for OCI dependencies")
                        self._login_ecr_public_chart()
                    except Exception as e:
                        logger.warning(f"Helm registry login to public ECR for dependencies failed: {e}")
                # Log any OCI dependency hosts for visibility
                oci_hosts = [(d.get("repository") or "") for d in decls_for_login if (d.get("repository") or "").startswith("oci://")]
                if oci_hosts:
                    logger.info(f"Detected OCI dependencies: {', '.join(oci_hosts)}")
                # Use sandboxed helm with OCI enabled for dependency operations
                dep_build_out = self.run_helm(["dependency", "build", chart_root], "Failed to build chart dependencies")
                if dep_build_out is None:
                    logger.warning("Dependency build failed; skipping vendored subcharts and relying on --dependency-update during template")
                # Enable any conditional dependencies explicitly
                declared = self._collect_declared_dependencies(chart_root)
                for dep in declared:
                    cond = (dep.get("condition") or "").strip()
                    if cond:
                        set_args += ["--set", f"{cond}=true"]

                template_target = Path(chart_root)

            # Exclude vendored subcharts by temporarily moving charts/ away
            if exclude_dependencies and os.path.isdir(chart_root):
                charts_dir = os.path.join(chart_root, "charts")
                if os.path.isdir(charts_dir):
                    renamed_charts_dir = charts_dir + ".skipped"
                    try:
                        os.rename(charts_dir, renamed_charts_dir)
                        logger.info(f"Temporarily excluding vendored subcharts at {charts_dir}")
                    except Exception as e:
                        logger.warning(f"Unable to temporarily exclude subcharts at {charts_dir}: {e}")
                template_target = Path(chart_root)

            # Pre-inject minimal overrides for known charts to satisfy required values during template
            if self.addon_chart == "aws-load-balancer-controller":
                set_args += ["--set-string", "clusterName=placeholder"]
            if self.addon_chart == "karpenter":
                set_args += ["--set-string", "settings.clusterName=placeholder", "--set-string", "settings.clusterEndpoint=https://placeholder"]

            # Run helm template using sandboxed helm and enable dependency update to cope with stale locks
            cmd_get_images = ["helm", "template", "--dependency-update", str(template_target)] + set_args
            args_template = cmd_get_images[1:]
            helm_output = self.run_helm(args_template, "Failed to get images")

            # If template failed due to required values, retry with minimal per-chart overrides
            if helm_output is None:
                override_flags = []
                if self.addon_chart == "karpenter":
                    override_flags += ["--set-string", "settings.clusterName=placeholder", "--set-string", "settings.clusterEndpoint=https://placeholder"]
                if self.addon_chart == "aws-load-balancer-controller":
                    override_flags += ["--set-string", "clusterName=placeholder"]
                if override_flags:
                    logger.info(f"Retrying helm template with minimal overrides for {self.addon_chart}")
                    cmd_get_images_override = ["helm", "template", "--dependency-update", str(template_target)] + set_args + override_flags
                    args_template_override = cmd_get_images_override[1:]
                    helm_output = self.run_helm(args_template_override, "Failed to get images with overrides")
            
            if not helm_output:
                raise Exception(f"Helm template produced no output for {self.addon_chart}; cannot extract images")

            cmd_extract_images = ["yq", "..|.image? | select(.)"]
            process = subprocess.Popen(cmd_extract_images, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = process.communicate(input=helm_output)
            unique_images = {image.split('@')[0] if '@' in image else image for image in stdout.splitlines() if image and image != '---'}
            # Normalize hosts and dedupe again after normalization
            normalized_images = list({ self._normalize_image_host(img) for img in unique_images })
            # Authenticate to Docker Hub up-front if any Docker Hub images are present and creds provided
            if any(self._is_dockerhub_image(img) for img in normalized_images) and getattr(self, "dockerhub_username", "") and getattr(self, "dockerhub_token", ""):
                try:
                    self._login_dockerhub()
                except Exception as e:
                    logger.warning(f"Docker Hub authentication attempt failed; will continue unauthenticated. Details: {e}")
            # Always authenticate to public ECR up-front if any public ECR images are present
            if any("public.ecr.aws" in img for img in normalized_images):
                logger.info("Public ECR images detected; authenticating to public ECR")
                try:
                    self.authenticate_ecr(is_public=True)
                except Exception as e:
                    logger.warning(f"Public ECR authentication attempt failed; will continue with retries. Details: {e}")
            for image in normalized_images:
                cmd_docker_manifest = ["docker", "manifest", "inspect", image]
                # First try manifest inspect without login (public or not)
                ok = self.run_docker(cmd_docker_manifest, f"Manifest inspect failed for {image}") is not None
                if not ok and "public.ecr.aws" in image:
                    # Fallback: authenticate to public ECR then retry
                    logger.info("Authenticating to public ECR (fallback)")
                    try:
                        self.authenticate_ecr(is_public=True)
                        ok = self.run_docker(cmd_docker_manifest, f"Manifest inspect failed for {image}") is not None
                    except Exception as e:
                        ok = False
                if ok:
                    self.public_addon_chart_images.append(image)
                else:
                    logger.warning(f"Skipping image {image} due to failure in manifest inspection")
                    self.failed_pull_addon_chart_images.append(image)
            logger.info(f"Extracted images: {self.public_addon_chart_images}")
        finally:
            # Restore vendored subcharts directory if it was renamed
            if renamed_charts_dir and os.path.exists(renamed_charts_dir):
                try:
                    os.rename(renamed_charts_dir, os.path.join(chart_root, "charts"))
                except Exception as e:
                    logger.warning(f"Unable to restore vendored subcharts: {e}")

    def pulling_chart_images(self, retry_count=3, retry_delay=5):
        """
        Pulls Docker images for the chart with retry logic.

        Args:
            retry_count (int): Number of times to retry pulling the images on failure.
            retry_delay (int): Delay between retry attempts in seconds.
        """
        logger.info(f"Pulling images {self.public_addon_chart_images} for chart {self.addon_chart}")
        for image in self.public_addon_chart_images:
            # Normalize any known host typos before pulling
            image = self._normalize_image_host(image)
            for attempt in range(retry_count):
                try:
                    # Authenticate to Docker Hub if applicable (improves rate limits/private pulls)
                    if self._is_dockerhub_image(image) and getattr(self, "dockerhub_username", "") and getattr(self, "dockerhub_token", ""):
                        self._login_dockerhub()
                    if "public.ecr.aws" in image:
                        logger.info(f"Pulling public ECR image {image}")
                        self.authenticate_ecr(is_public=True)
                    else:
                        logger.info(f"Pulling image {image}")
                        self.authenticate_ecr(is_public=False)

                    ok = self._pull_image_with_platform_logic(image)
                    if ok:
                        break
                    else:
                        raise RuntimeError(f"Pull failed for {image} with platform preference {getattr(self, 'platform', 'auto')}")
                except Exception as e:
                    logger.error(f"Attempt {attempt + 1} failed to pull image {image}: {e}")
                    if attempt + 1 < retry_count:
                        logger.info(f"Retrying in {retry_delay} seconds...")
                        time.sleep(retry_delay)
                    else:
                        logger.error(f"Maximum attempts reached for pulling image {image}.")
                        self.failed_pull_addon_chart_images.append(image)


    def push_images_to_ecr(self, retry_count=3, retry_delay=5):
        """
        Pushes Docker images to the private ECR repository with retry logic.

        Args:
            retry_count (int): Number of times to retry pushing the images on failure.
            retry_delay (int): Delay between retry attempts in seconds.
        """
        for public_repo in self.public_addon_chart_images:
            image_name = public_repo.rsplit('/', 1)[-1]
            repo_path = f"{self.repository_prefix}/{self.addon_chart}" if getattr(self, "repository_prefix", "") else self.addon_chart
            image_with_repo_path = f"{repo_path}/{image_name}"
            private_image = f"{self.private_ecr_url}/{image_with_repo_path}"
            self.private_addon_chart_images.append(private_image)
            # Ensure source image has a concrete platform before tagging/pushing
            effective = self._docker_inspect_platform(public_repo)
            if not effective:
                logger.warning(f"Source image {public_repo} lacks platform; re-pulling with platform logic")
                if not self._pull_image_with_platform_logic(public_repo):
                    logger.error(f"Unable to obtain platform-specific image for {public_repo}; skipping push")
                    self.failed_push_addon_chart_images.append(private_image)
                    continue
                effective = self._docker_inspect_platform(public_repo)
                if not effective:
                    logger.error(f"Platform still missing for {public_repo} after re-pull; skipping")
                    self.failed_push_addon_chart_images.append(private_image)
                    continue
            try:
                logger.info(f"Tagging Image for Private ECR (platform={effective})")
                docker_tag = ["docker", "tag", public_repo, private_image]
                self.run_docker(docker_tag, f"Failed to tag image {public_repo} to {private_image}")
                # Verify the private tag resolves to a concrete platform; if not, normalize now
                priv_effective = self._docker_inspect_platform(private_image)
                if not priv_effective:
                    pref = getattr(self, "platform", "auto")
                    plat = "linux/amd64" if pref == "auto" else pref
                    logger.warning(f"Private tag {private_image} lacks platform; re-pulling {public_repo} with {plat} and re-tagging")
                    # Best-effort cleanup to avoid cached manifest lists
                    try:
                        self.run_docker(["docker", "image", "rm", "-f", private_image], f"Cleanup local tag {private_image}")
                    except Exception:
                        pass
                    try:
                        self.run_docker(["docker", "image", "rm", "-f", public_repo], f"Cleanup local source {public_repo}")
                    except Exception:
                        pass
                    if self._docker_pull(public_repo, plat):
                        # Prefer digest-based retagging
                        digests = self._get_repo_digests(public_repo) or []
                        digest_ref = None
                        for d in digests:
                            if "@sha256:" in d:
                                digest_ref = d
                                break
                        if digest_ref:
                            logger.info(f"Retagging from {digest_ref} -> {private_image}")
                            self.run_docker(["docker", "tag", digest_ref, private_image], f"Failed to tag {digest_ref} -> {private_image}")
                        else:
                            image_id = self._get_local_image_id(public_repo)
                            if image_id:
                                logger.info(f"Retagging from {image_id} -> {private_image}")
                                self.run_docker(["docker", "tag", image_id, private_image], f"Failed to tag {image_id} -> {private_image}")
                            else:
                                logger.warning(f"Unable to resolve digest or image ID for {public_repo} after explicit pull; push may still fail")
                    else:
                        logger.warning(f"Re-pull with explicit platform {plat} failed for {public_repo}; push may still fail")
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to tag image {public_repo} to {private_image}: {e}")
                raise e

            # Diagnostics before push
            pub_plat = self._docker_inspect_platform(public_repo)
            priv_plat = self._docker_inspect_platform(private_image)
            pub_digests = self._get_repo_digests(public_repo) or []
            logger.info(f"Pre-push inspect: src={public_repo} platform={pub_plat}, digests={pub_digests} -> dst={private_image} platform={priv_plat}")
            try:
                ecr_repo = image_with_repo_path.split(':')[0]
                self.ecr_client.describe_repositories(repositoryNames=[ecr_repo])
                logger.info(f"ECR repository {ecr_repo} exists.")
            except ClientError as e:
                if e.response['Error']['Code'] == 'RepositoryNotFoundException':
                    logger.info(f"Repository {ecr_repo} not found, creating new repository...")
                    try:
                        self.ecr_client.create_repository(repositoryName=ecr_repo, tags=[{"Key": "chart-syncer", "Value": "true"}])
                    except ClientError as create_err:
                        logger.error(f"Unable to create ECR repository: {create_err}")
                else:
                    logger.error(f"Error describing ECR repositories: {e}")

            self.authenticate_ecr(is_public=False)
            logger.info(f"Pushing image to private ECR: {private_image}")
            attempted_recovery = False
            for attempt in range(retry_count):
                try:
                    push_command = ["docker", "push", private_image]
                    result = self.run_docker(push_command, f"Failed to push image {public_repo} to {private_image}")
                    if result is not None:
                        logger.info(f"Successfully pushed {public_repo} to {private_image}.")
                        break
                    else:
                        # First, attempt a one-time explicit-platform recovery regardless of current inspect results.
                        if not attempted_recovery:
                            logger.warning(f"Push failed for {private_image}; attempting explicit platform recovery")
                            if self._recover_push_no_platform(public_repo, private_image):
                                logger.info(f"Successfully pushed {public_repo} to {private_image} after platform recovery.")
                                break
                            attempted_recovery = True
                        # If push failed, check if either source or private tag lacks a concrete platform and recover.
                        eff_pub = self._docker_inspect_platform(public_repo)
                        eff_priv = self._docker_inspect_platform(private_image)
                        if not eff_pub or not eff_priv:
                            # Attempt recovery: default to linux/amd64 (or explicit platform) and retry once
                            if self._recover_push_no_platform(public_repo, private_image):
                                logger.info(f"Successfully pushed {public_repo} to {private_image} after platform recovery.")
                                break
                        logger.warning(f"Attempt {attempt + 1} failed to push image {public_repo} to {private_image}")
                        if attempt + 1 < retry_count:
                            logger.info(f"Retrying in {retry_delay} seconds...")
                            time.sleep(retry_delay)
                        else:
                            # Final fallback: remove remote tag and retry once with explicit-platform recovery
                            try:
                                # Derive repo name and tag for ECR delete
                                repo_no_tag = image_with_repo_path.split(":")[0]
                                image_tag = image_name.split(":")[1] if ":" in image_name else None
                                if image_tag:
                                    logger.warning(f"Deleting remote tag {repo_no_tag}:{image_tag} from ECR before final retry")
                                    self.ecr_client.batch_delete_image(repositoryName=repo_no_tag, imageIds=[{"imageTag": image_tag}])
                            except Exception as e_del:
                                logger.warning(f"Unable to delete remote tag before final retry: {e_del}")
                            # Attempt one last explicit-platform recovery and push
                            if self._recover_push_no_platform(public_repo, private_image):
                                logger.info(f"Successfully pushed {public_repo} to {private_image} after remote cleanup recovery.")
                                break
                            logger.error(f"Maximum attempts reached for pushing image {public_repo} to {private_image}.")
                            self.failed_push_addon_chart_images.append(private_image)
                except Exception as e:
                    logger.error(f"Unexpected error occurred while pushing image {public_repo} to {private_image}: {e}")
                    self.failed_push_addon_chart_images.append(private_image)

    def push_chart_to_ecr(self, chart_file, retry_count=5, retry_delay=10):
        """
        Pushes the Helm chart to the private ECR repository with retry logic.

        Args:
            chart_file (str): The path to the chart file.
            retry_count (int): Number of times to retry pushing the chart on failure.
            retry_delay (int): Delay between retry attempts in seconds.

        Raises:
            Exception: If creating or describing the ECR repository fails, or if pushing the chart fails.
        """
        try:
            repo_name = self.addon_chart
            self.ecr_client.describe_repositories(repositoryNames=[repo_name])
            logger.info(f"ECR repository {self.private_ecr_url}/{repo_name} exists.")
        except ClientError as e:
            if e.response["Error"]["Code"] == "RepositoryNotFoundException":
                repo_name = self.addon_chart
                logger.info(f"Repository {self.private_ecr_url}/{repo_name} not found, creating new repository...")
                try:
                    self.ecr_client.create_repository(repositoryName=repo_name, tags=[{"Key": "chart-syncer", "Value": "true"}])
                except ClientError as create_err:
                    logger.error(f"Unable to create ECR repository: {create_err}")
                    raise Exception(f"Unable to create ECR repository: {create_err}")
            else:
                logger.error(f"Error describing ECR repositories: {e}")
                raise Exception(f"Error describing ECR repositories: {e}")

        try:
            repo_name = self.addon_chart
            self.ecr_client.describe_images(repositoryName=repo_name, imageIds=[{"imageTag": self.addon_chart_version}])
            logger.info(f"Chart {self.addon_chart} version {self.addon_chart_version} already exists in ECR at {self.private_ecr_url}/{repo_name}, skipping push")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ImageNotFoundException":
                logger.error(f"Error checking for image existence: {e}")
                raise Exception(f"Error checking for image existence: {e}")

        self.authenticate_ecr(is_public=False)
        # Helm registry login for private ECR using current AWS identity (sandboxed)
        try:
            self._login_ecr_private_chart()
        except Exception as e:
            logger.warning(f"Helm registry login to private ECR failed; proceeding may fail: {e}")
        # Flattened chart push: push under chart name with no prefix
        dest_repo = f"oci://{self.private_ecr_url}"
        for attempt in range(retry_count):
            try:
                args_push_chart = ["push", chart_file, dest_repo]
                result = self.run_helm(args_push_chart, "Failed to push chart to ECR")
                if result is not None:
                    logger.info(f"Successfully pushed {self.addon_chart} to ECR.")
                    break
                else:
                    logger.warning(f"Attempt {attempt + 1} failed to push chart {chart_file} to {self.private_ecr_url}")
                    if attempt + 1 < retry_count:
                        logger.info(f"Retrying in {retry_delay} seconds...")
                        time.sleep(retry_delay)
                    else:
                        logger.error(f"Maximum attempts reached for pushing chart {chart_file} to {self.private_ecr_url}.")
                        self.failed_push_addon_chart_images.append(chart_file)
            except Exception as e:
                logger.error(f"Unexpected error occurred while pushing chart {chart_file} to {self.private_ecr_url}: {e}")
                self.failed_push_addon_chart_images.append(chart_file)

    def __str__(self):
        """
        Returns a string representation of the HelmChart instance.
        """
        return (f"addon_chart='{self.addon_chart}'\n"
                f"addon_chart_version='{self.addon_chart_version}'\n"
                f"addon_chart_repository='{self.addon_chart_repository}'\n"
                f"addon_chart_repository_namespace='{self.addon_chart_repository_namespace}'\n"
                f"addon_chart_release_name='{self.addon_chart_release_name}'\n"
                f"private_ecr_url='{self.private_ecr_url}'\n"
                f"public_addon_chart_images='{self.public_addon_chart_images}'\n"
                f"private_addon_chart_images='{self.private_addon_chart_images}'\n"
                f"image_vulnerabilities='{self.image_vulnerabilities}'\n"
                f"failed_pull_addon_chart_images='{self.failed_pull_addon_chart_images}'\n"
                f"failed_push_addon_chart_images='{self.failed_push_addon_chart_images}'\n"
                f"failed_commands='{self.failed_commands}'\n")
