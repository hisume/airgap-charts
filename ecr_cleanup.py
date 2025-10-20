import argparse
import logging
import sys
from typing import List, Tuple

import boto3
from botocore.exceptions import ClientError

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("ecr_cleanup")


def list_repositories(ecr_client) -> List[dict]:
    """
    List all private ECR repositories using pagination.
    """
    repos: List[dict] = []
    paginator = ecr_client.get_paginator("describe_repositories")
    for page in paginator.paginate():
        repos.extend(page.get("repositories", []))
    return repos


def has_chart_syncer_tag(ecr_client, repo_arn: str) -> bool:
    """
    Check if the repository has tag chart-syncer=true.
    """
    try:
        resp = ecr_client.list_tags_for_resource(resourceArn=repo_arn)
        for tag in resp.get("tags", []):
            if tag.get("Key") == "chart-syncer" and str(tag.get("Value")).lower() == "true":
                return True
    except ClientError as e:
        logger.warning(f"Unable to list tags for {repo_arn}: {e}")
    return False


def select_candidates(ecr_client, repositories: List[dict], delete_all: bool) -> List[Tuple[str, str]]:
    """
    Select repositories to delete based on flags.
    Returns list of (repositoryName, repositoryArn).
    """
    candidates: List[Tuple[str, str]] = []
    for repo in repositories:
        name = repo.get("repositoryName")
        arn = repo.get("repositoryArn")
        if not name or not arn:
            continue
        if delete_all:
            candidates.append((name, arn))
        else:
            # Default: only repos created by this tool (chart-syncer=true)
            if has_chart_syncer_tag(ecr_client, arn):
                candidates.append((name, arn))
    return candidates


def delete_repositories(ecr_client, repos: List[Tuple[str, str]]) -> Tuple[List[str], List[Tuple[str, str, str]]]:
    """
    Delete repositories with force=True. Returns (deleted_names, failed_entries).
    failed_entries is list of (name, arn, error).
    """
    deleted: List[str] = []
    failed: List[Tuple[str, str, str]] = []
    for name, arn in repos:
        try:
            logger.info(f"Deleting ECR repository: {name} ({arn})")
            ecr_client.delete_repository(repositoryName=name, force=True)
            deleted.append(name)
        except ClientError as e:
            msg = str(e)
            logger.error(f"Failed to delete {name}: {msg}")
            failed.append((name, arn, msg))
    return deleted, failed


def main():
    parser = argparse.ArgumentParser(
        description="Delete private ECR repositories in the current AWS account/region.\n"
                    "Defaults to deleting only repositories created by this tool (tag chart-syncer=true).",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Delete ALL repositories in the account/region (destructive). Without this flag, only repositories "
             "tagged chart-syncer=true are deleted."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted and exit without performing deletion."
    )
    args = parser.parse_args()

    # Use default AWS identity/region from environment/config; do not accept ECR passwords for deletion.
    session = boto3.Session()
    region = session.region_name
    if not region:
        logger.error("No AWS region detected. Set AWS_DEFAULT_REGION or configure a default region (aws configure).")
        sys.exit(1)
    sts = session.client("sts", region_name=region)
    try:
        identity = sts.get_caller_identity()
        account = identity.get("Account")
        logger.info(f"Using AWS account {account} in region {region}")
    except ClientError as e:
        logger.error(f"Unable to get AWS caller identity: {e}")
        sys.exit(1)

    ecr = session.client("ecr", region_name=region)

    try:
        all_repos = list_repositories(ecr)
    except ClientError as e:
        logger.error(f"Failed to list ECR repositories: {e}")
        sys.exit(1)

    logger.info(f"Found {len(all_repos)} repositories in region {region}")
    candidates = select_candidates(ecr, all_repos, delete_all=args.all)
    logger.info(f"Selected {len(candidates)} repositories for deletion "
                f"({'ALL' if args.all else 'tag chart-syncer=true'})")

    if not candidates:
        logger.info("Nothing to do.")
        return

    # Dry run output
    if args.dry_run:
        logger.info("Dry run mode enabled. The following repositories would be deleted:")
        for name, arn in candidates:
            logger.info(f"- {name} ({arn})")
        logger.info(f"Total: {len(candidates)} repositories would be deleted.")
        return

    # Execute deletions
    deleted, failed = delete_repositories(ecr, candidates)

    # Summary
    logger.info("Deletion summary")
    logger.info(f"- Deleted: {len(deleted)}")
    for name in deleted:
        logger.info(f"  * {name}")
    logger.info(f"- Failed: {len(failed)}")
    for name, arn, err in failed:
        logger.info(f"  * {name} ({arn}) -> {err}")


if __name__ == "__main__":
    main()
