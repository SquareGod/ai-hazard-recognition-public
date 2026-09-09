from __future__ import annotations

"""Injectable HCNetSDK/PlayCtrl runtime for the Windows bridge."""

import ctypes
import base64
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from ctypes import wintypes

NET_DVR_SYSHEAD = 1
NET_DVR_STREAMDATA = 2
T_YV12 = 3


class _LocalSdkPath(ctypes.Structure):
    _fields_ = [("sPath", ctypes.c_char * 256), ("byRes", ctypes.c_byte * 128)]


class _DeviceInfoV30(ctypes.Structure):
    # Exact 80-byte layout from the vendor's Win64 Python demo. In particular,
    # start_d_channel is the physical SDK preview number for IPCamera1.
    _fields_ = [("serial", ctypes.c_ubyte * 48), ("alarm_in", ctypes.c_ubyte), ("alarm_out", ctypes.c_ubyte),
                ("disk", ctypes.c_ubyte), ("dvr_type", ctypes.c_ubyte), ("channel_count", ctypes.c_ubyte),
                ("start_channel", ctypes.c_ubyte), ("audio", ctypes.c_ubyte), ("ip_count", ctypes.c_ubyte),
                ("zero", ctypes.c_ubyte), ("main_proto", ctypes.c_ubyte), ("sub_proto", ctypes.c_ubyte),
                ("support", ctypes.c_ubyte), ("support1", ctypes.c_ubyte), ("support2", ctypes.c_ubyte),
                ("device_type", ctypes.c_ushort), ("support3", ctypes.c_ubyte), ("multi_stream_proto", ctypes.c_ubyte),
                ("start_d_channel", ctypes.c_ubyte), ("start_d_talk_channel", ctypes.c_ubyte),
                ("high_d_channel_count", ctypes.c_ubyte), ("support4", ctypes.c_ubyte), ("language", ctypes.c_ubyte),
                ("voice_in_count", ctypes.c_ubyte), ("start_voice_in", ctypes.c_ubyte), ("support5", ctypes.c_ubyte),
                ("support6", ctypes.c_ubyte), ("mirror_channel_count", ctypes.c_ubyte),
                ("start_mirror_channel", ctypes.c_ushort), ("support7", ctypes.c_ubyte), ("reserved2", ctypes.c_ubyte)]


class _PreviewInfo(ctypes.Structure):
    _fields_ = [("lChannel", ctypes.c_uint32), ("dwStreamType", ctypes.c_uint32), ("dwLinkMode", ctypes.c_uint32),
                # Keep byte-for-byte compatibility with Hikvision's official
                # Win64 Python sample (the SDK ABI uses a 32-bit window handle
                # slot in this packed preview structure).
                ("hPlayWnd", ctypes.c_uint32), ("bBlocked", ctypes.c_uint32), ("bPassbackRecord", ctypes.c_uint32),
                ("byPreviewMode", ctypes.c_ubyte), ("byStreamID", ctypes.c_ubyte * 32),
                ("byProtoType", ctypes.c_ubyte), ("byRes1", ctypes.c_ubyte), ("byVideoCodingType", ctypes.c_ubyte),
                ("dwDisplayBufNum", ctypes.c_uint32), ("byNPQMode", ctypes.c_ubyte),
                ("byRecvMetaData", ctypes.c_ubyte), ("byDataType", ctypes.c_ubyte), ("byRes", ctypes.c_ubyte * 213)]


class _FrameInfo(ctypes.Structure):
    _fields_ = [("nWidth", ctypes.c_uint32), ("nHeight", ctypes.c_uint32), ("nStamp", ctypes.c_uint32),
                ("nType", ctypes.c_uint32), ("nFrameRate", ctypes.c_uint32), ("dwFrameNum", ctypes.c_uint32)]


class _JpegPara(ctypes.Structure):
    """NET_DVR_JPEGPARA. 0xff keeps the NVR's configured picture size."""
    _fields_ = [("wPicSize", ctypes.c_ushort), ("wPicQuality", ctypes.c_ushort)]


class _IpChanInfo(ctypes.Structure):
    _fields_ = [("byEnable", ctypes.c_ubyte), ("byIPID", ctypes.c_ubyte),
                ("byChannel", ctypes.c_ubyte), ("byIPIDHigh", ctypes.c_ubyte),
                ("byTransProtocol", ctypes.c_ubyte), ("byGetStream", ctypes.c_ubyte),
                ("byRes", ctypes.c_ubyte * 30)]


