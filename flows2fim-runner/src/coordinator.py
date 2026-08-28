#!/usr/bin/env python3
import boto3
from botocore.exceptions import ClientError
import pdb
import re
import sys
import subprocess
import logging
import argparse
import yaml
import os
import requests
from dotenv import load_dotenv

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Find and dispatch unprocessed Flow-to-FIM jobs"
    )
    parser.add_argument(
        "--config",
        default="./config/coord_config.yaml",
        help="Path to configuration file",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to environment file",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Find jobs but do not dispatch them"
    )
    # Add new flag for IAM role usage
    parser.add_argument(
        "--use-iam",
        action="store_true",
        help="Use IAM roles for worker instead of passing AWS credentials",
    )
    return parser.parse_args()


def load_config(config_path):
    """Load YAML configuration file"""
    if not os.path.exists(config_path):
        logger.error(f"Configuration file not found: {config_path}")
        sys.exit(1)

    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
        return config
    except Exception as e:
        logger.error(f"Error loading configuration: {e}")
        sys.exit(1)


def get_s3_client():
    """Get an S3 client"""
    return boto3.client("s3")


def get_ripple_directories(config):
    """Get all ripple directories from S3 with pagination"""
    s3_client = get_s3_client()
    bucket = config["s3"]["bucket"]
    prefix = config["s3"]["paths"]["ripple_collections"]
    all_dirs = []

    try:
        logger.info(f"Fetching ripple directories from {bucket}/{prefix}")
        continuation_token = None
        directory_count = 0

        while True:
            # If we have a continuation token, use it
            list_params = {"Bucket": bucket, "Prefix": prefix, "Delimiter": "/"}
            if continuation_token:
                list_params["ContinuationToken"] = continuation_token

            response = s3_client.list_objects_v2(**list_params)

            # Process the directories in this batch
            if "CommonPrefixes" in response:
                batch_dirs = [
                    prefix["Prefix"].rstrip("/").split("/")[-1]
                    for prefix in response["CommonPrefixes"]
                ]
                all_dirs.extend(batch_dirs)

                directory_count += len(batch_dirs)
                logger.debug(f"Found {len(batch_dirs)} directories in this batch")

            # Check if there are more objects
            if response.get("IsTruncated", False):
                continuation_token = response.get("NextContinuationToken")
                logger.debug(
                    f"Response truncated, continuing with token: {continuation_token}"
                )
            else:
                break

        logger.info(f"Found a total of {len(all_dirs)} ripple directories")

        if not all_dirs:
            logger.warning(f"No directories found in {bucket}/{prefix}")

        return all_dirs
    except ClientError as e:
        logger.error(f"Error listing S3 directories: {e}")
        sys.exit(1)


