FaceLive runs FFmpeg as a separate process for DirectShow camera capture.

This local build includes FFmpeg 4.4-full_build-www.gyan.dev, copied from
the user's existing DeepFaceLive installation. It is licensed under GPL
version 3 or later. See LICENSE-FFmpeg-GPL-3.0.txt.

FFmpeg project and source: https://ffmpeg.org/
FFmpeg 4.4 source: https://ffmpeg.org/releases/ffmpeg-4.4.tar.xz
Windows build provider: https://www.gyan.dev/ffmpeg/builds/
Build configuration and third-party library details: ffmpeg.exe -version
License notice: ffmpeg.exe -L

For a source build, place a Windows x64 FFmpeg executable here as ffmpeg.exe.
It must include the dshow input, scale and pad filters, and rawvideo output.
