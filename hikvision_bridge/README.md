# Windows 海康取流桥接服务

此服务只部署在可访问 NVR SDK 端口的 Windows 电脑。它保存海康账号的本机加密副本，使用 HCNetSDK 拉取一路子码流，解码后由 FFmpeg 推送到 MediaMTX。业务后端和网页永远不接触 NVR 密码。

## 首次配置

1. 将 `HIKVISION_SDK_DIR` 指向 HCNetSDK 的“库文件”目录，`HIKVISION_FFMPEG` 指向 `ffmpeg.exe`。
2. 在本目录创建 `.env`，配置 `BRIDGE_TOKEN`、`MEDIAMTX_PUBLISH_URL`；令牌必须与业务后端 `HIKVISION_BRIDGE_TOKEN` 相同。
3. 启动：`D:\python3.11.9\python.exe -m uvicorn hikvision_bridge.app:app --host 127.0.0.1 --port 8020`

桥接 API 仅监听 `127.0.0.1`。正式部署时通过受控内网或 mTLS 连接业务后端，不能将 8020 暴露到公网。

当前实现提供完整的配置、加密保存、通道目录、共享租约和发布接口。实际 NVR 取流使用 `hikvision_bridge/sdk_runtime.py` 的 HCNetSDK 运行时；不同固件的通道枚举字段可能不同，首台设备联调时只需在该适配器内调整，不影响前端、算法或 MediaMTX 接口。
