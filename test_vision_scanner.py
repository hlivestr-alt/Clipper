import types
import unittest
from unittest import mock

from vision_scanner import _resolve_yolo_predict_options


class VisionScannerDeviceTests(unittest.TestCase):
    def test_cuda_config_falls_back_to_cpu_when_torch_has_no_cuda(self):
        cfg = types.SimpleNamespace(YOLO_DEVICE="0", YOLO_HALF=True, YOLO_IMGSZ=640)
        fake_torch = types.SimpleNamespace(
            cuda=types.SimpleNamespace(
                is_available=lambda: False,
                device_count=lambda: 0,
            )
        )

        with mock.patch.dict("sys.modules", {"torch": fake_torch}):
            options = _resolve_yolo_predict_options(cfg)

        self.assertEqual(options["device"], "cpu")
        self.assertFalse(options["half"])
        self.assertEqual(options["imgsz"], 640)

    def test_valid_cuda_config_is_preserved_when_available(self):
        cfg = types.SimpleNamespace(YOLO_DEVICE="0", YOLO_HALF=True, YOLO_IMGSZ=640)
        fake_torch = types.SimpleNamespace(
            cuda=types.SimpleNamespace(
                is_available=lambda: True,
                device_count=lambda: 1,
            )
        )

        with mock.patch.dict("sys.modules", {"torch": fake_torch}):
            options = _resolve_yolo_predict_options(cfg)

        self.assertEqual(options["device"], "0")
        self.assertTrue(options["half"])


if __name__ == "__main__":
    unittest.main()