def get_completed_jobs(config):
    """Get set of all completed jobs (success or non-retry error) from S3 with pagination"""
    s3_client = get_s3_client()
    bucket = config["s3"]["bucket"]
    success_prefix = config["s3"]["paths"]["success_markers"]
    nonretry_error_prefix = config["s3"]["paths"]["error_nonretry"]
    completed = set()

    try:
        # Process successful jobs with pagination
        logger.info(f"Fetching success markers from {bucket}/{success_prefix}")
        continuation_token = None
        success_count = 0
        while True:
            # If we have a continuation token, use it
            list_params = {"Bucket": bucket, "Prefix": success_prefix}
            if continuation_token:
                list_params["ContinuationToken"] = continuation_token

            response = s3_client.list_objects_v2(**list_params)

            # Process the objects in this batch
            if "Contents" in response:
                batch_count = 0
                for obj in response["Contents"]:
                    key = obj["Key"]
                    filename = key.split("/")[-1]
                    match = re.search(
                        r"(.+?)_flows_(\d+year)\.success\.json$", filename
                    )
                    if match:
                        dir_name, flow_base = match.groups()
                        flow_file = f"flows_{flow_base}.csv"
                        completed.add((dir_name, flow_file))
                        batch_count += 1

                success_count += batch_count
                logger.debug(f"Processed {batch_count} success markers in this batch")

            # Check if there are more objects
            if response.get("IsTruncated", False):
                continuation_token = response.get("NextContinuationToken")
                logger.debug(
                    f"Response truncated, continuing with token: {continuation_token}"
                )
            else:
                break

        logger.info(f"Processed {success_count} total success markers")

        # Process jobs with non-retry errors with pagination
        logger.info(
            f"Fetching non-retry error markers from {bucket}/{nonretry_error_prefix}"
        )
        continuation_token = None
        error_count = 0
        while True:
            # If we have a continuation token, use it
            list_params = {"Bucket": bucket, "Prefix": nonretry_error_prefix}
            if continuation_token:
                list_params["ContinuationToken"] = continuation_token

            response = s3_client.list_objects_v2(**list_params)

            # Process the objects in this batch
            if "Contents" in response:
                batch_count = 0
                for obj in response["Contents"]:
                    key = obj["Key"]
                    filename = key.split("/")[-1]
                    match = re.search(r"(.+?)_flows_(\d+year)\.error\.json$", filename)
                    if match:
                        dir_name, flow_base = match.groups()
                        flow_file = f"flows_{flow_base}.csv"
                        completed.add((dir_name, flow_file))
                        batch_count += 1

                error_count += batch_count
                logger.debug(
                    f"Processed {batch_count} non-retry error markers in this batch"
                )

            # Check if there are more objects
            if response.get("IsTruncated", False):
                continuation_token = response.get("NextContinuationToken")
                logger.debug(
                    f"Response truncated, continuing with token: {continuation_token}"
                )
            else:
                break

        logger.info(f"Processed {error_count} total non-retry error markers")
        logger.info(f"Found a total of {len(completed)} completed jobs")
        return completed
    except ClientError as e:
        logger.error(f"Error getting completed jobs: {e}")
        return set()  # Return empty set on error


def get_retry_jobs(config):
    """Get set of jobs with retry errors from S3 with pagination"""
    s3_client = get_s3_client()
    bucket = config["s3"]["bucket"]
    retry_error_prefix = config["s3"]["paths"]["error_retry"]
    retry_jobs = set()

    try:
        # Process retry error jobs with pagination
        logger.info(f"Fetching retry error markers from {bucket}/{retry_error_prefix}")
        continuation_token = None
        retry_count = 0
        while True:
            # If we have a continuation token, use it
            list_params = {"Bucket": bucket, "Prefix": retry_error_prefix}
            if continuation_token:
                list_params["ContinuationToken"] = continuation_token

            response = s3_client.list_objects_v2(**list_params)

            # Process the objects in this batch
            if "Contents" in response:
                batch_count = 0
                for obj in response["Contents"]:
                    key = obj["Key"]
                    filename = key.split("/")[-1]
                    match = re.search(r"(.+?)_flows_(\d+year)\.error\.json$", filename)
                    if match:
                        dir_name, flow_base = match.groups()
                        flow_file = f"flows_{flow_base}.csv"
                        retry_jobs.add((dir_name, flow_file))
                        batch_count += 1

                retry_count += batch_count
                logger.debug(
                    f"Processed {batch_count} retry error markers in this batch"
                )

            # Check if there are more objects
            if response.get("IsTruncated", False):
                continuation_token = response.get("NextContinuationToken")
                logger.debug(
                    f"Response truncated, continuing with token: {continuation_token}"
                )
            else:
                break

        logger.info(f"Found a total of {len(retry_jobs)} jobs with retry errors")
        return retry_jobs
    except ClientError as e:
        logger.error(f"Error getting retry jobs: {e}")
        return set()  # Return empty set on error


def get_aws_credentials():
    """Get AWS credentials from environment variables if available"""
    aws_access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    aws_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    aws_session_token = os.environ.get("AWS_SESSION_TOKEN", "")

    # Return credentials even if empty
    return aws_access_key, aws_secret_key, aws_session_token


