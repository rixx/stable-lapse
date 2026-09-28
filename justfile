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
