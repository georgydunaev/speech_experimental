import os
import subprocess
import unittest
import xml.etree.ElementTree as ET


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ANDROID_DIR = os.path.join(REPO_ROOT, "android_recorder")
MANIFEST_PATH = os.path.join(ANDROID_DIR, "app", "src", "main", "AndroidManifest.xml")
JAVA_DIR = os.path.join(ANDROID_DIR, "app", "src", "main", "java", "com", "speech", "recorder")


class TestAndroidMiuiRecorder(unittest.TestCase):
    def test_manifest_permissions_and_foreground_service(self):
        self.assertTrue(os.path.isfile(MANIFEST_PATH), "AndroidManifest.xml must exist")
        tree = ET.parse(MANIFEST_PATH)
        root = tree.getroot()
        android_ns = "{http://schemas.android.com/apk/res/android}"

        permissions = {
            elem.attrib.get(f"{android_ns}name")
            for elem in root.findall("uses-permission")
        }
        required_permissions = {
            "android.permission.RECORD_AUDIO",
            "android.permission.INTERNET",
            "android.permission.ACCESS_NETWORK_STATE",
            "android.permission.WAKE_LOCK",
            "android.permission.FOREGROUND_SERVICE",
            "android.permission.FOREGROUND_SERVICE_MICROPHONE",
            "android.permission.POST_NOTIFICATIONS",
            "android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS",
        }
        self.assertTrue(
            required_permissions.issubset(permissions),
            f"Missing required permissions: {required_permissions - permissions}",
        )

        app_elem = root.find("application")
        self.assertIsNotNone(app_elem)
        service_elem = app_elem.find("service")
        self.assertIsNotNone(service_elem)
        self.assertEqual(service_elem.attrib.get(f"{android_ns}name"), ".AudioForegroundService")
        self.assertEqual(service_elem.attrib.get(f"{android_ns}foregroundServiceType"), "microphone")

    def test_miui_optimization_helper_intents(self):
        helper_path = os.path.join(JAVA_DIR, "MiuiOptimizationHelper.kt")
        self.assertTrue(os.path.isfile(helper_path))
        with open(helper_path, "r", encoding="utf-8") as f:
            src = f.read()

        self.assertIn("com.miui.securitycenter", src)
        self.assertIn("com.miui.permcenter.autostart.AutoStartManagementActivity", src)
        self.assertIn("com.miui.powerkeeper", src)
        self.assertIn("com.miui.powerkeeper.ui.HiddenAppsConfigActivity", src)
        self.assertIn("ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS", src)

    def test_audio_chunk_buffer_and_foreground_service_contract(self):
        buf_path = os.path.join(JAVA_DIR, "AudioChunkBuffer.kt")
        svc_path = os.path.join(JAVA_DIR, "AudioForegroundService.kt")
        with open(buf_path, "r", encoding="utf-8") as f:
            buf_src = f.read()
        with open(svc_path, "r", encoding="utf-8") as f:
            svc_src = f.read()

        self.assertIn("AudioChunkDbHelper", buf_src)
        self.assertIn("persistChunk", buf_src)
        self.assertIn("markChunkSent", buf_src)
        self.assertIn("markChunkRetry", buf_src)

        self.assertIn("FOREGROUND_SERVICE_TYPE_MICROPHONE", svc_src)
        self.assertIn("PARTIAL_WAKE_LOCK", svc_src)
        self.assertIn("const val SAMPLE_RATE = 16000", svc_src)
        self.assertIn("const val CHUNK_MS = 250", svc_src)
        self.assertIn("/api/upload_chunk", svc_src)
        self.assertIn("X-Session-ID", svc_src)
        self.assertIn("X-Sequence-ID", svc_src)
        self.assertIn("X-Client-ID", svc_src)

    def test_gradle_wrapper_and_provisioning_script_dry_run(self):
        gradlew_path = os.path.join(ANDROID_DIR, "gradlew")
        props_path = os.path.join(ANDROID_DIR, "gradle", "wrapper", "gradle-wrapper.properties")
        prov_script = os.path.join(ANDROID_DIR, "build_and_provision_miui.sh")

        self.assertTrue(os.path.isfile(gradlew_path))
        self.assertTrue(os.path.isfile(props_path))
        self.assertTrue(os.path.isfile(prov_script))

        res = subprocess.run(
            ["bash", prov_script, "--dry-run"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("assembleDebug", res.stdout)
        self.assertIn("adb install -r", res.stdout)
        self.assertIn("AutoStartManagementActivity", res.stdout)
        self.assertIn("HiddenAppsConfigActivity", res.stdout)

    def test_web_client_css_class_scope_isolation(self):
        auditor = "/botworkspace/tools/css_class_scope_auditor.py"
        res = subprocess.run(
            ["python3", auditor, "--strict", REPO_ROOT],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, f"CSS scoping violations found:\n{res.stdout}\n{res.stderr}")
        self.assertIn("found 0 violation(s)", res.stdout)


if __name__ == "__main__":
    unittest.main()