class _GetStreamUnion(ctypes.Union):
    _fields_ = [("struChanInfo", _IpChanInfo), ("raw", ctypes.c_ubyte * 492)]


class _StreamMode(ctypes.Structure):
    _fields_ = [("byGetStreamType", ctypes.c_ubyte), ("byRes", ctypes.c_ubyte * 3),
                ("uGetStream", _GetStreamUnion)]


class _IpParaCfgV40(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("dwGroupNum", ctypes.c_uint32),
                ("dwAChanNum", ctypes.c_uint32), ("dwDChanNum", ctypes.c_uint32),
                ("dwStartDChan", ctypes.c_uint32), ("byAnalogChanEnable", ctypes.c_ubyte * 64),
                ("struIPDevInfo", (ctypes.c_ubyte * 296) * 64),
                ("struStreamMode", _StreamMode * 64), ("byRes2", ctypes.c_ubyte * 20)]


@dataclass(frozen=True)
class YuvFrame:
    data: bytes
    width: int
    height: int
    fps: int = 15
    timestamp: float = 0.0


class SdkSession(Protocol):
    def stop(self) -> None: ...


class SdkAdapter(Protocol):
    def list_channels(self, profile: dict[str, Any], password: str) -> list[dict[str, Any]]: ...
    def open_preview(self, profile: dict[str, Any], password: str, channel_no: int, on_data: Callable[[int, bytes], None]) -> SdkSession: ...
    def capture_jpeg(self, profile: dict[str, Any], password: str, channel_no: int) -> bytes: ...


class Publisher(Protocol):
    def start(self, path_name: str, first_frame: YuvFrame) -> None: ...
    def push(self, frame: YuvFrame) -> None: ...
    def stop(self) -> None: ...


def yv12_to_i420(data: bytes, width: int, height: int) -> bytes:
    """Convert PlayCtrl YV12 (Y, V, U) into FFmpeg yuv420p/I420 (Y, U, V)."""
    y_size = width * height
    uv_size = y_size // 4
    expected = y_size + uv_size * 2
    if width <= 0 or height <= 0 or len(data) < expected:
        raise RuntimeError("PlayCtrl输出YV12数据尺寸异常")
    y = data[:y_size]
    v = data[y_size:y_size + uv_size]
    u = data[y_size + uv_size:expected]
    return y + u + v


class BoundedFrameQueue:
    """Latest-frame queue; SDK callbacks must never block on downstream work."""
    def __init__(self, maxsize: int = 3):
        self._q: queue.Queue[YuvFrame] = queue.Queue(maxsize=maxsize)
        self.dropped = 0

    def put_latest(self, frame: YuvFrame) -> None:
        try:
            self._q.put_nowait(frame)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            self.dropped += 1
            try:
                self._q.put_nowait(frame)
            except queue.Full:
                self.dropped += 1

    def get(self, timeout: float = 0.2) -> YuvFrame:
        return self._q.get(timeout=timeout)


