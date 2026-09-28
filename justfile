set shell := ["bash", "-euo", "pipefail", "-c"]
set quiet
set fallback
set default-list

script := justfile_directory() / "timelapse.py"

alias run := watch

# Daemon: watch for print jobs, generate one mp4 per print
watch *args:
    "{{ script }}" watch {{ args }}

# Grab frames now. --interval N for time based, default per layer via PrusaLink
capture *args:
    "{{ script }}" capture {{ args }}

# Render frames to mp4. --gif for a gif
[no-cd]
render dir *args:
    "{{ script }}" render "{{ dir }}" {{ args }}

# Print PrusaLink status
status:
    "{{ script }}" status

# Run ruff format, ruff check --fix, and mypy
[group('linting')]
fmt: format (check "--fix") mypy && fmt-done

# Run formatters and linters in check mode
[group('linting')]
fmt-check: (format "--check") check mypy && check-done

[private]
format *args="":
    uvx ruff format {{ args }} "{{ script }}"

[private]
check *args="":
    uvx ruff check {{ args }} "{{ script }}"

# mypy against the script's own uv environment, so numpy and cv2 resolve
[private]
mypy:
    uv sync --script "{{ script }}" --quiet
    uvx mypy --python-executable "$(uv python find --script "{{ script }}")" "{{ script }}"

[private]
fmt-done:
    echo '{{ GREEN }}Formatting complete{{ NORMAL }}'

[private]
check-done:
    echo '{{ GREEN }}All checks passed{{ NORMAL }}'
