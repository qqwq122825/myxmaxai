"""Bundled-Python tests of output backend and Qt panel; no GPU inference/devices."""
import importlib
import os
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
SOURCE = Path(os.environ.get('FACELIVE_DFL_SOURCE', 'D:/DeepFaceLive_Official/DeepFaceLive_NVIDIA/_internal/DeepFaceLive'))
sys.path.insert(0, str(SOURCE))
import numpy as np
from xlib import qt as qtx
from xlib.mp import csw
backend = importlib.import_module('apps.DeepFaceLive.backend.StreamOutput')
from apps.DeepFaceLive.ui.QStreamOutput import QStreamOutput


class Camera:
    def __init__(self):
        self.active = False
        self.status = 'stopped'
        self.images = []
    def start(self): self.active = True
    def stop(self): self.active = False
    def clear(self): self.images.clear()
    def submit(self, image): self.images.append(image.copy())


class Data:
    def __init__(self, image): self.image = image
    def assign_weak_heap(self, heap): pass
    def get_frame_num(self): return 1
    def get_frame_timestamp(self): return time.time()
    def get_merged_image_name(self): return 'merged'
    def get_image(self, name): return self.image


class Connection:
    def __init__(self): self.item = None
    def read(self, **kwargs):
        item, self.item = self.item, None
        return item


class Buffered:
    def __init__(self): self.image = None
    def set_target_delay(self, value): pass
    def add_buffer(self, timestamp, image): self.image = image
    def process(self): return types.SimpleNamespace(new_data=self.image)


class Tests(unittest.TestCase):
    def make_worker(self):
        worker = backend.StreamOutputWorker.__new__(backend.StreamOutputWorker)
        state, sheet = backend.WorkerState(), backend.Sheet.Worker()
        worker.get_state = lambda: state
        worker.get_control_sheet = lambda: sheet
        worker.save_state = lambda: None
        worker.reemit_frame_signal = types.SimpleNamespace(send=lambda: None)
        connection = Connection()
        worker.on_start(None, worker.reemit_frame_signal, connection, None)
        sheet.source_type.select(backend.SourceType.MERGED_FRAME)
        return worker, state, sheet, connection

    def test_camera_is_independent_of_show_window_and_stops_without_stopping_input(self):
        with patch.object(backend, 'ResultCamera', Camera), patch.object(backend.lib_logic, 'DelayedBuffers', Buffered), patch.object(backend.cv2, 'imshow') as show:
            worker, state, sheet, connection = self.make_worker()
            try:
                self.assertFalse(worker._virtual_requested)
                sheet.start_virtual_camera.signal()
                self.assertTrue(worker._virtual_requested)
                self.assertEqual(sheet.start_virtual_camera.get_state(), csw.Control.State.FREEZED)
                self.assertFalse(state.is_showing_window)
                image = np.full((32, 48, 3), .4, np.float32)
                connection.item = Data(image)
                worker.on_tick()
                self.assertEqual(len(worker._result_camera.images), 1)
                self.assertTrue(np.array_equal(worker._result_camera.images[0], image))
                self.assertFalse(show.called)
                sheet.stop_virtual_camera.signal()
                self.assertFalse(worker._virtual_requested)
                connection.item = Data(image)
                worker.on_tick()
                self.assertIsNone(connection.item)  # Backend keeps consuming input.
            finally:
                worker.on_stop()

    def test_missing_driver_is_reported_and_start_can_be_retried(self):
        class Missing(Camera):
            def start(self): raise RuntimeError('missing Camera #3')
        with patch.object(backend, 'ResultCamera', Missing):
            worker, state, sheet, _ = self.make_worker()
            try:
                sheet.start_virtual_camera.signal()
                self.assertFalse(worker._virtual_requested)
                self.assertIn('missing Camera #3', sheet.virtual_camera_status.get_text())
                self.assertEqual(sheet.start_virtual_camera.get_state(), csw.Control.State.ENABLED)
            finally:
                worker.on_stop()

    def test_stale_window_is_cleared_instead_of_holding_old_face(self):
        with patch.object(backend, 'ResultCamera', Camera), patch.object(backend.cv2, 'imshow') as show, patch.object(backend.cv2, 'waitKey'):
            worker, state, _, _ = self.make_worker()
            try:
                state.is_showing_window = True
                worker._last_view_at = time.monotonic() - 2
                worker._last_view_shape = (32, 48)
                worker.on_tick()
                self.assertTrue(show.called)
                self.assertEqual(float(show.call_args[0][1].mean()), 0)
            finally:
                worker.on_stop()

    def test_qt_panel_contains_two_camera_buttons(self):
        app = qtx.QXMainApplication()
        host = backend.StreamOutput(None, None, None)
        panel = QStreamOutput(host)
        from PyQt6.QtWidgets import QPushButton
        labels = [button.text() for button in panel.findChildren(QPushButton)]
        self.assertIn('启动虚拟摄像头', labels)
        self.assertIn('关闭虚拟摄像头', labels)
        sheet = host.get_control_sheet()
        sheet.start_virtual_camera._set_state(csw.Control.State.ENABLED)
        sheet.stop_virtual_camera._set_state(csw.Control.State.FREEZED)
        sheet.virtual_camera_status._set_state(csw.Control.State.ENABLED)
        panel._on_backend_state_change(host, True, False, False, False, False)
        window = qtx.QXWindow(layout=qtx.QXVBoxLayout([panel]))
        window.resize(360, 400)
        window.show()
        app.processEvents()
        buttons = {button.text(): button for button in panel.findChildren(QPushButton)}
        self.assertTrue(buttons['启动虚拟摄像头'].isVisible())
        self.assertTrue(buttons['启动虚拟摄像头'].isEnabled())
        self.assertTrue(buttons['关闭虚拟摄像头'].isVisible())
        self.assertFalse(buttons['关闭虚拟摄像头'].isEnabled())
        window.close()
        app.quit()


if __name__ == '__main__':
    unittest.main()