def get_registry_token():
    """Get GitLab registry token from environment variables"""
    registry_token = os.environ.get("REGISTRY_TOKEN", "")

    # Verify token is available
    if not registry_token:
        logger.error(
            "GitLab registry token required but not found in environment variables"
        )
        logger.error("Please set REGISTRY_TOKEN")
        sys.exit(1)

    return registry_token


def get_nomad_token():
    """Get Nomad token from environment variables if available"""
    return os.environ.get("NOMAD_TOKEN", "")


def dispatch_job(
    nomad_address,
    job_template,
    dir_name,
    flow_file,
    return_period,
    aws_access_key,
    aws_secret_key,
    aws_session_token,
    registry_token,
    dry_run=False,
    nomad_token=None,
    use_iam=False,  # Add use_iam parameter
):
    """Dispatch a job to the parameterized job template using the Nomad API"""
    if dry_run:
        logger.info(
            f"Would dispatch job for {dir_name} with {flow_file} ({return_period}yr)"
        )
        return True

    # Prepare API URL (ensure proper format)
    if not nomad_address.startswith(("http://", "https://")):
        nomad_address = f"http://{nomad_address}"
    nomad_address = nomad_address.rstrip("/")

    api_url = f"{nomad_address}/v1/job/{job_template}/dispatch"

    # Prepare metadata for the job
    payload = {
        "Meta": {
            "dir_name": dir_name,
            "flow_file": flow_file,
            "return_period": return_period,
            "registry_token": registry_token,
        }
    }

    # Only add AWS credentials to payload if they're provided AND use_iam is False
    if aws_access_key and aws_secret_key and not use_iam:
        payload["Meta"].update(
            {
                "aws_access_key": aws_access_key,
                "aws_secret_key": aws_secret_key,
            }
        )
        if aws_session_token:
            payload["Meta"]["aws_session_token"] = aws_session_token
    else:
        logger.info(f"Using IAM roles for job: {dir_name} with {flow_file}")

    # Prepare headers
    headers = {"Content-Type": "application/json"}
    if nomad_token:
        headers["X-Nomad-Token"] = nomad_token

    try:
        # Make API request
        response = requests.post(api_url, json=payload, headers=headers)
        response.raise_for_status()  # Raise exception for HTTP errors

        # Log response for debugging
        logger.debug(f"Nomad API response: {response.status_code} - {response.text}")

        logger.info(
            f"Dispatched job for {dir_name} with {flow_file} ({return_period}yr)"
        )
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to dispatch job via API: {str(e)}")
        if hasattr(e, "response") and e.response is not None:
            logger.error(
                f"Response details: {e.response.status_code} - {e.response.text}"
            )
        return False


def clean_retry_directory(config, completed_jobs, retry_jobs, dry_run=False):
    """Remove retry error markers for jobs that have been successfully completed

    Uses the intersection of completed_jobs and retry_jobs to determine which markers to delete,
    avoiding additional S3 API calls to list the contents of the retry directory.
    """
    s3_client = get_s3_client()
    bucket = config["s3"]["bucket"]
    retry_error_prefix = config["s3"]["paths"]["error_retry"]

    # Find jobs that are both in completed_jobs and retry_jobs
    jobs_to_clean = completed_jobs.intersection(retry_jobs)

    if not jobs_to_clean:
        logger.info("No retry error markers need cleanup")
        return 0

    logger.info(f"Found {len(jobs_to_clean)} retry markers to clean up")

    # Generate S3 keys for the retry markers to be deleted
    markers_to_delete = []
    for dir_name, flow_file in jobs_to_clean:
        # Extract the return period from the flow_file (e.g., "flows_100year.csv" -> "100year")
        match = re.search(r"flows_(\d+year)\.csv$", flow_file)
        if match:
            return_period = match.group(1)
            # Construct the S3 key for the retry marker
            marker_key = f"{retry_error_prefix.rstrip('/')}/{dir_name}_flows_{return_period}.error.json"
            markers_to_delete.append(marker_key)

    # Delete the identified markers
    if dry_run:
        logger.info(f"Would delete {len(markers_to_delete)} retry markers (dry run)")
        for key in markers_to_delete:
            logger.debug(f"Would delete: {key}")
    else:
        try:
            # Delete markers in batches (S3 allows up to 1000 objects per delete operation)
            batch_size = 1000
            for i in range(0, len(markers_to_delete), batch_size):
                batch = markers_to_delete[i : i + batch_size]
                objects_to_delete = [{"Key": key} for key in batch]

                s3_client.delete_objects(
                    Bucket=bucket, Delete={"Objects": objects_to_delete, "Quiet": True}
                )

            logger.info(
                f"Successfully cleaned up {len(markers_to_delete)} retry error markers"
            )
        except ClientError as e:
            logger.error(f"Error cleaning up retry directory: {e}")
            return 0

    return len(markers_to_delete)


