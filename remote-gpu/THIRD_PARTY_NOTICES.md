# Third-party components

This prototype does not include DeepLiveCam Pro, DeepFaceLive, their models or licenses. They are installed and operated separately on the rented computer.

The FaceLive DirectShow driver derives from Unity Capture by Bernhard Schelling and UnityCam by MHD Yamen Saraiji, under MIT. Its notice is preserved in `VirtualCam/LICENSE-UnityCaptureFilter.txt`; protocol references are in the repository's `vcam/shared.inl`. This prototype uses the existing driver's additional camera channel, without modifying third-party face-swap applications.

Transport uses aiortc (BSD-3-Clause), PyAV (BSD), aiohttp (Apache-2.0), NumPy (BSD) and OpenCV (Apache-2.0), plus their dependencies. Actual dependency metadata and installed license files are copied to `licenses/` during packaging. Tk/Tcl and Python notices are included in the packaged runtime.

Selected-window capture uses [windows-capture](https://github.com/NiiightmareXD/windows-capture) (MIT), through Windows Graphics Capture, or the Windows PrintWindow API for the compatibility option. Its native extension runs in a child process. No desktop fallback is performed.

PyAV wheels contain FFmpeg libraries and this prototype uses libx264. The wheel's FFmpeg component configuration and license notices must be considered alongside PyAV's own license. Upstream source and build resources:

- [PyAV source](https://github.com/PyAV-Org/PyAV)
- [PyAV FFmpeg build infrastructure](https://github.com/PyAV-Org/pyav-ffmpeg)
- [FFmpeg licensing and source](https://ffmpeg.org/legal.html)
- [x264 source](https://code.videolan.org/videolan/x264)
- [aiortc source](https://github.com/aiortc/aiortc)
- [Unity Capture source](https://github.com/schellingb/UnityCapture)

The generated folder is a local testing artifact. Before public binary distribution, verify the exact bundled FFmpeg build and provide the corresponding source, license texts and other required distribution materials. Do not assume collecting Python package notices alone fulfills every bundled native component's requirements.
