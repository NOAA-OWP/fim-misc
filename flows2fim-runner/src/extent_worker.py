#!/usr/bin/env python3

import io
import os
import sys
import subprocess
import shutil
import time
import logging
import boto3
import yaml
import json
import datetime
from botocore.exceptions import ClientError
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class F2FWorker:
    def __init__(
        self,
        ripple_dir,
        flow_file,
        return_period,
        config_path="/app/config/worker_config.yaml",
    ):
        self.ripple_dir = ripple_dir
        self.flow_file = flow_file
        self.return_period = return_period

        # Load configuration
        self.config = self.load_config(config_path)

        # Get S3 bucket name (can be overridden by environment variable)
        self.s3_bucket = os.environ.get("S3_BUCKET", self.config["s3"]["bucket"])

        # Set up work directories
        self.work_dir = self.config["work_dirs"]["base"]
        self.library_dir = self.config["work_dirs"]["ripple_library"]
        self.control_dir = self.config["work_dirs"]["control_files"]
        self.output_dir = self.config["work_dirs"]["output"]

        # Check for AWS credentials
        aws_access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
        aws_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")

        if aws_access_key and aws_secret_key:
            logger.info("Using AWS credentials from environment variables")
        else:
            logger.info(
                "No AWS credentials found in environment, using IAM role if available"
            )

        # Initialize S3 client - will use env vars if available, otherwise IAM role
        self.s3_client = boto3.client("s3")

        # Parse components from directory name
        parts = self.ripple_dir.split("_")
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError(f"Invalid directory name format: {self.ripple_dir}")

        self.source = parts[0]
        self.digit_string = parts[1]
        self.common_name = "_".join(parts[2:]) if len(parts) > 2 else None
        self.common_suffix = f"_{self.common_name}" if self.common_name else ""

    def load_config(self, config_path):
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

    def setup_workspace(self):
        """Create clean workspace and required directories"""
        if os.path.exists(self.work_dir):
            shutil.rmtree(self.work_dir)

        os.makedirs(f"{self.work_dir}/{self.library_dir}", exist_ok=True)
        os.makedirs(f"{self.work_dir}/{self.control_dir}", exist_ok=True)
        os.makedirs(f"{self.work_dir}/{self.output_dir}", exist_ok=True)
        os.makedirs(f"{self.work_dir}/nwm_return_period_flows", exist_ok=True)

        # Change to work directory
        os.chdir(self.work_dir)

    def download_file(self, s3_path, local_path):
        """Download a file from S3 to local path"""
        try:
            logger.info(f"Downloading {s3_path} to {local_path}")
            self.s3_client.download_file(self.s3_bucket, s3_path, local_path)
            return True, None
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                error_msg = f"File not found in S3: {s3_path}"
                logger.error(error_msg)
                return False, error_msg
            else:
                error_msg = f"Error downloading {s3_path}: {e}"
                logger.error(error_msg)
                return False, error_msg

    def upload_file(self, local_path, s3_path):
        """Upload a file from local path to S3"""
        try:
            logger.info(f"Uploading {local_path} to {s3_path}")
            self.s3_client.upload_file(local_path, self.s3_bucket, s3_path)
            return True
        except ClientError as e:
            logger.error(f"Error uploading {local_path} to {s3_path}: {e}")
            return False

    def get_formatted_timestamp(self):
        """Return a human-readable timestamp"""
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def mark_success(self):
        """Create a success marker in S3 using JSON format"""
        # Remove .csv extension from flow_file for the marker name
        flow_file_base = self.flow_file.replace(".csv", "")
        marker_name = self.config["naming"]["success_marker"].format(
            dir_name=self.ripple_dir, flow_file=flow_file_base
        )
        marker_key = f"{self.config['s3']['paths']['success_markers']}{marker_name}"

        # Create JSON content
        success_data = {
            "directory": self.ripple_dir,
            "flow_file": self.flow_file,
            "status": "success",
            "timestamp": self.get_formatted_timestamp(),
        }

        json_content = json.dumps(success_data, indent=2)

        try:
            self.s3_client.put_object(
                Bucket=self.s3_bucket,
                Key=marker_key,
                Body=json_content,
                ContentType="application/json",  # Add JSON content type
            )
            logger.info(f"Created success marker: {marker_key}")
            return True
        except ClientError as e:
            logger.error(f"Error creating success marker: {e}")
            return False

    def record_error(self, error_type, error_message):
        """Record error details in S3 as JSON"""
        # Remove .csv extension from flow_file for the error marker name
        flow_file_base = self.flow_file.replace(".csv", "")
        error_name = self.config["naming"]["error_marker"].format(
            dir_name=self.ripple_dir, flow_file=flow_file_base
        )

        # Handle "File not found" errors as non-retry errors
        if error_type == "download_error" and "File not found in S3" in error_message:
            error_category = self.config["s3"]["paths"]["error_nonretry"]
            logger.info(f"Categorizing 'File not found in S3' as non-retry error")
        elif (
            error_type == "fim_error"
            and "error converting VRT to GTIFF: signal: killed" in error_message
        ):
            error_category = self.config["s3"]["paths"]["error_retry"]
            logger.info(f"Categorizing 'signal: killed' fim_error as retry error")
        # Otherwise, determine error category based on error type
        elif error_type in ["controls_error", "fim_error"]:
            error_category = self.config["s3"]["paths"]["error_nonretry"]
        else:
            error_category = self.config["s3"]["paths"]["error_retry"]

        error_key = f"{error_category}{error_name}"

        # Create JSON content
        error_data = {
            "directory": self.ripple_dir,
            "flow_file": self.flow_file,
            "error_type": error_type,
            "error_message": error_message,
            "timestamp": self.get_formatted_timestamp(),
        }

        json_content = json.dumps(error_data, indent=2)

        try:
            self.s3_client.put_object(
                Bucket=self.s3_bucket,
                Key=error_key,
                Body=json_content,
                ContentType="application/json",  # Add JSON content type
            )
            logger.info(f"Recorded error: {error_key}")
            return True
        except ClientError as e:
            logger.error(f"Error recording error details: {e}")
            return False

    def download_required_files(self):
        """Download essential files needed for processing"""
        # Format the base path for ripple directory
        base_path = (
            f"{self.config['s3']['paths']['ripple_collections']}{self.ripple_dir}"
        )

        # Download ripple.gpkg
        success, error_msg = self.download_file(
            f"{base_path}/ripple.gpkg", f"{self.library_dir}/ripple.gpkg"
        )
        if not success:
            self.record_error("download_error", error_msg)
            return False

        # Download start_reaches.csv
        success, error_msg = self.download_file(
            f"{base_path}/start_reaches.csv", f"{self.library_dir}/start_reaches.csv"
        )
        if not success:
            self.record_error("download_error", error_msg)
            return False

        # Download flow file
        flow_s3_path = f"{self.config['s3']['paths']['flow_files']}{self.flow_file}"
        success, error_msg = self.download_file(
            flow_s3_path, f"nwm_return_period_flows/{self.flow_file}"
        )
        if not success:
            self.record_error("download_error", error_msg)
            return False

        return True

    def run_command(self, cmd):
        """Run a shell command and return result"""
        logger.info(f"Running command: {cmd}")
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)

        if result.stdout:
            logger.info(f"Command output: {result.stdout[:500]}...")
        if result.stderr:
            logger.error(f"Command error: {result.stderr}")

        return result

    def generate_control_file(self):
        """Generate control file for the extent"""
        control_filename = self.config["naming"]["control_file"].format(
            source=self.source,
            digit_string=self.digit_string,
            common_suffix=self.common_suffix,
            return_period=self.return_period,
        )
        control_file_path = f"{self.control_dir}/{control_filename}"

        flows2fim_path = self.config["processing"]["flows2fim_path"]

        cmd = (
            f"{flows2fim_path} controls "
            f"-db '{self.library_dir}/ripple.gpkg' "
            f"-f 'nwm_return_period_flows/{self.flow_file}' "
            f"-o '{control_file_path}' "
            f"-scsv '{self.library_dir}/start_reaches.csv'"
        )

        result = self.run_command(cmd)
        if result.returncode != 0:
            self.record_error(
                "controls_error",
                (
                    result.stderr
                    if result.stderr
                    else "Unknown error generating control file"
                ),
            )
            return None

        return control_file_path

    def generate_extent(self, control_file):
        """Generate flood extent using flows2fim"""
        # Determine output filename
        if self.common_name:
            output_filename = self.config["naming"]["output_with_common"].format(
                return_period=self.return_period, common_name=self.common_name
            )
        else:
            output_filename = self.config["naming"]["output_file"].format(
                return_period=self.return_period
            )

        output_path = f"{self.output_dir}/{output_filename}"

        # Use VSI path for library_extent
        library_path = self.config["s3"]["paths"]["library_extent"].format(
            dir_name=self.ripple_dir
        )
        vsi_library_path = f"/vsis3/{self.s3_bucket}/{library_path}"

        flows2fim_path = self.config["processing"]["flows2fim_path"]
        output_format = self.config["processing"]["output_format"]

        cmd = (
            f"{flows2fim_path} fim "
            f"-lib '{vsi_library_path}' "
            f"-c '{control_file}' "
            f"-fmt '{output_format}' "
            f"-o '{output_path}' "
            f"-type 'extent'"
        )

        result = self.run_command(cmd)

        # Check for key_not_exist error
        if result.returncode != 0:
            if (
                "The specified key does not exist" in result.stderr
                or "does not exist in the file system" in result.stderr
            ):
                self.record_error(
                    "key_not_exist", "Required files missing in library_extent"
                )
            else:
                self.record_error(
                    "fim_error",
                    (
                        result.stderr
                        if result.stderr
                        else "Unknown error generating extent"
                    ),
                )
            return None

        # Check if output file was actually created
        if not os.path.exists(output_path):
            self.record_error(
                "missing_output", "Output file not created despite successful command"
            )
            return None

        return output_path

    def upload_extent(self, output_path):
        """Upload the generated extent to S3"""
        # Determine S3 path based on whether there's a common name
        if self.common_name:
            s3_filename = self.config["naming"]["output_with_common"].format(
                return_period=self.return_period, common_name=self.common_name
            )
            s3_key_template = self.config["s3"]["paths"]["output_with_common"]
            s3_key = (
                s3_key_template.format(
                    source=self.source,
                    id=self.digit_string,
                    common_name=self.common_name,
                )
                + s3_filename
            )
        else:
            s3_filename = self.config["naming"]["output_file"].format(
                return_period=self.return_period
            )
            s3_key_template = self.config["s3"]["paths"]["output_base"]
            s3_key = (
                s3_key_template.format(source=self.source, id=self.digit_string)
                + s3_filename
            )

        if not self.upload_file(output_path, s3_key):
            self.record_error("upload_error", f"Failed to upload extent to {s3_key}")
            return False

        return True

    def process(self):
        """Run the full processing workflow"""
        try:
            # Set up workspace
            self.setup_workspace()

            # Download required files
            if not self.download_required_files():
                return False

            # Generate control file
            control_file = self.generate_control_file()
            if not control_file:
                return False

            # Generate extent
            output_path = self.generate_extent(control_file)
            if not output_path:
                return False

            # Upload extent
            if not self.upload_extent(output_path):
                return False

            # Mark as successful
            self.mark_success()

            logger.info(
                f"Successfully processed {self.ripple_dir} with {self.flow_file}"
            )
            return True

        except Exception as e:
            logger.exception("Unexpected error during processing")
            self.record_error("unexpected_error", str(e))
            return False
        finally:
            # Clean up
            if os.path.exists(self.work_dir):
                try:
                    shutil.rmtree(self.work_dir)
                except:
                    logger.warning(
                        f"Failed to clean up work directory: {self.work_dir}"
                    )


def main():
    """Main function to process a single extent"""
    if len(sys.argv) < 4:
        print(
            "Usage: process_f2f_extent.py <ripple_dir> <flow_file> <return_period> [config_path]"
        )
        sys.exit(1)

    ripple_dir = sys.argv[1]
    flow_file = sys.argv[2]
    return_period = sys.argv[3]

    # Optional config path
    config_path = sys.argv[4] if len(sys.argv) > 4 else "/app/config/worker_config.yaml"

    logger.info(
        f"Starting processing for {ripple_dir} with {flow_file} ({return_period}yr)"
    )

    try:
        worker = F2FWorker(ripple_dir, flow_file, return_period, config_path)
        success = worker.process()

        if success:
            logger.info("Processing completed successfully")
            sys.exit(0)
        else:
            logger.error("Processing failed")
            sys.exit(1)
    except Exception as e:
        logger.exception("Unhandled exception in worker")
        sys.exit(1)


if __name__ == "__main__":
    main()
