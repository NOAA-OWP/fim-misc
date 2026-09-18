# This script takes a list of ripple directories from a text file and deletes benchmark extent directories not on that list.
#!/usr/bin/env python3
import boto3
import re
import sys
import argparse


def parse_text_file(file_path):
    """
    Parse the text file and extract 'ble' and 'mip' identifiers.
    Returns two sets: ble_ids and mip_ids
    """
    ble_ids = set()
    mip_ids = set()

    with open(file_path, "r") as file:
        for line in file:
            line = line.strip()

            # Extract BLE identifiers (e.g., "ble_08020203_LowerStFrancis")
            ble_match = re.match(r"ble_(\d{2,12})(?:_.*)?$", line)
            if ble_match:
                numeric_part = ble_match.group(1)
                ble_ids.add(numeric_part)
                continue

            # Extract MIP identifiers (e.g., "mip_03110203")
            mip_match = re.match(r"mip_(\d{2,12})$", line)
            if mip_match:
                numeric_part = mip_match.group(1)
                mip_ids.add(numeric_part)

    return ble_ids, mip_ids


def list_s3_directories(s3_client, bucket, prefix):
    """
    List all directories in the given S3 path.
    Returns a list of directory names.
    """
    response = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix, Delimiter="/")

    directories = []
    if "CommonPrefixes" in response:
        for common_prefix in response["CommonPrefixes"]:
            # Extract the directory name from the prefix
            dir_path = common_prefix["Prefix"]
            dir_name = dir_path.rstrip("/").split("/")[-1]
            directories.append(dir_name)

    return directories


def extract_numeric_part(dir_name):
    """
    Extract the numeric part from a directory name.
    For example, from "05119_Pulaski" extract "05119".
    """
    match = re.match(r"(\d{2,12})(?:_.*)?$", dir_name)
    if match:
        return match.group(1)
    return None


def delete_s3_directory(s3_client, bucket, prefix):
    """
    Delete a directory and all its contents from S3.
    """
    # List all objects in the directory
    objects_to_delete = []
    paginator = s3_client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        if "Contents" in page:
            for obj in page["Contents"]:
                objects_to_delete.append({"Key": obj["Key"]})

    # Delete the objects in batches of 1000 (S3 limit)
    if objects_to_delete:
        for i in range(0, len(objects_to_delete), 1000):
            batch = objects_to_delete[i : i + 1000]
            s3_client.delete_objects(Bucket=bucket, Delete={"Objects": batch})

    return len(objects_to_delete)


def main():
    parser = argparse.ArgumentParser(
        description="Delete S3 directories that don't have corresponding entries in a text file."
    )
    parser.add_argument(
        "file_path", help="Path to the text file containing ble and mip entries"
    )
    parser.add_argument("--bucket", default="fimc-data", help="S3 bucket name")
    parser.add_argument(
        "--ble-prefix",
        default="benchmark/ripple_fim_100/ble/",
        help="S3 prefix for BLE directories",
    )
    parser.add_argument(
        "--mip-prefix",
        default="benchmark/ripple_fim_100/mip/",
        help="S3 prefix for MIP directories",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without actually deleting",
    )

    args = parser.parse_args()

    # Parse the text file
    ble_ids, mip_ids = parse_text_file(args.file_path)

    print(f"Found {len(ble_ids)} BLE IDs and {len(mip_ids)} MIP IDs in the text file")

    # Create S3 client
    s3_client = boto3.client("s3")

    # Process MIP directories
    mip_directories = list_s3_directories(s3_client, args.bucket, args.mip_prefix)
    print(f"Found {len(mip_directories)} MIP directories in S3")

    deleted_mip_count = 0
    deleted_mip_files = 0

    # For MIP, the directory name should match the ID exactly
    for dir_name in mip_directories:
        if dir_name not in mip_ids:
            full_path = f"{args.mip_prefix}{dir_name}/"
            if args.dry_run:
                print(f"Would delete: {full_path}")
            else:
                files_deleted = delete_s3_directory(s3_client, args.bucket, full_path)
                deleted_mip_count += 1
                deleted_mip_files += files_deleted
                print(f"Deleted: {full_path} ({files_deleted} files)")

    # Process BLE directories
    ble_directories = list_s3_directories(s3_client, args.bucket, args.ble_prefix)
    print(f"Found {len(ble_directories)} BLE directories in S3")

    deleted_ble_count = 0
    deleted_ble_files = 0

    # For BLE, extract the numeric part from the directory name and check if it's in the IDs
    for dir_name in ble_directories:
        numeric_part = extract_numeric_part(dir_name)
        if not numeric_part or numeric_part not in ble_ids:
            full_path = f"{args.ble_prefix}{dir_name}/"
            if args.dry_run:
                print(f"Would delete: {full_path}")
            else:
                files_deleted = delete_s3_directory(s3_client, args.bucket, full_path)
                deleted_ble_count += 1
                deleted_ble_files += files_deleted
                print(f"Deleted: {full_path} ({files_deleted} files)")

    # Print summary
    if args.dry_run:
        print("\nDRY RUN SUMMARY:")
        print(f"Would delete {deleted_mip_count} MIP directories")
        print(f"Would delete {deleted_ble_count} BLE directories")
    else:
        print("\nDELETION SUMMARY:")
        print(
            f"Deleted {deleted_mip_count} MIP directories ({deleted_mip_files} files)"
        )
        print(
            f"Deleted {deleted_ble_count} BLE directories ({deleted_ble_files} files)"
        )


if __name__ == "__main__":
    main()
