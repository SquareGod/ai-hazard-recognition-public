"use client";

import Hls from "hls.js";
import { Maximize2, Radio, RefreshCcw } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { connectWhep, type WhepConnection } from "@/lib/whep-client";

export type ManagedStreamCamera = {
  id: string;
  name: string;
  webrtcUrl: string;
  hlsUrl: string;
};

type Props = {
  camera: ManagedStreamCamera;
  enlarged?: boolean;
  onEnlarge?: () => void;
};

type Mode = "webrtc" | "hls";
type PlayerStatus = "connecting" | "reconnecting" | "playing" | "fallback" | "hls-reconnecting" | "offline";

const statusText: Record<PlayerStatus, string> = {
  connecting: "正在连接实时画面",
  reconnecting: "正在重连（2/2）",
  playing: "实时播放中",
  fallback: "已切换兼容 HLS",
  "hls-reconnecting": "正在恢复兼容视频",
  offline: "视频暂时不可用，请检查视频源",
};

export default function ManagedStreamPlayer({ camera, enlarged = false, onEnlarge }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [mode, setMode] = useState<Mode>("webrtc");
  const [status, setStatus] = useState<PlayerStatus>("connecting");
  const [hlsRetry, setHlsRetry] = useState(0);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    const saved = window.sessionStorage.getItem(`zzy-player-mode:${camera.id}`);
    if (saved === "hls" || saved === "webrtc") setMode(saved);
  }, [camera.id]);

  useEffect(() => {
    const currentVideo = videoRef.current;
    if (!currentVideo) return;
    const videoElement: HTMLVideoElement = currentVideo;
    const controller = new AbortController();
    let cancelled = false;
    let whep: WhepConnection | null = null;
    let hls: Hls | null = null;
    let hlsRetryTimer: number | undefined;
    let reconnectTimer: number | undefined;
    let firstFrameTimer: number | undefined;
    let unsubscribeDisconnected: (() => void) | undefined;

    async function startWebRtc() {
      for (let attempt = 1; attempt <= 2 && !cancelled; attempt += 1) {
        setStatus(attempt === 1 ? "connecting" : "reconnecting");
        try {
          const firstFrame = new Promise<void>((resolve, reject) => {
            videoElement.onplaying = () => {
              if (firstFrameTimer !== undefined) window.clearTimeout(firstFrameTimer);
              resolve();
            };
            firstFrameTimer = window.setTimeout(() => reject(new Error("WebRTC首帧超时")), 6000);
          });
          void firstFrame.catch(() => undefined);
          whep = await connectWhep(videoElement, camera.webrtcUrl, controller.signal);
          await videoElement.play().catch(() => undefined);
          await firstFrame;
          if (cancelled) await whep.close();
          else {
            setStatus("playing");
            window.sessionStorage.setItem(`zzy-player-mode:${camera.id}`, "webrtc");
            unsubscribeDisconnected = whep.onDisconnected?.(() => {
              if (cancelled || reconnectTimer !== undefined) return;
              setStatus("reconnecting");
              reconnectTimer = window.setTimeout(() => setReloadKey((value) => value + 1), 1000);
            });
          }
          return;
        } catch (error) {
          if (firstFrameTimer !== undefined) window.clearTimeout(firstFrameTimer);
          videoElement.onplaying = null;
          await whep?.close();
          whep = null;
          if (cancelled || (error instanceof DOMException && error.name === "AbortError")) return;
        }
      }
      if (!cancelled) {
        setMode("hls");
        setStatus("fallback");
      }
    }

    function startHls() {
      setStatus("fallback");
      videoElement.srcObject = null;
      if (videoElement.canPlayType("application/vnd.apple.mpegurl")) {
        videoElement.src = camera.hlsUrl;
        videoElement.onerror = () => {
          if (cancelled || reconnectTimer !== undefined) return;
          setStatus("hls-reconnecting");
          reconnectTimer = window.setTimeout(() => setReloadKey((value) => value + 1), 2000);
        };
        videoElement.onplaying = () => { setStatus("playing"); window.sessionStorage.setItem(`zzy-player-mode:${camera.id}`, "hls"); };
        void videoElement.play().catch(() => setStatus("offline"));
        return;
      }
      if (!Hls.isSupported()) {
        setStatus("offline");
        return;
      }
      hls = new Hls({ enableWorker: true, lowLatencyMode: true });
      let retryCount = 0;
      const retryDelays = [1000, 2000, 4000] as const;
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal || cancelled) return;
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR && retryCount < retryDelays.length) {
          const delay = retryDelays[retryCount];
          retryCount += 1;
          setHlsRetry(retryCount);
          setStatus("hls-reconnecting");
          hlsRetryTimer = window.setTimeout(() => {
            if (!cancelled) hls?.startLoad();
          }, delay);
          return;
        }
        setStatus("offline");
        hlsRetryTimer = window.setTimeout(() => setReloadKey((value) => value + 1), 5000);
      });
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        void videoElement.play().catch(() => undefined);
      });
      videoElement.onplaying = () => { setStatus("playing"); window.sessionStorage.setItem(`zzy-player-mode:${camera.id}`, "hls"); };
      hls.loadSource(camera.hlsUrl);
      hls.attachMedia(videoElement);
    }

    if (mode === "webrtc") void startWebRtc();
    else startHls();

    return () => {
      cancelled = true;
      controller.abort();
      if (hlsRetryTimer !== undefined) window.clearTimeout(hlsRetryTimer);
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      if (firstFrameTimer !== undefined) window.clearTimeout(firstFrameTimer);
      unsubscribeDisconnected?.();
      void whep?.close();
      hls?.destroy();
      videoElement.onerror = null;
      videoElement.onplaying = null;
      videoElement.removeAttribute("src");
      videoElement.load();
    };
  }, [camera.hlsUrl, camera.webrtcUrl, mode, reloadKey]);

  return <section className={`stream-player-card managed-player ${enlarged ? "enlarged" : ""}`}>
    <header>
      <span><Radio size={15}/>{camera.name}</span>
      <div>
        <button className={mode === "webrtc" ? "active" : ""} onClick={() => setMode("webrtc")}>低延迟 WebRTC</button>
        <button className={mode === "hls" ? "active" : ""} onClick={() => setMode("hls")}>兼容 HLS</button>
        <button onClick={() => setReloadKey((value) => value + 1)} aria-label="重新加载"><RefreshCcw size={14}/></button>
        {onEnlarge && <button onClick={onEnlarge} aria-label="放大画面"><Maximize2 size={14}/></button>}
      </div>
    </header>
    <div className="managed-video-stage">
      <video ref={videoRef} autoPlay muted playsInline aria-label={`${camera.name}实时画面`} />
      {status !== "playing" && <div className={`player-state ${status}`}><span>{status === "hls-reconnecting" ? `${statusText[status]}（${hlsRetry}/3）` : statusText[status]}</span></div>}
      {status === "playing" && <span className="player-live-state">{statusText[status]}</span>}
    </div>
    <footer>默认使用 WebRTC；网络或浏览器不兼容时自动切换 HLS。摄像头账号和原始地址仅保存在后端。</footer>
  </section>;
}
