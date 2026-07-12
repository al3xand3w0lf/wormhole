"""Streaming server — receives a live, framed byte stream from field devices.

The device is a thin pipe: it tees a raw binary stream (u-blox GNSS: UBX + RTCM3)
straight off its receiver and interleaves its own private frames (sensor readings,
identification, heartbeat, CLI) over one persistent TCP socket per station. All
protocol intelligence lives here on the server.
"""

__version__ = "0.1.0"
