#!/bin/bash
# batch_adjudicate.sh - Batch adjudicate permits for single-labeler phase
#
# Usage: ./batch_adjudicate.sh <adjudicator_name>
#
# Example: ./batch_adjudicate.sh ton-adjudicator
#
# This script:
# - Finds all permits that need adjudication (adjudicated_by is "-" or empty)
# - Adjudicates each permit using the provided adjudicator name
# - Logs errors and continues with the next permit
# - Shows progress to console

set -o pipefail

# Check if adjudicator name is provided
if [ -z "$1" ]; then
    echo "Error: Adjudicator name is required"
    echo "Usage: $0 <adjudicator_name>"
    echo "Example: $0 ton-adjudicator"
    exit 1
fi

ADJUDICATOR_NAME="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/../logs"
LOG_FILE="$LOG_DIR/batch_adjudicate_$(date +%Y%m%d_%H%M%S).log"

# Create log directory if it doesn't exist
mkdir -p "$LOG_DIR"

# Counters
TOTAL=0
PROCESSED=0
FAILED=0
SKIPPED=0

log() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE"
}

log_error() {
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $1"
    echo "$msg" >&2
    echo "$msg" >> "$LOG_FILE"
}

# Get the list of permits that need adjudication
# Filter for rows where adjudicated_by is "-" (empty/unadjudicated)
PERMITS=$(pts label list 2>/dev/null | awk 'NR>1 && $3 == "-" {print $1}')

if [ -z "$PERMITS" ]; then
    log "No permits found that need adjudication (adjudicated_by is '-')"
    exit 0
fi

# Count total permits
TOTAL=$(echo "$PERMITS" | wc -l)
log "Found $TOTAL permits that need adjudication"
log "Starting batch adjudication as '$ADJUDICATOR_NAME'"
log "----------------------------------------"

# Process each permit
while IFS= read -r permit; do
    # Skip empty lines
    if [ -z "$permit" ]; then
        continue
    fi

    ((PROCESSED++))
    echo -n "[$PROCESSED/$TOTAL] Adjudicating $permit... "

    # Run the adjudicate command
    OUTPUT=$(pts label judge -p "$permit" \
        --labeler "$ADJUDICATOR_NAME" \
        --note "Batch adjudication - single labeler phase" \
        --single-labeler 2>&1)

    if [ $? -eq 0 ]; then
        echo "✓ OK"
    else
        echo "✗ FAILED"
        log_error "Failed to adjudicate $permit: $OUTPUT"
        ((FAILED++))
    fi
done <<< "$PERMITS"

# Summary
echo ""
echo "----------------------------------------"
echo "Batch adjudication complete!"
echo "  Total found:    $TOTAL"
echo "  Processed:      $PROCESSED"
echo "  Successful:     $((PROCESSED - FAILED))"
echo "  Failed:         $FAILED"
echo ""
echo "Log file: $LOG_FILE"

# Exit with appropriate code
if [ $FAILED -gt 0 ]; then
    log_error "Batch completed with $FAILED failure(s). Please review the log file."
    exit 1
else
    log "Batch completed successfully!"
    exit 0
fi