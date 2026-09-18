#!/bin/bash
set -e  # Exit on any error

# Memory-related environment variables (can be set before running the script)
# GDAL_CACHEMAX_SETTING - GDAL cache size in MB (default: 32)
# MAX_CONCURRENT_JOBS - Maximum concurrent processes (default: 4)
# VSI_CACHE_SIZE_SETTING - VSI cache size (default: 1000000)
# CHUNK_SIZE_SETTING - Maximum chunk dimension (default: 4000)
# CHUNKING_MODE - Chunking strategy: "grid" or "strips" (default: "strips")
# STRIP_HEIGHT - Height of strips in strip mode (default: 2000)
# USE_WEBP - Use WEBP compression instead of DEFLATE (default: "NO")

# Check if input directory is provided
if [ $# -lt 1 ]; then
    echo "Usage: $0 directory_with_vrts [output_dir]"
    echo "If output_dir is not specified, it will use the input directory"
    echo ""
    echo "Environment variables for tuning:"
    echo "  GDAL_CACHEMAX_SETTING - GDAL cache size in MB (default: 32)"
    echo "  MAX_CONCURRENT_JOBS - Maximum concurrent processes (default: 4)"
    echo "  VSI_CACHE_SIZE_SETTING - VSI cache size (default: 1000000)"
    echo "  CHUNK_SIZE_SETTING - Maximum chunk dimension (default: 4000)"
    echo "  CHUNKING_MODE - Chunking strategy: 'grid' or 'strips' (default: 'strips')"
    echo "  STRIP_HEIGHT - Height of strips in strip mode (default: 2000)"
    echo "  USE_WEBP - Use WEBP compression instead of DEFLATE (default: NO)"
    exit 1
fi

input_dir="$1"
output_dir="${2:-$input_dir}"

# Create output directory if it doesn't exist
mkdir -p "$output_dir"

# Default memory settings - can be overridden by environment variables
DEFAULT_GDAL_CACHEMAX=${GDAL_CACHEMAX_SETTING:-32}
DEFAULT_MAX_CONCURRENT=${MAX_CONCURRENT_JOBS:-4}
DEFAULT_VSI_CACHE_SIZE=${VSI_CACHE_SIZE_SETTING:-1000000}
DEFAULT_CHUNK_SIZE=${CHUNK_SIZE_SETTING:-4000}
CHUNKING_MODE=${CHUNKING_MODE:-"strips"}
STRIP_HEIGHT=${STRIP_HEIGHT:-2000}
USE_WEBP=${USE_WEBP:-"NO"}

# Function to check if a command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Process VRT using strips mode - process horizontal strips instead of a grid
process_vrt_strips() {
    local input_vrt="$1"
    local output_cog="$2"
    local temp_dir="$3"
    local width="$4"
    local height="$5"
    
    echo "Processing VRT in strips mode..."
    
    # Calculate number of strips based on height and STRIP_HEIGHT
    local num_strips=$((height / STRIP_HEIGHT))
    if [ $((height % STRIP_HEIGHT)) -ne 0 ]; then
        num_strips=$((num_strips + 1))
    fi
    
    echo "Dividing VRT into $num_strips horizontal strips of height ~$STRIP_HEIGHT pixels"
    
    # Determine max concurrent jobs based on memory
    local available_memory=0
    if [ -f /proc/meminfo ]; then
        available_memory=$(grep "MemAvailable" /proc/meminfo | awk '{print int($2/1024)}')
    fi
    
    # Calculate per-strip memory usage (rough estimate)
    local strip_size_mb=$(((width * STRIP_HEIGHT * 4) / 1024 / 1024))
    local suggested_max_concurrent=$DEFAULT_MAX_CONCURRENT
    
    if [ $available_memory -gt 0 ]; then
        # Each strip process might use strip_size_mb + overhead (100MB)
        local mem_per_process=$((strip_size_mb + 100))
        suggested_max_concurrent=$((available_memory / mem_per_process / 2))  # Conservative
        
        # Sanity limits
        if [ $suggested_max_concurrent -lt 1 ]; then
            suggested_max_concurrent=1
        elif [ $suggested_max_concurrent -gt 8 ]; then
            suggested_max_concurrent=8  # Cap at 8 for strip mode
        fi
        
        echo "Memory-based concurrency: $suggested_max_concurrent processes (est. memory per strip: ${strip_size_mb}MB)"
    else
        suggested_max_concurrent=4  # Default
        echo "Using default concurrency: $suggested_max_concurrent processes"
    fi
    
    # Process strips
    local max_concurrent=$suggested_max_concurrent
    local batch=0
    local has_failures=0
    
    for i in $(seq 0 $((num_strips-1))); do
        # Calculate strip coordinates
        local y_off=$((i * STRIP_HEIGHT))
        local this_height=$STRIP_HEIGHT
        
        # Handle last strip which might be shorter
        if [ $i -eq $((num_strips-1)) ] && [ $((height % STRIP_HEIGHT)) -ne 0 ]; then
            this_height=$((height % STRIP_HEIGHT))
        fi
        
        local strip_file="$temp_dir/strip_${i}.tif"
        
        # Skip if already processed
        if [ -f "$strip_file" ]; then
            echo "Strip $i already processed, skipping."
            continue
        fi
        
        echo "Processing strip $i: y-offset $y_off, height $this_height"
        
        # Process the strip
        (
            # Use lower memory settings for strip processing
            local gdal_cache=$((DEFAULT_GDAL_CACHEMAX / 2))
            local vsi_cache_size=$((DEFAULT_VSI_CACHE_SIZE / 2))
            
            # Determine compression type based on USE_WEBP setting
            local compress_opt="COMPRESS=DEFLATE"
            if [ "$USE_WEBP" = "YES" ]; then
                compress_opt="COMPRESS=WEBP"
            fi
            
            gdal_translate \
              --config AWS_S3_MAX_CONNECTIONS 5 \
              --config GDAL_CACHEMAX $gdal_cache \
              --config CPL_VSIL_USE_TEMP_FILE_FOR_RANDOM_WRITE YES \
              --config GDAL_DISABLE_READDIR_ON_OPEN TRUE \
              --config VSI_CACHE TRUE \
              --config VSI_CACHE_SIZE $vsi_cache_size \
              --config GDAL_MAX_DATASET_POOL_SIZE 4 \
              --config GDAL_VRT_ENABLE_PYTHON NO \
              --config GDAL_USE_MMAP NO \
              --config VRT_SHARED_SOURCE NO \
              --config GDAL_VRT_IGNORE_SOURCE_OVERVIEWS YES \
              --config CPL_DISABLE_REMAP_MATRIX TRUE \
              --config GDAL_ONE_BIG_READ NO \
              -srcwin 0 $y_off $width $this_height \
              -of GTiff \
              -co BIGTIFF=YES \
              -co $compress_opt \
              -co NUM_THREADS=1 \
              -co SPARSE_OK=YES \
              "$input_vrt" "$strip_file" || { 
                  echo "Error processing strip $i"; 
                  touch "$temp_dir/error_strip_${i}"; 
                  exit 1; 
              }
            
            if [ ! -f "$strip_file" ]; then
                echo "Failed to create strip $i"
                touch "$temp_dir/error_strip_${i}"
                exit 1
            fi
            
            echo "Completed strip $i"
        ) &
        
        batch=$((batch + 1))
        
        # Wait for batch to complete if needed
        if [ $batch -ge $max_concurrent ] || [ $i -eq $((num_strips-1)) ]; then
            echo "Waiting for batch to complete..."
            wait
            
            # Check for errors
            error_count=$(find "$temp_dir" -name "error_strip_*" | wc -l)
            if [ $error_count -gt 0 ]; then
                echo "Errors detected in this batch ($error_count strips failed)."
                has_failures=1
            fi
            
            batch=0
        fi
    done
    
    # Handle failed strips sequentially if needed
    if [ $has_failures -eq 1 ]; then
        echo "Some strips failed in parallel mode. Trying sequential processing for failed strips..."
        
        # List failed strips
        failed_strips=$(find "$temp_dir" -name "error_strip_*" | sed 's/.*error_strip_\(.*\)/\1/')
        
        for strip_id in $failed_strips; do
            # Calculate strip coordinates
            local y_off=$((strip_id * STRIP_HEIGHT))
            local this_height=$STRIP_HEIGHT
            
            # Handle last strip which might be shorter
            if [ $strip_id -eq $((num_strips-1)) ] && [ $((height % STRIP_HEIGHT)) -ne 0 ]; then
                this_height=$((height % STRIP_HEIGHT))
            fi
            
            local strip_file="$temp_dir/strip_${strip_id}.tif"
            
            # Remove error marker
            rm -f "$temp_dir/error_strip_${strip_id}"
            
            echo "Retrying strip $strip_id sequentially: y-offset $y_off, height $this_height"
            
            # Use minimal memory settings
            gdal_translate \
              --config GDAL_CACHEMAX 16 \
              --config CPL_VSIL_USE_TEMP_FILE_FOR_RANDOM_WRITE YES \
              --config GDAL_DISABLE_READDIR_ON_OPEN TRUE \
              --config VSI_CACHE TRUE \
              --config VSI_CACHE_SIZE 500000 \
              --config GDAL_MAX_DATASET_POOL_SIZE 2 \
              --config GDAL_VRT_ENABLE_PYTHON NO \
              --config GDAL_USE_MMAP NO \
              --config VRT_SHARED_SOURCE NO \
              --config GDAL_VRT_IGNORE_SOURCE_OVERVIEWS YES \
              --config CPL_DISABLE_REMAP_MATRIX TRUE \
              --config GDAL_ONE_BIG_READ NO \
              -srcwin 0 $y_off $width $this_height \
              -of GTiff \
              -co BIGTIFF=YES \
              -co COMPRESS=DEFLATE \
              -co NUM_THREADS=1 \
              -co SPARSE_OK=YES \
              "$input_vrt" "$strip_file" || {
                  echo "Failed to process strip $strip_id even with conservative settings."
                  touch "$temp_dir/error_strip_${strip_id}"
                  has_failures=1
              }
        done
        
        # Final check
        error_count=$(find "$temp_dir" -name "error_strip_*" | wc -l)
        if [ $error_count -gt 0 ]; then
            echo "Error: $error_count strips still failed after sequential processing."
            return 1
        fi
    fi
    
    # Verify all strips were created
    expected_strips=$num_strips
    actual_strips=$(find "$temp_dir" -name "strip_*.tif" | wc -l)
    
    if [ $actual_strips -ne $expected_strips ]; then
        echo "Error: Expected $expected_strips strips but found $actual_strips."
        return 1
    fi
    
    echo "All strips processed successfully. Creating VRT..."
    
    # Create a VRT from all strips
    local chunks_vrt="${temp_dir}/strips.vrt"
    find "$temp_dir" -name "strip_*.tif" | sort > "${temp_dir}/strip_list.txt"
    gdalbuildvrt -input_file_list "${temp_dir}/strip_list.txt" "$chunks_vrt"
    
    # Check if VRT was created successfully
    if [ ! -f "$chunks_vrt" ]; then
        echo "Error: Failed to create VRT from strips."
        return 1
    fi
    
    return 0
}

# Process VRT using grid mode
process_vrt_grid() {
    local input_vrt="$1"
    local output_cog="$2"
    local temp_dir="$3"
    local width="$4"
    local height="$5"
    
    echo "Processing VRT in grid mode..."

    # Dynamically adjust chunk count based on image size and available memory
    local pixels=$((width * height))
    local total_size_estimate=$((pixels * 4 / 1024 / 1024))  # Rough estimate in MB (assuming 4 bytes per pixel)
    echo "Estimated raster size: ~${total_size_estimate}MB"

    # Adjust min/max chunks based on image size
    local min_chunks=60
    local max_chunks=400  # Allow for more chunks with very large images

    # For very large images, increase the minimum number of chunks
    if [ $total_size_estimate -gt 10000 ]; then  # >10GB estimate
        min_chunks=200
        max_chunks=800
        echo "Large raster detected, increasing chunk count (min=$min_chunks, max=$max_chunks)"
    fi

    # Calculate maximum size per chunk based on available memory
    local max_chunk_size=0
    if [ -f /proc/meminfo ]; then
        available_memory=$(grep "MemAvailable" /proc/meminfo | awk '{print int($2/1024)}')
        # Target ~10% of available memory per chunk, with concurrent processing
        max_chunk_size=$((available_memory / 16))  # MB
        echo "Target maximum chunk size: ~${max_chunk_size}MB based on available memory"
    else
        max_chunk_size=200  # Conservative default (200MB per chunk)
    fi

    # Find the best grid size that divides the image evenly
    best_grid_size=0
    min_remainder=999999999

    # Try different grid sizes to find optimal division
    for grid_size in $(seq 12 30); do  # Higher starting point for more chunks
        total_chunks=$((grid_size * grid_size))
        if [ $total_chunks -ge $min_chunks ] && [ $total_chunks -le $max_chunks ]; then
            # Calculate chunk size and check if it's within memory constraints
            local chunk_width_approx=$((width / grid_size))
            local chunk_height_approx=$((height / grid_size))
            local chunk_size_mb=$(((chunk_width_approx * chunk_height_approx * 4) / 1024 / 1024))
            
            # For VRT files, we need to be more conservative with chunk size
            # because of the memory allocation issue in the VRT driver
            if [ $chunk_size_mb -le $max_chunk_size ] || [ $best_grid_size -eq 0 ]; then
                # Calculate remainders
                width_remainder=$((width % grid_size))
                height_remainder=$((height % grid_size))
                total_remainder=$((width_remainder + height_remainder))
                
                # Check if this is better than our current best
                if [ $total_remainder -lt $min_remainder ] || [ $best_grid_size -eq 0 ]; then
                    min_remainder=$total_remainder
                    best_grid_size=$grid_size
                    
                    # If we found a perfect division, break early
                    if [ $total_remainder -eq 0 ]; then
                        break
                    fi
                fi
            fi
        fi
    done

    # Use the best grid size
    grid_size=$best_grid_size
    total_chunks=$((grid_size * grid_size))

    # Calculate chunk dimensions
    chunk_width=$((width / grid_size))
    chunk_height=$((height / grid_size))
    remainder_width=$((width % grid_size))
    remainder_height=$((height % grid_size))

    echo "Using grid size $grid_size x $grid_size ($total_chunks chunks)"
    echo "Base chunk size: $chunk_width x $chunk_height"
    if [ $remainder_width -gt 0 ] || [ $remainder_height -gt 0 ]; then
        echo "Note: Last row/column will adjust for remainders (width +$remainder_width, height +$remainder_height)"
    fi

    # Dynamically adjust max concurrent processes based on available memory
    local suggested_max_concurrent=$DEFAULT_MAX_CONCURRENT
    if [ -f /proc/meminfo ]; then
        available_memory=$(grep "MemAvailable" /proc/meminfo | awk '{print int($2/1024)}')
        # Roughly estimate how many concurrent processes we can handle
        # Each process might use max_chunk_size + overhead (50MB)
        local mem_per_process=$((max_chunk_size + 50))
        suggested_max_concurrent=$((available_memory / mem_per_process))
        
        # Sanity limits
        if [ $suggested_max_concurrent -lt 2 ]; then
            suggested_max_concurrent=2  # At least 2 processes
        elif [ $suggested_max_concurrent -gt 16 ]; then
            suggested_max_concurrent=16  # Max 16 processes
        fi
        
        echo "Memory-based concurrency: $suggested_max_concurrent processes"
    else
        echo "Using default concurrency: $suggested_max_concurrent processes"
    fi

    # Process chunks in batches
    echo "Processing chunks..."
    local max_concurrent=$suggested_max_concurrent
    local batch=0
    local has_failures=0

    # Use one loop to reduce total CPU load at any given time
    for row in $(seq 0 $((grid_size-1))); do
        for col in $(seq 0 $((grid_size-1))); do
            chunk_num=$((row * grid_size + col + 1))
            chunk_file="$temp_dir/chunk_${row}_${col}.tif"
            
            # Skip if the chunk was already processed successfully
            if [ -f "$chunk_file" ]; then
                echo "Chunk $chunk_num (${row}_${col}) already processed, skipping."
                continue
            fi
            
            # Calculate position and size for this chunk
            x_off=$((col * chunk_width))
            y_off=$((row * chunk_height))
            
            # Handle the last column/row which might be larger due to remainder
            this_chunk_width=$chunk_width
            this_chunk_height=$chunk_height
            
            if [ $col -eq $((grid_size-1)) ] && [ $remainder_width -gt 0 ]; then
                this_chunk_width=$((chunk_width + remainder_width))
            fi
            
            if [ $row -eq $((grid_size-1)) ] && [ $remainder_height -gt 0 ]; then
                this_chunk_height=$((chunk_height + remainder_height))
            fi
            
            # Create the command to run
            echo "Processing chunk $chunk_num (${row}_${col}): offset $x_off,$y_off size ${this_chunk_width}x${this_chunk_height}"
            
            # Determine compression type based on USE_WEBP setting
            local compress_opt="COMPRESS=DEFLATE"
            if [ "$USE_WEBP" = "YES" ]; then
                compress_opt="COMPRESS=WEBP"
            fi
            
            # Process the chunk (using simplified parameters to reduce memory pressure)
            (
                gdal_translate \
                  --config GDAL_CACHEMAX 16 \
                  --config CPL_VSIL_USE_TEMP_FILE_FOR_RANDOM_WRITE YES \
                  --config GDAL_DISABLE_READDIR_ON_OPEN TRUE \
                  --config VSI_CACHE TRUE \
                  --config VSI_CACHE_SIZE 500000 \
                  --config GDAL_MAX_DATASET_POOL_SIZE 1 \
                  --config GDAL_VRT_ENABLE_PYTHON NO \
                  --config GDAL_USE_MMAP NO \
                  --config VRT_SHARED_SOURCE NO \
                  --config GDAL_VRT_IGNORE_SOURCE_OVERVIEWS YES \
                  --config CPL_DISABLE_REMAP_MATRIX TRUE \
                  --config GDAL_ONE_BIG_READ NO \
                  -srcwin $x_off $y_off $this_chunk_width $this_chunk_height \
                  -of GTiff \
                  -co BIGTIFF=YES \
                  -co $compress_opt \
                  -co NUM_THREADS=1 \
                  -co SPARSE_OK=YES \
                  "$input_vrt" "$chunk_file" || { 
                      echo "Error processing chunk $chunk_num"; 
                      touch "$temp_dir/error_${row}_${col}"; 
                      exit 1; 
                  }
                
                echo "Completed chunk $chunk_num (${row}_${col})"
            ) &
            
            batch=$((batch + 1))
            
            # If we've reached max_concurrent jobs or the end, wait for all to complete
            if [ $batch -ge $max_concurrent ] || [ $chunk_num -eq $total_chunks ]; then
                echo "Waiting for batch to complete..."
                wait
                
                # Check for error files
                error_count=$(find "$temp_dir" -name "error_*" | wc -l)
                if [ $error_count -gt 0 ]; then
                    echo "Errors detected in this batch ($error_count chunks failed)."
                    has_failures=1
                fi
                
                batch=0
            fi
        done
    done

    # At this point, we've tried processing all chunks with our current approach
    # If there are failed chunks, try a more conservative approach
    if [ $has_failures -eq 1 ]; then
        echo "Some chunks failed to process with the parallel approach."
        echo "Switching to sequential processing for failed chunks..."
        
        # List all failed chunks
        failed_chunks=$(find "$temp_dir" -name "error_*" | sed 's/.*error_\(.*\)/\1/')
        
        # Process each failed chunk sequentially with extreme memory optimization
        for failed_chunk in $failed_chunks; do
            row=$(echo $failed_chunk | cut -d '_' -f1)
            col=$(echo $failed_chunk | cut -d '_' -f2)
            chunk_num=$((row * grid_size + col + 1))
            chunk_file="$temp_dir/chunk_${row}_${col}.tif"
            
            # Remove the error marker first
            rm -f "$temp_dir/error_${row}_${col}"
            
            # Calculate position and size for this chunk
            x_off=$((col * chunk_width))
            y_off=$((row * chunk_height))
            
            # Handle the last column/row which might be larger due to remainder
            this_chunk_width=$chunk_width
            this_chunk_height=$chunk_height
            
            if [ $col -eq $((grid_size-1)) ] && [ $remainder_width -gt 0 ]; then
                this_chunk_width=$((chunk_width + remainder_width))
            fi
            
            if [ $row -eq $((grid_size-1)) ] && [ $remainder_height -gt 0 ]; then
                this_chunk_height=$((chunk_height + remainder_height))
            fi
            
            echo "Retrying failed chunk $chunk_num (${row}_${col})..."
            
            # Determine compression type
            local compress_opt="COMPRESS=DEFLATE"
            if [ "$USE_WEBP" = "YES" ]; then
                compress_opt="COMPRESS=WEBP"
            fi
            
            # Try minimal memory usage approach
            gdal_translate \
              --config GDAL_CACHEMAX 8 \
              --config CPL_VSIL_USE_TEMP_FILE_FOR_RANDOM_WRITE YES \
              --config GDAL_DISABLE_READDIR_ON_OPEN TRUE \
              --config VSI_CACHE TRUE \
              --config VSI_CACHE_SIZE 100000 \
              --config GDAL_MAX_DATASET_POOL_SIZE 1 \
              --config GDAL_VRT_ENABLE_PYTHON NO \
              --config GDAL_USE_MMAP NO \
              --config VRT_SHARED_SOURCE NO \
              --config GDAL_VRT_IGNORE_SOURCE_OVERVIEWS YES \
              --config CPL_DISABLE_REMAP_MATRIX TRUE \
              --config GDAL_ONE_BIG_READ NO \
              -srcwin $x_off $y_off $this_chunk_width $this_chunk_height \
              -of GTiff \
              -co BIGTIFF=YES \
              -co $compress_opt \
              -co NUM_THREADS=1 \
              -co SPARSE_OK=YES \
              "$input_vrt" "$chunk_file" || {
                  echo "Failed to process chunk $chunk_num even with minimal memory settings."
                  touch "$temp_dir/error_${row}_${col}"
                  has_failures=1
              }
        done
        
        # Check if we still have failures
        error_count=$(find "$temp_dir" -name "error_*" | wc -l)
        if [ $error_count -gt 0 ]; then
            echo "Error: $error_count chunks still failed after sequential processing."
            return 1
        fi
        
        echo "All failed chunks were successfully processed."
    fi

    # Verify all chunks were created successfully
    expected_chunks=$total_chunks
    actual_chunks=$(find "$temp_dir" -name "chunk_*.tif" | wc -l)

    if [ $actual_chunks -ne $expected_chunks ]; then
        echo "Error: Expected $expected_chunks chunks but found $actual_chunks."
        echo "Some chunks may have failed to process."
        return 1
    fi

    echo "All chunks processed successfully. Creating VRT..."

    # Create a VRT from all chunks
    find "$temp_dir" -name "chunk_*.tif" | sort > "${temp_dir}/chunk_list.txt"
    gdalbuildvrt -input_file_list "${temp_dir}/chunk_list.txt" "${temp_dir}/chunks.vrt"

    # Check if VRT was created successfully
    if [ ! -f "${temp_dir}/chunks.vrt" ]; then
        echo "Error: Failed to create VRT from chunks."
        return 1
    fi
    
    return 0
}

# Function to process a single VRT file
process_vrt() {
    local input_vrt="$1"
    local output_cog="$2"
    local temp_dir="temp_chunks_$(basename "$input_vrt" .vrt)"
    local chunks_vrt="${temp_dir}/chunks.vrt"
    
    echo "==============================================="
    echo "Processing: $input_vrt -> $output_cog"
    echo "==============================================="
    
    # Create temp directory
    mkdir -p "$temp_dir"
    
    # Try to get available memory in MB (works on Linux)
    local available_memory=0
    if [ -f /proc/meminfo ]; then
        available_memory=$(grep "MemAvailable" /proc/meminfo | awk '{print int($2/1024)}')
        echo "Available memory: approximately ${available_memory}MB"
    else
        echo "Cannot determine available memory, using conservative settings"
    fi

    # Check for required tools
    for cmd in gdal_translate gdalinfo gdalbuildvrt; do
        if ! command_exists $cmd; then
            echo "Error: Required command '$cmd' not found. Please install GDAL."
            rm -rf "$temp_dir"
            return 1
        fi
    done

    # Get VRT dimensions using gdalinfo
    echo "Getting dimensions of $input_vrt..."
    size_output=$(gdalinfo "$input_vrt" | grep -E "^Size is")
    if [ $(echo "$size_output" | wc -l) -ne 1 ]; then
        echo "Error: Could not determine size of input VRT."
        rm -rf "$temp_dir"
        return 1
    fi
    width=$(echo "$size_output" | awk '{print $3}' | tr -d ',')
    height=$(echo "$size_output" | awk '{print $4}')
    echo "VRT dimensions: $width x $height"
    
    # Determine chunking strategy
    if [ "$CHUNKING_MODE" = "strips" ]; then
        # Use strips mode
        process_vrt_strips "$input_vrt" "$output_cog" "$temp_dir" "$width" "$height"
        process_result=$?
        
        if [ $process_result -ne 0 ]; then
            echo "Failed to process VRT using strips mode."
            rm -rf "$temp_dir"
            return 1
        fi
        
        # Set chunks_vrt for the next step
        chunks_vrt="${temp_dir}/strips.vrt"
    else
        # Use traditional grid mode
        process_vrt_grid "$input_vrt" "$output_cog" "$temp_dir" "$width" "$height"
        process_result=$?
        
        if [ $process_result -ne 0 ]; then
            echo "Failed to process VRT using grid mode."
            rm -rf "$temp_dir"
            return 1
        fi
        
        # Set chunks_vrt for the next step
        chunks_vrt="${temp_dir}/chunks.vrt"
    fi

    # Convert the VRT to the final COG with memory-optimized settings
    echo "Creating final COG..."

    # Use a two-step process to convert to COG
    # First, create an intermediate GeoTIFF
    echo "Step 1: Creating intermediate GeoTIFF..."
    intermediate_tiff="${output_cog}.tmp.tif"
    
    # Determine compression type
    local compress_opt="COMPRESS=DEFLATE"
    if [ "$USE_WEBP" = "YES" ]; then
        compress_opt="COMPRESS=WEBP"
    fi

    gdal_translate \
      --config GDAL_CACHEMAX $DEFAULT_GDAL_CACHEMAX \
      --config CPL_VSIL_USE_TEMP_FILE_FOR_RANDOM_WRITE YES \
      --config VSI_CACHE TRUE \
      --config VSI_CACHE_SIZE $DEFAULT_VSI_CACHE_SIZE \
      --config GDAL_MAX_DATASET_POOL_SIZE 4 \
      --config GDAL_ONE_BIG_READ NO \
      --config VRT_SHARED_SOURCE NO \
      --config GDAL_VRT_IGNORE_SOURCE_OVERVIEWS YES \
      --config CPL_DISABLE_REMAP_MATRIX TRUE \
      -of GTiff \
      -co BIGTIFF=YES \
      -co $compress_opt \
      -co NUM_THREADS=2 \
      -co SPARSE_OK=YES \
      "$chunks_vrt" "$intermediate_tiff"

    # Check if intermediate file was created successfully
    if [ ! -f "$intermediate_tiff" ]; then
        echo "Error: Failed to create intermediate GeoTIFF."
        rm -rf "$temp_dir"
        return 1
    fi

    # Then convert the intermediate file to COG
    echo "Step 2: Converting intermediate file to COG..."
    gdal_translate \
      --config GDAL_CACHEMAX $DEFAULT_GDAL_CACHEMAX \
      -of COG \
      -co BIGTIFF=YES \
      -co $compress_opt \
      -co NUM_THREADS=4 \
      -co OVERVIEW_RESAMPLING=NEAREST \
      -co SPARSE_OK=YES \
      "$intermediate_tiff" "$output_cog"

    # Remove the intermediate file
    rm -f "$intermediate_tiff"

    # Check if the final COG was created successfully
    if [ ! -f "$output_cog" ]; then
        echo "Error: Failed to create final COG."
        rm -rf "$temp_dir"
        return 1
    fi

    echo "Completed! Final COG is at $output_cog"

    # Clean up
    echo "Cleaning up temporary files..."
    rm -rf "$temp_dir"

    echo "Done processing $input_vrt"
    return 0
}

# Find all VRT files in the input directory
echo "Looking for VRT files in $input_dir..."
vrt_files=$(find "$input_dir" -maxdepth 1 -name "*.vrt" -type f)
vrt_count=$(echo "$vrt_files" | grep -c "^" || echo 0)

if [ "$vrt_count" -eq 0 ]; then
    echo "No VRT files found in $input_dir"
    exit 1
fi

echo "Found $vrt_count VRT les to process"

# Process each VRT file
for vrt_file in $vrt_files; do
    # Get the filename without path
    vrt_basename=$(basename "$vrt_file")
    
    # Create output filename with .tif extension
    output_basename="${vrt_basename%.vrt}.tif"
    output_file="$output_dir/$output_basename"
    
    # Process this VRT file
    process_vrt "$vrt_file" "$output_file"
done

echo "All VRT files have been processed successfully!"
