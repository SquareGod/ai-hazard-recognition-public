export type WhepConnection = {
  close: () => Promise<void>;
  onDisconnected?: (callback: () => void) => () => void;
};

function waitForIceGathering(pc: RTCPeerConnection, signal?: AbortSignal) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise<void>((resolve, reject) => {
    const finish = () => {
      pc.removeEventListener("icegatheringstatechange", onChange);
      signal?.removeEventListener("abort", onAbort);
    };
    const onChange = () => {
      if (pc.iceGatheringState !== "complete") return;
      finish();
      resolve();
    };
    const onAbort = () => {
      finish();
      reject(new DOMException("播放请求已取消", "AbortError"));
    };
    pc.addEventListener("icegatheringstatechange", onChange);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export async function connectWhep(video: HTMLVideoElement, url: string, signal?: AbortSignal): Promise<WhepConnection> {
  const pc = new RTCPeerConnection();
  const timed = new AbortController();
  const timeout = window.setTimeout(() => timed.abort(new DOMException("WebRTC连接超时", "TimeoutError")), 6000);
  const forwardAbort = () => timed.abort(new DOMException("播放请求已取消", "AbortError"));
  signal?.addEventListener("abort", forwardAbort, { once: true });
  let sessionUrl = "";
  let closed = false;
  const disconnectedListeners = new Set<() => void>();
  const notifyDisconnected = () => {
    if (!closed && ["failed", "disconnected"].includes(pc.connectionState)) {
      disconnectedListeners.forEach((callback) => callback());
    }
  };
  pc.addEventListener("connectionstatechange", notifyDisconnected);
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.ontrack = (event) => {
    const [stream] = event.streams;
    if (stream) video.srcObject = stream;
  };

  try {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await waitForIceGathering(pc, timed.signal);
    if (!pc.localDescription?.sdp) throw new Error("浏览器未生成WebRTC协商信息");

    const response = await fetch(url, {
      method: "POST",
      headers: businessRequestHeaders("POST", { "Content-Type": "application/sdp" }),
      body: pc.localDescription.sdp,
      signal: timed.signal,
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error(`WHEP信令失败（${response.status}）`);
    const answer = await response.text();
    sessionUrl = response.headers.get("Location") ?? "";
    await pc.setRemoteDescription({ type: "answer", sdp: answer });
  } catch (error) {
    pc.close();
    video.srcObject = null;
    throw error;
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener("abort", forwardAbort);
  }

  return {
    onDisconnected: (callback) => {
      disconnectedListeners.add(callback);
      return () => disconnectedListeners.delete(callback);
    },
    close: async () => {
      if (closed) return;
      closed = true;
      disconnectedListeners.clear();
      pc.removeEventListener("connectionstatechange", notifyDisconnected);
      pc.close();
      video.srcObject = null;
      if (sessionUrl) {
        await fetch(sessionUrl, { method: "DELETE", headers: businessRequestHeaders("DELETE"), credentials: "same-origin", keepalive: true }).catch(() => undefined);
      }
    },
  };
}
import { businessRequestHeaders } from "@/lib/api";
