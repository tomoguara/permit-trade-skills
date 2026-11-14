#!/bin/bash

# pgvector Management Script
# Manages PostgreSQL with pgvector extension via Docker Compose

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
CONTAINER_NAME="permit-trade-skills-db"
DB_NAME="permit_trade_skills"
DB_USER="postgres"
DB_PASSWORD="postgres"
DB_PORT="5432"
VOLUME_NAME="permit_trade_skills_data"

# Get the absolute path to the script's directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.yml"

log_info() {
    echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"
}

log_error() {
    echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1" >&2
}

# Check if Docker is running
check_docker() {
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed or not in PATH"
        exit 1
    fi

    if ! docker info &> /dev/null; then
        log_error "Docker daemon is not running"
        exit 1
    fi
}

# Check if docker-compose is available
check_docker_compose() {
    if ! command -v docker compose &> /dev/null; then
        if command -v docker-compose &> /dev/null; then
            alias docker compose="docker-compose"
        else
            log_error "Docker Compose is not installed or not in PATH"
            exit 1
        fi
    fi
}

# Check if project root has docker-compose.yml
check_project_files() {
    if [ ! -f "$COMPOSE_FILE" ]; then
        log_error "docker-compose.yml not found at: $COMPOSE_FILE"
        log_error "Make sure you're running this script from the project root"
        exit 1
    fi
}

# Check if container is already running
container_running() {
    docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"
}

# Check if container exists (stopped or running)
container_exists() {
    docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"
}

# Check if volume exists
volume_exists() {
    docker volume ls --format '{{.Name}}' | grep -q "^${VOLUME_NAME}$"
}

# Start the pgvector container
start() {
    log_info "Starting pgvector container..."

    # Check if container already exists
    if container_running; then
        log_warn "Container ${CONTAINER_NAME} is already running"
        echo "Database URL: postgresql://${DB_USER}:${DB_PASSWORD}@localhost:${DB_PORT}/${DB_NAME}"
        return 0
    fi

    if container_exists; then
        log_info "Starting existing container..."
        cd "$SCRIPT_DIR"
        docker compose up -d
    else
        log_info "Creating new container..."
        cd "$SCRIPT_DIR"
        docker compose up -d
    fi

    # Wait for PostgreSQL to be ready
    log_info "Waiting for PostgreSQL to be ready..."
    wait_for_postgres

    # Create vector extension
    log_info "Creating vector extension..."
    create_vector_extension

    log_info "pgvector container started successfully!"
    echo ""
    echo "Database URL: ${GREEN}postgresql://${DB_USER}:${DB_PASSWORD}@localhost:${DB_PORT}/${DB_NAME}${NC}"
    echo ""
    echo "Usage example:"
    echo "  export PERMIT_TRADE_SKILLS_DB_URL=\"postgresql://${DB_USER}:${DB_PASSWORD}@localhost:${DB_PORT}/${DB_NAME}\""
    echo "  pts --help"
}

# Stop and remove the container
stop() {
    log_info "Stopping pgvector container..."

    if ! container_exists; then
        log_warn "Container ${CONTAINER_NAME} does not exist"
        return 0
    fi

    cd "$SCRIPT_DIR"
    docker compose down

    log_info "Container stopped and removed."
}

# Stop, remove container and volume (destroys all data)
clean() {
    log_warn "This will permanently delete all PostgreSQL data!"
    read -p "Are you sure? Type 'yes' to confirm: " CONFIRM

    if [ "$CONFIRM" != "yes" ]; then
        log_info "Operation cancelled"
        return 0
    fi

    log_info "Stopping and removing container and volume..."

    if container_exists; then
        cd "$SCRIPT_DIR"
        docker compose down -v

        if volume_exists; then
            log_info "Removing volume..."
            docker volume rm "$VOLUME_NAME"
        fi
    else
        log_warn "Container does not exist, nothing to clean"
    fi

    log_info "pgvector container and volume removed."
}

# Check if PostgreSQL is ready
wait_for_postgres() {
    local max_attempts=30
    local attempt=0

    while [ $attempt -lt $max_attempts ]; do
        if docker exec "$CONTAINER_NAME" pg_isready -U "$DB_USER" -q 2>/dev/null; then
            log_info "PostgreSQL is ready!"
            return 0
        fi

        attempt=$((attempt + 1))
        sleep 1
    done

    log_error "PostgreSQL failed to become ready after ${max_attempts} seconds"
    exit 1
}

# Create vector extension
create_vector_extension() {
    # Check if extension already exists
    local extension_exists
    extension_exists=$(docker exec "$CONTAINER_NAME" psql -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT 1 FROM pg_extension WHERE extname='vector';" 2>/dev/null | tr -d ' ')

    if [ "$extension_exists" = "1" ]; then
        log_info "Vector extension already exists"
        return 0
    fi

    docker exec "$CONTAINER_NAME" psql -U "$DB_USER" -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS vector;"
    log_info "Vector extension created successfully"
}

# Check status
status() {
    echo "pgvector Container Status"
    echo "========================"
    echo ""

    if container_running; then
        echo -e "Container: ${GREEN}Running${NC}"
        echo ""
        echo "Container Details:"
        docker inspect --format '{{.State.Status}} - Started: {{.State.StartedAt}}' "$CONTAINER_NAME" 2>/dev/null || true
        echo ""
        echo "Database URL: postgresql://${DB_USER}:${DB_PASSWORD}@localhost:${DB_PORT}/${DB_NAME}"
        echo ""
        echo "Vector Extension:"
        docker exec "$CONTAINER_NAME" psql -U "$DB_USER" -d "$DB_NAME" -c "\dx vector" 2>/dev/null || echo "Not installed"
    else
        if container_exists; then
            echo -e "Container: ${YELLOW}Stopped${NC}"
            docker ps -a --filter "name=${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
        else
            echo -e "Container: ${RED}Not found${NC}"
            echo "Run '$0 --start' to create and start the container"
        fi
    fi
}

# Show usage
usage() {
    echo "Usage: $0 [COMMAND]"
    echo ""
    echo "Commands:"
    echo "  --start    Start pgvector container with vector extension"
    echo "  --stop     Stop and remove pgvector container"
    echo "  --clean    Stop, remove container and volume (destroys all data)"
    echo "  --status   Show container status"
    echo "  --help     Show this help message"
    echo ""
    echo "Example:"
    echo "  $0 --start"
    echo "  export PERMIT_TRADE_SKILLS_DB_URL=\"postgresql://${DB_USER}:${DB_PASSWORD}@localhost:${DB_PORT}/${DB_NAME}\""
}

# Main
main() {
    check_docker
    check_docker_compose
    check_project_files

    case "${1:-}" in
        --start)
            start
            ;;
        --stop)
            stop
            ;;
        --clean)
            clean
            ;;
        --status)
            status
            ;;
        --help|-h)
            usage
            ;;
        *)
            log_error "Unknown command: ${1:-}"
            usage
            exit 1
            ;;
    esac
}

main "$@"
