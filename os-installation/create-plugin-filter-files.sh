#!/bin/bash

# Function to display usage
usage() {
  echo "Usage: $0 [--device | -d sensoredge] [-h | --help]"
  exit 1
}

# Parse arguments
DEVICE=""
while [[ "$#" -gt 0 ]]; do
  case $1 in
    --device|-d) DEVICE="$2"; shift ;;
    --help|-h) usage ;;
    *) usage ;;
  esac
  shift
done

# Define the path variable where the cockpit plugin filter files should be located
PLUGIN_FILTER_PATH="/etc/cockpit/"

# Check if the directory PLUGIN_FILTER_PATH exists, if not create it
if [ ! -d "$PLUGIN_FILTER_PATH" ]; then
  sudo mkdir -p "$PLUGIN_FILTER_PATH"
fi

# List of original cockpit plugins that should be filtered
plugins=("apps" "packagekit" "playground" "sosreport" "storaged")

# Add sensor edge specific plugins to the list
if [ "$DEVICE" == "sensoredge" ]; then
   # Plugins that are shall not be available on sensor edge devices
  sensoredge_plugins=("docker" "generalSettings" "iotedge-docker" "networkservices" "onboard")
  plugins+=("${sensoredge_plugins[@]}")
fi

# Iterate through the list of plugin names
for plugin in "${plugins[@]}"; do
  # Create a file with the name pattern <pluginname>.override.json
  file_path="${PLUGIN_FILTER_PATH}${plugin}.override.json"
  echo '{ "menu": null, "tools": null }' | sudo tee "$file_path" > /dev/null
done