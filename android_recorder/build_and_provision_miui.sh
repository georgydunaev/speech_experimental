#!/usr/bin/env bash
# build_and_provision_miui.sh — Host APK Build & Xiaomi Redmi Note 8 (MIUI / HyperOS) Provisioning Helper
#
# Usage:
#   bash android_recorder/build_and_provision_miui.sh [--dry-run] [--skip-build] [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_NAME="com.speech.recorder"
APK_REL_PATH="app/build/outputs/apk/debug/app-debug.apk"
APK_PATH="${SCRIPT_DIR}/${APK_REL_PATH}"

DRY_RUN=0
SKIP_BUILD=0

for arg in "$@"; do
  case "${arg}" in
    --dry-run)
      DRY_RUN=1
      ;;
    --skip-build)
      SKIP_BUILD=1
      ;;
    -h|--help)
      echo "Usage: bash $0 [--dry-run] [--skip-build] [--help]"
      echo ""
      echo "Steps performed:"
      echo "  1. Builds Debug APK via ./gradlew assembleDebug in ${SCRIPT_DIR}"
      echo "  2. Verifies USB-connected Xiaomi Redmi Note 8 via adb devices"
      echo "  3. Installs ${APK_REL_PATH} via adb install -r"
      echo "  4. Grants RECORD_AUDIO and POST_NOTIFICATIONS runtime permissions"
      echo "  5. Whitelists ${PACKAGE_NAME} in Doze / DeviceIdle (dumpsys deviceidle whitelist)"
      echo "  6. Launches MIUI Autostart and PowerKeeper ('No restrictions') settings activities"
      exit 0
      ;;
  esac
done

run_cmd() {
  if [ "${DRY_RUN}" -eq 1 ]; then
    echo "[DRY-RUN] $*"
  else
    echo "[EXEC] $*"
    "$@"
  fi
}

echo "=== Step 1: Building Speech Foreground Recorder APK ==="
if [ "${SKIP_BUILD}" -eq 0 ]; then
  run_cmd bash "${SCRIPT_DIR}/gradlew" assembleDebug
else
  echo "[SKIP] Skipping Gradle build (--skip-build)"
fi

echo "=== Step 2: Checking ADB Device Connection ==="
if [ "${DRY_RUN}" -eq 0 ]; then
  if ! command -v adb >/dev/null 2>&1; then
    echo "[ERROR] 'adb' not found on PATH. Install android-tools-adb on the host." >&2
    exit 1
  fi
  ADB_DEVICES="$(adb devices | awk 'NR>1 && $2=="device" {print $1}')"
  if [ -z "${ADB_DEVICES}" ]; then
    echo "[WARN] No USB-connected Android device in 'device' state detected." >&2
    echo "Connect the Xiaomi Redmi Note 8 via USB with USB Debugging enabled and re-run." >&2
    exit 2
  fi
fi

echo "=== Step 3: Installing APK onto Xiaomi Redmi Note 8 ==="
run_cmd adb install -r "${APK_PATH}"

echo "=== Step 4: Granting Runtime Microphone & Notification Permissions ==="
run_cmd adb shell pm grant "${PACKAGE_NAME}" android.permission.RECORD_AUDIO || true
run_cmd adb shell pm grant "${PACKAGE_NAME}" android.permission.POST_NOTIFICATIONS || true
run_cmd adb shell appops set "${PACKAGE_NAME}" RECORD_AUDIO allow || true
run_cmd adb shell appops set "${PACKAGE_NAME}" RUN_IN_BACKGROUND allow || true
run_cmd adb shell appops set "${PACKAGE_NAME}" RUN_ANY_IN_BACKGROUND allow || true

echo "=== Step 5: Whitelisting Package from Doze & MIUI PowerKeeper ==="
run_cmd adb shell dumpsys deviceidle whitelist "+${PACKAGE_NAME}" || true

echo "=== Step 6: Opening MIUI Autostart & PowerKeeper Settings on Device ==="
run_cmd adb shell am start -n com.miui.securitycenter/com.miui.permcenter.autostart.AutoStartManagementActivity || true
run_cmd adb shell am start -n com.miui.powerkeeper/com.miui.powerkeeper.ui.HiddenAppsConfigActivity \
  --es package_name "${PACKAGE_NAME}" \
  --es package_label "Speech Foreground Recorder" || true

echo "=== Provisioning Complete ==="
echo "On the Xiaomi Redmi Note 8 screen, confirm:"
echo "  1. Autostart is toggled ON for 'Speech Foreground Recorder'"
echo "  2. Battery Saver is set to 'No restrictions'"
echo "  3. In Recent Apps, long-press the card and tap the Padlock icon"