def main():
    args = parse_args()

    # Load configuration
    config = load_config(args.config)

    # Load environment variables from .env file
    if os.path.exists(args.env_file):
        logger.info(f"Loading environment variables from {args.env_file}")
        load_dotenv(args.env_file)
    else:
        logger.warning(
            f"Environment file {args.env_file} not found, using existing environment variables"
        )

    # Get Nomad information
    nomad_address = config["nomad"]["address"]
    job_template = config["nomad"]["job_template"]

    # Get AWS credentials from environment
    aws_access_key, aws_secret_key, aws_session_token = get_aws_credentials()

    # Get GitLab registry token from environment
    registry_token = get_registry_token()

    # Get Nomad token if available
    nomad_token = get_nomad_token()

    # Log IAM role usage decision
    if args.use_iam:
        logger.info("Using IAM roles for workers (not passing AWS credentials)")
    else:
        logger.info("Using AWS credentials for workers")

    logger.info("Finding and dispatching unprocessed jobs")

    # Get all directories and flow files
    directories = get_ripple_directories(config)
    flow_files = config["flow_files"]

    # Get already completed jobs (successful or with non-retry errors)
    completed_jobs = get_completed_jobs(config)

    # Get jobs with retry errors
    retry_jobs = get_retry_jobs(config)

    # Create work items and dispatch them
    unprocessed_count = 0
    retry_count = 0
    dispatched_count = 0
    # Process unprocessed jobs
    for dir_name in directories:
        for flow_file_config in flow_files:
            flow_file = flow_file_config["name"]
            return_period = str(flow_file_config["return_period"])
            if (dir_name, flow_file) not in completed_jobs:
                is_retry = (dir_name, flow_file) in retry_jobs
                if is_retry:
                    retry_count += 1
                    logger.info(f"Found retry job: {dir_name} with {flow_file}")
                else:
                    unprocessed_count += 1
                    logger.info(
                        f"Found new unprocessed job: {dir_name} with {flow_file}"
                    )

                # Dispatch the job using the API
                if dispatch_job(
                    nomad_address,
                    job_template,
                    dir_name,
                    flow_file,
                    return_period,
                    aws_access_key,
                    aws_secret_key,
                    aws_session_token,
                    registry_token,
                    args.dry_run,
                    nomad_token,
                    args.use_iam,
                ):
                    dispatched_count += 1

    total_possible = len(directories) * len(flow_files)
    logger.info(
        f"Found {unprocessed_count} new unprocessed jobs and {retry_count} retry jobs out of {total_possible} possible combinations"
    )

    if args.dry_run:
        logger.info(f"Would have dispatched {dispatched_count} jobs (dry run)")
    else:
        logger.info(f"Successfully dispatched {dispatched_count} jobs")

    # Clean up retry directory
    logger.info("Cleaning up retry directory...")
    cleaned_count = clean_retry_directory(
        config, completed_jobs, retry_jobs, args.dry_run
    )
    if args.dry_run:
        logger.info(
            f"Would have removed {cleaned_count} obsolete retry markers (dry run)"
        )
    else:
        logger.info(f"Removed {cleaned_count} obsolete retry markers")


if __name__ == "__main__":
    main()
