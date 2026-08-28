#!/bin/bash

# usage: ./get_error_messages.sh s3://fimc-data/benchmark/ripple_fim_100/status/errors/nonretry/
# counts the number of unique error messages in an flows2fim extent directory

# Check if the S3 URI is provided
if [ $# -ne 1 ]; then
    echo "Usage: $0 <S3_URI>"
    exit 1
fi

S3_URI=$1
TEMP_DIR=$(mktemp -d)

# Function to clean up temporary files
cleanup() {
    rm -rf "$TEMP_DIR"
}

# Set trap to clean up on exit
trap cleanup EXIT

# Extract the bucket and prefix from the S3 URI
S3_URI_NO_PROTOCOL=${S3_URI#s3://}
S3_BUCKET=$(echo "$S3_URI_NO_PROTOCOL" | cut -d'/' -f1)
S3_PREFIX=${S3_URI_NO_PROTOCOL#$S3_BUCKET/}

echo "Extracting unique error messages from JSON files at $S3_URI..."

# List all JSON files in the prefix
aws s3api list-objects-v2 --bucket "$S3_BUCKET" --prefix "$S3_PREFIX" --output json | \
  jq -r '.Contents[].Key' | grep '\.json$' > "$TEMP_DIR/keys.txt"

total_files=$(wc -l < "$TEMP_DIR/keys.txt")
echo "Found $total_files JSON files."

if [ "$total_files" -eq 0 ]; then
    echo "No JSON files found."
    exit 0
fi

# Extract error messages from each file
while read -r key; do
    aws s3 cp "s3://$S3_BUCKET/$key" - | jq -r '.error_message' >> "$TEMP_DIR/error_messages.txt"
done < "$TEMP_DIR/keys.txt"

# Output unique error messages
echo "Unique error messages:"
sort "$TEMP_DIR/error_messages.txt" | uniq
