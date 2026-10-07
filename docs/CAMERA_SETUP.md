# Practice camera setup before commissioning

For a Panasonic pan/tilt/zoom installation, confirm the exact model, firmware, outputs and installed settings before choosing an input route. Camera support remains unqualified. A front photograph does not establish RTSP, USB video or capture-card compatibility; do not choose a device profile from appearance.

## Select the input route

| Confirmed camera capability | Hub route | What remains to verify |
| --- | --- | --- |
| RTSP video stream | Private `FFmpegConfig` with `input_format: rtsp` | Exact model/firmware stream URL, authentication, codec, resolution/rate and reception on the selected practice computer |
| HDMI video output | Compatible HDMI capture device exposed through Windows DirectShow or Linux V4L2 | Capture hardware, supported camera output format, device identity and actual FFmpeg capture |
| SDI video output | Compatible SDI capture device with a supported computer input route | SDI signal format, capture driver and actual FFmpeg capture; the hub has no native SDI device adapter |
| USB connector | Direct capture only if that model exposes a supported video device | A USB connector may serve another purpose; confirm video operation in the model manual |

Panasonic's [AW-HE40H specifications](https://eu.connect.panasonic.com/gb/en/broadcast-proav/aw-he40h) list H.264 and RTSP support. Its [HE40 upgrade announcement](https://pro-av.panasonic.net/en/sales_o/news_info/news2015/20150402_02.html) describes the firmware addition of direct IP streaming and distinguishes HDMI and SDI versions. Panasonic's [HE60 announcement](https://pro-av.panasonic.net/en/sales_o/news_info/news120910/02.html) likewise distinguishes HDMI and SDI variants. These are examples of why the exact model and firmware matter, not identification or qualification of the available camera.

Ethernet connectivity or remote pan/tilt control alone does not establish a usable video stream. There is no camera discovery, automatic firmware update or camera-control integration in the hub. Existing RTSP/DirectShow/V4L2 command shapes still need physical qualification with the selected camera and input hardware.

## Installation choices

Use one fixed, wide overview covering the practice floor, walls and relevant mechanisms throughout a run. A high mounting position can reduce people blocking the robot. Keep pan, tilt and zoom fixed during a comparison session; close-up or tracking footage can be a separate optional camera. Check actual footage for motion blur, exposure changes, occlusion and missing frames while the robot moves through the useful area.

Record continuously during a practice session, independently of robot enable. The hub already uses continuous capture with closed segments; video can retain the context before a late note. Use a wired practice camera/computer network where available, independently of the robot radio. Select 1080p and an available 30 or 60 fps mode provisionally, then measure image usefulness, encoder/disk load and dropped frames. These are starting choices, not a qualified performance profile.

## Information to collect when equipment is ready

Keep a private installation record with the exact model suffix, firmware, available video outputs, power requirements from its manual, selected resolution/rate, mounting/view, recording computer and network/capture-device arrangement. For a network stream, confirm its endpoint and permission to read it. Keep credentials in the private video configuration; do not publish them or store real camera footage in Git.

Follow [recording setup](RECORDING.md) for explicit tools and private settings. First verify a short capture, actual frame PTS, original hashes, clean final-segment shutdown and visible capture health. Then measure a full practice session, input loss/reconnection, storage behavior and coexistence with the team's normal software. Do not enable automatic camera reconnection or label hardware qualified based on generated-media tests.

Clock synchronization is useful but does not establish exposure time: network/capture buffering can shift video relative to the robot. Use the [alignment contract](VIDEO_ALIGNMENT.md) and measured shared cues before treating a video frame as aligned to a log event. [Saved note/run investigation](VIDEO_CONTEXT.md) selects candidate frames under declared clock models; preview, clips and physical timing qualification remain unfinished. A camera installation alone does not complete them.