class FfmpegPublisher:
    """Publish decoded YUV420 frames to a MediaMTX RTSP path."""
    def __init__(self, ffmpeg_bin: str | None = None, publish_url: str | None = None, process_factory=subprocess.Popen):
        self.ffmpeg_bin = ffmpeg_bin or os.getenv("HIKVISION_FFMPEG", os.getenv("FFMPEG_BIN", "ffmpeg"))
        self.publish_url = publish_url or os.getenv("MEDIAMTX_PUBLISH_URL", "rtsp://127.0.0.1:8554")
        self._factory = process_factory
        self._proc: Any = None

    def start(self, path_name: str, first_frame: YuvFrame) -> None:
        if self._proc is not None:
            return
        url = self.publish_url.rstrip("/") + "/" + path_name.lstrip("/")
        # 弱CPU桥接机可设 HIKVISION_X264_PRESET=ultrafast 降低编码延迟；默认 veryfast。
        preset = os.getenv("HIKVISION_X264_PRESET", "veryfast")
        command = [self.ffmpeg_bin, "-hide_banner", "-loglevel", "warning", "-f", "rawvideo", "-pix_fmt", "yuv420p",
                   "-video_size", f"{first_frame.width}x{first_frame.height}", "-framerate", str(max(1, first_frame.fps)),
                   "-i", "-", "-an", "-c:v", "libx264", "-preset", preset, "-tune", "zerolatency",
                   "-pix_fmt", "yuv420p", "-g", str(max(2, first_frame.fps * 2)), "-f", "rtsp", "-rtsp_transport", "tcp", url]
        self._proc = self._factory(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.push(first_frame)

    def push(self, frame: YuvFrame) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        if self._proc.poll() is not None:
            raise RuntimeError("FFmpeg发布进程已退出，无法推送MediaMTX")
        expected = frame.width * frame.height * 3 // 2
        if len(frame.data) < expected:
            raise RuntimeError(f"PlayCtrl输出YUV数据长度异常：收到{len(frame.data)}，至少需要{expected}")
        try:
            self._proc.stdin.write(frame.data[:expected]); self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError("FFmpeg发布进程已退出，无法推送MediaMTX") from exc

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None: return
        try:
            if proc.stdin: proc.stdin.close()
        except OSError: pass
        try:
            proc.terminate(); proc.wait(timeout=3)
        except Exception:
            try: proc.kill()
            except Exception: pass


def yuv_frame_to_jpeg(frame: YuvFrame, ffmpeg_bin: str | None = None) -> bytes:
    """Encode one decoded YUV420 frame to JPEG via a short-lived ffmpeg process.

    Used by the snapshot fast path: when the channel is already being decoded
    by a live pipeline, this serves a thumbnail without touching the NVR.
    """
    bin_path = ffmpeg_bin or os.getenv("HIKVISION_FFMPEG", os.getenv("FFMPEG_BIN", "ffmpeg"))
    expected = frame.width * frame.height * 3 // 2
    command = [bin_path, "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "yuv420p",
               "-video_size", f"{frame.width}x{frame.height}", "-i", "-",
               "-frames:v", "1", "-q:v", "5", "-f", "mjpeg", "pipe:1"]
    try:
        proc = subprocess.run(command, input=frame.data[:expected], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("ffmpeg 编码预览图超时") from exc
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
        raise RuntimeError(f"ffmpeg 编码预览图失败：{detail or '无输出'}")
    return proc.stdout


class PlayCtrlDecoder:
    """Official-Demo order: GetPort -> OpenStream -> decode callback -> Play."""
    def __init__(self, playctrl: Any, on_frame: Callable[[YuvFrame], None], stream_buffer_size: int = 2 * 1024 * 1024):
        self.playctrl, self.on_frame = playctrl, on_frame
        self.stream_buffer_size = stream_buffer_size
        self.port = ctypes.c_long(-1); self._opened = False; self._decode_cb: Any = None

    def on_sdk_data(self, data_type: int, data: bytes) -> None:
        if self.port.value < 0:
            port = ctypes.c_long(-1)
            if not self.playctrl.PlayM4_GetPort(ctypes.byref(port)): raise RuntimeError("PlayCtrl获取解码端口失败")
            self.port = port
        port = int(self.port.value)
        if data_type == NET_DVR_SYSHEAD:
            self.playctrl.PlayM4_SetStreamOpenMode(port, 0)
            buf = ctypes.create_string_buffer(data)
            if not self.playctrl.PlayM4_OpenStream(port, buf, len(data), self.stream_buffer_size): raise RuntimeError("PlayCtrl打开系统头失败")
            self.playctrl.PlayM4_SetDecodeEngine(port, 0)
            callback_type = getattr(self.playctrl, "DECCBFUNWIN", None)
            if callback_type is None: raise RuntimeError("PlayCtrl缺少DECCBFUNWIN回调类型")

            def decode_cb(_port: int, p_buf: Any, size: int, p_info: Any, _user: Any, _reserved: Any) -> None:
                info = p_info.contents; width, height = int(info.nWidth), int(info.nHeight)
                if int(info.nType) != T_YV12:
                    return
                if width > 0 and height > 0:
                    raw = ctypes.string_at(p_buf, size)
                    self.on_frame(YuvFrame(yv12_to_i420(raw, width, height), width, height, max(1, int(info.nFrameRate) or 15), time.time()))
            self._decode_cb = callback_type(decode_cb)
            if not self.playctrl.PlayM4_SetDecCallBackExMend(port, self._decode_cb, None, 0, None): raise RuntimeError("PlayCtrl设置解码回调失败")
            if not self.playctrl.PlayM4_Play(port, None): raise RuntimeError("PlayCtrl开始播放失败")
            self._opened = True
        elif data_type == NET_DVR_STREAMDATA and self._opened:
            buf = ctypes.create_string_buffer(data)
            for attempt in range(5):
                if self.playctrl.PlayM4_InputData(port, buf, len(data)):
                    break
                if attempt < 4:
                    time.sleep(0.01)
            else:
                raise RuntimeError("PlayCtrl输入码流失败")

    def close(self) -> None:
        port = self.port.value
        if port < 0: return
        for name in ("PlayM4_Stop", "PlayM4_CloseStream", "PlayM4_FreePort"):
            fn = getattr(self.playctrl, name, None)
            if fn:
                try: fn(int(port))
                except Exception: pass
        self.port = ctypes.c_long(-1); self._opened = False; self._decode_cb = None


class CtypesHikvisionSdk:
    """Win64 adapter based on the vendor Python preview demo."""
    _runtime_lock = threading.RLock()
    _shared_sdk: Any = None
    _runtime_refs = 0
    # (host, port, username) -> {"sdk","user","info","password","refs"};复用登录避免每次取流都阻塞重登。
    _login_cache: dict[tuple, dict[str, Any]] = {}
    # 网络类错误码：登录会话大概率已失效（如NVR重启），此时才丢弃共享登录重新登录。
    _SESSION_INVALID_ERRORS = {7, 9, 10, 11, 12}

    def __init__(self, sdk_dir: Path | None = None):
        self.sdk_dir = sdk_dir or Path(os.getenv("HIKVISION_SDK_DIR", "")); self._callbacks: list[Any] = []

    def create_decoder(self, on_frame: Callable[[YuvFrame], None]) -> PlayCtrlDecoder:
        """Load PlayCtrl from the same vendor package as HCNetSDK."""
        if os.name != "nt":
            raise RuntimeError("PlayCtrl解码必须运行在Windows Win64环境")
        dll = self.sdk_dir / "PlayCtrl.dll"
        if not dll.exists():
            raise RuntimeError("未找到PlayCtrl.dll，请确保HIKVISION_SDK_DIR指向同一SDK库目录")
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(self.sdk_dir))
        playctrl = ctypes.WinDLL(str(dll))
        # The callback type is normally declared by the vendor Python demo.
        playctrl.DECCBFUNWIN = ctypes.WINFUNCTYPE(None, ctypes.c_long, ctypes.POINTER(ctypes.c_char), ctypes.c_long, ctypes.POINTER(_FrameInfo), ctypes.c_void_p, ctypes.c_void_p)
        return PlayCtrlDecoder(playctrl, on_frame)

    @staticmethod
    def _configure_network_timeouts(sdk: Any) -> None:
        # 公网映射高延迟时SDK默认等待极长；显式设置连接/重连参数（毫秒）。
        connect_ms = max(1000, int(os.getenv("HIKVISION_SDK_CONNECT_TIMEOUT_MS", "5000")))
        reconnect_ms = max(1000, int(os.getenv("HIKVISION_SDK_RECONNECT_MS", "10000")))
        sdk.NET_DVR_SetConnectTime.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        sdk.NET_DVR_SetConnectTime.restype = ctypes.c_bool
        sdk.NET_DVR_SetConnectTime(ctypes.c_uint32(connect_ms), ctypes.c_uint32(1))
        sdk.NET_DVR_SetReconnect.argtypes = [ctypes.c_uint32, ctypes.c_bool]
        sdk.NET_DVR_SetReconnect.restype = ctypes.c_bool
        sdk.NET_DVR_SetReconnect(ctypes.c_uint32(reconnect_ms), ctypes.c_bool(True))

    def _load(self) -> Any:
        if os.name != "nt": raise RuntimeError("HCNetSDK桥接服务必须运行在Windows Win64环境")
        if not (self.sdk_dir / "HCNetSDK.dll").exists(): raise RuntimeError("未找到HCNetSDK.dll，请检查HIKVISION_SDK_DIR")
        with self._runtime_lock:
            if self.__class__._shared_sdk is None:
                if hasattr(os, "add_dll_directory"): os.add_dll_directory(str(self.sdk_dir))
                sdk = ctypes.WinDLL(str(self.sdk_dir / "HCNetSDK.dll"))
                sdk.NET_DVR_SetSDKInitCfg.argtypes = [ctypes.c_int, ctypes.c_void_p]
                sdk.NET_DVR_SetSDKInitCfg.restype = ctypes.c_bool
                sdk_path = _LocalSdkPath(); sdk_path.sPath = str(self.sdk_dir).encode("mbcs")
                sdk.NET_DVR_SetSDKInitCfg(2, ctypes.byref(sdk_path))
                crypto = next(iter(self.sdk_dir.glob("libcrypto*-x64.dll")), None)
                ssl = next(iter(self.sdk_dir.glob("libssl*-x64.dll")), None)
                if crypto: sdk.NET_DVR_SetSDKInitCfg(3, ctypes.create_string_buffer(str(crypto).encode("mbcs")))
                if ssl: sdk.NET_DVR_SetSDKInitCfg(4, ctypes.create_string_buffer(str(ssl).encode("mbcs")))
                sdk.NET_DVR_Init.restype = ctypes.c_bool
                if not sdk.NET_DVR_Init(): raise RuntimeError(f"HCNetSDK初始化失败，错误码 {sdk.NET_DVR_GetLastError()}")
                self._configure_network_timeouts(sdk)
                self.__class__._shared_sdk = sdk
            self.__class__._runtime_refs += 1
            return self.__class__._shared_sdk

    @classmethod
    def _release_runtime(cls) -> None:
        with cls._runtime_lock:
            cls._runtime_refs = max(0, cls._runtime_refs - 1)
            if cls._runtime_refs == 0 and cls._shared_sdk is not None:
                cls._shared_sdk.NET_DVR_Cleanup()
                cls._shared_sdk = None
                cls._login_cache.clear()

    def _acquire_login(self, profile: dict[str, Any], password: str) -> tuple[Any, int, _DeviceInfoV30, tuple]:
        key = (str(profile["host"]), int(profile["port"]), str(profile["username"]))
        # 全程持有可重入锁（_load会再次进入），保证并发未命中时缓存不被互相覆盖。
        with self._runtime_lock:
            cached = self.__class__._login_cache.get(key)
            if cached is not None and cached["password"] == password:
                cached["refs"] += 1
                return cached["sdk"], cached["user"], cached["info"], key
            sdk = self._load()
            sdk.NET_DVR_Login_V30.argtypes = [ctypes.c_char_p, ctypes.c_ushort, ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(_DeviceInfoV30)]; sdk.NET_DVR_Login_V30.restype = ctypes.c_long
            info = _DeviceInfoV30(); user = sdk.NET_DVR_Login_V30(profile["host"].encode(), int(profile["port"]), profile["username"].encode(), password.encode(), ctypes.byref(info))
            if user < 0:
                error = sdk.NET_DVR_GetLastError(); self._release_runtime(); raise RuntimeError(f"HCNetSDK登录失败，错误码 {error}")
            self.__class__._login_cache[key] = {"sdk": sdk, "user": int(user), "info": info, "password": password, "refs": 1}
            return sdk, int(user), info, key

    @classmethod
    def _release_login(cls, key: tuple) -> None:
        with cls._runtime_lock:
            entry = cls._login_cache.get(key)
            if entry is None: return
            entry["refs"] -= 1
            if entry["refs"] > 0: return
            del cls._login_cache[key]
            try: entry["sdk"].NET_DVR_Logout(entry["user"])
            except Exception: pass
            cls._release_runtime()

    @classmethod
    def _drop_login(cls, key: tuple) -> None:
        """Force-logout a stale shared login (e.g. after an NVR restart)."""
        with cls._runtime_lock:
            entry = cls._login_cache.pop(key, None)
            if entry is None: return
            try: entry["sdk"].NET_DVR_Logout(entry["user"])
            except Exception: pass
            cls._release_runtime()

    def list_channels(self, profile: dict[str, Any], password: str) -> list[dict[str, Any]]:
        sdk, user, info, key = self._acquire_login(profile, password)
        try:
            cfg = _IpParaCfgV40(); cfg.dwSize = ctypes.sizeof(cfg); returned = ctypes.c_uint32(0)
            sdk.NET_DVR_GetDVRConfig.argtypes = [ctypes.c_long, ctypes.c_uint32, ctypes.c_long, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
            sdk.NET_DVR_GetDVRConfig.restype = ctypes.c_bool
            if sdk.NET_DVR_GetDVRConfig(user, 1062, 0, ctypes.byref(cfg), ctypes.sizeof(cfg), ctypes.byref(returned)):
                digital_count = min(int(cfg.dwDChanNum), len(cfg.struStreamMode))
                # Several NVR families report dwStartDChan/byStartDChan as 1
                # while reserving the first byChanNum SDK channel numbers for
                # analog inputs. Preview therefore starts after that reserved
                # block (commonly channel 33 for IPCamera1).
                physical_start = max(int(cfg.dwStartDChan), int(info.start_d_channel),
                                     int(info.start_channel) + int(info.channel_count))
                channels = []
                for index in range(digital_count):
                    number = physical_start + index
                    online = bool(cfg.struStreamMode[index].uGetStream.struChanInfo.byEnable)
                    channels.append({"channel_no": number, "display_no": index + 1, "name": f"IPCamera{index + 1}", "online": online})
                if channels:
                    return channels
            analog, ip_count = int(info.channel_count) & 0xFF, int(info.ip_count) & 0xFF; start = (int(info.start_channel) & 0xFF) or 1; count = max(analog + ip_count, analog)
            if count <= 0: raise RuntimeError("NVR登录成功，但未返回可用通道数")
            return [{"channel_no": start + i, "name": f"IPCamera{start + i}", "online": None} for i in range(count)]
        finally:
            self._release_login(key)

    def open_preview(self, profile: dict[str, Any], password: str, channel_no: int, on_data: Callable[[int, bytes], None]) -> SdkSession:
        sdk, user, _, key = self._acquire_login(profile, password); cb_type = ctypes.WINFUNCTYPE(None, ctypes.c_long, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_ulong, ctypes.c_void_p)
        def real_data(_handle: int, data_type: int, ptr: Any, size: int, _user: Any) -> None:
            if ptr and size:
                on_data(int(data_type), ctypes.string_at(ptr, size))
        callback = cb_type(real_data); self._callbacks.append(callback); preview = _PreviewInfo(); preview.lChannel = channel_no
        preview.dwStreamType = int(profile.get("_stream_type", 1)); preview.dwLinkMode = 0; preview.hPlayWnd = 0; preview.bBlocked = 1
        sdk.NET_DVR_RealPlay_V40.argtypes = [ctypes.c_long, ctypes.POINTER(_PreviewInfo), cb_type, ctypes.c_void_p]; sdk.NET_DVR_RealPlay_V40.restype = ctypes.c_long
        handle = sdk.NET_DVR_RealPlay_V40(user, ctypes.byref(preview), callback, None)
        if handle < 0:
            error = sdk.NET_DVR_GetLastError()
            if error in self._SESSION_INVALID_ERRORS:
                # 共享登录疑似失效（NVR重启/链路中断）：丢弃后用全新登录重试一次。
                self._drop_login(key)
                sdk, user, _, key = self._acquire_login(profile, password)
                handle = sdk.NET_DVR_RealPlay_V40(user, ctypes.byref(preview), callback, None)
            if handle < 0:
                error = sdk.NET_DVR_GetLastError(); self._release_login(key); raise RuntimeError(f"NET_DVR_RealPlay_V40失败，错误码 {error}")
        return _SdkPreviewSession(sdk, user, int(handle), callback, self._callbacks, key, self._release_login)

    def capture_jpeg(self, profile: dict[str, Any], password: str, channel_no: int) -> bytes:
        """Read a single JPEG through the existing SDK login without opening a persistent preview."""
        sdk, user, _, key = self._acquire_login(profile, password)
        try:
            if not hasattr(sdk, "NET_DVR_CaptureJPEGPicture_NEW"):
                raise RuntimeError("当前HCNetSDK不支持通道抓图")
            sdk.NET_DVR_CaptureJPEGPicture_NEW.argtypes = [ctypes.c_long, ctypes.c_long, ctypes.POINTER(_JpegPara), ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
            sdk.NET_DVR_CaptureJPEGPicture_NEW.restype = ctypes.c_bool
            # Large enough for a typical 4K JPEG while still bounded.
            buffer = ctypes.create_string_buffer(8 * 1024 * 1024)
            attempts: list[str] = []
            # 0xFF=沿用NVR自身配置；2=704x576（部分固件拒绝0xFF时的兜底）。
            sizes = [0xFF, 2]
            channels = [int(channel_no)]
            # 与预览回退对称：老NVR的数字通道从33起（1-32为模拟保留），同步目录
            # 给出的是1起编号，抓图需要+32；反之亦然。双向都试。
            if channel_no > 32:
                channels.append(int(channel_no) - 32)
            else:
                channels.append(int(channel_no) + 32)
            for channel in channels:
                for size in sizes:
                    para = _JpegPara(size, 1)
                    returned = ctypes.c_uint32(0)
                    ok = sdk.NET_DVR_CaptureJPEGPicture_NEW(user, channel, ctypes.byref(para), buffer, len(buffer), ctypes.byref(returned))
                    if ok and returned.value > 0:
                        return bytes(buffer.raw[:returned.value])
                    attempts.append(f"通道{channel}/尺寸{size}:错误码 {sdk.NET_DVR_GetLastError()}")
            raise RuntimeError("NET_DVR_CaptureJPEGPicture_NEW失败（" + "；".join(attempts) + "）。若持续失败，该通道可能未接入摄像头")
        finally:
            self._release_login(key)


class _SdkPreviewSession:
    def __init__(self, sdk: Any, user: int, handle: int, callback: Any, callbacks: list[Any], login_key: tuple, release_login: Callable[[tuple], None]): self.sdk, self.user, self.handle, self.callback, self.callbacks, self.login_key, self.release_login, self._stopped = sdk, user, handle, callback, callbacks, login_key, release_login, False
    def stop(self) -> None:
        if self._stopped: return
        self._stopped = True
        try: self.sdk.NET_DVR_StopRealPlay(self.handle)
        finally:
            # 登录会话按NVR复用，停止预览只归还引用，不注销共享登录。
            self.release_login(self.login_key)
            try: self.callbacks.remove(self.callback)
            except ValueError: pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect_secret(secret_file: Path, value: str) -> None:
    """Encrypt a secret with Windows DPAPI for the current user, without child processes."""
    if os.name != "nt":
        raise RuntimeError("NVR密码加密目前仅支持Windows桥接机")
    plain, plain_buffer = _blob(value.encode("utf-8"))
    entropy, entropy_buffer = _blob(b"zhizhuyun-hikvision-bridge-v1")
    encrypted = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptProtectData(ctypes.byref(plain), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(encrypted)):
        raise RuntimeError("Windows DPAPI加密NVR密码失败")
    try:
        payload = ctypes.string_at(encrypted.pbData, encrypted.cbData)
        secret_file.write_text(base64.b64encode(payload).decode("ascii"), encoding="ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(encrypted.pbData)
        del plain_buffer, entropy_buffer


def _unprotect(secret_file: Path) -> str:
    if os.name != "nt":
        raise RuntimeError("NVR密码解密目前仅支持Windows桥接机")
    try:
        encoded = secret_file.read_text(encoding="ascii").strip()
        encrypted_bytes = base64.b64decode(encoded, validate=True)
    except (OSError, ValueError) as exc:
        raise RuntimeError("本机NVR密码密文损坏，请重新配置") from exc
    encrypted, encrypted_buffer = _blob(encrypted_bytes)
    entropy, entropy_buffer = _blob(b"zhizhuyun-hikvision-bridge-v1")
    plain = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptUnprotectData(ctypes.byref(encrypted), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(plain)):
        raise RuntimeError("无法解密NVR密码，请在当前Windows用户下重新配置")
    try:
        return ctypes.string_at(plain.pbData, plain.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(plain.pbData)
        del encrypted_buffer, entropy_buffer


def list_channels(profile: dict[str, Any], secret_file: Path, sdk: SdkAdapter | None = None) -> list[dict[str, Any]]:
    """Compatibility wrapper used by the HTTP layer and easy to replace in tests."""
    if not secret_file.exists():
        raise RuntimeError("本机加密的NVR密码不存在，请重新配置该NVR")
    adapter = sdk or CtypesHikvisionSdk()
    return adapter.list_channels(profile, _unprotect(secret_file))


class FakeSdkSession:
    def __init__(self):
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakeSdk:
    """A non-networking SDK substitute for bridge tests and local demos."""
    def __init__(self):
        self.opens = 0
        self.sessions: list[FakeSdkSession] = []

    def list_channels(self, profile: dict[str, Any], password: str) -> list[dict[str, Any]]:
        return [{"channel_no": 1, "name": "FakeChannel1", "online": True}]

    def open_preview(self, profile: dict[str, Any], password: str, channel_no: int, on_data: Callable[[int, bytes], None]) -> SdkSession:
        self.opens += 1
        session = FakeSdkSession()
        self.sessions.append(session)
        return session

    def capture_jpeg(self, profile: dict[str, Any], password: str, channel_no: int) -> bytes:
        return b"\xff\xd8fake-jpeg\xff\xd9"


class FakePublisher:
    def __init__(self):
        self.started: list[tuple[str, YuvFrame]] = []
        self.frames: list[YuvFrame] = []
        self.stopped = False

    def start(self, path_name: str, first_frame: YuvFrame) -> None:
        self.started.append((path_name, first_frame))

    def push(self, frame: YuvFrame) -> None:
        self.frames.append(frame)

    def stop(self) -> None:
        self.stopped = True


class StreamPipeline:
    """Owns one SDK preview and one publisher, shared by all lease owners."""
    def __init__(self, profile: dict[str, Any], password: str, channel_no: int, path_name: str,
                 sdk: SdkAdapter, publisher: Publisher,
                 decoder_factory: Callable[[Callable[[YuvFrame], None]], PlayCtrlDecoder],
                 frame_timeout: float | None = None):
        self.profile, self.password, self.channel_no, self.path_name = profile, password, channel_no, path_name
        self.sdk, self.publisher, self.decoder_factory = sdk, publisher, decoder_factory
        self.frame_timeout = frame_timeout
        self.queue = BoundedFrameQueue(3); self.state = "starting"
        self.received_chunks = self.decoded_frames = self.published_frames = self.dropped_frames = 0
        self.last_frame_at: float | None = None; self.last_error: str | None = None
        self.last_frame: YuvFrame | None = None
        self._stop = threading.Event(); self._session: SdkSession | None = None
        self._runtime_error = threading.Event()
        self._decoder: PlayCtrlDecoder | None = None; self._worker: threading.Thread | None = None
        self._publisher_started = False
        self.stream_type = 1
        self.effective_channel_no = channel_no

    def start(self) -> None:
        self._worker = threading.Thread(target=self._run, name=f"hikvision-{self.channel_no}", daemon=True)
        self._worker.start()

    def _on_frame(self, frame: YuvFrame) -> None:
        self.decoded_frames += 1; self.last_frame_at = time.time(); self.queue.put_latest(frame)

    def _on_data(self, data_type: int, data: bytes) -> None:
        self.received_chunks += 1
        try:
            if self._decoder is not None: self._decoder.on_sdk_data(data_type, data)
        except Exception as exc:
            self.last_error = str(exc); self.state = "reconnecting"; self._runtime_error.set()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            self._decoder = None; self._session = None; self._publisher_started = False; self._runtime_error.clear()
            attempt_chunks = self.received_chunks
            try:
                self._decoder = self.decoder_factory(self._on_frame)
                preview_profile = {**self.profile, "_stream_type": self.stream_type}
                self._session = self.sdk.open_preview(preview_profile, self.password, self.effective_channel_no, self._on_data)
                self.state = "streaming"; backoff = 1.0; opened_at = time.time()
                timeout = self.frame_timeout if self.frame_timeout is not None else float(os.getenv("HIKVISION_FRAME_TIMEOUT_SECONDS", "10"))
                while not self._stop.is_set():
                    if self._runtime_error.is_set():
                        raise RuntimeError(self.last_error or "海康解码发生错误")
                    try: frame = self.queue.get(0.2)
                    except queue.Empty:
                        if timeout > 0 and time.time() - (self.last_frame_at or opened_at) >= timeout:
                            raise RuntimeError(f"连续{int(timeout)}秒未收到可解码画面，正在重连")
                        continue
                    if not self._publisher_started:
                        self.publisher.start(self.path_name, frame); self._publisher_started = True
                    else: self.publisher.push(frame)
                    self.last_frame = frame
                    self.published_frames += 1; self.dropped_frames = self.queue.dropped
                    self.last_error = None
            except Exception as exc:
                self.last_error = str(exc); self.state = "reconnecting"
                # Some NVR channels do not expose a sub-stream even though the
                # channel itself is online. Prefer sub-stream, then fall back to
                # main stream only when no bytes at all were returned.
                if self.stream_type == 1 and self.received_chunks == attempt_chunks:
                    self.stream_type = 0
                elif self.received_chunks == attempt_chunks and self.effective_channel_no <= 32:
                    # Older and hybrid NVRs may list IPCamera1 as logical
                    # channel 1 but require SDK preview channel 33. Try the
                    # standard 32-channel analog reservation automatically.
                    self.effective_channel_no += 32
                    self.stream_type = 1
            finally:
                if self._decoder: self._decoder.close()
                if self._session: self._session.stop()
                self.publisher.stop()
                self._decoder = None; self._session = None
            if self._stop.is_set(): break
            # 退避上限10秒：公网弱网下30秒意味着画面长时间黑屏，宁可更快重试。
            self._stop.wait(backoff); backoff = min(backoff * 2, 10.0)
        self.state = "stopped"

    def stop(self) -> None:
        self._stop.set()
        if self._worker and self._worker is not threading.current_thread(): self._worker.join(timeout=5)
        self.password = ""
        self.last_frame = None
