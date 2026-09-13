#!/bin/bash
# Запускает maybe_compile.py для каждого проекта в CMC_PROJECT_ROOTS.
# Вызывается launchd в 4:00 (или при пробуждении если пропустил).
# Безопасно вызывать несколько раз — maybe_compile.py идемпотентен.

TOOL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_ROOTS="${CMC_PROJECT_ROOTS:-}"

# launchd не грузит пользовательский PATH — добавляем вручную
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
UV="$(command -v uv)"

if [ -z "$PROJECT_ROOTS" ]; then
    echo "CMC_PROJECT_ROOTS is empty" >&2
    exit 64
fi

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*"
}

log "compile-all-projects.sh started"

old_ifs=$IFS
IFS=:
for project_root in $PROJECT_ROOTS; do
  for project_dir in "$project_root"/*/; do
    [ -d "$project_dir/wiki" ] || continue

    project_name=$(basename "$project_dir")
    log "Checking: $project_name"

    CMC_PROJECT_DIR="$project_dir" \
    CLAUDE_INVOKED_BY="" \
        "$UV" run --directory "$TOOL_ROOT" python "$TOOL_ROOT/scripts/maybe_compile.py" \
        >> "$project_dir/.cmc/compile.log" 2>&1

    status=$?
    log "  → exit $status"
  done
done
IFS=$old_ifs

log "compile-all-projects.sh done"
